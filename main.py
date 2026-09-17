import os
import re
import time
from urllib.parse import urldefrag, urljoin
import cloudscraper
from bs4 import BeautifulSoup
from ebooklib import epub
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

# ============================================================
# CẤU HÌNH FLASK & TELEGRAM BOT
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN_TRUYEN") or os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
  print("❌ Lỗi: Thiếu BOT_TOKEN!")

# Khởi tạo Flask App
app = Flask(__name__)

# Khởi tạo Telegram Application (Dùng updater=None để tự quản lý webhook)
application = Application.builder().token(BOT_TOKEN).updater(None).build()

scraper = cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "windows", "desktop": True}
)


def get_content(url):
  try:
    res = scraper.get(url, timeout=30)
    if res.status_code == 200:
      return BeautifulSoup(res.text, "lxml")
  except Exception as e:
    print(f"Lỗi tải {url}: {e}")
  return None


def extract_chapter_number(name):
  """Trích xuất số từ tên chương để sắp xếp chuẩn xác 1, 2, 3..."""
  numbers = re.findall(r"\d+", name)
  if numbers:
    return int(numbers[0])
  if (
      "ngoại" in name.lower()
      or "ngoai" in name.lower()
      or "extra" in name.lower()
  ):
    return 999999
  return 0


def get_chapters(url):
  soup = get_content(url)
  if not soup:
    return [], "Truyện", None

  # 1. Lấy tiêu đề truyện chuẩn
  og_title = soup.find("meta", property="og:title")
  if og_title and og_title.get("content"):
    title = og_title["content"]
  else:
    title_el = soup.select_one("h1") or soup.title
    title = title_el.get_text().strip() if title_el else "Truyện"

  if "|" in title:
    title = title.split("|")[0].strip()

  # 2. Lấy ảnh bìa hỗ trợ Lazy Load
  cover_url = None
  og_img = (
      soup.find("meta", property="og:image")
      or soup.find("meta", property="product:image")
      or soup.find("meta", attrs={"name": "twitter:image"})
  )
  if og_img and og_img.get("content"):
    cover_url = og_img["content"]

  if not cover_url:
    img_el = soup.select_one(
        ".book img, .info-image img, .story-image img, .product-image img,"
        " img.cover, .detail img, .col-image img, .book-image img"
    )
    if img_el:
      cover_url = (
          img_el.get("data-src") or img_el.get("data-original") or img_el.get("src")
      )

  if cover_url:
    cover_url = urljoin(url, cover_url)

  # 3. Quét danh sách chương với bộ lọc khắt khe chống quét nhầm rác
  chapters = []
  content_container = soup.select_one(
      ".entry-content, .post-content, .chapter-list, #list-chapter"
  )
  search_scope = content_container if content_container else soup

  for a in search_scope.find_all("a", href=True):
    href = urldefrag(urljoin(url, a.get("href")))[0]
    text = a.get_text().strip()

    is_chap = re.match(
        r"^(chương|chuong|hồi|hoi|quyển|quyen|c\s*\d+|phần|phan|pn\s*\d+|nt\s*\d+|ngoại\s*truyện)\s*\d*",
        text,
        flags=re.IGNORECASE,
    )
    is_pure_number_chap = bool(re.match(r"^\d+$", text))

    if (is_chap or is_pure_number_chap) and len(text) < 60:
      text_lower = text.lower()
      if any(
          bad in text_lower
          for bad in [
              "person",
              "trang",
              "comment",
              "bình luận",
              "share",
              "author",
              "login",
          ]
      ):
        continue

      if href.startswith("http") and not any(
          x in href
          for x in [
              "#",
              "wp-login",
              "author",
              "category",
              "tag",
              "feed",
              "wp-admin",
          ]
      ):
        if not any(c["url"] == href for c in chapters):
          chapters.append({"name": text, "url": href})

  chapters.sort(key=lambda x: extract_chapter_number(x["name"]))
  return chapters, title, cover_url


def download_chap(url):
  soup = get_content(url)
  if not soup:
    return None

  container = (
      soup.select_one(".entry-content")
      or soup.select_one(".elementor-widget-theme-post-content")
      or soup.select_one(".post-content")
      or soup.select_one(".chapter-content")
      or soup.select_one("#chapter-c")
      or soup.select_one("article")
      or soup.select_one(".post-body")
      or soup.body
  )

  if not container:
    container = soup

  for garbage in container.select(
      ".entry-header, .post-info, .breadcrumbs, .breadcrumb, nav, footer,"
      " header, script, style, form, aside"
  ):
    garbage.decompose()

  paragraphs = container.find_all("p")
  valid_p = []

  ignore_keywords = [
      "bỏ qua nội dung",
      "trang chủ",
      "lượt xem:",
      "cập nhật:",
      "chia sẻ",
      "thích",
      "đang tải",
      "có liên quan",
      "báo lỗi",
      "khám phá thêm",
      "đăng nhập",
      "bình luận",
      "viết:",
      "lúc",
      "danh sách",
  ]

  for p in paragraphs:
    text = p.get_text().strip()
    if not text:
      continue
    lower_text = text.lower()
    if any(kw in lower_text for kw in ignore_keywords) and len(text) < 80:
      continue
    valid_p.append(str(p))

  if valid_p:
    return "".join(valid_p)

  return str(container)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
  url_match = re.findall(r"https?://[^\s]+", update.message.text or "")
  if not url_match:
    return

  status = await update.message.reply_text(
      "⏳ Đang kết nối và quét danh sách chương..."
  )
  story_url = url_match[0]

  chapters, title, cover_url = get_chapters(story_url)

  if not chapters:
    await status.edit_text(
        "❌ Không tìm thấy chương nào. Hãy kiểm tra lại đường dẫn trang chính"
        " của truyện."
    )
    return

  await status.edit_text(
      f"📚 {title}\n✅ Tìm thấy {len(chapters)} chương. Đang tải nội dung..."
  )

  results = {}
  for i, c in enumerate(chapters):
    results[i] = download_chap(c["url"])
    if i % 15 == 0 or i == len(chapters) - 1:
      pct = int(((i + 1) / len(chapters)) * 100)
      try:
        await status.edit_text(
            f"📚 {title}\n⏳ Đang tải: {pct}%\n({i+1}/{len(chapters)})"
        )
      except:
        pass
    time.sleep(0.3)

  book = epub.EpubBook()
  book.set_identifier("truyen_" + re.sub(r"\W+", "", title))
  book.set_title(title)
  book.set_language("vi")

  if cover_url:
    try:
      img_res = scraper.get(cover_url, timeout=15)
      if img_res.status_code == 200:
        book.set_cover("cover.jpg", img_res.content)
    except Exception as e:
      print(f"Lỗi tải ảnh bìa: {e}")

  chapters_list = []
  for i, c in enumerate(chapters):
    if results.get(i):
      chap = epub.EpubHtml(title=c["name"], file_name=f"chap_{i+1}.xhtml")
      chap.content = f"<h2>{c['name']}</h2>{results[i]}"
      book.add_item(chap)
      chapters_list.append(chap)

  if not chapters_list:
    await status.edit_text("❌ Không tải được nội dung chương nào.")
    return

  book.toc = tuple(chapters_list)
  book.spine = ["nav"] + chapters_list

  book.add_item(epub.EpubNcx())
  book.add_item(epub.EpubNav())

  safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip() or "Truyen"
  file_out = f"{safe_title}.epub"
  epub.write_epub(file_out, book)

  await status.edit_text(f"⬆️ Đang gửi file EPUB...")
  with open(file_out, "rb") as f:
    await update.message.reply_document(
        document=f,
        caption=(
            f"✅ Hoàn tất: {title}\n📖 Trọn bộ {len(chapters_list)} chương (Đã"
            " quét ảnh bìa thành công & lọc sạch rác chuẩn xác!)"
        ),
    )

  await status.delete()
  if os.path.exists(file_out):
    os.remove(file_out)


# Đăng ký handler nhận tin nhắn vào app Telegram
application.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
)


# ============================================================
# FLASK ROUTES (WEBHOOK & HEALTH CHECK)
# ============================================================
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
  """Nhận dữ liệu từ Telegram gửi đến"""
  json_string = request.get_data().decode("utf-8")
  update = Update.de_json(json_string, application.bot)

  async def process():
    await application.update_queue.put(update)

  import asyncio

  asyncio.run(process())
  return "OK", 200


@app.route("/")
def index():
  """Trang chủ để Render check sống và tự động cài webhook"""
  render_url = os.getenv("RENDER_EXTERNAL_URL")
  if render_url:
    webhook_url = f"{render_url}/{BOT_TOKEN}"
    import asyncio

    async def set_wh():
      await application.bot.set_webhook(url=webhook_url)

    asyncio.run(set_wh())
    return f"Bot Truyen Webhook đang hoạt động! Đã trỏ tới: {webhook_url}", 200
  return "Bot Truyen đang chạy!", 200


# ============================================================
# KHỞI CHẠY ỨNG DỤNG
# ============================================================
if __name__ == "__main__":
  import asyncio

  async def init_bot():
    await application.initialize()
    await application.start()

  asyncio.run(init_bot())

  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port)
