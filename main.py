# -*- coding: utf-8 -*-
"""
TVTRUYEN -> TELEGRAM BOT -> EPUB
--------------------------------
Bot nhận link truyện TVTruyen, tự quét các page danh sách chương,
lấy toàn bộ chương theo thứ tự và đóng thành EPUB.

Cài:
    pip install python-telegram-bot==21.6 requests beautifulsoup4 ebooklib lxml

Chạy:
    python main.py

Đặt BOT_TOKEN ở biến môi trường:
    macOS/Linux:
        export BOT_TOKEN="TOKEN_CUA_BAN"
        python main.py

Hoặc điền trực tiếp vào BOT_TOKEN bên dưới.
"""

import asyncio
import html
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup, Tag
from ebooklib import epub

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

SITE_HOST = "tvtruyen.vip"
REQUEST_TIMEOUT = 25
MAX_PAGES = 500
MAX_CHAPTERS = 10000
DELAY = 0.15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0 Safari/537.36"
    ),
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("tvtruyen-bot")


# ============================================================
# HTTP
# ============================================================

session = requests.Session()
session.headers.update(HEADERS)


def get_html(url: str) -> str:
    last_error = None

    for attempt in range(3):
        try:
            r = session.get(url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()

            # requests đôi khi đoán encoding sai với trang tiếng Việt.
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding or "utf-8"

            return r.text
        except Exception as e:
            last_error = e
            time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(f"Không tải được trang: {url}\n{last_error}")


def normalize_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url

    p = urlparse(url)
    return urlunparse((
        p.scheme or "https",
        p.netloc,
        p.path,
        p.params,
        p.query,
        "",  # bỏ #mobile-list-chapter
    ))


def page_url(story_url: str, page: int) -> str:
    p = urlparse(story_url)
    q = parse_qs(p.query)
    q["page"] = [str(page)]
    return urlunparse((
        p.scheme,
        p.netloc,
        p.path,
        p.params,
        urlencode(q, doseq=True),
        "",
    ))


def is_tvtruyen_url(url: str) -> bool:
    host = urlparse(url).netloc.lower().split(":")[0]
    return host == SITE_HOST or host.endswith("." + SITE_HOST)


# ============================================================
# TEXT / CHAPTER NUMBER
# ============================================================

def clean_text(s: str) -> str:
    s = html.unescape(s or "")
    s = s.replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n[ \t]+", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def chapter_number(text: str):
    text = clean_text(text)
    patterns = [
        r"(?:chương|chuong)\s*[-:#.]?\s*(\d+)",
        r"(?:#)\s*(\d+)",
        r"^\s*(\d+)\s*[\.\-:]",
    ]

    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return int(m.group(1))

    return None


def chapter_title_from_text(text: str, number=None) -> str:
    text = clean_text(text)

    if not text:
        return f"Chương {number}" if number is not None else "Chương"

    # Giữ tiêu đề kiểu "#51. Chương 51" hoặc "Chương 51: ..."
    m = re.search(
        r"(?:#\s*)?(\d+)\s*[\.\-:]\s*(.+)",
        text,
        re.I,
    )
    if m and number is not None and int(m.group(1)) == number:
        return f"Chương {number}: {m.group(2).strip()}"

    m = re.search(
        r"(chương\s+\d+(?:\s*[:\-–—]\s*.+)?)",
        text,
        re.I,
    )
    if m:
        return m.group(1).strip()

    return f"Chương {number}" if number is not None else text[:100]


# ============================================================
# FIND CHAPTER LINKS
# ============================================================

def extract_chapter_links(page_html: str, base_url: str):
    soup = BeautifulSoup(page_html, "lxml")
    found = {}

    # Trên TVTruyen, link chương thường có text "#51. Chương 51"
    # hoặc "Chương 51". Ta dùng heuristic thay vì khóa vào một class duy nhất.
    for a in soup.find_all("a", href=True):
        text = clean_text(a.get_text(" ", strip=True))
        href = a.get("href", "").strip()

        if not href:
            continue

        num = chapter_number(text)

        # Có trường hợp text ngắn, số chương nằm trong title/aria-label.
        if num is None:
            extra = " ".join([
                str(a.get("title", "")),
                str(a.get("aria-label", "")),
            ])
            num = chapter_number(extra)

        if num is None:
            continue

        url = urljoin(base_url, href)
        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https"):
            continue

        # Không lấy link ngoài site.
        host = parsed.netloc.lower().split(":")[0]
        base_host = urlparse(base_url).netloc.lower().split(":")[0]
        if host != base_host and host != SITE_HOST:
            continue

        # Loại các link không giống link đọc chương.
        path = parsed.path.lower()
        if not (
            "chuong" in path
            or "chapter" in path
            or "truyen" in path
            or "doc" in path
            or "read" in path
            or num is not None
        ):
            continue

        title = chapter_title_from_text(text, num)
        found[num] = (title, url)

    return found


# ============================================================
# CRAWL ALL LIST PAGES
# ============================================================

def scan_chapter_pages(story_url: str, progress_callback=None):
    """
    Quét page=1,2,3...
    Không dựa vào số chương cuối.
    Dừng khi liên tiếp không tìm thấy chương mới.
    """
    all_chapters = {}
    empty_streak = 0

    for page in range(1, MAX_PAGES + 1):
        url = page_url(story_url, page)

        try:
            text = get_html(url)
        except Exception as e:
            log.warning("Page %s lỗi: %s", page, e)
            # Nếu page 1 cũng lỗi thì báo luôn.
            if page == 1:
                raise
            empty_streak += 1
            if empty_streak >= 2:
                break
            continue

        items = extract_chapter_links(text, url)

        before = len(all_chapters)

        for num, data in items.items():
            if num not in all_chapters:
                all_chapters[num] = data

        added = len(all_chapters) - before

        if progress_callback:
            progress_callback(page, added, len(all_chapters))

        log.info(
            "page=%s | tìm thấy=%s | mới=%s | tổng=%s",
            page, len(items), added, len(all_chapters)
        )

        if added == 0:
            empty_streak += 1
        else:
            empty_streak = 0

        # Trang cuối thường lặp lại hoặc không còn chương.
        if empty_streak >= 2:
            break

        if len(all_chapters) >= MAX_CHAPTERS:
            break

        time.sleep(DELAY)

    return dict(sorted(all_chapters.items(), key=lambda x: x[0]))


# ============================================================
# FIND CONTENT CONTAINER
# ============================================================

CONTENT_KEYWORDS = [
    "chapter-content",
    "chapter-content",
    "content-chapter",
    "chapter_body",
    "chapter-body",
    "chapterbody",
    "chapter",
    "content",
    "noi-dung",
    "noidung",
    "reading",
    "read-content",
    "entry-content",
    "post-content",
]


def score_container(tag: Tag) -> int:
    if not isinstance(tag, Tag):
        return -999

    name = tag.name.lower()
    if name in ("html", "body", "head", "nav", "header", "footer", "script", "style"):
        return -999

    ident = " ".join([
        str(tag.get("id", "")),
        " ".join(tag.get("class", [])),
    ]).lower()

    text = clean_text(tag.get_text("\n", strip=True))

    if len(text) < 200:
        return -999

    score = 0

    for kw in CONTENT_KEYWORDS:
        if kw in ident:
            score += 30

    # Nội dung truyện có nhiều đoạn <p>.
    p_count = len(tag.find_all("p"))
    score += min(p_count * 2, 50)

    # Có các nút điều hướng thì trừ điểm.
    nav_words = ["trước", "tiếp", "bình luận", "đăng nhập", "đăng ký"]
    low = text.lower()
    score -= sum(5 for w in nav_words if w in low)

    # Container quá lớn thường là body/wrapper toàn trang.
    if len(text) > 100000:
        score -= 20

    score += min(len(text) // 5000, 20)

    return score


def remove_noise(container: Tag):
    # Xóa các thành phần không phải nội dung truyện.
    selectors = [
        "script", "style", "noscript", "iframe",
        "form", "nav", "header", "footer",
        ".ads", ".ad", ".advertisement", ".banner",
        ".comment", ".comments", ".social",
        ".breadcrumb", ".pagination",
    ]

    for sel in selectors:
        for x in container.select(sel):
            x.decompose()

    # Xóa nút Trước/Tiếp nếu nằm trong container.
    for el in list(container.find_all(["a", "button"])):
        txt = clean_text(el.get_text(" ", strip=True)).lower()
        if txt in {
            "trước", "tiếp", "trước →", "→ tiếp",
            "← trước", "next", "previous"
        }:
            el.decompose()


def extract_chapter(page_html: str, url: str, expected_number=None):
    soup = BeautifulSoup(page_html, "lxml")

    # Tiêu đề.
    title = None
    for selector in [
        "h1", ".chapter-title", ".title-chapter",
        ".chapter_name", ".chapter-name",
    ]:
        el = soup.select_one(selector)
        if el:
            t = clean_text(el.get_text(" ", strip=True))
            if chapter_number(t) is not None or "chương" in t.lower():
                title = t
                break

    if not title and expected_number is not None:
        title = f"Chương {expected_number}"

    # Tìm container có điểm cao nhất.
    candidates = []
    for tag in soup.find_all(["div", "article", "section", "main"]):
        score = score_container(tag)
        if score > 0:
            candidates.append((score, tag))

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        container = candidates[0][1]
    else:
        # fallback: article/main/body
        container = (
            soup.select_one("article")
            or soup.select_one("main")
            or soup.body
        )

    if container is None:
        raise RuntimeError(f"Không tìm được nội dung chương: {url}")

    remove_noise(container)

    # Chuyển <br> thành xuống dòng trước khi lấy text.
    for br in container.find_all("br"):
        br.replace_with("\n")

    paragraphs = []

    # Ưu tiên p/div có text để EPUB đẹp hơn.
    for el in container.find_all(["p", "div", "blockquote"]):
        txt = clean_text(el.get_text(" ", strip=True))
        if len(txt) >= 2:
            # Bỏ các dòng rõ ràng là menu.
            low = txt.lower()
            if low in {"trước", "tiếp", "bình luận", "đăng nhập", "đăng ký"}:
                continue
            paragraphs.append(txt)

    # Nếu không có p/div, lấy text toàn container.
    if not paragraphs:
        raw = container.get_text("\n", strip=True)
        paragraphs = [
            clean_text(x)
            for x in raw.splitlines()
            if clean_text(x)
        ]

    # Loại các đoạn trùng liên tiếp do div chứa p.
    cleaned = []
    seen_recent = set()

    for p in paragraphs:
        key = re.sub(r"\s+", " ", p).strip()

        if len(key) < 2:
            continue

        if key in seen_recent:
            continue

        cleaned.append(key)
        seen_recent.add(key)

        if len(seen_recent) > 30:
            seen_recent = set(list(seen_recent)[-20:])

    # Nếu parser lấy phải cả wrapper, lọc các đoạn menu.
    bad_exact = {
        "trước", "tiếp", "trang chủ", "danh sách",
        "thể loại", "tìm truyện", "nhóm dịch", "chương",
    }
    cleaned = [x for x in cleaned if x.lower() not in bad_exact]

    if not cleaned or sum(len(x) for x in cleaned) < 300:
        raise RuntimeError(
            f"Nội dung chương quá ngắn/không tìm thấy: {url}"
        )

    return title or f"Chương {expected_number}", cleaned


# ============================================================
# EPUB
# ============================================================

def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:150] or "truyen"


def create_epub(story_title, chapters, output_path):
    book = epub.EpubBook()

    book.set_identifier(
        "tvtruyen-" + re.sub(r"[^0-9a-zA-Z]+", "-", story_title.lower())[:60]
    )
    book.set_title(story_title)
    book.set_language("vi")

    book.add_author("TVTruyen")

    css = """
    body {
        font-family: serif;
        line-height: 1.65;
        margin: 5%;
    }
    h1 {
        text-align: center;
        margin-bottom: 2em;
    }
    p {
        text-indent: 1.5em;
        margin: 0 0 0.9em 0;
    }
    """

    style = epub.EpubItem(
        uid="style",
        file_name="style/book.css",
        media_type="text/css",
        content=css.encode("utf-8"),
    )
    book.add_item(style)

    spine = ["nav"]
    toc = []

    for index, (num, title, paragraphs) in enumerate(chapters, start=1):
        fname = f"chapter_{num:05d}.xhtml"

        body = [
            f"<h1>{html.escape(title)}</h1>"
        ]

        for p in paragraphs:
            body.append(f"<p>{html.escape(p)}</p>")

        page = epub.EpubHtml(
            title=title,
            file_name=fname,
            lang="vi",
        )
        page.content = f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta charset="utf-8"/>
<title>{html.escape(title)}</title>
<link rel="stylesheet" type="text/css" href="../style/book.css"/>
</head>
<body>
{''.join(body)}
</body>
</html>
""".encode("utf-8")

        page.add_item(style)
        book.add_item(page)

        toc.append(page)
        spine.append(page)

    book.toc = tuple(toc)

    nav = epub.EpubNav()
    book.add_item(nav)

    ncx = epub.EpubNcx()
    book.add_item(ncx)

    book.spine = spine

    epub.write_epub(str(output_path), book, {})


# ============================================================
# STORY INFO
# ============================================================

def extract_story_title(page_html: str, fallback_url: str):
    soup = BeautifulSoup(page_html, "lxml")

    for selector in [
        "h1", ".book-title", ".story-title",
        ".truyen-title", ".title",
    ]:
        el = soup.select_one(selector)
        if el:
            text = clean_text(el.get_text(" ", strip=True))
            if 2 <= len(text) <= 200:
                return text

    if soup.title:
        t = clean_text(soup.title.get_text(" ", strip=True))
        t = re.sub(r"\s*[-|]\s*(TVTruyen|Truyện VIP).*?$", "", t, flags=re.I)
        if t:
            return t

    path = Path(urlparse(fallback_url).path).stem
    return path.replace("-", " ").strip().title()


# ============================================================
# TELEGRAM
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 Gửi cho mình link truyện TVTruyen.\n\n"
        "Ví dụ:\n"
        "https://www.tvtruyen.vip/trien-chi-mieu-thuong-chi-dau.html?page=2\n\n"
        "Bot sẽ tự quét từ page=1 đến trang cuối, "
        "lấy toàn bộ chương và đóng thành EPUB."
    )


async def process_story(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    msg = await update.message.reply_text("🔎 Đang kiểm tra link...")

    try:
        url = normalize_url(url)

        if not is_tvtruyen_url(url):
            await msg.edit_text("❌ Link này không phải TVTruyen.")
            return

        # Bỏ page đang đứng, luôn bắt đầu từ page 1.
        p = urlparse(url)
        story_url = urlunparse((
            p.scheme,
            p.netloc,
            p.path,
            p.params,
            "",
            "",
        ))

        first_html = await asyncio.to_thread(get_html, story_url)
        story_title = extract_story_title(first_html, story_url)

        await msg.edit_text(
            f"📚 {story_title}\n\n"
            "🔎 Đang quét danh sách chương..."
        )

        progress = {"last_page": 0}

        def scan_progress(page, added, total):
            progress["last_page"] = page

        chapter_map = await asyncio.to_thread(
            scan_chapter_pages,
            story_url,
            scan_progress,
        )

        if not chapter_map:
            await msg.edit_text(
                "❌ Không tìm thấy chương nào.\n"
                "Có thể TVTruyen đã đổi HTML hoặc chặn bot."
            )
            return

        total = len(chapter_map)

        await msg.edit_text(
            f"📚 {story_title}\n"
            f"📖 Tìm thấy {total} chương\n"
            f"📄 Đang tải nội dung..."
        )

        parsed_chapters = []
        failed = []

        for index, (num, (title, chapter_url)) in enumerate(
            chapter_map.items(), start=1
        ):
            try:
                chapter_html = await asyncio.to_thread(get_html, chapter_url)

                real_title, paragraphs = await asyncio.to_thread(
                    extract_chapter,
                    chapter_html,
                    chapter_url,
                    num,
                )

                parsed_chapters.append(
                    (num, real_title or title, paragraphs)
                )

            except Exception as e:
                failed.append((num, str(e)))
                log.exception("Lỗi chương %s: %s", num, e)

            if index == 1 or index % 5 == 0 or index == total:
                percent = int(index * 100 / total)
                try:
                    await msg.edit_text(
                        f"📚 {story_title}\n"
                        f"📖 {index}/{total} chương\n"
                        f"⏳ {percent}%"
                    )
                except Exception:
                    pass

            await asyncio.sleep(DELAY)

        if not parsed_chapters:
            await msg.edit_text(
                "❌ Tìm được danh sách chương nhưng không lấy được "
                "nội dung chương nào."
            )
            return

        parsed_chapters.sort(key=lambda x: x[0])

        await msg.edit_text(
            f"📚 {story_title}\n"
            f"📖 Đã lấy {len(parsed_chapters)}/{total} chương\n"
            "📦 Đang đóng EPUB..."
        )

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / f"{safe_filename(story_title)}.epub"

            await asyncio.to_thread(
                create_epub,
                story_title,
                parsed_chapters,
                out,
            )

            await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)

            caption = (
                f"📚 {story_title}\n"
                f"📖 {len(parsed_chapters)}/{total} chương\n"
                "✅ EPUB hoàn tất!"
            )

            if failed:
                caption += (
                    f"\n⚠️ Không lấy được {len(failed)} chương."
                )

            with out.open("rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename=out.name,
                    caption=caption,
                )

        await msg.delete()

    except Exception as e:
        log.exception("Lỗi xử lý truyện")
        try:
            await msg.edit_text(
                "❌ Có lỗi trong quá trình xử lý:\n\n"
                f"{str(e)[:3000]}"
            )
        except Exception:
            await update.message.reply_text(
                f"❌ Có lỗi: {str(e)[:3000]}"
            )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    m = re.search(r"https?://[^\s]+", text, re.I)

    if not m:
        await update.message.reply_text(
            "📚 B gửi link truyện TVTruyen cho mình nhé."
        )
        return

    await process_story(update, context, m.group(0))


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "Chưa có BOT_TOKEN. "
            "Hãy export BOT_TOKEN='token' rồi chạy lại."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    log.info("TVTrUYEN EPUB BOT đang chạy...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
