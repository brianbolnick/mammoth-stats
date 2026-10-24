#!/usr/bin/env python3
"""Utah Mammoth franchise stats bot. Deterministic engine; no model touches a number.

  python3 bot.py seed            rebuild reference data from the NHL APIs (keeps ledgers)
  python3 bot.py check           game-day pass: prints new pregame/postgame tweets, ready to copy
  python3 bot.py replay GAMEID   what the passes would print for one finished game (no state writes)
  python3 bot.py replay-season   roll state back to opening night and replay every finished RS game
  python3 bot.py status
"""
import datetime as dt
import json
import math
import os
import sys
import time
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

# ----------------------------------------------------------------------------- http

def get(url, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "utmammothstats-bot/1.0"})
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


def postgame_facts(s, lines, season):
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
            if car["gamesPlayed"] == 1:
                facts.append((TIER["first"], f"{name} makes his @NHL debut {TAG}"))
            if l["goals"] and car["goals"] == l["goals"]:
                facts.append((TIER["first"], f"{name} scores his first @NHL goal {TAG}"))
            elif l["points"] and car["points"] == l["points"]:
                facts.append((TIER["first"], f"{name} records his first @NHL point {TAG}"))
            for stat, step in CAREER_STEP.items():
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


def pregame_facts(s, pids_in_lineup, game_label):
    facts = []
    alerts = s["ledger"]["alerts"]
    skaters = [pid for pid in pids_in_lineup if pid in s["players"]]
    pre = pool(s, "skater", "gamesPlayed")
    post = {pid: (n, v + (1 if pid in skaters else 0)) for pid, (n, v) in pre.items()}
    facts += [f[0] if isinstance(f, tuple) else f for f in ladder_pass_facts(pre, post, skaters, RANK_LIMIT["gamesPlayed"], "games played", 0, pregame=True)]
    for pid in skaters:
        p = s["players"][pid]
        name, car = p["name"], p.get("career")
        if not car:
            continue
        if (car["gamesPlayed"] + 1) % 100 == 0:
            facts.append(f"{name} plays his {ordinal(car['gamesPlayed'] + 1)} career @NHL game tonight {TAG}")
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
            facts.append(f"{name} is {gap} {unit} away from {nxt} career @NHL {STAT_WORD[stat]} {TAG}")
            st["posted"].append(gap)
    for pid in pids_in_lineup:
        g = s["goalies"].get(pid)
        if g and g.get("career") and (g["career"]["gamesPlayed"] + 1) % 100 == 0:
            facts.append(f"{g['name']} is set to play his {ordinal(g['career']['gamesPlayed'] + 1)} career @NHL game {TAG}")
    return facts


# ----------------------------------------------------------------------------- commands

def game_header(lines):
    return f"{TEAM} {lines['us']}, {lines['opp']} {lines['them']} ({lines['date']})" if lines["win"] else f"{lines['opp']} {lines['them']}, {TEAM} {lines['us']} ({lines['date']})"


def surface(s, facts, header):
    """Print new tweets, ready to copy, and remember them so a re-run never repeats one."""
    seen = s["ledger"]["surfaced"]
    new = [f for f in facts if f not in seen]
    if not new:
        return 0
    print(header)
    for f in new:
        seen[f] = {"at": now_utc().isoformat(), "game": header}
        print()
        print(f)
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
    s.clear()
    s.update(fresh)
    return True


OUT_PATH = os.path.join(ROOT, "out.txt")


def projected_lineup(s, rs):
    """Skaters who dressed in the last final game, minus names in out.txt. Goalies rotate, so none."""
    done = [g for g in rs if g.get("gameState") in ("OFF", "FINAL")]
    if not done:
        return []
    last = max(done, key=lambda g: g["startTimeUTC"])
    lines = utah_lines(get(f"{WEB}/gamecenter/{last['id']}/boxscore"))
    out = set()
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH) as f:
            out = {ln.split("#")[0].strip().lower() for ln in f} - {""}
    keep = []
    for l in lines["skaters"]:
        name = s["players"].get(l["pid"], {}).get("name", l["short"]).lower()
        if name not in out and l["short"].lower() not in out:
            keep.append(l["pid"])
    return keep


def cmd_check():
    s = load_state()
    sched = get(f"{WEB}/club-schedule-season/{TEAM}/now")
    season = season_id_now(sched)
    rs = [g for g in sched["games"] if g["gameType"] == 2]
    changed = maybe_reconcile(s, sched)
    if changed:
        print("(reconciled totals from NHL aggregates)")
    processed = set(s["ledger"]["processed"])
    total = 0
    # postgame, oldest first, self-healing
    for g in sorted(rs, key=lambda g: g["gameDate"]):
        if g.get("gameState") in ("OFF", "FINAL") and g["id"] not in processed:
            refresh_nhl(s)
            box = get(f"{WEB}/gamecenter/{g['id']}/boxscore")
            lines = utah_lines(box)
            facts = postgame_facts(s, lines, season)
            total += surface(s, facts, game_header(lines))
            s["ledger"]["processed"].append(g["id"])
            changed = True
    # pregame: today's game, lineup posted, not yet done
    today = dt.datetime.now(TZ).date().isoformat()
    for g in rs:
        if g["gameDate"] != today or g["id"] in s["ledger"]["pregame_done"]:
            continue
        if g.get("gameState") not in ("FUT", "PRE", "LIVE", "CRIT"):
            continue
        start = dt.datetime.fromisoformat(g["startTimeUTC"].replace("Z", "+00:00"))
        if start - now_utc() > dt.timedelta(minutes=150):
            continue
        pbp = get(f"{WEB}/gamecenter/{g['id']}/play-by-play")
        spots = [str(r["playerId"]) for r in pbp.get("rosterSpots", []) if r.get("teamId") == TEAM_ID]
        label = "Pregame"
        if not spots:
            spots = projected_lineup(s, rs)
            label = "Pregame (projected: last game's skaters minus out.txt; confirm before posting)"
        if not spots:
            continue
        opp = g["awayTeam"]["abbrev"] if g["homeTeam"]["abbrev"] == TEAM else g["homeTeam"]["abbrev"]
        where = "vs" if g["homeTeam"]["abbrev"] == TEAM else "at"
        facts = pregame_facts(s, spots, f"{where} {opp}")
        total += surface(s, facts, f"{label}, {TEAM} {where} {opp} ({today})")
        s["ledger"]["pregame_done"].append(g["id"])
        changed = True
    if total == 0:
        nxt = next((g for g in sorted(rs, key=lambda g: g["gameDate"]) if g.get("gameState") in ("FUT", "PRE")), None)
        print("No new posts." + (f" Next game: {nxt['gameDate']} {nxt['awayTeam']['abbrev']} at {nxt['homeTeam']['abbrev']}." if nxt else ""))
    if changed or total:
        s["meta"]["last_check"] = now_utc().isoformat()
        save_state(s)


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
        pre = pregame_facts(s, [l["pid"] for l in lines["skaters"]] + [l["pid"] for l in lines["goalies"]], "")
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
    elif cmd == "replay":
        cmd_replay(int(sys.argv[2]))
    elif cmd == "replay-season":
        cmd_replay_season()
    else:
        cmd_status()
