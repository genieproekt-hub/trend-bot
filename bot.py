"""Бот-трендолог для контент-ниш (RU + US рынок) с оффером Джин-клуба.

Пользователь пишет свою нишу -> бот собирает три слоя данных о трендах:
1) официальный чарт YouTube Trending (реально живые, актуальные ролики по региону),
2) веб-поиск Tavily по видеоплощадкам (Instagram/TikTok/YouTube/VK) за последний месяц,
3) общий контекст (статьи-обзоры трендов) для понимания сути.
Затем ИИ (через ProxyAPI) собирает до 5 трендов на каждый рынок со ссылкой на
реальный ролик и идеей адаптации под нишу. Подборка выдаётся по частям с кнопкой
«Продолжаем». Лимит — 3 подборки в сутки на пользователя, после каждой —
приглашение в Genie Club.
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
from tavily import TavilyClient

# ═══════════════════════════════════════════════════════════════
# КОНФИГ — секреты берутся из переменных окружения Railway
# ═══════════════════════════════════════════════════════════════

BOT_TOKEN = os.getenv("BOT_TOKEN")
AI_API_KEY = os.getenv("PROXYAPI_KEY") or os.getenv("OPENAI_API_KEY")
AI_BASE_URL = os.getenv("PROXYAPI_BASE_URL") or "https://api.proxyapi.ru/openai/v1"
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o-mini")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
# Telegram ID Жени (или других админов) через запятую — команда /reset только для них.
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

DAILY_LIMIT = 3
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
TRENDS_PER_MARKET = 5

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("trend_bot")

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в переменных окружения Railway")
if not AI_API_KEY:
    raise RuntimeError("Не задан PROXYAPI_KEY (или OPENAI_API_KEY) в переменных окружения Railway")
if not TAVILY_API_KEY:
    raise RuntimeError("Не задан TAVILY_API_KEY в переменных окружения Railway")
if not YOUTUBE_API_KEY:
    raise RuntimeError("Не задан YOUTUBE_API_KEY в переменных окружения Railway")

ai = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)
tavily = TavilyClient(api_key=TAVILY_API_KEY)
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

TRANSLATE_PROMPT = (
    "Переведи название ниши в короткую поисковую фразу на английском (2-4 слова), "
    "как её искали бы в англоязычных источниках про маркетинг и контент. "
    "Ответь только фразой, без кавычек и пояснений.\nНиша: {niche}"
)

TRENDS_SYSTEM_PROMPT = """Ты — аналитик по трендам в коротких видео (Reels/TikTok/Shorts) и SMM-стратег с опытом работы на {market_label}.
Тебе дали нишу и три блока данных (см. ниже, в порядке приоритета):
1) официальный чарт YouTube Trending по региону — реально живые тренды прямо сейчас, ссылки гарантированно рабочие;
2) свежие ссылки на ролики из веб-поиска по видеоплощадкам за последний месяц;
3) дополнительные статьи/обзоры трендов — только для контекста.

Собери до {n} РЕАЛЬНЫХ трендов коротких видео за последний месяц, которые люди сейчас снимают и адаптируют под себя на {market_label}. Тренд не обязан изначально относиться к нише пользователя — бери реальные, действительно существующие тренды форматов и механик (в том числе из YouTube Trending, даже если ролик по теме, далёкой от ниши — например, спорт, музыка, новости), а затем отдельно предложи, как его адаптировать под нишу «{niche}».

Жёсткие правила про ссылки:
- Поле "Референс" заполняй ТОЛЬКО ссылками, дословно скопированными из блоков данных ниже. Никогда не выдумывай, не досочиняй и не изменяй ни одного символа в URL.
- Приоритет источников для поля «Референс»: сначала блок 1 (YouTube Trending), затем блок 2 (ссылки на ролики из веб-поиска). Блок 3 (статьи/обзоры) используй для «Референс» только если для тренда совсем нет ссылки на сам ролик — и в этом случае явно пиши в поле: «(обзор/статья, не прямой ролик)».
- Если по какому-то потенциальному тренду вообще нет ни одной подходящей ссылки среди предоставленных данных — не включай такой тренд в список вообще. Лучше вернуть 3 подтверждённых тренда, чем {n} с неточной или притянутой ссылкой.
- Бери только тренды, которые по данным актуальны сейчас (последний месяц) — не бери устаревшие форматы.
- Не используй markdown (звёздочки, решётки) — Telegram их не показывает как форматирование.
- Пиши на русском языке, даже если рынок — американский.

Формат ответа — от 3 до {n} пунктов, каждый строго по шаблону:

1. [Название тренда]
Платформа: [Reels/TikTok/Shorts/YouTube и т.д.]
Референс: [URL из данных — дословно]
Почему заходит: [1-2 предложения — психология или механика формата]
Как адаптировать под нишу «{niche}»: [конкретная идея, как переложить этот тренд на нишу пользователя]

(и так далее)

Если пригодных ссылок по этой нише почти нет — в самом конце добавь отдельную строку: "⚠️ Мало свежих роликов с прямыми ссылками по этой нише — часть трендов дана по обзорам, не по прямым видео." Если ссылки хорошие — эту строку не добавляй."""

TRENDS_USER_PROMPT = """Ниша: {niche}
Рынок: {market_label}

Результаты веб-поиска (используй как фактическую опору, ссылки бери отсюда дословно):
{context}

Собери до {n} трендов строго по формату из системного промпта."""

MARKETS = {
    "ru": {
        "label": "российском рынке контента",
        "country": "russia",
        "youtube_region": "RU",
        "video_domains": ["instagram.com", "youtube.com", "vk.com", "rutube.ru", "dzen.ru"],
        "video_query_tpl": "тренд рилс shorts клип {niche} свежий вирусный",
        "context_query_tpl": "тренды коротких видео {niche} этот месяц соцсети",
        "header": "🇷🇺 ТРЕНДЫ КОРОТКИХ ВИДЕО — РОССИЙСКИЙ РЫНОК",
    },
    "us": {
        "label": "американском рынке контента (US)",
        "country": "united states",
        "youtube_region": "US",
        "video_domains": ["tiktok.com", "instagram.com", "youtube.com"],
        "video_query_tpl": "TikTok Reels Shorts trend {niche_en} viral this month",
        "context_query_tpl": "short video content trend {niche_en} this month viral",
        "header": "🇺🇸 ТРЕНДЫ КОРОТКИХ ВИДЕО — АМЕРИКАНСКИЙ РЫНОК (US)",
    },
}


# ═══════════════════════════════════════════════════════════════
# ПОИСК И ГЕНЕРАЦИЯ
# ═══════════════════════════════════════════════════════════════

async def translate_niche(niche: str) -> str:
    try:
        completion = await ai.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": TRANSLATE_PROMPT.format(niche=niche)}],
            max_tokens=20,
            temperature=0,
        )
        text = (completion.choices[0].message.content or "").strip().strip('"')
        return text or niche
    except Exception as e:
        log.warning("Ошибка перевода ниши: %s", e)
        return niche


def _parse_iso8601_duration(value: str) -> int | None:
    """PT#H#M#S -> секунды. Возвращает None, если не удалось разобрать."""
    m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", value or "")
    if not m or not any(m.groups()):
        return None
    h, mnt, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mnt * 60 + s


async def fetch_youtube_trending(region_code: str, max_results: int = 50) -> list[dict]:
    """Официальный чарт YouTube «В тренде» по региону — реально живые, актуальные ролики."""
    params = {
        "part": "snippet,contentDetails",
        "chart": "mostPopular",
        "regionCode": region_code,
        "maxResults": max_results,
        "key": YOUTUBE_API_KEY,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get("https://www.googleapis.com/youtube/v3/videos", params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        log.error("Ошибка YouTube Trending API (%s): %s", region_code, e)
        return []

    items = []
    for it in data.get("items", []):
        vid = it.get("id")
        snippet = it.get("snippet", {})
        content = it.get("contentDetails", {})
        duration_s = _parse_iso8601_duration(content.get("duration", ""))
        if not vid:
            continue
        items.append(
            {
                "title": snippet.get("title", ""),
                "channel": snippet.get("channelTitle", ""),
                "url": f"https://www.youtube.com/watch?v={vid}",
                "tags": ", ".join((snippet.get("tags") or [])[:6]),
                "duration_s": duration_s,
                "is_short": duration_s is not None and duration_s <= 180,
            }
        )
    return items


def format_youtube_trending(items: list[dict]) -> str:
    if not items:
        return "(не удалось получить трендовые видео YouTube для этого региона)"
    lines = []
    for it in items:
        tag = "Shorts" if it["is_short"] else "видео"
        dur = f"{it['duration_s']}с" if it["duration_s"] is not None else "?"
        tags_part = f" | теги: {it['tags']}" if it["tags"] else ""
        lines.append(f"- [{tag}, {dur}] {it['title']} — канал {it['channel']} ({it['url']}){tags_part}")
    return "\n".join(lines)


async def tavily_search(
    query: str,
    max_results: int = 8,
    time_range: str = "month",
    include_domains: list[str] | None = None,
    country: str | None = None,
) -> list[dict]:
    kwargs = dict(query=query, search_depth="advanced", max_results=max_results, time_range=time_range)
    if include_domains:
        kwargs["include_domains"] = include_domains
    if country:
        kwargs["country"] = country
    try:
        result = await asyncio.to_thread(tavily.search, **kwargs)
        return result.get("results", [])
    except Exception as e:
        log.error("Ошибка поиска Tavily (%s): %s", query, e)
        return []


def _format_results(results: list[dict]) -> str:
    lines = []
    for r in results:
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        content = (r.get("content") or "").strip()[:400]
        lines.append(f"- {title} ({url})\n  {content}")
    return "\n".join(lines)


def build_context(youtube_items: list[dict], video_results: list[dict], general_results: list[dict]) -> str:
    parts = [
        "1) YOUTUBE TRENDING ПРЯМО СЕЙЧАС (официальный чарт YouTube по региону) — "
        "САМЫЙ НАДЁЖНЫЙ источник: это реально живые тренды, ссылки гарантированно рабочие:",
        format_youtube_trending(youtube_items),
        "",
        "2) СВЕЖИЕ ССЫЛКИ НА РОЛИКИ ИЗ ВЕБ-ПОИСКА (Instagram/TikTok/YouTube/VK, последний месяц) — "
        "используй, если в разделе 1 нет подходящего варианта:",
        _format_results(video_results) if video_results else "(прямых ссылок на ролики не найдено)",
        "",
        "3) ДОПОЛНИТЕЛЬНЫЙ КОНТЕКСТ (статьи и обзоры трендов — только для понимания сути, "
        "НЕ для поля «Референс», если выше есть более точная ссылка):",
        _format_results(general_results) if general_results else "(нет дополнительных материалов)",
    ]
    return "\n".join(parts)


async def generate_trends(niche: str, market_key: str, context: str) -> str:
    market = MARKETS[market_key]
    system_prompt = TRENDS_SYSTEM_PROMPT.format(market_label=market["label"], niche=niche, n=TRENDS_PER_MARKET)
    user_prompt = TRENDS_USER_PROMPT.format(
        niche=niche, market_label=market["label"], context=context, n=TRENDS_PER_MARKET
    )
    completion = await ai.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=2200,
        temperature=0.6,
    )
    return completion.choices[0].message.content or "Не получилось собрать тренды, попробуй ещё раз."


async def build_market_report(niche: str, market_key: str) -> str:
    market = MARKETS[market_key]
    if market_key == "us":
        niche_en = await translate_niche(niche)
        video_query = market["video_query_tpl"].format(niche_en=niche_en)
        context_query = market["context_query_tpl"].format(niche_en=niche_en)
    else:
        video_query = market["video_query_tpl"].format(niche=niche)
        context_query = market["context_query_tpl"].format(niche=niche)

    youtube_items, video_results, general_results = await asyncio.gather(
        fetch_youtube_trending(market["youtube_region"]),
        tavily_search(video_query, max_results=10, include_domains=market["video_domains"], country=market["country"]),
        tavily_search(context_query, max_results=6, country=market["country"]),
    )
    context = build_context(youtube_items, video_results, general_results)
    body = await generate_trends(niche, market_key, context)
    return f"{market['header']}\nНиша: {niche}\n\n{body}"


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
    await bot.send_message(chat_id, "Продолжаем?", reply_markup=CONTINUE_KEYBOARD)


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
        f"и я подберу {TRENDS_PER_MARKET} трендов коротких видео для российского рынка и "
        f"{TRENDS_PER_MARKET} — для американского. К каждому тренду — ссылка-референс, почему он заходит "
        "и как адаптировать его под твою нишу.\n\n"
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
    await message.answer(f"🔎 Ищу тренды для ниши «{niche}» — РФ и США. Это займёт 20-40 секунд...")

    try:
        ru_report, us_report = await asyncio.gather(
            build_market_report(niche, "ru"),
            build_market_report(niche, "us"),
        )
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

    batch_ru = split_message(ru_report)
    batch_us = split_message(us_report)
    batch_final = [GENIE_PITCH, follow_up]

    # Показываем сразу только первую подборку. Остальное — в очереди, ждёт нажатия кнопки.
    PENDING[user_id] = [batch_us, batch_final]
    await send_batch(message.chat.id, batch_ru)
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
