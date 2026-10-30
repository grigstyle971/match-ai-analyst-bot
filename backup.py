"""Резервный источник статистики — football-data.org (бесплатный ключ, текущий сезон).

Включается, только когда основной источник (ESPN) не отвечает. Покрывает топ-футбол:
расписание, турнирную таблицу и последние матчи команд. Коэффициентов и лайва в
бесплатном тарифе нет — карточка матча честно об этом говорит.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import aiohttp

log = logging.getLogger(__name__)

API = "https://api.football-data.org/v4"
TTL = 900  # лимит бесплатного тарифа — 10 запросов в минуту, поэтому кэшируем надолго

# Ключ лиги ESPN → код турнира football-data.org
COMPETITIONS = {
    "soccer/eng.1": "PL", "soccer/esp.1": "PD", "soccer/ita.1": "SA", "soccer/ger.1": "BL1",
    "soccer/fra.1": "FL1", "soccer/uefa.champions": "CL", "soccer/eng.2": "ELC",
    "soccer/ned.1": "DED", "soccer/por.1": "PPL", "soccer/bra.1": "BSA",
}

_cache: dict[str, tuple[float, dict]] = {}
_limit = asyncio.Semaphore(2)


def token() -> str:
    return os.getenv("FOOTBALL_DATA_TOKEN", "").strip()


def available(league: str) -> bool:
    return bool(token()) and league in COMPETITIONS


async def _get(session: aiohttp.ClientSession, path: str) -> dict:
    url = f"{API}{path}"
    cached = _cache.get(url)
    if cached and time.time() - cached[0] < TTL:
        return cached[1]
    try:
        async with _limit, session.get(url, headers={"X-Auth-Token": token()},
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            data = await resp.json()
    except Exception as e:
        if cached:
            return cached[1]
        raise RuntimeError(f"football-data.org: {e}") from e
    _cache[url] = (time.time(), data)
    return data


def _date(value: str) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


async def upcoming(session: aiohttp.ClientSession, league: str, league_name: str, limit: int = 12) -> list:
    from sources import Fixture  # локальный импорт: sources сам импортирует этот модуль

    today = datetime.now(timezone.utc).date()
    data = await _get(session, f"/competitions/{COMPETITIONS[league]}/matches"
                               f"?dateFrom={today}&dateTo={today + timedelta(days=7)}")
    fixtures = [
        Fixture(
            event_id=f"fd{m['id']}",  # префикс fd — признак резервного источника
            league=league,
            league_name=league_name,
            kickoff=_date(m["utcDate"]),
            home=m["homeTeam"].get("name", "?"),
            away=m["awayTeam"].get("name", "?"),
            state="pre",
            note=f"fd:{m['homeTeam'].get('id')}:{m['awayTeam'].get('id')}",
        )
        for m in data.get("matches", [])
        if m.get("status") in ("SCHEDULED", "TIMED")
    ]
    return sorted(fixtures, key=lambda f: f.kickoff)[:limit]


async def match_card(session: aiohttp.ClientSession, fixture) -> str:
    _, home_id, away_id = fixture.note.split(":")
    standings, home_games, away_games = await asyncio.gather(
        _get(session, f"/competitions/{COMPETITIONS[fixture.league]}/standings"),
        _get(session, f"/teams/{home_id}/matches?status=FINISHED&limit=5"),
        _get(session, f"/teams/{away_id}/matches?status=FINISHED&limit=5"),
        return_exceptions=True,
    )

    table = {}
    if isinstance(standings, dict):
        for row in (standings.get("standings") or [{}])[0].get("table", []):
            table[str(row["team"]["id"])] = row

    lines = [
        f"Матч: {fixture.home} (дома) — {fixture.away} (в гостях)",
        "Вид спорта: футбол",
        f"Турнир: {fixture.league_name}",
        f"Начало: {fixture.kickoff_msk} МСК",
        "",
    ]
    for team, team_id, games in ((fixture.home, home_id, home_games), (fixture.away, away_id, away_games)):
        lines.append(f"{team}:")
        if row := table.get(team_id):
            lines.append(
                f"  Таблица: {row['position']}-е место, {row['points']} очк. за {row['playedGames']} игр, "
                f"В-Н-П {row['won']}-{row['draw']}-{row['lost']}, разница {row['goalDifference']:+d}"
            )
        if isinstance(games, dict) and games.get("matches"):
            lines.append("  Последние матчи, счёт забили:пропустили (W — победа, D — ничья, L — поражение):")
            for g in sorted(games["matches"], key=lambda g: g["utcDate"])[-5:]:
                at_home = str(g["homeTeam"]["id"]) == team_id
                ft = g.get("score", {}).get("fullTime", {})
                scored, conceded = (ft.get("home"), ft.get("away")) if at_home else (ft.get("away"), ft.get("home"))
                result = "?" if scored is None else ("W" if scored > conceded else "D" if scored == conceded else "L")
                opponent = (g["awayTeam"] if at_home else g["homeTeam"]).get("name", "?")
                lines.append(f"    {g['utcDate'][:10]} {'дома' if at_home else 'в гостях'} vs {opponent}: "
                             f"{scored}:{conceded} ({result})")
        lines.append("")
    lines.append("Коэффициенты: в резервном источнике нет — оцени вероятности только по форме и таблице.")
    return "\n".join(lines)
