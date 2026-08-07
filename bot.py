"""Бот-трендолог для контент-ниш (RU + US рынок) с оффером Джин-клуба.

Пользователь пишет свою нишу -> бот ищет свежие тренды коротких видео через
Tavily (веб-поиск) отдельно для российского и американского рынка, затем ИИ
(через ProxyAPI) собирает по 5 трендов на каждый рынок со ссылкой-референсом
и идеей адаптации под нишу. Лимит — 3 подборки в сутки на пользователя,
после каждой подборки — приглашение в Genie Club.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
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
Тебе дали нишу и свежие результаты веб-поиска (заголовки, сниппеты, ссылки).

Собери {n} РЕАЛЬНЫХ трендов коротких видео, которые сейчас на слуху на {market_label}. Тренд не обязан быть придуман специально под нишу пользователя — бери реальные, действительно существующие тренды форматов и механик, а затем отдельно предложи, как его можно адаптировать под нишу «{niche}».

Жёсткие правила:
- Название тренда и факты о нём бери из предоставленных результатов поиска.
- Поле "Референс" — это URL, СКОПИРОВАННЫЙ БЕЗ ИЗМЕНЕНИЙ из результатов поиска ниже. Никогда не выдумывай и не досочиняй ссылки. Если среди результатов нет прямой ссылки на видео — возьми ссылку на источник, где этот тренд показан или разобран (не выдавай её за прямую ссылку на ролик).
- Не используй markdown (звёздочки, решётки) — Telegram их не показывает как форматирование.
- Пиши на русском языке, даже если рынок — американский.

Формат ответа — ровно {n} пунктов, каждый строго по шаблону:

1. [Название тренда]
Платформа: [Reels/TikTok/Shorts/посты/карусели и т.д.]
Референс: [URL из результатов поиска]
Почему заходит: [1-2 предложения — психология или механика формата]
Как адаптировать под нишу «{niche}»: [конкретная идея, как переложить этот тренд на нишу пользователя]

(и так далее до пункта {n})"""

TRENDS_USER_PROMPT = """Ниша: {niche}
Рынок: {market_label}

Результаты веб-поиска по теме (используй как фактическую опору, ссылки бери отсюда дословно):
{context}

Собери {n} трендов строго по формату из системного промпта."""

MARKETS = {
    "ru": {
        "label": "российском рынке контента",
        "query_tpl": "тренды коротких видео {niche} 2026 рилс тикток shorts примеры",
        "header": "🇷🇺 ТРЕНДЫ КОРОТКИХ ВИДЕО — РОССИЙСКИЙ РЫНОК",
    },
    "us": {
        "label": "американском рынке контента (US)",
        "query_tpl": "short video content trends {niche_en} 2026 TikTok Instagram Reels examples",
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


async def tavily_search(query: str, max_results: int = 8) -> list[dict]:
    try:
        result = await asyncio.to_thread(
            tavily.search,
            query=query,
            search_depth="advanced",
            max_results=max_results,
            days=180,
        )
        return result.get("results", [])
    except Exception as e:
        log.error("Ошибка поиска Tavily (%s): %s", query, e)
        return []


def build_context(results: list[dict]) -> str:
    if not results:
        return "(поиск не дал результатов — опирайся на общеизвестные устойчивые форматы коротких видео и честно укажи, что прямой ссылки нет)"
    lines = []
    for r in results:
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        content = (r.get("content") or "").strip()[:400]
        lines.append(f"- {title} ({url})\n  {content}")
    return "\n".join(lines)


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
        query = market["query_tpl"].format(niche_en=niche_en)
    else:
        query = market["query_tpl"].format(niche=niche)
    results = await tavily_search(query)
    context = build_context(results)
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


async def send_sequence(chat_id: int, messages: list[str], delay: float = 1.3) -> None:
    """Отправляет сообщения одно за другим с небольшой паузой — вместо одной стены текста."""
    for i, text in enumerate(messages):
        await bot.send_message(chat_id, text)
        if i < len(messages) - 1:
            try:
                await bot.send_chat_action(chat_id, "typing")
            except Exception:
                pass
            await asyncio.sleep(delay)


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

    sequence: list[str] = []
    sequence.extend(split_message(ru_report))
    sequence.append("Продолжаем?")
    sequence.extend(split_message(us_report))
    sequence.append("Продолжаем?")
    sequence.append(GENIE_PITCH)
    if remaining > 0:
        sequence.append(
            "Хочешь ещё подборку трендов? По этой же нише или по другой — просто напиши 🙂\n"
            f"Осталось подборок сегодня: {remaining}/{DAILY_LIMIT}."
        )
    else:
        sequence.append(f"На сегодня лимит подборок исчерпан ({DAILY_LIMIT}/{DAILY_LIMIT}). Возвращайся завтра 🙂")

    await send_sequence(message.chat.id, sequence)


async def main():
    me = await bot.get_me()
    log.info("Bot started: @%s", me.username)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
