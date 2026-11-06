"""Merge browser extracts into one raw snapshot for score.py, plus a finals file for grade.py.

Usage: python3 merge.py in_dir out_raw.json out_finals.json CAPTURED_AT_ISO
in_dir holds any of: espn.json, kalshi_nba_spread.json, kalshi_nba_total.json,
kalshi_nfl_spread.json, kalshi_nfl_total.json, tickets_nba.json, tickets_nfl.json,
context.json (SNIPPET CONTEXT output) and notes.json (the analyst reads written for this run)
"""
import json, os, sys

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
    games, finals = [], []
    for g in espn:
        lg = g["league"]
        finals.append([g["id"], g.get("awayScore", 0), g.get("homeScore", 0), g.get("status")])
        if g.get("status") not in (None, "STATUS_SCHEDULED"):
            pass  # started or final: score.py freezes it from the previous slate
        r = {x: g[x] for x in ("league", "date", "id", "start", "away", "awayName", "home", "homeName")}
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
    print("games", len(games), "with kalshi spread", sum(1 for g in games if g.get("kSpread")),
          "kalshi total", sum(1 for g in games if g.get("kTotal")), "tickets", sum(1 for g in games if g.get("tickets")),
          "books", sum(1 for g in games if g.get("books")))


if __name__ == "__main__":
    main()
