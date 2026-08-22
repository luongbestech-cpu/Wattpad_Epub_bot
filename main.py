import asyncio
import os
import re
import time
import threading
import smtplib
from email.message import EmailMessage
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, urldefrag
import cloudscraper
from bs4 import BeautifulSoup
from ebooklib import epub
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

# ============================================================
# 🌐 WEB SERVER CHO RENDER (GIỮ APP KHÔNG BỊ CRASH)
# ============================================================
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write("Bot Wattpad Kindle đang hoạt động!".encode('utf-8'))

    def log_message(self, format, *args):
        return

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    server.serve_forever()

# ============================================================
# ⚙️ CẤU HÌNH BIẾN MÔI TRƯỜNG & GMAIL GỬI KINDLE
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Cài đặt sẵn thông tin Gmail dùng để gửi file sang Kindle (lấy từ Environment Variables trên Render)
GMAIL_USER = os.getenv("GMAIL_USER")          # Email Gmail của bạn (VD: tenncuaban@gmail.com)
GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD")  # Mật khẩu ứng dụng (App Password) của Gmail
KINDLE_EMAIL = os.getenv("KINDLE_EMAIL")      # Email Kindle của bạn (VD: tentai_khoan@kindle.com)

scraper = cloudscraper.create_scraper(
    browser={
        'browser': 'chrome',
        'platform': 'windows',
        'desktop': True
    }
)

def get_soup(url):
    try:
        response = scraper.get(url, timeout=35)
        if response.status_code == 200:
            return BeautifulSoup(response.text, "lxml")
    except Exception as e:
        print(f"Lỗi tải {url}: {e}")
    return None

def send_epub_to_kindle(file_path, book_title):
    """Hàm tự động gửi file EPUB tới thiết bị Kindle qua Email"""
    if not GMAIL_USER or not GMAIL_PASSWORD or not KINDLE_EMAIL:
        print("⚠️ Chưa cấu hình đầy đủ thông tin Gmail/Kindle trong Environment Variables!")
        return False
    
    try:
        msg = EmailMessage()
        msg['Subject'] = f"Convert {book_title}"
        msg['From'] = GMAIL_USER
        msg['To'] = KINDLE_EMAIL
        msg.set_content(f"Tự động gửi truyện: {book_title} từ Telegram Bot.")

        with open(file_path, 'rb') as f:
            file_data = f.read()
            file_name = os.path.basename(file_path)

        msg.add_attachment(file_data, maintype='application', subtype='epub+zip', filename=file_name)

        # Kết nối tới SMTP Server của Gmail
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_PASSWORD)
            smtp.send_message(msg)
        return True
    except Exception as e:
        print(f"Lỗi gửi Kindle: {e}")
        return False

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_msg = update.message.text or ""
    url_match = re.findall(r"https?://[^\s]+", text_msg)
    if not url_match: return
    
    story_url = url_match[0]
    if "wattpad.com" not in story_url:
        await update.message.reply_text("⚠️ Vui lòng gửi đường dẫn (link) truyện từ **Wattpad** nhé!")
        return

    status = await update.message.reply_text("⏳ Đang kết nối Wattpad để quét danh sách chương...")
    
    # Wattpad thường dùng API hoặc trang mục lục web
    main_soup = get_soup(story_url)
    if not main_soup:
        await status.edit_text("❌ Không thể kết nối tới truyện Wattpad này.")
        return
        
    title_el = main_soup.select_one("h1") or main_soup.select_one(".story-title") or main_soup.title
    title = title_el.get_text().strip() if title_el else "Truyen Wattpad"
    
    # Lấy ảnh bìa Wattpad
    cover_url = None
    img_tag = main_soup.select_one(".story-cover img") or main_soup.find("meta", property="og:image")
    if img_tag:
        cover_url = img_tag.get("content") if img_tag.name == "meta" else img_tag.get("src")

    # Lấy danh sách link chương trên Wattpad
    links = []
    chapter_tags = main_soup.select(".table-of-contents li a, .story-parts li a, ul.toc li a")
    
    if not chapter_tags:
        # Fallback quét toàn bộ thẻ a nếu cấu trúc thay đổi
        chapter_tags = main_soup.find_all("a", href=True)

    for a in chapter_tags:
        href = a.get('href', '')
        if "/story/" in href or "-chuyen-" in href or "/chap-" in href:
            full_url = href if href.startswith("http") else "https://www.wattpad.com" + href
            full_url = urldefrag(full_url)[0]
            text = a.get_text().strip()
            if text and not any(l['url'] == full_url for l in links):
                links.append({"name": text, "url": full_url})

    if not links:
        await status.edit_text("❌ Không tìm thấy chương nào trên Wattpad. Hãy kiểm tra lại link.")
        return
        
    await status.edit_text(f"📚 {title}\n✅ Tìm thấy {len(links)} chương. Đang tải nội dung...")
    
    book = epub.EpubBook()
    book.set_identifier('wattpad_' + re.sub(r'\W+', '', title))
    book.set_title(title)
    book.set_language('vi')
    
    if cover_url:
        try:
            img_data = scraper.get(cover_url, timeout=15).content
            book.set_cover("cover.jpg", img_data)
        except:
            pass

    chapters_list = []
    success_count = 0
    
    for i, item in enumerate(links):
        chap_soup = get_soup(item['url'])
        if chap_soup:
            content_div = chap_soup.select_one(".pre-text") or chap_soup.select_one(".part-text") or chap_soup.select_one("div[data-p-id]")
            if content_div:
                for tag in content_div.find_all(["script", "style", "iframe"]):
                    tag.decompose()
                
                chap = epub.EpubHtml(title=item['name'], file_name=f"chap_{i+1}.xhtml")
                chap.content = f"<h2>{item['name']}</h2>{str(content_div)}"
                book.add_item(chap)
                chapters_list.append(chap)
                success_count += 1
        
        if i % 10 == 0 or i == len(links) - 1:
            pct = int((i / len(links)) * 100)
            try:
                await status.edit_text(f"📚 {title}\n⏳ Đang tải: {pct}%\n({i+1}/{len(links)})")
            except:
                pass
        time.sleep(0.3)

    if success_count == 0:
        await status.edit_text("❌ Không thể trích xuất nội dung chương từ Wattpad.")
        return

    book.toc = tuple(chapters_list)
    book.spine = ['nav'] + chapters_list
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    
    safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip() or "TruyenWattpad"
    file_name = f"{safe_title}.epub"
    epub.write_epub(file_name, book)
    
    # Gửi file về Telegram trước
    await update.message.reply_document(
        document=open(file_name, "rb"), 
        caption=f"✅ Xong: {title}\n📖 Đã tải {success_count}/{len(links)} chương từ Wattpad!"
    )
    
    # Tự động gửi sang Kindle nếu đã cấu hình email
    if GMAIL_USER and KINDLE_EMAIL:
        await status.edit_text("📧 Đang tự động gửi file EPUB sang Kindle của bạn...")
        sent_ok = send_epub_to_kindle(file_name, title)
        if sent_ok:
            await status.edit_text(f"✅ Đã gửi sách **{title}** thẳng vào Kindle thành công qua email ({KINDLE_EMAIL})!")
        else:
            await status.edit_text("⚠️ Gửi Kindle thất bại. Hãy kiểm tra lại mật khẩu ứng dụng Gmail trên Render.")
    else:
        await status.delete()
    
    if os.path.exists(file_name):
        os.remove(file_name)

def main():
    threading.Thread(target=run_web_server, daemon=True).start()
    if not BOT_TOKEN:
        print("❌ Thiếu BOT_TOKEN!")
        return
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
