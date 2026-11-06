"""Sharp Money Sheet scoring: raw snapshot JSON -> slate, pick and status documents.

Usage: python3 score.py raw.json out_dir [--prev prev_slate_dir]
Writes out_dir/slates/<date>.json, out_dir/picks/<id>.json, out_dir/meta/status.json
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
        "kalshiLiquid": liquid, "kalshiGap": None if gap is None else round(gap, 4),
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


def build(raw, prev=None):
    prev = prev or {"slates": {}, "pick_ids": set()}
    now = raw["capturedAt"]
    by_date, picks = {}, []
    from datetime import timedelta
    cutoff = (datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ") - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for g in raw["games"]:
        if g["start"] < cutoff and g["date"] not in prev["slates"]:
            continue  # finished before this run and never tracked: leave it out
        mk = [m for m in (market_eval(g, "spread"), market_eval(g, "total")) if m]
        probs = model_probs(g, next((m for m in mk if m["type"] == "spread"), None), next((m for m in mk if m["type"] == "total"), None))
        for m in mk:
            pr = probs.get(m["type"])
            for sd in m["sides"]:
                sd["modelProb"] = None if not pr else (pr["blend"] if sd["key"] in ("home", "over") else round(1 - pr["blend"], 4))
        game = {k: g[k] for k in ("league", "id", "start", "away", "awayName", "home", "homeName")}
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
        cands = []
        for game in games:
            for m in game["markets"]:
                for s in m["sides"]:
                    if s["activeSignals"] >= 2 and s["availMax"] >= 45:
                        cands.append((s["score"], game, m, s))
        cands.sort(key=lambda c: -c[0])
        best = None
        pslate = prev["slates"].get(date)
        if pslate and pslate.get("best"):
            best = pslate["best"]
        elif cands and cands[0][0] >= BEST_MIN:
            sc, game, m, s = cands[0]
            best = {"gameId": game["id"], "market": m["type"], "side": s["key"], "label": s["label"], "score": sc}
        for sc, game, m, s in cands:
            if sc < PICK_MIN:
                continue
            pid = f"{date}-{game['id']}-{m['type']}-{s['key']}"
            if pid in prev["pick_ids"] or game.get("frozen") or game["start"] <= now:
                continue
            is_best = bool(best and best["gameId"] == game["id"] and best["market"] == m["type"] and best["side"] == s["key"])
            price = ({"venue": "Kalshi", "cents": round(s["kalshiCost"] * 100, 1), "breakEven": s["kalshiBreakEven"]}
                     if s["bestVenue"] == "Kalshi" else
                     {"venue": "DraftKings", "odds": s["odds"], "breakEven": s["dkBreakEven"]})
            picks.append({"id": pid, "doc": {
                "date": date, "league": game["league"], "gameId": game["id"], "start": game["start"],
                "matchup": f"{game['away']} @ {game['home']}", "market": m["type"], "side": s["key"],
                "label": s["label"], "line": s["line"], "price": price, "score": sc,
                "signals": s["signals"], "isBest": is_best, "flaggedAt": now, "status": "open",
                "modelProb": s.get("modelProb"), "consProb": (s.get("consensus") or {}).get("prob") or s["dkFair"],
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
    return slates, picks, maker_updates(raw, prev.get("picks") or {}, now)


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
    slates, picks, pupd = build(raw, prev)
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
        print(" ", p["id"], p["doc"]["label"], p["doc"]["score"], p["doc"]["signals"], p["doc"]["price"])


if __name__ == "__main__":
    main()
