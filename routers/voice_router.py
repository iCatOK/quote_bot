"""
Роутер для расшифровки голосовых сообщений через faster-whisper.

Сценарий использования:
  1. Пользователь делает reply (ответ) на голосовое сообщение в чате.
  2. В тексте reply пишет команду /гс (русскими символами).
  3. Бот расшифровывает голосовое и отвечает текстом.

Аудио-файлы, кружочки и сообщения без reply на voice — игнорируются.

Зависимости:
  pip install faster-whisper aiogram aiofiles
"""

import os
import asyncio
import logging
import tempfile
from functools import lru_cache

import aiofiles
from aiogram import Router, F, Bot
from aiogram.types import Message
from faster_whisper import WhisperModel

from utils.perfromance import PerfTimer

# ──────────────────────────────────────────────
# НАСТРОЙКИ (можно вынести в config.py / .env)
# ──────────────────────────────────────────────

# Размер модели Whisper.
# Варианты: "tiny", "base", "small", "medium", "large-v3", "turbo"
# Рекомендация для русского языка без GPU: "medium" (баланс скорость/точность)
# Если сервер мощный с GPU — используйте "large-v3" или "turbo"
WHISPER_MODEL_SIZE: str = os.getenv("WHISPER_MODEL_SIZE", "tiny")

# Устройство для инференса: "cpu" или "cuda" (если есть GPU + CUDA)
WHISPER_DEVICE: str = os.getenv("WHISPER_DEVICE", "cpu")

# Тип вычислений. На CPU: "int8" (быстро, мало памяти). На GPU: "float16"
WHISPER_COMPUTE_TYPE: str = os.getenv("WHISPER_COMPUTE_TYPE", "int8")

# Количество потоков CPU для транскрипции (0 = авто)
WHISPER_CPU_THREADS: int = int(os.getenv("WHISPER_CPU_THREADS", "4"))

# Язык транскрипции. None = автоопределение. "ru" = только русский (быстрее)
WHISPER_LANGUAGE: str | None = os.getenv("WHISPER_LANGUAGE", "ru") or None

# Папка для временных аудиофайлов
TEMP_DIR: str = os.getenv("TEMP_DIR", tempfile.gettempdir())

# Максимальный размер файла для обработки (в байтах). По умолчанию 25 МБ.
MAX_FILE_SIZE: int = int(os.getenv("MAX_VOICE_FILE_SIZE", str(25 * 1024 * 1024)))

# ──────────────────────────────────────────────
# ИНИЦИАЛИЗАЦИЯ
# ──────────────────────────────────────────────

logger = logging.getLogger(__name__)

# Роутер для регистрации хэндлеров
router = Router(name="voice_transcription")


@lru_cache(maxsize=1)
def get_whisper_model() -> WhisperModel:
    """
    Возвращает единственный экземпляр модели Whisper (Singleton).
    
    Модель загружается один раз при первом вызове и кешируется в памяти.
    Повторные вызовы возвращают уже загруженную модель без задержки.
    
    Returns:
        WhisperModel: Готовая к использованию модель faster-whisper.
    """
    logger.info(
        f"Loading Whisper model: size={WHISPER_MODEL_SIZE}, "
        f"device={WHISPER_DEVICE}, compute_type={WHISPER_COMPUTE_TYPE}"
    )
    model = WhisperModel(
        model_size_or_path=WHISPER_MODEL_SIZE,
        device=WHISPER_DEVICE,
        compute_type=WHISPER_COMPUTE_TYPE,
        cpu_threads=WHISPER_CPU_THREADS,
        # Кеш моделей — сохраняется между перезапусками контейнера при маунте /root/.cache
        download_root=None,
    )
    logger.info("Whisper model loaded successfully")
    return model


# ──────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ──────────────────────────────────────────────

async def download_voice_file(bot: Bot, file_id: str, suffix: str = ".ogg") -> str:
    """
    Скачивает аудиофайл из Telegram на сервер во временную директорию.

    Args:
        bot: Экземпляр aiogram Bot.
        file_id: Уникальный идентификатор файла в Telegram.
        suffix: Расширение временного файла (.ogg для voice, .mp3 для audio).

    Returns:
        str: Путь к скачанному файлу на сервере.

    Raises:
        ValueError: Если файл превышает MAX_FILE_SIZE.
    """
    # Получаем метаданные файла (включая размер)
    tg_file = await bot.get_file(file_id)

    if tg_file.file_size and tg_file.file_size > MAX_FILE_SIZE:
        raise ValueError(
            f"Файл слишком большой: {tg_file.file_size // 1024 // 1024} МБ "
            f"(максимум {MAX_FILE_SIZE // 1024 // 1024} МБ)"
        )

    # Создаём временный файл
    tmp = tempfile.NamedTemporaryFile(
        suffix=suffix,
        dir=TEMP_DIR,
        delete=False,
    )
    tmp_path = tmp.name
    tmp.close()

    # Скачиваем файл через aiogram
    await bot.download_file(tg_file.file_path, destination=tmp_path)
    logger.debug(f"File downloaded: {tmp_path} ({tg_file.file_size} bytes)")
    return tmp_path


def transcribe_audio(file_path: str) -> str:
    """
    Запускает транскрипцию аудиофайла через faster-whisper.

    Функция синхронная — вызывается через asyncio.to_thread() чтобы
    не блокировать event loop во время тяжёлых вычислений.

    Args:
        file_path: Путь к аудиофайлу на сервере.

    Returns:
        str: Расшифрованный текст. Пустая строка если речь не распознана.
    """
    model = get_whisper_model()

    # Запускаем транскрипцию
    # vad_filter=True — фильтрует тишину, ускоряет обработку
    segments, info = model.transcribe(
        audio=file_path,
        language=WHISPER_LANGUAGE,
        beam_size=5,             # Качество vs скорость (5 — хороший баланс)
        vad_filter=True,         # Фильтр голосовой активности (убирает паузы)
        vad_parameters={
            "min_silence_duration_ms": 500,  # Минимальная пауза для разбивки
        },
    )

    logger.debug(
        f"Detected language: {info.language} "
        f"(probability: {info.language_probability:.2f})"
    )

    # Собираем все сегменты в единый текст
    text_parts = [segment.text.strip() for segment in segments]
    full_text = " ".join(text_parts).strip()

    return full_text


async def process_voice_message(message: Message, bot: Bot, file_id: str, suffix: str) -> None:
    """
    Логика обработки пересланного голосового сообщения:
    1. Отправляет индикатор набора текста
    2. Скачивает файл
    3. Транскрибирует в отдельном потоке (не блокирует бота)
    4. Отвечает пользователю
    5. Удаляет временный файл

    Args:
        message: Входящее сообщение от пользователя.
        bot: Экземпляр aiogram Bot.
        file_id: ID голосового файла в Telegram.
        suffix: Расширение файла для сохранения (всегда .ogg для voice).
    """
    tmp_path: str | None = None

    try:
        # Показываем "печатает..." пока обрабатываем
        await bot.send_chat_action(message.chat.id, action="typing")

        # Скачиваем аудио
        tmp_path = await download_voice_file(bot, file_id, suffix)

        # Транскрибируем в отдельном потоке чтобы не блокировать event loop
        text = await asyncio.to_thread(transcribe_audio, tmp_path)

        if not text:
            await message.reply("🔇 Не удалось распознать речь в этом сообщении.")
            return

        # Telegram ограничивает сообщения до 4096 символов
        if len(text) > 4000:
            # Отправляем длинный текст как файл
            async with aiofiles.tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".txt",
                delete=False,
                encoding="utf-8",
            ) as txt_file:
                await txt_file.write(text)
                txt_path = txt_file.name

            await message.reply_document(
                document=txt_path,
                caption="📝 Текст слишком длинный, отправляю файлом.",
            )
            os.unlink(txt_path)
        else:
            await message.reply(f"📝 <b>Расшифровка:</b>\n\n{text}", parse_mode="HTML")

    except ValueError as e:
        # Ошибки валидации (например, файл слишком большой)
        await message.reply(f"⚠️ {e}")

    except Exception as e:
        logger.exception(f"Transcription error: {e}")
        await message.reply("❌ Произошла ошибка при расшифровке. Попробуйте позже.")

    finally:
        # Всегда удаляем временный файл
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
            logger.debug(f"Temp file deleted: {tmp_path}")


# ──────────────────────────────────────────────
# ХЭНДЛЕРЫ РОУТЕРА
# ──────────────────────────────────────────────

# Фильтр: текст сообщения равен команде /гс (русскими символами).
# Используем F.text вместо Command(), потому что aiogram Command() не поддерживает
# кириллические команды — Telegram технически принимает их, но BotFather их не регистрирует.
IS_GS_COMMAND = F.text == "/гс"

# Фильтр: reply указывает на голосовое сообщение ИЛИ кружочек (video_note).
# message.reply_to_message.voice      — голосовое (OGG/OPUS)
# message.reply_to_message.video_note — кружочек (видеосообщение)
REPLY_HAS_VOICE = F.reply_to_message.voice | F.reply_to_message.video_note


@router.message(IS_GS_COMMAND & REPLY_HAS_VOICE)
async def handle_gs_command(message: Message, bot: Bot) -> None:
    """
    Хэндлер срабатывает когда пользователь:
      1. Делает reply (ответ) на голосовое сообщение или кружочек.
      2. Пишет команду /гс в тексте этого reply.

    Голосовое берётся из message.reply_to_message.voice,
    кружочек — из message.reply_to_message.video_note.

    Не имеет значения: пересланное сообщение или нет, чужое или своё.
    """
    replied = message.reply_to_message

    # Определяем тип медиа и берём соответствующий file_id и суффикс
    if replied.voice:
        file_id = replied.voice.file_id
        duration = replied.voice.duration
        suffix = ".ogg"
        media_type = "voice"
    else:
        file_id = replied.video_note.file_id
        duration = replied.video_note.duration
        suffix = ".mp4"
        media_type = "video_note"

    logger.info(
        f"Command /гс from user_id={message.from_user.id}, "
        f"media_type={media_type}, duration={duration}s, file_id={file_id}"
    )
    with PerfTimer("whisper_transcription"):
        await process_voice_message(message, bot, file_id, suffix)