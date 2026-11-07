"""Merge browser extracts into one raw snapshot for score.py, plus a finals file for grade.py.

Usage: python3 merge.py in_dir out_raw.json out_finals.json CAPTURED_AT_ISO
in_dir holds any of: espn.json, kalshi_nba_spread.json, kalshi_nba_total.json,
kalshi_nfl_spread.json, kalshi_nfl_total.json, tickets_nba.json, tickets_nfl.json,
context.json (SNIPPET CONTEXT output) and notes.json (the analyst reads written for this run)
"""
import json, os, re, sys, unicodedata
from datetime import datetime, timedelta

KALSHI_ALIAS = {
    "NBA": {"UTAH": ["UTA"], "SA": ["SAS"], "NO": ["NOP"], "GS": ["GSW"], "NY": ["NYK"], "WSH": ["WAS"], "BKN": ["BKN", "BRK"], "PHX": ["PHX", "PHO"]},
    "NFL": {"WSH": ["WAS"], "JAX": ["JAX", "JAC"], "LAR": ["LAR", "LA"]},
}
TICKET_ALIAS = {"LA": "LAR", "JAC": "JAX", "WAS": "WSH", "UTA": "UTAH", "SAS": "SA", "NOP": "NO", "GSW": "GS", "NYK": "NY", "WSH": "WSH"}


def load(d, name):
    p = os.path.join(d, name)
    return json.load(open(p)) if os.path.exists(p) else None


def cands(lg, code):
    return [code] + KALSHI_ALIAS.get(lg, {}).get(code, [])


def find_event(o, lg, away, home):
    if not o:
        return None
    for a in cands(lg, away):
        for h in cands(lg, home):
            if a + h in o:
                return o[a + h]
    return None


# ---------- college (watch-only): sources disagree on team abbreviations, so games are matched by team name ----------
COLLEGE = ("NCAAB",)
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def norm_team(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().replace("&", " and ").replace("-", " ")
    s = re.sub(r"[().,'\u2019]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\bst$", "state", s)        # "Kansas St." = "Kansas State"
    s = re.sub(r"^saint\b", "st", s)        # "Saint Mary's" = "St. Mary's"
    return s


def same_team(abbr_a, names_a, abbr_b, names_b):
    na, nb = {norm_team(x) for x in names_a or [] if x}, {norm_team(x) for x in names_b or [] if x}
    return bool(na & nb) or bool(abbr_a and abbr_b and abbr_a == abbr_b)


def ts(iso):
    return datetime.strptime((iso or "")[:16], "%Y-%m-%dT%H:%M")


def match_an(entries, g):
    """Action Network entry for this game (same start within 3 hours, both teams matched); (entry, reversed) or (None, False).
    reversed = Action Network lists the teams the other way round (it happens at neutral sites)."""
    gh, ga = (g["home"], g.get("homeNames")), (g["away"], g.get("awayNames"))
    for e in entries or []:
        try:
            if abs((ts(e.get("start")) - ts(g["start"])).total_seconds()) > 3 * 3600:
                continue
        except ValueError:
            continue
        eh, ea = (e.get("home"), e.get("homeNames")), (e.get("away"), e.get("awayNames"))
        if same_team(*eh, *gh) and same_team(*ea, *ga):
            return e, False
        if same_team(*eh, *ga) and same_team(*ea, *gh):
            return e, True
    return None, False


def kalshi_index(kc, games):
    """Match college Kalshi events to ESPN games once per run. Kalshi's market titles aren't matchups ("Little Rock
    wins by over 8.5 points"), its team codes often differ from ESPN's (UALR, MOH) and it sometimes lists the home team
    first, so an event matches a game by (1) both ESPN codes in either order, (2) a spread market's team name, or
    (3) one ESPN code at the start or end of the event code, the last only when exactly one game fits.
    Totals reuse the spread event with the same event code. Returns {"spread": {gid: (key, codes)}, "total": {gid: key}}."""
    def dcode(x):
        return f"{x.strftime('%y')}{MONTHS[x.month - 1]}{x.strftime('%d')}"
    by_dc = {}
    for g in games:
        d = datetime.strptime(g["date"], "%Y-%m-%d")
        for x in (d - timedelta(days=1), d, d + timedelta(days=1)):
            by_dc.setdefault(dcode(x), []).append(g)
    sp, tot = kc.get("spread") or {}, kc.get("total") or {}
    names = sp.get("names") or {}

    def find(key):
        dc, teams = key[:7], key[7:]
        cands = by_dc.get(dc, [])
        hit = [g for g in cands if teams in (g["away"] + g["home"], g["home"] + g["away"])]
        if len(hit) == 1:
            return hit[0]
        hit = {g["id"]: g for g in cands for name in (names.get(key) or {}).values()
               if same_team(None, [name], None, g.get("homeNames")) or same_team(None, [name], None, g.get("awayNames"))}
        if len(hit) == 1:
            return next(iter(hit.values()))
        hit = [g for g in cands if any(len(x) >= 3 and (teams.startswith(x) or teams.endswith(x)) for x in (g["away"], g["home"]))]
        return hit[0] if len(hit) == 1 else None

    def side_codes(key, g):
        teams, codes = key[7:], {}
        for code, name in (names.get(key) or {}).items():
            for side in ("home", "away"):
                if same_team(None, [name], None, g.get(side + "Names")):
                    codes[side] = code
        for code in (sp.get("o") or {}).get(key) or {}:
            for side in ("home", "away"):
                if side not in codes and code == g[side]:
                    codes[side] = code
        # a code still unplaced: the side opposite a placed code at the other end of the event code
        for code in (sp.get("o") or {}).get(key) or {}:
            if code in codes.values():
                continue
            for side, other in (("home", "away"), ("away", "home")):
                oc = codes.get(other)
                if side not in codes and oc and ((teams.startswith(oc) and teams.endswith(code)) or (teams.startswith(code) and teams.endswith(oc))):
                    codes[side] = code
        return codes

    out, taken = {"spread": {}, "total": {}}, set()
    for key in (sp.get("o") or {}):
        g = find(key)
        if g and g["id"] not in out["spread"]:
            out["spread"][g["id"]] = (key, side_codes(key, g))
            taken.add(key)
    spread_key_game = {k: gid for gid, (k, _) in out["spread"].items()}
    for key in (tot.get("o") or {}):
        gid = spread_key_game.get(key)
        if not gid:
            g = find(key)
            gid = g["id"] if g else None
        if gid and gid not in out["total"]:
            out["total"][gid] = key
            taken.add(key)
    KALSHI_USED.update(taken)
    return out


KALSHI_USED = set()   # college Kalshi events some game matched (the rest are logged)


def college_fill(r, g, kc, kix, tickets, books, ctx):
    """Kalshi, public bets, sportsbook prices and ESPN context for one college game."""
    if g.get("spread"):
        r["spread"] = g["spread"]
        fav = "home" if g["spread"]["home"][2] < 0 else "away"
        key, codes = kix["spread"].get(g["id"], (None, None))
        if key and codes and codes.get(fav) and codes[fav] in kc["spread"]["o"][key]:
            r["kSpread"] = {"fav": fav, "q": kc["spread"]["o"][key][codes[fav]]}
    if g.get("total"):
        r["total"] = g["total"]
        key = kix["total"].get(g["id"])
        if key:
            r["kTotal"] = kc["total"]["o"][key]
    t, rev = match_an(tickets, g)
    if t:
        vals = {"home": t.get("away_t" if rev else "home_t"), "away": t.get("home_t" if rev else "away_t"), "over": t.get("over_t"), "under": t.get("under_t")}
        if all(v is not None for v in vals.values()):
            r["tickets"] = dict(vals, bets=t.get("bets"))
    b, rev = match_an(books, g)
    if b:
        bk = b["books"]
        if rev:
            sw = lambda m: {"home": m["away"], "away": m["home"]} if m else m
            bk = {name: {k: (sw(v) if k in ("spread", "ml") else v) for k, v in x.items()} for name, x in bk.items()}
        r["books"] = bk
    if ctx.get(g["id"]):
        r["ctx"] = ctx[g["id"]]


def main():
    d, out_raw, out_finals, captured = sys.argv[1:5]
    espn = load(d, "espn.json") or []
    k = {(lg, m): (load(d, f"kalshi_{lg.lower()}_{m}.json") or {}).get("o") for lg in ("NBA", "NFL") for m in ("spread", "total")}
    tickets = (load(d, "tickets_nba.json") or []) + (load(d, "tickets_nfl.json") or [])
    ctx = load(d, "context.json") or {}
    notes = load(d, "notes.json") or {}
    books = (load(d, "books_nba.json") or []) + (load(d, "books_nfl.json") or [])
    stand = load(d, "standings.json") or {}
    wx = load(d, "weather.json") or {}
    lg_avg = {lg: sum(t["pf"] for t in st.values()) / len(st) for lg, st in stand.items() if st}
    kc = {m: load(d, f"kalshi_ncaab_{m}.json") for m in ("spread", "total")}
    tickets_c, books_c = load(d, "tickets_ncaab.json") or [], load(d, "books_ncaab.json") or []
    ctx_c = load(d, "context_college.json") or {}
    try:
        kix = kalshi_index(kc, [g for g in espn if g["league"] in COLLEGE])
    except Exception as e:  # noqa: BLE001
        print("college kalshi matching failed", e)
        kix = {"spread": {}, "total": {}}
    games, finals = [], []
    for g in espn:
        lg = g["league"]
        finals.append([g["id"], g.get("awayScore", 0), g.get("homeScore", 0), g.get("status")])
        if g.get("status") not in (None, "STATUS_SCHEDULED"):
            pass  # started or final: score.py freezes it from the previous slate
        r = {x: g[x] for x in ("league", "date", "id", "start", "away", "awayName", "home", "homeName")}
        r["neutral"] = bool(g.get("neutral"))
        if lg in COLLEGE:
            try:
                college_fill(r, g, kc, kix, tickets_c, books_c, ctx_c)
                games.append(r)
            except Exception as e:  # noqa: BLE001  (watch-only: a bad college row is dropped, never the run)
                print("college merge failed", g.get("id"), e)
            continue
        if g.get("spread"):
            r["spread"] = g["spread"]
            fav = "home" if g["spread"]["home"][2] < 0 else "away"
            ev = find_event(k[(lg, "spread")], lg, g["away"], g["home"])
            if ev:
                q = next((ev[c] for c in cands(lg, g[fav]) if c in ev), None)
                if q:
                    r["kSpread"] = {"fav": fav, "q": q}
        if g.get("total"):
            r["total"] = g["total"]
            ev = find_event(k[(lg, "total")], lg, g["away"], g["home"])
            if ev:
                r["kTotal"] = ev
        for t in tickets:
            if t["start"][:16] != g["start"][:16]:
                continue
            th, ta = TICKET_ALIAS.get(t["home"], t["home"]), TICKET_ALIAS.get(t["away"], t["away"])
            if th == g["home"] or ta == g["away"]:
                vals = {"home": t.get("home_t"), "away": t.get("away_t"), "over": t.get("over_t"), "under": t.get("under_t")}
                if all(v is not None for v in vals.values()):
                    r["tickets"] = dict(vals, bets=t.get("bets"))
                break
        for b in books:
            if (b.get("start") or "")[:16] != g["start"][:16]:
                continue
            bh, ba = TICKET_ALIAS.get(b["home"], b["home"]), TICKET_ALIAS.get(b["away"], b["away"])
            if bh == g["home"] or ba == g["away"]:
                r["books"] = b["books"]
                break
        if ctx.get(g["id"]):
            r["ctx"] = ctx[g["id"]]
        st = stand.get(lg) or {}
        if st.get(g["home"]) and st.get(g["away"]):
            r["rating"] = {"home": st[g["home"]], "away": st[g["away"]], "lgAvg": round(lg_avg[lg], 2)}
        if wx.get(g["id"]):
            r["wx"] = dict(wx[g["id"]], indoor=False)
        if notes.get(g["id"]):
            r["note"] = notes[g["id"]]
        games.append(r)
    raw = {"capturedAt": captured, "sources": {
        "lines": "DraftKings via ESPN scoreboard (open and current)",
        "kalshi": "Kalshi public market data, mid of best bid and ask",
        "tickets": "Action Network consensus share of bets (free figures only)"}, "games": games}
    json.dump(raw, open(out_raw, "w"))
    json.dump(finals, open(out_finals, "w"))
    for m in ("spread", "total"):   # college Kalshi events no game matched, to tune the name matching
        kf = kc.get(m) or {}
        miss = [f"{k}: {(kf.get('t') or {}).get(k)} {list(((kf.get('names') or {}).get(k) or {}).items())}" for k in (kf.get("o") or {}) if k not in KALSHI_USED]
        if miss:
            print("college kalshi", m, "unmatched", len(miss), "|", " ; ".join(miss[:25]))
    for label, sel in (("games", lambda g: g["league"] not in COLLEGE), ("college games", lambda g: g["league"] in COLLEGE)):
        gs = [g for g in games if sel(g)]
        print(label, len(gs), "with lines", sum(1 for g in gs if g.get("spread") or g.get("total")), "kalshi spread", sum(1 for g in gs if g.get("kSpread")),
              "kalshi total", sum(1 for g in gs if g.get("kTotal")), "tickets", sum(1 for g in gs if g.get("tickets")),
              "books", sum(1 for g in gs if g.get("books")))


if __name__ == "__main__":
    main()
