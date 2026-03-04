import asyncio
import gc
import hashlib
import json
import logging
import os
import resource
import tempfile
import time
import math
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

from PIL import Image, ImageDraw, ImageEnhance, ImageFont
from pilmoji import Pilmoji
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message

# ─────────────────────────── config ───────────────────────────

load_dotenv()

BOT_TOKEN: str = os.environ["BOT_TOKEN"]

SUPERCHAT_TO_THREAD_MAP = {
    -1002692670592: 188271,
    -1003721142275: 2
}

QUOTE_THREAD_ID: int = int(os.environ["QUOTE_THREAD_ID"])

FONT_DIR = Path("./fonts")
FONT_PATH = FONT_DIR / "Caveat-Bold.ttf"
FONT_URL = "https://github.com/googlefonts/caveat/raw/main/fonts/ttf/Caveat-Bold.ttf"

# Директория кэша фонов аватаров
BG_CACHE_DIR = Path("./bg_cache")
BG_CACHE_META = BG_CACHE_DIR / "_meta.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("quote_bot")

# ─────────────────────────── constants ────────────────────────

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

DARKEN_FACTOR = 0.6

# ── Лимит символов цитаты — увеличен до 800 ──
MAX_QUOTE_LENGTH = 800

# ── Минимальный шрифт уменьшен до 28 для вмещения длинных цитат ──
MIN_FONT_SIZE = 28

# ── Семафор: максимум 4 одновременных генерации изображений ──
IMAGE_GEN_SEMAPHORE = asyncio.Semaphore(4)

# ── ThreadPoolExecutor с 2 воркерами ──
IMAGE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="img_gen")


# ─────────────────────────── profiling helpers ────────────────

def _get_memory_mb() -> float:
    """Текущий RSS процесса в МБ (Linux/macOS)."""
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        rss_kb = usage.ru_maxrss
        if os.uname().sysname == "Darwin":
            return rss_kb / 1024 / 1024
        return rss_kb / 1024
    except Exception:
        return 0.0


def _get_rss_mb() -> float:
    """Текущий RSS через /proc/self/status (точнее для Linux)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    return _get_memory_mb()


class PerfTimer:
    """Контекстный менеджер для профилирования блоков кода."""

    def __init__(self, label: str):
        self.label = label
        self.start_time = 0.0
        self.start_cpu = 0.0
        self.start_rss = 0.0

    def __enter__(self):
        self.start_rss = _get_rss_mb()
        self.start_cpu = time.process_time()
        self.start_time = time.perf_counter()
        return self

    def __exit__(self, *exc):
        elapsed = time.perf_counter() - self.start_time
        cpu_used = time.process_time() - self.start_cpu
        end_rss = _get_rss_mb()
        delta_rss = end_rss - self.start_rss
        log.info(
            "⏱ [%s] wall=%.3fs cpu=%.3fs RSS=%.1fMB (Δ%+.1fMB)",
            self.label, elapsed, cpu_used, end_rss, delta_rss,
        )
        return False


# ─────────────────────────── font bootstrap ───────────────────

def ensure_font() -> None:
    FONT_DIR.mkdir(exist_ok=True)
    if FONT_PATH.exists():
        log.info("Font already present: %s", FONT_PATH)
        return
    log.info("Downloading Caveat-Bold.ttf …")
    urllib.request.urlretrieve(FONT_URL, FONT_PATH)
    log.info("Font saved to %s", FONT_PATH)


# ─────────────────────────── font cache (LRU) ────────────────

@lru_cache(maxsize=32)
def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """Загрузка шрифта с кэшированием в памяти."""
    log.debug("Loading font size=%d (cache miss)", size)
    return ImageFont.truetype(str(FONT_PATH), size)


# ─── «Заглушка» для textbbox — один раз создаётся dummy image+draw ───
_DUMMY_IMG = Image.new("RGB", (1, 1))
_DUMMY_DRAW = ImageDraw.Draw(_DUMMY_IMG)


# ─────────────────────────── bg cache management ──────────────

def _ensure_bg_cache_dir() -> None:
    BG_CACHE_DIR.mkdir(exist_ok=True)
    if not BG_CACHE_META.exists():
        BG_CACHE_META.write_text("{}")


def _load_bg_meta() -> dict:
    try:
        return json.loads(BG_CACHE_META.read_text())
    except Exception:
        return {}


def _save_bg_meta(meta: dict) -> None:
    BG_CACHE_META.write_text(json.dumps(meta, indent=2))


def _avatar_file_hash(path: str) -> str:
    """SHA-256 первых 64 КБ файла аватарки."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(65536))
    return h.hexdigest()


def _get_cached_bg(user_id: int, avatar_hash: str) -> str | None:
    meta = _load_bg_meta()
    entry = meta.get(str(user_id))
    if not entry:
        return None
    if entry.get("hash") != avatar_hash:
        old_path = BG_CACHE_DIR / entry.get("file", "")
        if old_path.exists():
            old_path.unlink()
            log.info("Removed stale bg cache: %s", old_path)
        del meta[str(user_id)]
        _save_bg_meta(meta)
        return None
    cached_path = BG_CACHE_DIR / entry["file"]
    if cached_path.exists():
        return str(cached_path)
    return None


def _save_bg_to_cache(user_id: int, avatar_hash: str, bg_img: Image.Image) -> str:
    filename = f"bg_{user_id}.png"
    path = BG_CACHE_DIR / filename
    bg_img.save(str(path), "PNG", optimize=True)

    meta = _load_bg_meta()
    meta[str(user_id)] = {
        "hash": avatar_hash,
        "file": filename,
        "updated": time.time(),
    }
    _save_bg_meta(meta)
    log.info("Cached bg for user %d → %s", user_id, path)
    return str(path)


# ─────────────────────────── avatar download ──────────────────

async def _download_avatar(bot: Bot, user_id: int) -> str | None:
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if not photos.photos:
            return None
        photo_size = photos.photos[0][-1]
        file = await bot.get_file(photo_size.file_id)
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.close()
        await bot.download_file(file.file_path, tmp.name)
        log.info("Avatar downloaded for user %d → %s", user_id, tmp.name)
        return tmp.name
    except Exception as exc:
        log.warning("Could not download avatar for user %d: %s", user_id, exc)
        return None


def _get_author_user_id(msg: Message) -> int | None:
    fwd = msg.forward_origin
    if fwd:
        user = getattr(fwd, "sender_user", None)
        if user:
            return user.id
        return None
    if msg.from_user:
        return msg.from_user.id
    return None


# ─────────────────────────── image generation ─────────────────

def _estimate_font_size(
    text: str,
    max_width: int,
    max_text_height: int,
    step: int = 4,
    s_max: int = 92,
    s_min: int = MIN_FONT_SIZE,
) -> int:
    """
    Оценивает оптимальный размер шрифта за 2 вызова textbbox
    (вместо ~17 × N_words в цикле).

    Возвращает размер, выровненный по step, который с высокой
    вероятностью подойдёт. Требует одной верификации через _wrap_text.
    """
    s_ref = s_max
    font_ref = _load_font(s_ref)

    # ── Два единственных вызова textbbox ──────────────────────
    # 1) Полная ширина текста без переноса
    bbox = _DUMMY_DRAW.textbbox((0, 0), text, font=font_ref)
    w_total = bbox[2] - bbox[0]

    # 2) Высота строки
    h_bbox = _DUMMY_DRAW.textbbox((0, 0), "Ag", font=font_ref)
    h_line = (h_bbox[3] - h_bbox[1]) * LINE_SPACING_FACTOR

    if w_total <= 0 or h_line <= 0:
        return s_max

    # ── Если при s_max текст и так влезает в одну строку ──────
    if w_total <= max_width:
        return s_max

    # ── Формула: S ≤ √(H_avail × S_ref² × max_width / (W_total × H_line))
    s_squared = (max_text_height * s_ref * s_ref * max_width) / (w_total * h_line)
    s_est = math.isqrt(int(s_squared))  # округление вниз

    # Выравниваем вниз по шагу (консервативно)
    s_est = (s_est // step) * step

    # Зажимаем в допустимый диапазон
    return max(s_min, min(s_max, s_est))


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Перенос текста по словам с учётом ширины в пикселях."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        bbox = _DUMMY_DRAW.textbbox((0, 0), candidate, font=font)
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
    pilmoji_drawer: Pilmoji,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
    anchor: str = "lt",
    emoji_scale_factor: float = 1.0,
) -> None:
    """
    Рисуем текст с тенью через Pilmoji (поддержка эмодзи).

    Pilmoji.text() не поддерживает параметр anchor напрямую,
    поэтому для anchor="rt" (right-top) нужно вручную вычислять x.
    Здесь мы используем anchor только для вычисления позиции,
    а сам вызов pilmoji.text() всегда рисует с left-top.
    """
    x, y = xy
    shadows = [(4, 30), (2, 50), (1, 70)]
    for offset, alpha in shadows:
        shadow_color = (*BG_COLOR[:3], alpha)
        pilmoji_drawer.text(
            (x + offset, y + offset), text, font=font,
            fill=shadow_color,
            emoji_scale_factor=emoji_scale_factor,
        )
    pilmoji_drawer.text(
        (x, y), text, font=font, fill=fill,
        emoji_scale_factor=emoji_scale_factor,
    )


def _prepare_bg_from_photo(
    photo_path: str,
    canvas_w: int,
    canvas_h: int,
) -> Image.Image:
    with PerfTimer("prepare_bg"):
        photo = Image.open(photo_path).convert("RGB")

        canvas_ratio = canvas_w / canvas_h
        photo_ratio = photo.width / photo.height

        if photo_ratio < canvas_ratio:
            new_w = canvas_w
            new_h = int(canvas_w / photo_ratio)
        else:
            new_h = canvas_h
            new_w = int(canvas_h * photo_ratio)

        photo = photo.resize((new_w, new_h), Image.LANCZOS, reducing_gap=2.0)

        left = (new_w - canvas_w) // 2
        top = (new_h - canvas_h) // 2
        photo = photo.crop((left, top, left + canvas_w, top + canvas_h))

        enhancer = ImageEnhance.Brightness(photo)
        darkened = enhancer.enhance(DARKEN_FACTOR)

        result = darkened.convert("RGBA")
        photo.close()
        darkened.close()

        return result


def _prepare_bg_from_photo_cached(
    photo_path: str,
    user_id: int | None,
    canvas_w: int,
    canvas_h: int,
) -> Image.Image:
    if user_id is not None and photo_path:
        avatar_hash = _avatar_file_hash(photo_path)
        cached_path = _get_cached_bg(user_id, avatar_hash)

        if cached_path:
            log.info("Using cached bg for user %d", user_id)
            with PerfTimer("load_cached_bg"):
                img = Image.open(cached_path).convert("RGBA")
                if img.height != canvas_h:
                    top = (img.height - canvas_h) // 2
                    img = img.crop((0, max(0, top), canvas_w, max(0, top) + canvas_h))
                return img

        with PerfTimer("generate_and_cache_bg"):
            full_bg = _prepare_bg_from_photo(photo_path, IMG_WIDTH, IMG_MAX_HEIGHT)
            _save_bg_to_cache(user_id, avatar_hash, full_bg)

            if full_bg.height != canvas_h:
                top = (full_bg.height - canvas_h) // 2
                result = full_bg.crop((0, max(0, top), canvas_w, max(0, top) + canvas_h))
                full_bg.close()
                return result
            return full_bg

    return _prepare_bg_from_photo(photo_path, canvas_w, canvas_h)


# ── Предгенерированный «базовый» тёмный фон ──
_BASE_BG_CACHE: dict[int, Image.Image] = {}


def _get_dark_bg(canvas_h: int) -> Image.Image:
    if canvas_h in _BASE_BG_CACHE:
        return _BASE_BG_CACHE[canvas_h].copy()

    img = Image.new("RGBA", (IMG_WIDTH, canvas_h), (*BG_COLOR, 255))
    draw = ImageDraw.Draw(img)
    for row in range(canvas_h):
        alpha = int(18 * (1 - row / canvas_h))
        draw.line([(0, row), (IMG_WIDTH, row)], fill=(255, 255, 255, alpha))

    if len(_BASE_BG_CACHE) > 10:
        _BASE_BG_CACHE.clear()

    _BASE_BG_CACHE[canvas_h] = img
    return img.copy()


def generate_quote_image(
    quote: str,
    author: str,
    bg_image_path: str | None = None,
    author_user_id: int | None = None,
) -> str:
    """
    Build a premium quote card and save to a temp file.
    Returns temp file path.
    """
    with PerfTimer("generate_quote_image_total"):

        # ── 1. Оборачиваем цитату в ёлочки ──────────────────────────
        display_quote = f"«{quote}»"
        text_max_w = IMG_WIDTH - 2 * PAD_X
        max_text_h = IMG_MAX_HEIGHT - PAD_TOP - PAD_BOTTOM - 140

        # ── 2. Оценка оптимального размера шрифта (2 вызова textbbox) ─
        font_size = _estimate_font_size(display_quote, text_max_w, max_text_h)

        # ── 3. Верификация + корректировка (обычно 0–2 итерации) ──────
        font = _load_font(font_size)
        lines = _wrap_text(display_quote, font, text_max_w)
        sample_bbox = _DUMMY_DRAW.textbbox((0, 0), "Ag", font=font)
        line_h = int((sample_bbox[3] - sample_bbox[1]) * LINE_SPACING_FACTOR)

        while line_h * len(lines) > max_text_h and font_size > MIN_FONT_SIZE:
            font_size -= 4
            font = _load_font(font_size)
            lines = _wrap_text(display_quote, font, text_max_w)
            sample_bbox = _DUMMY_DRAW.textbbox((0, 0), "Ag", font=font)
            line_h = int((sample_bbox[3] - sample_bbox[1]) * LINE_SPACING_FACTOR)

        # ── 4. Шрифт автора и метрики ────────────────────────────────
        emoji_scale = max(0.85, min(1.15, font_size / 80))
        author_font_size = max(28, min(60, font_size - 24))
        author_font = _load_font(author_font_size)

        auth_bbox = _DUMMY_DRAW.textbbox((0, 0), f"— {author}", font=author_font)
        auth_h = auth_bbox[3] - auth_bbox[1]

        # ── 5. Высота холста ─────────────────────────────────────────
        gap_after_quote = int(line_h * 0.6)
        content_h = line_h * len(lines) + gap_after_quote + auth_h
        img_h = max(IMG_MIN_HEIGHT, min(IMG_MAX_HEIGHT, content_h + PAD_TOP + PAD_BOTTOM))

        start_y = (img_h - content_h) // 2

        # ── 6. Создание холста ───────────────────────────────────────
        with PerfTimer("canvas_creation"):
            if bg_image_path:
                img = _prepare_bg_from_photo_cached(
                    bg_image_path, author_user_id, IMG_WIDTH, img_h
                )
            else:
                img = _get_dark_bg(img_h)

        # ── 7. Рендеринг текста через Pilmoji ───────────────────────
        with Pilmoji(img) as pilmoji_drawer:

            # ── 7a. Строки цитаты (с ёлочками) ───────────────────────
            text_y = start_y
            for line in lines:
                lb = _DUMMY_DRAW.textbbox((0, 0), line, font=font)
                line_w = lb[2] - lb[0]
                x = (IMG_WIDTH - line_w) // 2
                _draw_text_with_shadow(
                    pilmoji_drawer, (x, text_y), line, font, TEXT_COLOR,
                    emoji_scale_factor=emoji_scale,
                )
                text_y += line_h

            # ── 7b. Акцентная линия ──────────────────────────────────
            draw = ImageDraw.Draw(img)
            line_y = text_y + gap_after_quote // 2
            accent_x1 = IMG_WIDTH - PAD_X - 200
            accent_x2 = IMG_WIDTH - PAD_X
            draw.line(
                [(accent_x1, line_y), (accent_x2, line_y)],
                fill=(180, 160, 140, 120), width=1,
            )

            # ── 7c. Автор (правое выравнивание вручную) ──────────────
            author_text = f"— {author}"
            auth_y = text_y + gap_after_quote

            ab = _DUMMY_DRAW.textbbox((0, 0), author_text, font=author_font)
            author_w = ab[2] - ab[0]
            author_x = IMG_WIDTH - PAD_X - author_w

            _draw_text_with_shadow(
                pilmoji_drawer,
                (author_x, auth_y),
                author_text,
                author_font,
                AUTHOR_COLOR,
                emoji_scale_factor=emoji_scale,
            )

        # ── 8. Сохранение JPEG ───────────────────────────────────────
        with PerfTimer("jpeg_save"):
            background = Image.new("RGB", img.size, BG_COLOR)
            background.paste(img, mask=img.split()[3])

            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            background.save(tmp.name, "JPEG", quality=88, optimize=True)
            tmp.close()

        img.close()
        background.close()
        gc.collect()

        return tmp.name


# ─────────────────────────── router / handlers ────────────────
router = Router()


def _get_author(msg: Message) -> str:
    fwd = msg.forward_origin
    if fwd:
        user = getattr(fwd, "sender_user", None)
        if user:
            parts = [user.first_name or "", user.last_name or ""]
            name = " ".join(p for p in parts if p).strip()
            return name or (f"@{user.username}" if user.username else "Аноним")
        sender_name = getattr(fwd, "sender_user_name", None)
        if sender_name:
            return sender_name
        chat = getattr(fwd, "chat", None)
        if chat:
            return chat.title or "Аноним"

    sender = msg.from_user
    if sender:
        parts = [sender.first_name or "", sender.last_name or ""]
        name = " ".join(p for p in parts if p).strip()
        return name or (f"@{sender.username}" if sender.username else "Аноним")
    return "Аноним"


@router.message(
    Command("цитата"),
    F.chat.type.in_({"supergroup", "group"}),
    F.message_thread_id == None
)
async def cmd_quote(message: Message) -> None:
    reply = message.reply_to_message

    if not reply:
        await message.reply("↩️ Ответьте командой /цитата на сообщение с цитатой.")
        return

    quote_text: str | None = reply.text or reply.caption
    if not quote_text or not quote_text.strip():
        await message.reply("😕 В сообщении нет текста для цитаты.")
        return

    quote_text = quote_text.strip()

    # ── Лимит символов ────────────────────────────────────────
    if len(quote_text) > MAX_QUOTE_LENGTH:
        await message.reply(
            f"📏 Цитата слишком длинная ({len(quote_text)} симв.). "
            f"Максимум — {MAX_QUOTE_LENGTH}."
        )
        return

    author = _get_author(reply)

    # ── скачиваем аватарку ────────────────────────────────────
    avatar_path: str | None = None
    author_user_id = _get_author_user_id(reply)
    if author_user_id is not None:
        avatar_path = await _download_avatar(message.bot, author_user_id)

    log.info(
        "Generating quote for chat_id=%d author=%r, len=%d, avatar=%s",
        message.chat.id, author, len(quote_text), "yes" if avatar_path else "no",
    )

    # ── Семафор: ограничиваем одновременные генерации ─────────
    async with IMAGE_GEN_SEMAPHORE:
        log.info(
            "Semaphore acquired (active=%d/%d), RSS=%.1fMB",
            IMAGE_GEN_SEMAPHORE._value,
            2,
            _get_rss_mb(),
        )
        try:
            loop = asyncio.get_running_loop()
            img_path = await loop.run_in_executor(
                IMAGE_EXECUTOR,
                generate_quote_image,
                quote_text,
                author,
                avatar_path,
                author_user_id,
            )
        except Exception as exc:
            log.error("Image generation failed: %s", exc, exc_info=True)
            await message.reply("❌ Не удалось создать картинку. Попробуйте позже.")
            return
        finally:
            if avatar_path:
                try:
                    os.unlink(avatar_path)
                except OSError:
                    pass

    # ── send photo ────────────────────────────────────────────
    thread_id = SUPERCHAT_TO_THREAD_MAP.get(message.chat.id, QUOTE_THREAD_ID)

    try:
        photo = FSInputFile(img_path)
        await message.bot.send_photo(
            chat_id=message.chat.id,
            photo=photo,
            message_thread_id=thread_id,
        )
    except Exception as exc:
        log.error("Failed to send photo: %s", exc, exc_info=True)
        await message.reply("❌ Не удалось отправить картинку.")
    finally:
        try:
            os.unlink(img_path)
        except OSError:
            pass


@router.message(Command("цитата"))
async def cmd_quote_wrong_thread(message: Message) -> None:
    log.debug(
        "Ignored /цитата from chat=%s thread=%s",
        message.chat.id,
        message.message_thread_id,
    )


# ─────────────────────────── lifecycle ────────────────────────

async def on_startup(bot: Bot) -> None:
    me = await bot.get_me()
    log.info("Bot started: @%s (id=%d)", me.username, me.id)
    log.info("Initial RSS: %.1f MB", _get_rss_mb())


async def on_shutdown(bot: Bot) -> None:
    log.info("Bot shutting down, RSS: %.1f MB", _get_rss_mb())
    IMAGE_EXECUTOR.shutdown(wait=False)
    await bot.session.close()


# ─────────────────────────── entry point ──────────────────────

async def main() -> None:
    ensure_font()
    _ensure_bg_cache_dir()

    # Предварительно загрузим шрифты в кэш (расширенный диапазон до 28)
    for size in range(28, 96, 4):
        _load_font(size)
    # Плюс размеры для автора
    for size in (28, 32, 36, 40, 44, 48, 52, 56, 60):
        _load_font(size)
    log.info("Font cache warmed up (%d entries)", _load_font.cache_info().currsize)

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    log.info("Starting polling …")
    await dp.start_polling(bot, allowed_updates=["message"])


if __name__ == "__main__":
    asyncio.run(main())