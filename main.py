import asyncio
import os
import re
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urljoin, urldefrag
import requests
from bs4 import BeautifulSoup
from ebooklib import epub
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

# ============================================================
# WEB SERVER TƯƠNG THÍCH UPTIMEROBOT (GET & HEAD) CHO RENDER
# ============================================================
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write("Bot Truyen đang hoạt động!".encode('utf-8'))

    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()

    def log_message(self, format, *args):
        return

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    server.serve_forever()

# ============================================================
# CẤU HÌNH BOT & SESSION
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN_TRUYEN") or os.getenv("BOT_TOKEN")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}

session = requests.Session()
session.headers.update(HEADERS)

def get_content(url):
    try:
        res = session.get(url, timeout=25)
        if res.status_code == 200:
            return BeautifulSoup(res.text, "lxml")
    except Exception as e:
        print(f"Lỗi tải {url}: {e}")
    return None

def get_chapters(url):
    soup = get_content(url)
    if not soup: return [], "Truyện", None
    
    # 1. Lấy tiêu đề truyện
    title_el = soup.select_one("h1") or soup.select_one(".title") or soup.title
    title = title_el.get_text().strip() if title_el else "Truyện"
    if "|" in title:
        title = title.split('|')[0].strip()
        
    # 2. Lấy ảnh bìa
    cover_url = None
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        cover_url = og_img["content"]
    else:
        img_tag = soup.select_one(".book img") or soup.select_one(".truyen-info img") or soup.find("img")
        if img_tag and img_tag.get('src'):
            cover_url = img_tag['src']

    # 3. Quét danh sách chương đa dạng
    chapters = []
    for a in soup.find_all("a", href=True):
        href = urldefrag(urljoin(url, a.get("href")))[0]
        text = a.get_text().strip()
        
        is_chap = re.match(r"^(chương|chuong|hồi|hoi|quyển|quyen|c\s*\d+|\d+|pn\s*\d+|nt\s*\d+)", text, flags=re.IGNORECASE)
        
        if is_chap and len(text) < 60:
            if any(k in href for k in ["chuong-", "hoi-", "chapter-", "/truyen/"]):
                if not any(c['url'] == href for c in chapters):
                    chapters.append({"name": text, "url": href})

    return chapters, title, cover_url

def download_chap(url):
    soup = get_content(url)
    if not soup: return None
    
    # Mở rộng bộ chọn tìm kiếm khung chứa nội dung truyện (hỗ trợ cả các trang blog/web tùy biến cao)
    content = (
        soup.select_one(".entry-content") or 
        soup.select_one(".post-content") or 
        soup.select_one(".chapter-content") or 
        soup.select_one("#chapter-c") or 
        soup.select_one("article") or
        soup.select_one(".post-body") or
        soup.select_one(".entry")
    )
    
    if not content:
        # Fallback cuối cùng nếu không tìm thấy thẻ đặc thù: lấy toàn bộ body nhưng loại bỏ rác
        content = soup.body
        if not content: return None

    # Xóa các thẻ rác, quảng cáo, điều hướng, và đặc biệt là KHUNG BÌNH LUẬN (comments)
    for t in content.find_all(["script", "style", "ins", "button", "iframe", "form", "nav", "comments", "aside", "footer"]): 
        t.decompose()
        
    # Xóa các khối div/section chứa bình luận thường gặp
    for c_div in content.find_all(id=re.compile("comment", re.I)) + content.find_all(class_=re.compile("comment|social|share|nav|footer", re.I)):
        c_div.decompose()

    # Dọn sạch các dòng chữ thừa, nút chuyển chương, chia sẻ mạng xã hội, thông tin đăng nhập bình luận
    keywords_to_remove = [
        "chương trước", "chương sau", "chia sẻ", "thích", "đang tải", 
        "có liên quan", "báo lỗi", "khám phá thêm", "truyện chỉ đăng tại", 
        "edit by", "đăng nhập để bình luận", "viết:", "lúc"
    ]
    
    for element in content.find_all(["div", "p", "span", "li"]):
        text = element.get_text().strip().lower()
        if any(kw in text for kw in keywords_to_remove):
            element.decompose()
            
    return str(content)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url_match = re.findall(r"https?://[^\s]+", update.message.text or "")
    if not url_match: return
    
    status = await update.message.reply_text("⏳ Đang quét danh sách chương từ trang truyện...")
    story_url = url_match[0]
    
    chapters, title, cover_url = get_chapters(story_url)
    
    if not chapters:
        await status.edit_text("❌ Không tìm thấy chương nào. Hãy kiểm tra lại đường dẫn trang chính của truyện.")
        return

    await status.edit_text(f"📚 {title}\n✅ Tìm thấy {len(chapters)} chương. Đang tiến hành tải nội dung...")

    results = {}
    for i, c in enumerate(chapters):
        results[i] = download_chap(c["url"])
        if i % 15 == 0 or i == len(chapters) - 1:
            pct = int(((i + 1) / len(chapters)) * 100)
            try:
                await status.edit_text(f"📚 {title}\n⏳ Đang tải: {pct}%\n({i+1}/{len(chapters)})")
            except:
                pass
        time.sleep(0.2)

    # Tạo file EPUB chuẩn Kindle
    book = epub.EpubBook()
    book.set_identifier('truyen_' + re.sub(r'\W+', '', title))
    book.set_title(title)
    book.set_language('vi')

    # Thêm ảnh bìa
    if cover_url:
        try:
            full_cover_url = cover_url if cover_url.startswith("http") else urljoin(story_url, cover_url)
            img_res = session.get(full_cover_url, timeout=15)
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

    # Cấu hình Mục lục (TOC) chuẩn Kindle
    book.toc = tuple(chapters_list)
    book.spine = ['nav'] + chapters_list

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip() or "Truyen"
    file_out = f"{safe_title}.epub"
    epub.write_epub(file_out, book)

    await status.edit_text(f"⬆️ Đang gửi file EPUB...")
    with open(file_out, "rb") as f:
        await update.message.reply_document(
            document=f, 
            caption=f"✅ Hoàn tất: {title}\n📖 Trọn bộ {len(chapters_list)} chương + Đã lọc sạch bình luận, có Ảnh bìa & Mục lục chuẩn Kindle!"
        )
    
    await status.delete()
    if os.path.exists(file_out):
        os.remove(file_out)

def main():
    threading.Thread(target=run_web_server, daemon=True).start()
    
    if not BOT_TOKEN:
        print("❌ Lỗi: Thiếu BOT_TOKEN!")
        return
        
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
