import asyncio
import gc
import hashlib
import json
import logging
import math
import os
import resource
import tempfile
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from supabase import create_client, Client as SupabaseClient

import emoji as emoji_lib
from dotenv import load_dotenv

from PIL import Image, ImageDraw, ImageEnhance, ImageFont
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultPhoto,
    InputTextMessageContent,
    Message,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from groq import AsyncGroq


# ─────────────────────────── config ───────────────────────────

load_dotenv()

BOT_TOKEN: str = os.environ["BOT_TOKEN"]

SUPERCHAT_TO_THREAD_MAP = {
    -1002692670592: 188271,
    -1003721142275: 2
}

QUOTE_THREAD_ID: int = int(os.environ["QUOTE_THREAD_ID"])

# Webhook config (для Render)
WEBHOOK_HOST = os.environ.get("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.environ.get("PORT", "10000"))  # Render использует PORT env
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")  # Полный URL для webhook
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
BOT_MODE = os.environ.get("BOT_MODE", "webhook")  # "webhook" или "polling"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

# Supabase config
SUPABASE_URL: str = os.environ["SUPABASE_URL"]
SUPABASE_KEY: str = os.environ["SUPABASE_KEY"]
SUPABASE_BUCKET: str = os.environ.get("SUPABASE_BUCKET", "quote-backgrounds")

FONT_DIR = Path("./fonts")
FONT_PATH = FONT_DIR / "Caveat-Bold.ttf"
FONT_URL = "https://github.com/googlefonts/caveat/raw/main/fonts/ttf/Caveat-Bold.ttf"

# Шрифт для водяного знака
WATERMARK_FONT_PATH = FONT_DIR / "Roboto-Regular.ttf"
WATERMARK_FONT_URL = "https://github.com/googlefonts/roboto/raw/main/src/hinted/Roboto-Regular.ttf"
WATERMARK_TEXT = "@chpi_quote_bot"
WATERMARK_FONT_SIZE = 22
WATERMARK_COLOR = (180, 180, 180, 180)
WATERMARK_PADDING = 20

# Директория кэша фонов аватаров
BG_CACHE_DIR = Path("./bg_cache")
BG_CACHE_META = BG_CACHE_DIR / "_meta.json"

# Директория кэша PNG-эмодзи (Twemoji)
EMOJI_CACHE_DIR = Path("./emoji_cache")
TWEMOJI_CDN = "https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/"

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

MAX_QUOTE_LENGTH = 800
MIN_FONT_SIZE = 28

MAX_SEMAPHORE_COUNT = 4
MAX_WORKERS = 4
GROQ_WHISPER_MODEL = "whisper-large-v3-turbo"
VOICE_AUTO_TRANSCRIBE_ENABLED = False

IMAGE_GEN_SEMAPHORE = asyncio.Semaphore(MAX_SEMAPHORE_COUNT)
IMAGE_EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="img_gen")

# Inline Pinterest search
INLINE_PAGE_SIZE = 50
INLINE_CACHE_TTL_SECONDS = 15 * 60
INLINE_MAX_PAGES = 100
PINTEREST_SEARCH_WORKERS = 4

PINTEREST_SEARCH_SEMAPHORE = asyncio.Semaphore(PINTEREST_SEARCH_WORKERS)
PINTEREST_SEARCH_EXECUTOR = ThreadPoolExecutor(
    max_workers=PINTEREST_SEARCH_WORKERS,
    thread_name_prefix="pin_search",
)

# ─────────────────────────── supabase client ──────────────────

def _get_supabase() -> SupabaseClient:
    """Возвращает синхронный Supabase-клиент (создаётся один раз через lru_cache)."""
    return create_client(SUPABASE_URL, SUPABASE_KEY)

# Создаём клиент один раз при старте
_supabase_client: SupabaseClient = _get_supabase()


# ─────────────────────────── supabase storage helpers ─────────

def _supabase_retry(fn, retries: int = 3, delay: float = 1.5):
    """Повторяет вызов fn при сетевых ошибках."""
    last_exc = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            log.warning("Supabase retry %d/%d: %s", attempt + 1, retries, exc)
            if attempt < retries - 1:
                time.sleep(delay)
    if last_exc:
        raise last_exc


def supabase_upload_image(local_path: str, storage_path: str) -> str:
    with open(local_path, "rb") as f:
        data = f.read()
    mime = "image/jpeg" if local_path.lower().endswith(".jpg") else "image/png"

    def _do():
        _supabase_client.storage.from_(SUPABASE_BUCKET).upload(
            path=storage_path,
            file=data,
            file_options={"content-type": mime, "upsert": "true"},
        )

    _supabase_retry(_do)
    log.info("Supabase: uploaded %s → %s", local_path, storage_path)
    return _supabase_client.storage.from_(SUPABASE_BUCKET).get_public_url(storage_path)


def supabase_download_image(storage_path: str, local_path: str) -> None:
    def _do():
        data: bytes = _supabase_client.storage.from_(SUPABASE_BUCKET).download(storage_path)
        with open(local_path, "wb") as f:
            f.write(data)

    _supabase_retry(_do)
    log.info("Supabase: downloaded %s → %s", storage_path, local_path)


def supabase_delete_image(storage_path: str) -> None:
    """
    Удаляет файл из Supabase Storage.
    storage_path — путь внутри бакета.
    """
    try:
        _supabase_client.storage.from_(SUPABASE_BUCKET).remove([storage_path])
        log.info("Supabase: deleted %s", storage_path)
    except Exception as exc:
        log.error("Supabase delete failed (%s): %s", storage_path, exc)
        raise


def supabase_image_exists(storage_path: str) -> bool:
    """
    Проверяет наличие файла в бакете через list().
    Возвращает True, если файл найден.
    """
    try:
        # Разбиваем путь на директорию и имя файла
        parts = storage_path.rsplit("/", 1)
        folder = parts[0] if len(parts) == 2 else ""
        filename = parts[-1]

        files = _supabase_client.storage.from_(SUPABASE_BUCKET).list(
            path=folder,
            options={"search": filename},
        )
        return any(f.get("name") == filename for f in (files or []))
    except Exception as exc:
        log.warning("Supabase exists-check failed (%s): %s", storage_path, exc)
        return False

# ─────────────────────────── profiling helpers ────────────────

def _get_memory_mb() -> float:
    try:
        usage = resource(resource.RUSAGE_SELF)
        rss_kb = usage.ru_maxrss
        if os.uname().sysname == "Darwin":
            return rss_kb / 1024 / 1024
        return rss_kb / 1024
    except Exception:
        return 0.0


def _get_rss_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    return _get_memory_mb()


class PerfTimer:
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


def ensure_watermark_font() -> None:
    FONT_DIR.mkdir(exist_ok=True)
    if WATERMARK_FONT_PATH.exists():
        log.info("Watermark font already present: %s", WATERMARK_FONT_PATH)
        return
    log.info("Downloading Roboto-Regular.ttf …")
    urllib.request.urlretrieve(WATERMARK_FONT_URL, WATERMARK_FONT_PATH)
    log.info("Watermark font saved to %s", WATERMARK_FONT_PATH)


# ─────────────────────────── font cache (LRU) ────────────────

@lru_cache(maxsize=32)
def _load_font(size: int) -> ImageFont.FreeTypeFont:
    log.debug("Loading font size=%d (cache miss)", size)
    return ImageFont.truetype(str(FONT_PATH), size)


@lru_cache(maxsize=4)
def _load_watermark_font(size: int) -> ImageFont.FreeTypeFont:
    log.debug("Loading watermark font size=%d (cache miss)", size)
    return ImageFont.truetype(str(WATERMARK_FONT_PATH), size)


_DUMMY_IMG = Image.new("RGB", (1, 1))
_DUMMY_DRAW = ImageDraw.Draw(_DUMMY_IMG)


# ─────────────────────────── emoji rendering ──────────────────

def _ensure_emoji_cache() -> None:
    EMOJI_CACHE_DIR.mkdir(exist_ok=True)


def _emoji_to_twemoji_name(em_str: str) -> str:
    """
    Конвертирует строку эмодзи в имя файла Twemoji.
    Правило: соединяем кодпоинты через «-», исключая U+FE0F (variation selector-16).
    """
    cps = [f"{ord(c):x}" for c in em_str if ord(c) != 0xFE0F]
    return "-".join(cps)


# Кэш в памяти: {(emoji_str, size): Image | None}
_emoji_img_cache: dict[tuple[str, int], Image.Image | None] = {}


def _get_emoji_image(em_str: str, size: int) -> Image.Image | None:
    """
    Возвращает RGBA-изображение эмодзи размером size×size.
    Скачивает PNG из Twemoji CDN и кэширует на диск + в памяти.
    """
    cache_key = (em_str, size)
    if cache_key in _emoji_img_cache:
        cached = _emoji_img_cache[cache_key]
        return cached.copy() if cached is not None else None

    base_name = _emoji_to_twemoji_name(em_str)
    disk_path = EMOJI_CACHE_DIR / f"{base_name}.png"

    # Попытка скачать (без FE0F)
    if not disk_path.exists():
        url = TWEMOJI_CDN + f"{base_name}.png"
        try:
            urllib.request.urlretrieve(url, disk_path)
        except Exception as e1:
            # Fallback: попробуем с FE0F
            alt_name = "-".join(f"{ord(c):x}" for c in em_str)
            alt_path = EMOJI_CACHE_DIR / f"{alt_name}.png"
            if not alt_path.exists():
                try:
                    urllib.request.urlretrieve(TWEMOJI_CDN + f"{alt_name}.png", alt_path)
                    disk_path = alt_path
                except Exception as e2:
                    log.warning("Emoji download failed %r (tried %s and %s): %s / %s",
                                em_str, base_name, alt_name, e1, e2)
                    _emoji_img_cache[cache_key] = None
                    return None
            else:
                disk_path = alt_path

    try:
        full = Image.open(disk_path).convert("RGBA")
        resized = full.resize((size, size), Image.LANCZOS)
        full.close()
        _emoji_img_cache[cache_key] = resized
        return resized.copy()
    except Exception as e:
        log.warning("Cannot open emoji image %s: %s", disk_path, e)
        _emoji_img_cache[cache_key] = None
        return None


def _segment_text(text: str) -> list[tuple[str, bool]]:
    """
    Разбивает текст на сегменты [(строка, is_emoji), ...].
    is_emoji=True — этот сегмент нужно рендерить как эмодзи-изображение.
    """
    result: list[tuple[str, bool]] = []
    last = 0
    for em in emoji_lib.emoji_list(text):
        start, end = em["match_start"], em["match_end"]
        if last < start:
            result.append((text[last:start], False))
        result.append((em["emoji"], True))
        last = end
    if last < len(text):
        result.append((text[last:], False))
    return result


def _measure_line_width(text: str, font: ImageFont.FreeTypeFont, emoji_size: int) -> int:
    """Измеряет ширину строки с учётом эмодзи (каждый = emoji_size пикселей)."""
    total = 0
    for seg, is_emoji_seg in _segment_text(text):
        if is_emoji_seg:
            # emoji_list возвращает по одному объекту на каждую кластер-последовательность
            total += len(emoji_lib.emoji_list(seg)) * emoji_size
        elif seg:
            bbox = _DUMMY_DRAW.textbbox((0, 0), seg, font=font)
            total += bbox[2] - bbox[0]
    return total


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
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(65536))
    return h.hexdigest()


def _get_cached_bg(user_id: int, avatar_hash: str) -> str | None:
    meta = _load_bg_meta()
    entry = meta.get(str(user_id))
    
    if not entry:
        filename = f"bg_{user_id}.png"
        storage_path = f"bg_cache/{filename}"
        cached_path = BG_CACHE_DIR / filename
        if supabase_image_exists(storage_path):
            try:
                supabase_download_image(storage_path, str(cached_path))
                # Восстанавливаем мету
                meta[str(user_id)] = {
                    "hash": avatar_hash,
                    "file": filename,
                    "updated": time.time(),
                }
                _save_bg_meta(meta)
                log.info("Restored lost meta from Supabase for user %d", user_id)
                return str(cached_path)
            except Exception as exc:
                log.warning("Supabase meta restore failed for user %d: %s", user_id, exc)
        return None
    
    if entry.get("hash") != avatar_hash:
        old_file = entry.get("file", "")
        old_path = BG_CACHE_DIR / old_file
        if old_path.exists():
            old_path.unlink()
            log.info("Removed stale local bg cache: %s", old_path)
        # Удаляем и из Supabase
        if old_file:
            try:
                supabase_delete_image(f"bg_cache/{old_file}")
            except Exception as exc:
                log.warning("Supabase stale bg delete failed: %s", exc)
        del meta[str(user_id)]
        _save_bg_meta(meta)
        return None
    
    cached_path = BG_CACHE_DIR / entry["file"]
    
    if cached_path.exists():
        return str(cached_path)

    # Локальный файл пропал (например, после редеплоя на Render) —
    # пробуем восстановить из Supabase
    storage_path = f"bg_cache/{entry['file']}"
    if supabase_image_exists(storage_path):
        try:
            supabase_download_image(storage_path, str(cached_path))
            log.info("Restored bg from Supabase for user %d", user_id)
            return str(cached_path)
        except Exception as exc:
            log.warning("Supabase restore failed for user %d: %s", user_id, exc)
    
    return None


def _save_bg_to_cache(user_id: int, avatar_hash: str, bg_img: Image.Image) -> str:
    filename = f"bg_{user_id}.png"
    path = BG_CACHE_DIR / filename
    bg_img.save(str(path), "PNG", optimize=True)

    # ── Supabase Storage ─────────────────────────────────────
    storage_path = f"bg_cache/{filename}"
    try:
        supabase_upload_image(str(path), storage_path)
    except Exception as exc:
        log.warning("Supabase bg upload skipped: %s", exc)
    # ─────────────────────────────────────────────────────────

    meta = _load_bg_meta()
    meta[str(user_id)] = {
        "hash": avatar_hash,
        "file": filename,
        "updated": time.time(),
    }
    _save_bg_meta(meta)
    log.info("Cached bg for user %d → %s", user_id, path)
    return str(path)


def _bg_cache_exists_for_avatar(user_id: int | None, avatar_path: str | None) -> bool:
    """Проверяет, есть ли актуальный кэш фона для данной аватарки."""
    if user_id is None or not avatar_path:
        return True  # Не нужно кэширование с аватаркой
    avatar_hash = _avatar_file_hash(avatar_path)
    return _get_cached_bg(user_id, avatar_hash) is not None


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


# ─────────────────────────── message link ─────────────────────

def _get_message_link(chat_id: int, message_id: int) -> str:
    """Генерирует ссылку на сообщение в Telegram."""
    # Для супергрупп chat_id начинается с -100, нужно убрать это
    if chat_id < 0:
        # Убираем -100 из начала
        chat_id_str = str(chat_id)
        if chat_id_str.startswith("-100"):
            chat_id_clean = chat_id_str[4:]
        else:
            chat_id_clean = chat_id_str[1:]  # Убираем только минус
        return f"https://t.me/c/{chat_id_clean}/{message_id}"
    else:
        return f"https://t.me/c/{chat_id}/{message_id}"


def _create_go_to_reply_message_keyboard(chat_id: int, message_id: int) -> InlineKeyboardMarkup:
    """Создаёт клавиатуру с кнопкой-ссылкой на оригинальное сообщение."""
    link = _get_message_link(chat_id, message_id)
    button = InlineKeyboardButton(text="💬 К сообщению", url=link)
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


# ─────────────────────────── inline pinterest search ──────────

@dataclass
class PinterestInlinePage:
    items: list[dict]
    next_bookmark: str | None


@dataclass
class CachedPinterestQuery:
    updated_at: float
    bookmarks: dict[int, str | None] = field(default_factory=lambda: {0: None})
    pages: dict[int, list[dict]] = field(default_factory=dict)
    has_more: dict[int, bool] = field(default_factory=dict)


def _truncate(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _looks_like_jpeg(url: str | None) -> bool:
    if not url:
        return False
    path = urllib.parse.urlparse(url).path.lower()
    return path.endswith(".jpg") or path.endswith(".jpeg")


def _extract_search_pin_images(raw_pin: dict) -> dict[str, dict]:
    images: dict[str, dict] = {}

    raw_images = raw_pin.get("images")
    if isinstance(raw_images, dict):
        for size, meta in raw_images.items():
            if isinstance(meta, dict) and meta.get("url"):
                images[str(size)] = {
                    "url": meta["url"],
                    "width": meta.get("width"),
                    "height": meta.get("height"),
                }
            elif isinstance(meta, str) and meta:
                images[str(size)] = {"url": meta, "width": None, "height": None}

    for key, val in raw_pin.items():
        if key.startswith("images_") and isinstance(val, dict) and val.get("url"):
            size = key.replace("images_", "")
            images.setdefault(size, {
                "url": val["url"],
                "width": val.get("width"),
                "height": val.get("height"),
            })

    flat_candidates = (
        ("orig", raw_pin.get("imageLargeUrl") or raw_pin.get("image_xlarge_url")),
        ("736x", raw_pin.get("image_medium_url") or raw_pin.get("imageMediumUrl")),
        ("474x", raw_pin.get("image_large_url") or raw_pin.get("imageLargeUrl")),
        ("236x", raw_pin.get("image_small_url") or raw_pin.get("imageSmallUrl")),
    )

    for size, url in flat_candidates:
        if isinstance(url, str) and url:
            images.setdefault(size, {"url": url, "width": None, "height": None})

    return images


def _pick_image(
    images: dict[str, dict],
    preferred_sizes: tuple[str, ...],
    jpeg_only: bool = False,
) -> dict | None:
    for size in preferred_sizes:
        item = images.get(size)
        if not item:
            continue
        url = item.get("url")
        if not url:
            continue
        if jpeg_only and not _looks_like_jpeg(url):
            continue
        return item

    for item in images.values():
        url = item.get("url")
        if not url:
            continue
        if jpeg_only and not _looks_like_jpeg(url):
            continue
        return item

    return None


def _normalize_search_pin(raw_pin: dict) -> dict | None:
    pin_id = str(raw_pin.get("id") or raw_pin.get("pin_id") or "").strip()
    if not pin_id:
        return None

    images = _extract_search_pin_images(raw_pin)

    photo_meta = (
        _pick_image(images, ("736x", "564x", "474x", "orig", "1200x"), jpeg_only=True)
        or _pick_image(images, ("736x", "564x", "474x", "orig", "1200x"))
    )
    thumb_meta = (
        _pick_image(images, ("236x", "474x", "564x", "736x"), jpeg_only=True)
        or _pick_image(images, ("236x", "474x", "564x", "736x"))
        or photo_meta
    )

    if not photo_meta or not thumb_meta:
        return None

    photo_url = photo_meta.get("url")
    thumb_url = thumb_meta.get("url")
    if not photo_url or not thumb_url:
        return None

    title = (
        raw_pin.get("title")
        or raw_pin.get("grid_title")
        or raw_pin.get("seo_description")
        or raw_pin.get("description")
        or "Pinterest"
    )
    description = raw_pin.get("description") or raw_pin.get("seo_description") or ""

    return {
        "pin_id": pin_id,
        "photo_url": photo_url,
        "thumbnail_url": thumb_url,
        "width": photo_meta.get("width"),
        "height": photo_meta.get("height"),
        "title": _truncate(title, 80),
        "description": _truncate(description, 120),
        "pin_link": f"https://www.pinterest.com/pin/{pin_id}/",
    }


class PinterestSearchPager:
    """
    Самостоятельный клиент поиска Pinterest (без наследования от Pinterest).
    Реализует bookmark-пагинацию через внутренний API Pinterest.
    """

    def __init__(self):
        import http.cookiejar
        self._cj = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cj)
        )
        self._ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
        self._timeout = 20
        self._opener.addheaders = [("User-Agent", self._ua)]

    def _fetch(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": self._ua})
        resp = self._opener.open(req, timeout=self._timeout)
        return resp.read()

    def _bootstrap_session(self) -> tuple[str, str]:
        self._fetch("https://www.pinterest.com/")
        csrf = ""
        for c in self._cj:
            if c.name == "csrftoken":
                csrf = c.value
                break
        cookies = "; ".join(f"{c.name}={c.value}" for c in self._cj)
        return csrf, cookies

    @staticmethod
    def _extract_next_bookmark(payload: dict) -> str | None:
        rr = payload.get("resource_response", {}) if isinstance(payload, dict) else {}
        bookmark = rr.get("bookmark")

        if not bookmark:
            bookmarks = rr.get("bookmarks")
            if isinstance(bookmarks, list) and bookmarks:
                bookmark = bookmarks[0]
            elif isinstance(bookmarks, str):
                bookmark = bookmarks

        if bookmark in (None, "", "-end-"):
            return None
        return bookmark

    @staticmethod
    def _extract_results(payload: dict) -> list[dict]:
        rr = payload.get("resource_response", {}) if isinstance(payload, dict) else {}
        data = rr.get("data", {})
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            results = data.get("results")
            if isinstance(results, list):
                return results
        return []

    def _common_headers(
        self,
        query: str,
        csrf: str,
        cookies: str,
        source_url: str,
    ) -> dict[str, str]:
        return {
            "User-Agent": self._ua,
            "Accept": "application/json, text/javascript, */*, q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRFToken": csrf,
            "X-Pinterest-AppState": "active",
            "X-Pinterest-Source-Url": source_url,
            "Cookie": cookies,
            "Referer": f"https://www.pinterest.com{source_url}",
        }

    def _search_get(self, query: str, bookmark: str | None, limit: int) -> dict:
        csrf, cookies = self._bootstrap_session()

        source_url = f"/search/pins/?q={urllib.parse.quote(query)}"
        payload = {
            "options": {
                "query": query,
                "scope": "pins",
                "page_size": limit,
                "bookmarks": [bookmark] if bookmark else [],
            },
            "context": {},
        }

        params = urllib.parse.urlencode({
            "source_url": source_url,
            "data": json.dumps(payload, separators=(",", ":")),
        })
        url = f"https://www.pinterest.com/resource/BaseSearchResource/get/?{params}"

        req = urllib.request.Request(url, headers=self._common_headers(query, csrf, cookies, source_url))
        resp = self._opener.open(req, timeout=self._timeout)
        return json.loads(resp.read().decode("utf-8", errors="replace"))

    def _search_post(self, query: str, bookmark: str | None, limit: int) -> dict:
        csrf, cookies = self._bootstrap_session()

        source_url = f"/search/pins/?q={urllib.parse.quote(query)}"
        payload = {
            "options": {
                "query": query,
                "scope": "pins",
                "page_size": limit,
                "bookmarks": [bookmark] if bookmark else [],
            },
            "context": {},
        }

        body = urllib.parse.urlencode({
            "source_url": source_url,
            "data": json.dumps(payload, separators=(",", ":")),
        }).encode("utf-8")

        headers = self._common_headers(query, csrf, cookies, source_url)
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"

        req = urllib.request.Request(
            "https://www.pinterest.com/resource/BaseSearchResource/get/",
            data=body,
            headers=headers,
            method="POST",
        )
        resp = self._opener.open(req, timeout=self._timeout)
        return json.loads(resp.read().decode("utf-8", errors="replace"))

    def search_page(
        self,
        query: str,
        bookmark: str | None = None,
        limit: int = INLINE_PAGE_SIZE,
    ) -> PinterestInlinePage:
        query = query.strip()
        if not query:
            return PinterestInlinePage(items=[], next_bookmark=None)

        last_exc: Exception | None = None
        payload: dict | None = None

        for method in (self._search_get, self._search_post):
            try:
                payload = method(query, bookmark, limit)
                break
            except Exception as exc:
                last_exc = exc
                log.warning("Pinterest search method %s failed: %s", method.__name__, exc)

        if payload is None:
            raise RuntimeError(f"Pinterest search failed: {last_exc}")

        raw_results = self._extract_results(payload)
        next_bookmark = self._extract_next_bookmark(payload)

        items: list[dict] = []
        seen_pin_ids: set[str] = set()

        for raw_pin in raw_results:
            if not isinstance(raw_pin, dict):
                continue
            pin = _normalize_search_pin(raw_pin)
            if not pin:
                continue
            if pin["pin_id"] in seen_pin_ids:
                continue
            seen_pin_ids.add(pin["pin_id"])
            items.append(pin)

        if next_bookmark == bookmark:
            next_bookmark = None

        return PinterestInlinePage(items=items, next_bookmark=next_bookmark)


class PinterestInlineSearchService:
    def __init__(self, ttl_seconds: int, page_size: int):
        self.ttl_seconds = ttl_seconds
        self.page_size = page_size
        self._cache: dict[tuple[int, str], CachedPinterestQuery] = {}
        self._lock = asyncio.Lock()

    def _cleanup(self) -> None:
        now = time.time()
        expired = [
            key for key, value in self._cache.items()
            if now - value.updated_at > self.ttl_seconds
        ]
        for key in expired:
            del self._cache[key]

    def _fetch_page_sync(self, query: str, bookmark: str | None) -> PinterestInlinePage:
        client = PinterestSearchPager()
        return client.search_page(query=query, bookmark=bookmark, limit=self.page_size)

    async def _fetch_page(self, query: str, bookmark: str | None) -> PinterestInlinePage:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            PINTEREST_SEARCH_EXECUTOR,
            self._fetch_page_sync,
            query,
            bookmark,
        )

    async def get_page(
        self,
        user_id: int,
        query: str,
        page: int,
    ) -> tuple[list[dict], bool]:
        page = max(0, min(page, INLINE_MAX_PAGES - 1))
        key = (user_id, query.casefold())

        async with self._lock:
            self._cleanup()

            session = self._cache.get(key)
            if session is None:
                session = CachedPinterestQuery(updated_at=time.time())
                self._cache[key] = session
            else:
                session.updated_at = time.time()

            current_page = 0
            while current_page <= page:
                if current_page in session.pages:
                    current_page += 1
                    continue

                if current_page > 0 and not session.has_more.get(current_page - 1, True):
                    session.pages[current_page] = []
                    session.has_more[current_page] = False
                    break

                bookmark = session.bookmarks.get(current_page)
                result = await self._fetch_page(query, bookmark)

                session.pages[current_page] = result.items
                session.has_more[current_page] = bool(result.next_bookmark and result.items)
                session.bookmarks[current_page + 1] = result.next_bookmark
                current_page += 1

            return (
                session.pages.get(page, []),
                session.has_more.get(page, False),
            )


PINTEREST_INLINE_SERVICE = PinterestInlineSearchService(
    ttl_seconds=INLINE_CACHE_TTL_SECONDS,
    page_size=INLINE_PAGE_SIZE,
)


def _create_pinterest_result_keyboard(pin_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📌 Открыть в Pinterest", url=pin_url)]
        ]
    )


# ─────────────────────────── watermark ────────────────────────

def _draw_watermark(img: Image.Image, draw: ImageDraw.ImageDraw) -> None:
    """Рисует водяной знак @chpi_quote_bot в левом нижнем углу."""
    font = _load_watermark_font(WATERMARK_FONT_SIZE)
    bbox = draw.textbbox((0, 0), WATERMARK_TEXT, font=font)
    text_h = bbox[3] - bbox[1]
    x = WATERMARK_PADDING
    y = img.height - text_h - WATERMARK_PADDING
    # Тень для читаемости
    draw.text((x + 1, y + 1), WATERMARK_TEXT, font=font, fill=(0, 0, 0, 120))
    # Основной текст
    draw.text((x, y), WATERMARK_TEXT, font=font, fill=WATERMARK_COLOR)


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
    Быстро оценивает оптимальный размер шрифта (2 вызова textbbox).
    Для текста с эмодзи может переоценить — корректируется в цикле верификации.
    """
    s_ref = s_max
    font_ref = _load_font(s_ref)

    bbox = _DUMMY_DRAW.textbbox((0, 0), text, font=font_ref)
    w_total = bbox[2] - bbox[0]

    h_bbox = _DUMMY_DRAW.textbbox((0, 0), "Ag", font=font_ref)
    h_line = (h_bbox[3] - h_bbox[1]) * LINE_SPACING_FACTOR

    if w_total <= 0 or h_line <= 0:
        return s_max

    if w_total <= max_width:
        return s_max

    s_squared = (max_text_height * s_ref * s_ref * max_width) / (w_total * h_line)
    s_est = math.isqrt(int(s_squared))
    s_est = (s_est // step) * step
    return max(s_min, min(s_max, s_est))


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """
    Перенос текста по словам с учётом:
    - ширины смешанного контента (текст + эмодзи)
    - «слов» без пробелов (строки из одних эмодзи)
    """
    sample_bbox = _DUMMY_DRAW.textbbox((0, 0), "Ag", font=font)
    # Приближённый размер эмодзи = высота строки * 0.9
    emoji_size = max(16, int((sample_bbox[3] - sample_bbox[1]) * 0.9))

    def measure(s: str) -> int:
        return _measure_line_width(s, font, emoji_size)

    def split_long_word(word: str) -> list[str]:
        """Разбивает слишком длинное слово (или строку эмодзи) по символам/кластерам."""
        # Получаем список «атомарных» единиц: каждый эмодзи-кластер — одна единица
        atoms: list[str] = []
        last_pos = 0
        for em in emoji_lib.emoji_list(word):
            for ch in word[last_pos:em["match_start"]]:
                atoms.append(ch)
            atoms.append(em["emoji"])
            last_pos = em["match_end"]
        for ch in word[last_pos:]:
            atoms.append(ch)

        sub_lines: list[str] = []
        current = ""
        for atom in atoms:
            candidate = current + atom
            if measure(candidate) <= max_width:
                current = candidate
            else:
                if current:
                    sub_lines.append(current)
                current = atom
        if current:
            sub_lines.append(current)
        return sub_lines

    words = text.split()
    lines: list[str] = []
    current = ""

    for word in words:
        candidate = (current + " " + word).strip() if current else word
        if measure(candidate) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
                current = ""

            if measure(word) <= max_width:
                current = word
            else:
                # Слово слишком длинное — дробим посимвольно
                for sub in split_long_word(word):
                    if not current:
                        current = sub
                    else:
                        candidate2 = current + sub
                        if measure(candidate2) <= max_width:
                            current = candidate2
                        else:
                            lines.append(current)
                            current = sub

    if current:
        lines.append(current)
    return lines


def _draw_line_with_shadow(
    draw: ImageDraw.ImageDraw,
    img: Image.Image,
    x: int,
    y: int,
    text: str,
    font: ImageFont.FreeTypeFont,
    emoji_size: int,
    fill: tuple,
    line_h: int,
) -> None:
    """
    Рисует строку смешанного текста (текст + эмодзи) с тенью.

    • Обычный текст — через ImageDraw.text() с RGBA-тенями
    • Эмодзи — Twemoji PNG, вертикально центрированные по метрикам шрифта
    """
    segments = _segment_text(text)

    # Вертикальное смещение для центровки эмодзи
    # textbbox при anchor lt: y0 — отступ сверху (обычно отрицательный у рукописных шрифтов)
    sample = _DUMMY_DRAW.textbbox((0, 0), "Ag", font=font)
    text_top_offset = sample[1]      # обычно <= 0 для кириллицы/латиницы
    text_bot_offset = sample[3]
    text_visual_center = (text_top_offset + text_bot_offset) / 2
    emoji_y = int(y + text_visual_center - emoji_size / 2)

    current_x = x

    for seg, is_emoji_seg in segments:
        if is_emoji_seg:
            em_img = _get_emoji_image(seg, emoji_size)
            if em_img is not None:
                # Полупрозрачная тень под эмодзи
                for sx, sy, sa in ((3, 3, 60), (1, 1, 40)):
                    dark_layer = Image.new("RGBA", em_img.size, (*BG_COLOR, sa))
                    shadow = Image.composite(
                        dark_layer,
                        Image.new("RGBA", em_img.size, (0, 0, 0, 0)),
                        em_img.split()[3],
                    )
                    img.paste(shadow, (current_x + sx, emoji_y + sy), shadow)
                img.paste(em_img, (current_x, emoji_y), em_img)
                em_img.close()
                current_x += emoji_size
            else:
                # Fallback: рисуем «□» вместо неизвестного эмодзи
                draw.text((current_x, y), "□", font=font, fill=fill)
                fb_bbox = _DUMMY_DRAW.textbbox((0, 0), "□", font=font)
                current_x += fb_bbox[2] - fb_bbox[0]
        else:
            if not seg:
                continue
            # Тень текста
            for sx, sy, sa in ((4, 4, 30), (2, 2, 50), (1, 1, 70)):
                draw.text(
                    (current_x + sx, y + sy), seg, font=font,
                    fill=(*BG_COLOR[:3], sa),
                )
            draw.text((current_x, y), seg, font=font, fill=fill)
            seg_bbox = _DUMMY_DRAW.textbbox((0, 0), seg, font=font)
            current_x += seg_bbox[2] - seg_bbox[0]


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
    Строит карточку-цитату и сохраняет во временный файл.
    Возвращает путь к файлу.
    """
    with PerfTimer("generate_quote_image_total"):

        # ── 1. Оборачиваем цитату в ёлочки ──────────────────────────
        display_quote = f"«{quote}»"
        text_max_w = IMG_WIDTH - 2 * PAD_X
        max_text_h = IMG_MAX_HEIGHT - PAD_TOP - PAD_BOTTOM - 140

        # ── 2. Оценка размера шрифта ─────────────────────────────────
        font_size = _estimate_font_size(display_quote, text_max_w, max_text_h)

        # ── 3. Верификация + корректировка ───────────────────────────
        font = _load_font(font_size)
        # emoji_size для переноса = высота строки * 0.9
        sample_bbox = _DUMMY_DRAW.textbbox((0, 0), "Ag", font=font)
        emoji_size = max(16, int((sample_bbox[3] - sample_bbox[1]) * 0.9))
        line_h = int((sample_bbox[3] - sample_bbox[1]) * LINE_SPACING_FACTOR)

        lines = _wrap_text(display_quote, font, text_max_w)

        while line_h * len(lines) > max_text_h and font_size > MIN_FONT_SIZE:
            font_size -= 4
            font = _load_font(font_size)
            sample_bbox = _DUMMY_DRAW.textbbox((0, 0), "Ag", font=font)
            emoji_size = max(16, int((sample_bbox[3] - sample_bbox[1]) * 0.9))
            line_h = int((sample_bbox[3] - sample_bbox[1]) * LINE_SPACING_FACTOR)
            lines = _wrap_text(display_quote, font, text_max_w)

        # ── 4. Шрифт и метрики автора ────────────────────────────────
        author_font_size = max(28, min(60, font_size - 24))
        author_font = _load_font(author_font_size)
        author_emoji_size = max(16, int(
            (_DUMMY_DRAW.textbbox((0, 0), "Ag", font=author_font)[3]
             - _DUMMY_DRAW.textbbox((0, 0), "Ag", font=author_font)[1]) * 0.9
        ))

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

        # ── 7. Рендеринг текста ──────────────────────────────────────
        draw = ImageDraw.Draw(img)

        with PerfTimer("text_rendering"):

            # ── 7a. Строки цитаты ────────────────────────────────────
            text_y = start_y
            for line in lines:
                # Для центровки используем измерение с учётом эмодзи
                line_w = _measure_line_width(line, font, emoji_size)
                x = (IMG_WIDTH - line_w) // 2
                _draw_line_with_shadow(
                    draw, img, x, text_y,
                    line, font, emoji_size,
                    TEXT_COLOR, line_h,
                )
                text_y += line_h

            # ── 7b. Акцентная линия ──────────────────────────────────
            line_y = text_y + gap_after_quote // 2
            accent_x1 = IMG_WIDTH - PAD_X - 200
            accent_x2 = IMG_WIDTH - PAD_X
            draw.line(
                [(accent_x1, line_y), (accent_x2, line_y)],
                fill=(180, 160, 140, 120), width=1,
            )

            # ── 7c. Автор (правое выравнивание) ──────────────────────
            author_text = f"— {author}"
            auth_y = text_y + gap_after_quote

            author_w = _measure_line_width(author_text, author_font, author_emoji_size)
            author_x = IMG_WIDTH - PAD_X - author_w

            _draw_line_with_shadow(
                draw, img, author_x, auth_y,
                author_text, author_font, author_emoji_size,
                AUTHOR_COLOR, line_h,
            )

            # ── 7d. Водяной знак ─────────────────────────────────────
            _draw_watermark(img, draw)

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


def _get_transcribable_media(message: Message):
    if message.voice:
        return message.voice, ".ogg", "voice"
    if message.video_note:
        return message.video_note, ".mp4", "video_note"
    return None, "", ""


async def _download_transcribable_media_to_temp(bot: Bot, message: Message) -> tuple[str, str]:
    media, suffix, media_type = _get_transcribable_media(message)
    if media is None:
        raise ValueError("message does not contain voice or video note")

    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        file = await bot.get_file(media.file_id)
        if file.file_path is None:
            raise RuntimeError(f"Telegram returned empty file_path for {media_type}")
        await bot.download_file(file.file_path, destination=tmp_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return tmp_path, media_type


async def _transcribe_voice(path: str) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set")

    client = AsyncGroq(api_key=GROQ_API_KEY)
    transcription = await client.audio.transcriptions.create(
        model=GROQ_WHISPER_MODEL,
        file=Path(path),
        response_format="json",
        temperature=0,
    )

    text = (getattr(transcription, "text", "") or "").strip()
    if not text:
        raise RuntimeError("Groq returned empty transcription")
    return text


async def _transcribe_message_media(
    bot: Bot,
    source_message: Message,
    *,
    chat_id: int,
    user_id: int | None,
    log_prefix: str,
) -> str:
    media, _, media_type = _get_transcribable_media(source_message)
    if media is None:
        raise ValueError("message does not contain voice or video note")

    media_path: str | None = None

    try:
        log.info(
            "%s downloading transcribable media type=%s file_id=%s duration=%s file_size=%s",
            log_prefix,
            media_type,
            media.file_id,
            getattr(media, "duration", None),
            getattr(media, "file_size", None),
        )
        media_path, media_type = await _download_transcribable_media_to_temp(bot, source_message)
        log.info("%s transcribable media downloaded to temp file: %s type=%s", log_prefix, media_path, media_type)

        text = await _transcribe_voice(media_path)
        log.info(
            "%s media transcription completed chat_id=%s user_id=%s media_type=%s text_len=%d",
            log_prefix,
            chat_id,
            user_id,
            media_type,
            len(text),
        )
        return text
    finally:
        if media_path:
            try:
                os.unlink(media_path)
                log.info("%s transcribable media temp file deleted: %s", log_prefix, media_path)
            except OSError as exc:
                log.warning("%s failed to delete transcribable media temp file %s: %s", log_prefix, media_path, exc)


def _build_silent_wav_bytes(duration_seconds: float = 1.0, sample_rate: int = 16000) -> bytes:
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * int(sample_rate * duration_seconds))
    return buffer.getvalue()


async def _check_voice_transcription_service() -> tuple[bool, str, dict[str, str]]:
    if not GROQ_API_KEY:
        log.warning("Voice transcription service check failed: GROQ_API_KEY is not set")
        return False, "GROQ_API_KEY is not set", {}

    try:
        client = AsyncGroq(api_key=GROQ_API_KEY)
        silent_wav = _build_silent_wav_bytes()
        response = await client.audio.transcriptions.with_raw_response.create(
            model=GROQ_WHISPER_MODEL,
            file=("silence.wav", silent_wav, "audio/wav"),
            response_format="json",
            temperature=0,
        )
        rate_limits = {
            "limit_requests": response.headers.get("x-ratelimit-limit-requests", ""),
            "remaining_requests": response.headers.get("x-ratelimit-remaining-requests", ""),
            "reset_requests": response.headers.get("x-ratelimit-reset-requests", ""),
        }
        await response.parse()
        log.info(
            "Voice transcription service is available, model=%s, rate_limits=%s",
            GROQ_WHISPER_MODEL,
            rate_limits,
        )
        return True, "", rate_limits
    except Exception as exc:
        log.error("Voice transcription service check failed: %s", exc, exc_info=True)
        return False, str(exc), {}


@router.message(Command("эхо"))
async def cmd_echo(message: Message) -> None:
    await message.answer(text=f"{message.text}")


@router.message(Command("гсчек"))
async def cmd_voice_check(message: Message) -> None:
    log.info(
        "Voice transcription API check requested chat_id=%s user_id=%s",
        message.chat.id,
        message.from_user.id if message.from_user else None,
    )

    is_available, error, rate_limits = await _check_voice_transcription_service()
    auto_status = "включён" if VOICE_AUTO_TRANSCRIBE_ENABLED else "выключен"
    limits_text = ""
    if rate_limits:
        requests_remaining = rate_limits.get("remaining_requests") or "неизвестно"
        requests_limit = rate_limits.get("limit_requests") or "неизвестно"
        requests_reset = rate_limits.get("reset_requests") or "неизвестно"
        limits_text = (
            "\n\n📊 Лимиты Groq:"
            f"\nЗапросы: {requests_remaining} / {requests_limit}"
            f"\nСброс запросов через: {requests_reset}"
        )

    if is_available:
        await message.reply(
            "✅ Сервис для перевода голосовых доступен.\n"
            f"Автотранскрибинг: {auto_status}."
            f"{limits_text}"
        )
    else:
        error_text = f"\nОшибка: {error}" if error else ""
        await message.reply(
            "❌ Сервис для перевода голосовых недоступен.\n"
            f"Автотранскрибинг: {auto_status}."
            f"{error_text}"
        )


@router.message(Command("гсавто"))
async def cmd_voice_auto(message: Message) -> None:
    global VOICE_AUTO_TRANSCRIBE_ENABLED

    args = (message.text or "").split(maxsplit=1)
    user_id = message.from_user.id if message.from_user else None

    log.info(
        "Voice auto transcription toggle requested chat_id=%s user_id=%s text=%r",
        message.chat.id,
        user_id,
        message.text,
    )

    if len(args) != 2 or args[1].strip() not in {"0", "1"}:
        auto_status = "включён" if VOICE_AUTO_TRANSCRIBE_ENABLED else "выключен"
        await message.reply(
            "Использование: /гсавто <0 или 1>\n"
            f"Сейчас автотранскрибинг: {auto_status}."
        )
        return

    VOICE_AUTO_TRANSCRIBE_ENABLED = args[1].strip() == "1"
    auto_status = "включён" if VOICE_AUTO_TRANSCRIBE_ENABLED else "выключен"
    log.info("Voice auto transcription set to %s by user_id=%s", VOICE_AUTO_TRANSCRIBE_ENABLED, user_id)
    await message.reply(f"Автотранскрибинг {auto_status}.")


@router.message(
    Command("гс"),
    F.chat.type.in_({"supergroup", "group"}),
    F.message_thread_id == None,
)
async def cmd_voice_transcribe(message: Message) -> None:
    reply = message.reply_to_message
    user_id = message.from_user.id if message.from_user else None

    log.info(
        "Voice transcription requested chat_id=%s user_id=%s reply_message_id=%s",
        message.chat.id,
        user_id,
        reply.message_id if reply else None,
    )

    media, _, _ = _get_transcribable_media(reply) if reply else (None, "", "")
    if not reply or media is None:
        log.info("Voice transcription rejected: command is not a reply to voice or video note message")
        await message.reply("↩️ Ответьте командой /гс на голосовое сообщение или кружочек.")
        return

    status_msg = await message.answer("⏳ Расшифровываю сообщение...")

    try:
        text = await _transcribe_message_media(
            message.bot,
            reply,
            chat_id=message.chat.id,
            user_id=user_id,
            log_prefix="Manual",
        )
        await status_msg.edit_text(
            text,
            reply_markup=_create_go_to_reply_message_keyboard(message.chat.id, reply.message_id),
        )
    except Exception as exc:
        log.error("Media transcription failed: %s", exc, exc_info=True)
        await status_msg.edit_text("❌ Не удалось расшифровать сообщение.")


@router.message(
    F.chat.type.in_({"supergroup", "group"}),
    F.message_thread_id == None,
    F.voice | F.video_note,
)
async def auto_voice_transcribe(message: Message) -> None:
    if not VOICE_AUTO_TRANSCRIBE_ENABLED:
        return

    media, _, media_type = _get_transcribable_media(message)
    if media is None:
        return

    user_id = message.from_user.id if message.from_user else None

    log.info(
        "Auto media transcription requested chat_id=%s user_id=%s message_id=%s media_type=%s",
        message.chat.id,
        user_id,
        message.message_id,
        media_type,
    )

    try:
        text = await _transcribe_message_media(
            message.bot,
            message,
            chat_id=message.chat.id,
            user_id=user_id,
            log_prefix="Auto",
        )
        await message.reply(
            text,
            reply_markup=_create_go_to_reply_message_keyboard(message.chat.id, message.message_id),
        )
    except Exception as exc:
        log.error("Auto media transcription failed: %s", exc, exc_info=True)
        await message.reply("❌ Не удалось расшифровать сообщение.")


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

    if len(quote_text) > MAX_QUOTE_LENGTH:
        await message.reply(
            f"📏 Цитата слишком длинная ({len(quote_text)} симв.). "
            f"Максимум — {MAX_QUOTE_LENGTH}."
        )
        return
    
    # Если не удаётся найти ID пользователя, то будет чёрный фон
    status_warning = ''
    author_user_id = _get_author_user_id(reply)
    author = _get_author(reply)
    if author_user_id is None:
        log.warning("Cannot get quote author's user_id (username=%r, author_user_id=%r), bg will be black", author, str(author_user_id))
        status_warning = '(чёрный фон)'

    # Отправляем статусное сообщение
    status_msg = await message.answer(f"⏳ Создаю цитату{status_warning}...")

    avatar_path: str | None = None
    if author_user_id is not None:
        avatar_path = await _download_avatar(message.bot, author_user_id)

    # Проверяем, нужно ли создавать кэш фона (долгая операция)
    if avatar_path and not _bg_cache_exists_for_avatar(author_user_id, avatar_path):
        await status_msg.edit_text("🖼 Создаю фон из аватарки (около 30-50 секунд)...")

    log.info(
        "Generating quote for chat_id=%d, author=%r, author_id=%r, len=%d, avatar=%s",
        message.chat.id, author, str(author_user_id) or 'None', len(quote_text), "yes" if avatar_path else "no",
    )

    async with IMAGE_GEN_SEMAPHORE:
        log.info(
            "Semaphore acquired (active=%d/%d), RSS=%.1fMB",
            IMAGE_GEN_SEMAPHORE._value, MAX_SEMAPHORE_COUNT, _get_rss_mb(),
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
            try:
                await status_msg.delete()
            except Exception:
                pass
            return
        finally:
            if avatar_path:
                try:
                    os.unlink(avatar_path)
                except OSError:
                    pass

    thread_id = SUPERCHAT_TO_THREAD_MAP.get(message.chat.id, QUOTE_THREAD_ID)

    # Создаём клавиатуру с ссылкой на оригинальное сообщение
    keyboard = _create_go_to_reply_message_keyboard(message.chat.id, reply.message_id)

    try:
        photo = FSInputFile(img_path)
        await message.bot.send_photo(
            chat_id=message.chat.id,
            photo=photo,
            message_thread_id=thread_id,
            reply_markup=keyboard,
        )
    except Exception as exc:
        log.error("Failed to send photo: %s", exc, exc_info=True)
        await message.reply("❌ Не удалось отправить картинку.")
    finally:
        try:
            os.unlink(img_path)
        except OSError:
            pass
        try:
            await status_msg.delete()
        except Exception:
            pass


@router.inline_query()
async def inline_pinterest_search(inline_query: InlineQuery) -> None:
    query = (inline_query.query or "").strip()

    if not query:
        await inline_query.answer(
            results=[
                InlineQueryResultArticle(
                    id="inline_help",
                    title="Введите запрос для поиска в Pinterest",
                    description="Например: cats, nature wallpaper, room design",
                    input_message_content=InputTextMessageContent(
                        message_text=(
                            "Введите запрос после имени бота в inline-режиме.\n\n"
                            "Пример:\n"
                            "@chpi_quote_bot cats"
                        )
                    ),
                )
            ],
            cache_time=1,
            is_personal=True,
        )
        return

    try:
        page = int(inline_query.offset) if inline_query.offset else 0
    except ValueError:
        page = 0

    page = max(0, min(page, INLINE_MAX_PAGES - 1))

    log.info(
        "Inline Pinterest search user_id=%d query=%r page=%d",
        inline_query.from_user.id,
        query,
        page,
    )

    async with PINTEREST_SEARCH_SEMAPHORE:
        try:
            pins, has_more = await PINTEREST_INLINE_SERVICE.get_page(
                user_id=inline_query.from_user.id,
                query=query,
                page=page,
            )
        except Exception as exc:
            log.error("Inline Pinterest search failed: %s", exc, exc_info=True)
            await inline_query.answer(
                results=[
                    InlineQueryResultArticle(
                        id="inline_error",
                        title="Не удалось выполнить поиск",
                        description="Попробуйте повторить запрос чуть позже",
                        input_message_content=InputTextMessageContent(
                            message_text="Ошибка поиска Pinterest. Попробуйте позже."
                        ),
                    )
                ],
                cache_time=1,
                is_personal=True,
            )
            return

    results: list[InlineQueryResultPhoto] = []
    for idx, pin in enumerate(pins[:INLINE_PAGE_SIZE]):
        result_id = hashlib.md5(
            f"{pin['pin_id']}:{page}:{idx}:{query}".encode("utf-8")
        ).hexdigest()

        kwargs = {}
        if isinstance(pin.get("width"), int):
            kwargs["photo_width"] = pin["width"]
        if isinstance(pin.get("height"), int):
            kwargs["photo_height"] = pin["height"]

        results.append(
            InlineQueryResultPhoto(
                id=result_id,
                photo_url=pin["photo_url"],
                thumbnail_url=pin["thumbnail_url"],
                title=pin.get("title"),
                description=pin.get("description"),
                reply_markup=_create_pinterest_result_keyboard(pin["pin_link"]),
                **kwargs,
            )
        )

    if not results:
        await inline_query.answer(
            results=[
                InlineQueryResultArticle(
                    id=f"no_results_{hashlib.md5(query.encode()).hexdigest()}",
                    title="Ничего не найдено",
                    description="Попробуйте изменить запрос",
                    input_message_content=InputTextMessageContent(
                        message_text=f"По запросу «{query}» ничего не найдено."
                    ),
                )
            ],
            cache_time=5,
            is_personal=True,
        )
        return

    next_offset = str(page + 1) if has_more and (page + 1) < INLINE_MAX_PAGES else ""

    await inline_query.answer(
        results=results,
        cache_time=30,
        is_personal=True,
        next_offset=next_offset,
    )


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
    log.info("supports_inline_queries=%s", getattr(me, "supports_inline_queries", None))


async def on_startup_webhook(bot: Bot) -> None:
    """Startup hook для webhook режима - устанавливает webhook URL"""
    me = await bot.get_me()
    log.info("Bot starting (webhook): @%s (id=%d)", me.username, me.id)
    log.info("Initial RSS: %.1f MB", _get_rss_mb())
    log.info("supports_inline_queries=%s", getattr(me, "supports_inline_queries", None))
    
    if WEBHOOK_URL:
        await bot.set_webhook(
            url=WEBHOOK_URL,
            secret_token=WEBHOOK_SECRET or None,
            allowed_updates=["message", "inline_query"]
        )
        log.info("Webhook set to: %s", WEBHOOK_URL)
    else:
        log.warning("WEBHOOK_URL not set, skipping webhook registration")


async def on_shutdown(bot: Bot) -> None:
    log.info("Bot shutting down, RSS: %.1f MB", _get_rss_mb())
    IMAGE_EXECUTOR.shutdown(wait=False)
    PINTEREST_SEARCH_EXECUTOR.shutdown(wait=False)

    try:
        _supabase_client.auth.sign_out()
        log.info("Supabase client closed")
    except Exception as e:
        log.warning("Supabase client close failed: %s", e)

    await bot.session.close()


async def on_shutdown_webhook(bot: Bot) -> None:
    """Shutdown hook для webhook режима - удаляет webhook"""
    log.info("Bot shutting down (webhook), RSS: %.1f MB", _get_rss_mb())
    IMAGE_EXECUTOR.shutdown(wait=False)
    PINTEREST_SEARCH_EXECUTOR.shutdown(wait=False)

    try:
        _supabase_client.auth.sign_out()
        log.info("Supabase client closed")
    except Exception as e:
        log.warning("Supabase client close failed: %s", e)
    
    # Удаляем webhook при остановке
    try:
        await bot.delete_webhook()
        log.info("Webhook deleted")
    except Exception as e:
        log.warning("Failed to delete webhook: %s", e)
    
    await bot.session.close()


# ─────────────────────────── health endpoint для cron-job.org ─

async def health_handler(request: web.Request) -> web.Response:
    """Health check endpoint для cron-job.org и мониторинга"""
    return web.json_response({
        "status": "ok",
        "service": "quote_bot",
        "timestamp": time.time(),
    })


# ─────────────────────────── entry point ──────────────────────

def init_bot() -> tuple[Bot, Dispatcher]:
    """Инициализация бота и диспетчера"""
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    return bot, dp


def warmup_cache() -> None:
    """Прогрев кэшей перед стартом"""
    ensure_font()
    ensure_watermark_font()
    _ensure_bg_cache_dir()
    _ensure_emoji_cache()

    for size in range(28, 96, 4):
        _load_font(size)
    for size in (28, 32, 36, 40, 44, 48, 52, 56, 60):
        _load_font(size)

    log.info("Font cache warmed up (%d entries)", _load_font.cache_info().currsize)

    # Проверяем подключение к Supabase
    try:
        _supabase_client.storage.list_buckets()
        log.info("Supabase Storage: connection OK, bucket='%s'", SUPABASE_BUCKET)
    except Exception as exc:
        log.error("Supabase Storage: connection FAILED: %s", exc)


async def main_polling() -> None:
    """Запуск бота в режиме polling (для локальной разработки)"""
    warmup_cache()
    
    bot, dp = init_bot()
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    log.info("Starting polling …")
    await dp.start_polling(bot, allowed_updates=["message", "inline_query"])


def main_webhook() -> None:
    """Запуск бота в режиме webhook через aiohttp (для Render)"""
    warmup_cache()
    
    bot, dp = init_bot()
    dp.startup.register(on_startup_webhook)
    dp.shutdown.register(on_shutdown_webhook)

    # Создаём aiohttp приложение
    app = web.Application()
    
    # Health endpoint для cron-job.org (будит сервер)
    app.router.add_get("/health", health_handler)
    
    # Webhook handler от aiogram
    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET or None
    )
    webhook_handler.register(app, path=WEBHOOK_PATH)
    
    # Настраиваем startup/shutdown hooks
    setup_application(app, dp, bot=bot)

    # Запускаем сервер
    port = WEBHOOK_PORT
    log.info("Starting webhook server on %s:%d%s", WEBHOOK_HOST, port, WEBHOOK_PATH)
    web.run_app(app, host=WEBHOOK_HOST, port=port)


if __name__ == "__main__":
    if BOT_MODE == "polling":
        asyncio.run(main_polling())
    else:
        main_webhook()