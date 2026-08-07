"""Бот-трендолог для контент-ниш (RU + US рынок) с оффером Джин-клуба.

Пользователь пишет свою нишу -> бот ищет свежие тренды контента через Tavily
(веб-поиск) отдельно для российского и американского рынка, затем ИИ
(через ProxyAPI) собирает по 10 трендов на каждый рынок с примером под нишу.
Первая ниша — бесплатно, дальше бот показывает фиксированный оффер Джин-клуба.
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path

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
# ХРАНЕНИЕ СОСТОЯНИЯ (кто уже использовал бесплатную нишу)
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


def has_used_free_niche(user_id: int) -> bool:
    return STATE.get(str(user_id), {}).get("used_free", False)


def mark_used_free_niche(user_id: int, niche: str) -> None:
    STATE[str(user_id)] = {"used_free": True, "niche": niche}
    save_state(STATE)


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

FREE_NICHE_INTRO = (
    "🎁 Это твой бесплатный разбор ниши. Дальше — покажу, как получить безлимит "
    "по трендам и ещё 20+ ИИ-ботов."
)

GATE_MESSAGE = (
    "Бесплатный разбор уже использован ✅\n\n"
    "Чтобы получать тренды по любым нишам без ограничений — плюс полный "
    "набор ИИ-ботов для маркетинга — загляни в Genie Club:\n\n" + GENIE_PITCH
)

# ═══════════════════════════════════════════════════════════════
# ПРОМПТЫ
# ═══════════════════════════════════════════════════════════════

TRANSLATE_PROMPT = (
    "Переведи название ниши в короткую поисковую фразу на английском (2-4 слова), "
    "как её искали бы в англоязычных источниках про маркетинг и контент. "
    "Ответь только фразой, без кавычек и пояснений.\nНиша: {niche}"
)

TRENDS_SYSTEM_PROMPT = """Ты — аналитик по контент-трендам и SMM-стратег с опытом работы на {market_label}.
Тебе дали нишу и свежие результаты веб-поиска по этой нише (заголовки, сниппеты, ссылки).
Твоя задача — на их основе выделить 10 РЕАЛЬНЫХ трендов контента, актуальных именно для {market_label} в этой нише.

Жёсткие правила:
- Опирайся на предоставленные результаты поиска. Если для какого-то тренда фактов не хватает — используй устоявшиеся, проверяемые форматы контента для этой ниши и явно не выдумывай несуществующие названия сервисов/функций.
- Никогда не проси у пользователя лишних уточнений — работай с тем, что дано.
- Не используй markdown (звёздочки, решётки) — Telegram их не показывает как форматирование.
- Пиши на русском языке, даже если рынок — американский.

Формат ответа — ровно 10 пунктов, каждый строго по шаблону:

1. [Название тренда]
Платформа: [где заходит: Reels/TikTok/Shorts/посты/карусели/подкасты и т.д.]
Почему заходит: [1-2 предложения — психология или механика формата]
Пример под нишу «{niche}»: [конкретный, готовый к съёмке/публикации пример именно для этой ниши]
Как применить: [1 конкретный практический шаг на сегодня]

(и так далее до пункта 10)

После списка добавь строку "Источники:" и перечисли 3-5 доменов из результатов поиска, на которые ты опирался."""

TRENDS_USER_PROMPT = """Ниша: {niche}
Рынок: {market_label}

Результаты веб-поиска по теме (используй как фактическую опору):
{context}

Собери 10 трендов строго по формату из системного промпта."""

MARKETS = {
    "ru": {
        "label": "российском рынке контента",
        "query_tpl": "тренды контента {niche} 2026 соцсети рилс блог",
        "header": "🇷🇺 ТРЕНДЫ — РОССИЙСКИЙ РЫНОК",
    },
    "us": {
        "label": "американском рынке контента (US)",
        "query_tpl": "content marketing trends {niche_en} 2026 TikTok Instagram Reels",
        "header": "🇺🇸 ТРЕНДЫ — АМЕРИКАНСКИЙ РЫНОК (US)",
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


async def tavily_search(query: str, max_results: int = 6) -> list[dict]:
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
        return "(поиск не дал результатов — опирайся на общеизвестные устойчивые форматы для этой ниши)"
    lines = []
    for r in results:
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        content = (r.get("content") or "").strip()[:400]
        lines.append(f"- {title} ({url})\n  {content}")
    return "\n".join(lines)


async def generate_trends(niche: str, market_key: str, context: str) -> str:
    market = MARKETS[market_key]
    system_prompt = TRENDS_SYSTEM_PROMPT.format(market_label=market["label"], niche=niche)
    user_prompt = TRENDS_USER_PROMPT.format(niche=niche, market_label=market["label"], context=context)
    completion = await ai.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=3000,
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


# ═══════════════════════════════════════════════════════════════
# ХЕНДЛЕРЫ
# ═══════════════════════════════════════════════════════════════

@dp.message(CommandStart())
async def on_start(message: Message):
    await message.answer(
        "Привет! 👋 Я — бот-трендолог.\n\n"
        "Напиши свою нишу (например: «фитнес-тренер», «психолог», «продажа украшений hand-made») — "
        "и я подберу 10 актуальных трендов контента для российского рынка и 10 — для американского, "
        "с примером под твою нишу и советом, как применить уже сегодня.\n\n"
        "Первый разбор — бесплатно 🎁"
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

    if has_used_free_niche(user_id):
        await message.answer(GATE_MESSAGE)
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

    for chunk in split_message(ru_report):
        await message.answer(chunk)
    for chunk in split_message(us_report):
        await message.answer(chunk)

    mark_used_free_niche(user_id, niche)
    await message.answer(FREE_NICHE_INTRO + "\n\n" + GENIE_PITCH)


async def main():
    me = await bot.get_me()
    log.info("Bot started: @%s", me.username)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
