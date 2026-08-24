import asyncio
import os
import re
import threading
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urljoin, urldefrag
import requests
from bs4 import BeautifulSoup
from ebooklib import epub
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

# ============================================================
# 🌐 WEB SERVER DUMMY CHO RENDER
# ============================================================
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write("Bot đọc truyện WordPress & Wattpad đang hoạt động!".encode('utf-8'))

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
# ⚙️ CẤU HÌNH BOT & PHIÊN LÀM VIỆC
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN_WORDPRESS") or os.getenv("BOT_TOKEN")

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")        
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD", "") 
KINDLE_EMAIL = os.getenv("KINDLE_EMAIL", "")        

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7"
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
    
    # Xử lý riêng cho Wattpad
    if "wattpad.com" in url:
        title_el = soup.select_one(".story-info__title") or soup.select_one("h1") or soup.title
        title = title_el.get_text().strip() if title_el else "Truyện Wattpad"
        
        cover_url = None
        img_el = soup.select_one(".story-cover img") or soup.select_one(".cover img")
        if img_el and img_el.get("src"):
            cover_url = img_el["src"]
            
        chapters = []
        # Lấy các link chương từ danh sách mục lục Wattpad
        for a in soup.select(".table-of-contents a, .story-parts a, ul.list-parts a"):
            href = urldefrag(urljoin(url, a.get("href")))[0]
            text = a.get_text().strip()
            if href and text:
                if not any(c['url'] == href for c in chapters):
                    chapters.append({"name": text, "url": href})
        return chapters, title, cover_url

    # Xử lý cho WordPress / Leosansutu cũ
    title_el = soup.select_one("h1.entry-title") or soup.select_one("h1") or soup.title
    title = title_el.get_text().strip() if title_el else "Truyện WordPress"
    if "|" in title:
        title = title.split('|')[0].strip()
        
    cover_url = None
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        cover_url = og_img["content"]
        
    content_area = soup.select_one(".entry-content") or soup.select_one(".post-content") or soup.select_one("article") or soup.body
    if not cover_url and content_area:
        first_img = content_area.find("img")
        if first_img and first_img.get("src"):
            cover_url = first_img["src"]

    chapters = []
    if content_area:
        for a in content_area.find_all("a", href=True):
            href = urldefrag(urljoin(url, a.get("href")))[0]
            text = a.get_text().strip()
            is_chapter = re.match(r"^(\d+|chuong|chương|hồi|hoi|quyển|quyen|phần|phan|ngoại truyện|ngoai truyen|pn|nt|c\s*\d+)", text, flags=re.IGNORECASE)
            if is_chapter and len(text) < 100: 
                if not any(c['url'] == href for c in chapters):
                    chapters.append({"name": text, "url": href})
                    
    return chapters, title, cover_url

def download_chap(url):
    soup = get_content(url)
    if not soup: return None
    
    # Xử lý nội dung chương cho Wattpad
    if "wattpad.com" in url:
        paragraphs = soup.select(".story-text p, pre p, .p")
        if paragraphs:
            content_html = "".join([str(p) for p in paragraphs])
            return content_html
        # Fallback nếu cấu trúc thay đổi
        content_div = soup.select_one(".reading-content") or soup.select_one("pre")
        if content_div:
            return str(content_div)
        return None

    # Xử lý cho WordPress
    content = soup.select_one(".entry-content") or soup.select_one(".post-content") or soup.select_one("article")
    if not content: return None
    
    for t in content.find_all(["script", "style", "ins", "button", "iframe", "form", "nav", "comments"]): 
        t.decompose()
        
    keywords_to_remove = [
        "chia sẻ", "share", "thích", "đang tải", "có liên quan", 
        "chương trước", "chương sau", "truyện chỉ đăng tại", "edit by",
        "bấm để", "để lại bình luận", "thông báo", "mật khẩu", "gợi ý"
    ]
    
    for element in content.find_all(["div", "p", "span", "section", "footer"]):
        text = element.get_text().strip().lower()
        if not text:
            continue
        if any(kw in text for kw in keywords_to_remove):
            element.decompose()
        elif text in ["x", "facebook", "twitter", "pinterest", "tumblr"]:
            element.decompose()
            
    return str(content)

def send_to_kindle_email(file_path, title):
    if not SENDER_EMAIL or not SENDER_PASSWORD or not KINDLE_EMAIL:
        return False, "Chưa cấu hình thông tin Email trên Render."
    try:
        msg = MIMEMultipart()
        msg['From'] = SENDER_EMAIL
        msg['To'] = KINDLE_EMAIL
        msg['Subject'] = f"Convert {title}"

        msg.attach(MIMEText("Gửi file EPUB từ Bot tự động."))

        with open(file_path, "rb") as f:
            part = MIMEApplication(f.read(), Name=os.path.basename(file_path))
            part.add_header('Content-Disposition', 'attachment', filename=os.path.basename(file_path))
            msg.attach(part)

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, KINDLE_EMAIL, msg.as_string())
        server.quit()
        return True, "Đã gửi thành công vào Kindle!"
    except Exception as e:
        return False, f"Lỗi gửi Kindle: {str(e)}"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = re.findall(r"https?://[^\s]+", update.message.text or "")
    if not url: return
    
    status = await update.message.reply_text("⏳ Đang phân tích đường dẫn truyện...")
    chapters, title, cover_url = get_chapters(url[0])
    
    if not chapters:
        await status.edit_text("❌ Không tìm thấy chương nào. Hãy chắc chắn bạn gửi link trang mục lục chính của truyện (không phải link một chương lẻ).")
        return

    await status.edit_text(f"📚 {title}\n✅ Tìm thấy {len(chapters)} chương. Đang tải dữ liệu...")

    results = {}
    for i, c in enumerate(chapters):
        results[i] = download_chap(c["url"])
        if i % 10 == 0 or i == len(chapters) - 1:
            pct = int(((i + 1) / len(chapters)) * 100)
            try:
                await status.edit_text(f"📚 {title}\n⏳ Đang tải nội dung: {pct}%\n({i+1}/{len(chapters)})")
            except:
                pass

    book = epub.EpubBook()
    book.set_identifier('story_' + re.sub(r'\W+', '', title))
    book.set_title(title)
    book.set_language('vi')

    if cover_url:
        try:
            img_res = session.get(cover_url, timeout=15)
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
        await status.edit_text("❌ Không tải được nội dung chương nào từ trang này.")
        return

    book.toc = tuple(chapters_list)
    book.spine = ['nav'] + chapters_list

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip() or "Truyen"
    file_out = f"{safe_title}.epub"
    epub.write_epub(file_out, book)

    await status.edit_text(f"⬆️ Đang gửi file EPUB...")
    await update.message.reply_document(
        document=open(file_out, "rb"), 
        caption=f"✅ Hoàn tất: {title}\n📖 Trọn bộ {len(chapters_list)} chương!"
    )
    
    if KINDLE_EMAIL and SENDER_EMAIL:
        success, msg_note = send_to_kindle_email(file_out, title)
        await update.message.reply_text(f"📧 Trạng thái gửi Kindle: {msg_note}")

    await status.delete()
    if os.path.exists(file_out):
        os.remove(file_out)

def main():
    threading.Thread(target=run_web_server, daemon=True).start()
    
    if not BOT_TOKEN:
        print("❌ Lỗi: Thiếu BOT_TOKEN trong Environment Variables!")
        return
        
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
