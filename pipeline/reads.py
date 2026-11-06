"""Write a short automatic read for each game, built only from the numbers in the scored slate.

python3 reads.py out/slates
Fills game["note"] = {"auto": true, "read": ..., "lean": null, "watch": ...} for games that aren't frozen.
Automatic reads explain the board; they never add a lean, so they never change a score.
"""
import json, os, sys


def fl(x):
    if x is None:
        return "—"
    if x == 0:
        return "PK"
    return f"{'+' if x > 0 else '-'}{abs(x):g}"


def names(lst, n=2):
    if not lst:
        return ""
    s = " and ".join(lst[:n]) if len(lst) <= n else ", ".join(lst[:n]) + f" and {len(lst) - n} more"
    return s


def spread_line(g, m):
    home = next(s for s in m["sides"] if s["key"] == "home")
    away = next(s for s in m["sides"] if s["key"] == "away")
    mv = home["move"]  # positive: the home side got more expensive
    opn, now = f"{g['home']} {fl(home['openLine'])}", f"{g['home']} {fl(home['line'])}"
    if m["news"]:
        return 10, f"The spread moved {abs(mv):g} points ({opn} to {now}), more than betting alone usually moves it, so expect injury or lineup news behind it."
    if abs(mv) >= 0.5:
        side, team = (home, g["home"]) if mv > 0 else (away, g["away"])
        if side["tickets"] is not None and side["tickets"] < 50:
            return 9, f"The spread moved toward {team} ({opn} to {now}) even though only {side['tickets']}% of bets are on them, a sign of bigger money on that side."
        return 6, f"The spread moved {abs(mv):g} toward {team} ({opn} to {now})."
    pub = next((s for s in (home, away) if s["tickets"] is not None and s["tickets"] >= 60), None)
    if pub:
        team = g["home"] if pub is home else g["away"]
        return 5, f"{pub['tickets']}% of bets are on {team}, but the spread hasn't moved from {now}."
    return 1, f"The spread is steady at {now}."


def total_line(m):
    over = next(s for s in m["sides"] if s["key"] == "over")
    under = next(s for s in m["sides"] if s["key"] == "under")
    mv = over["move"]
    if m["news"]:
        return 9, f"The total moved {abs(mv):g} points ({over['openLine']:g} to {over['line']:g}), likely news or weather rather than money."
    if abs(mv) >= 0.5:
        side, word = (over, "over") if mv > 0 else (under, "under")
        if side["tickets"] is not None and side["tickets"] < 50:
            return 9, f"The total moved toward the {word} ({over['openLine']:g} to {over['line']:g}) while only {side['tickets']}% of bets are on it."
        return 6, f"The total moved {abs(mv):g} toward the {word} ({over['openLine']:g} to {over['line']:g})."
    return 0, None


def kalshi_line(g):
    best = None
    for m in g["markets"]:
        for s in m["sides"]:
            if s.get("kalshiLiquid") and s.get("kalshiGap") is not None and s["kalshiGap"] >= 0.02:
                if not best or s["kalshiGap"] > best["kalshiGap"]:
                    best = s
    if not best:
        return 0, None
    return 7, f"Kalshi prices {best['label']} at {best['kalshiProb'] * 100:.1f}%, {best['kalshiGap'] * 100:.1f} points above DraftKings."


def model_line(g):
    m = next((x for x in g["markets"] if x["type"] == "spread"), None)
    if not m:
        return 0, None
    si = next(s for s in m["sides"] if s["key"] == "home").get("situation") or {}
    if si.get("modelMargin") is None:
        return 0, None
    mm, edge = si["modelMargin"], si["modelEdge"]
    fav, by = (g["home"], mm) if mm >= 0 else (g["away"], -mm)
    if si.get("modelFar"):
        return 6, f"ESPN's model makes it {fav} by {by:.1f}, {abs(edge):.1f} points off the line, which usually means the model is missing news."
    if abs(edge) >= 1.5:
        team = g["home"] if edge > 0 else g["away"]
        return 7, f"ESPN's model makes it {fav} by {by:.1f}, which leans {team} against the line by {abs(edge):.1f} points."
    return 2, f"ESPN's model ({fav} by {by:.1f}) agrees with the line."


def injury_line(g):
    c = g.get("ctx") or {}
    parts = []
    for k in ("away", "home"):
        t = c.get(k) or {}
        if t.get("out"):
            parts.append(f"{t['ab']} is without {names(t['out'])}")
    if not parts:
        return 0, None
    return 4, "; ".join(parts) + "."


def rest_line(g):
    m = next((x for x in g["markets"] if x["type"] == "spread"), None)
    if not m:
        return 0, None
    si = next(s for s in m["sides"] if s["key"] == "home").get("situation") or {}
    r, o = si.get("rest"), si.get("oppRest")
    if r is None or o is None:
        return 0, None
    if g["league"] == "NBA":
        if r == 0 and o >= 1:
            return 6, f"{g['home']} is on the second night of a back-to-back against a rested {g['away']}."
        if o == 0 and r >= 1:
            return 5, f"{g['away']} is on the second night of a back-to-back."
        return 0, None
    if abs(r - o) >= 3:
        team, d = (g["home"], r - o) if r > o else (g["away"], o - r)
        return 4, f"{team} has {d} more days of rest."
    return 0, None


def watch(g):
    c = g.get("ctx") or {}
    for k in ("away", "home"):
        t = c.get(k) or {}
        if t.get("coreQ"):
            return f"{t['coreQ'][0]}'s status for {t['ab']}."
    if any(m["news"] for m in g["markets"]):
        return "The final injury report."
    return "Whether the number holds until game time."


def read_for(g):
    if not g.get("markets"):
        inj = injury_line(g)[1]
        return {"auto": True, "read": "No line posted yet." + (" " + inj if inj else ""), "lean": None, "watch": "The opening number."}
    cands = []
    sp = next((m for m in g["markets"] if m["type"] == "spread"), None)
    tt = next((m for m in g["markets"] if m["type"] == "total"), None)
    if sp:
        cands.append(spread_line(g, sp))
    if tt:
        cands.append(total_line(tt))
    for f in (kalshi_line, model_line, injury_line, rest_line):
        cands.append(f(g))
    cands = [c for c in cands if c[1]]
    cands.sort(key=lambda c: -c[0])
    return {"auto": True, "read": " ".join(c[1] for c in cands[:3]), "lean": None, "watch": watch(g)}


def main():
    d = sys.argv[1]
    for f in sorted(os.listdir(d)):
        p = os.path.join(d, f)
        s = json.load(open(p))
        n = 0
        for g in s["games"]:
            if g.get("frozen") or (g.get("note") and not g["note"].get("auto")):
                continue
            g["note"] = read_for(g)
            n += 1
        json.dump(s, open(p, "w"))
        print(f, "reads", n)


if __name__ == "__main__":
    main()
