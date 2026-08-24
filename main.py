import os
import re
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import cloudscraper
from bs4 import BeautifulSoup
from ebooklib import epub
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

# ============================================================
# 🌐 WEB SERVER (HỖ TRỢ CẢ GET & HEAD CHO RENDER & UPTIMEROBOT)
# ============================================================
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write("Bot Truyện đang hoạt động hoàn hảo!".encode('utf-8'))

    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()

    def log_message(self, format, *args):
        return  # Tắt log rườm rà

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    server.serve_forever()

# ============================================================
# ⚙️ CẤU HÌNH SCRAPER
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN_TRUYENFULL") or os.getenv("BOT_TOKEN")

scraper = cloudscraper.create_scraper(
    browser={
        'browser': 'chrome',
        'platform': 'windows',
        'desktop': True
    }
)

def get_soup(url):
    try:
        response = scraper.get(url, timeout=30)
        if response.status_code == 200:
            return BeautifulSoup(response.text, "html.parser")
    except Exception as e:
        print(f"Lỗi kết nối {url}: {e}")
    return None

def download_chapter(url):
    soup = get_soup(url)
    if not soup: return None
    
    # Tìm vùng nội dung chương phổ biến
    content = (
        soup.select_one(".chapter-content") or 
        soup.select_one("#chapter-c") or 
        soup.select_one(".entry-content") or 
        soup.select_one(".post-content") or 
        soup.select_one("article")
    )
    if not content: return None
    
    # Xóa các thẻ rác
    for tag in content.find_all(["script", "style", "ins", "iframe", "button", "form", "nav"]):
        tag.decompose()
        
    # Lọc bỏ các đoạn văn chứa từ khóa rác (chia sẻ, bản quyền editor,...)
    keywords_to_remove = [
        "chia sẻ", "share", "thích", "đang tải", "có liên quan", 
        "chương trước", "chương sau", "truyện chỉ đăng tại", "edit by"
    ]
    
    for element in content.find_all(["div", "p", "span"]):
        text = element.get_text().strip().lower()
        if any(kw in text for kw in keywords_to_remove):
            element.decompose()
            
    return str(content)

# ============================================================
# 📥 XỬ LÝ TIN NHẮN TỪ TELEGRAM
# ============================================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url_match = re.findall(r"https?://[^\s]+", update.message.text or "")
    if not url_match: return
    
    status = await update.message.reply_text("⏳ Đang kết nối và quét danh sách chương...")
    story_url = url_match[0]
    
    main_soup = get_soup(story_url)
    if not main_soup:
        await status.edit_text("❌ Không thể kết nối tới đường dẫn này. Trang web có thể đang chặn mạnh.")
        return
        
    # Lấy tiêu đề truyện
    title_el = main_soup.select_one("h1") or main_soup.select_one(".title") or main_soup.title
    title = title_el.get_text().strip() if title_el else "Truyện"
    if "|" in title:
        title = title.split('|')[0].strip()
        
    # Lấy ảnh bìa
    cover_url = None
    og_img = main_soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        cover_url = og_img["content"]
    else:
        img_tag = main_soup.select_one(".book img") or main_soup.select_one("article img")
        if img_tag and img_tag.get('src'):
            cover_url = img_tag['src']

    # Lấy danh sách chương
    links = []
    chapter_tags = main_soup.select("#list-chapter a, .list-chapter a, .chapter-list a, .entry-content a, article a")
    
    for a in chapter_tags:
        href = a.get('href', '')
        text = a.get_text().strip()
        if href and text:
            is_chapter = re.match(r"^(\d+|PN\s*\d+|NT\s*\d+|chương|chuong|hồi|hoi|quyển|quyen|c\s*\d+)", text, flags=re.IGNORECASE)
            if is_chapter or "chuong-" in href or "chap-" in href:
                domain = "https://" + story_url.split('/')[2]
                full_url = href if href.startswith("http") else domain + href
                if not any(l['url'] == full_url for l in links):
                    links.append({"name": text, "url": full_url})
            
    if not links:
        await status.edit_text("❌ Không tìm thấy danh sách chương tự động. Hãy đảm bảo bạn gửi link trang mục lục chính.")
        return
        
    await status.edit_text(f"📚 {title}\n✅ Tìm thấy {len(links)} chương. Đang tiến hành tải nội dung...")
    
    # Tạo sách EPUB
    book = epub.EpubBook()
    book.set_identifier('epub_' + re.sub(r'\W+', '', title))
    book.set_title(title)
    book.set_language('vi')
    
    # Thêm ảnh bìa nếu có
    if cover_url:
        try:
            domain = "https://" + story_url.split('/')[2]
            full_cover_url = cover_url if cover_url.startswith("http") else domain + cover_url
            img_data = scraper.get(full_cover_url, timeout=15).content
            book.set_cover("cover.jpg", img_data)
        except Exception as e:
            print(f"Không tải được ảnh bìa: {e}")

    chapters_list = []
    success_count = 0
    
    for i, item in enumerate(links):
        content = download_chapter(item['url'])
        if content:
            chap = epub.EpubHtml(title=item['name'], file_name=f"chap_{i+1}.xhtml")
            chap.content = f"<h2>{item['name']}</h2>{content}"
            book.add_item(chap)
            chapters_list.append(chap)
            success_count += 1
        
        if i % 15 == 0 or i == len(links) - 1:
            pct = int((i / len(links)) * 100)
            try:
                await status.edit_text(f"📚 {title}\n⏳ Đang tải: {pct}%\n({i+1}/{len(links)})")
            except:
                pass
        
        time.sleep(0.2)

    if success_count == 0:
        await status.edit_text("❌ Không thể tải nội dung các chương do bị trang web chặn.")
        return

    # Cấu hình Mục lục (TOC) & Spine chuẩn Kindle
    book.toc = tuple(chapters_list)
    book.spine = ['nav'] + chapters_list

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    
    # Lưu file
    safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip() or "Truyen"
    file_name = f"{safe_title}.epub"
    epub.write_epub(file_name, book)
    
    # Gửi file qua Telegram
    await status.edit_text(f"⬆️ Đang gửi file EPUB...")
    with open(file_name, "rb") as f:
        await update.message.reply_document(
            document=f, 
            caption=f"✅ Hoàn tất: {title}\n📖 Trọn bộ {success_count} chương + Ảnh bìa & Mục lục chuẩn Kindle!"
        )
        
    await status.delete()
    if os.path.exists(file_name):
        os.remove(file_name)

def main():
    threading.Thread(target=run_web_server, daemon=True).start()
    
    if not BOT_TOKEN:
        print("❌ Lỗi: Thiếu BOT_TOKEN trong biến môi trường!")
        return
        
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🤖 Bot đang chạy...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
