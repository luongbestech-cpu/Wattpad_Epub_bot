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
# 🌐 WEB SERVER CHO RENDER
# ============================================================
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write("Bot Wattpad Mobile đang hoạt động!".encode('utf-8'))

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
GMAIL_USER = os.getenv("GMAIL_USER")          
GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD")  
KINDLE_EMAIL = os.getenv("KINDLE_EMAIL")      

# Ép cloudscraper giả lập trình duyệt trên điện thoại để lấy bản m.wattpad.com sạch sẽ
scraper = cloudscraper.create_scraper(
    browser={
        'browser': 'iphone',
        'platform': 'ios',
        'desktop': False
    }
)

def get_soup(url):
    try:
        # Chuyển hướng link thường sang bản mobile m.wattpad.com để dễ cào dữ liệu
        if "www.wattpad.com" in url:
            url = url.replace("www.wattpad.com", "m.wattpad.com")
            
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1",
            "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7"
        }
        response = scraper.get(url, headers=headers, timeout=35)
        if response.status_code == 200:
            return BeautifulSoup(response.text, "lxml")
    except Exception as e:
        print(f"Lỗi tải {url}: {e}")
    return None

def send_epub_to_kindle(file_path, book_title):
    if not GMAIL_USER or not GMAIL_PASSWORD or not KINDLE_EMAIL:
        return False
    try:
        msg = EmailMessage()
        msg['Subject'] = f"Convert {book_title}"
        msg['From'] = GMAIL_USER
        msg['To'] = KINDLE_EMAIL
        msg.set_content(f"Tự động gửi truyện: {book_title}")

        with open(file_path, 'rb') as f:
            file_data = f.read()
            file_name = os.path.basename(file_path)

        msg.add_attachment(file_data, maintype='application', subtype='epub+zip', filename=file_name)

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_PASSWORD)
            smtp.send_message(msg)
        return True
    except Exception as e:
        print(f"Lỗi gửi mail Kindle: {e}")
        return False

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_msg = update.message.text or ""
    url_match = re.findall(r"https?://[^\s]+", text_msg)
    if not url_match: return
    
    story_url = url_match[0]
    if "wattpad.com" not in story_url:
        await update.message.reply_text("⚠️ Vui lòng gửi link truyện từ **Wattpad** nhé!")
        return

    status = await update.message.reply_text("⏳ Đang kết nối Wattpad (Bản Mobile tối ưu)...")
    
    main_soup = get_soup(story_url)
    if not main_soup:
        await status.edit_text("❌ Không thể kết nối tới trang truyện Wattpad này.")
        return
        
    # Lấy tiêu đề truyện
    title_el = main_soup.select_one("h1") or main_soup.select_one(".story-title") or main_soup.title
    title = title_el.get_text().strip() if title_el else "Truyen Wattpad"
    if "|" in title:
        title = title.split('|')[0].strip()
        
    # Lấy ảnh bìa
    cover_url = None
    img_tag = main_soup.select_one(".story-cover img") or main_soup.find("meta", property="og:image")
    if img_tag:
        cover_url = img_tag.get("content") if img_tag.name == "meta" else img_tag.get("src")

    # Lấy danh sách chương từ bản mobile
    links = []
    # Bản mobile của Wattpad chứa danh sách mục lục trong các thẻ ul/li hoặc bảng mục lục
    chapter_tags = main_soup.select("ul.table-of-contents a, .parts-list a, .story-parts a, li.part-item a")
    
    if not chapter_tags:
        # Fallback quét toàn bộ thẻ a nếu cấu trúc linh hoạt
        chapter_tags = main_soup.find_all("a", href=True)

    for a in chapter_tags:
        href = a.get('href', '')
        if "/story/" in href or re.search(r"/\d+-[^/]+$", href):
            full_url = href if href.startswith("http") else "https://www.wattpad.com" + href
            full_url = urldefrag(full_url)[0]
            text = a.get_text().strip()
            
            # Lọc chỉ lấy các link là chương truyện thực tế
            if text and len(text) < 100 and not any(l['url'] == full_url for l in links):
                if any(x in full_url for x in ["/story/", "-"]):
                    # Đảm bảo không lấy nhầm link trang chủ hoặc link rác
                    if re.search(r"\d+-[a-zA-Z0-9-]+", full_url):
                        links.append({"name": text, "url": full_url})

    if not links:
        # Thêm một lớp vét cạn tổng lực cuối cùng nếu giao diện mobile thay đổi ID
        for a in main_soup.find_all("a", href=True):
            href = a.get('href', '')
            text = a.get_text().strip()
            if text and re.search(r"^\d+", text) and "wattpad.com" in href:
                full_url = urldefrag(href)[0]
                if not any(l['url'] == full_url for l in links):
                    links.append({"name": text, "url": full_url})

    if not links:
        await status.edit_text("❌ Không tìm thấy danh sách chương trên Wattpad. Hãy kiểm tra lại link.")
        return
        
    await status.edit_text(f"📚 {title}\n✅ Quét thành công {len(links)} chương. Đang tải nội dung...")
    
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
            # Nội dung chương trên bản mobile Wattpad
            content_div = chap_soup.select_one(".part-text") or chap_soup.select_one("pre") or chap_soup.select_one(".story-text")
            if content_div:
                for tag in content_div.find_all(["script", "style", "iframe", "button"]):
                    tag.decompose()
                
                chap = epub.EpubHtml(title=item['name'], file_name=f"chap_{i+1}.xhtml")
                chap.content = f"<h2>{item['name']}</h2>{str(content_div)}"
                book.add_item(chap)
                chapters_list.append(chap)
                success_count += 1
        
        if i % 10 == 0 or i == len(links) - 1:
            pct = int(((i + 1) / len(links)) * 100)
            try:
                await status.edit_text(f"📚 {title}\n⏳ Đang tải: {pct}%\n({i+1}/{len(links)})")
            except:
                pass
        time.sleep(0.3)

    if success_count == 0:
        await status.edit_text("❌ Không thể tải nội dung chương từ Wattpad.")
        return

    book.toc = tuple(chapters_list)
    book.spine = ['nav'] + chapters_list
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    
    safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip() or "TruyenWattpad"
    file_name = f"{safe_title}.epub"
    epub.write_epub(file_name, book)
    
    # Gửi file lên Telegram
    await update.message.reply_document(
        document=open(file_name, "rb"), 
        caption=f"✅ Xong: {title}\n📖 Đã tải đủ {success_count}/{len(links)} chương từ Wattpad!"
    )
    
    # Gửi sang Kindle tự động
    if GMAIL_USER and KINDLE_EMAIL:
        await status.edit_text("📧 Đang tự động gửi file EPUB tới Kindle...")
        sent_ok = send_epub_to_kindle(file_name, title)
        if sent_ok:
            await status.edit_text(f"✅ Đã gửi thành công truyện **{title}** vào Kindle qua email (`{KINDLE_EMAIL}`)!")
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
