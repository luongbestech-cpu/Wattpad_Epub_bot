import asyncio
import os
import re
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, urljoin, urldefrag
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
        self.wfile.write("Bot Truyyen đang hoạt động!".encode('utf-8'))

    def log_message(self, format, *args):
        return

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    server.serve_forever()

# ============================================================
# ⚙️ CẤU HÌNH BOT & CHỐNG TƯỜNG LỬA (CLOUDSCRAPER)
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN_TRUYENFULL") or os.getenv("BOT_TOKEN_WORDPRESS") or os.getenv("BOT_TOKEN")

scraper = cloudscraper.create_scraper(
    browser={
        'browser': 'chrome',
        'platform': 'windows',
        'desktop': True
    }
)

def get_soup(url):
    try:
        parsed_url = urlparse(url)
        base_domain_url = f"{parsed_url.scheme}://{parsed_url.netloc}/"
        
        # Thêm Header giả lập người dùng thật và Referer chống chặn
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Referer": base_domain_url,
            "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
        }
        
        response = scraper.get(url, headers=headers, timeout=35)
        if response.status_code == 200:
            return BeautifulSoup(response.text, "lxml")
    except Exception as e:
        print(f"Lỗi tải {url}: {e}")
    return None

def download_chapter(url):
    soup = get_soup(url)
    if not soup: return None
    
    # Tìm vùng nội dung chương linh hoạt theo nhiều cấu trúc web phổ biến
    content = soup.select_one(".chapter-content") or soup.select_one("#chapter-c") or soup.select_one(".chapter-c") or soup.select_one(".entry-content") or soup.select_one("article")
    if not content: return None
    
    # 1. Xóa các thẻ rác hệ thống
    for tag in content.find_all(["script", "style", "div", "ins", "iframe", "button", "form", "nav"]):
        tag.decompose()
        
    # 2. Xóa các đoạn văn chứa từ khóa quảng cáo, chia sẻ, bản quyền
    keywords_to_remove = [
        "chia sẻ", "share", "thích", "đang tải", "có liên quan", 
        "chương trước", "chương sau", "truyện chỉ đăng tại", "edit by", "nguồn"
    ]
    
    for element in content.find_all(["p", "span", "section"]):
        text = element.get_text().strip().lower()
        if not text: continue
        if any(kw in text for kw in keywords_to_remove):
            element.decompose()
        elif text in ["x", "facebook", "twitter", "pinterest"]:
            element.decompose()
        
    return str(content)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url_match = re.findall(r"https?://[^\s]+", update.message.text or "")
    if not url_match: return
    
    status = await update.message.reply_text("⏳ Đang vượt tường lửa kết nối trang truyện (Quét danh sách chương)...")
    
    story_url = url_match[0]
    main_soup = get_soup(story_url)
    if not main_soup:
        await status.edit_text("❌ Không thể kết nối tới trang truyện. Web có thể đang chặn mạnh hoặc sai đường dẫn.")
        return
        
    title_el = main_soup.select_one("h1") or main_soup.select_one(".title") or main_soup.title
    title = title_el.get_text().strip() if title_el else "Truyện"
    if "|" in title:
        title = title.split('|')[0].strip()
        
    # Lấy ảnh bìa
    cover_url = None
    img_tag = main_soup.select_one(".book img") or main_soup.select_one(".truyen-info img") or main_soup.find("meta", property="og:image")
    if img_tag:
        if img_tag.name == "meta":
            cover_url = img_tag.get("content")
        else:
            cover_url = img_tag.get("src")

    # --- HỆ THỐNG QUÉT PHÂN TRANG THÔNG MINH (HỖ TRỢ CẢ ?page=N VÀ NÚT BẤM) ---
    links = []
    parsed_base = urlparse(story_url)
    base_domain = f"{parsed_base.scheme}://{parsed_base.netloc}"
    current_page_url = story_url
    page_num = 1
    
    while current_page_url:
        soup = get_soup(current_page_url)
        if not soup: break
            
        chapter_tags = soup.select("#list-chapter a, .list-chapter a, .chapter-list a, .entry-content a, article a")
        if not chapter_tags: break
            
        new_chapters_in_page = 0
        for a in chapter_tags:
            href = a.get('href', '')
            if href:
                full_url = href if href.startswith("http") else base_domain + href
                full_url = urldefrag(full_url)[0]
                text = a.get_text().strip()
                
                # Bộ lọc nhận diện chương (Số, Chương, Hồi, Ngoại truyện...)
                is_chap = re.match(r"^(\d+|PN\s*\d+|NT\s*\d+|chương|chuong|hồi|hoi|quyển|quyen|c\s*\d+)", text, flags=re.IGNORECASE)
                
                if text and is_chap and not any(l['url'] == full_url for l in links):
                    links.append({"name": text, "url": full_url})
                    new_chapters_in_page += 1
                    
        # QUAN TRỌNG: Không tìm thấy chương mới ở trang này -> Dừng vòng lặp ngay lập tức
        if new_chapters_in_page == 0:
            break
            
        # Tìm nút phân trang trang tiếp theo
        pagination_links = soup.select(".pagination a, .pages a, .nav-links a")
        next_url = None
        for p_link in pagination_links:
            text_p = p_link.get_text().strip()
            if "Trang sau" in text_p or ">" in text_p or str(page_num + 1) == text_p:
                next_url = p_link.get('href')
                break
                
        if next_url:
            next_full_url = next_url if next_url.startswith("http") else base_domain + next_url
            if next_full_url == current_page_url: break
            current_page_url = next_full_url
            page_num += 1
        else:
            # Tự động sinh link phân trang theo dạng ?page=N nếu web dùng query parameter
            if page_num < 80: # Giới hạn an toàn tối đa 80 trang
                page_num += 1
                clean_base = story_url.split("?")[0]
                current_page_url = f"{clean_base}?page={page_num}"
            else:
                break
                
        time.sleep(0.4)

    if not links:
        await status.edit_text("❌ Không tìm thấy chương nào. Hãy kiểm tra lại đường dẫn truyện.")
        return
        
    await status.edit_text(f"📚 {title}\n✅ Quét thành công tổng cộng {len(links)} chương. Đang tiến hành tải...")
    
    # Tạo file EPUB chuẩn Kindle
    book = epub.EpubBook()
    book.set_identifier('epub_' + re.sub(r'\W+', '', title))
    book.set_title(title)
    book.set_language('vi')
    
    # Thêm ảnh bìa
    if cover_url:
        try:
            full_cover_url = cover_url if cover_url.startswith("http") else base_domain + cover_url
            img_data = scraper.get(full_cover_url, timeout=15).content
            book.set_cover("cover.jpg", img_data)
        except Exception as e:
            print(f"Lỗi tải ảnh bìa: {e}")

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
        
        time.sleep(0.3)

    if success_count == 0:
        await status.edit_text("❌ Tải thất bại do trang web chặn toàn bộ nội dung.")
        return

    # Cấu hình Mục lục (TOC) & Luồng đọc chuẩn Kindle
    book.toc = tuple(chapters_list)
    book.spine = ['nav'] + chapters_list

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    
    safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip() or "Truyen"
    file_name = f"{safe_title}.epub"
    
    epub.write_epub(file_name, book)
    
    await update.message.reply_document(
        document=open(file_name, "rb"), 
        caption=f"✅ Xong: {title}\n📖 Đã tải đủ trọn bộ {success_count}/{len(links)} chương + Ảnh bìa & Mục lục chuẩn Kindle!"
    )
    await status.delete()
    
    if os.path.exists(file_name):
        os.remove(file_name)

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
