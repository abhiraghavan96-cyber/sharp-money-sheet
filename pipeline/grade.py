"""Grade open paper picks.

Usage: python3 grade.py finals.json state_dir out_dir
  finals.json : [[espnGameId, awayScore, homeScore, "STATUS_FINAL"], ...]
  state_dir   : slates/<date>.json and picks/<id>.json as currently stored in the app database
Writes out_dir/picks/<id>.json for every pick that got graded (merge these with an update).

Closing price = the game's last pre-game snapshot in its slate (games freeze at start time).
CLV (clvProb) = closing win probability at the line you took minus the break-even you paid.
Closing probability at your line comes from Kalshi's closing ladder when it is liquid; otherwise
DraftKings' closing no-vig probability, shifted by a per-point slope when the line moved.
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from score import interp, imp, KALSHI_MAX_WIDTH

SLOPE = {("NFL", "spread"): 0.03, ("NFL", "total"): 0.025, ("NBA", "spread"): 0.03, ("NBA", "total"): 0.02}


def close_prob(game, pick):
    m = next((x for x in game["markets"] if x["type"] == pick["market"]), None)
    if not m:
        return None, None
    side = next(s for s in m["sides"] if s["key"] == pick["side"])
    taken = pick["line"]
    k = (game.get("kalshi") or {}).get(pick["market"])
    if k:
        if pick["market"] == "spread":
            fav = k["fav"]
            # the pick's line from the favourite's perspective
            fav_line = abs(taken)
            r = interp(k["q"], fav_line) if fav_line > 0 else None
            if r and r[3] <= KALSHI_MAX_WIDTH + 1e-9:
                p = r[0] if pick["side"] == fav else 1 - r[0]
                return p, side["line"]
        else:
            r = interp(k, taken)
            if r and r[3] <= KALSHI_MAX_WIDTH + 1e-9:
                p = r[0] if pick["side"] == "over" else 1 - r[0]
                return p, side["line"]
    # DraftKings fallback
    p = side["dkFair"]
    diff = taken - side["line"]  # spread: + means you hold more points than the close
    if pick["market"] == "total":
        diff = (side["line"] - taken) if pick["side"] == "over" else (taken - side["line"])
    p += diff * SLOPE[(game["league"], pick["market"])]
    return max(0.01, min(0.99, p)), side["line"]


def result(pick, away, home):
    if pick["market"] == "spread":
        margin = (home - away) if pick["side"] == "home" else (away - home)
        v = margin + pick["line"]
    else:
        tot = away + home
        v = (tot - pick["line"]) if pick["side"] == "over" else (pick["line"] - tot)
    return "W" if v > 0 else "L" if v < 0 else "P"


def main():
    finals = {g[0]: g for g in json.load(open(sys.argv[1]))}
    state, out = sys.argv[2], sys.argv[3]
    os.makedirs(os.path.join(out, "picks"), exist_ok=True)
    slates = {f[:-5]: json.load(open(os.path.join(state, "slates", f))) for f in os.listdir(os.path.join(state, "slates"))}
    n = 0
    for f in sorted(os.listdir(os.path.join(state, "picks"))):
        p = json.load(open(os.path.join(state, "picks", f)))
        if p.get("result") or p["gameId"] not in finals:
            continue
        gid, away, home, status = finals[p["gameId"]]
        if status != "STATUS_FINAL":
            continue
        game = next((g for g in slates.get(p["date"], {}).get("games", []) if g["id"] == p["gameId"]), None)
        res = result(p, away, home)
        be = p["price"]["breakEven"]
        cp, cl = close_prob(game, p) if game else (None, None)
        upd = {
            "result": res,
            "units": round(1 / be - 1, 3) if res == "W" else (-1.0 if res == "L" else 0.0),
            "finalScore": f"{game['away'] if game else 'Away'} {away}, {game['home'] if game else 'Home'} {home}",
            "closeLine": cl, "closeProb": None if cp is None else round(cp, 4),
            "clvProb": None if cp is None else round(cp - be, 4),
            "status": "graded",
        }
        json.dump(upd, open(os.path.join(out, "picks", f), "w"))
        n += 1
        print(f[:-5], upd)
    print("graded", n)


if __name__ == "__main__":
    main()
