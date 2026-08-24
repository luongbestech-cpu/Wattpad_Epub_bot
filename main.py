import os
import re
import asyncio
import zipfile
import tempfile
from pathlib import Path
from html import escape

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")

INPUT_DIR = Path("chapters")
OUTPUT_DIR = Path("output")

INPUT_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)


def natural_key(text):
    return [
        int(x) if x.isdigit() else x.lower()
        for x in re.split(r"(\d+)", text)
    ]


def read_chapters():
    files = sorted(
        [
            p for p in INPUT_DIR.iterdir()
            if p.suffix.lower() in {".txt", ".html", ".htm"}
        ],
        key=lambda p: natural_key(p.stem),
    )

    chapters = []

    for path in files:
        if path.suffix.lower() == ".txt":
            text = path.read_text(
                encoding="utf-8",
                errors="ignore"
            )

            title = path.stem

            paragraphs = [
                p.strip()
                for p in text.splitlines()
                if p.strip()
            ]

            content = "\n".join(
                f"<p>{escape(p)}</p>"
                for p in paragraphs
            )

        else:
            from bs4 import BeautifulSoup

            html = path.read_text(
                encoding="utf-8",
                errors="ignore"
            )

            soup = BeautifulSoup(html, "html.parser")

            title = (
                soup.find("title").get_text(strip=True)
                if soup.find("title")
                else path.stem
            )

            body = soup.body or soup

            paragraphs = [
                p.get_text(" ", strip=True)
                for p in body.find_all("p")
                if p.get_text(strip=True)
            ]

            content = "\n".join(
                f"<p>{escape(p)}</p>"
                for p in paragraphs
            )

        chapters.append({
            "title": title,
            "content": content,
        })

    return chapters


def create_epub(book_title, chapters, output_file):
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)

        (root / "META-INF").mkdir()
        (root / "OEBPS").mkdir()

        # EPUB bắt buộc: mimetype không được nén
        (root / "mimetype").write_text(
            "application/epub+zip",
            encoding="utf-8"
        )

        (root / "META-INF" / "container.xml").write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0"
    xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
    <rootfiles>
        <rootfile
            full-path="OEBPS/content.opf"
            media-type="application/oebps-package+xml"/>
    </rootfiles>
</container>
""",
            encoding="utf-8"
        )

        manifest = []
        spine = []
        toc_items = []

        for index, chapter in enumerate(chapters, 1):
            filename = f"chapter_{index}.xhtml"
            item_id = f"chapter{index}"

            html = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta charset="utf-8"/>
<title>{escape(chapter["title"])}</title>
<style>
body {{
    font-family: serif;
    line-height: 1.6;
    margin: 5%;
}}

h1 {{
    text-align: center;
    margin-bottom: 2em;
}}

p {{
    text-indent: 2em;
    margin: 0.7em 0;
}}
</style>
</head>
<body>
<h1>{escape(chapter["title"])}</h1>
{chapter["content"]}
</body>
</html>
"""

            (root / "OEBPS" / filename).write_text(
                html,
                encoding="utf-8"
            )

            manifest.append(
                f'<item id="{item_id}" '
                f'href="{filename}" '
                f'media-type="application/xhtml+xml"/>'
            )

            spine.append(
                f'<itemref idref="{item_id}"/>'
            )

            toc_items.append(
                f'<li><a href="{filename}">'
                f'{escape(chapter["title"])}</a></li>'
            )

        nav = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:epub="http://www.idpf.org/2007/ops">
<head>
<meta charset="utf-8"/>
<title>Mục lục</title>
</head>
<body>
<nav epub:type="toc">
<h1>Mục lục</h1>
<ol>
{"".join(toc_items)}
</ol>
</nav>
</body>
</html>
"""

        (root / "OEBPS" / "nav.xhtml").write_text(
            nav,
            encoding="utf-8"
        )

        manifest.append(
            '<item id="nav" '
            'href="nav.xhtml" '
            'media-type="application/xhtml+xml" '
            'properties="nav"/>'
        )

        opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf"
         version="3.0"
         unique-identifier="book-id">

<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">book-{abs(hash(book_title))}</dc:identifier>
    <dc:title>{escape(book_title)}</dc:title>
    <dc:language>vi</dc:language>
    <meta property="dcterms:modified">
        2026-08-24T00:00:00Z
    </meta>
</metadata>

<manifest>
    {"".join(manifest)}
</manifest>

<spine>
    {"".join(spine)}
</spine>

</package>
"""

        (root / "OEBPS" / "content.opf").write_text(
            opf,
            encoding="utf-8"
        )

        with zipfile.ZipFile(
            output_file,
            "w",
            zipfile.ZIP_DEFLATED
        ) as epub:

            # mimetype phải là file đầu tiên và không nén
            epub.write(
                root / "mimetype",
                "mimetype",
                compress_type=zipfile.ZIP_STORED
            )

            for file in root.rglob("*"):
                if file.is_file() and file.name != "mimetype":
                    epub.write(
                        file,
                        file.relative_to(root).as_posix()
                    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 EPUB Bot\n\n"
        "Hãy đặt các file chương (.txt/.html) vào thư mục "
        "`chapters/`, sau đó gửi /convert"
    )


async def convert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔎 Đang đọc các chương..."
    )

    chapters = read_chapters()

    if not chapters:
        await update.message.reply_text(
            "❌ Không tìm thấy chương trong thư mục chapters/."
        )
        return

    book_title = "Wattpad EPUB"

    output_file = OUTPUT_DIR / "book.epub"

    create_epub(
        book_title,
        chapters,
        output_file
    )

    await update.message.reply_text(
        f"📚 Đã tìm thấy {len(chapters)} chương.\n"
        "📦 Đang gửi EPUB..."
    )

    with output_file.open("rb") as f:
        await update.message.reply_document(
            document=f,
            filename="book.epub",
            caption=(
                f"✅ EPUB hoàn tất!\n"
                f"📖 {len(chapters)} chương"
            )
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start - Hướng dẫn\n"
        "/convert - Tạo EPUB từ các chương"
    )


def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
        raise RuntimeError(
            "Hãy đặt BOT_TOKEN trước khi chạy bot."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("convert", convert))

    print("🤖 EPUB bot đang chạy...")

    app.run_polling()


if __name__ == "__main__":
    main()
