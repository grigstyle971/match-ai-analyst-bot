"""Сервер Telegram Mini App: отдаёт страницу приложения и JSON-API.

Все запросы к API подписаны Telegram (заголовок X-Telegram-Init-Data) — без валидной
подписи сервер не отвечает, поэтому посторонний не сможет тратить лимит ИИ через туннель.
"""

import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from urllib.parse import parse_qsl

import aiohttp
from aiohttp import web

from config import Settings
from llm import AnalysisError, MatchAnalyst
from prompts import EXPRESS_PROMPT, LIVE_PROMPT, PREMATCH_PROMPT, RADAR_PROMPT, SYSTEM_PROMPT
from sources import (
    SPORTS, SourceError, express_matches_card, find_fixture, live_card,
    live_matches, match_card, radar_anomalies, upcoming_fixtures
)

STATIC = Path(__file__).with_name("static")
INIT_DATA_MAX_AGE = 24 * 3600
FORECAST_TTL = 1800

log = logging.getLogger(__name__)


def verify_init_data(init_data: str, bot_token: str) -> dict | None:
    """Проверка подписи Telegram WebApp initData. Возвращает данные пользователя или None.
    Алгоритм из документации Telegram: HMAC-SHA256 с ключом HMAC("WebAppData", token)."""
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received_hash = pairs.pop("hash", "")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None
    if time.time() - int(pairs.get("auth_date", 0)) > INIT_DATA_MAX_AGE:
        return None
    return json.loads(pairs.get("user", "{}"))


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    user = verify_init_data(request.headers.get("X-Telegram-Init-Data", ""), request.app["settings"].bot_token)
    if user is None:
        return web.json_response({"error": "Откройте приложение из Telegram"}, status=401)
    request["user_id"] = user.get("id", 0)
    return await handler(request)


def _fixture_json(f) -> dict:
    return {
        "id": f.event_id, "league": f.league, "league_name": f.league_name, "sport": f.sport,
        "home": f.home, "away": f.away, "kickoff": f.kickoff_msk, "clock": f.clock,
        "home_score": f.home_score, "away_score": f.away_score, "note": f.note,
    }


def _cooldown(request: web.Request) -> float:
    settings: Settings = request.app["settings"]
    last = request.app["last_request"].get(request["user_id"], 0.0)
    return max(0.0, settings.cooldown_seconds - (time.monotonic() - last))


async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def api_sports(_: web.Request) -> web.Response:
    return web.json_response([
        {"code": code, "label": label, "leagues": [{"key": k, "label": v} for k, v in leagues.items()]}
        for code, (label, leagues) in SPORTS.items()
    ])


async def api_fixtures(request: web.Request) -> web.Response:
    league = request.query.get("league", "")
    try:
        fixtures = await upcoming_fixtures(request.app["http"], league)
    except SourceError:
        return web.json_response({"error": "Не удалось загрузить расписание"}, status=502)
    return web.json_response([_fixture_json(f) for f in fixtures])


async def api_live(request: web.Request) -> web.Response:
    try:
        matches = await live_matches(request.app["http"])
    except SourceError:
        return web.json_response({"error": "Не удалось загрузить лайв"}, status=502)
    return web.json_response([_fixture_json(m) for m in matches])


async def api_radar(request: web.Request) -> web.Response:
    try:
        anomalies = await radar_anomalies(request.app["http"])
    except SourceError:
        return web.json_response({"error": "Не удалось загрузить радар аномалий"}, status=502)
    return web.json_response([
        {
            "event_id": a.event_id,
            "league": a.league,
            "league_name": a.league_name,
            "home": a.home,
            "away": a.away,
            "clock": a.clock,
            "home_score": a.home_score,
            "away_score": a.away_score,
            "sport": a.sport,
            "kind": a.kind,
            "badge": a.badge,
            "tagline": a.tagline,
            "priority": a.priority,
        }
        for a in anomalies
    ])


async def api_express(request: web.Request) -> web.Response:
    body = await request.json()
    preset = str(body.get("preset", "optimal")).strip()
    if preset not in ("reliable", "optimal", "risky"):
        preset = "optimal"
    http: aiohttp.ClientSession = request.app["http"]
    forecasts: dict = request.app["forecasts"]
    analyst: MatchAnalyst = request.app["analyst"]

    cache_key = f"express:{preset}"
    if not body.get("force"):
        if (hit := forecasts.get(cache_key)) and time.monotonic() - hit[0] < 900:
            return web.json_response({"card": hit[2], "analysis": hit[1], "slip_title": hit[3]})

    if (wait := _cooldown(request)) > 0:
        return web.json_response({"error": f"Подождите {wait:.0f} с"}, status=429)

    try:
        card, slip_title = await express_matches_card(http, preset)
    except Exception as e:
        log.warning("Express card error: %s", e)
        return web.json_response({"error": "Не удалось собрать матчи для экспресса"}, status=502)

    request.app["last_request"][request["user_id"]] = time.monotonic()
    try:
        analysis = await analyst.analyze(card, EXPRESS_PROMPT)
    except AnalysisError as e:
        return web.json_response({"error": str(e)}, status=502)

    forecasts[cache_key] = (time.monotonic(), analysis, card, slip_title)
    return web.json_response({"card": card, "analysis": analysis, "slip_title": slip_title})


async def api_analyze(request: web.Request) -> web.Response:
    """mode: pre — прогноз до матча, live — разбор идущего, text — свои данные."""
    body = await request.json()
    mode = body.get("mode")
    http: aiohttp.ClientSession = request.app["http"]
    forecasts: dict = request.app["forecasts"]
    analyst: MatchAnalyst = request.app["analyst"]
    settings: Settings = request.app["settings"]

    cache_key = f"{body.get('league')}:{body.get('id')}"
    if mode == "pre" and (hit := forecasts.get(cache_key)) and time.monotonic() - hit[0] < FORECAST_TTL:
        return web.json_response({"card": hit[2], "analysis": hit[1]})

    if (wait := _cooldown(request)) > 0:
        return web.json_response({"error": f"Подождите {wait:.0f} с"}, status=429)

    try:
        if mode == "pre":
            fixture = await find_fixture(http, body["league"], body["id"])
            if fixture is None:
                return web.json_response({"error": "Матч уже начался или пропал из расписания"}, status=404)
            card, prompt = await match_card(http, fixture), PREMATCH_PROMPT
        elif mode == "live":
            card, prompt = await live_card(http, body["league"], body["id"]), LIVE_PROMPT
            if card is None:
                return web.json_response({"error": "Матч уже закончился"}, status=404)
        elif mode == "radar":
            tagline = body.get("tagline", "")
            badge = body.get("badge", "")
            base_card = await live_card(http, body["league"], body["id"])
            if base_card is None:
                return web.json_response({"error": "Матч уже закончился"}, status=404)
            card = f"ВНИМАНИЕ: АЛГОРИТМИЧЕСКИЙ РАДАР ЗАФИКСИРОВАЛ АНОМАЛИЮ: {badge}\n{tagline}\n\n{base_card}"
            prompt = RADAR_PROMPT
        elif mode == "text":
            card, prompt = str(body.get("text", "")).strip(), SYSTEM_PROMPT
            if not 5 <= len(card) <= settings.max_input_chars:
                return web.json_response({"error": "Напишите от 5 до 6000 символов (например, названия команд)"}, status=400)
        elif mode == "image":
            b64_img = body.get("image", "")
            caption = body.get("caption", "")
            if not b64_img:
                return web.json_response({"error": "Изображение не передано"}, status=400)
            if "," in b64_img:
                b64_img = b64_img.split(",", 1)[1]
            card = f"[Скриншот матча / линии БК]{': ' + caption if caption else ''}"
            request.app["last_request"][request["user_id"]] = time.monotonic()
            try:
                analysis = await analyst.analyze_image(b64_img, caption=caption, system_prompt=SYSTEM_PROMPT)
            except AnalysisError as e:
                return web.json_response({"error": str(e)}, status=502)
            return web.json_response({"card": card, "analysis": analysis})
        else:
            return web.json_response({"error": "Неизвестный режим"}, status=400)
    except (SourceError, KeyError) as e:
        log.warning("Webapp data error: %s", e)
        return web.json_response({"error": "Не удалось загрузить данные матча"}, status=502)

    request.app["last_request"][request["user_id"]] = time.monotonic()
    try:
        analysis = await analyst.analyze(card, prompt)
    except AnalysisError as e:
        return web.json_response({"error": str(e)}, status=502)

    if mode == "pre":
        forecasts[cache_key] = (time.monotonic(), analysis, card)
    return web.json_response({"card": card, "analysis": analysis})


def create_app(settings: Settings, http: aiohttp.ClientSession, analyst: MatchAnalyst,
               last_request: dict, forecasts: dict) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    # Общие с ботом объекты: кэш прогнозов и антиспам работают для обоих интерфейсов
    app.update(settings=settings, http=http, analyst=analyst, last_request=last_request, forecasts=forecasts)
    app.router.add_get("/", index)
    app.router.add_get("/api/sports", api_sports)
    app.router.add_get("/api/fixtures", api_fixtures)
    app.router.add_get("/api/live", api_live)
    app.router.add_get("/api/radar", api_radar)
    app.router.add_post("/api/express", api_express)
    app.router.add_post("/api/analyze", api_analyze)
    return app
