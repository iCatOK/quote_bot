"""Daily-style chat summarisation feature backed by AIHubMix LLM."""
from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command
from aiogram.types import Message, TelegramObject

try:
    from openai import AsyncOpenAI
except ImportError:  # pragma: no cover - optional dep at import time
    AsyncOpenAI = None  # type: ignore[assignment]


log = logging.getLogger("quote_bot.summary")

# AIHubMix — OpenAI-совместимый агрегатор. Используем стандартный SDK openai,
# указывая ему base_url AIHubMix.
AI_HUB_MIX_API_KEY = os.environ.get("AI_HUB_MIX_API_KEY")
AI_HUB_MIX_BASE_URL = os.environ.get(
    "AI_HUB_MIX_BASE_URL", "https://aihubmix.com/v1"
)
AI_HUB_MIX_MODEL = os.environ.get("AI_HUB_MIX_MODEL", "gpt-5.5-free")

# Если новое сообщение приходит позже, чем `SUMMARY_AUTO_TRIGGER_DELTA`
# после последнего вызова /summary — автоматически суммаризируем буфер,
# чтобы он не рос бесконтрольно.
SUMMARY_AUTO_TRIGGER_DELTA = timedelta(days=1)

# Жёсткий потолок на размер буфера в одном чате (страховка от OOM).
HISTORY_MAX_MESSAGES = 5000

# Максимум сообщений, попадающих в один LLM-запрос (контроль контекста и цены).
SUMMARY_MAX_MESSAGES_PER_REQUEST = 1500

SYSTEM_PROMPT = (
    "Ты — свой человек в чате. Не диктор новостей, не бот, не «ассистент». "
    "Скорее — наблюдательный приятель, который пролистал переписку и в двух "
    "словах рассказывает, что там было: с подколами, узнаваемыми деталями и "
    "лёгкой иронией. Ты пишешь по-русски, для своих.\n\n"
    "Формат входных данных (то, что тебе пришлёт пользователь):\n"
    "- Каждое сообщение в отдельной строке вида:\n"
    "  `[HH:MM] #<msg_id> [↪#<reply_to>] [🎤] Имя: текст`\n"
    "- `#123` — это id сообщения в чате.\n"
    "- `↪#100` означает, что это ответ на сообщение с id 100. Используй эту связь, "
    "чтобы понимать нить разговора (кто кому отвечает, как развивается обсуждение).\n"
    "- Маркер `🎤` означает, что текст — это автоматическая расшифровка голосового "
    "или видеосообщения. Возможны неточности распознавания (опечатки, странные "
    "слова, неверные имена). Не цепляйся к буквальным формулировкам таких "
    "сообщений: ориентируйся на общий смысл и помни, что человек говорил голосом, "
    "а не печатал.\n\n"
    "Как писать:\n"
    "- Живой разговорный язык. Короткие фразы. Можно неполные предложения — "
    "как в обычной речи. Можно начать с «ну», «короче», «в общем», если по делу.\n"
    "- Никакого канцелярита и пресс-релизного тона: забудь про «осуществили "
    "обсуждение», «было принято решение», «участники затронули тему». Просто "
    "скажи, что произошло.\n"
    "- Цепляйся за конкретику: реальные имена, мемные фразы, забавные детали, "
    "кто кого подколол. Без конкретики саммари мёртвое.\n"
    "- Лёгкая ирония и подколы — да. Сарказм, токсичность, морализаторство — нет. "
    "Не суди людей и не вставай ни на чью сторону.\n"
    "- Эмодзи — по вкусу, чтобы расставить акценты (😅 🤣 😎 🔥 ✅ ❓ 🛠️ 📌). "
    "Не лепи их в каждое предложение и не превращай текст в гирлянду.\n"
    "- Разрешены лёгкие шероховатости и субъективные ремарки в скобках — "
    "это делает текст человечным. Но не переигрывай и не паясничай.\n"
    "- Пиши только по тому, что реально было в переписке. Ничего не выдумывай, "
    "не додумывай за людей. Если тема не раскрыта — так и скажи.\n\n"
    "Чего избегать:\n"
    "- Шаблонов вроде «в этот замечательный день», «команда обсудила», "
    "«подводя итог, можно сказать».\n"
    "- Длинных вводных и метакомментариев («сейчас я расскажу…»).\n"
    "- Перечислений в стиле протокола собрания.\n"
    "- Не вставляй в ответ id сообщений (`#123`) и стрелки (`↪`) — это служебная "
    "разметка только для тебя.\n\n"
    "Структура ответа (соблюдай, но без занудства):\n"
    "1. Заголовок: *📅 Ежедневное саммари*.\n"
    "2. 3–5 тематических блоков. У каждого — короткий ироничный заголовок "
    "жирным (например, *📌 Перепалка из-за пельменей* или *🛠️ Очередной "
    "героический деплой в пятницу*).\n"
    "3. В блоке — 1–3 живых предложения: о чём спорили/шутили/договорились, "
    "с именами и деталями. Решения помечай ✅, нерешённые вопросы — ❓.\n"
    "4. В конце — одна-две фразы про общее настроение чата. Без морали и "
    "выводов «что мы из этого вынесли».\n\n"
    "ФОРМАТИРОВАНИЕ — Telegram MarkdownV2 (обязательно соблюдай):\n"
    "- Жирный: `*текст*` (одна звёздочка с каждой стороны). Используй для "
    "заголовка и заголовков блоков.\n"
    "- Курсив: `_текст_`.\n"
    "- Моноширинный (для ников/команд/коротких цитат при желании): `` `текст` ``.\n"
    "- В обычном тексте ОБЯЗАТЕЛЬНО экранируй обратной косой чертой следующие "
    "символы: `_ * [ ] ( ) ~ \\` `>` `#` `+` `-` `=` `|` `{` `}` `.` `!`.\n"
    "  Пример правильного экранирования: `Алиса сказала: всё ок\\. И ушла спать\\!`\n"
    "  Пример со списком: `1\\. первое  2\\. второе`\n"
    "- Внутри `*...*` и `_..._` правила экранирования те же.\n"
    "- Никаких `**жирный**`, `##заголовков`, HTML-тегов — это не MarkdownV2.\n"
    "- Эмодзи экранировать не нужно."
)


@dataclass
class StoredMessage:
    user_id: Optional[int]
    full_name: str
    text: str
    date: datetime
    message_id: Optional[int] = None
    reply_to_message_id: Optional[int] = None
    # True для расшифровок голосовых/видео — даёт LLM понять, что текст
    # получен автоматически и может содержать неточности.
    is_media: bool = False


@dataclass
class ChatHistory:
    messages: list[StoredMessage] = field(default_factory=list)
    last_summary_at: Optional[datetime] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_histories: dict[int, ChatHistory] = defaultdict(ChatHistory)


def _get_history(chat_id: int) -> ChatHistory:
    return _histories[chat_id]


def _author_name(message: Message) -> str:
    user = message.from_user
    if not user:
        return "Аноним"
    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(p for p in parts if p).strip()
    if name:
        return name
    if user.username:
        return f"@{user.username}"
    return "Аноним"


def _extract_text(message: Message) -> Optional[str]:
    text = message.text or message.caption
    if not text:
        return None
    text = text.strip()
    if not text:
        return None
    # Не сохраняем команды боту — они не несут контента для саммари.
    if text.startswith("/"):
        return None
    return text


def _estimate_tokens(text: str) -> int:
    """Грубая оценка числа токенов.

    Точный токенизатор Qwen-3 в пакете не поставляется, поэтому используем
    эвристику: для смеси русского и латиницы в BPE-токенизаторах в среднем
    выходит ~3 символа на токен. Этого достаточно для индикатора в /info.
    """
    return max(1, len(text) // 3) if text else 0


def _format_relative_delta(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    if total_seconds < 60:
        return "только что"
    minutes = total_seconds // 60
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч назад"
    days = hours // 24
    return f"{days} дн назад"


def format_chat_summary_info(chat_id: int) -> str:
    """Краткая сводка о состоянии буфера саммаризации в чате.

    Используется командой `/info` для диагностики.
    """
    history = _histories.get(chat_id)
    if history is None or not history.messages:
        messages_count = 0
        tokens_estimate = 0
    else:
        messages_count = len(history.messages)
        # Считаем тот же текст, который реально полетит в LLM (system + user).
        slice_for_llm = history.messages
        if messages_count > SUMMARY_MAX_MESSAGES_PER_REQUEST:
            slice_for_llm = slice_for_llm[-SUMMARY_MAX_MESSAGES_PER_REQUEST:]
        user_prompt = _format_messages(slice_for_llm)
        tokens_estimate = _estimate_tokens(SYSTEM_PROMPT) + _estimate_tokens(user_prompt)

    last_at = history.last_summary_at if history else None
    if last_at is None:
        last_line = "никогда"
    else:
        now = datetime.now(timezone.utc)
        delta = now - last_at.astimezone(timezone.utc)
        absolute = last_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        last_line = f"{absolute} ({_format_relative_delta(delta)})"

    return (
        "🧾 Саммаризация:"
        f"\nСообщений в буфере: {messages_count}"
        f"\nПримерно токенов в промпте: ~{tokens_estimate}"
        f"\nПоследний вызов /summary: {last_line}"
    )


def _format_messages(messages: list[StoredMessage]) -> str:
    lines: list[str] = []
    for m in messages:
        ts = m.date.astimezone(timezone.utc).strftime("%H:%M")
        parts: list[str] = [f"[{ts}]"]
        if m.message_id is not None:
            parts.append(f"#{m.message_id}")
        if m.reply_to_message_id is not None:
            parts.append(f"↪#{m.reply_to_message_id}")
        if m.is_media:
            parts.append("🎤")
        prefix = " ".join(parts)
        lines.append(f"{prefix} {m.full_name}: {m.text}")
    return "\n".join(lines)


async def _generate_summary(messages: list[StoredMessage]) -> str:
    if AsyncOpenAI is None:
        raise RuntimeError("openai package is not installed")
    if not AI_HUB_MIX_API_KEY:
        raise RuntimeError("AI_HUB_MIX_API_KEY is not set")

    # При очень длинной истории берём последние N — приоритет свежему контексту.
    if len(messages) > SUMMARY_MAX_MESSAGES_PER_REQUEST:
        messages = messages[-SUMMARY_MAX_MESSAGES_PER_REQUEST:]

    user_prompt = (
        "Переписка чата. Формат строки:\n"
        "  `[HH:MM] #<msg_id> [↪#<reply_to>] [🎤] Имя: текст`\n"
        "(`↪` — ответ на сообщение, `🎤` — расшифровка голосового/видео).\n\n"
        + _format_messages(messages)
    )

    client = AsyncOpenAI(
        api_key=AI_HUB_MIX_API_KEY,
        base_url=AI_HUB_MIX_BASE_URL,
    )
    try:
        completion = await client.chat.completions.create(
            model=AI_HUB_MIX_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.7,
        )
    finally:
        await client.close()

    choice = completion.choices[0]
    text = (getattr(choice.message, "content", "") or "").strip()
    if not text:
        raise RuntimeError("AIHubMix returned empty completion")
    return text


async def save_message_to_history(message: Message) -> None:
    """Сохранить сообщение в буфер истории чата.

    Если с момента последнего вызова `/summary` прошло больше суток —
    выгружает накопленный буфер в фоновую задачу-суммаризатор и очищает его.
    """
    if message.chat.type not in {"group", "supergroup"}:
        return
    # Игнорируем сообщения от ботов — они шумят в summary
    # (статусы, ответы команд, авто-транскрибация и т.п.).
    if message.from_user is None or message.from_user.is_bot:
        return
    text = _extract_text(message)
    if text is None:
        return

    chat_id = message.chat.id
    history = _get_history(chat_id)
    msg_date = message.date or datetime.now(timezone.utc)

    auto_flush: list[StoredMessage] | None = None
    async with history.lock:
        if (
            history.last_summary_at is not None
            and history.messages
            and msg_date - history.last_summary_at > SUMMARY_AUTO_TRIGGER_DELTA
        ):
            auto_flush = history.messages
            history.messages = []
            history.last_summary_at = msg_date

        reply_to = message.reply_to_message
        history.messages.append(
            StoredMessage(
                user_id=message.from_user.id if message.from_user else None,
                full_name=_author_name(message),
                text=text,
                date=msg_date,
                message_id=message.message_id,
                reply_to_message_id=reply_to.message_id if reply_to else None,
                is_media=False,
            )
        )
        if len(history.messages) > HISTORY_MAX_MESSAGES:
            history.messages = history.messages[-HISTORY_MAX_MESSAGES:]

    if auto_flush:
        asyncio.create_task(
            _send_auto_summary(message.bot, chat_id, auto_flush)
        )


async def save_transcribed_media(message: Message, transcribed_text: str) -> None:
    """Сохранить в историю расшифровку голосового или видеосообщения.

    `message` — оригинальное медиа-сообщение (его автор и id используются как
    источник). `transcribed_text` — результат распознавания. Сообщение помечается
    как `is_media=True`, чтобы LLM не воспринимало формулировки слишком буквально.
    """
    if message.chat.type not in {"group", "supergroup"}:
        return
    if message.from_user is None or message.from_user.is_bot:
        return
    text = (transcribed_text or "").strip()
    if not text:
        return

    chat_id = message.chat.id
    history = _get_history(chat_id)
    msg_date = message.date or datetime.now(timezone.utc)
    reply_to = message.reply_to_message

    auto_flush: list[StoredMessage] | None = None
    async with history.lock:
        if (
            history.last_summary_at is not None
            and history.messages
            and msg_date - history.last_summary_at > SUMMARY_AUTO_TRIGGER_DELTA
        ):
            auto_flush = history.messages
            history.messages = []
            history.last_summary_at = msg_date

        history.messages.append(
            StoredMessage(
                user_id=message.from_user.id,
                full_name=_author_name(message),
                text=text,
                date=msg_date,
                message_id=message.message_id,
                reply_to_message_id=reply_to.message_id if reply_to else None,
                is_media=True,
            )
        )
        if len(history.messages) > HISTORY_MAX_MESSAGES:
            history.messages = history.messages[-HISTORY_MAX_MESSAGES:]

    if auto_flush:
        asyncio.create_task(
            _send_auto_summary(message.bot, chat_id, auto_flush)
        )


_MARKDOWN_V2_SPECIAL_CHARS = r"_*[]()~`>#+-=|{}.!\\"


def _strip_md_v2_escapes(text: str) -> str:
    """Снимает экранирование MarkdownV2 для плейн-текстового fallback'а.

    Когда Telegram отказывается парсить ответ LLM как MarkdownV2 (например,
    модель оставила непарную `*`), нам нужно отправить текст без parse_mode.
    Тогда литералы `\\.` или `\\!` смотрелись бы как мусор — этот хелпер их чистит.
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] in _MARKDOWN_V2_SPECIAL_CHARS:
            out.append(text[i + 1])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


async def _send_summary_text(
    bot,
    chat_id: int,
    text: str,
    *,
    edit_message=None,
) -> None:
    """Отправить готовый текст саммари с MarkdownV2; при ошибке — plain text.

    `edit_message` — если задано, пытаемся `edit_text`, иначе `send_message`.
    Длинные тексты, которые не лезут в edit, переотправляются новым сообщением.
    """
    async def _do(parse_mode: Optional[str], payload: str) -> None:
        if edit_message is not None:
            try:
                await edit_message.edit_text(payload, parse_mode=parse_mode)
                return
            except Exception as exc:
                log.warning(
                    "Summary edit_text failed chat_id=%s parse_mode=%s: %s; "
                    "fallback to send_message",
                    chat_id, parse_mode, exc,
                )
        await bot.send_message(chat_id, payload, parse_mode=parse_mode)

    try:
        await _do("MarkdownV2", text)
        return
    except Exception as exc:
        # Скорее всего LLM сгенерировала невалидный MarkdownV2.
        log.warning(
            "Summary MarkdownV2 send failed chat_id=%s: %s; fallback to plain text",
            chat_id, exc,
        )

    plain = _strip_md_v2_escapes(text)
    try:
        await _do(None, plain)
    except Exception as exc:
        log.error(
            "Summary plain-text send failed chat_id=%s: %s", chat_id, exc, exc_info=True
        )


async def _send_auto_summary(
    bot, chat_id: int, messages: list[StoredMessage]
) -> None:
    log.info(
        "Auto summary triggered chat_id=%s messages=%d", chat_id, len(messages)
    )
    try:
        text = await _generate_summary(messages)
    except Exception as exc:
        log.error(
            "Auto summary generation failed chat_id=%s: %s",
            chat_id,
            exc,
            exc_info=True,
        )
        return
    await _send_summary_text(bot, chat_id, text)


# ─────────────────────────── middleware ──────────────────────────────────

class MessageHistoryMiddleware(BaseMiddleware):
    """Outer middleware: фиксируем каждое сообщение из групп в историю."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            try:
                await save_message_to_history(event)
            except Exception as exc:
                log.error(
                    "save_message_to_history failed: %s", exc, exc_info=True
                )
        return await handler(event, data)


# ─────────────────────────── router / handlers ───────────────────────────

router = Router(name="summary")


@router.message(
    Command("summary"),
    F.chat.type.in_({"supergroup", "group"}),
)
async def cmd_summary(message: Message) -> None:
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else None
    log.info(
        "Summary requested chat_id=%s user_id=%s", chat_id, user_id
    )

    if AsyncOpenAI is None:
        await message.reply(
            "❌ Пакет `openai` не установлен на сервере."
        )
        return
    if not AI_HUB_MIX_API_KEY:
        await message.reply("❌ `AI_HUB_MIX_API_KEY` не настроен.")
        return

    history = _get_history(chat_id)
    now = message.date or datetime.now(timezone.utc)

    async with history.lock:
        msgs = list(history.messages)
        # Сохраняем дату вызова команды сразу — даже если истории нет,
        # чтобы корректно работало правило 1-дневного авто-флаша.
        history.last_summary_at = now

    if not msgs:
        await message.reply(
            "📭 История сообщений пока пуста — суммаризировать нечего."
        )
        return

    status = await message.reply(
        f"⏳ Генерирую саммари по {len(msgs)} сообщениям…"
    )

    try:
        text = await _generate_summary(msgs)
    except Exception as exc:
        log.error("Summary generation failed chat_id=%s: %s", chat_id, exc, exc_info=True)
        try:
            await status.edit_text("❌ Не удалось сгенерировать саммари.")
        except Exception:
            await message.answer("❌ Не удалось сгенерировать саммари.")
        return

    # Очищаем буфер только после успешной генерации.
    async with history.lock:
        history.messages.clear()

    await _send_summary_text(message.bot, chat_id, text, edit_message=status)
