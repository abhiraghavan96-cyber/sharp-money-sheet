"""Pull the raw inputs for one run straight from the public sources (no browser, no Claude).

python3 fetch.py refresh in_dir   -> espn.json, context.json, kalshi_*.json, tickets_*.json
python3 fetch.py grade in_dir     -> espn.json (yesterday and today, with final scores)
python3 fetch.py backfill in_dir  -> hist_new.json (every finished game this season not yet in hist.json)
Refresh and grade also write results_new.json and hist_new.json for games that just finished.

Every source is optional: a source that fails is logged and skipped, and the page shows it as missing.
"""
import json, os, re, sys, time, unicodedata, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from merge import cands  # noqa: E402  (Kalshi team-code aliases)

UA = "SharpMoneySheet/1.0 (personal research project)"
PT = ZoneInfo("America/Los_Angeles")
SPORT = {"nba": "basketball", "nfl": "football"}
KALSHI = "https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker={s}&status=open&limit=1000"
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def get(url, as_json=True, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, text/html"})
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8")
            return json.loads(body) if as_json else body
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def num(s):
    if s is None:
        return None
    s = str(s).strip()
    if s.upper() == "EVEN":
        return 100.0
    s = re.sub(r"^[ouOU]", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def iso(d):
    return datetime.fromisoformat(d.replace("Z", "+00:00")).strftime("%Y-%m-%dT%H:%M:%SZ")


def pt_date(d):
    return datetime.fromisoformat(d.replace("Z", "+00:00")).astimezone(PT).strftime("%Y-%m-%d")


def oc(x):
    """[open line, open odds, close line, close odds] from ESPN's {open: {line, odds}, close: {...}}, or None."""
    if not x or not x.get("close"):
        return None
    op = x["open"] if x.get("open") and x["open"].get("line") is not None else x["close"]
    vals = [num(op.get("line")), num(op.get("odds")), num(x["close"].get("line")), num(x["close"].get("odds"))]
    return None if any(v is None for v in vals) else vals


def espn(dates, leagues=("nba", "nfl")):
    out, seen = [], set()
    for lg in leagues:
        for d in dates:
            try:
                j = get(f"https://site.api.espn.com/apis/site/v2/sports/{SPORT[lg]}/{lg}/scoreboard?dates={d}")
            except Exception as e:  # noqa: BLE001
                print("espn scoreboard failed", lg, d, e)
                continue
            for e in j.get("events", []):
                if e["id"] in seen:
                    continue
                seen.add(e["id"])
                c = e["competitions"][0]
                h = next(x for x in c["competitors"] if x["homeAway"] == "home")
                a = next(x for x in c["competitors"] if x["homeAway"] == "away")
                o = (c.get("odds") or [{}])[0]
                v = c.get("venue") or {}
                ad = v.get("address") or {}
                g = {"league": lg.upper(), "date": pt_date(e["date"]), "id": e["id"], "start": iso(e["date"]),
                     "status": e["status"]["type"]["name"],
                     "away": a["team"]["abbreviation"], "awayName": a["team"]["displayName"], "awayId": a["team"].get("id"),
                     "home": h["team"]["abbreviation"], "homeName": h["team"]["displayName"], "homeId": h["team"].get("id"),
                     "awayScore": float(a.get("score") or 0), "homeScore": float(h.get("score") or 0),
                     "indoor": v.get("indoor"), "city": ad.get("city"), "state": ad.get("state"), "country": ad.get("country"),
                     "stype": (e.get("season") or {}).get("type"), "neutral": bool(c.get("neutralSite"))}
                q = oc
                ps, t = o.get("pointSpread") or {}, o.get("total") or {}
                if q(ps.get("home")) and q(ps.get("away")):
                    g["spread"] = {"home": q(ps["home"]), "away": q(ps["away"])}
                if q(t.get("over")) and q(t.get("under")):
                    g["total"] = {"over": q(t["over"]), "under": q(t["under"])}
                out.append(g)
    return out


def context(games):
    out = {}
    for g in games:
        if g["status"] != "STATUS_SCHEDULED":
            continue
        lg = g["league"].lower()
        try:
            s = get(f"https://site.api.espn.com/apis/site/v2/sports/{SPORT[lg]}/{lg}/summary?event={g['id']}")
        except Exception as e:  # noqa: BLE001
            print("espn summary failed", g["id"], e)
            continue
        teams = {}
        for c in s["header"]["competitions"][0]["competitors"]:
            ab = c["team"]["abbreviation"]
            inj = next((t for t in s.get("injuries", []) if (t.get("team") or {}).get("abbreviation") == ab), {"injuries": []})
            st = lambda i: (i.get("status") or "").lower()
            out_ = [i["athlete"]["displayName"] for i in inj.get("injuries", []) if st(i) in ("out", "doubtful")]
            out_ath = [(i["athlete"].get("id"), i["athlete"]["displayName"], ((i["athlete"].get("position") or {}).get("abbreviation") or ""))
                       for i in inj.get("injuries", []) if st(i) in ("out", "doubtful")]
            qq = [i["athlete"]["displayName"] for i in inj.get("injuries", []) if st(i) == "questionable"]
            lt = next((t for t in s.get("leaders", []) if (t.get("team") or {}).get("abbreviation") == ab), {"leaders": []})
            lead = []
            for x in lt.get("leaders", []):
                if re.search(r"tackle|sack|interception", x.get("name", ""), re.I):
                    continue
                if x.get("leaders") and x["leaders"][0].get("athlete"):
                    n = x["leaders"][0]["athlete"]["displayName"]
                    if n not in lead:
                        lead.append(n)
            l5 = next((t for t in s.get("lastFiveGames", []) if (t.get("team") or {}).get("abbreviation") == ab), {"events": []})
            dates = sorted(e["gameDate"] for e in l5.get("events", []) if e.get("gameDate"))
            last = dates[-1] if dates else None
            last_ev = next((e for e in l5.get("events", []) if e.get("gameDate") == last), {})
            rec = next((r.get("summary") for r in c.get("record", []) if r.get("type") == "total"), None)
            teams[c["homeAway"]] = {"ab": ab, "rec": rec, "last": last, "lastAtVs": last_ev.get("atVs"), "recent": dates,
                                    "out": out_[:6], "q": qq[:4], "lead": lead,
                                    "coreOut": [n for n in lead if n in out_], "coreQ": [n for n in lead if n in qq]}
            teams[c["homeAway"]].update(lost_production(lg, out_ath, lead))
        p = s.get("predictor") or {}
        pred = None
        try:
            pred = {"home": float(p["homeTeam"]["gameProjection"]), "away": float(p["awayTeam"]["gameProjection"])}
        except (KeyError, TypeError, ValueError):
            pass
        out[g["id"]] = {"pred": pred, "home": teams.get("home"), "away": teams.get("away")}
    return out


# ---------- player game logs ----------
NBA_COLS = ["minutes", "points", "totalRebounds", "assists", "threePointFieldGoalsMade-threePointFieldGoalsAttempted"]
NFL_COLS = ["passingYards", "rushingYards", "receivingYards", "receptions"]
_LOGS = {}


def season_ids(lg):
    t = datetime.now(PT).date()
    cur = (t.year + 1 if t.month >= 8 else t.year) if lg == "nba" else (t.year if t.month >= 3 else t.year - 1)
    return cur, cur - 1


def stat_num(x):
    s = str(x if x is not None else "").replace(",", "").strip()
    if ":" in s:
        m, sec = s.split(":", 1)
        try:
            return round(int(m) + int(sec) / 60, 1)
        except ValueError:
            return 0
    if "-" in s[1:]:
        s = s.split("-")[0] if not s.startswith("-") else "-" + s[1:].split("-")[0]
    try:
        v = float(s)
    except ValueError:
        return 0
    return int(v) if v.is_integer() else round(v, 1)


def gamelog(lg, aid, prev=False):
    """One player's regular-season and playoff games, newest first, as rows of NBA_COLS or NFL_COLS."""
    key = (lg, aid, prev)
    if key in _LOGS:
        return _LOGS[key]
    url = f"https://site.web.api.espn.com/apis/common/v3/sports/{SPORT[lg]}/{lg}/athletes/{aid}/gamelog"
    if prev:
        url += f"?season={season_ids(lg)[1]}"
    rows = []
    try:
        j = get(url, tries=2)
        names, meta = j.get("names") or [], j.get("events") or {}
        cols = NBA_COLS if lg == "nba" else NFL_COLS
        for stype in j.get("seasonTypes") or []:
            dn = stype.get("displayName") or ""
            if "Regular" not in dn and "Postseason" not in dn:
                continue
            for cat in stype.get("categories") or []:
                if cat.get("type") not in (None, "event"):
                    continue
                for ev in cat.get("events") or []:
                    stats = ev.get("stats") or []
                    if len(stats) != len(names):
                        continue
                    d = dict(zip(names, stats))
                    rows.append(((meta.get(ev.get("eventId")) or {}).get("gameDate") or "", [stat_num(d.get(c)) for c in cols]))
    except Exception as e:  # noqa: BLE001
        print("espn gamelog failed", lg, aid, prev, e)
    rows.sort(key=lambda r: r[0], reverse=True)
    out = [r[1] for r in rows]
    _LOGS[key] = out
    return out


def lost_production(lg, out_ath, lead):
    """How much of a team is missing. NBA: points per game of players listed out or doubtful.
    NFL: 1.0 for the starting QB (the team's passing leader), 0.35 for other team leaders, 0.05 for anyone else."""
    if lg == "nfl":
        units, detail = 0.0, []
        for _, name, pos in out_ath:
            u = 1.0 if (pos == "QB" and name in lead) else 0.35 if name in lead else 0.05
            units += u
            if u >= 0.35:
                detail.append([name, pos])
        return {"lostUnits": round(units, 2), "lostDetail": detail[:4]}
    total, detail = 0.0, []
    for aid, name, _ in out_ath:
        if not aid:
            continue
        rows = gamelog(lg, aid)
        if len(rows) < 3:
            rows = rows + gamelog(lg, aid, prev=True)[:20]
        if not rows:
            continue
        ppg = sum(r[1] for r in rows) / len(rows)
        total += ppg
        if ppg >= 5:
            detail.append([name, round(ppg, 1)])
    detail.sort(key=lambda x: -x[1])
    return {"lostPts": round(total, 1), "lostDetail": detail[:4]}


def standings(lg):
    """Season points for and against per game, by team abbreviation."""
    try:
        j = get(f"https://site.api.espn.com/apis/v2/sports/{SPORT[lg]}/{lg}/standings")
    except Exception as e:  # noqa: BLE001
        print("espn standings failed", lg, e)
        return {}
    out = {}

    def walk(o):
        for ch in o.get("children") or []:
            walk(ch)
        for e in (o.get("standings") or {}).get("entries") or []:
            st = {s.get("name"): s.get("value") for s in e.get("stats", [])}
            gp = sum(int(st.get(k) or 0) for k in ("wins", "losses", "ties"))
            if not gp:
                continue
            pf = st.get("avgPointsFor") if st.get("avgPointsFor") is not None else (st.get("pointsFor") or 0) / gp
            pa = st.get("avgPointsAgainst") if st.get("avgPointsAgainst") is not None else (st.get("pointsAgainst") or 0) / gp
            out[e["team"]["abbreviation"]] = {"gp": gp, "pf": round(float(pf), 2), "pa": round(float(pa), 2)}
    walk(j)
    return out


# ---------- player props (Kalshi ladders + ESPN game logs) ----------
PROP_SERIES = {"nba": {"pts": "KXNBAPTS", "reb": "KXNBAREB", "ast": "KXNBAAST", "tpm": "KXNBA3PT", "pra": "KXNBAPRA"},
               "nfl": {"passYds": "KXNFLPASSYDS", "rushYds": "KXNFLRSHYDS", "recYds": "KXNFLRECYDS", "rec": "KXNFLREC"}}


def norm_name(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().replace("-", " ")
    parts = [p for p in re.sub(r"[^a-z ]", "", s).split() if p not in ("jr", "sr", "ii", "iii", "iv", "v")]
    return " ".join(parts)


def roster(lg, tid):
    try:
        j = get(f"https://site.api.espn.com/apis/site/v2/sports/{SPORT[lg]}/{lg}/teams/{tid}/roster", tries=2)
    except Exception as e:  # noqa: BLE001
        print("espn roster failed", lg, tid, e)
        return []
    out = []
    for a in j.get("athletes") or []:
        for x in (a.get("items") if "items" in a else [a]):
            inj = x.get("injuries") or []
            out.append({"id": x.get("id"), "name": x.get("displayName") or x.get("fullName") or "",
                        "jersey": str(x.get("jersey") or ""), "pos": (x.get("position") or {}).get("abbreviation"),
                        "status": inj[0].get("status") if inj else None})
    return out


def match_player(name, ros, pcode):
    n = norm_name(name)
    hit = [r for r in ros if norm_name(r["name"]) == n]
    if not hit and n:
        last = n.split()[-1]
        hit = [r for r in ros if norm_name(r["name"]).split()[-1:] == [last]]
        if len(hit) > 1:
            jersey = re.sub(r"^\D+", "", pcode)
            hit = [r for r in hit if r["jersey"] == jersey] or [r for r in hit if norm_name(r["name"])[:1] == n[:1]]
    return hit[0] if len(hit) == 1 else None


def kalshi_markets(series):
    out, cursor = [], ""
    for _ in range(6):
        url = KALSHI.format(s=series) + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        j = get(url, tries=2)
        ms = j.get("markets", [])
        out += ms
        cursor = j.get("cursor") or ""
        if not cursor or len(ms) < 1000:
            break
    return out


def kalshi_props(lg, codes):
    """{(event key, player code): {"name", "ladders": {stat: [[strike, bid, ask], ...]}}}; strike x means 'more than x'."""
    res = {}
    for stat, series in PROP_SERIES[lg].items():
        try:
            ms = kalshi_markets(series)
        except Exception as e:  # noqa: BLE001
            print("kalshi props failed", series, e)
            continue
        n = 0
        for m in ms:
            ev = m.get("event_ticker", "")
            code = next((c for c in codes if f"-{c}" in ev), None)
            parts = m.get("ticker", "").split("-")
            if not code or len(parts) < 4 or m.get("floor_strike") is None:
                continue
            try:
                b, a = float(m["yes_bid_dollars"]), float(m["yes_ask_dollars"])
            except (KeyError, TypeError, ValueError):
                continue
            key = ev.split("-")[1][len(code):]
            name = (m.get("yes_sub_title") or m.get("title") or "").split(":")[0].strip()
            r = res.setdefault((key, parts[2]), {"name": name, "ladders": {}})
            r["ladders"].setdefault(stat, []).append([m["floor_strike"], b, a])
            n += 1
        print("kalshi props", series, n)
    for r in res.values():
        for q in r["ladders"].values():
            q.sort()
    return res


ROSTER_POS = {"NFL": {"QB", "RB", "WR", "TE"}}
SKIP_STATUS = {"Out", "Injured Reserve", "Suspension", "Suspended"}


def keep_rotation(lg, cur, prev):
    """A roster player worth simulating: a regular part of the rotation (or, with no games yet this season, last season)."""
    def avg(rows, i):
        return sum(r[i] or 0 for r in rows) / len(rows) if rows else 0
    if lg == "NBA":
        return (len(cur) >= 2 and avg(cur, 0) >= 12) or (len(cur) < 2 and len(prev) >= 20 and avg(prev, 0) >= 20)
    rows = cur if len(cur) >= 2 else prev
    return len(rows) >= 2 and max(avg(rows, 0), avg(rows, 1) + avg(rows, 2)) >= 15


def props(games, codes, stand):
    """Players for every scheduled game in the window, with their game logs: everyone Kalshi lists a prop for, plus the
    rest of each team's rotation, so any regular can be simulated before (or without) a Kalshi market. Game logs are
    reused from the previous props.json when they were fetched earlier the same day (they only change after games)."""
    players, rosters = [], {}
    today = datetime.now(PT).strftime("%Y-%m-%d")
    cache = {}
    try:
        for p in json.load(open(os.path.join(ROOT, "props.json"))).get("players") or []:
            if p.get("logAsOf") == today and (p.get("cur") or p.get("prev")):
                cache[(p["lg"], p["id"])] = (p.get("cur") or [], p.get("prev") or [])
    except Exception:  # noqa: BLE001
        pass
    for lg in ("nba", "nfl"):
        LG = lg.upper()
        sched = [g for g in games if g["league"] == LG and g["status"] == "STATUS_SCHEDULED"]
        if not sched:
            continue
        for (key, pcode), info in kalshi_props(lg, codes).items():
            g = next((x for x in sched if any(a + h == key for a in cands(LG, x["away"]) for h in cands(LG, x["home"]))), None)
            if not g:
                continue
            side, best = None, 0
            for s in ("away", "home"):
                for c in cands(LG, g[s]):
                    if pcode.startswith(c) and len(c) > best:
                        side, best = s, len(c)
            if not side or not g.get(side + "Id"):
                continue
            tid = g[side + "Id"]
            if tid not in rosters:
                rosters[tid] = roster(lg, tid)
            ath = match_player(info["name"], rosters[tid], pcode)
            if not ath:
                print("prop player not matched", LG, info["name"], pcode)
                continue
            other = "home" if side == "away" else "away"
            players.append({"id": ath["id"], "name": ath["name"], "lg": LG, "team": g[side], "opp": g[other],
                            "home": side == "home", "gameId": g["id"], "date": g["date"], "start": g["start"],
                            "pos": ath["pos"], "status": ath["status"], "ladders": info["ladders"]})
        # the rest of each rotation, no Kalshi market needed
        listed = {(p["gameId"], p["id"]) for p in players}
        for g in sched:
            for side in ("away", "home"):
                tid = g.get(side + "Id")
                if not tid:
                    continue
                if tid not in rosters:
                    rosters[tid] = roster(lg, tid)
                other = "home" if side == "away" else "away"
                for ath in rosters[tid]:
                    if not ath.get("id") or (g["id"], ath["id"]) in listed or (ath.get("status") or "") in SKIP_STATUS:
                        continue
                    if LG in ROSTER_POS and ath.get("pos") not in ROSTER_POS[LG]:
                        continue
                    listed.add((g["id"], ath["id"]))
                    players.append({"id": ath["id"], "name": ath["name"], "lg": LG, "team": g[side], "opp": g[other],
                                    "home": side == "home", "gameId": g["id"], "date": g["date"], "start": g["start"],
                                    "pos": ath["pos"], "status": ath["status"], "ladders": {}, "rosterOnly": True})
    need = [p for p in players if (p["lg"], p["id"]) not in cache]
    with ThreadPoolExecutor(max_workers=8) as ex:
        cur = dict(zip([p["id"] for p in need], ex.map(lambda p: gamelog(p["lg"].lower(), p["id"]), need)))
    for p in players:
        c = cache.get((p["lg"], p["id"]))
        p["cur"] = c[0] if c else cur.get(p["id"], [])
    # last season only for players who make the cut (it's the slow part)
    def wanted(p):
        return not p.get("rosterOnly") or keep_rotation(p["lg"], p["cur"], [])  or len(p["cur"]) < 2
    need_prev = [p for p in players if (p["lg"], p["id"]) not in cache and wanted(p)]
    with ThreadPoolExecutor(max_workers=8) as ex:
        prev = dict(zip([p["id"] for p in need_prev], ex.map(lambda p: gamelog(p["lg"].lower(), p["id"], prev=True), need_prev)))
    for p in players:
        c = cache.get((p["lg"], p["id"]))
        p["prev"] = (c[1] if c else prev.get(p["id"], []))[:60 if not p.get("rosterOnly") else 40]
        p["logAsOf"] = today
    players = [p for p in players if (p["cur"] or p["prev"]) and (not p.get("rosterOnly") or keep_rotation(p["lg"], p["cur"], p["prev"]))]
    print("prop players: Kalshi-listed", sum(1 for p in players if not p.get("rosterOnly")), "| rotation only", sum(1 for p in players if p.get("rosterOnly")),
          "| logs fetched", len(need), "+", len(need_prev), "| reused", sum(1 for p in players if (p["lg"], p["id"]) in cache))
    lg_avg = {LG: round(sum(t["pf"] for t in st.values()) / len(st), 2) for LG, st in stand.items() if st}
    return {"cols": {"NBA": ["min", "pts", "reb", "ast", "tpm"], "NFL": ["passYds", "rushYds", "recYds", "rec"]},
            "players": players, "teams": stand, "lgAvg": lg_avg}


# ---------- weather for outdoor NFL games (Open-Meteo, free, no key) ----------
STATES = {"AL": "Alabama", "AZ": "Arizona", "CA": "California", "CO": "Colorado", "FL": "Florida", "GA": "Georgia",
          "IL": "Illinois", "IN": "Indiana", "LA": "Louisiana", "MA": "Massachusetts", "MD": "Maryland", "MI": "Michigan",
          "MN": "Minnesota", "MO": "Missouri", "NC": "North Carolina", "NJ": "New Jersey", "NV": "Nevada", "NY": "New York",
          "OH": "Ohio", "PA": "Pennsylvania", "TN": "Tennessee", "TX": "Texas", "WA": "Washington", "WI": "Wisconsin",
          "DC": "District of Columbia", "VA": "Virginia", "UT": "Utah", "OR": "Oregon", "KY": "Kentucky", "SC": "South Carolina"}


def weather(games):
    geo, out = {}, {}
    for g in games:
        if g["league"] != "NFL" or g.get("indoor") is not False or g["status"] != "STATUS_SCHEDULED" or not g.get("city"):
            continue
        try:
            k = (g["city"], g.get("state"))
            if k not in geo:
                us = (g.get("country") or "USA") in ("USA", "US", "United States")
                j = get("https://geocoding-api.open-meteo.com/v1/search?count=10&name=" + urllib.parse.quote(g["city"]) + ("&country=US" if us else ""))
                res = j.get("results") or []
                want = STATES.get(g.get("state") or "", "").lower()
                pick = next((r for r in res if want and (r.get("admin1") or "").lower() == want), res[0] if res else None)
                geo[k] = (pick["latitude"], pick["longitude"]) if pick else None
            if not geo[k]:
                continue
            lat, lon = geo[k]
            f = get(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
                    "&hourly=wind_speed_10m,wind_gusts_10m,precipitation_probability,temperature_2m"
                    "&wind_speed_unit=mph&temperature_unit=fahrenheit&timezone=UTC&forecast_days=16")
            h = f.get("hourly") or {}
            t = g["start"][:13] + ":00"
            if t not in (h.get("time") or []):
                continue
            i = h["time"].index(t)
            out[g["id"]] = {"wind": h["wind_speed_10m"][i], "gust": h["wind_gusts_10m"][i],
                            "precip": h["precipitation_probability"][i], "temp": h["temperature_2m"][i]}
        except Exception as e:  # noqa: BLE001
            print("weather failed", g["id"], e)
    return out


def kalshi(series, codes):
    try:
        j = get(KALSHI.format(s=series))
    except Exception as e:  # noqa: BLE001
        print("kalshi failed", series, e)
        return {"n": 0, "o": {}}
    o = {}
    for m in j.get("markets", []):
        ev = m.get("event_ticker", "")
        code = next((c for c in codes if f"-{c}" in ev), None)
        if not code:
            continue
        try:
            b, a = float(m["yes_bid_dollars"]), float(m["yes_ask_dollars"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 0.25 <= (a + b) / 2 <= 0.75:
            continue
        key = ev.split("-")[1][len(code):]
        if "TOTAL" in ev:
            o.setdefault(key, []).append([m.get("floor_strike"), b, a])
        else:
            team = re.sub(r"[0-9]+$", "", m["ticker"].split("-")[-1])
            o.setdefault(key, {}).setdefault(team, []).append([m.get("floor_strike"), b, a])
    return {"n": len(j.get("markets", [])), "o": o}


AN_API = "https://api.actionnetwork.com/web/v2/scoreboard/publicbetting/{lg}?bookIds=15&date={d}&periods=event"


def tickets(lg, dates):
    """Free share-of-bets figures from Action Network's public betting feed (the same one its public page loads).
    Only bet_info.tickets.percent is read. Money percentages are never used."""
    games, seen = [], set()
    for day in dates:
        try:
            j = get(AN_API.format(lg=lg, d=day))
            for g in j.get("games") or []:
                if g.get("id") not in seen:
                    seen.add(g.get("id"))
                    games.append(g)
        except Exception as e:  # noqa: BLE001
            print("action network feed failed", lg, day, e)
    if games:
        return parse_tickets(games)
    try:
        html = get(f"https://www.actionnetwork.com/{lg}/public-betting", as_json=False)
    except Exception as e:  # noqa: BLE001
        print("action network page failed", lg, e)
        return []
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        print("action network: no page data", lg, len(html), re.sub(r"\s+", " ", html[:160]))
        return []
    d = json.loads(m.group(1))

    def find(o, depth=0):
        if o is None or depth > 8:
            return None
        if isinstance(o, list) and o and isinstance(o[0], dict) and "teams" in o[0] and "start_time" in o[0]:
            return o
        if isinstance(o, dict):
            for v in o.values():
                r = find(v, depth + 1)
                if r:
                    return r
        if isinstance(o, list):
            for v in o:
                r = find(v, depth + 1)
                if r:
                    return r
        return None
    return parse_tickets(find(d.get("props")) or [])


def parse_tickets(games):
    out = []
    for g in games:
        if g.get("status") == "complete":
            continue
        ab = lambda tid: next((t.get("abbr") for t in g.get("teams", []) if t.get("id") == tid), None)
        e = ((g.get("markets") or {}).get("15") or {}).get("event") or {}

        def tk(arr, side):
            x = next((y for y in (arr or []) if y.get("side") == side and ((y.get("bet_info") or {}).get("tickets") or {}).get("percent")), None)
            return x["bet_info"]["tickets"]["percent"] if x else None
        out.append({"away": ab(g.get("away_team_id")), "home": ab(g.get("home_team_id")), "start": g.get("start_time"),
                    "bets": g.get("num_bets"), "home_t": tk(e.get("spread"), "home"), "away_t": tk(e.get("spread"), "away"),
                    "over_t": tk(e.get("total"), "over"), "under_t": tk(e.get("total"), "under")})
    return out


# ---------- sportsbook consensus (Action Network's free odds feed) ----------
AN_ODDS = "https://api.actionnetwork.com/web/v2/scoreboard/{lg}?bookIds={books}&date={d}&periods=event"
BOOKS = {68: "DraftKings", 69: "FanDuel", 75: "BetMGM", 79: "bet365", 123: "Caesars", 71: "BetRivers", 2988: "Fanatics", 247: "Unibet"}


def book_odds(lg, dates):
    """Current full-game spread, total and moneyline at eight US sportsbooks."""
    out, seen = [], set()
    for day in dates:
        try:
            j = get(AN_ODDS.format(lg=lg, books=",".join(map(str, BOOKS)), d=day))
        except Exception as e:  # noqa: BLE001
            print("action network odds failed", lg, day, e)
            continue
        for g in j.get("games") or []:
            if g.get("id") in seen or g.get("status") == "complete":
                continue
            seen.add(g.get("id"))
            ab = lambda tid: next((t.get("abbr") for t in g.get("teams", []) if t.get("id") == tid), None)
            books = {}
            for bid, m in (g.get("markets") or {}).items():
                name = BOOKS.get(int(bid)) if str(bid).isdigit() else None
                if not name:
                    continue
                e = (m or {}).get("event") or {}

                def pick(arr, side, with_line=True):
                    x = next((y for y in (arr or []) if y.get("side") == side and y.get("odds") is not None), None)
                    if not x or (with_line and x.get("value") is None):
                        return None
                    return [x["value"], x["odds"]] if with_line else x["odds"]
                b = {}
                sh, sa = pick(e.get("spread"), "home"), pick(e.get("spread"), "away")
                if sh and sa:
                    b["spread"] = {"home": sh, "away": sa}
                to, tu = pick(e.get("total"), "over"), pick(e.get("total"), "under")
                if to and tu:
                    b["total"] = {"over": to, "under": tu}
                mh, ma = pick(e.get("moneyline"), "home", False), pick(e.get("moneyline"), "away", False)
                if mh and ma:
                    b["ml"] = {"home": mh, "away": ma}
                if b:
                    books[name] = b
            if books:
                out.append({"away": ab(g.get("away_team_id")), "home": ab(g.get("home_team_id")), "start": g.get("start_time"), "books": books})
    return out


# ---------- results for grading paper trades (final scores and box scores) ----------
def summary(lg, gid):
    return get(f"https://site.api.espn.com/apis/site/v2/sports/{SPORT[lg]}/{lg}/summary?event={gid}", tries=2)


def hist_row(g, s):
    """One finished game for hist.json: DraftKings opening and closing lines (ESPN's pick center), the final score and
    ESPN's pregame win chance. sp/tot = [open line, close line, close odds (home/over), close odds (away/under), open odds, open odds]."""
    pcs = s.get("pickcenter") or []
    pc = next((p for p in pcs if "draft" in ((p.get("provider") or {}).get("name") or "").lower()), pcs[0] if pcs else None)
    if not pc:
        return None
    r = {"lg": g["league"], "date": g["date"], "start": g["start"], "home": g["home"], "away": g["away"],
         "hs": g["homeScore"], "as": g["awayScore"], "neutral": g.get("neutral", False)}
    ps, t = pc.get("pointSpread") or {}, pc.get("total") or {}
    h, a = oc(ps.get("home")), oc(ps.get("away"))
    if h and a:
        r["sp"] = [h[0], h[2], h[3], a[3], h[1], a[1]]
    elif num(pc.get("spread")) is not None:
        hs, as_ = num((pc.get("homeTeamOdds") or {}).get("spreadOdds")), num((pc.get("awayTeamOdds") or {}).get("spreadOdds"))
        if hs is not None and as_ is not None:
            r["sp"] = [num(pc["spread"]), num(pc["spread"]), hs, as_, hs, as_]
    o, u = oc(t.get("over")), oc(t.get("under"))
    if o and u:
        r["tot"] = [o[0], o[2], o[3], u[3], o[1], u[1]]
    elif num(pc.get("overUnder")) is not None and num(pc.get("overOdds")) is not None and num(pc.get("underOdds")) is not None:
        r["tot"] = [num(pc["overUnder"]), num(pc["overUnder"]), num(pc["overOdds"]), num(pc["underOdds"]), num(pc["overOdds"]), num(pc["underOdds"])]
    hm, am = num((pc.get("homeTeamOdds") or {}).get("moneyLine")), num((pc.get("awayTeamOdds") or {}).get("moneyLine"))
    if hm is not None and am is not None:
        r["ml"] = [hm, am]
    wp = (s.get("winprobability") or [{}])[0].get("homeWinPercentage")
    if wp is not None and 0 < wp < 1:
        r["wp"] = round(wp, 4)
    return r if ("sp" in r or "tot" in r) else None


def box(lg, gid, s=None):
    s = s or summary(lg, gid)
    rows = {}
    for t in (s.get("boxscore") or {}).get("players") or []:
        for cat in t.get("statistics") or []:
            keys = cat.get("keys") or []
            for a in cat.get("athletes") or []:
                aid = (a.get("athlete") or {}).get("id")
                if not aid or a.get("didNotPlay") or not a.get("stats"):
                    continue
                d = dict(zip(keys, a["stats"]))
                if lg == "nba":
                    rows[aid] = [stat_num(d.get("points")), stat_num(d.get("rebounds")), stat_num(d.get("assists")),
                                 stat_num(d.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))]
                else:
                    r = rows.setdefault(aid, [0, 0, 0, 0])
                    if cat.get("name") == "passing":
                        r[0] = stat_num(d.get("passingYards"))
                    elif cat.get("name") == "rushing":
                        r[1] = stat_num(d.get("rushingYards"))
                    elif cat.get("name") == "receiving":
                        r[2], r[3] = stat_num(d.get("receivingYards")), stat_num(d.get("receptions"))
    return rows


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def known_ids(name, key="games"):
    try:
        return json.load(open(os.path.join(ROOT, name))).get(key, {})
    except Exception:  # noqa: BLE001
        return {}


def results(games):
    """Finished (or called-off) games not yet in results.json, with player box scores; plus history rows for hist.json."""
    known, hist_known = known_ids("results.json"), known_ids("hist.json")
    out, hist = {}, {}
    for g in games:
        st = g.get("status")
        if st not in ("STATUS_FINAL", "STATUS_POSTPONED", "STATUS_CANCELED") or (g["id"] in known and known[g["id"]].get("status") == st):
            continue
        r = {"lg": g["league"], "date": g["date"], "start": g["start"], "status": st, "away": g["away"], "home": g["home"]}
        if st == "STATUS_FINAL":
            r["as"], r["hs"] = g["awayScore"], g["homeScore"]
            try:
                s = summary(g["league"].lower(), g["id"])
                r["p"] = box(g["league"].lower(), g["id"], s)
                if g["id"] not in hist_known and g.get("stype") in (2, 3):
                    h = hist_row(g, s)
                    if h:
                        hist[g["id"]] = h
            except Exception as e:  # noqa: BLE001
                print("espn box score failed", g["id"], e)
                continue
        out[g["id"]] = r
    return out, hist


SEASON_START = {"nba": "2026-10-20", "nfl": "2026-09-09"}


def backfill(d):
    """Every finished regular-season game since the season started that hist.json doesn't have yet."""
    known, hist = known_ids("hist.json"), {}
    end = datetime.now(PT).date() - timedelta(days=1)
    for lg, first in SEASON_START.items():
        day, dates = datetime.strptime(first, "%Y-%m-%d").date(), []
        while day <= end:
            dates.append(day.strftime("%Y%m%d"))
            day += timedelta(days=1)
        games = [g for g in espn(dates, (lg,)) if g["status"] == "STATUS_FINAL" and g.get("stype") in (2, 3) and g["id"] not in known]
        print("backfill", lg, len(dates), "days,", len(games), "finished games to fetch")
        for g in games:
            try:
                h = hist_row(g, summary(lg, g["id"]))
            except Exception as e:  # noqa: BLE001
                print("espn summary failed", g["id"], e)
                continue
            if h:
                hist[g["id"]] = h
            time.sleep(0.15)
    json.dump(hist, open(os.path.join(d, "hist_new.json"), "w"), separators=(",", ":"))
    print("history rows", len(hist))


def main():
    mode, d = sys.argv[1], sys.argv[2]
    os.makedirs(d, exist_ok=True)
    if mode == "backfill":
        backfill(d)
        return
    today = datetime.now(PT).date()
    # refresh looks three days ahead so Sunday's NFL games are on the board (and testable) from Friday
    days = [today - timedelta(days=1), today] if mode == "grade" else [today + timedelta(days=i) for i in range(3)]
    games = espn([x.strftime("%Y%m%d") for x in days])
    json.dump(games, open(os.path.join(d, "espn.json"), "w"))
    print("espn games", len(games))
    res, hist = results(games)
    json.dump(res, open(os.path.join(d, "results_new.json"), "w"), separators=(",", ":"))
    json.dump(hist, open(os.path.join(d, "hist_new.json"), "w"), separators=(",", ":"))
    print("new results", len(res), "history rows", len(hist))
    if mode != "refresh":
        return
    for lg in ("nba", "nfl"):
        bo = book_odds(lg, [x.strftime("%Y%m%d") for x in days])
        json.dump(bo, open(os.path.join(d, f"books_{lg}.json"), "w"))
        print("sportsbook odds", lg, len(bo), "games")
    json.dump(context(games), open(os.path.join(d, "context.json"), "w"))
    codes = [f"{x.strftime('%y')}{MONTHS[x.month - 1]}{x.strftime('%d')}" for x in days]
    for lg in ("nba", "nfl"):
        for mk in ("spread", "total"):
            k = kalshi(f"KX{lg.upper()}{mk.upper()}", codes)
            json.dump(k, open(os.path.join(d, f"kalshi_{lg}_{mk}.json"), "w"))
            print("kalshi", lg, mk, "events", len(k["o"]))
        t = tickets(lg, [x.strftime("%Y%m%d") for x in days])
        json.dump(t, open(os.path.join(d, f"tickets_{lg}.json"), "w"))
        print("tickets", lg, len(t))
    stand = {lg.upper(): standings(lg) for lg in ("nba", "nfl")}
    json.dump(stand, open(os.path.join(d, "standings.json"), "w"))
    print("standings", {k: len(v) for k, v in stand.items()})
    wx = weather(games)
    json.dump(wx, open(os.path.join(d, "weather.json"), "w"))
    print("weather games", len(wx))
    try:
        pr = props(games, codes, stand)
        json.dump(pr, open(os.path.join(d, "props.json"), "w"), separators=(",", ":"))
        print("prop players", len(pr["players"]), {LG: sum(1 for p in pr["players"] if p["lg"] == LG) for LG in ("NBA", "NFL")})
    except Exception as e:  # noqa: BLE001
        print("props failed", e)


if __name__ == "__main__":
    main()
