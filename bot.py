"""Telegram-бот «ИИ-аналитик матчей». Точка входа: python bot.py

Режимы:
1. Лайв — идущие матчи всех видов спорта, ИИ разбирает живую статистику.
2. Прогнозы на ближайшие матчи — бот сам берёт расписание, таблицу, форму и
   коэффициенты, а ИИ делает предматчевый прогноз.
3. Разбор присланных данных — пользователь вставляет статистику/коэффициенты текстом.

Те же функции доступны в Mini App (webapp.py + static/index.html), который
запускается вместе с ботом.
"""

import asyncio
import base64
import html
import io
import logging
import time

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)
from aiogram.utils.chat_action import ChatActionSender
from aiohttp import web

from config import Settings, load_settings
from llm import AnalysisError, MatchAnalyst
from prompts import EXPRESS_PROMPT, LIVE_PROMPT, PREMATCH_PROMPT, RADAR_PROMPT, SYSTEM_PROMPT
from sources import (
    LEAGUES,
    SPORTS,
    SourceError,
    express_matches_card,
    find_fixture,
    keep_cache_warm,
    live_card,
    live_matches,
    match_card,
    radar_anomalies,
    upcoming_fixtures,
)
from tunnel import start_tunnel
from webapp import create_app

# Адрес Mini App выясняется при запуске (туннель), поэтому кнопка собирается динамически
webapp_url: str | None = None

TELEGRAM_LIMIT = 4096  # максимальная длина одного сообщения в Telegram
FORECAST_TTL = 1800  # готовый прогноз по матчу переиспользуется 30 минут

EXAMPLE = (
    "Реал Мадрид — Барселона, 60-я минута, 1:1\n"
    "Владение 46% / 54%, удары 9 (4 в створ) / 12 (3 в створ)\n"
    "xG 1.35 / 0.92, угловые 3 / 6, жёлтые 2 / 1\n"
    "Коэф. на П1: до матча 2.40 → сейчас 2.95\n"
    "Тотал больше 2.5: 2.10 → 1.72"
)

WELCOME = (
    "👋 Я — ИИ-аналитик матчей.\n"
    "Футбол (40 лиг и все лиги мира в лайве), баскетбол, хоккей, теннис, американский футбол, бейсбол, MMA.\n\n"
    "⚡ Радар аномалий — сканирует все матчи прямо сейчас и находит, где назревает гол, есть удаление или сухой ТМ!\n\n"
    "🎯 Экспресс дня — ИИ собирает купон из независимых событий с математическим перевесом над линией.\n\n"
    "🔴 Лайв — выберите идущий матч: свежая статистика, оценка ситуации и ставка со справедливым коэффициентом.\n\n"
    "📅 Прогнозы — таблица, форма, травмы и движение коэффициентов на ближайшие матчи.\n\n"
    "📸 Скриншот из БК — просто отправьте скриншот линии или матча, и ИИ сделает разбор."
)

HELP = (
    "Как пользоваться:\n"
    "• /radar — ⚡ Радар аномалий: матчи, где назревает гол или есть перевес\n"
    "• /express — 🎯 ИИ-Конструктор экспрессов (двойники, оптимум, риск)\n"
    "• /live — идущие сейчас матчи всех видов спорта и ИИ-идея ставки по живой статистике\n"
    "• /matches — ближайшие матчи: вид спорта → лига → ИИ-прогноз\n"
    "• /example — разбор примера лайв-статистики\n"
    "• 📸 Скриншот — отправьте скриншот из приложения БК с матчем или кэфами\n"
    "• или просто пришлите свои данные текстом: статистику (удары, xG, владение), составы"
)

def main_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="⚡ Радар лайв-аномалий (Hot)", callback_data="radar")],
        [InlineKeyboardButton(text="🎯 Собрать ИИ-экспресс дня", callback_data="express")],
        [InlineKeyboardButton(text="🔴 Лайв: матчи, которые идут сейчас", callback_data="live")],
        [InlineKeyboardButton(text="📅 Прогнозы на ближайшие матчи", callback_data="leagues")],
        [InlineKeyboardButton(text="⚡ Разобрать пример лайв-статистики", callback_data="demo")],
    ]
    if webapp_url:
        rows.insert(0, [InlineKeyboardButton(text="📱 Открыть приложение", web_app=WebAppInfo(url=webapp_url))])
    return InlineKeyboardMarkup(inline_keyboard=rows)

router = Router()


def split_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Режет длинный ответ на части по границам строк, чтобы влезть в лимит Telegram."""
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def _grid(buttons: list[InlineKeyboardButton], per_row: int = 2) -> list[list[InlineKeyboardButton]]:
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def sports_keyboard() -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text=label, callback_data=f"sp:{code}") for code, (label, _) in SPORTS.items()]
    return InlineKeyboardMarkup(inline_keyboard=_grid(buttons))


def leagues_keyboard(sport: str) -> InlineKeyboardMarkup:
    _, leagues = SPORTS[sport]
    buttons = [InlineKeyboardButton(text=name, callback_data=f"lg:{key}") for key, name in leagues.items()]
    rows = _grid(buttons)
    rows.append([InlineKeyboardButton(text="⬅️ Другой вид спорта", callback_data="leagues")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cooldown_left(user_id: int, settings: Settings, last_request: dict[int, float]) -> float:
    """Сколько секунд пользователю ждать до следующего запроса к ИИ (0 — можно)."""
    return max(0.0, settings.cooldown_seconds - (time.monotonic() - last_request.get(user_id, 0.0)))


async def reply_with_analysis(message: Message, data: str, analyst: MatchAnalyst, system_prompt: str) -> str | None:
    """Отправляет данные модели и выводит ответ в чат. Возвращает текст ответа или None при ошибке."""
    status = await message.answer("🤖 ИИ анализирует данные… (10–30 секунд)")
    try:
        # Пока модель думает, в чате висит «печатает…»
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            analysis = await analyst.analyze(data, system_prompt)
    except AnalysisError as e:
        await status.edit_text(f"❌ {e}")
        return None
    except Exception:
        logging.exception("Unexpected error while analysing")
        await status.edit_text("❌ Внутренняя ошибка. Попробуйте ещё раз позже.")
        return None

    parts = split_text(analysis)
    await status.edit_text(parts[0])
    for part in parts[1:]:
        await message.answer(part)
    return analysis


async def reply_with_photo_analysis(
    message: Message, b64_img: str, caption: str, analyst: MatchAnalyst, system_prompt: str = SYSTEM_PROMPT
) -> str | None:
    """Отправляет скриншот модели и выводит ответ в чат. Возвращает текст ответа или None при ошибке."""
    status = await message.answer("📸 ИИ распознаёт скриншот матча, коэффициенты и статистику… (10–25 секунд)")
    try:
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            analysis = await analyst.analyze_image(b64_img, caption=caption, system_prompt=system_prompt)
    except AnalysisError as e:
        await status.edit_text(f"❌ {e}")
        return None
    except Exception:
        logging.exception("Unexpected error while analysing image")
        await status.edit_text("❌ Внутренняя ошибка при анализе изображения. Попробуйте ещё раз позже.")
        return None

    parts = split_text(analysis)
    await status.edit_text(parts[0])
    for part in parts[1:]:
        await message.answer(part)
    return analysis


# ---------- Команды и меню ----------

@router.message(CommandStart())
async def on_start(message: Message) -> None:
    hint = "\n\n📱 Удобнее всего — в приложении: кнопка «Открыть приложение» ниже." if webapp_url else ""
    await message.answer(WELCOME + hint, reply_markup=main_menu())


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    await message.answer(HELP, reply_markup=main_menu())


@router.message(Command("matches"))
async def on_matches(message: Message) -> None:
    await message.answer("Выберите вид спорта:", reply_markup=sports_keyboard())


@router.callback_query(F.data == "leagues")
async def on_leagues_button(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer("Выберите вид спорта:", reply_markup=sports_keyboard())


@router.callback_query(F.data.startswith("sp:"))
async def on_sport(callback: CallbackQuery) -> None:
    sport = callback.data.split(":", 1)[1]
    await callback.answer()
    if sport not in SPORTS:
        await callback.message.answer("Выберите вид спорта:", reply_markup=sports_keyboard())
        return
    await callback.message.answer(f"{SPORTS[sport][0]} — выберите турнир:", reply_markup=leagues_keyboard(sport))


# ---------- Режим 1: прогнозы на ближайшие матчи ----------

@router.callback_query(F.data.startswith("lg:"))
async def on_league(callback: CallbackQuery, http: aiohttp.ClientSession) -> None:
    league = callback.data.split(":", 1)[1]
    sport = league.split("/", 1)[0]
    await callback.answer("Загружаю расписание…")
    try:
        fixtures = await upcoming_fixtures(http, league)
    except SourceError as e:
        logging.warning("Fixtures failed for %s: %s", league, e)
        await callback.message.answer("❌ Не удалось загрузить расписание. Попробуйте чуть позже.")
        return

    if not fixtures:
        await callback.message.answer(
            f"{LEAGUES.get(league, league)}: в ближайшие 7 дней матчей нет. Выберите другой турнир.",
            reply_markup=leagues_keyboard(sport),
        )
        return

    rows = [
        [InlineKeyboardButton(text=f"{f.kickoff_msk} · {f.title}", callback_data=f"fx:{league}:{f.event_id}")]
        for f in fixtures
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Другой турнир", callback_data=f"sp:{sport}")])
    await callback.message.answer(
        f"{LEAGUES.get(league, league)} — ближайшие матчи (время МСК).\nВыберите матч для прогноза:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("fx:"))
async def on_fixture(
    callback: CallbackQuery,
    http: aiohttp.ClientSession,
    analyst: MatchAnalyst,
    settings: Settings,
    last_request: dict[int, float],
    forecasts: dict[str, tuple[float, str]],
) -> None:
    _, league, event_id = callback.data.split(":", 2)
    message = callback.message

    # Свежий прогноз по этому матчу уже есть — отдаём сразу, без запроса к ИИ
    cached = forecasts.get(event_id)
    if cached and time.monotonic() - cached[0] < FORECAST_TTL:
        await callback.answer()
        for part in split_text(cached[1]):
            await message.answer(part)
        return

    wait = cooldown_left(callback.from_user.id, settings, last_request)
    if wait > 0:
        await callback.answer(f"Подождите {wait:.0f} с", show_alert=False)
        return
    await callback.answer("Собираю данные матча…")

    try:
        fixture = await find_fixture(http, league, event_id)
        if fixture is None:
            await message.answer("Этот матч уже начался или пропал из расписания. /matches")
            return
        card = await match_card(http, fixture)
    except SourceError as e:
        logging.warning("Match card failed for %s: %s", event_id, e)
        await message.answer("❌ Не удалось загрузить данные матча. Попробуйте чуть позже.")
        return

    last_request[callback.from_user.id] = time.monotonic()
    for chunk in split_text(f"📥 Данные, которые получит ИИ:\n\n{card}"):
        await message.answer(chunk)
    analysis = await reply_with_analysis(message, card, analyst, PREMATCH_PROMPT)
    if analysis:
        forecasts[event_id] = (time.monotonic(), analysis)
        await message.answer("Ещё прогноз?", reply_markup=sports_keyboard())


# ---------- Лайв: идущие матчи ----------

async def show_live(message: Message, http: aiohttp.ClientSession) -> None:
    try:
        matches = await live_matches(http)
    except SourceError as e:
        logging.warning("Live list failed: %s", e)
        await message.answer("❌ Не удалось загрузить лайв. Попробуйте чуть позже.")
        return

    if not matches:
        await message.answer(
            "Сейчас нет идущих матчей. Загляните во время игр или посмотрите прогнозы на ближайшие:",
            reply_markup=sports_keyboard(),
        )
        return

    # Эмодзи вида спорта в начале кнопки: в общем списке сразу видно, где футбол, а где теннис
    icons = {code: label.split()[0] for code, (label, _) in SPORTS.items()}
    shown = matches[:40]
    rows = [
        [InlineKeyboardButton(text=f"{icons.get(m.sport, '🔴')} {m.live_title}", callback_data=f"lv:{m.league}:{m.event_id}")]
        for m in shown
    ]
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="live")])
    more = f" (показаны первые {len(shown)} из {len(matches)})" if len(matches) > len(shown) else ""
    await message.answer(
        f"🔴 Идут сейчас: {len(matches)} матч(ей){more}.\nВыберите матч — ИИ разберёт живую статистику:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.message(Command("live"))
async def on_live_command(message: Message, http: aiohttp.ClientSession) -> None:
    await show_live(message, http)


@router.callback_query(F.data == "live")
async def on_live_button(callback: CallbackQuery, http: aiohttp.ClientSession) -> None:
    await callback.answer("Загружаю лайв…")
    await show_live(callback.message, http)


@router.callback_query(F.data.startswith("lv:"))
async def on_live_match(
    callback: CallbackQuery,
    http: aiohttp.ClientSession,
    analyst: MatchAnalyst,
    settings: Settings,
    last_request: dict[int, float],
) -> None:
    _, league, event_id = callback.data.split(":", 2)
    message = callback.message

    wait = cooldown_left(callback.from_user.id, settings, last_request)
    if wait > 0:
        await callback.answer(f"Подождите {wait:.0f} с")
        return
    await callback.answer("Беру свежую статистику…")

    try:
        card = await live_card(http, league, event_id)
    except SourceError as e:
        logging.warning("Live card failed for %s: %s", event_id, e)
        await message.answer("❌ Не удалось загрузить статистику матча. Попробуйте чуть позже.")
        return
    if card is None:
        await message.answer("Этот матч уже закончился. /live")
        return

    # Лайв не кэшируем: через минуту картина на поле другая
    last_request[callback.from_user.id] = time.monotonic()
    for chunk in split_text(f"📥 Живые данные для ИИ:\n\n{card}"):
        await message.answer(chunk)
    if await reply_with_analysis(message, card, analyst, LIVE_PROMPT):
        await message.answer(
            "Обновить разбор этого матча?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Свежая статистика + разбор", callback_data=callback.data)],
                [InlineKeyboardButton(text="🔴 Другие лайв-матчи", callback_data="live")],
            ]),
        )


# ---------- Радар лайв-аномалий ----------

async def show_radar(message: Message, http: aiohttp.ClientSession) -> None:
    try:
        anomalies = await radar_anomalies(http)
    except SourceError as e:
        logging.warning("Radar failed: %s", e)
        await message.answer("❌ Не удалось загрузить радар аномалий. Попробуйте чуть позже.")
        return

    if not anomalies:
        await message.answer(
            "⚡ Прямо сейчас нет экстремальных аномалий в лайве (все матчи идут в спокойном темпе).\n"
            "Посмотрите общий список идущих матчей или соберите экспресс дня:",
            reply_markup=main_menu(),
        )
        return

    rows = []
    for a in anomalies[:8]:
        btn_text = f"{a.badge.split()[0]} {a.clock} {a.home_score}:{a.away_score} {a.home} — {a.away}"
        rows.append([InlineKeyboardButton(text=btn_text[:55], callback_data=f"rd:{a.league}:{a.event_id}")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить радар", callback_data="radar")])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="back_main")])

    text_lines = [
        "⚡ <b>РАДАР ЛАЙВ-АНОМАЛИЙ И НАЗРЕВАЮЩИХ ГОЛОВ</b>\n",
        f"Обнаружено горячих матчей: {len(anomalies)}. Выберите матч для мгновенного разбора точки входа:\n",
    ]
    for i, a in enumerate(anomalies[:5], 1):
        h_badge = html.escape(a.badge)
        h_clock = html.escape(a.clock)
        h_home = html.escape(a.home)
        h_away = html.escape(a.away)
        h_tag = html.escape(a.tagline)
        text_lines.append(f"<b>{i}. {h_badge}</b> ({h_clock})\n{h_home} {a.home_score}:{a.away_score} {h_away}\n👉 <i>{h_tag}</i>\n")

    await message.answer("\n".join(text_lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(Command("radar"))
async def on_radar_command(message: Message, http: aiohttp.ClientSession) -> None:
    await show_radar(message, http)


@router.callback_query(F.data == "radar")
async def on_radar_button(callback: CallbackQuery, http: aiohttp.ClientSession) -> None:
    await callback.answer("Сканирую аномалии…")
    await show_radar(callback.message, http)


@router.callback_query(F.data.startswith("rd:"))
async def on_radar_match(
    callback: CallbackQuery,
    http: aiohttp.ClientSession,
    analyst: MatchAnalyst,
    settings: Settings,
    last_request: dict[int, float],
) -> None:
    _, league, event_id = callback.data.split(":", 2)
    message = callback.message

    wait = cooldown_left(callback.from_user.id, settings, last_request)
    if wait > 0:
        await callback.answer(f"Подождите {wait:.0f} с")
        return
    await callback.answer("Беру статистику аномалии…")

    try:
        card = await live_card(http, league, event_id)
    except SourceError as e:
        logging.warning("Radar live card failed: %s", e)
        await message.answer("❌ Не удалось загрузить статистику матча.")
        return
    if card is None:
        await message.answer("Этот матч уже закончился. /radar")
        return

    last_request[callback.from_user.id] = time.monotonic()
    for chunk in split_text(f"📥 Живые данные аномалии:\n\n{card}"):
        await message.answer(chunk)
    if await reply_with_analysis(message, card, analyst, RADAR_PROMPT):
        await message.answer(
            "Что дальше?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚡ Другие аномалии", callback_data="radar")],
                [InlineKeyboardButton(text="🎯 Собрать экспресс дня", callback_data="express")],
                [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="back_main")],
            ]),
        )


# ---------- Конструктор экспрессов ----------

def express_keyboard(current_preset: str | None = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🛡️ Надежный двойник (кэф ~2.00)", callback_data="exp:reliable")],
        [InlineKeyboardButton(text="⚖️ Оптимальный баланс (кэф ~3.50)", callback_data="exp:optimal")],
        [InlineKeyboardButton(text="🚀 Хайроллер / Риск (кэф ~6.00+)", callback_data="exp:risky")],
    ]
    if current_preset:
        rows.insert(0, [InlineKeyboardButton(text="🔄 Собрать другой вариант", callback_data=f"exp_force:{current_preset}")])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("express"))
async def on_express_command(message: Message) -> None:
    await message.answer(
        "🎯 <b>ИИ-Конструктор экспрессов дня</b>\n\n"
        "ИИ анализирует ближайшие статусные матчи из разных лиг (АПЛ, Ла Лига, Серия А, НХЛ, НБА, UFC), "
        "выбирает независимые события с перевесом над линией букмекера и рассчитывает оптимальный купон.\n\n"
        "Выберите целевой профиль риска:",
        parse_mode="HTML",
        reply_markup=express_keyboard(),
    )


@router.callback_query(F.data == "express")
async def on_express_button(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer(
        "🎯 <b>ИИ-Конструктор экспрессов дня</b>\n\n"
        "Выберите желаемый уровень риска и целевой коэффициент:",
        parse_mode="HTML",
        reply_markup=express_keyboard(),
    )


@router.callback_query(F.data.startswith("exp_force:"))
async def on_express_force(
    callback: CallbackQuery,
    http: aiohttp.ClientSession,
    analyst: MatchAnalyst,
    settings: Settings,
    last_request: dict[int, float],
    forecasts: dict[str, tuple[float, str]],
) -> None:
    preset = callback.data.split(":", 1)[1]
    forecasts.pop(f"express:{preset}", None)
    await run_express_generation(callback, http, analyst, settings, last_request, forecasts, preset)


@router.callback_query(F.data.startswith("exp:"))
async def on_express_preset(
    callback: CallbackQuery,
    http: aiohttp.ClientSession,
    analyst: MatchAnalyst,
    settings: Settings,
    last_request: dict[int, float],
    forecasts: dict[str, tuple[float, str]],
) -> None:
    preset = callback.data.split(":", 1)[1]
    await run_express_generation(callback, http, analyst, settings, last_request, forecasts, preset)


async def run_express_generation(
    callback: CallbackQuery,
    http: aiohttp.ClientSession,
    analyst: MatchAnalyst,
    settings: Settings,
    last_request: dict[int, float],
    forecasts: dict[str, tuple[float, str]],
    preset: str,
) -> None:
    message = callback.message

    cache_key = f"express:{preset}"
    cached = forecasts.get(cache_key)
    if cached and time.monotonic() - cached[0] < 180:
        await callback.answer()
        for part in split_text(cached[1]):
            await message.answer(part)
        await message.answer("Собрать другой экспресс?", reply_markup=express_keyboard(preset))
        return

    wait = cooldown_left(callback.from_user.id, settings, last_request)
    if wait > 0:
        await callback.answer(f"Подождите {wait:.0f} с", show_alert=False)
        return
    await callback.answer("Подбираю матчи для экспресса…")

    try:
        card, title = await express_matches_card(http, preset)
    except Exception as e:
        logging.warning("Express matches card failed: %s", e)
        await message.answer("❌ Не удалось собрать матчи для экспресса. Попробуйте чуть позже.")
        return

    last_request[callback.from_user.id] = time.monotonic()
    await message.answer(f"📋 <b>Матчи, отобранные для расчёта:</b>\n\n{html.escape(card)}", parse_mode="HTML")
    analysis = await reply_with_analysis(message, card, analyst, EXPRESS_PROMPT)
    if analysis:
        forecasts[cache_key] = (time.monotonic(), analysis)
        await message.answer(
            "Собрать другой экспресс?",
            reply_markup=express_keyboard(preset),
        )


@router.callback_query(F.data == "back_main")
async def on_back_main(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer("Главное меню:", reply_markup=main_menu())


# ---------- Режим 2: разбор присланных данных ----------

async def analyse_user_data(
    message: Message, text: str, user_id: int, analyst: MatchAnalyst, settings: Settings, last_request: dict[int, float]
) -> None:
    wait = cooldown_left(user_id, settings, last_request)
    if wait > 0:
        await message.answer(f"⏳ Подождите {wait:.0f} с перед следующим запросом.")
        return
    if len(text) < 5:
        await message.answer("Напишите название матча (например, «Реал — Барселона») или пришлите статистику. /help")
        return
    if len(text) > settings.max_input_chars:
        await message.answer(f"Сообщение слишком длинное (лимит {settings.max_input_chars} символов). Сократите данные.")
        return

    last_request[user_id] = time.monotonic()
    await reply_with_analysis(message, text, analyst, SYSTEM_PROMPT)


@router.message(Command("example"))
async def on_example(message: Message, analyst: MatchAnalyst, settings: Settings, last_request: dict[int, float]) -> None:
    await message.answer(f"Пример лайв-данных:\n\n{EXAMPLE}")
    await analyse_user_data(message, EXAMPLE, message.from_user.id, analyst, settings, last_request)


@router.callback_query(F.data == "demo")
async def on_demo_button(
    callback: CallbackQuery, analyst: MatchAnalyst, settings: Settings, last_request: dict[int, float]
) -> None:
    await callback.answer()
    await callback.message.answer(f"Пример лайв-данных:\n\n{EXAMPLE}")
    await analyse_user_data(callback.message, EXAMPLE, callback.from_user.id, analyst, settings, last_request)


@router.message(F.text & ~F.text.startswith("/"))
async def on_match_data(message: Message, analyst: MatchAnalyst, settings: Settings, last_request: dict[int, float]) -> None:
    user_id = message.from_user.id if message.from_user else message.chat.id
    await analyse_user_data(message, message.text.strip(), user_id, analyst, settings, last_request)


@router.message(F.photo)
async def on_photo(
    message: Message, analyst: MatchAnalyst, settings: Settings, last_request: dict[int, float], bot: Bot
) -> None:
    user_id = message.from_user.id if message.from_user else message.chat.id
    wait = cooldown_left(user_id, settings, last_request)
    if wait > 0:
        await message.answer(f"⏳ Подождите {wait:.0f} с перед следующим запросом.")
        return

    photo = message.photo[-1]
    buf = io.BytesIO()
    try:
        await bot.download(photo, destination=buf)
    except Exception as e:
        logging.warning("Failed to download photo: %s", e)
        await message.answer("❌ Не удалось загрузить фото. Попробуйте отправить ещё раз.")
        return

    b64_img = base64.b64encode(buf.getvalue()).decode("ascii")
    last_request[user_id] = time.monotonic()
    caption = message.caption or ""
    await reply_with_photo_analysis(message, b64_img, caption, analyst, SYSTEM_PROMPT)


@router.message()
async def on_other(message: Message) -> None:
    await message.answer(
        "Пришлите статистику матча текстом или отправьте скриншот линии из БК / Flashscore. /help",
        reply_markup=main_menu(),
    )


# ---------- Запуск ----------

async def setup_bot_profile(bot: Bot) -> None:
    """Меню команд и тексты, которые человек видит до нажатия Start.
    Ошибка здесь не критична — бот продолжит работу."""
    try:
        await bot.set_my_commands([
            BotCommand(command="radar", description="⚡ Радар аномалий: назревающие голы и перевес"),
            BotCommand(command="express", description="🎯 ИИ-Конструктор экспрессов дня"),
            BotCommand(command="live", description="🔴 Лайв: идея ставки по живой статистике"),
            BotCommand(command="matches", description="📅 Прогнозы на ближайшие матчи"),
            BotCommand(command="example", description="⚡ Пример лайв-статистики"),
            BotCommand(command="help", description="📖 Как пользоваться"),
            BotCommand(command="start", description="🏠 Главное меню"),
        ])
        await bot.set_my_short_description(
            "⚡ Радар аномалий, 🎯 ИИ-экспрессы и лайв-аналитика 7 видов спорта"
        )
        await bot.set_my_description(
            "⚡ Радар аномалий: находит матчи, где назревает гол или есть удаление прямо сейчас.\n\n"
            "🎯 Экспресс дня: купон из независимых событий с математическим перевесом.\n\n"
            "🔴 Лайв: идея ставки со справедливым коэффициентом по живой статистике.\n\n"
            "📅 Прогнозы на ближайшие матчи топ-лиг.\n\nНажмите Start."
        )
    except Exception:
        logging.warning("Could not update bot profile", exc_info=True)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()

    # parse_mode=None: ответ модели отправляется как обычный текст,
    # поэтому случайные символы * _ [ не ломают отправку
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=None))
    dp = Dispatcher()
    dp.include_router(router)

    # Стандартный User-Agent aiohttp: со «своим» ESPN отвечает 403
    async with aiohttp.ClientSession() as http:
        # Эти объекты aiogram сам передаст в хендлеры по именам аргументов
        dp["http"] = http
        dp["analyst"] = MatchAnalyst(settings)
        dp["settings"] = settings
        dp["last_request"] = {}
        dp["forecasts"] = {}

        runner, tunnel = await start_webapp(bot, settings, dp)
        warmer = asyncio.create_task(keep_cache_warm(http))
        try:
            await setup_bot_profile(bot)
            logging.info("Bot started, model=%s, webapp=%s", settings.llm_model, webapp_url)
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot)
        finally:
            warmer.cancel()
            if tunnel:
                tunnel.kill()
            if runner:
                await runner.cleanup()


async def start_webapp(bot: Bot, settings: Settings, dp: Dispatcher):
    """Поднимает сервер Mini App и туннель, ставит кнопку «Приложение» в меню чата.
    Если что-то не получилось — бот работает дальше без Mini App."""
    global webapp_url
    if not settings.webapp_port:
        return None, None

    app = create_app(settings, dp["http"], dp["analyst"], dp["last_request"], dp["forecasts"])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", settings.webapp_port).start()

    tunnel = None
    try:
        if settings.webapp_url:
            webapp_url = settings.webapp_url
        else:
            tunnel, webapp_url = await start_tunnel(settings.webapp_port)
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Приложение", web_app=WebAppInfo(url=webapp_url))
        )
    except Exception:
        logging.warning("Mini App is unavailable, bot continues without it", exc_info=True)
        webapp_url = None
    return runner, tunnel


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped")
