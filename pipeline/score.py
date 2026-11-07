"""Sharp Money Sheet scoring: raw snapshot JSON -> slate, pick and status documents.

Usage: python3 score.py raw.json out_dir [--prev prev_slate_dir]
Writes out_dir/slates/<date>.json, out_dir/picks/<id>.json, out_dir/meta/status.json, out_dir/pickupd/<id>.json
(limit-order fills) and out_dir/snaps_new.json (snapshots for the move model). Reads model.json and snaps.json
from the repository root when present.
"""
import json, sys, os, math
from datetime import datetime, timezone, timedelta

STEAM = {"NBA": {"spread": 2.0, "total": 3.0}, "NFL": {"spread": 1.5, "total": 2.0}}
NEWS_MULT = 2.5          # a move bigger than 2.5 steam moves is treated as news (injury, QB change)
KALSHI_MAX_WIDTH = 0.04  # ignore Kalshi quotes wider than 4 cents
FEE_RATE = 0.07          # assumed Kalshi taker fee: 0.07 * p * (1 - p) per contract
MAKER_RATE = 0.0175      # Kalshi maker fee on NFL/NBA game markets: 0.0175 * p * (1 - p) per contract (a quarter of the taker fee)
BEST_MIN, PICK_MIN = 60, 50


def imp(o):
    o = float(o)
    return -o / (-o + 100) if o < 0 else 100 / (o + 100)


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def interp(q, x):
    """q = [[strike, bid, ask], ...]; returns (mid, bid, ask, width) at strike x, or None."""
    q = sorted(q)
    for s, b, a in q:
        if abs(s - x) < 1e-9:
            return (b + a) / 2, b, a, a - b
    for (s1, b1, a1), (s2, b2, a2) in zip(q, q[1:]):
        if s1 < x < s2:
            t = (x - s1) / (s2 - s1)
            b, a = b1 + t * (b2 - b1), a1 + t * (a2 - a1)
            return (b + a) / 2, b, a, max(a1 - b1, a2 - b2)
    return None


def fmt_line(x):
    if x == 0:
        return "PK"
    s = f"{abs(x):g}"
    return ("+" if x > 0 else "-") + s


CONS_SLOPE = {("NBA", "spread"): 0.03, ("NBA", "total"): 0.02, ("NFL", "spread"): 0.03, ("NFL", "total"): 0.025}
CONS_MAXDIFF = {("NBA", "spread"): 3.0, ("NBA", "total"): 6.0, ("NFL", "spread"): 3.0, ("NFL", "total"): 4.0}
CONS_MIN_BOOKS = 3


def dec(o):
    o = float(o)
    return 1 + (100 / -o if o < 0 else o / 100)


def consensus(g, mtype, key, other, line):
    """Median no-vig probability across sportsbooks for this side at the board's line.
    A book hanging a different line is shifted by a per-point slope; books far off the board line (bad data) are dropped."""
    lg, probs, best = g["league"], [], None
    for name, b in (g.get("books") or {}).items():
        m = b.get(mtype)
        if not m or key not in m or other not in m:
            continue
        (bl, bo), (_, oo) = m[key], m[other]
        try:
            p = imp(bo) / (imp(bo) + imp(oo))
        except (ZeroDivisionError, ValueError, TypeError):
            continue
        diff = (line - bl) if (mtype == "spread" or key == "under") else (bl - line)
        if abs(line - bl) > CONS_MAXDIFF[(lg, mtype)]:
            continue
        probs.append(clamp(p + diff * CONS_SLOPE[(lg, mtype)], 0.01, 0.99))
        if abs(bl - line) < 1e-9 and (best is None or dec(bo) > dec(best["odds"])):
            best = {"book": name, "odds": bo}
    if len(probs) < CONS_MIN_BOOKS:
        return None
    probs.sort()
    n = len(probs)
    med = probs[n // 2] if n % 2 else (probs[n // 2 - 1] + probs[n // 2]) / 2
    return {"prob": round(med, 4), "books": n, "best": best}


def ml_consensus(g):
    """Moneyline: median no-vig win probability and best price per side."""
    out = {}
    for key, other in (("home", "away"), ("away", "home")):
        probs, best = [], None
        for name, b in (g.get("books") or {}).items():
            m = b.get("ml")
            if not m:
                continue
            try:
                probs.append(imp(m[key]) / (imp(m[key]) + imp(m[other])))
            except (ZeroDivisionError, ValueError, TypeError):
                continue
            if best is None or dec(m[key]) > dec(best["odds"]):
                best = {"book": name, "odds": m[key]}
        if len(probs) >= CONS_MIN_BOOKS:
            probs.sort()
            n = len(probs)
            out[key] = {"prob": round(probs[n // 2] if n % 2 else (probs[n // 2 - 1] + probs[n // 2]) / 2, 4), "books": n, "best": best}
    return out or None


def side_eval(g, mtype, key, other):
    lg = g["league"]
    m = g[mtype]
    o_line, o_odds, line, odds = m[key]
    oth_odds = m[other][3]
    dk_fair = imp(odds) / (imp(odds) + imp(oth_odds))
    dk_be = imp(odds)
    steam = STEAM[lg][mtype]

    if mtype == "spread":
        toward = o_line - line          # positive = this side got more expensive
        team = g[key]
        label = f"{team} {fmt_line(line)}"
    else:
        toward = (line - o_line) if key == "over" else (o_line - line)
        label = f"{'Over' if key == 'over' else 'Under'} {line:g}"
    news = abs(toward) > NEWS_MULT * steam

    # Kalshi
    kp = kcost = kfee = kbe = kbid = kstrike = None
    liquid = exact = False
    if mtype == "spread" and g.get("kSpread"):
        fav = g["kSpread"]["fav"]
        fav_line = abs(m[fav][2])
        r = interp(g["kSpread"]["q"], fav_line)
        if r:
            mid, b, a, w = r
            liquid = w <= KALSHI_MAX_WIDTH + 1e-9
            kstrike, exact = fav_line, any(abs(q[0] - fav_line) < 1e-9 for q in g["kSpread"]["q"])
            if key == fav:
                kp, kcost, kbid = mid, a, b
            else:
                kp, kcost, kbid = 1 - mid, 1 - b, 1 - a
    if mtype == "total" and g.get("kTotal"):
        r = interp(g["kTotal"], line)
        if r:
            mid, b, a, w = r
            liquid = w <= KALSHI_MAX_WIDTH + 1e-9
            kstrike, exact = line, any(abs(q[0] - line) < 1e-9 for q in g["kTotal"])
            if key == "over":
                kp, kcost, kbid = mid, a, b
            else:
                kp, kcost, kbid = 1 - mid, 1 - b, 1 - a
    if kcost is not None:
        kfee = FEE_RATE * kcost * (1 - kcost)
        kbe = kcost + kfee
    maker = None
    if kbid is not None and exact and kbid >= 0.02:
        lim = round(kbid + 0.01, 2) if kbid + 0.01 < kcost - 1e-9 else round(kbid, 2)   # improve the best bid by a cent, or join it
        mfee = MAKER_RATE * lim * (1 - lim)
        maker = {"limit": lim, "fee": round(mfee, 4), "breakEven": round(lim + mfee, 4), "strike": kstrike,
                 "favTeam": g[g["kSpread"]["fav"]] if mtype == "spread" else None}
    cons = consensus(g, mtype, key, other, line)
    fair = cons["prob"] if cons else dk_fair      # the books' consensus is the fair price when available
    gap = (kp - fair) if kp is not None else None

    t = g.get("tickets")
    tickets = t.get(key) if t else None

    sig, avail = {}, 0
    if not news:
        avail += 25
        sig["move"] = round(clamp(toward / steam) * 25, 1) if toward > 0 else 0.0
    if kp is not None and liquid:
        avail += 25
        sig["kalshi"] = round(clamp(gap / 0.05) * 25, 1) if gap > 0 else 0.0
    if tickets is not None:
        avail += 20
        sig["fade"] = round(clamp((100 - tickets - 55) / 25) * 20, 1)
        if not news:
            avail += 30
            sig["rlm"] = round(clamp(toward / steam) * 30, 1) if (toward >= 0.5 and tickets < 50) else 0.0
    raw = sum(sig.values())
    market_score = round(100 * raw / avail) if avail else 0

    # Situation and analyst read (30% of the final score when present)
    sit, savail, info = situation(g, mtype, key)
    sit_score = round(100 * sum(sit.values()) / savail) if savail else None
    score = market_score if sit_score is None else round(0.7 * market_score + 0.3 * sit_score)
    active = sum(1 for v in list(sig.values()) + list(sit.values()) if v > 0)

    venue = None
    if kbe is not None:
        venue = "Kalshi" if kbe < dk_be else "DraftKings"
    else:
        venue = "DraftKings"
    return {
        "key": key, "label": label, "line": line, "openLine": o_line, "odds": odds, "openOdds": o_odds,
        "dkFair": round(dk_fair, 4), "dkBreakEven": round(dk_be, 4),
        "kalshiProb": None if kp is None else round(kp, 4),
        "kalshiCost": None if kcost is None else round(kcost, 4),
        "kalshiFee": None if kfee is None else round(kfee, 4),
        "kalshiBreakEven": None if kbe is None else round(kbe, 4),
        "kalshiLiquid": liquid, "kalshiExact": exact, "kalshiGap": None if gap is None else round(gap, 4),
        "consensus": cons, "kalshiMaker": maker,
        "bestVenue": venue,
        "tickets": tickets, "move": toward, "newsMove": news,
        "signals": dict(sig, **sit), "availMax": avail, "marketScore": market_score,
        "situationScore": sit_score, "situation": info, "score": score, "activeSignals": active,
    }


SD_MARGIN = {"NBA": 12.0, "NFL": 13.5}   # spread of final margins, for turning a win probability into points
MODEL_FAR = 6.0                          # a model this far from the market is probably missing news


def pt_date(iso):
    d = datetime.strptime(iso[:16], "%Y-%m-%dT%H:%M") - timedelta(hours=8)
    return d.date()


HCA = {"NBA": 2.0, "NFL": 1.5}              # home-court / home-field points
SHRINK = {"NBA": 10, "NFL": 4}             # games of evidence before a team's season numbers count half
# power-rating edge (points): credit starts, full credit, and "too far, probably news"
POWER_SPREAD = {"NBA": (1.0, 5.0, 9.0), "NFL": (0.5, 3.5, 8.0)}
POWER_TOTAL = {"NBA": (2.0, 8.0, 16.0), "NFL": (1.0, 5.0, 10.0)}


def power(g):
    """Our own projection from season points for and against, shrunk toward average early in the season."""
    r = g.get("rating")
    if not r or not r.get("home") or not r.get("away") or not r.get("lgAvg"):
        return None
    lg, avg = g["league"], r["lgAvg"]

    def adj(t):
        w = t["gp"] / (t["gp"] + SHRINK[lg])
        return (t["pf"] - avg) * w, (t["pa"] - avg) * w
    ho, hd = adj(r["home"])
    ao, ad = adj(r["away"])
    return {"margin": round(ho + ad - ao - hd + HCA[lg], 1), "total": round(2 * avg + ho + ad + ao + hd, 1)}


def ramp(x, lo, hi):
    return clamp((x - lo) / (hi - lo)) if x > lo else 0.0


def recent_games(ctx_team, gd, days):
    """Games a team played in the `days` days before game day gd."""
    return sum(1 for d in (ctx_team.get("recent") or []) if 1 <= (gd - pt_date(d)).days <= days)


def situation(g, mtype, key):
    """ESPN model, power rating, rest, health, wind and analyst lean for one side. Returns (signals, available points, info)."""
    from statistics import NormalDist
    ctx, note = g.get("ctx"), g.get("note")
    sig, avail, info = {}, 0, {}
    lg = g["league"]
    pw = power(g)
    if pw and mtype == "spread":
        market_home = -g["spread"]["home"][2]
        edge_home = pw["margin"] - market_home
        edge = edge_home if key == "home" else -edge_home
        lo, hi, far = POWER_SPREAD[lg]
        info["powerMargin"] = pw["margin"] if key == "home" else -pw["margin"]
        info["powerEdge"] = round(edge, 1)
        info["powerFar"] = abs(edge_home) > far
        avail += 15
        sig["power"] = 0.0 if info["powerFar"] else round(ramp(edge, lo, hi) * 15, 1)
    if pw and mtype == "total":
        e_over = pw["total"] - g["total"]["over"][2]
        edge = e_over if key == "over" else -e_over
        lo, hi, far = POWER_TOTAL[lg]
        info["powerTotal"] = pw["total"]
        info["powerEdge"] = round(edge, 1)
        info["powerFar"] = abs(e_over) > far
        avail += 15
        sig["power"] = 0.0 if info["powerFar"] else round(ramp(edge, lo, hi) * 15, 1)
    wx = g.get("wx")
    if mtype == "total" and lg == "NFL" and wx and wx.get("wind") is not None:
        info["wind"], info["gust"] = wx["wind"], wx.get("gust")
        avail += 10
        sig["weather"] = round(ramp(wx["wind"], 10, 20) * 10, 1) if key == "under" else 0.0
    if mtype == "spread" and ctx and ctx.get("home") and ctx.get("away"):
        other = "home" if key == "away" else "away"
        gd = pt_date(g["start"])
        rest = {}
        for k in ("home", "away"):
            last = ctx[k].get("last")
            rest[k] = (gd - pt_date(last)).days - 1 if last else None
        info["rest"] = rest[key]
        info["oppRest"] = rest[other]
        if rest[key] is not None and rest[other] is not None:
            avail += 10
            diff = rest[key] - rest[other]
            if lg == "NBA":
                # second night of a back-to-back, or a third game in four nights / fourth in six
                dense = {k: recent_games(ctx[k], gd, 3) >= 2 or recent_games(ctx[k], gd, 5) >= 3 for k in ("home", "away")}
                info["dense"], info["oppDense"] = dense[key], dense[other]
                if rest[other] == 0 and rest[key] >= 1:
                    pts = 10
                elif dense[other] and not dense[key] and rest[key] >= 1:
                    pts = 6
                else:
                    pts = 0
            else:
                pts = round(clamp((diff - 2) / 5) * 10, 1) if diff > 2 else 0
            sig["rest"] = pts
        info["out"], info["oppOut"] = ctx[key].get("out", []), ctx[other].get("out", [])
        avail += 10
        if lg == "NBA" and ctx[key].get("lostPts") is not None and ctx[other].get("lostPts") is not None:
            team_pts = lambda k: ((g.get("rating") or {}).get(k) or {}).get("pf") or 112.0
            mine, theirs = ctx[key]["lostPts"] / team_pts(key), ctx[other]["lostPts"] / team_pts(other)
            info["lost"], info["oppLost"] = round(mine, 3), round(theirs, 3)
            sig["health"] = round(clamp((theirs - mine) / 0.20) * 10, 1) if theirs > mine else 0.0
        elif lg == "NFL" and ctx[key].get("lostUnits") is not None and ctx[other].get("lostUnits") is not None:
            mine, theirs = ctx[key]["lostUnits"], ctx[other]["lostUnits"]
            info["lost"], info["oppLost"] = mine, theirs
            sig["health"] = round(clamp((theirs - mine) / 1.0) * 10, 1) if theirs > mine else 0.0
        else:
            mine, theirs = len(ctx[key].get("out", [])), len(ctx[other].get("out", []))
            sig["health"] = round(clamp((theirs - mine) / 3) * 10, 1) if theirs > mine else 0.0
        pred = ctx.get("pred")
        if pred and pred.get("home") and 0 < pred["home"] < 100:
            p = pred["home"] / 100
            model_home = SD_MARGIN[g["league"]] * NormalDist().inv_cdf(p)
            market_home = -g["spread"]["home"][2]
            edge_home = model_home - market_home
            edge = edge_home if key == "home" else -edge_home
            info["modelMargin"] = round(model_home if key == "home" else -model_home, 1)
            info["modelEdge"] = round(edge, 1)
            info["modelFar"] = abs(edge_home) > MODEL_FAR
            avail += 20
            sig["model"] = 0.0 if info["modelFar"] or edge <= 0.5 else round(clamp((edge - 0.5) / 3.5) * 20, 1)
    if note:
        avail += 15
        lean = note.get("lean")
        hit = bool(lean and lean.get("market") == mtype and lean.get("side") == key)
        sig["analyst"] = {1: 5.0, 2: 10.0, 3: 15.0}.get(lean.get("conf"), 0.0) if hit else 0.0
    return sig, avail, info


SD_TOTAL = {"NBA": 18.0, "NFL": 13.5}


def model_probs(g, sp, tt):
    """Chance the home side covers / the over hits, from each source and from the app's blend
    (the same blend the Test-a-bet simulation uses: 60% books' consensus, 25% ESPN model, 15% power rating; totals 80/20)."""
    from statistics import NormalDist
    N = NormalDist()
    lg, out = g["league"], {}
    if sp:
        h = next(x for x in sp["sides"] if x["key"] == "home")
        L, si, c, sd = h["line"], h.get("situation") or {}, h.get("consensus"), SD_MARGIN[lg]
        cover = lambda mu: N.cdf((mu + L) / sd)
        mk = sd * N.inv_cdf(clamp(c["prob"] if c else h["dkFair"], 0.01, 0.99)) - L     # the market's implied margin (DraftKings alone when no consensus)
        parts, src = [(0.6, mk)], {"line": L, "cons": c["prob"] if c else None, "dk": h["dkFair"],
                                   "kalshi": h.get("kalshiProb") if h.get("kalshiLiquid") else None}
        if si.get("modelMargin") is not None:
            src["espn"] = round(cover(si["modelMargin"]), 4)
            if not si.get("modelFar"):
                parts.append((0.25, si["modelMargin"]))
        if si.get("powerMargin") is not None:
            src["power"] = round(cover(si["powerMargin"]), 4)
            if si.get("powerFar") is False:
                parts.append((0.15, si["powerMargin"]))
        src["blend"] = round(cover(sum(w * x for w, x in parts) / sum(w for w, _ in parts)), 4)
        out["spread"] = src
    if tt:
        o = next(x for x in tt["sides"] if x["key"] == "over")
        L, si, c, sd = o["line"], o.get("situation") or {}, o.get("consensus"), SD_TOTAL[lg]
        over = lambda mu: N.cdf((mu - L) / sd)
        mk = L + sd * N.inv_cdf(clamp(c["prob"] if c else o["dkFair"], 0.01, 0.99))
        parts, src = [(0.8, mk)], {"line": L, "cons": c["prob"] if c else None, "dk": o["dkFair"],
                                   "kalshi": o.get("kalshiProb") if o.get("kalshiLiquid") else None}
        if si.get("powerTotal") is not None:
            src["power"] = round(over(si["powerTotal"]), 4)
            if si.get("powerFar") is False:
                parts.append((0.2, si["powerTotal"]))
        src["blend"] = round(over(sum(w * x for w, x in parts) / sum(w for w, _ in parts)), 4)
        out["total"] = src
    return out


# ---------- v3: the fair price ----------
# A side's fair chance = the sportsbooks' consensus now
#   + the move the line is expected to make before the game (learned from where lines actually closed)
#   + any edge beyond the closing line a source has shown against results (learned slowly, starts near zero).
# Both sets of weights are refit every night by learn.py from this season's games and written to model.json;
# the priors below are what the sheet assumes before the data says otherwise.
MOVE_FEATS = ["kalshi", "dk", "espn", "rating", "public", "move", "rest", "health", "wind", "home", "over"]
MOVE_PRIOR = {"kalshi": 0.25, "dk": 0.10, "espn": 0.03, "rating": 0.05}
RESULT_FEATS = ["kalshi", "dk", "espn", "rating", "rest", "health", "wind", "home", "over"]
RESULT_PRIOR = {"kalshi": 0.10, "espn": 0.05, "rating": 0.03}
MOVE_N0, RESULT_N0 = 300, 2000   # how many games of evidence the priors are worth
TAU_HOURS = 12.0                 # a line has about all its remaining movement ahead of it 12+ hours out, none at tip-off
ADJ_CAP = 0.06                   # never move the fair price more than 6 points off the consensus
EV_PICK, EV_BEST = 0.015, 0.025  # log a pick at +1.5% expected return at the best available price; best bet at +2.5%
KELLY_FRAC, KELLY_CAP = 0.25, 3.0  # quarter Kelly, at most 3 units (1 unit = 1% of bankroll)
SUSPECT_GAP = 0.06               # a book price this much better than the consensus is probably stale or bad data
N01 = None


def _nd():
    global N01
    if N01 is None:
        from statistics import NormalDist
        N01 = NormalDist()
    return N01


def nv(o1, o2):
    """No-vig chance of the first side from a two-way American price."""
    return imp(o1) / (imp(o1) + imp(o2))


def at_line(p, L_from, L_to, mtype, key, lg):
    """A side's chance at one line moved to another line along the same bell curve the simulator uses."""
    if L_from is None or L_to is None or abs(L_from - L_to) < 1e-9:
        return p
    N, p = _nd(), clamp(p, 0.01, 0.99)
    if mtype == "spread":
        sd = SD_MARGIN[lg]
        mu = sd * N.inv_cdf(p) - L_from
        return N.cdf((mu + L_to) / sd)
    sd = SD_TOTAL[lg]
    if key == "over":
        mu = L_from + sd * N.inv_cdf(p)
        return N.cdf((mu - L_to) / sd)
    mu = L_from - sd * N.inv_cdf(p)
    return N.cdf((L_to - mu) / sd)


def rating_line(model, g):
    """(home margin, total) the market's own season ratings imply for this matchup, or None."""
    rt = ((model or {}).get("ratings") or {}).get(g["league"])
    if not rt:
        return None
    th, ta = rt["team"].get(g["home"]), rt["team"].get(g["away"])
    oh, oa = rt["tot"].get(g["home"]), rt["tot"].get(g["away"])
    if not th or not ta or min(th[1], ta[1]) < rt.get("minGames", 4):
        return None
    margin = (0 if g.get("neutral") else rt["hca"]) + th[0] - ta[0]
    total = rt["T"] + oh[0] + oa[0] if oh and oa else None
    return margin, total


def market_features(g, m, now, model):
    """Everything the two learned models look at, for the home side (spreads) or the over (totals)."""
    lg, mt = g["league"], m["type"]
    key = "home" if mt == "spread" else "over"
    s = next(x for x in m["sides"] if x["key"] == key)
    o = next(x for x in m["sides"] if x["key"] != key)
    L = s["line"]
    p0 = clamp(s["consensus"]["prob"] if s.get("consensus") else s["dkFair"], 0.02, 0.98)
    N = _nd()
    sd = SD_MARGIN[lg] if mt == "spread" else SD_TOTAL[lg]
    cov = (lambda mu: N.cdf((mu + L) / sd)) if mt == "spread" else (lambda mu: N.cdf((mu - L) / sd))
    clip = lambda v, c=0.15: max(-c, min(c, v))
    si = s.get("situation") or {}
    x = {}
    if s.get("kalshiProb") is not None and s.get("kalshiLiquid"):
        x["kalshi"] = clip(s["kalshiProb"] - p0)
    if s.get("consensus"):
        x["dk"] = clip(s["dkFair"] - p0)
    if mt == "spread" and si.get("modelMargin") is not None:
        x["espn"] = clip(cov(si["modelMargin"]) - p0, 0.10)
    rl = rating_line(model, g)
    rp = None
    if rl and (mt == "spread" or rl[1] is not None):
        rp = cov(rl[0] if mt == "spread" else rl[1])
        x["rating"] = clip(rp - p0)
    if s.get("tickets") is not None:
        x["public"] = (50 - s["tickets"]) / 100
    if not s.get("newsMove"):
        x["move"] = clip(s["move"] * CONS_SLOPE[(lg, mt)])
    ss, so = s.get("signals") or {}, o.get("signals") or {}
    if mt == "spread":
        x["rest"] = (ss.get("rest", 0) - so.get("rest", 0)) / 10
        x["health"] = (ss.get("health", 0) - so.get("health", 0)) / 10
        if not g.get("neutral"):
            x["home"] = 1.0
    else:
        x["wind"] = -so.get("weather", 0) / 10
        x["over"] = 1.0
    hours = (datetime.strptime(g["start"], "%Y-%m-%dT%H:%M:%SZ") - datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ")).total_seconds() / 3600
    return {"L": L, "p": round(p0, 4), "h": round(hours, 2), "x": {k: round(v, 4) for k, v in x.items() if v},
            "ratingProb": None if rp is None else round(rp, 4), "ratingLine": rl}


def model_active(part):
    chk = (part or {}).get("check")
    return not chk or chk.get("skillVsZero") is None or chk["skillVsZero"] > 0


def fair_eval(feat, model):
    """Fair chance for the home side / the over, with each input's contribution in probability points."""
    mv = ((model or {}).get("move") or {}).get("coef") or MOVE_PRIOR
    rs = ((model or {}).get("result") or {}).get("coef") or RESULT_PRIOR
    # a fitted model that can't beat "no adjustment" on the games it never saw is switched off
    if not model_active((model or {}).get("move")):
        mv = {}
    if not model_active((model or {}).get("result")):
        rs = {}
    tau = clamp(feat["h"] / TAU_HOURS)
    x = feat["x"]
    move = tau * sum(mv.get(k, 0.0) * x.get(k, 0.0) for k in MOVE_FEATS)
    tilt = sum(rs.get(k, 0.0) * x.get(k, 0.0) for k in RESULT_FEATS)
    contrib = {}
    for k in set(MOVE_FEATS) | set(RESULT_FEATS):
        c = tau * mv.get(k, 0.0) * x.get(k, 0.0) + rs.get(k, 0.0) * x.get(k, 0.0)
        if abs(c) >= 5e-5:
            contrib[k] = round(c, 4)
    adj = clamp(move + tilt, -ADJ_CAP, ADJ_CAP)
    return {"fair": round(clamp(feat["p"] + adj, 0.02, 0.98), 4), "move": round(move, 4), "tilt": round(tilt, 4), "contrib": contrib, "tau": round(tau, 2)}


def best_price(sd, fair):
    """Cheapest way to take this side right now: DraftKings, the best of the eight books, or Kalshi at an exact strike."""
    opts = [{"venue": "DraftKings", "odds": sd["odds"], "breakEven": sd["dkBreakEven"]}]
    bb = (sd.get("consensus") or {}).get("best")
    if bb and bb["book"] != "DraftKings":
        be = imp(bb["odds"])
        if be >= (sd["consensus"]["prob"] - SUSPECT_GAP):
            opts.append({"venue": bb["book"], "odds": bb["odds"], "breakEven": round(be, 4)})
    if sd.get("kalshiExact") and sd.get("kalshiBreakEven") is not None:
        opts.append({"venue": "Kalshi", "cents": round(sd["kalshiCost"] * 100, 1), "breakEven": sd["kalshiBreakEven"]})
    best = min(opts, key=lambda o: o["breakEven"])
    ev = fair / best["breakEven"] - 1
    d = 1 / best["breakEven"]
    f = (fair * d - 1) / (d - 1) if d > 1 else 0.0
    return dict(best, ev=round(ev, 4), kelly=round(clamp(KELLY_FRAC * f * 100, 0, KELLY_CAP), 2))


def load_model(path=None):
    path = path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "model.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def kalshi_side_quote(g, mtype, key, mk):
    """(bid, ask) for one side at a limit order's strike in this snapshot, or None."""
    if mtype == "spread":
        ks = g.get("kSpread")
        if not ks or g.get(ks["fav"]) != mk.get("favTeam"):
            return None
        q, yes = ks["q"], key == ks["fav"]
    else:
        q, yes = g.get("kTotal"), key == "over"
    row = next((x for x in (q or []) if abs(x[0] - mk["strike"]) < 1e-9), None)
    if not row:
        return None
    _, b, a = row
    return (b, a) if yes else (1 - a, 1 - b)


def market_eval(g, mtype):
    if not g.get(mtype):
        return None
    keys = ("away", "home") if mtype == "spread" else ("over", "under")
    a = side_eval(g, mtype, keys[0], keys[1])
    b = side_eval(g, mtype, keys[1], keys[0])
    sides = [a, b]

    def sharp_pts(s):
        return s["signals"].get("rlm", 0) + s["signals"].get("kalshi", 0) + s["signals"].get("move", 0)
    sharp = max(sides, key=sharp_pts)
    sharp = sharp if sharp_pts(sharp) >= 12.5 else None
    pub = next((s for s in sides if s["tickets"] is not None and s["tickets"] >= 60), None)
    if sharp and pub and sharp is not pub:
        tag = "split"
    elif sharp and pub:
        tag = "consensus"
    elif sharp:
        tag = "sharp"
    elif pub:
        tag = "public"
    else:
        tag = "balanced"
    for s in sides:
        s["isSharp"] = s is sharp
        s["isPublic"] = s is pub
    return {"type": mtype, "tag": tag, "news": any(s["newsMove"] for s in sides), "sides": sides}


def snap_bucket(h):
    return "a" if h >= 12 else "b" if h >= 3 else "c"


def build(raw, prev=None, model=None, snaps=None):
    prev = prev or {"slates": {}, "pick_ids": set()}
    model = model if model is not None else {}
    snaps = snaps if snaps is not None else {}
    new_snaps = {}
    now = raw["capturedAt"]
    by_date, picks = {}, []
    from datetime import timedelta
    cutoff = (datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ") - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for g in raw["games"]:
        if g["start"] < cutoff and g["date"] not in prev["slates"]:
            continue  # finished before this run and never tracked: leave it out
        mk = [m for m in (market_eval(g, "spread"), market_eval(g, "total")) if m]
        probs = model_probs(g, next((m for m in mk if m["type"] == "spread"), None), next((m for m in mk if m["type"] == "total"), None))
        rl = None
        for m in mk:
            pr = probs.get(m["type"])
            ft = market_features(g, m, now, model)
            fe = fair_eval(ft, model)
            rl = rl or ft["ratingLine"]
            key = "home" if m["type"] == "spread" else "over"
            for sd in m["sides"]:
                sd["modelProb"] = None if not pr else (pr["blend"] if sd["key"] in ("home", "over") else round(1 - pr["blend"], 4))
                sg = 1 if sd["key"] == key else -1
                sd["fair"] = fe["fair"] if sg == 1 else round(1 - fe["fair"], 4)
                sd["fairNow"] = ft["p"] if sg == 1 else round(1 - ft["p"], 4)
                sd["expMove"], sd["tilt"] = round(sg * fe["move"], 4), round(sg * fe["tilt"], 4)
                sd["contrib"] = {k: round(sg * v, 4) for k, v in fe["contrib"].items()}
                sd["best"] = best_price(sd, sd["fair"])
                sd["ev"] = sd["best"]["ev"]
            m["inputs"] = {"x": ft["x"], "hours": ft["h"], "tau": fe["tau"]}
            if pr:
                pr.update({"rating": ft["ratingProb"], "fair": fe["fair"], "rest": ft["x"].get("rest"), "health": ft["x"].get("health"), "wind": ft["x"].get("wind")})
            if g["start"] > now:   # keep the first snapshot in each time window for the move model
                sk = f"{m['type']}|{snap_bucket(ft['h'])}"
                if sk not in (snaps.get(g["id"]) or {}):
                    new_snaps.setdefault(g["id"], {})[sk] = {"t": now, "h": ft["h"], "L": ft["L"], "p": ft["p"], "x": ft["x"]}
        game = {k: g[k] for k in ("league", "id", "start", "away", "awayName", "home", "homeName")}
        game["neutral"] = bool(g.get("neutral"))
        game["ratingLine"] = None if not rl else {"margin": round(rl[0], 1), "total": None if rl[1] is None else round(rl[1], 1)}
        game["probs"] = probs
        game["asOf"] = now   # when this snapshot of the game was taken (a frozen game keeps its last pre-game time)
        game["markets"] = mk
        game["kalshi"] = {"spread": g.get("kSpread"), "total": g.get("kTotal")}
        game["ctx"] = g.get("ctx")
        game["note"] = g.get("note")
        game["power"] = power(g)
        game["ml"] = ml_consensus(g)
        game["rating"] = g.get("rating")
        game["wx"] = g.get("wx")
        game["hasLines"] = bool(mk)
        game["hasTickets"] = bool(g.get("tickets"))
        game["bets"] = (g.get("tickets") or {}).get("bets")
        pslate = prev["slates"].get(g["date"])
        if pslate and g["start"] <= now:
            old = next((x for x in pslate["games"] if x["id"] == g["id"]), None)
            if old:
                old = dict(old, frozen=True)
                game = old
        by_date.setdefault(g["date"], []).append(game)

    slates = {}
    for date, games in by_date.items():
        # v3: a side is a pick only when the best price available beats its fair chance (expected return >= EV_PICK)
        cands = []
        for game in games:
            if game.get("frozen") or game["start"] <= now:
                continue
            for m in game["markets"]:
                for s in m["sides"]:
                    if s.get("ev") is not None and s["ev"] >= EV_PICK and s.get("consensus"):
                        cands.append((s["ev"], game, m, s))
        cands.sort(key=lambda c: -c[0])
        best = None
        pslate = prev["slates"].get(date)
        if pslate and pslate.get("best"):
            best = pslate["best"]
        elif cands and cands[0][0] >= EV_BEST:
            ev, game, m, s = cands[0]
            best = {"gameId": game["id"], "market": m["type"], "side": s["key"], "label": s["label"], "score": s["score"], "ev": ev, "method": 3}
        for ev, game, m, s in cands:
            pid = f"{date}-{game['id']}-{m['type']}-{s['key']}"
            if pid in prev["pick_ids"]:
                continue
            is_best = bool(best and best["gameId"] == game["id"] and best["market"] == m["type"] and best["side"] == s["key"])
            sc = s["score"]
            price = {k: v for k, v in s["best"].items() if k in ("venue", "odds", "cents", "breakEven")}
            picks.append({"id": pid, "doc": {
                "date": date, "league": game["league"], "gameId": game["id"], "start": game["start"],
                "matchup": f"{game['away']} @ {game['home']}", "market": m["type"], "side": s["key"],
                "label": s["label"], "line": s["line"], "price": price, "score": sc,
                "signals": s["signals"], "isBest": is_best, "flaggedAt": now, "status": "open",
                "modelProb": s.get("modelProb"), "consProb": (s.get("consensus") or {}).get("prob") or s["dkFair"],
                "method": 3, "ev": s["ev"], "fair": s["fair"], "fairNow": s["fairNow"], "expMove": s["expMove"], "tilt": s["tilt"],
                "kelly": s["best"]["kelly"], "contrib": s["contrib"],
                "maker": (dict(s["kalshiMaker"], status="posted", postedAt=now)   # only worth posting when it beats taking the best price
                          if s.get("kalshiMaker") and s["kalshiMaker"]["breakEven"] < price["breakEven"] - 1e-9 else None),
                "closeLine": None, "closeProb": None, "clvPoints": None, "clvProb": None,
                "result": None, "units": None, "finalScore": None,
            }})
        slates[date] = {
            "date": date, "updatedAt": now, "games": sorted(games, key=lambda x: (x["start"], x["id"])),
            "best": best, "leagues": sorted({x["league"] for x in games}),
            "sources": raw["sources"],
            "notes": [],
        }
    return slates, picks, maker_updates(raw, prev.get("picks") or {}, now), new_snaps


def maker_updates(raw, picks, now):
    """Paper limit orders on Kalshi: a posted order fills only if a later snapshot shows the market's ask at or below
    our price (the market came to us); an order still unfilled at tip-off is cancelled."""
    games, upd = {g["id"]: g for g in raw["games"]}, {}
    for pid, p in picks.items():
        mk = p.get("maker")
        if not mk or mk.get("status") != "posted":
            continue
        if p["start"] <= now:
            upd[pid] = {"maker": dict(mk, status="expired")}
            continue
        g = games.get(p["gameId"])
        q = kalshi_side_quote(g, p["market"], p["side"], mk) if g else None
        if q and q[1] <= mk["limit"] + 1e-9:
            upd[pid] = {"maker": dict(mk, status="filled", filledAt=now)}
    return upd


def main():
    raw = json.load(open(sys.argv[1]))
    out = sys.argv[2]
    prev = {"slates": {}, "pick_ids": set()}
    if "--prev" in sys.argv:
        pd = sys.argv[sys.argv.index("--prev") + 1]
        sd = os.path.join(pd, "slates")
        if os.path.isdir(sd):
            for f in os.listdir(sd):
                prev["slates"][f[:-5]] = json.load(open(os.path.join(sd, f)))
        pkd = os.path.join(pd, "picks")
        if os.path.isdir(pkd):
            prev["pick_ids"] = {f[:-5] for f in os.listdir(pkd)}
            prev["picks"] = {f[:-5]: json.load(open(os.path.join(pkd, f))) for f in os.listdir(pkd) if f.endswith(".json")}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    model = load_model()
    try:
        snaps = json.load(open(os.path.join(root, "snaps.json"))).get("games", {})
    except Exception:  # noqa: BLE001
        snaps = {}
    slates, picks, pupd, new_snaps = build(raw, prev, model, snaps)
    os.makedirs(out, exist_ok=True)
    json.dump(new_snaps, open(os.path.join(out, "snaps_new.json"), "w"), separators=(",", ":"))
    print("model", "v3 fitted " + model["fittedAt"] if model.get("fittedAt") else "priors only (no model.json yet)",
          "| new snapshots", sum(len(v) for v in new_snaps.values()))
    for sub in ("slates", "picks", "meta", "pickupd"):
        os.makedirs(os.path.join(out, sub), exist_ok=True)
    for k, v in pupd.items():
        json.dump(v, open(os.path.join(out, "pickupd", f"{k}.json"), "w"))
    if pupd:
        print("limit orders", {k: v["maker"]["status"] for k, v in pupd.items()})
    for d, s in slates.items():
        json.dump(s, open(os.path.join(out, "slates", f"{d}.json"), "w"))
    for p in picks:
        json.dump(p["doc"], open(os.path.join(out, "picks", f"{p['id']}.json"), "w"))
    status = {"lastRefresh": raw["capturedAt"], "dates": sorted(slates), "notes": raw.get("notes", [])}
    json.dump(status, open(os.path.join(out, "meta", "status.json"), "w"))
    for d, s in slates.items():
        print(d, "games", len(s["games"]), "best", s["best"])
    print("picks", len(picks))
    for p in picks:
        print(" ", p["id"], p["doc"]["label"], "edge", p["doc"]["ev"], "fair", p["doc"]["fair"], p["doc"]["price"], "size", p["doc"]["kelly"])


if __name__ == "__main__":
    main()
