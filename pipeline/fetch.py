"""Pull the raw inputs for one run straight from the public sources (no browser, no Claude).

python3 fetch.py refresh in_dir   -> espn.json, context.json, kalshi_*.json, tickets_*.json
python3 fetch.py grade in_dir     -> espn.json (yesterday and today, with final scores)

Every source is optional: a source that fails is logged and skipped, and the page shows it as missing.
"""
import json, os, re, sys, time, urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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


def espn(dates):
    out, seen = [], set()
    for lg in ("nba", "nfl"):
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
                g = {"league": lg.upper(), "date": pt_date(e["date"]), "id": e["id"], "start": iso(e["date"]),
                     "status": e["status"]["type"]["name"],
                     "away": a["team"]["abbreviation"], "awayName": a["team"]["displayName"],
                     "home": h["team"]["abbreviation"], "homeName": h["team"]["displayName"],
                     "awayScore": float(a.get("score") or 0), "homeScore": float(h.get("score") or 0)}

                def q(x):
                    if not x or not x.get("close"):
                        return None
                    op = x["open"] if x.get("open") and x["open"].get("line") is not None else x["close"]
                    vals = [num(op.get("line")), num(op.get("odds")), num(x["close"].get("line")), num(x["close"].get("odds"))]
                    return None if any(v is None for v in vals) else vals
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
            teams[c["homeAway"]] = {"ab": ab, "rec": rec, "last": last, "lastAtVs": last_ev.get("atVs"),
                                    "out": out_[:6], "q": qq[:4], "lead": lead,
                                    "coreOut": [n for n in lead if n in out_], "coreQ": [n for n in lead if n in qq]}
        p = s.get("predictor") or {}
        pred = None
        try:
            pred = {"home": float(p["homeTeam"]["gameProjection"]), "away": float(p["awayTeam"]["gameProjection"])}
        except (KeyError, TypeError, ValueError):
            pass
        out[g["id"]] = {"pred": pred, "home": teams.get("home"), "away": teams.get("away")}
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


def tickets(lg):
    """Free share-of-bets figures from Action Network's public page. Money percentages are never used."""
    try:
        html = get(f"https://www.actionnetwork.com/{lg}/public-betting", as_json=False)
    except Exception as e:  # noqa: BLE001
        print("action network failed", lg, e)
        return []
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        print("action network: no page data", lg)
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
    out = []
    for g in find(d.get("props")) or []:
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


def main():
    mode, d = sys.argv[1], sys.argv[2]
    os.makedirs(d, exist_ok=True)
    today = datetime.now(PT).date()
    days = [today - timedelta(days=1), today] if mode == "grade" else [today, today + timedelta(days=1)]
    games = espn([x.strftime("%Y%m%d") for x in days])
    json.dump(games, open(os.path.join(d, "espn.json"), "w"))
    print("espn games", len(games))
    if mode != "refresh":
        return
    json.dump(context(games), open(os.path.join(d, "context.json"), "w"))
    codes = [f"{x.strftime('%y')}{MONTHS[x.month - 1]}{x.strftime('%d')}" for x in days]
    for lg in ("nba", "nfl"):
        for mk in ("spread", "total"):
            k = kalshi(f"KX{lg.upper()}{mk.upper()}", codes)
            json.dump(k, open(os.path.join(d, f"kalshi_{lg}_{mk}.json"), "w"))
            print("kalshi", lg, mk, "events", len(k["o"]))
        t = tickets(lg)
        json.dump(t, open(os.path.join(d, f"tickets_{lg}.json"), "w"))
        print("tickets", lg, len(t))


if __name__ == "__main__":
    main()
