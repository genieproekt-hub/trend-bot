"""Бот-трендолог для контент-ниш (российский рынок) с оффером Джин-клуба.

Пользователь пишет свою нишу -> бот ищет через YouTube Data API реальные
ролики именно по этой нише в России, отсортированные по числу просмотров
(вирусные «миллионники» в теме пользователя) — единственный источник ссылок,
без стороннего веб-поиска. Затем ИИ (через ProxyAPI) собирает подборку из
10 примеров со ссылкой на реальный ролик и идеей, что из него перенять.
Подборка выдаётся по частям с кнопкой «Продолжаем». Лимит — 3 подборки в
сутки на пользователя, после каждой — приглашение в Genie Club.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from openai import AsyncOpenAI

# ═══════════════════════════════════════════════════════════════
# КОНФИГ — секреты берутся из переменных окружения Railway
# ═══════════════════════════════════════════════════════════════

BOT_TOKEN = os.getenv("BOT_TOKEN")
AI_API_KEY = os.getenv("PROXYAPI_KEY") or os.getenv("OPENAI_API_KEY")
AI_BASE_URL = os.getenv("PROXYAPI_BASE_URL") or "https://api.proxyapi.ru/openai/v1"
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o-mini")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
# Telegram ID Жени (или других админов) через запятую — команда /reset только для них.
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

DAILY_LIMIT = 3
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
TRENDS_COUNT = 10

YOUTUBE_REGION = "RU"
MARKET_LABEL = "российском рынке контента"
REPORT_HEADER = "🇷🇺 ТРЕНДЫ КОРОТКИХ ВИДЕО — РОССИЙСКИЙ РЫНОК"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("trend_bot")

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в переменных окружения Railway")
if not AI_API_KEY:
    raise RuntimeError("Не задан PROXYAPI_KEY (или OPENAI_API_KEY) в переменных окружения Railway")
if not YOUTUBE_API_KEY:
    raise RuntimeError("Не задан YOUTUBE_API_KEY в переменных окружения Railway")

ai = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ═══════════════════════════════════════════════════════════════
# ХРАНЕНИЕ СОСТОЯНИЯ — сколько подборок пользователь получил сегодня.
# Простой JSON-файл — этого достаточно для лид-магнита.
# После редеплоя на Railway файл сбрасывается (диск эфемерный) —
# для постоянного хранения между релизами нужна база данных (Postgres/Railway Volume).
# ═══════════════════════════════════════════════════════════════

DATA_FILE = Path("users_state.json")


def load_state() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("Не удалось прочитать %s, начинаю с чистого состояния", DATA_FILE)
    return {}


def save_state(state: dict) -> None:
    try:
        DATA_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        log.error("Не удалось сохранить состояние: %s", e)


STATE = load_state()


def today_str() -> str:
    return datetime.now(MOSCOW_TZ).date().isoformat()


def check_quota(user_id: int) -> tuple[bool, int]:
    """Возвращает (можно ли делать запрос, сколько подборок уже использовано сегодня)."""
    entry = STATE.get(str(user_id), {})
    if entry.get("date") != today_str():
        return True, 0
    used = entry.get("count", 0)
    return used < DAILY_LIMIT, used


def use_quota(user_id: int, niche: str) -> int:
    """Увеличивает счётчик за сегодня, возвращает новое количество использованных подборок."""
    today = today_str()
    entry = STATE.get(str(user_id), {})
    if entry.get("date") != today:
        entry = {"date": today, "count": 0}
    entry["count"] = entry.get("count", 0) + 1
    entry["niche"] = niche
    STATE[str(user_id)] = entry
    save_state(STATE)
    return entry["count"]


def reset_user(user_id: int) -> None:
    STATE.pop(str(user_id), None)
    save_state(STATE)


# ═══════════════════════════════════════════════════════════════
# ПИТЧ ДЖИН-КЛУБА — ФИКСИРОВАННЫЙ ТЕКСТ (не генерируется ИИ)
# Цену и гарантию модель никогда не придумывает сама — только этот блок.
# Актуализировать вручную, если изменятся условия на genieclub.tilda.ws
# ═══════════════════════════════════════════════════════════════

GENIE_PITCH = (
    "🧞 GENIE CLUB — простой маркетинг с ИИ\n\n"
    "Хочешь тренды и готовый контент по любой нише не раз в месяц, а когда угодно — "
    "плюс ещё 20+ ИИ-ботов под маркетинг (ЦА, офферы, воронки, лид-магниты, Reels)?\n\n"
    "Внутри Genie Club:\n"
    "• 20+ ИИ-ботов GPTS под задачи маркетинга\n"
    "• 15+ уроков по маркетингу с нуля\n"
    "• Сборник Джин-промтов\n"
    "• Воркшоп по созданию ИИ-агентов под контент-завод\n"
    "• Живые онлайн-разборы проектов\n"
    "• Бонусы и закрытое сообщество\n\n"
    "Один платёж 4 900 ₽ (было 10 000 ₽) — доступ навсегда, без подписки.\n"
    "Гарантия: 14 дней на возврат, без вопросов.\n\n"
    "Вступить: https://genieclub.tilda.ws/"
)

QUOTA_MESSAGE = (
    f"На сегодня подборки трендов закончились ({DAILY_LIMIT} из {DAILY_LIMIT}) 🙌\n"
    "Лимит обновится завтра. Если хочешь тренды без ограничений — плюс ещё 20+ ИИ-ботов "
    "для маркетинга — загляни в Genie Club:\n\n" + GENIE_PITCH
)

# ═══════════════════════════════════════════════════════════════
# ПРОМПТЫ
# ═══════════════════════════════════════════════════════════════

TRENDS_SYSTEM_PROMPT = """Ты — аналитик по контенту в нише «{niche}» на {market_label} и SMM-стратег.
Тебе дали список реальных роликов YouTube именно по этой нише (или близкой тематике) с наибольшим числом просмотров в России — настоящие вирусные ролики-«миллионники», которые уже посмотрели тысячи и миллионы людей. Это единственный источник фактов и ссылок, других данных у тебя нет.

Собери до {n} примеров на основе этого списка. Для каждого объясни, что конкретно в нём сработало (подача, формат, крючок в начале, эмоция, музыка) и как использовать эту же идею в собственных роликах в нише «{niche}».

Жёсткие правила:
- Название и ссылка в поле "Референс" должны опираться на конкретный ролик из списка ниже. Ссылку бери ДОСЛОВНО, никогда не выдумывай и не изменяй ни одного символа в URL.
- Платформу и формат указывай ровно так, как помечено в списке (Shorts или обычное видео) — не выдумывай и не переименовывай.
- Если подходящих роликов в списке меньше {n} — верни меньше пунктов, но не выдумывай.
- Пиши живо и по-человечески, с уместными эмодзи (по 1-2 на пункт) — текст не должен быть сухим. Но не используй markdown-разметку (звёздочки, решётки) — Telegram её не показывает как форматирование.
- Пиши на русском языке.

Формат ответа — от 3 до {n} пунктов, каждый строго по шаблону (эмодзи в начале строк обязательны):

1. [Название/суть ролика] 🔥
📱 Платформа: [Shorts / YouTube-видео — как указано в списке]
🔗 Референс: [URL из списка — дословно]
💡 Почему заходит: [1-2 живых предложения, что именно сработало]
🎯 Как использовать в нише «{niche}»: [конкретная идея для собственного контента]

(и так далее)"""

TRENDS_USER_PROMPT = """Ниша: {niche}

Ролики YouTube по нише с наибольшим числом просмотров в России (единственный источник фактов и ссылок, бери дословно):
{context}

Собери до {n} примеров строго по формату из системного промпта."""


# ═══════════════════════════════════════════════════════════════
# ПОИСК И ГЕНЕРАЦИЯ
# ═══════════════════════════════════════════════════════════════

def _parse_iso8601_duration(value: str) -> int | None:
    """PT#H#M#S -> секунды. Возвращает None, если не удалось разобрать."""
    m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", value or "")
    if not m or not any(m.groups()):
        return None
    h, mnt, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mnt * 60 + s


YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"


async def _youtube_search_and_detail(search_params: dict) -> list[dict]:
    """search.list по заданным параметрам + videos.list для точной длительности и просмотров."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            search_resp = await client.get(YOUTUBE_SEARCH_URL, params=search_params)
            search_resp.raise_for_status()
            search_data = search_resp.json()

            video_ids = [
                it["id"]["videoId"]
                for it in search_data.get("items", [])
                if it.get("id", {}).get("videoId")
            ]
            if not video_ids:
                return []

            details_params = {
                "part": "snippet,contentDetails,statistics",
                "id": ",".join(video_ids),
                "key": YOUTUBE_API_KEY,
            }
            details_resp = await client.get(YOUTUBE_VIDEOS_URL, params=details_params)
            details_resp.raise_for_status()
            details_data = details_resp.json()
    except Exception as e:
        log.error("Ошибка YouTube API: %s", e)
        return []

    items = []
    for it in details_data.get("items", []):
        vid = it.get("id")
        snippet = it.get("snippet", {})
        content = it.get("contentDetails", {})
        stats = it.get("statistics", {})
        duration_s = _parse_iso8601_duration(content.get("duration", ""))
        if not vid or duration_s is None:
            continue
        is_short = duration_s <= 180
        items.append(
            {
                "title": snippet.get("title", ""),
                "channel": snippet.get("channelTitle", ""),
                "url": f"https://www.youtube.com/shorts/{vid}" if is_short else f"https://www.youtube.com/watch?v={vid}",
                "views": int(stats.get("viewCount", 0) or 0),
                "tags": ", ".join((snippet.get("tags") or [])[:6]),
                "duration_s": duration_s,
                "is_short": is_short,
            }
        )
    items.sort(key=lambda x: x["views"], reverse=True)
    return items


async def fetch_youtube_niche_videos(query: str, max_results: int = 30) -> list[dict]:
    """Реальные ролики YouTube по конкретной нише в России, отсортированные по числу
    просмотров (вирусные «миллионники» в теме пользователя) — единственный источник ссылок для промпта.
    """
    base_params = {
        "part": "snippet",
        "type": "video",
        "q": query,
        "order": "viewCount",
        "regionCode": YOUTUBE_REGION,
        "relevanceLanguage": "ru",
        "safeSearch": "none",
        "maxResults": max_results,
        "key": YOUTUBE_API_KEY,
    }
    # Сначала пробуем короткие вертикальные ролики (формат бота — идеи для Reels/Shorts).
    items = await _youtube_search_and_detail({**base_params, "videoDuration": "short"})
    if items:
        return items
    # Если по нише не нашлось коротких — берём вирусные ролики любой длины, честно помечаем формат.
    return await _youtube_search_and_detail(base_params)


def format_youtube_trending(items: list[dict]) -> str:
    if not items:
        return "(не удалось найти популярные ролики YouTube по этой нише в этом регионе)"
    lines = []
    for it in items:
        tag = "Shorts" if it["is_short"] else "видео"
        views = f"{it['views']:,}".replace(",", " ")
        tags_part = f" | теги: {it['tags']}" if it["tags"] else ""
        lines.append(
            f"- [{tag}, {it['duration_s']}с, {views} просмотров] "
            f"{it['title']} — канал {it['channel']} ({it['url']}){tags_part}"
        )
    return "\n".join(lines)


async def generate_trends(niche: str, context: str) -> str:
    system_prompt = TRENDS_SYSTEM_PROMPT.format(market_label=MARKET_LABEL, niche=niche, n=TRENDS_COUNT)
    user_prompt = TRENDS_USER_PROMPT.format(niche=niche, context=context, n=TRENDS_COUNT)
    completion = await ai.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=4000,
        temperature=0.6,
    )
    return completion.choices[0].message.content or "Не получилось собрать тренды, попробуй ещё раз."


async def build_report(niche: str) -> str:
    youtube_items = await fetch_youtube_niche_videos(niche)
    context = format_youtube_trending(youtube_items)
    body = await generate_trends(niche, context)
    return f"{REPORT_HEADER}\nНиша: {niche}\n\n{body}"


def split_message(text: str, limit: int = 3500) -> list[str]:
    """Режем длинный текст на части под лимит Telegram, стараясь резать по пунктам трендов."""
    if len(text) <= limit:
        return [text]
    parts = []
    chunks = re.split(r"\n(?=\d{1,2}\. )", text)
    current = ""
    for chunk in chunks:
        if len(current) + len(chunk) + 1 > limit:
            if current:
                parts.append(current.strip())
            current = chunk
        else:
            current = f"{current}\n{chunk}" if current else chunk
    if current:
        parts.append(current.strip())
    return parts or [text[:limit]]


async def keep_typing(chat_id: int, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4)
        except asyncio.TimeoutError:
            pass


async def send_batch(chat_id: int, batch: list[str], delay: float = 1.0) -> None:
    """Отправляет несколько сообщений подряд (например, части одной подборки) с небольшой паузой."""
    for i, text in enumerate(batch):
        await bot.send_message(chat_id, text)
        if i < len(batch) - 1:
            try:
                await bot.send_chat_action(chat_id, "typing")
            except Exception:
                pass
            await asyncio.sleep(delay)


CONTINUE_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="✅ Да, продолжаем", callback_data="continue")]]
)


async def send_gate(chat_id: int) -> None:
    """Присылает кнопку «Продолжаем?» — следующая часть уходит только по нажатию."""
    await bot.send_message(chat_id, "Продолжаем? 👇", reply_markup=CONTINUE_KEYBOARD)


# user_id -> очередь оставшихся «пачек» сообщений, которые ждут нажатия кнопки «Продолжаем»
PENDING: dict[int, list[list[str]]] = {}


# ═══════════════════════════════════════════════════════════════
# ХЕНДЛЕРЫ
# ═══════════════════════════════════════════════════════════════

@dp.message(CommandStart())
async def on_start(message: Message):
    await message.answer(
        "Привет! 👋 Я — бот-трендолог.\n\n"
        "Напиши свою нишу (например: «фитнес-тренер», «психолог», «продажа украшений hand-made») — "
        f"и я подберу подборку из {TRENDS_COUNT} вирусных роликов по этой нише на российском рынке. "
        "К каждому — ссылка-референс, почему он заходит и как использовать эту идею в своих роликах.\n\n"
        f"Лимит — {DAILY_LIMIT} подборки в сутки 🎁"
    )


@dp.message(Command("reset"))
async def on_reset(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    reset_user(message.from_user.id)
    await message.answer("Готово, сбросил лимит для тебя.")


@dp.message()
async def on_message(message: Message):
    if not message.text:
        await message.answer("Я понимаю только текст. Напиши нишу словами 🙂")
        return

    niche = message.text.strip()
    if len(niche) < 2:
        await message.answer("Напиши нишу чуть подробнее, например: «коучинг для мам» 🙂")
        return
    if len(niche) > 200:
        await message.answer("Слишком длинно — сократи до пары слов, обозначающих нишу 🙂")
        return

    user_id = message.from_user.id

    allowed, used_today = check_quota(user_id)
    if not allowed:
        await message.answer(QUOTA_MESSAGE)
        return

    stop_event = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(message.chat.id, stop_event))
    await message.answer(f"🔎 Ищу тренды для ниши «{niche}» — российский рынок. Это займёт 20-40 секунд...")

    try:
        report = await build_report(niche)
    except Exception as e:
        log.error("Ошибка генерации трендов: %s", e)
        stop_event.set()
        await typing_task
        await message.answer("Упс, что-то пошло не так. Попробуй ещё раз через минуту 🛠")
        return

    stop_event.set()
    await typing_task

    used_now = use_quota(user_id, niche)
    remaining = max(DAILY_LIMIT - used_now, 0)

    if remaining > 0:
        follow_up = (
            "Хочешь ещё подборку трендов? По этой же нише или по другой — просто напиши 🙂\n"
            f"Осталось подборок сегодня: {remaining}/{DAILY_LIMIT}."
        )
    else:
        follow_up = f"На сегодня лимит подборок исчерпан ({DAILY_LIMIT}/{DAILY_LIMIT}). Возвращайся завтра 🙂"

    batch_main = split_message(report)
    batch_final = [GENIE_PITCH, follow_up]

    # Показываем сразу только подборку роликов. Питч и вопрос про ещё одну подборку — после нажатия кнопки.
    PENDING[user_id] = [batch_final]
    await send_batch(message.chat.id, batch_main)
    await send_gate(message.chat.id)


@dp.callback_query(F.data == "continue")
async def on_continue(callback: CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    queue = PENDING.get(user_id)
    if not queue:
        await bot.send_message(callback.message.chat.id, "Эта подборка уже устарела — напиши нишу ещё раз 🙂")
        return

    batch = queue.pop(0)
    await send_batch(callback.message.chat.id, batch)

    if queue:
        await send_gate(callback.message.chat.id)
    else:
        PENDING.pop(user_id, None)


async def main():
    me = await bot.get_me()
    log.info("Bot started: @%s", me.username)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
