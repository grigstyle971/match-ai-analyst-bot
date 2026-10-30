"""Загрузка матчей, таблицы, формы, коэффициентов и лайв-статистики по всем видам спорта.

Источник — публичный JSON-API ESPN (без ключа). Из него собирается текстовая
«карточка матча», которую бот отдаёт языковой модели.

Ключ лиги везде имеет вид "вид_спорта/лига", например "soccer/eng.1" или "basketball/nba".
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

import aiohttp

import backup

log = logging.getLogger(__name__)

BASE = "https://site.api.espn.com/apis/site/v2/sports"
MSK = timezone(timedelta(hours=3))

DAYS_AHEAD = 7
CACHE_TTL = 600  # секунд: расписание и коэффициенты не нужно дёргать на каждый клик
LIVE_TTL = 45  # секунд: лайв обновляем часто

# Вид спорта → (кнопка, {ключ лиги: кнопка})
SPORTS: dict[str, tuple[str, dict[str, str]]] = {
    "soccer": ("⚽ Футбол", {
        "soccer/eng.1": "🏴 АПЛ", "soccer/esp.1": "🇪🇸 Ла Лига", "soccer/ita.1": "🇮🇹 Серия А",
        "soccer/ger.1": "🇩🇪 Бундеслига", "soccer/fra.1": "🇫🇷 Лига 1", "soccer/rus.1": "🇷🇺 РПЛ",
        "soccer/uefa.champions": "⭐ Лига чемпионов", "soccer/uefa.europa": "🟠 Лига Европы",
        "soccer/uefa.europa.conf": "🟢 Лига конференций", "soccer/eng.2": "🏴 Чемпионшип",
        "soccer/ned.1": "🇳🇱 Эредивизи", "soccer/por.1": "🇵🇹 Португалия", "soccer/tur.1": "🇹🇷 Турция",
        "soccer/bel.1": "🇧🇪 Бельгия", "soccer/sco.1": "🏴 Шотландия", "soccer/gre.1": "🇬🇷 Греция",
        "soccer/esp.2": "🇪🇸 Ла Лига 2", "soccer/ita.2": "🇮🇹 Серия B", "soccer/ger.2": "🇩🇪 Бундеслига 2",
        "soccer/fra.2": "🇫🇷 Лига 2", "soccer/aut.1": "🇦🇹 Австрия", "soccer/sui.1": "🇨🇭 Швейцария",
        "soccer/den.1": "🇩🇰 Дания", "soccer/nor.1": "🇳🇴 Норвегия", "soccer/swe.1": "🇸🇪 Швеция",
        "soccer/bra.1": "🇧🇷 Бразилия", "soccer/arg.1": "🇦🇷 Аргентина", "soccer/usa.1": "🇺🇸 MLS",
        "soccer/mex.1": "🇲🇽 Мексика", "soccer/ksa.1": "🇸🇦 Саудовская Аравия", "soccer/jpn.1": "🇯🇵 Япония",
        "soccer/chn.1": "🇨🇳 Китай", "soccer/aus.1": "🇦🇺 Австралия",
        "soccer/conmebol.libertadores": "🏆 Либертадорес", "soccer/afc.champions": "🏆 Азиатская ЛЧ",
        "soccer/eng.fa": "🏆 Кубок Англии", "soccer/esp.copa_del_rey": "🏆 Кубок Испании",
        "soccer/uefa.nations": "🌍 Лига наций",
    }),
    "basketball": ("🏀 Баскетбол", {
        "basketball/nba": "🏀 НБА", "basketball/wnba": "🏀 WNBA",
        "basketball/mens-college-basketball": "🎓 NCAA",
    }),
    "hockey": ("🏒 Хоккей", {"hockey/nhl": "🏒 НХЛ"}),
    "tennis": ("🎾 Теннис", {"tennis/atp": "🎾 ATP", "tennis/wta": "🎾 WTA"}),
    "football": ("🏈 Амер. футбол", {"football/nfl": "🏈 NFL", "football/college-football": "🎓 NCAA"}),
    "baseball": ("⚾ Бейсбол", {"baseball/mlb": "⚾ MLB"}),
    "mma": ("🥊 MMA", {"mma/ufc": "🥊 UFC"}),
}

LEAGUES: dict[str, str] = {key: name for _, leagues in SPORTS.values() for key, name in leagues.items()}

# Для лайва футбол берём одним запросом по всем лигам мира ("soccer/all"), остальное — по лигам
LIVE_SOURCES = ["soccer/all"] + [key for key in LEAGUES if not key.startswith("soccer/")]

SPORT_NAMES = {
    "soccer": "футбол", "basketball": "баскетбол", "hockey": "хоккей", "tennis": "теннис",
    "football": "американский футбол", "baseball": "бейсбол", "mma": "MMA",
}

STAT_LABELS = {
    # футбол
    "possessionPct": "Владение, %", "totalShots": "Удары", "shotsOnTarget": "Удары в створ",
    "wonCorners": "Угловые", "foulsCommitted": "Фолы", "yellowCards": "Жёлтые", "redCards": "Красные",
    "offsides": "Офсайды", "saves": "Сейвы",
    # баскетбол
    "fieldGoalPct": "Процент с игры", "threePointFieldGoalPct": "Процент трёхочковых",
    "freeThrowPct": "Процент штрафных", "totalRebounds": "Подборы", "assists": "Передачи",
    "turnovers": "Потери", "steals": "Перехваты", "blocks": "Блок-шоты",
    # хоккей
    "shotsTotal": "Броски", "powerPlayGoals": "Голы в большинстве", "faceoffsWon": "Выигранные вбрасывания",
    "hits": "Силовые приёмы", "takeaways": "Отборы", "giveaways": "Потери",
}

EVENT_KINDS = {
    "Goal": "⚽ Гол", "Penalty - Scored": "⚽ Гол (пен.)", "Own Goal": "⚽ Автогол",
    "Yellow Card": "🟨 ЖК", "Red Card": "🟥 КК",
}

WOMEN_KEYWORDS = (
    "women", "feminin", "féminin", "femenil", "femenino", "frauen",
    "damer", "damallsvenskan", "wchampions", "w-league", "wsl", "nwsl",
    ".w.", "w.europa", "женск",
)

YOUTH_KEYWORDS = (
    "u19", "u-19", "u20", "u-20", "u21", "u-21", "u23", "u-23",
    "primavera", "youth league", "юнош", "молод",
)

CLEAN_LEAGUES: dict[str, str] = {
    "uefa.wchampions": "Лига чемпионов (жен.)",
    "uefa.w.europa": "Кубок Европы (жен.)",
    "ita.1": "Серия А",
    "ita.2": "Серия B",
    "ger.dfb_pokal": "Кубок Германии",
    "ger.1": "Бундеслига",
    "ger.2": "Бундеслига 2",
    "ned.cup": "Кубок Нидерландов",
    "ned.1": "Эредивизи",
    "afc.cup": "Лига чемпионов АФК 2",
    "afc.champions": "Лига чемпионов АФК",
    "eng.league_cup": "Кубок английской лиги",
    "eng.fa": "Кубок Англии",
    "eng.1": "АПЛ",
    "eng.2": "Чемпионшип",
    "esp.1": "Ла Лига",
    "esp.2": "Ла Лига 2",
    "esp.copa_del_rey": "Кубок Испании",
    "fra.1": "Лига 1",
    "fra.2": "Лига 2",
    "fra.coupe_de_france": "Кубок Франции",
    "rus.1": "РПЛ",
    "rus.cup": "Кубок России",
    "uefa.champions": "Лига чемпионов",
    "uefa.europa": "Лига Европы",
    "uefa.europa.conf": "Лига конференций",
}


class SourceError(Exception):
    """Источник данных недоступен или вернул что-то неожиданное."""


@dataclass(frozen=True)
class Fixture:
    event_id: str
    league: str  # ключ лиги; для лайв-футбола — "soccer/all"
    league_name: str
    kickoff: datetime
    home: str
    away: str
    state: str  # pre / in / post
    clock: str = ""
    home_score: str = ""
    away_score: str = ""
    note: str = ""  # теннис: турнир и раунд

    @property
    def sport(self) -> str:
        return self.league.split("/", 1)[0]

    @property
    def title(self) -> str:
        return f"{self.home} — {self.away}"

    @property
    def live_title(self) -> str:
        if self.sport == "tennis":  # счёт по сетам: «Dart 6 3 4 — 4 6 1 Tjen»
            return f"{self.home} {self.home_score} — {self.away_score} {self.away}"
        return f"{self.clock} {self.home} {self.home_score}:{self.away_score} {self.away}".strip()

    @property
    def kickoff_msk(self) -> str:
        return self.kickoff.astimezone(MSK).strftime("%d.%m %H:%M")


# ---------- Сеть и резерв ----------
#
# 1. Кэш: каждый удачный ответ запоминается (url → время, данные) и пишется на диск,
#    поэтому переживает перезапуск бота.
# 2. Если ESPN не ответил — отдаём последние удачные данные, даже устаревшие.
# 3. На 403/429 (ESPN режет частые запросы) на BLOCK_PAUSE секунд вообще перестаём
#    его дёргать и работаем из кэша — так ограничение снимается быстрее.

CACHE_FILE = Path(__file__).with_name("cache_store.json")
BLOCK_PAUSE = 180

_limit = asyncio.Semaphore(4)  # не больше N одновременных запросов к ESPN
_blocked_until = 0.0
_last_save = 0.0


def _load_cache() -> dict[str, tuple[float, object]]:
    try:
        if CACHE_FILE.is_dir():
            import shutil
            shutil.rmtree(CACHE_FILE)
            return {}
        return {url: (ts, data) for url, (ts, data) in json.loads(CACHE_FILE.read_text("utf-8")).items()}
    except (OSError, ValueError):
        return {}


_cache: dict[str, tuple[float, object]] = _load_cache()


def save_cache(force: bool = False) -> None:
    global _last_save
    if not force and time.time() - _last_save < 30:
        return
    try:
        if CACHE_FILE.is_dir():
            import shutil
            shutil.rmtree(CACHE_FILE)
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_cache, ensure_ascii=False), "utf-8")
        tmp.replace(CACHE_FILE)
        _last_save = time.time()
    except OSError as e:
        log.warning("Could not save cache: %s", e)


async def _get_json(session: aiohttp.ClientSession, url: str, ttl: float = CACHE_TTL) -> dict:
    global _blocked_until
    cached = _cache.get(url)
    if cached and time.time() - cached[0] < ttl:
        return cached[1]  # type: ignore[return-value]
    try:
        if time.time() < _blocked_until:
            raise SourceError("ESPN временно ограничил запросы, работаем из кэша")
        async with _limit, session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status in (403, 429):
                _blocked_until = time.time() + BLOCK_PAUSE
            if resp.status != 200:
                raise SourceError(f"HTTP {resp.status}")
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, SourceError, ValueError) as e:
        if cached:
            log.warning("Source failed (%s), serving cached data %.0fs old: %s", e, time.time() - cached[0], url)
            return cached[1]  # type: ignore[return-value]
        raise e if isinstance(e, SourceError) else SourceError(str(e) or type(e).__name__) from e
    _cache[url] = (time.time(), data)
    save_cache()
    return data


# Лиги, которые прогреваются заранее: их чаще всего откроют на демонстрации
WARM_LEAGUES = [
    "soccer/eng.1", "soccer/esp.1", "soccer/ita.1", "soccer/ger.1", "soccer/fra.1", "soccer/rus.1",
    "soccer/uefa.champions", "basketball/nba", "hockey/nhl", "tennis/atp", "mma/ufc",
]


async def keep_cache_warm(session: aiohttp.ClientSession, every: float = CACHE_TTL) -> None:
    """Фоном держит в памяти свежие расписания и лайв. Если ESPN вдруг перестанет
    отвечать, у бота останутся данные последнего удачного обновления."""
    while True:
        started = time.monotonic()
        for league in WARM_LEAGUES:  # по очереди, чтобы не создавать всплеск запросов
            try:
                await upcoming_fixtures(session, league)
            except SourceError as e:
                log.warning("Warm-up failed for %s: %s", league, e)
        try:
            await live_matches(session)
        except SourceError as e:
            log.warning("Warm-up failed for live: %s", e)
        save_cache(force=True)
        log.info("Cache warmed in %.1fs", time.monotonic() - started)
        await asyncio.sleep(every)


def _parse_date(value: str) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        pass
    for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def _name(competitor: dict) -> str:
    for key in ("team", "athlete", "roster"):
        if name := (competitor.get(key) or {}).get("displayName"):
            return name
    # Парный теннис: игроки лежат списком
    athletes = [a.get("displayName") or a.get("shortName") for a in competitor.get("athletes", [])]
    return " / ".join(a for a in athletes if a) or "?"


def _score(competitor: dict, tennis: bool) -> str:
    if tennis:  # в теннисе «счёт» — это сеты: 6-4 3-6 2
        return " ".join(str(int(s.get("value", 0))) for s in competitor.get("linescores", [])) or "0"
    return str(competitor.get("score", "0"))


def _record_summary(competitor: dict) -> str:
    for rec in competitor.get("records", []):
        if s := rec.get("summary"):
            return s
    return ""


def _parse_board(data: dict, league: str) -> list[tuple[Fixture, dict]]:
    """Все матчи из ответа scoreboard: (Fixture, сырой объект competition).
    В теннисе один турнир содержит много матчей (groupings → competitions).
    В MMA турнир UFC содержит кард из многих боёв (competitions)."""
    default_league_name = LEAGUES.get(league) or (data.get("leagues") or [{}])[0].get("name", league)
    out: list[tuple[Fixture, dict]] = []
    is_mma = league.startswith("mma")

    for event in data.get("events", []):
        if event.get("groupings"):
            items = [(c, f"{event.get('name', '')}, {c.get('round', {}).get('displayName', '')}".strip(", "))
                     for g in event["groupings"] for c in g.get("competitions", [])]
            is_sub = True
            tennis = True
        elif is_mma:
            event_name = event.get("name", "")
            items = []
            for c in event.get("competitions", []):
                weight = c.get("type", {}).get("abbreviation", "")
                note_parts = [p for p in (event_name, weight) if p]
                items.append((c, " · ".join(note_parts)))
            is_sub = True
            tennis = False
        else:
            items = [(event["competitions"][0], "")] if event.get("competitions") else []
            is_sub = False
            tennis = False

        for comp, note in items:
            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                continue
            sides = {c.get("homeAway"): c for c in competitors}
            home, away = sides.get("home", competitors[0]), sides.get("away", competitors[1])
            status = comp.get("status") or event.get("status") or {}

            alt = comp.get("altGameNote") or ""
            if not alt:
                for n in comp.get("notes", []):
                    if n.get("headline"):
                        alt = n.get("headline")
                        break

            league_slug = ""
            for l in event.get("links", []):
                m = re.search(r"/league/([a-z0-9_.-]+)", l.get("href", ""))
                if m:
                    league_slug = m.group(1)
                    break

            if default_league_name and default_league_name != "soccer/all":
                league_name = default_league_name
            else:
                league_name = CLEAN_LEAGUES.get(league_slug) or (alt.split(",")[0].strip() if alt else "") or league_slug or "Футбол"

            check_text = f"{league_name} {alt} {league_slug} {event.get('name', '')}".lower()
            is_women = any(k in check_text for k in WOMEN_KEYWORDS)
            is_youth = any(k in check_text for k in YOUTH_KEYWORDS)

            home_name = _name(home)
            away_name = _name(away)
            if home_name.strip().upper() in ("TBD", "?", "") and away_name.strip().upper() in ("TBD", "?", ""):
                continue
            if home_name.strip() == "TBD" and away_name.strip() == "TBD":
                continue
            if is_mma:
                home_rec = _record_summary(home)
                away_rec = _record_summary(away)
                if home_rec:
                    home_name = f"{home_name} ({home_rec})"
                if away_rec:
                    away_name = f"{away_name} ({away_rec})"
            elif is_women:
                if not any(x in home_name.lower() for x in ("(ж)", "women", "fem", "frauen", "dames")):
                    home_name += " (Ж)"
                if not any(x in away_name.lower() for x in ("(ж)", "women", "fem", "frauen", "dames")):
                    away_name += " (Ж)"
            elif is_youth:
                if not any(x in home_name.lower() for x in ("(мол.)", "u19", "u20", "u21", "u23", "primavera")):
                    home_name += " (мол.)"
                if not any(x in away_name.lower() for x in ("(мол.)", "u19", "u20", "u21", "u23", "primavera")):
                    away_name += " (мол.)"

            event_id = str(comp.get("id")) if is_sub else str(event["id"])

            out.append((
                Fixture(
                    event_id=event_id,
                    league=league,
                    league_name=league_name,
                    kickoff=_parse_date(comp.get("date") or event.get("date", "")),
                    home=home_name,
                    away=away_name,
                    state=status.get("type", {}).get("state", ""),
                    clock=status.get("displayClock", "") if not tennis else status.get("type", {}).get("shortDetail", ""),
                    home_score=_score(home, tennis),
                    away_score=_score(away, tennis),
                    note=note,
                ),
                comp,
            ))
    return out


# ---------- Предматч ----------

async def upcoming_fixtures(session: aiohttp.ClientSession, league: str, limit: int = 12) -> list[Fixture]:
    """Матчи лиги, которые ещё не начались, на ближайшие DAYS_AHEAD дней.

    Сначала один запрос «текущего тура» (обычно его хватает). Если матчей мало —
    добираем по дням, по одному запросу, чтобы не упереться в лимит ESPN."""
    today = datetime.now(timezone.utc).date()
    max_limit = 30 if league.startswith("mma") else limit
    urls = [f"{BASE}/{league}/scoreboard"] + [
        f"{BASE}/{league}/scoreboard?dates={(today + timedelta(days=i)).strftime('%Y%m%d')}"
        for i in range(DAYS_AHEAD)
    ]

    fixtures: dict[str, Fixture] = {}
    failures = 0
    for url in urls:
        try:
            data = await _get_json(session, url)
        except SourceError as e:
            log.warning("Scoreboard request failed for %s: %s", league, e)
            failures += 1
            if failures >= 2:  # источник лежит — не тратим время на остальные дни
                break
            continue
        for fixture, _ in _parse_board(data, league):
            if fixture.state == "pre":
                fixtures[fixture.event_id] = fixture
        if len(fixtures) >= max_limit:
            break

    if not fixtures and failures:
        # Запасной источник: football-data.org (топ-футбол)
        if backup.available(league):
            try:
                log.warning("ESPN unavailable for %s, using football-data.org", league)
                return await backup.upcoming(session, league, LEAGUES.get(league, league), max_limit)
            except Exception as e:
                log.warning("Backup source failed for %s: %s", league, e)
        raise SourceError("расписание недоступно")
    return sorted(fixtures.values(), key=lambda f: f.kickoff)[:max_limit]


def _american_to_decimal(value) -> str | None:
    """Американский коэффициент (+310 / -130) → десятичный (4.10 / 1.77)."""
    try:
        v = float(str(value).replace("+", ""))
    except (TypeError, ValueError):
        return None
    if v == 0:
        return None
    return f"{1 + v / 100:.2f}" if v > 0 else f"{1 + 100 / abs(v):.2f}"


def _odds_lines(summary: dict, title: str = "открытие → сейчас") -> list[str]:
    pick = (summary.get("pickcenter") or [{}])[0]
    if not pick:
        return ["Коэффициенты: нет данных"]

    lines = [f"Коэффициенты ({pick.get('provider', {}).get('name', 'букмекер')}), {title}:"]
    ml = pick.get("moneyline") or {}
    for side, label in (("home", "П1"), ("draw", "X"), ("away", "П2")):
        node = ml.get(side) or {}
        opened = _american_to_decimal((node.get("open") or {}).get("odds"))
        closed = _american_to_decimal((node.get("close") or {}).get("odds"))
        if closed:
            lines.append(f"  {label}: {opened or '?'} → {closed}")

    total = pick.get("total") or {}
    for side, label in (("over", "ТБ"), ("under", "ТМ")):
        node = total.get(side) or {}
        o, c = node.get("open") or {}, node.get("close") or {}
        if c.get("line"):
            o_line = str(o.get("line", "?")).lstrip("ouOU") or "?"
            c_line = str(c.get("line", "?")).lstrip("ouOU") or "?"
            lines.append(
                f"  {label}: {o_line} @ {_american_to_decimal(o.get('odds')) or '?'}"
                f" → {c_line} @ {_american_to_decimal(c.get('odds')) or '?'}"
            )
    # В футболе details — это денежная линия фаворита, а не фора; показываем только для очковых видов
    if pick.get("details") and pick.get("drawOdds") is None:
        lines.append(f"  Фора фаворита: {pick['details']}")
    return lines


def _standings_line(summary: dict, team: str) -> str | None:
    for group in (summary.get("standings") or {}).get("groups", []):
        for entry in group.get("standings", {}).get("entries", []):
            if entry.get("team") == team:
                s = {x["name"]: x.get("displayValue") for x in entry.get("stats", [])}
                return (
                    f"{s.get('rank', '?')}-е место, {s.get('points', '?')} очк. за {s.get('gamesPlayed', '?')} игр, "
                    f"В-Н-П {s.get('wins', '?')}-{s.get('ties', '?')}-{s.get('losses', '?')}, "
                    f"разница {s.get('pointDifferential', '?')}"
                )
    return None


def _form_lines(summary: dict, team: str) -> list[str]:
    for block in summary.get("lastFiveGames", []):
        if block.get("team", {}).get("displayName") != team:
            continue
        team_id = block["team"].get("id")
        lines = []
        for g in block.get("events", []):
            at_home = g.get("homeTeamId") == team_id
            # Счёт всегда «забили:пропустили» с точки зрения этой команды
            scored, conceded = (
                (g.get("homeTeamScore"), g.get("awayTeamScore"))
                if at_home
                else (g.get("awayTeamScore"), g.get("homeTeamScore"))
            )
            opponent = g.get("opponent", {}).get("displayName", "?")
            date = g.get("gameDate", "")[:10]
            lines.append(
                f"    {date} {'дома' if at_home else 'в гостях'} vs {opponent}: "
                f"{scored}:{conceded} ({g.get('gameResult', '?')})"
            )
        return lines
    return []


def _injury_lines(summary: dict, team: str) -> list[str]:
    for block in summary.get("injuries", []):
        if block.get("team", {}).get("displayName") != team:
            continue
        return [
            f"    {i.get('athlete', {}).get('displayName', '?')} — {i.get('status', '?')}"
            for i in block.get("injuries", [])[:8]
        ]
    return []


def _header(f: Fixture) -> list[str]:
    lines = [
        f"Вид спорта: {SPORT_NAMES.get(f.sport, f.sport)}",
        f"Турнир: {f.league_name}" + (f" ({f.note})" if f.note else ""),
    ]
    if f.sport == "mma":
        lines.insert(0, f"Бой: {f.home} — {f.away}")
    elif f.sport == "tennis":
        lines.insert(0, f"Матч: {f.home} — {f.away}")
    else:
        lines.insert(0, f"Матч: {f.home} (дома) — {f.away} (в гостях)")
    return lines


async def match_card(session: aiohttp.ClientSession, fixture: Fixture) -> str:
    """Текстовая карточка предстоящего матча со всеми цифрами, которые увидит модель."""
    if fixture.event_id.startswith("fd"):  # матч из резервного источника
        return await backup.match_card(session, fixture)
    lines = _header(fixture) + [f"Начало: {fixture.kickoff_msk} МСК", ""]

    if fixture.sport == "tennis":
        # Для тенниса ESPN не отдаёт ни формы, ни коэффициентов
        lines.append("Дополнительных данных (рейтинг, форма, коэффициенты) источник не предоставляет.")
        return "\n".join(lines)

    if fixture.sport == "mma":
        lines += [
            "Формат боя: октагон UFC (3 раунда по 5 минут, главный бой — 5 раундов).",
            "",
            "Сделай подробный экспертный тактический разбор поединка:",
            f"1. {fixture.home} — сильные и слабые стороны, ударная техника, джиу-джитсу/борьба, кардио.",
            f"2. {fixture.away} — сильные и слабые стороны, стойка, защита от тейкдаунов, нокаутирующая мощь.",
            "3. Стилистическое столкновение (clash of styles) и контроль дистанции/темпа.",
            "4. Идея ставки: кто победит и каким методом (нокаут / сабмишен / судейское решение) или тотал раундов.",
        ]
        return "\n".join(lines)

    summary: dict = {}
    try:
        summary = await _get_json(session, f"{BASE}/{fixture.league}/summary?event={fixture.event_id}")
    except SourceError as e:
        log.warning("Summary request failed for %s (%s): %s", fixture.event_id, fixture.league, e)

    if summary:
        for team in (fixture.home, fixture.away):
            lines.append(f"{team}:")
            if standing := _standings_line(summary, team):
                lines.append(f"  Таблица: {standing}")
            if form := _form_lines(summary, team):
                lines.append("  Последние матчи, счёт забили:пропустили (W — победа, D — ничья, L — поражение):")
                lines.extend(form)
            if injuries := _injury_lines(summary, team):
                lines.append("  Травмы:")
                lines.extend(injuries)
            lines.append("")
        lines.extend(_odds_lines(summary))
    return "\n".join(lines)


async def find_fixture(session: aiohttp.ClientSession, league: str, event_id: str) -> Fixture | None:
    """Матч из списка ближайших по его id (список берётся из кэша, поэтому быстро)."""
    if event_id.startswith("fd") and backup.available(league):
        fixtures = await backup.upcoming(session, league, LEAGUES.get(league, league))
    else:
        fixtures = await upcoming_fixtures(session, league)
    return next((f for f in fixtures if f.event_id == event_id), None)


# ---------- Лайв ----------

async def live_matches(session: aiohttp.ClientSession) -> list[Fixture]:
    """Все идущие сейчас матчи: футбол всех лиг мира + остальные виды спорта."""
    results = await asyncio.gather(
        *(_get_json(session, f"{BASE}/{src}/scoreboard", ttl=LIVE_TTL) for src in LIVE_SOURCES),
        return_exceptions=True,
    )
    matches: list[Fixture] = []
    for src, result in zip(LIVE_SOURCES, results):
        if isinstance(result, Exception):
            log.warning("Live scoreboard failed for %s: %s", src, result)
            continue
        matches.extend(f for f, _ in _parse_board(result, src) if f.state == "in")
    if not matches and all(isinstance(r, Exception) for r in results):
        raise SourceError("лайв недоступен")
    return matches


def _team_stats(competitors: list[dict]) -> dict[str, dict[str, str]]:
    return {
        c.get("homeAway", str(i)): {s["name"]: s.get("displayValue") for s in c.get("statistics", [])}
        for i, c in enumerate(competitors)
    }


async def live_card(session: aiohttp.ClientSession, league: str, event_id: str) -> str | None:
    """Карточка идущего матча: счёт, время, статистика, события и предматчевая линия.
    None — матч уже закончился или не найден."""
    data = await _get_json(session, f"{BASE}/{league}/scoreboard", ttl=LIVE_TTL)
    found = next(((f, c) for f, c in _parse_board(data, league) if f.event_id == event_id and f.state == "in"), None)
    if found is None:
        return None
    f, comp = found

    lines = ["ЛАЙВ", *_header(f), f"Время/период: {f.clock}", f"Счёт: {f.home} {f.home_score} — {f.away_score} {f.away}"]
    if f.sport == "tennis":
        lines.append("(счёт по сетам; статистика розыгрышей и коэффициенты источником не предоставляются)")
        return "\n".join(lines)

    if f.sport == "mma":
        lines = [
            "ЛАЙВ", *_header(f),
            f"Раунд/время: {f.clock or 'Идёт бой'}",
            f"Бойцы: {f.home} — {f.away}",
            "",
            "Формат: рейтинговый бой UFC в октагоне.",
            "Оцени текущий ход боя по времени и предложи лучшую лайв-ставку (победитель, досрочное завершение или тотал раундов).",
        ]
        return "\n".join(lines)

    summary: dict = {}
    try:
        summary = await _get_json(session, f"{BASE}/{league}/summary?event={event_id}", ttl=LIVE_TTL)
    except SourceError as e:
        log.warning("Summary for live %s failed: %s", event_id, e)

    # Для «футбола всех лиг» настоящее название турнира есть только в summary
    if real_league := (summary.get("header") or {}).get("league", {}).get("name"):
        lines = [line.replace(f"Турнир: {f.league_name}", f"Турнир: {real_league}") for line in lines]

    # Командная статистика: сначала из boxscore (есть во всех командных видах), иначе из scoreboard
    box = (summary.get("boxscore") or {}).get("teams") or []
    if box:
        stats: dict[str, dict[str, str]] = {"home": {}, "away": {}}
        for t in box:
            side = "home" if t.get("homeAway") == "home" or t.get("team", {}).get("displayName") == f.home else "away"
            stats[side] = {s.get("name"): s.get("displayValue") for s in t.get("statistics", [])}
    else:
        stats = _team_stats(comp.get("competitors", []))

    home_stats, away_stats = stats.get("home", {}), stats.get("away", {})

    # Нормализация и защита от противоречий в данных
    clock_min = _parse_min(f.clock)
    if f.sport == "soccer":
        h_shots = _parse_num(home_stats.get("totalShots", 0))
        a_shots = _parse_num(away_stats.get("totalShots", 0))
        h_poss = _parse_num(home_stats.get("possessionPct", 0))
        a_poss = _parse_num(away_stats.get("possessionPct", 0))
        h_sc = _parse_num(f.home_score)
        a_sc = _parse_num(f.away_score)

        # Если матч идет уже > 15 мин, а ударов 0 и владения нет — лига не передаёт live-статистику
        if clock_min >= 15 and (h_shots + a_shots == 0) and (h_poss + a_poss < 70):
            home_stats = {}
            away_stats = {}
            lines += ["", "Статистика: детальные показатели ударов и владения источником для этого турнира в лайве не передаются (ориентируйся на счёт, статус и события)."]
        else:
            # Удары в створ не могут быть меньше забитых голов, а удары всего — не меньше створов
            if "shotsOnTarget" in home_stats:
                h_sot = max(_parse_num(home_stats.get("shotsOnTarget", 0)), h_sc)
                home_stats["shotsOnTarget"] = str(int(h_sot))
                if "totalShots" in home_stats:
                    home_stats["totalShots"] = str(int(max(h_shots, h_sot)))
            if "shotsOnTarget" in away_stats:
                a_sot = max(_parse_num(away_stats.get("shotsOnTarget", 0)), a_sc)
                away_stats["shotsOnTarget"] = str(int(a_sot))
                if "totalShots" in away_stats:
                    away_stats["totalShots"] = str(int(max(a_shots, a_sot)))
    elif f.sport == "hockey":
        h_sc = _parse_num(f.home_score)
        a_sc = _parse_num(f.away_score)
        if "shotsTotal" in home_stats:
            home_stats["shotsTotal"] = str(int(max(_parse_num(home_stats.get("shotsTotal", 0)), h_sc)))
        if "shotsTotal" in away_stats:
            away_stats["shotsTotal"] = str(int(max(_parse_num(away_stats.get("shotsTotal", 0)), a_sc)))

    keys = [k for k in STAT_LABELS if k in home_stats or k in away_stats]
    if not keys and home_stats:  # незнакомый вид спорта — отдаём первые показатели как есть
        keys = [k for k in home_stats if "-" not in k][:12]
    if keys:
        lines += ["", f"Статистика ({f.home} / {f.away}):"]
        lines += [f"  {STAT_LABELS.get(k, k)}: {home_stats.get(k, '?')} / {away_stats.get(k, '?')}" for k in keys]

    names = {c.get("team", {}).get("id"): _name(c) for c in comp.get("competitors", [])}
    events = []
    for d in comp.get("details", []):
        if d.get("scoringPlay") or d.get("redCard") or d.get("yellowCard"):
            kind = d.get("type", {}).get("text", "")
            if kind.startswith("Goal") and kind not in EVENT_KINDS:  # "Goal - Volley", "Goal - Header"…
                kind = "Goal"
            who = ", ".join(a.get("displayName", "") for a in d.get("athletesInvolved", []))
            team = names.get(d.get("team", {}).get("id"), "?")
            events.append(f"  {d.get('clock', {}).get('displayValue', '?')} {EVENT_KINDS.get(kind, kind)} — {team} {who}".rstrip())
    if events:
        lines += ["", "События:", *events]

    if summary:
        # Предматчевая линия показывает, кого рынок считал фаворитом до старта
        lines += ["", *_odds_lines(summary, "до матча, открытие → закрытие")]
    return "\n".join(lines)


# ---------- Радар лайв-аномалий и Экспресс-конструктор ----------

@dataclass(frozen=True)
class LiveAnomaly:
    event_id: str
    league: str
    league_name: str
    home: str
    away: str
    clock: str
    home_score: str
    away_score: str
    sport: str
    kind: str           # "storm" | "red_card" | "dry" | "comeback" | "hot"
    badge: str          # "🔥 Назревает гол" | "🟥 Удаление" | "🔒 Бетонный ТМ" | "⚡ Камбэк-зона" | "🎯 Горячий лайв"
    tagline: str        # Краткое описание аномалии
    priority: float     # Рейтинг важности для сортировки

    @property
    def title(self) -> str:
        return f"{self.home} — {self.away}"

    @property
    def live_title(self) -> str:
        return f"{self.clock} {self.home} {self.home_score}:{self.away_score} {self.away}".strip()


def _parse_num(val) -> float:
    try:
        return float(str(val).replace("%", "").strip())
    except (ValueError, TypeError):
        return 0.0


def _parse_min(clock_str: str) -> int:
    cs = (clock_str or "").lower()
    if "half" in cs or "ht" in cs:
        return 45
    m = re.search(r"(\d+)", cs)
    return int(m.group(1)) if m else 0


async def radar_anomalies(session: aiohttp.ClientSession) -> list[LiveAnomaly]:
    """Сканирует все идущие матчи и выявляет статистические и игровые аномалии для ставок."""
    results = await asyncio.gather(
        *(_get_json(session, f"{BASE}/{src}/scoreboard", ttl=LIVE_TTL) for src in LIVE_SOURCES),
        return_exceptions=True,
    )
    all_live: list[tuple[Fixture, dict]] = []
    for src, res in zip(LIVE_SOURCES, results):
        if isinstance(res, Exception):
            continue
        all_live.extend((f, c) for f, c in _parse_board(res, src) if f.state == "in")

    anomalies: list[LiveAnomaly] = []
    for f, comp in all_live:
        clock_min = _parse_min(f.clock)
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            continue

        c1, c2 = competitors[0], competitors[1]
        s1 = {st["name"]: st.get("displayValue", "0") for st in c1.get("statistics", [])}
        s2 = {st["name"]: st.get("displayValue", "0") for st in c2.get("statistics", [])}

        shots1, shots2 = _parse_num(s1.get("totalShots", 0)), _parse_num(s2.get("totalShots", 0))
        sot1, sot2 = _parse_num(s1.get("shotsOnTarget", 0)), _parse_num(s2.get("shotsOnTarget", 0))
        poss1, poss2 = _parse_num(s1.get("possessionPct", 0)), _parse_num(s2.get("possessionPct", 0))
        sc1, sc2 = _parse_num(f.home_score), _parse_num(f.away_score)

        # 1. Удаление в матче (проверяем по событиям даже при скудной статистике)
        reds = [d for d in comp.get("details", []) if d.get("redCard")]
        rc1 = _parse_num(s1.get("redCards", 0))
        rc2 = _parse_num(s2.get("redCards", 0))
        if reds or rc1 > 0 or rc2 > 0:
            if rc1 > 0 and rc2 == 0:
                tagline = f"Красная карточка у {f.home} ({f.clock}), счёт {f.home_score}:{f.away_score}. Численное большинство у {f.away}!"
            elif rc2 > 0 and rc1 == 0:
                tagline = f"Красная карточка у {f.away} ({f.clock}), счёт {f.home_score}:{f.away_score}. Численное большинство у {f.home}!"
            else:
                tagline = f"Красная карточка на {f.clock}, счёт {f.home_score}:{f.away_score}. Игра в неравных составах!"
            anomalies.append(LiveAnomaly(
                event_id=f.event_id, league=f.league, league_name=f.league_name,
                home=f.home, away=f.away, clock=f.clock, home_score=f.home_score, away_score=f.away_score,
                sport=f.sport, kind="red_card", badge="🟥 Удаление в матче",
                tagline=tagline,
                priority=95.0 + clock_min / 10.0,
            ))
            continue

        # ВАЖНО: Разделение по видам спорта
        if f.sport == "soccer":
            # Исключаем матчи, где статистика ударов не собирается источником
            if (shots1 + shots2 == 0) or (poss1 + poss2 < 70 and shots1 + shots2 < 3):
                continue

            # Строгая нормализация: створ >= голам, удары >= створам
            sot1 = max(sot1, sc1)
            sot2 = max(sot2, sc2)
            shots1 = max(shots1, sot1)
            shots2 = max(shots2, sot2)
            total_goals = sc1 + sc2
            total_sot = sot1 + sot2
            total_shots = shots1 + shots2

            # 2. Камбэк-зона: проигрывающая команда мощно давит
            if clock_min >= 40:
                if sc1 < sc2 and (poss1 >= 58 or (shots1 >= shots2 + 4 and shots1 >= 7)):
                    anomalies.append(LiveAnomaly(
                        event_id=f.event_id, league=f.league, league_name=f.league_name,
                        home=f.home, away=f.away, clock=f.clock, home_score=f.home_score, away_score=f.away_score,
                        sport=f.sport, kind="comeback", badge="⚡ Камбэк-зона",
                        tagline=f"{f.home} уступает {f.home_score}:{f.away_score}, но давит: владение {int(poss1)}%, удары {int(shots1)}:{int(shots2)}",
                        priority=88.0 + shots1,
                    ))
                    continue
                elif sc2 < sc1 and (poss2 >= 58 or (shots2 >= shots1 + 4 and shots2 >= 7)):
                    anomalies.append(LiveAnomaly(
                        event_id=f.event_id, league=f.league, league_name=f.league_name,
                        home=f.home, away=f.away, clock=f.clock, home_score=f.home_score, away_score=f.away_score,
                        sport=f.sport, kind="comeback", badge="⚡ Камбэк-зона",
                        tagline=f"{f.away} уступает ({f.home_score}:{f.away_score}), но давит: владение {int(poss2)}%, удары {int(shots1)}:{int(shots2)} ({int(shots2)} у {f.away})",
                        priority=88.0 + shots2,
                    ))
                    continue

            # 3. Штурм ворот / Назревает гол: равный счёт при мощном давлении И реальных ударах в створ
            if clock_min >= 25 and sc1 == sc2:
                if total_sot >= 3 and (total_shots >= 12 or abs(shots1 - shots2) >= 5 or max(poss1, poss2) >= 63):
                    anomalies.append(LiveAnomaly(
                        event_id=f.event_id, league=f.league, league_name=f.league_name,
                        home=f.home, away=f.away, clock=f.clock, home_score=f.home_score, away_score=f.away_score,
                        sport=f.sport, kind="storm", badge="🔥 Назревает гол",
                        tagline=f"Счёт {f.home_score}:{f.away_score} при мощном давлении: удары {int(shots1)}:{int(shots2)} (створ {int(sot1)}:{int(sot2)})",
                        priority=85.0 + total_shots,
                    ))
                    continue

            # 4. Бетонный сушняк / ТМ: второй тайм, минимум моментов и створов (но статистика реально собирается!)
            if clock_min >= 55 and total_goals <= 1 and total_shots >= 3 and total_sot <= 2:
                anomalies.append(LiveAnomaly(
                    event_id=f.event_id, league=f.league, league_name=f.league_name,
                    home=f.home, away=f.away, clock=f.clock, home_score=f.home_score, away_score=f.away_score,
                    sport=f.sport, kind="dry", badge="🔒 Бетонный ТМ",
                    tagline=f"{f.clock} при счёте {f.home_score}:{f.away_score}: всего {int(total_sot)} уд. в створ на двоих ({int(total_shots)} ударов всего)",
                    priority=80.0 + clock_min / 5.0,
                ))
                continue

            # 5. Высокий темп (для остальных активных игр с высокой плотностью моментов)
            if total_shots >= 16 and total_sot >= 6:
                anomalies.append(LiveAnomaly(
                    event_id=f.event_id, league=f.league, league_name=f.league_name,
                    home=f.home, away=f.away, clock=f.clock, home_score=f.home_score, away_score=f.away_score,
                    sport=f.sport, kind="hot", badge="🎯 Горячий лайв",
                    tagline=f"{f.clock}: сверхактивный темп, {int(total_shots)} ударов, {int(total_sot)} в створ",
                    priority=70.0 + total_shots,
                ))
                continue

        elif f.sport == "hockey":
            h_shots1 = max(_parse_num(s1.get("shotsTotal", 0)), sc1)
            h_shots2 = max(_parse_num(s2.get("shotsTotal", 0)), sc2)
            period = comp.get("status", {}).get("period", 0)
            if period >= 3 and abs(sc1 - sc2) <= 1:
                shots_note = f" (броски {int(h_shots1)}:{int(h_shots2)})" if (h_shots1 + h_shots2 > 0) else ""
                anomalies.append(LiveAnomaly(
                    event_id=f.event_id, league=f.league, league_name=f.league_name,
                    home=f.home, away=f.away, clock=f.clock or f"{period}-й период",
                    home_score=f.home_score, away_score=f.away_score,
                    sport=f.sport, kind="storm", badge="🏒 Развязка в хоккее",
                    tagline=f"{period}-й период, напряжённый счёт {f.home_score}:{f.away_score}{shots_note}",
                    priority=86.0 + (h_shots1 + h_shots2) / 5.0,
                ))

        elif f.sport == "basketball":
            period = comp.get("status", {}).get("period", 0)
            if period >= 4 and abs(sc1 - sc2) <= 6 and (sc1 + sc2 > 80):
                anomalies.append(LiveAnomaly(
                    event_id=f.event_id, league=f.league, league_name=f.league_name,
                    home=f.home, away=f.away, clock=f.clock or "4-я четверть",
                    home_score=f.home_score, away_score=f.away_score,
                    sport=f.sport, kind="hot", badge="🏀 Клатч в баскетболе",
                    tagline=f"4-я четверть, плотный счёт {f.home_score}:{f.away_score} (разница {int(abs(sc1-sc2))} очк.)",
                    priority=87.0,
                ))

    anomalies.sort(key=lambda a: a.priority, reverse=True)
    return anomalies


async def express_matches_card(session: aiohttp.ClientSession, preset: str = "optimal") -> tuple[str, str]:
    """Собирает карточку из топ-матчей ближайших 48 часов для составления экспресса."""
    target_leagues = [
        "soccer/eng.1", "soccer/esp.1", "soccer/ita.1", "soccer/ger.1", "soccer/fra.1",
        "soccer/uefa.champions", "hockey/nhl", "basketball/nba", "mma/ufc", "soccer/rus.1"
    ]
    collected: list[Fixture] = []
    for l_key in target_leagues:
        try:
            fixes = await upcoming_fixtures(session, l_key)
            if fixes:
                collected.append(fixes[0])
            if len(collected) >= 7:
                break
        except Exception:
            continue

    if len(collected) < 5:
        for l_key in target_leagues:
            try:
                fixes = await upcoming_fixtures(session, l_key)
                for f in fixes[1:3]:
                    if f.event_id not in {c.event_id for c in collected}:
                        collected.append(f)
                    if len(collected) >= 7:
                        break
                if len(collected) >= 7:
                    break
            except Exception:
                continue

    preset_titles = {
        "reliable": "🛡️ Надежный двойник (кэф ~1.85–2.25)",
        "optimal": "⚖️ Оптимальный баланс (кэф ~2.80–3.80)",
        "risky": "🚀 Хайроллер / Риск (кэф ~5.00–8.00+)",
    }
    title = preset_titles.get(preset, preset_titles["optimal"])

    lines = [
        f"ПАРАМЕТРЫ ЗАПРОСА: {title}",
        f"Целевой профиль риска: {preset}",
        "",
        "Доступные статусные матчи ближайших 48 часов из разных лиг. Выбери из них независимые события для купона:",
        ""
    ]

    for i, f in enumerate(collected, 1):
        lines.append(f"{i}. {f.league_name}: {f.home} — {f.away} (Начало: {f.kickoff_msk} МСК, вид спорта: {SPORT_NAMES.get(f.sport, f.sport)})")

    return "\n".join(lines), title

