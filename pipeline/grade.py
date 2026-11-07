"""Grade open paper picks, and score every finished game's pre-game probabilities for the accuracy test.

Usage: python3 grade.py finals.json state_dir out_dir [results.json]
  finals.json : [[espnGameId, awayScore, homeScore, "STATUS_FINAL"], ...]
  state_dir   : slates/<date>.json and picks/<id>.json as currently stored in the app database
  results.json: optional; older final scores (lets the accuracy test backfill games graded before it existed)
Writes out_dir/picks/<id>.json for every pick that got graded (merge these with an update), and
out_dir/calib/<gameId>-<market>.json: each source's pre-game probability for the home side / the over at the
closing line, and what happened (res 1 or 0; pushes are left out).

Closing price = the game's last pre-game snapshot in its slate (games freeze at start time).
CLV (clvProb) = closing win probability at the line you took minus the break-even you paid.
Closing probability at your line comes from the sportsbooks' closing consensus (median no-vig across eight books),
moved to your line along the simulator's bell curve; failing that Kalshi's closing ladder, then DraftKings alone.
Also writes out_dir/moves/<gameId>-<market>-<window>.json: the move model's training rows (see learn.py).
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from score import interp, imp, KALSHI_MAX_WIDTH, model_probs, at_line, MOVE_FEATS, SHADOW_FEATS

CALIB_SOURCES = ("cons", "dk", "kalshi", "espn", "power", "blend", "rating", "fair", "rest", "health", "wind") + tuple(SHADOW_FEATS)
MOVES_COLS = ["date", "league", "market", "bucket", "hours", "y"] + MOVE_FEATS + SHADOW_FEATS
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SLOPE = {("NFL", "spread"): 0.03, ("NFL", "total"): 0.025, ("NBA", "spread"): 0.03, ("NBA", "total"): 0.02}


def close_prob(game, pick):
    m = next((x for x in game["markets"] if x["type"] == pick["market"]), None)
    if not m:
        return None, None
    side = next(s for s in m["sides"] if s["key"] == pick["side"])
    taken = pick["line"]
    if side.get("consensus"):   # the sportsbooks' closing consensus, moved to the line you took
        return clamp01(at_line(side["consensus"]["prob"], side["line"], taken, pick["market"], pick["side"], game["league"])), side["line"]
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
    return clamp01(at_line(side["dkFair"], side["line"], taken, pick["market"], pick["side"], game["league"])), side["line"]


def clamp01(p):
    return max(0.01, min(0.99, p))


def result(pick, away, home):
    if pick["market"] == "spread":
        margin = (home - away) if pick["side"] == "home" else (away - home)
        v = margin + pick["line"]
    else:
        tot = away + home
        v = (tot - pick["line"]) if pick["side"] == "over" else (pick["line"] - tot)
    return "W" if v > 0 else "L" if v < 0 else "P"


def main():
    finals = {}
    if len(sys.argv) > 4 and os.path.exists(sys.argv[4]):
        for gid, r in (json.load(open(sys.argv[4])).get("games") or {}).items():
            if r.get("status") == "STATUS_FINAL" and r.get("as") is not None:
                finals[gid] = [gid, r["as"], r["hs"], "STATUS_FINAL"]
    finals.update({g[0]: g for g in json.load(open(sys.argv[1]))})
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
            "finalScore": f"{game['away'] if game else 'Away'} {away:g}, {game['home'] if game else 'Home'} {home:g}",
            "closeLine": cl, "closeProb": None if cp is None else round(cp, 4),
            "clvProb": None if cp is None else round(cp - be, 4),
            "status": "graded",
        }
        mk = p.get("maker")
        if mk and mk.get("status") == "filled":   # the paper Kalshi limit order filled before tip-off
            mbe = mk["breakEven"]
            upd["maker"] = dict(mk, result=res, units=round(1 / mbe - 1, 3) if res == "W" else (-1.0 if res == "L" else 0.0),
                                clvProb=None if cp is None else round(cp - mbe, 4))
        elif mk and mk.get("status") == "posted":
            upd["maker"] = dict(mk, status="expired")
        json.dump(upd, open(os.path.join(out, "picks", f), "w"))
        n += 1
        print(f[:-5], upd)
    print("graded", n)
    calib(finals, slates, out)
    moves(slates, out)


def pregame(g):
    """True when this stored copy of the game was taken before it started."""
    return g["asOf"] < g["start"] if g.get("asOf") else bool(g.get("frozen"))


def calib(finals, slates, out):
    os.makedirs(os.path.join(out, "calib"), exist_ok=True)
    n = 0
    for date, sl in slates.items():
        for g in sl.get("games", []):
            f = finals.get(g["id"])
            if not f or f[3] != "STATUS_FINAL" or not g.get("markets") or not pregame(g):
                continue
            _, away, home, _ = f
            probs = g.get("probs")
            if not probs:  # stored before the app kept per-source probabilities: work them out from the stored markets
                sp = next((m for m in g["markets"] if m["type"] == "spread"), None)
                tt = next((m for m in g["markets"] if m["type"] == "total"), None)
                try:
                    probs = model_probs(g, sp, tt)
                except Exception as e:  # noqa: BLE001
                    print("calib skipped", g["id"], e)
                    continue
            for mt, pr in probs.items():
                L = pr["line"]
                v = (home - away + L) if mt == "spread" else (away + home - L)
                if v == 0:
                    continue
                row = {"date": date, "league": g["league"], "market": mt, "line": L, "res": 1 if v > 0 else 0}
                row.update({k: pr.get(k) for k in CALIB_SOURCES})
                json.dump(row, open(os.path.join(out, "calib", f"{g['id']}-{mt}.json"), "w"))
                n += 1
    print("calibration rows", n)


def moves(slates, out):
    """Move-model rows: each snapshot the sheet kept for a game (inputs some hours out) against where the
    consensus closed, at the snapshot's line."""
    try:
        snaps = json.load(open(os.path.join(ROOT, "snaps.json"))).get("games", {})
    except Exception:  # noqa: BLE001
        snaps = {}
    os.makedirs(os.path.join(out, "moves"), exist_ok=True)
    n = 0
    for date, sl in slates.items():
        for g in sl.get("games", []):
            sn = snaps.get(g["id"])
            if not sn or not g.get("frozen") or not g.get("markets") or not pregame(g):
                continue
            for sk, snap in sn.items():
                mt, b = sk.split("|")
                m = next((x for x in g["markets"] if x["type"] == mt), None)
                key = "home" if mt == "spread" else "over"
                side = next((x for x in (m or {}).get("sides", []) if x["key"] == key), None)
                if not side:
                    continue
                pc = side["consensus"]["prob"] if side.get("consensus") else side["dkFair"]
                y = at_line(pc, side["line"], snap["L"], mt, key, g["league"]) - snap["p"]
                row = [date, g["league"], mt, b, snap["h"], round(y, 4)] + [snap["x"].get(f) for f in MOVE_FEATS + SHADOW_FEATS]
                json.dump(row, open(os.path.join(out, "moves", f"{g['id']}-{mt}-{b}.json"), "w"))
                n += 1
    print("move rows", n)


if __name__ == "__main__":
    main()
