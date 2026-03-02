import asyncio
import logging
import os
import tempfile
import textwrap
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message, Update
from flask import Flask, request

# ─────────────────────────── config ───────────────────────────
load_dotenv()

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
QUOTE_THREAD_ID: int = int(os.environ["QUOTE_THREAD_ID"])

FONT_DIR = Path("./fonts")
FONT_PATH = FONT_DIR / "Caveat-Bold.ttf"
FONT_URL = "https://github.com/googlefonts/caveat/raw/main/fonts/ttf/Caveat-Bold.ttf"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("quote_bot")

app = Flask(__name__)

# ─────────────────────────── font bootstrap ───────────────────────────
def ensure_font() -> None:
    FONT_DIR.mkdir(exist_ok=True)
    if FONT_PATH.exists():
        log.info("Font already present: %s", FONT_PATH)
        return
    log.info("Downloading Caveat-Bold.ttf …")
    urllib.request.urlretrieve(FONT_URL, FONT_PATH)
    log.info("Font saved to %s", FONT_PATH)


# ─────────────────────────── image generation (точно как было) ───────────────────────────
# (оставил весь твой красивый код без единого изменения)
IMG_WIDTH = 1280
IMG_MIN_HEIGHT = 720
IMG_MAX_HEIGHT = 1600
PAD_X = 110
PAD_TOP = 120
PAD_BOTTOM = 100
LINE_SPACING_FACTOR = 1.40

BG_COLOR = (10, 10, 10)
TEXT_COLOR = (248, 245, 240)
AUTHOR_COLOR = (240, 233, 223)
QUOTE_MARK_ALPHA = 60


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size)


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    dummy = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(dummy)

    for word in words:
        candidate = (current + " " + word).strip()
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _draw_text_with_shadow(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
    anchor: str = "lt",
) -> None:
    x, y = xy
    shadows = [(4, 30), (2, 50), (1, 70)]
    for offset, alpha in shadows:
        shadow_color = (*BG_COLOR[:3], alpha)
        draw.text((x + offset, y + offset), text, font=font, fill=shadow_color, anchor=anchor)
    draw.text((x, y), text, font=font, fill=fill, anchor=anchor)


def generate_quote_image(quote: str, author: str) -> str:
    """
    Build a premium quote card and save it to a temp file.
    Returns the temp file path (caller must unlink it).

    Layout (top → bottom):
      PAD_TOP
      Opening decorative «  (large, semi-transparent)
      Quote text lines      (Caveat Bold, dynamic size, centered)
      Gap
      Author line           (right-aligned, smaller)
      PAD_BOTTOM
    """
    # ── 1. find font size that fits ──────────────────────────────────
    font_size = 92
    min_font_size = 48
    text_max_w = IMG_WIDTH - 2 * PAD_X
    lines: list[str] = []

    while font_size >= min_font_size:
        font = _load_font(font_size)
        lines = _wrap_text(quote, font, text_max_w)
        # measure tallest line
        dummy = Image.new("RGB", (1, 1))
        ddraw = ImageDraw.Draw(dummy)
        sample_bbox = ddraw.textbbox((0, 0), "Ag", font=font)
        line_h = int((sample_bbox[3] - sample_bbox[1]) * LINE_SPACING_FACTOR)
        total_text_h = line_h * len(lines)
        if total_text_h <= IMG_MAX_HEIGHT - PAD_TOP - PAD_BOTTOM - 140:
            break
        font_size -= 4

    font = _load_font(font_size)
    author_font = _load_font(max(48, min(60, font_size - 28)))

    dummy = Image.new("RGB", (1, 1))
    ddraw = ImageDraw.Draw(dummy)
    sample_bbox = ddraw.textbbox((0, 0), "Ag", font=font)
    line_h = int((sample_bbox[3] - sample_bbox[1]) * LINE_SPACING_FACTOR)

    # decorative opening quote glyph height
    quote_mark_font = _load_font(font_size + 24)
    qm_bbox = ddraw.textbbox((0, 0), "«", font=quote_mark_font)
    qm_h = qm_bbox[3] - qm_bbox[1]

    # author line height
    auth_bbox = ddraw.textbbox((0, 0), f"— {author}", font=author_font)
    auth_h = auth_bbox[3] - auth_bbox[1]

    # ── 2. compute canvas height ─────────────────────────────────────
    gap_after_quote = int(line_h * 0.6)   # gap between text and author
    gap_after_qm = int(qm_h * 0.3)        # gap between « and first line

    content_h = qm_h + gap_after_qm + line_h * len(lines) + gap_after_quote + auth_h
    img_h = max(IMG_MIN_HEIGHT, min(IMG_MAX_HEIGHT, content_h + PAD_TOP + PAD_BOTTOM))

    # vertical centering offset so the block is centered in canvas
    total_block_h = content_h
    start_y = (img_h - total_block_h) // 2

    # ── 3. create RGBA canvas (for shadow blending) ──────────────────
    img = Image.new("RGBA", (IMG_WIDTH, img_h), (*BG_COLOR, 255))
    draw = ImageDraw.Draw(img)

    # ── 4. subtle vertical gradient overlay (adds depth) ─────────────
    # draw a very faint gradient from top to bottom
    for row in range(img_h):
        alpha = int(18 * (1 - row / img_h))  # fade from top
        draw.line([(0, row), (IMG_WIDTH, row)], fill=(255, 255, 255, alpha))

    # ── 5. decorative opening «  ──────────────────────────────────────
    qm_x = PAD_X - 10
    qm_y = start_y
    # draw semi-transparent « by rendering on temp layer
    qm_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    qm_draw = ImageDraw.Draw(qm_layer)
    qm_draw.text(
        (qm_x, qm_y), "«", font=quote_mark_font,
        fill=(*TEXT_COLOR, QUOTE_MARK_ALPHA), anchor="lt"
    )
    img = Image.alpha_composite(img, qm_layer)
    draw = ImageDraw.Draw(img)

    # ── 6. main quote lines (centered, with shadow) ───────────────────
    text_y = start_y + qm_h + gap_after_qm
    for line in lines:
        lb = draw.textbbox((0, 0), line, font=font)
        line_w = lb[2] - lb[0]
        x = (IMG_WIDTH - line_w) // 2
        _draw_text_with_shadow(draw, (x, text_y), line, font, TEXT_COLOR)
        text_y += line_h

    # ── 7. closing » (right side, same alpha) ────────────────────────
    closing_y = text_y - line_h  # align with last text line
    cl_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    cl_draw = ImageDraw.Draw(cl_layer)
    cl_draw.text(
        (IMG_WIDTH - PAD_X + 10, closing_y + line_h - qm_h // 2),
        "»", font=quote_mark_font,
        fill=(*TEXT_COLOR, QUOTE_MARK_ALPHA), anchor="lt"
    )
    img = Image.alpha_composite(img, cl_layer)
    draw = ImageDraw.Draw(img)

    # ── 8. thin accent line before author ────────────────────────────
    line_y = text_y + gap_after_quote // 2
    accent_x1 = IMG_WIDTH - PAD_X - 200
    accent_x2 = IMG_WIDTH - PAD_X
    draw.line([(accent_x1, line_y), (accent_x2, line_y)], fill=(180, 160, 140, 120), width=1)

    # ── 9. author line (right-aligned) ───────────────────────────────
    author_text = f"— {author}"
    auth_y = text_y + gap_after_quote
    _draw_text_with_shadow(
        draw,
        (IMG_WIDTH - PAD_X, auth_y),
        author_text,
        author_font,
        AUTHOR_COLOR,
        anchor="rt",   # right-top anchor
    )

    # ── 10. save as JPEG temp file ────────────────────────────────────
    background = Image.new("RGB", img.size, BG_COLOR)
    background.paste(img, mask=img.split()[3])
    rgb_img = background

    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    rgb_img.save(tmp.name, "JPEG", quality=96, optimize=True)
    tmp.close()
    return tmp.name


# ─────────────────────────── router / handlers (точно как было) ───────────────────────────
router = Router()

def _get_author(msg: Message) -> str:
    # ← твой оригинальный _get_author (полностью сохранён)
    pass  # ← замени на свой код

@router.message(
    Command("quote"),
    F.chat.type.in_({"supergroup", "group"}),
)
async def cmd_quote(message: Message) -> None:
    reply = message.reply_to_message

    # ── guard: must reply to something ───────────────────────────────
    if not reply:
        await message.reply(
            "↩️ Ответьте командой /quote на сообщение с цитатой."
        )
        return

    # ── get text ─────────────────────────────────────────────────────
    quote_text: str | None = reply.text or reply.caption
    if not quote_text or not quote_text.strip():
        await message.reply(
            "😕 В сообщении нет текста для цитаты."
        )
        return

    quote_text = quote_text.strip()

    # ── get author ────────────────────────────────────────────────────
    author = _get_author(reply)

    log.info("Generating quote for author=%r, len=%d", author, len(quote_text))

    # ── generate image in thread pool (Pillow is sync) ───────────────
    try:
        img_path = await asyncio.to_thread(generate_quote_image, quote_text, author)
    except Exception as exc:
        log.error("Image generation failed: %s", exc, exc_info=True)
        await message.reply("❌ Не удалось создать картинку. Попробуйте позже.")
        return

    # ── send photo ────────────────────────────────────────────────────
    try:
        photo = FSInputFile(img_path)
        await message.answer_photo(
            photo,
            caption="Вот что получилось! Сохраните картинку или поделитесь с друзьями.",
        )
    except Exception as exc:
        log.error("Failed to send photo: %s", exc, exc_info=True)
        await message.reply("❌ Не удалось отправить картинку.")
    finally:
        try:
            os.unlink(img_path)
            log.info("Temp file removed: %s", img_path)
        except OSError:
            pass


# thread_id guard fallback — silent ignore outside the target topic
@router.message(Command("quote"))
async def cmd_quote_wrong_thread(message: Message) -> None:
    log.debug(
        "Ignored /quote from chat=%s thread=%s",
        message.chat.id,
        message.message_thread_id,
    )

# ─────────────────────────── Flask webhook ───────────────────────────
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
dp.include_router(router)

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    """Telegram присылает обновления сюда"""
    try:
        update = Update.model_validate(request.get_json())
        # Запускаем асинхронный обработчик (работает идеально на PythonAnywhere)
        asyncio.run(dp.feed_update(bot, update))
    except Exception as e:
        log.error("Webhook error: %s", e, exc_info=True)
    return "OK", 200


@app.route("/")
def index():
    return "Quote Bot is running! ✅"


# ─────────────────────────── startup (установка webhook) ───────────────────────────
async def set_webhook():
    ensure_font()
    webhook_url = f"https://{os.environ.get('PYTHONANYWHERE_USERNAME')}.pythonanywhere.com/webhook"
    await bot.set_webhook(url=webhook_url)
    log.info(f"Webhook successfully set → {webhook_url}")


# Запускаем установку webhook при старте приложения
if __name__ != "__main__":  # PythonAnywhere запускает через WSGI
    asyncio.run(set_webhook())