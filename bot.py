#!/usr/bin/env python3
"""Utah Mammoth franchise stats bot. Deterministic engine; no model touches a number.

  python3 bot.py seed            rebuild reference data from the NHL APIs (keeps ledgers)
  python3 bot.py tick            scheduled entry point: check, then the live watcher if a game is on or about to start
  python3 bot.py check           pregame/postgame/off-day pass: prints (and pushes) new tweets
  python3 bot.py live            watch today's game until it ends, pushing goal facts once they settle
  python3 bot.py daily           print today's off-day candidates, best first (no state writes)
  python3 bot.py live-replay ID  what the live watcher would have pushed, goal by goal, for a finished game
  python3 bot.py backfill        fetch per-game summaries (first goal, period scores, scorers) for every franchise game
  python3 bot.py replay GAMEID   what the passes would print for one finished game (no state writes)
  python3 bot.py replay-season   roll state back to opening night and replay every finished RS game
  python3 bot.py status
"""
import copy
import datetime as dt
import json
import math
import os
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "state.json")

TEAM = "UTA"
TEAM_ID = 68
FRANCHISE = 40
TAG = "#TusksUp"
TEAM_HANDLE = "@utahmammoth"
TZ = ZoneInfo("America/Denver")
WEB = "https://api-web.nhle.com/v1"
REST = "https://api.nhle.com/stats/rest/en"

SKATER_LADDERS = {
    "points": "points", "goals": "goals", "assists": "assists", "gamesPlayed": "games played",
    "penaltyMinutes": "PIM", "ppGoals": "power play goals",
    "p2": "2+ point games", "p3": "3+ point games", "g2": "2+ goal games",
}
GOALIE_LADDERS = {"wins": "wins", "shutouts": "shutouts", "gamesPlayed": "games played"}
RANK_LIMIT = {
    "points": 15, "goals": 15, "assists": 15, "gamesPlayed": 10, "penaltyMinutes": 5, "ppGoals": 5,
    "p2": 3, "p3": 3, "g2": 3,
}
TIE_LIMIT = {"points": 3, "goals": 3, "assists": 3}  # ties announced only on these ladders, only at or above this rank
GOALIE_RANK_LIMIT = {"wins": 3, "shutouts": 3, "gamesPlayed": 3}
GOALIE_TIE_LIMIT = {"wins": 1, "shutouts": 1}
NHL_CUTOFF = {"points": 250, "goals": 250, "assists": 250, "gamesPlayed": 250}
NHL_D_CUTOFF = {"points": 100, "goals": 100, "assists": 100}
TIER = {"first": 0, "milestone": 1, "record": 1, "team": 1, "nhl": 2, "ladder": 3, "ladder2": 4, "goalie": 5}
CAREER_STEP = {"points": 100, "goals": 50, "assists": 100, "gamesPlayed": 100}
GOALIE_STEP = {"wins": 50, "shutouts": 10, "gamesPlayed": 100}
APPROACH_WINDOW = {"points": 3, "goals": 2, "assists": 3}
SEASON_THRESHOLDS = {"goals": [20, 30, 40, 50], "points": [50, 100]}
FRANCHISE_FIRST_STEP = {"points": 50, "goals": 25, "assists": 50}
STAT_WORD = {"points": "points", "goals": "goals", "assists": "assists", "gamesPlayed": "games played"}
UNIT = {"points": ("point", "points"), "goals": ("goal", "goals"), "assists": ("assist", "assists")}
ESPN_INJURIES = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries"
SETTLE = dt.timedelta(minutes=10)  # scorer/assist changes land within ~10 minutes of a goal
POLL_SECONDS = 15
LIVE_LEAD = dt.timedelta(minutes=25)  # start watching this long before puck drop
LIVE_MAX = dt.timedelta(hours=5, minutes=30)  # Actions jobs die at 6h; the next tick picks the game back up
LIVE_PER_POLL = 3  # most live tweets pushed in one poll; the rest wait for the next
EARLY_PREGAME_HOUR = 11  # local hour the projected-lineup pregame posts go out
DAILY_HOUR = 10  # local hour of the off-day post

# ----------------------------------------------------------------------------- http

def get(url, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "utmammothstats/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def rest(path, **params):
    q = {k: v for k, v in params.items() if v is not None}
    if "cayenneExp" in q:
        q["cayenneExp"] = q["cayenneExp"]
    return get(f"{REST}/{path}?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote))


def rest_all(path, cayenne, sort=None, aggregate=False, game=False):
    params = dict(isAggregate="true" if aggregate else "false", isGame="true" if game else "false",
                  limit=-1, cayenneExp=cayenne)
    if sort:
        params["sort"] = sort
    return rest(path, **params)["data"]

# ----------------------------------------------------------------------------- helpers

def ordinal(n):
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def rank_of(value, values):
    """1 + number of strictly greater values. values: iterable of numbers (others only)."""
    return 1 + sum(1 for v in values if v > value)


def rank_label(value, values):
    r = rank_of(value, values)
    tied = sum(1 for v in values if v == value)
    return (f"t-{ordinal(r)}" if tied else ordinal(r)), r, tied


def load_state():
    with open(STATE_PATH) as f:
        return json.load(f)


def save_state(s):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=1, sort_keys=True)
    os.replace(tmp, STATE_PATH)


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def persist(s, msg="check"):
    """Save state; on the Actions runner (BOT_GIT=1) also commit and push it so a crash never loses the ledger."""
    s["meta"]["last_check"] = now_utc().isoformat()
    save_state(s)
    if not os.environ.get("BOT_GIT"):
        return
    git = lambda *a: subprocess.run(["git", "-C", ROOT, *a], capture_output=True, text=True)
    git("add", "state.json")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("commit", "-m", msg)
    for _ in range(3):
        if git("push").returncode == 0:
            return
        git("pull", "--rebase", "-X", "theirs")
    raise RuntimeError("git push failed three times")


def notify(title, body, click=None, priority=3):
    """One phone push through ntfy (NTFY_TOPIC). Without a topic, does nothing; stdout still has everything."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    msg = {"topic": topic, "title": title, "message": body, "priority": priority}
    if click:
        msg["click"] = click
        msg["actions"] = [{"action": "view", "label": "Post", "url": click, "clear": True}]
    req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(msg).encode(),
                                 headers={"Content-Type": "application/json"})
    for i in range(3):
        try:
            urllib.request.urlopen(req, timeout=20).read()
            return
        except Exception:  # noqa
            time.sleep(2 * (i + 1))


def compose_url(text):
    return "https://x.com/intent/post?text=" + urllib.parse.quote(text, safe="")


def norm_name(n):
    n = unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode()
    return " ".join(n.lower().replace(".", "").replace("-", " ").split())


def local_now():
    return dt.datetime.now(TZ)


def parse_utc(x):
    return dt.datetime.fromisoformat(x.replace("Z", "+00:00"))


def season_id_now(schedule):
    return int(schedule["games"][0]["season"]) if schedule.get("games") else None

# ----------------------------------------------------------------------------- seed

def seed(keep=None):
    """Build reference data. `keep` is an existing state whose ledgers survive."""
    s = {"meta": {"team": TEAM, "franchise": FRANCHISE, "seeded_at": now_utc().isoformat()},
         "players": {}, "goalies": {}, "season_rows": [], "team_games": [],
         "nhl": {}, "ledger": {"surfaced": {}, "processed": [], "pregame_done": [], "alerts": {}}}
    if keep:
        s["ledger"] = keep.get("ledger", s["ledger"])
        s["games"] = keep.get("games", {})

    sched = get(f"{WEB}/club-schedule-season/{TEAM}/now")
    season = season_id_now(sched)
    s["meta"]["season"] = season
    cay = f"franchiseId={FRANCHISE} and gameTypeId=2"

    # franchise career ladders (everyone who ever dressed for Utah)
    for r in rest_all("skater/summary", cay, aggregate=True):
        s["players"][str(r["playerId"])] = {
            "name": r["skaterFullName"], "pos": r["positionCode"],
            "fr": {k: r[k] for k in ("points", "goals", "assists", "gamesPlayed", "penaltyMinutes", "ppGoals")},
            "career": None, "seasons": [], "season": {"goals": 0, "assists": 0, "points": 0, "gamesPlayed": 0},
        }
    for r in rest_all("goalie/summary", cay, aggregate=True):
        s["goalies"][str(r["playerId"])] = {
            "name": r["goalieFullName"],
            "fr": {k: r[k] for k in ("wins", "shutouts", "gamesPlayed")},
            "career": None,
        }

    # per-game rows -> multi-point ladders and franchise single-game history
    games_rows = rest_all("skater/summary", cay, game=True)
    for p in s["players"].values():
        p["fr"].update({"p2": 0, "p3": 0, "g2": 0})
    hist = {"hat": 0, "p4": 0, "maxg": 0, "maxp": 0}
    for r in games_rows:
        p = s["players"].get(str(r["playerId"]))
        if p is None:
            continue
        if r["points"] >= 2: p["fr"]["p2"] += 1
        if r["points"] >= 3: p["fr"]["p3"] += 1
        if r["goals"] >= 2: p["fr"]["g2"] += 1
        if r["goals"] >= 3: hist["hat"] += 1
        if r["points"] >= 4: hist["p4"] += 1
        hist["maxg"] = max(hist["maxg"], r["goals"])
        hist["maxp"] = max(hist["maxp"], r["points"])
    s["hist"] = hist

    # per-season rows for single-season records (all seasons, incl. current)
    for r in rest_all("skater/summary", cay):
        s["season_rows"].append({"season": r["seasonId"], "pid": str(r["playerId"]), "name": r["skaterFullName"],
                                 "goals": r["goals"], "assists": r["assists"], "points": r["points"]})

    # team game log for streak records
    for r in sorted(rest_all("team/summary", cay, game=True), key=lambda x: x["gameDate"]):
        s["team_games"].append({"gameId": r["gameId"], "date": r["gameDate"], "season": int(str(r["gameId"])[:4]) * 10000 + int(str(r["gameId"])[:4]) + 1,
                                "win": r["wins"] == 1, "otl": r["otLosses"] == 1, "home": r["homeRoad"] == "H",
                                "pts": r["points"]})

    # current roster: career totals + season history (authoritative, from player landing)
    roster = get(f"{WEB}/roster/{TEAM}/current")
    for group in ("forwards", "defensemen", "goalies"):
        for r in roster.get(group, []):
            pid = str(r["id"])
            land = get(f"{WEB}/player/{pid}/landing")
            full = f'{land["firstName"]["default"]} {land["lastName"]["default"]}'
            ct = (land.get("careerTotals") or {}).get("regularSeason") or {}
            seasons = [{"season": x["season"], "goals": x.get("goals", 0), "assists": x.get("assists", 0),
                        "points": x.get("points", 0), "gamesPlayed": x.get("gamesPlayed", 0),
                        "wins": x.get("wins", 0), "shutouts": x.get("shutouts", 0)}
                       for x in land.get("seasonTotals", []) if x.get("leagueAbbrev") == "NHL" and x.get("gameTypeId") == 2]
            if group == "goalies":
                g = s["goalies"].setdefault(pid, {"name": full, "fr": {"wins": 0, "shutouts": 0, "gamesPlayed": 0}})
                g["name"] = full
                g["career"] = {"wins": ct.get("wins", 0), "shutouts": ct.get("shutouts", 0), "gamesPlayed": ct.get("gamesPlayed", 0)}
                g["seasons"] = seasons
                g["season"] = {"wins": 0, "shutouts": 0, "gamesPlayed": 0}
                g["on_roster"] = True
            else:
                p = s["players"].setdefault(pid, {"name": full, "pos": r["positionCode"],
                                                  "fr": {"points": 0, "goals": 0, "assists": 0, "gamesPlayed": 0, "penaltyMinutes": 0, "ppGoals": 0, "p2": 0, "p3": 0, "g2": 0},
                                                  "seasons": []})
                p["name"] = full
                p["pos"] = r["positionCode"]
                p["career"] = {k: ct.get(k, 0) for k in ("points", "goals", "assists", "gamesPlayed")}
                p["seasons"] = seasons
                p["on_roster"] = True
    # season-to-date totals from the club stats page (current season only)
    club = get(f"{WEB}/club-stats/{TEAM}/now")
    for r in club.get("skaters", []):
        p = s["players"].get(str(r["playerId"]))
        if p:
            p["season"] = {k: r[k] for k in ("goals", "assists", "points", "gamesPlayed")}
    for r in club.get("goalies", []):
        g = s["goalies"].get(str(r["playerId"]))
        if g:
            g["season"] = {"wins": r["wins"], "shutouts": r["shutouts"], "gamesPlayed": r["gamesPlayed"]}

    s["nhl"] = build_nhl()

    # processed = everything already final this season (check only looks forward from here)
    finished = [g["id"] for g in sched["games"] if g["gameType"] == 2 and g.get("gameState") in ("OFF", "FINAL")]
    if not keep:
        s["ledger"]["processed"] = finished
    s["meta"]["reconciled_at"] = now_utc().isoformat()
    return s

# ----------------------------------------------------------------------------- game data

def utah_lines(box):
    side = "homeTeam" if box["homeTeam"]["abbrev"] == TEAM else "awayTeam"
    opp = "awayTeam" if side == "homeTeam" else "homeTeam"
    pbt = box["playerByGameStats"][side]
    skaters, goalies = [], []
    for grp in ("forwards", "defense"):
        for p in pbt[grp]:
            skaters.append({"pid": str(p["playerId"]), "short": p["name"]["default"], "pos": p["position"],
                            "goals": p["goals"], "assists": p["assists"], "points": p["points"],
                            "pim": p.get("pim", 0), "ppg": p.get("powerPlayGoals", 0)})
    played = [g for g in pbt["goalies"] if g.get("toi") and g["toi"] != "00:00"]
    us, them = box[side].get("score", 0), box[opp].get("score", 0)
    for g in played:
        goalies.append({"pid": str(g["playerId"]), "short": g["name"]["default"],
                        "win": g.get("decision") == "W", "ga": g.get("goalsAgainst", 0),
                        "shutout": len(played) == 1 and g.get("goalsAgainst", 0) == 0 and us > them})
    last_type = (box.get("gameOutcome") or {}).get("lastPeriodType", "REG")
    return {"skaters": skaters, "goalies": goalies, "us": us, "them": them, "home": side == "homeTeam",
            "opp": box[opp]["abbrev"], "win": us > them, "otl": us < them and last_type in ("OT", "SO"),
            "date": box["gameDate"], "id": box["id"], "season": int(box["season"])}


def scoring_goals(land):
    """Every non-shootout goal in a gamecenter landing: (period, eventId, ours, goal dict)."""
    out = []
    for per in (land.get("summary") or {}).get("scoring", []):
        pd = per["periodDescriptor"]
        if pd.get("periodType") == "SO":
            continue
        for g in per.get("goals", []):
            out.append((pd["number"], g.get("eventId"), g["teamAbbrev"]["default"] == TEAM, g))
    return out


def game_summary(land):
    """What a box score doesn't keep: who scored first, the score after 1 and 2, and Utah's scorers."""
    goals = scoring_goals(land)
    home = land["homeTeam"]["abbrev"] == TEAM
    after = {}
    us = them = 0
    first = None
    scorers = {}
    for per, _, ours, g in goals:
        if first is None:
            first = "us" if ours else "them"
        if ours:
            us += 1
            pid = str(g["playerId"])
            scorers[pid] = scorers.get(pid, 0) + 1
        else:
            them += 1
        after[per] = [us, them]
    def at(n):
        best = [0, 0]
        for per in sorted(after):
            if per <= n:
                best = after[per]
        return best
    return {"first": first, "p1": at(1), "p2": at(2), "reg": at(3), "scorers": scorers,
            "home": home, "opp": land["awayTeam" if home else "homeTeam"]["abbrev"]}


def ensure_summaries(s, quiet=True):
    """Fill s["games"][gameId] for every finished franchise game we don't have yet."""
    games = s.setdefault("games", {})
    missing = [t["gameId"] for t in s["team_games"] if str(t["gameId"]) not in games]
    for gid in missing:
        games[str(gid)] = game_summary(get(f"{WEB}/gamecenter/{gid}/landing"))
    if missing and not quiet:
        print(f"summaries: +{len(missing)}")
    return bool(missing)


def season_games(s, season):
    """Finished games this season, oldest first, each joined with its summary."""
    out = []
    for t in s["team_games"]:
        sm = s.get("games", {}).get(str(t["gameId"]))
        if t["season"] == season and sm:
            out.append({**t, **sm})
    return out


def record(gs):
    w = sum(1 for g in gs if g["win"])
    o = sum(1 for g in gs if g["otl"])
    return w, len(gs) - w - o, o


def split_fact(gs, phrase, min_n=5, min_pct=0.7):
    """'The @utahmammoth are W-L-OTL {phrase}' when the sample is big enough and the record is worth bragging about.
    Returns (score, text) or None."""
    if len(gs) < min_n:
        return None
    w, l, o = record(gs)
    pct = (2 * w + o) / (2 * len(gs))
    if pct < min_pct:
        return None
    score = 4 + 6 * (pct - min_pct) / (1 - min_pct) + (2 if l == 0 and o == 0 else 0) + min(len(gs), 20) / 10
    return score, f"The {TEAM_HANDLE} are {w}-{l}-{o} {phrase} {TAG}"


def ensure_player(s, line, box_line_sign=1):
    """Create an unknown skater from the player landing page (callups)."""
    pid = line["pid"]
    if pid in s["players"]:
        return s["players"][pid]
    land = get(f"{WEB}/player/{pid}/landing")
    full = f'{land["firstName"]["default"]} {land["lastName"]["default"]}'
    ct = (land.get("careerTotals") or {}).get("regularSeason") or {}
    career = {k: ct.get(k, 0) for k in ("points", "goals", "assists", "gamesPlayed")}
    # landing already includes tonight; back it out so apply(+1) lands on the truth
    if box_line_sign > 0:
        for k, v in (("points", line["points"]), ("goals", line["goals"]), ("assists", line["assists"]), ("gamesPlayed", 1)):
            career[k] = max(0, career[k] - v)
    seasons = [{"season": x["season"], "goals": x.get("goals", 0), "assists": x.get("assists", 0), "points": x.get("points", 0), "gamesPlayed": x.get("gamesPlayed", 0)}
               for x in land.get("seasonTotals", []) if x.get("leagueAbbrev") == "NHL" and x.get("gameTypeId") == 2]
    p = {"name": full, "pos": land.get("position", "F"),
         "fr": {"points": 0, "goals": 0, "assists": 0, "gamesPlayed": 0, "penaltyMinutes": 0, "ppGoals": 0, "p2": 0, "p3": 0, "g2": 0},
         "career": career, "seasons": seasons, "season": {"goals": 0, "assists": 0, "points": 0, "gamesPlayed": 0}, "on_roster": True}
    s["players"][pid] = p
    return p


def ensure_goalie(s, line, box_line_sign=1):
    pid = line["pid"]
    if pid in s["goalies"]:
        g = s["goalies"][pid]
        if g.get("career") is None:
            g["career"] = {"wins": 0, "shutouts": 0, "gamesPlayed": 0}
            g["season"] = {"wins": 0, "shutouts": 0, "gamesPlayed": 0}
        return g
    land = get(f"{WEB}/player/{pid}/landing")
    full = f'{land["firstName"]["default"]} {land["lastName"]["default"]}'
    ct = (land.get("careerTotals") or {}).get("regularSeason") or {}
    career = {"wins": ct.get("wins", 0), "shutouts": ct.get("shutouts", 0), "gamesPlayed": ct.get("gamesPlayed", 0)}
    if box_line_sign > 0:
        career["wins"] = max(0, career["wins"] - (1 if line["win"] else 0))
        career["shutouts"] = max(0, career["shutouts"] - (1 if line["shutout"] else 0))
        career["gamesPlayed"] = max(0, career["gamesPlayed"] - 1)
    g = {"name": full, "fr": {"wins": 0, "shutouts": 0, "gamesPlayed": 0}, "career": career, "seasons": [],
         "season": {"wins": 0, "shutouts": 0, "gamesPlayed": 0}, "on_roster": True}
    s["goalies"][pid] = g
    return g


def apply_game(s, lines, sign=1):
    """Add (sign=+1) or remove (sign=-1) one game's lines from every running total."""
    for l in lines["skaters"]:
        p = ensure_player(s, l, sign)
        fr, car, sea = p["fr"], p.get("career"), p.setdefault("season", {"goals": 0, "assists": 0, "points": 0, "gamesPlayed": 0})
        for k, v in (("points", l["points"]), ("goals", l["goals"]), ("assists", l["assists"]), ("gamesPlayed", 1),
                     ("penaltyMinutes", l["pim"]), ("ppGoals", l["ppg"])):
            fr[k] = fr.get(k, 0) + sign * v
        fr["p2"] += sign * (1 if l["points"] >= 2 else 0)
        fr["p3"] += sign * (1 if l["points"] >= 3 else 0)
        fr["g2"] += sign * (1 if l["goals"] >= 2 else 0)
        if car is not None:
            for k, v in (("points", l["points"]), ("goals", l["goals"]), ("assists", l["assists"]), ("gamesPlayed", 1)):
                car[k] += sign * v
        for k, v in (("points", l["points"]), ("goals", l["goals"]), ("assists", l["assists"]), ("gamesPlayed", 1)):
            sea[k] += sign * v
        if l["goals"] >= 3: s["hist"]["hat"] += sign
        if l["points"] >= 4: s["hist"]["p4"] += sign
    for l in lines["goalies"]:
        g = ensure_goalie(s, l, sign)
        for k, v in (("wins", 1 if l["win"] else 0), ("shutouts", 1 if l["shutout"] else 0), ("gamesPlayed", 1)):
            g["fr"][k] = g["fr"].get(k, 0) + sign * v
            if g.get("career") is not None:
                g["career"][k] += sign * v
            g.setdefault("season", {"wins": 0, "shutouts": 0, "gamesPlayed": 0})[k] += sign * v
    if sign > 0:
        if not any(t["gameId"] == lines["id"] for t in s["team_games"]):
            s["team_games"].append({"gameId": lines["id"], "date": lines["date"], "season": lines["season"],
                                    "win": lines["win"], "otl": lines["otl"], "home": lines["home"],
                                    "pts": 2 if lines["win"] else (1 if lines["otl"] else 0)})
            s["team_games"].sort(key=lambda t: (t["date"], t["gameId"]))
    else:
        s["team_games"] = [t for t in s["team_games"] if t["gameId"] != lines["id"]]
    if sign < 0:
        s["hist"]["maxg"] = 0
        s["hist"]["maxp"] = 0  # recomputed lazily from season_rows is not possible; keep as-is for replay (minor)

# ----------------------------------------------------------------------------- fact generation

def pool(s, group, stat):
    src_ = s["players"] if group == "skater" else s["goalies"]
    return {pid: (v["name"], v["fr"].get(stat, 0)) for pid, v in src_.items()}


def ladder_pass_facts(pre, post, movers, limit, label, tie_limit=0, pregame=False):
    """Diff a whole ladder. A pass is someone ahead (or level) before and strictly behind after.
    pre/post: {pid: (name, value)}. movers: pids whose value rose."""
    facts = []
    for pid in movers:
        if pid not in pre or pid not in post:
            continue
        name, after = post[pid]
        before = pre[pid][1]
        if after <= before:
            continue
        others_post = [(q, n, v) for q, (n, v) in post.items() if q != pid]
        rk = 1 + sum(1 for _, _, v in others_post if v > after)
        tied = sum(1 for _, _, v in others_post if v == after)
        if rk > limit:
            continue
        lab = f"t-{ordinal(rk)}" if tied else ordinal(rk)
        passed = [(q, n, v) for q, n, v in others_post if pre.get(q, (None, -1))[1] >= before and v < after]
        if passed:
            qid, q, _ = max(passed, key=lambda x: x[2])
            if pregame:
                facts.append(f"Tonight {name} is set to pass {q} on the franchise {label} leaderboard and will be {lab} all time {TAG}")
            else:
                facts.append((f"{name} passes {q} on the franchise {label} leaderboard and is now {lab} all time {TAG}", f"{label}|{'|'.join(sorted([pid, qid]))}"))
        elif tied and rk <= tie_limit:
            q = [n for q_, n, v in others_post if v == after and pre.get(q_, (None, -1))[1] > before]
            if q:
                if pregame:
                    facts.append(f"Tonight {name} is set to tie {q[0]} for {ordinal(rk)} on the franchise {label} leaderboard {TAG}")
                else:
                    facts.append(f"{name} ties {q[0]} for {ordinal(rk)} on the franchise {label} leaderboard {TAG}")
    return facts


def refresh_nhl(s, max_age_hours=12):
    """Re-pull the all-time lists so a pass over an active player is at most a day stale."""
    at = s["nhl"].get("at")
    if at and (now_utc() - dt.datetime.fromisoformat(at)) < dt.timedelta(hours=max_age_hours):
        return
    s["nhl"] = build_nhl()


def build_nhl():
    alltime = rest_all("skater/summary", "gameTypeId=2", aggregate=True)
    nhl = {"lists": {}, "d_lists": {}, "thresholds": {}, "self": {}, "at": now_utc().isoformat()}
    for stat, cutoff in NHL_CUTOFF.items():
        top = sorted(alltime, key=lambda r: -r[stat])[: cutoff + 200]
        nhl["lists"][stat] = [[str(r["playerId"]), r["skaterFullName"], r[stat]] for r in top]
        step = CAREER_STEP[stat]
        maxv = max(r[stat] for r in alltime)
        nhl["thresholds"][stat] = {str(n): sum(1 for r in alltime if r[stat] >= n) for n in range(step, maxv + step, step)}
    dmen = [r for r in alltime if r["positionCode"] == "D"]
    for stat, cutoff in NHL_D_CUTOFF.items():
        top = sorted(dmen, key=lambda r: -r[stat])[: cutoff + 100]
        nhl["d_lists"][stat] = [[str(r["playerId"]), r["skaterFullName"], r[stat]] for r in top]
    # cached values for everyone (used to keep "kth player in history" honest about the player himself)
    nhl["self"] = {str(r["playerId"]): {k: r[k] for k in CAREER_STEP} for r in alltime}
    return nhl


def kth_in_history(s, pid, stat, n):
    k = s["nhl"]["thresholds"][stat].get(str(n))
    if k is None:
        return None
    me = s["nhl"]["self"].get(pid, {}).get(stat, 0)
    if me >= n:
        k -= 1  # the cached list already counted this player
    return k + 1


def nhl_ladder_facts(s, p, stat, before, after):
    facts = []
    if after <= before or p.get("career") is None:
        return facts
    use_d = p["pos"] == "D" and stat in NHL_D_CUTOFF
    lst = s["nhl"]["d_lists"][stat] if use_d else s["nhl"]["lists"][stat]
    cutoff = NHL_D_CUTOFF[stat] if use_d else NHL_CUTOFF[stat]
    others = [(n, v) for pid, n, v in lst if pid != p.get("pid")]
    vals = [v for _, v in others]
    lab, rk, _ = rank_label(after, vals)
    if rk > cutoff:
        return facts
    passed = [(n, v) for n, v in others if before <= v < after]
    if passed:
        q = max(passed, key=lambda x: x[1])[0]
        suffix = " (defence)" if use_d else ""
        facts.append(f"{p['name']} passes {q} in {STAT_WORD[stat]} and is now {lab} in @NHL history{suffix} {TAG}")
    return facts


def prior_season_record(s, stat, season):
    best, who = 0, []
    for r in s["season_rows"]:
        if r["season"] >= season:
            continue
        if r[stat] > best:
            best, who = r[stat], [r["name"]]
        elif r[stat] == best and best:
            who.append(r["name"])
    for pid, p in s["players"].items():
        v = p.get("season", {}).get(stat, 0)
        if v > best:
            best, who = v, [p["name"]]
        elif v == best and best and p["name"] not in who:
            who.append(p["name"])
    return best, who


_CAREER_ROWS = {}


def career_games(pid, exclude_game):
    """Every regular-season NHL game line for a player, tonight excluded (cached per run)."""
    if pid not in _CAREER_ROWS:
        _CAREER_ROWS[pid] = rest_all("skater/summary", f"playerId={pid} and gameTypeId=2", game=True)
    return [r for r in _CAREER_ROWS[pid] if r["gameId"] != exclude_game]


def career_first_facts(name, pid, l, car, game_id):
    """First career multi-goal game, hat trick, 3- and 4-point game. Skipped when it's also his first goal(s),
    which the first-goal line already covers."""
    facts = []
    first_goals = l["goals"] and car["goals"] == l["goals"]
    checks = []
    if l["goals"] >= 3 and not first_goals:
        checks.append(("goals", 3, f"{name} records his first career @NHL hat trick {TAG}"))
    elif l["goals"] == 2 and not first_goals:
        checks.append(("goals", 2, f"{name} scores twice for his first career multi-goal @NHL game {TAG}"))
    if l["points"] >= 4:
        checks.append(("points", 4, f"{name} records the first {l['points']}-point game of his @NHL career {TAG}"))
    elif l["points"] == 3 and car["points"] > 3:
        checks.append(("points", 3, f"{name} records the first 3-point game of his @NHL career {TAG}"))
    if not checks:
        return facts
    rows = career_games(pid, game_id)
    for stat, n, text in checks:
        if not any(r[stat] >= n for r in rows):
            facts.append(text)
    return facts


def postgame_facts(s, lines, season, live=False):
    """live=True: called mid-game with only settled goals in `lines`; games-played, goalie and team facts wait for the final."""
    facts = []  # (tier, text)
    before = {}
    for l in lines["skaters"]:
        p = ensure_player(s, l, 1)
        before[l["pid"]] = {"career": dict(p["career"]) if p.get("career") else None, "season": dict(p.get("season", {})), "fr": dict(p["fr"])}
    gbefore = {}
    for l in lines["goalies"]:
        g = ensure_goalie(s, l, 1)
        gbefore[l["pid"]] = {"career": dict(g["career"]) if g.get("career") else None}
    pre_pools = {("skater", st): pool(s, "skater", st) for st in SKATER_LADDERS}
    pre_pools.update({("goalie", st): pool(s, "goalie", st) for st in GOALIE_LADDERS})
    hist_before = dict(s["hist"])
    season_rec_before = {stat: prior_season_record(s, stat, season) for stat in ("points", "goals", "assists")}
    franchise_max_before = {stat: max((p["fr"].get(stat, 0) for p in s["players"].values()), default=0) for stat in FRANCHISE_FIRST_STEP}
    team_before = [t for t in s["team_games"] if t["gameId"] != lines["id"]]

    apply_game(s, lines, +1)
    movers = [l["pid"] for l in lines["skaters"]]
    gmovers = [l["pid"] for l in lines["goalies"]]

    for l in lines["skaters"]:
        pid, p = l["pid"], s["players"][l["pid"]]
        name, b = p["name"], before[pid]
        car, carb = p.get("career"), b["career"]
        if car and carb is not None:
            if car["gamesPlayed"] == 1 and not live:
                facts.append((TIER["first"], f"{name} makes his @NHL debut {TAG}"))
            if l["goals"] and car["goals"] == l["goals"]:
                first = {1: "his first @NHL goal", 2: "his first two @NHL goals",
                         3: "a hat trick for his first three @NHL goals"}.get(l["goals"], f"his first {l['goals']} @NHL goals")
                facts.append((TIER["first"], f"{name} scores {first} {TAG}"))
            elif l["points"] and car["points"] == l["points"]:
                facts.append((TIER["first"], f"{name} records his first @NHL point {TAG}"))
            facts += [(TIER["first"], f) for f in career_first_facts(name, pid, l, car, lines["id"])]
            for stat, step in CAREER_STEP.items():
                if live and stat == "gamesPlayed":
                    continue
                if car[stat] // step > carb[stat] // step:
                    n = (car[stat] // step) * step
                    if n == 0:
                        continue
                    if stat == "gamesPlayed":
                        facts.append((TIER["milestone"], f"{name} plays his {ordinal(n)} career @NHL game {TAG}"))
                    else:
                        k = kth_in_history(s, pid, stat, n)
                        if k:
                            facts.append((TIER["milestone"], f"{name} becomes the {ordinal(k)} player in @NHL history to record {n} career {STAT_WORD[stat]} {TAG}"))
                        else:
                            facts.append((TIER["milestone"], f"{name} records his {ordinal(n)} career @NHL {UNIT[stat][0]} {TAG}"))
            p["pid"] = pid
            for stat in NHL_CUTOFF:
                if live and stat == "gamesPlayed":
                    continue
                facts += [(TIER["nhl"], f) for f in nhl_ladder_facts(s, p, stat, carb[stat], car[stat])]
            p.pop("pid", None)
        for stat, step in FRANCHISE_FIRST_STEP.items():
            fb, fa = b["fr"].get(stat, 0), p["fr"].get(stat, 0)
            if fa // step > fb // step:
                n = (fa // step) * step
                if n and franchise_max_before[stat] < n:
                    facts.append((TIER["record"], f"{name} becomes the first player in franchise history to reach {n} {STAT_WORD[stat]} {TAG}"))
        if l["goals"] >= 3:
            k = hist_before["hat"] + 1
            if hist_before["maxg"] and l["goals"] > hist_before["maxg"]:
                facts.append((TIER["record"], f"{name} scores {l['goals']} goals, a franchise single-game record {TAG}"))
            elif l["goals"] == hist_before["maxg"] and l["goals"] > 3:
                facts.append((TIER["record"], f"{name} scores {l['goals']} goals, tying the franchise single-game record {TAG}"))
            else:
                facts.append((TIER["record"], f"{name} records the {ordinal(k)} hat trick in franchise history {TAG}"))
        if l["points"] >= 4:
            k = hist_before["p4"] + 1
            if hist_before["maxp"] and l["points"] > hist_before["maxp"]:
                facts.append((TIER["record"], f"{name} records {l['points']} points, a franchise single-game record {TAG}"))
            else:
                facts.append((TIER["record"], f"{name}'s {l['points']}-point game is the {ordinal(k)} 4+ point game in franchise history {TAG}"))
        sb, sa = b["season"], p["season"]
        for stat, levels in SEASON_THRESHOLDS.items():
            for n in levels:
                if sb.get(stat, 0) < n <= sa.get(stat, 0):
                    prior = sum(1 for x in p.get("seasons", []) if x["season"] < season and x.get(stat, 0) >= n)
                    k = prior + 1
                    word = "goal" if stat == "goals" else "point"
                    facts.append((TIER["milestone"], f"{name} reaches the {n} {word} mark for the {ordinal(k) if k > 1 else 'first'} time {TAG}"))
        for stat in ("points", "goals", "assists"):
            best, who = season_rec_before[stat]
            if best and sb.get(stat, 0) <= best < sa.get(stat, 0):
                facts.append((TIER["record"], f"{name} sets the single-season franchise record for {STAT_WORD[stat]} ({sa[stat]}) {TAG}"))
            elif best and sb.get(stat, 0) < best == sa.get(stat, 0) and name not in who:
                facts.append((TIER["record"], f"{name} ties the single-season franchise record for {STAT_WORD[stat]} ({best}) {TAG}"))
    # franchise ladders, whole-ladder diff; a pair that swapped within the last 7 days stays quiet
    pairs = s["ledger"].setdefault("pairs", {})
    gdate = dt.date.fromisoformat(lines["date"])
    for stat, label in SKATER_LADDERS.items():
        if live and stat in ("gamesPlayed", "penaltyMinutes"):
            continue
        tier = TIER["ladder"] if stat in ("points", "goals", "assists") else TIER["ladder2"]
        for item in ladder_pass_facts(pre_pools[("skater", stat)], pool(s, "skater", stat), movers,
                                      RANK_LIMIT[stat], label, TIE_LIMIT.get(stat, 0)):
            if isinstance(item, tuple):
                text, key = item
                last = pairs.get(key)
                if last and (gdate - dt.date.fromisoformat(last)).days < 7:
                    continue
                pairs[key] = lines["date"]
                facts.append((tier, text))
            else:
                facts.append((tier, item))
    if live:
        facts.sort(key=lambda x: x[0])
        return [f for _, f in facts]
    for l in lines["goalies"]:
        pid, g = l["pid"], s["goalies"][l["pid"]]
        name, b = g["name"], gbefore[pid]
        car, carb = g.get("career"), b["career"]
        if car and carb is not None:
            if l["win"] and car["wins"] == 1:
                facts.append((TIER["first"], f"{name} records his first @NHL win {TAG}"))
            if l["shutout"] and car["shutouts"] == 1:
                facts.append((TIER["first"], f"{name} records his first @NHL shutout {TAG}"))
            for stat, step in GOALIE_STEP.items():
                if car[stat] // step > carb[stat] // step:
                    n = (car[stat] // step) * step
                    if n:
                        w = {"wins": "win", "shutouts": "shutout", "gamesPlayed": "game"}[stat]
                        facts.append((TIER["milestone"], f"{name} records his {ordinal(n)} career @NHL {w} {TAG}"))
    for stat, label in GOALIE_LADDERS.items():
        for item in ladder_pass_facts(pre_pools[("goalie", stat)], pool(s, "goalie", stat), gmovers,
                                      GOALIE_RANK_LIMIT[stat], label, GOALIE_TIE_LIMIT.get(stat, 0)):
            facts.append((TIER["goalie"], item[0] if isinstance(item, tuple) else item))
    facts += [(TIER["team"], f) for f in team_facts(s, lines, team_before)]
    facts.sort(key=lambda x: x[0])
    return [f for _, f in facts]


def streaks(games):
    """Return (win_streak, home_win_streak, point_streak) ending at the last game, within one season."""
    if not games:
        return 0, 0, 0
    season = games[-1]["season"]
    gs = [g for g in games if g["season"] == season]
    w = 0
    for g in reversed(gs):
        if g["win"]: w += 1
        else: break
    hw = 0
    for g in reversed([g for g in gs if g["home"]]):
        if g["win"]: hw += 1
        else: break
    ps = 0
    for g in reversed(gs):
        if g["win"] or g["otl"]: ps += 1
        else: break
    return w, hw, ps


def best_streaks(games):
    bw = bh = bp = 0
    for i in range(1, len(games) + 1):
        w, hw, ps = streaks(games[:i])
        bw, bh, bp = max(bw, w), max(bh, hw), max(bp, ps)
    return bw, bh, bp


def team_facts(s, lines, team_before):
    facts = []
    games = s["team_games"]
    w, hw, ps = streaks(games)
    bw, bh, bp = best_streaks(team_before)
    if w >= 4 and w > bw:
        facts.append(f"The {TEAM_HANDLE} extend their win streak to {w} games, the longest in franchise history {TAG}")
    elif w >= 4 and w == bw:
        facts.append(f"The {TEAM_HANDLE} extend their win streak to {w} games, tying the longest in franchise history {TAG}")
    if lines["home"] and hw >= 5 and hw > bh:
        facts.append(f"The {TEAM_HANDLE} extend their home win streak to {hw} games, the longest in franchise history {TAG}")
    if ps >= 6 and ps > bp and not (w >= 4 and w > bw):
        facts.append(f"The {TEAM_HANDLE} extend their point streak to {ps} games, the longest in franchise history {TAG}")
    # best start through N games
    season = lines["season"]
    cur = [g for g in games if g["season"] == season]
    n = len(cur)
    if n in (10, 20, 30, 41, 50, 60, 70, 82):
        pts = sum(g["pts"] for g in cur)
        prior = {}
        for g in games:
            if g["season"] < season:
                prior.setdefault(g["season"], []).append(g["pts"])
        prior_best = max((sum(v[:n]) for v in prior.values() if len(v) >= n), default=None)
        if prior_best is not None and pts > prior_best:
            facts.append(f"{pts} points through {n} games, the best start in franchise history {TAG}")
    return facts


def pregame_facts(s, pids_in_lineup):
    """[(text, pid)] for tonight's lineup. The pid lets a later lineup check flag a tweet about a scratched player."""
    facts = []
    alerts = s["ledger"]["alerts"]
    skaters = [pid for pid in pids_in_lineup if pid in s["players"]]
    pre = pool(s, "skater", "gamesPlayed")
    post = {pid: (n, v + (1 if pid in skaters else 0)) for pid, (n, v) in pre.items()}
    for pid in skaters:
        facts += [(f, pid) for f in ladder_pass_facts(pre, post, [pid], RANK_LIMIT["gamesPlayed"], "games played", 0, pregame=True)]
    for pid in skaters:
        p = s["players"][pid]
        name, car = p["name"], p.get("career")
        if not car:
            continue
        if (car["gamesPlayed"] + 1) % 100 == 0:
            facts.append((f"{name} plays his {ordinal(car['gamesPlayed'] + 1)} career @NHL game tonight {TAG}", pid))
        for stat, window in APPROACH_WINDOW.items():
            step = CAREER_STEP[stat]
            nxt = ((car[stat] // step) + 1) * step
            gap = nxt - car[stat]
            if gap > window:
                continue
            key = f"{pid}:{stat}:{nxt}"
            st = alerts.setdefault(key, {"posted": []})
            if gap in st["posted"] or (st["posted"] and gap != 1):
                continue
            unit = UNIT[stat][0] if gap == 1 else UNIT[stat][1]
            facts.append((f"{name} is {gap} {unit} away from {nxt} career @NHL {STAT_WORD[stat]} {TAG}", pid))
            st["posted"].append(gap)
    for pid in pids_in_lineup:
        g = s["goalies"].get(pid)
        if g and g.get("career") and (g["career"]["gamesPlayed"] + 1) % 100 == 0:
            facts.append((f"{g['name']} is set to play his {ordinal(g['career']['gamesPlayed'] + 1)} career @NHL game {TAG}", pid))
    return facts


# ----------------------------------------------------------------------------- commands

def game_header(lines):
    return f"{TEAM} {lines['us']}, {lines['opp']} {lines['them']} ({lines['date']})" if lines["win"] else f"{lines['opp']} {lines['them']}, {TEAM} {lines['us']} ({lines['date']})"


def surface(s, facts, header, push=True, limit=None):
    """Print new tweets, push each one to the phone with a one-tap compose link, and remember them so none repeats.
    Returns the number surfaced."""
    seen = s["ledger"]["surfaced"]
    new = [f for f in dict.fromkeys(facts) if f not in seen][:limit]
    if not new:
        return 0
    print(header)
    for f in new:
        seen[f] = {"at": now_utc().isoformat(), "game": header}
        print()
        print(f)
        if push:
            notify(header, f, click=compose_url(f))
    print()
    return len(new)


def maybe_reconcile(s, sched):
    """Overwrite running totals from the NHL aggregates once they've had time to settle (6h after the last final)."""
    rs = [g for g in sched["games"] if g["gameType"] == 2 and g.get("gameState") in ("OFF", "FINAL")]
    if not rs:
        return False
    last = max(dt.datetime.fromisoformat(g["startTimeUTC"].replace("Z", "+00:00")) for g in rs) + dt.timedelta(hours=3)
    rec = dt.datetime.fromisoformat(s["meta"]["reconciled_at"])
    if now_utc() - last < dt.timedelta(hours=6) or rec > last:
        return False
    fresh = seed(keep=s)
    fresh["ledger"] = s["ledger"]
    fresh["games"] = s.get("games", {})
    s.clear()
    s.update(fresh)
    return True


def injured(rs_unused=None):
    """Normalized names of Utah players ESPN lists as Out, Day-To-Day or on IR. Empty if the feed is down."""
    try:
        data = get(ESPN_INJURIES)
    except Exception:  # noqa
        return set()
    for t in data.get("injuries", []):
        if "Utah" in t.get("displayName", ""):
            return {norm_name(i["athlete"]["displayName"]) for i in t.get("injuries", [])}
    return set()


def projected_lineup(s, rs):
    """Skaters who dressed in the last final game, minus anyone on the injury report. Goalies rotate, so none."""
    done = [g for g in rs if g.get("gameState") in ("OFF", "FINAL")]
    if not done:
        return []
    last = max(done, key=lambda g: g["startTimeUTC"])
    lines = utah_lines(get(f"{WEB}/gamecenter/{last['id']}/boxscore"))
    hurt = injured()
    return [l["pid"] for l in lines["skaters"]
            if norm_name(s["players"].get(l["pid"], {}).get("name", l["short"])) not in hurt]


def game_label(g):
    home = g["homeTeam"]["abbrev"] == TEAM
    return f"{'vs' if home else 'at'} {g['awayTeam']['abbrev'] if home else g['homeTeam']['abbrev']}"


def official_lineup(gid):
    pbp = get(f"{WEB}/gamecenter/{gid}/play-by-play")
    return [str(r["playerId"]) for r in pbp.get("rosterSpots", []) if r.get("teamId") == TEAM_ID]


def pregame_pass(s, rs):
    """Morning: projected lineup (last game minus injuries). Once the NHL posts the lineup: anything new, plus a
    heads-up for any morning tweet about a player who isn't dressing."""
    led = s["ledger"]
    early = led.setdefault("pregame_early", {})
    now, today = local_now(), local_now().date().isoformat()
    total, changed = 0, False
    for g in rs:
        if g["gameDate"] != today or g.get("gameState") not in ("FUT", "PRE", "LIVE", "CRIT"):
            continue
        gid, start = str(g["id"]), parse_utc(g["startTimeUTC"])
        header = f"Pregame, {TEAM} {game_label(g)} ({today})"
        if g["id"] in led["pregame_done"]:
            continue
        spots = official_lineup(g["id"])
        if spots:
            for text, pid in early.get(gid, {}).items():
                if pid not in spots:
                    name = s["players"].get(pid, {}).get("name", "A player")
                    notify(f"{name} is not in tonight's lineup", f"Skip or delete this one: {text}", priority=4)
                    print(f"NOT IN LINEUP: {name}. Skip or delete: {text}\n")
            facts = pregame_facts(s, spots)
            total += surface(s, [t for t, _ in facts], header)
            led["pregame_done"].append(g["id"])
            changed = True
        elif gid not in early and (now.hour >= EARLY_PREGAME_HOUR or start - now_utc() <= dt.timedelta(hours=3)):
            facts = pregame_facts(s, projected_lineup(s, rs))
            early[gid] = {t: pid for t, pid in facts}
            total += surface(s, [t for t, _ in facts], header)
            changed = True
    return total, changed


def cmd_check(push=True):
    s = load_state()
    sched = get(f"{WEB}/club-schedule-season/{TEAM}/now")
    season = season_id_now(sched)
    rs = [g for g in sched["games"] if g["gameType"] == 2]
    changed = maybe_reconcile(s, sched)
    if changed:
        print("(reconciled totals from NHL aggregates)")
    changed |= ensure_summaries(s)
    processed = set(s["ledger"]["processed"])
    total = 0
    # postgame, oldest first, self-healing; waits SETTLE after the final so late scoring changes are in
    for g in sorted(rs, key=lambda g: g["gameDate"]):
        if g.get("gameState") in ("OFF", "FINAL") and g["id"] not in processed:
            fin = s["ledger"].setdefault("final_seen", {}).setdefault(str(g["id"]), now_utc().isoformat())
            if now_utc() - dt.datetime.fromisoformat(fin) < SETTLE and g.get("gameState") != "OFF":
                changed = True
                continue
            refresh_nhl(s)
            box = get(f"{WEB}/gamecenter/{g['id']}/boxscore")
            lines = utah_lines(box)
            facts = postgame_facts(s, lines, season)
            total += surface(s, facts, game_header(lines), push)
            s["ledger"]["processed"].append(g["id"])
            ensure_summaries(s)
            changed = True
    t, c = pregame_pass(s, rs)
    total, changed = total + t, changed or c
    t, c = daily_pass(s, rs, season, push)
    total, changed = total + t, changed or c
    if total == 0:
        nxt = next((g for g in sorted(rs, key=lambda g: g["gameDate"]) if g.get("gameState") in ("FUT", "PRE")), None)
        print("No new posts." + (f" Next game: {nxt['gameDate']} {nxt['awayTeam']['abbrev']} at {nxt['homeTeam']['abbrev']}." if nxt else ""))
    if changed or total:
        persist(s)
    return s, rs, season


# ----------------------------------------------------------------------------- live

def live_lines(s, land, goals):
    """Box-score-shaped lines built from settled Utah goals only."""
    per = {}
    for _, _, ours, g in goals:
        if not ours:
            continue
        sc = str(g["playerId"])
        d = per.setdefault(sc, {"goals": 0, "assists": 0, "ppg": 0})
        d["goals"] += 1
        d["ppg"] += 1 if g.get("strength") == "pp" else 0
        for a in g.get("assists", []):
            per.setdefault(str(a["playerId"]), {"goals": 0, "assists": 0, "ppg": 0})["assists"] += 1
    skaters = []
    for pid, d in per.items():
        if pid not in s["players"]:
            continue
        p = s["players"][pid]
        skaters.append({"pid": pid, "short": p["name"], "pos": p["pos"], "goals": d["goals"], "assists": d["assists"],
                        "points": d["goals"] + d["assists"], "pim": 0, "ppg": d["ppg"]})
    home = land["homeTeam"]["abbrev"] == TEAM
    return {"skaters": skaters, "goalies": [], "us": 0, "them": 0, "home": home,
            "opp": land["awayTeam" if home else "homeTeam"]["abbrev"], "win": False, "otl": False,
            "date": land["gameDate"], "id": land["id"], "season": int(land["season"])}


def live_facts(s, land, settled, season, all_goals, in_second_intermission):
    """Everything worth a tweet given the goals settled so far. Pure: works on a copy of state."""
    facts = []
    lines = live_lines(s, land, settled)
    if lines["skaters"]:
        facts += postgame_facts(copy.deepcopy(s), lines, season, live=True)
    gs = season_games(s, season)
    ours = [x for x in settled if x[2]]
    if all_goals and all_goals[0][2] and settled and settled[0][1] == all_goals[0][1]:
        f = split_fact([g for g in gs if g["first"] == "us"], "this season when scoring first")
        if f:
            facts.append(f[1])
    for pid in dict.fromkeys(str(g["playerId"]) for _, _, _, g in ours):
        name = s["players"].get(pid, {}).get("name")
        f = name and split_fact([g for g in gs if pid in g["scorers"]], f"this season when {name} scores", min_pct=0.75)
        if f:
            facts.append(f[1])
    if in_second_intermission:
        us = sum(1 for p, _, o, _ in all_goals if p <= 2 and o)
        them = sum(1 for p, _, o, _ in all_goals if p <= 2 and not o)
        if us > them:
            f = split_fact([g for g in gs if g["p2"][0] > g["p2"][1]], "this season when leading after two periods", min_pct=0.8)
            if f:
                facts.append(f[1])
    return facts


def cmd_live(s=None, rs=None, season=None, started=None):
    """Poll today's game every POLL_SECONDS; a goal is tweetable once its scorer and assists have held for SETTLE."""
    started = started or now_utc()
    if s is None:
        s, rs, season = cmd_check()
    g = live_game(rs)
    if not g or g["id"] in s["ledger"]["processed"]:  # state must be pre-game or every total double counts
        print("No game on.")
        return
    gid = str(g["id"])
    header = f"Live, {TEAM} {game_label(g)} ({g['gameDate']})"
    watch = s["ledger"].setdefault("live", {}).setdefault(gid, {})
    refresh_nhl(s)
    for pid in official_lineup(g["id"]):  # call-ups: learn pre-game career totals before they can score
        if pid not in s["players"] and pid not in s["goalies"]:
            try:
                land = get(f"{WEB}/player/{pid}/landing")
                if land.get("position") != "G":
                    ensure_player(s, {"pid": pid, "short": "", "points": 0, "goals": 0, "assists": 0}, 0)
            except Exception:  # noqa
                pass
    final_at = None
    print(f"watching {gid}")
    while now_utc() - started < LIVE_MAX:
        land = get(f"{WEB}/gamecenter/{gid}/landing")
        state = land.get("gameState")
        if state in ("FUT", "PRE"):
            time.sleep(60)
            continue
        goals = scoring_goals(land)
        now = now_utc()
        present = set()
        for _, eid, ours, gl in goals:
            key = str(eid)
            present.add(key)
            sig = f"{gl['playerId']}|{','.join(str(a['playerId']) for a in gl.get('assists', []))}"
            w = watch.get(key)
            if not w or w["sig"] != sig:
                watch[key] = {"sig": sig, "since": now.isoformat()}
        for key in [k for k in watch if k not in present]:
            del watch[key]  # overturned
        settled = [x for x in goals if now - dt.datetime.fromisoformat(watch[str(x[1])]["since"]) >= SETTLE]
        pd = land.get("periodDescriptor") or {}
        intermission2 = pd.get("number") == 2 and (land.get("clock") or {}).get("inIntermission")
        facts = live_facts(s, land, settled, season, goals, intermission2)
        if surface(s, facts, header, limit=LIVE_PER_POLL):
            persist(s, "live")
        if state in ("FINAL", "OFF"):
            final_at = final_at or now
            if now - final_at >= SETTLE:
                break
        time.sleep(POLL_SECONDS)
    persist(s, "live")
    if final_at:
        cmd_check()


def live_game(rs):
    """Today's Utah game if it's on, or starts within LIVE_LEAD."""
    for g in rs:
        st = g.get("gameState")
        if st in ("LIVE", "CRIT"):
            return g
        if st in ("FUT", "PRE") and parse_utc(g["startTimeUTC"]) - now_utc() <= LIVE_LEAD and parse_utc(g["startTimeUTC"]) > now_utc() - dt.timedelta(hours=6):
            return g
    return None


def cmd_tick():
    if os.environ.get("BOT_GIT") and not os.environ.get("NTFY_TOPIC"):
        sys.exit("NTFY_TOPIC is not set; refusing to surface tweets nobody would see")
    started = now_utc()
    s, rs, season = cmd_check()
    if live_game(rs):
        cmd_live(s, rs, season, started)


# ----------------------------------------------------------------------------- off-day

def fewest(r, noun):
    """'allow the 3rd-fewest shots per game' / 'are tied for the fewest shots allowed per game'."""
    tied, r = r.startswith("t-"), r.removeprefix("t-")
    core = "the fewest" if r == "1st" else f"the {r}-fewest"
    return f"are tied for {core} {noun} allowed per game" if tied else f"allow {core} {noun} per game"


TEAM_RANKS = [  # (field, higher is better, phrase builder)
    ("powerPlayPct", True, lambda r, v: f"The {TEAM_HANDLE} power play is {r} in the NHL at {v * 100:.1f}%"),
    ("penaltyKillPct", True, lambda r, v: f"The {TEAM_HANDLE} penalty kill is {r} in the NHL at {v * 100:.1f}%"),
    ("goalsForPerGame", True, lambda r, v: f"The {TEAM_HANDLE} are {r} in the NHL in goals per game ({v:.2f})"),
    ("goalsAgainstPerGame", False, lambda r, v: f"The {TEAM_HANDLE} {fewest(r, "goals")} in the NHL ({v:.2f})"),
    ("shotsForPerGame", True, lambda r, v: f"The {TEAM_HANDLE} are {r} in the NHL in shots per game ({v:.1f})"),
    ("shotsAgainstPerGame", False, lambda r, v: f"The {TEAM_HANDLE} {fewest(r, "shots")} in the NHL ({v:.1f})"),
    ("faceoffWinPct", True, lambda r, v: f"The {TEAM_HANDLE} are {r} in the NHL in faceoff percentage ({v * 100:.1f}%)"),
]
RANK_SCORE = {1: 10, 2: 7, 3: 5}


def daily_candidates(s, season):
    """Every off-day fact worth posting: (score, family, signature, text). Families cool down so the feed rotates."""
    out = []
    rows = rest_all("team/summary", f"seasonId={season} and gameTypeId=2")
    me = next((r for r in rows if r["teamId"] == TEAM_ID), None)
    if not me or me["gamesPlayed"] < 10:
        return out
    for field, hi, phrase in TEAM_RANKS:
        v = me[field]
        others = [(-r[field] if not hi else r[field]) for r in rows if r["teamId"] != TEAM_ID]
        lab, rk, _ = rank_label(-v if not hi else v, others)
        if rk in RANK_SCORE:
            out.append((RANK_SCORE[rk], f"rank:{field}", lab, f"{phrase(lab, v)} {TAG}"))
    st = next((r for r in get(f"{WEB}/standings/now")["standings"] if r["teamAbbrev"]["default"] == TEAM), None)
    if st and st["divisionSequence"] <= 2:
        out.append((6 if st["divisionSequence"] == 1 else 4, "standings", f"{st['divisionSequence']}:{st['points']}",
                    f"The {TEAM_HANDLE} sit {ordinal(st['divisionSequence'])} in the {st['divisionName']} with {st['points']} points through {st['gamesPlayed']} games {TAG}"))
    if me["gamesPlayed"] >= 15:
        pace = round(me["points"] / me["gamesPlayed"] * 82)
        by = {}
        for t in s["team_games"]:
            if t["season"] < season:
                by[t["season"]] = by.get(t["season"], 0) + t["pts"]
        best = max(by.values(), default=0)
        if best and pace > best:
            out.append((9, "pace", str(pace), f"The {TEAM_HANDLE} are on pace for {pace} points. The franchise record is {best} {TAG}"))
    gs = season_games(s, season)
    for fam, sub, phrase, pct in (("first", lambda g: g["first"] == "us", "this season when scoring first", 0.7),
                                  ("lead2", lambda g: g["p2"][0] > g["p2"][1], "this season when leading after two periods", 0.85),
                                  ("onegoal", lambda g: abs(g["reg"][0] - g["reg"][1]) <= 1 or g["otl"], "in one-goal games this season", 0.7),
                                  ("trail1", lambda g: g["p1"][0] < g["p1"][1], "this season when trailing after the first period", 0.6),
                                  ("home", lambda g: g["home"], "at home this season", 0.75),
                                  ("road", lambda g: not g["home"], "on the road this season", 0.7)):
        f = split_fact([g for g in gs if sub(g)], phrase, min_pct=pct)
        if f:
            out.append((f[0], f"split:{fam}", f[1], f[1]))
    for pid, p in s["players"].items():
        if not p.get("on_roster"):
            continue
        f = split_fact([g for g in gs if pid in g["scorers"]], f"this season when {p['name']} scores", min_pct=0.8)
        if f:
            out.append((f[0], f"scorer:{pid}", f[1], f[1]))
    out.sort(key=lambda x: -x[0])
    return out


def pick_daily(s, cands, today):
    """Best candidate whose family hasn't posted in 3 days and whose exact signature hasn't in 14."""
    fam = s["ledger"].setdefault("daily", {}).setdefault("fam", {})
    for score, family, sig, text in cands:
        last = fam.get(family)
        if last:
            days = (dt.date.fromisoformat(today) - dt.date.fromisoformat(last["at"])).days
            if days < 3 or (last["sig"] == sig and days < 14):
                continue
        if text in s["ledger"]["surfaced"]:
            continue
        return family, sig, text
    return None


def daily_pass(s, rs, season, push=True):
    """One off-day post, at DAILY_HOUR on days Utah doesn't play."""
    now = local_now()
    today = now.date().isoformat()
    d = s["ledger"].setdefault("daily", {})
    if now.hour < DAILY_HOUR or d.get("last") == today or any(g["gameDate"] == today for g in rs):
        return 0, False
    d["last"] = today
    pick = pick_daily(s, daily_candidates(s, season), today)
    if not pick:
        return 0, True
    family, sig, text = pick
    d.setdefault("fam", {})[family] = {"at": today, "sig": sig}
    return surface(s, [text], f"Off day ({today})", push), True


def cmd_daily():
    s = load_state()
    sched = get(f"{WEB}/club-schedule-season/{TEAM}/now")
    season = season_id_now(sched)
    ensure_summaries(s, quiet=False)
    cands = daily_candidates(s, season)
    pick = pick_daily(copy.deepcopy(s), cands, local_now().date().isoformat())
    for score, family, sig, text in cands:
        print(f"{score:5.1f}  {'>> ' if pick and pick[2] == text else '   '}{text}")


def cmd_live_replay(game_id):
    """Replay a finished game through the live watcher, assuming every goal settles in order."""
    s = load_state()
    box = get(f"{WEB}/gamecenter/{game_id}/boxscore")
    lines = utah_lines(box)
    if game_id in s["ledger"]["processed"]:
        apply_game(s, lines, -1)
    s["ledger"]["surfaced"] = {}
    s.get("games", {}).pop(str(game_id), None)
    season = lines["season"]
    land = get(f"{WEB}/gamecenter/{game_id}/landing")
    goals = scoring_goals(land)
    print(f"Live replay {game_header(lines)}\n")
    for i in range(1, len(goals) + 1):
        per, _, ours, g = goals[i - 1]
        who = g["name"]["default"]
        facts = live_facts(s, land, goals[:i], season, goals, False)
        new = [f for f in dict.fromkeys(facts) if f not in s["ledger"]["surfaced"]]
        print(f"P{per} {g.get('timeInPeriod')} {'UTA' if ours else 'opp'} goal ({who}) -> {len(new)} new")
        for f in new:
            s["ledger"]["surfaced"][f] = {}
            print(f"   {f}")
        if per == 2 and (i == len(goals) or goals[i][0] > 2):
            for f in live_facts(s, land, goals[:i], season, goals, True):
                if f not in s["ledger"]["surfaced"]:
                    s["ledger"]["surfaced"][f] = {}
                    print(f"   [2nd int] {f}")


def cmd_replay(game_id):
    s = load_state()
    box = get(f"{WEB}/gamecenter/{game_id}/boxscore")
    lines = utah_lines(box)
    apply_game(s, lines, -1)
    facts = postgame_facts(s, lines, lines["season"])
    print(game_header(lines))
    for i, f in enumerate(facts, 1):
        print(f"{i}. {f}")


def cmd_replay_season():
    s = seed()
    s["ledger"] = {"surfaced": {}, "processed": [], "pregame_done": [], "alerts": {}}
    sched = get(f"{WEB}/club-schedule-season/{TEAM}/now")
    season = season_id_now(sched)
    games = sorted([g for g in sched["games"] if g["gameType"] == 2 and g.get("gameState") in ("OFF", "FINAL")], key=lambda g: g["gameDate"])
    boxes = [utah_lines(get(f"{WEB}/gamecenter/{g['id']}/boxscore")) for g in games]
    for lines in reversed(boxes):
        apply_game(s, lines, -1)
    # start-of-season hist from prior seasons only
    s["hist"] = {"hat": s["hist"]["hat"], "p4": s["hist"]["p4"], "maxg": 0, "maxp": 0}
    prior = [r for r in rest_all("skater/summary", f"franchiseId={FRANCHISE} and gameTypeId=2", game=True) if r["gameId"] < season * 10000 + 0 or int(str(r["gameId"])[:4]) < season // 10000]
    s["hist"]["maxg"] = max((r["goals"] for r in prior), default=0)
    s["hist"]["maxp"] = max((r["points"] for r in prior), default=0)
    print(f"Replay {season}: {len(boxes)} games\n")
    for lines in boxes:
        pre = [t for t, _ in pregame_facts(s, [l["pid"] for l in lines["skaters"]] + [l["pid"] for l in lines["goalies"]])]
        if pre:
            print(f"Pregame ({lines['date']})")
            for i, f in enumerate(pre, 1):
                print(f"{i}. {f}")
            print()
        facts = postgame_facts(s, lines, season)
        print(game_header(lines))
        for i, f in enumerate(facts, 1):
            print(f"{i}. {f}")
        print()


def cmd_status():
    s = load_state()
    m = s["meta"]
    print(f"season {m.get('season')} seeded {m.get('seeded_at')} reconciled {m.get('reconciled_at')} last_check {m.get('last_check')}")
    print(f"players {len(s['players'])} goalies {len(s['goalies'])} processed {len(s['ledger']['processed'])} surfaced {len(s['ledger']['surfaced'])}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "seed":
        keep = load_state() if os.path.exists(STATE_PATH) else None
        save_state(seed(keep))
        print("seeded")
    elif cmd == "check":
        cmd_check()
    elif cmd == "tick":
        cmd_tick()
    elif cmd == "live":
        cmd_live()
    elif cmd == "daily":
        cmd_daily()
    elif cmd == "live-replay":
        cmd_live_replay(int(sys.argv[2]))
    elif cmd == "backfill":
        st = load_state()
        ensure_summaries(st, quiet=False)
        save_state(st)
    elif cmd == "replay":
        cmd_replay(int(sys.argv[2]))
    elif cmd == "replay-season":
        cmd_replay_season()
    else:
        cmd_status()
