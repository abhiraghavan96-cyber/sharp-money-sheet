"""Nightly learning step: market ratings from closing lines, and the two small models behind the fair price.

python3 learn.py ROOT_DIR
Reads hist.json (every finished game this season: DraftKings open/close, score, ESPN's pregame chance),
calib.json (every finished game the sheet tracked: each source's chance at the close, and the result) and
moves.json (the sheet's own snapshots: inputs some hours before a game, and where the consensus closed).
Writes model.json. Python standard library only.

1. Market ratings. Team ratings that best reproduce this season's closing spreads (and a per-team total),
   recent games weighted more. They are the market's own season-long view of each team.
2. Move model. How much of each input's gap shows up in the closing line. Target: closing chance minus the
   chance at the time, same line. Lines settle fast, so this learns in weeks.
3. Result model. Edge beyond the closing line, scored against results. Very noisy; its weights stay near the
   priors until hundreds of games say otherwise.
Both models are ridge regressions pulled toward the priors in score.py, so they move only as far as the data earns.
Each is checked on the most recent quarter of games, which the fit never saw.
"""
import bisect, json, math, os, sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from score import (nv, at_line, clamp, _nd, SD_MARGIN, SD_TOTAL, MOVE_FEATS, MOVE_PRIOR, RESULT_FEATS, RESULT_PRIOR,  # noqa: E402
                   MOVE_N0, RESULT_N0, TAU_HOURS)

HALF_LIFE = {"NBA": 30.0, "NFL": 56.0, "NCAAB": 45.0}   # days; older closing lines count half as much
PRO = ("NBA", "NFL")                      # the main sheet's leagues: one shared move model and one result model
COLLEGE = ("NCAAB",)                      # watch-only: fitted separately into model.json "college", never mixed into PRO
RATING_RIDGE = 2.0                        # each team's rating starts as if it had two average games
MIN_GAMES = 4                             # a team needs this many games before its rating is used
SEASON_DAYS = 150                         # college: only games in the last 150 days count toward MIN_GAMES (this season, not last)
INTERCEPT_STRENGTH = 5.0                  # a blanket home or over bias needs five times the evidence of a real input


# ---------- small linear algebra ----------
def solve(A, b):
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[piv][c]) < 1e-12:
            raise ValueError("singular")
        M[c], M[piv] = M[piv], M[c]
        for r in range(n):
            if r != c and M[r][c]:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] for i in range(n)]


def inv_diag(A):
    n = len(A)
    return [solve(A, [1.0 if j == i else 0.0 for j in range(n)])[i] for i in range(n)]


# ---------- market ratings ----------
def days_between(a, b):
    return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days


def fit_ratings(games, lg, asof):
    """Ratings from closing lines of games played before `asof` (YYYY-MM-DD)."""
    gs = [g for g in games if g["lg"] == lg and g["date"] < asof]
    teams = sorted({g["home"] for g in gs} | {g["away"] for g in gs})
    if len(gs) < 10 or len(teams) < 4:
        return None
    ix = {t: i + 1 for i, t in enumerate(teams)}
    n = len(teams) + 1
    out = {"hca": 0.0, "team": {}, "T": 0.0, "tot": {}, "n": len(gs), "minGames": MIN_GAMES, "asof": asof}
    count = {t: 0 for t in teams}
    for g in gs:
        count[g["home"]] += 1
        count[g["away"]] += 1
    for kind in ("sp", "tot"):
        A = [[0.0] * n for _ in range(n)]
        b = [0.0] * n
        m = 0
        for g in gs:
            if not g.get(kind):
                continue
            w = 0.5 ** (days_between(g["date"], asof) / HALF_LIFE[lg])
            if kind == "sp":
                y, xs = -g["sp"][1], [(0, 0.0 if g.get("neutral") else 1.0), (ix[g["home"]], 1.0), (ix[g["away"]], -1.0)]
            else:
                y, xs = g["tot"][1], [(0, 1.0), (ix[g["home"]], 1.0), (ix[g["away"]], 1.0)]
            for i, xi in xs:
                b[i] += w * xi * y
                for j, xj in xs:
                    A[i][j] += w * xi * xj
            m += 1
        if m < 10:
            continue
        for i in range(1, n):
            A[i][i] += RATING_RIDGE
        A[0][0] += 1e-6
        beta = solve(A, b)
        if kind == "sp":
            out["hca"] = round(beta[0], 2)
            out["team"] = {t: [round(beta[ix[t]], 2), count[t]] for t in teams}
        else:
            out["T"] = round(beta[0], 2)
            out["tot"] = {t: [round(beta[ix[t]], 2), count[t]] for t in teams}
    return out if out["team"] else None


class SparseRatings:
    """fit_ratings for leagues with hundreds of teams (college). Same model and output, solved incrementally:
    asked for dates in increasing order, it keeps the normal equations, decays them forward (every weight halves each
    half-life, so the whole sum scales by one factor), adds the new games, and runs Gauss-Seidel from the last answer."""
    C = "~hca"   # the intercept (home edge for spreads, league total for totals)

    def __init__(self, games, lg):
        self.lg = lg
        self.gs = sorted((g for g in games if g["lg"] == lg), key=lambda g: g["date"])
        self.i, self.cur, self.count, self.dates = 0, None, {}, {}
        self.A = {"sp": {}, "tot": {}}
        self.b = {"sp": {}, "tot": {}}
        self.m = {"sp": 0, "tot": 0}
        self.beta = {"sp": {}, "tot": {}}
        self.cache = {}

    def _add(self, kind, xs, y, w):
        A, b = self.A[kind], self.b[kind]
        for i, xi in xs:
            if not xi:
                continue
            b[i] = b.get(i, 0.0) + w * xi * y
            row = A.setdefault(i, {})
            for j, xj in xs:
                if xj:
                    row[j] = row.get(j, 0.0) + w * xi * xj

    def _solve(self, kind):
        A, b, beta = self.A[kind], self.b[kind], self.beta[kind]
        for _ in range(2000):
            delta = 0.0
            for i, row in A.items():
                diag = row.get(i, 0.0) + (1e-6 if i == self.C else RATING_RIDGE)
                new = (b.get(i, 0.0) - sum(v * beta.get(j, 0.0) for j, v in row.items() if j != i)) / diag
                delta = max(delta, abs(new - beta.get(i, 0.0)))
                beta[i] = new
            if delta < 1e-4:
                break
        return beta

    def at(self, asof):
        if asof in self.cache:
            return self.cache[asof]
        if self.cur is not None and asof < self.cur:
            raise ValueError("SparseRatings: dates must not go backwards")
        if self.cur is not None and asof > self.cur:
            f = 0.5 ** (days_between(self.cur, asof) / HALF_LIFE[self.lg])
            for kind in ("sp", "tot"):
                for row in self.A[kind].values():
                    for j in row:
                        row[j] *= f
                for i in self.b[kind]:
                    self.b[kind][i] *= f
        self.cur = asof
        while self.i < len(self.gs) and self.gs[self.i]["date"] < asof:
            g = self.gs[self.i]
            self.i += 1
            w = 0.5 ** (days_between(g["date"], asof) / HALF_LIFE[self.lg])
            for t in (g["home"], g["away"]):
                self.dates.setdefault(t, []).append(g["date"])
            if g.get("sp"):
                self._add("sp", [(self.C, 0.0 if g.get("neutral") else 1.0), (g["home"], 1.0), (g["away"], -1.0)], -g["sp"][1], w)
                self.m["sp"] += 1
            if g.get("tot"):
                self._add("tot", [(self.C, 1.0), (g["home"], 1.0), (g["away"], 1.0)], g["tot"][1], w)
                self.m["tot"] += 1
        # games this season only: last season's games still shape the rating a little (heavily decayed), but a team
        # needs MIN_GAMES of its own this season before the rating is used, as in the NBA and NFL
        cutoff = (datetime.strptime(asof, "%Y-%m-%d") - timedelta(days=SEASON_DAYS)).strftime("%Y-%m-%d")
        self.count = {t: len(ds) - bisect.bisect_left(ds, cutoff) for t, ds in self.dates.items()}
        teams = sorted(self.count)
        out = None
        if self.i >= 10 and len(teams) >= 4:
            out = {"hca": 0.0, "team": {}, "T": 0.0, "tot": {}, "n": self.i, "minGames": MIN_GAMES, "asof": asof}
            for kind in ("sp", "tot"):
                if self.m[kind] < 10:
                    continue
                beta = self._solve(kind)
                if kind == "sp":
                    out["hca"] = round(beta.get(self.C, 0.0), 2)
                    out["team"] = {t: [round(beta.get(t, 0.0), 2), self.count[t]] for t in teams}
                else:
                    out["T"] = round(beta.get(self.C, 0.0), 2)
                    out["tot"] = {t: [round(beta.get(t, 0.0), 2), self.count[t]] for t in teams}
            out = out if out["team"] else None
        self.cache[asof] = out
        return out


def rating_pred(rt, g):
    th, ta = rt["team"].get(g["home"]), rt["team"].get(g["away"])
    if not th or not ta or min(th[1], ta[1]) < MIN_GAMES:
        return None
    oh, oa = rt["tot"].get(g["home"]), rt["tot"].get(g["away"])
    return ((0 if g.get("neutral") else rt["hca"]) + th[0] - ta[0], (rt["T"] + oh[0] + oa[0]) if oh and oa else None)


# ---------- training rows ----------
def hist_rows(hist):
    """Move rows (open -> close) and result rows (close -> final score) from the season's history,
    using only ratings fit on games before each game's date."""
    N = _nd()
    games = sorted(hist.values(), key=lambda g: (g["date"], g.get("start", "")))
    cache, moves, results, rat_err = {}, [], [], {lg: [] for lg in PRO + COLLEGE}
    trackers = {}
    for g in games:
        lg = g["lg"]
        key = (lg, g["date"])
        if key not in cache:
            if lg in COLLEGE:
                cache[key] = trackers.setdefault(lg, SparseRatings(games, lg)).at(g["date"])
            else:
                cache[key] = fit_ratings(games, lg, g["date"])
        rt = cache[key]
        rp = rating_pred(rt, g) if rt else None
        res_margin, res_total = g["hs"] - g["as"], g["hs"] + g["as"]
        if g.get("sp"):
            Lo, Lc, ch, ca, oh, oa = g["sp"]
            try:
                po, pc = nv(oh, oa), nv(ch, ca)
            except (ZeroDivisionError, ValueError):
                po = pc = None
            if po is not None:
                sd = SD_MARGIN[lg]
                cov = lambda mu, L: N.cdf((mu + L) / sd)
                base_x = {} if g.get("neutral") else {"home": 1.0}
                xo = dict(base_x)
                if rp:
                    xo["rating"] = max(-0.15, min(0.15, cov(rp[0], Lo) - po))
                    rat_err[lg].append(("sp", rp[0] + Lc))
                moves.append({"date": g["date"], "lg": lg, "m": "spread", "src": "hist", "y": at_line(pc, Lc, Lo, "spread", "home", lg) - po, "x": xo, "tau": 1.0})
                v = res_margin + Lc
                if v != 0:
                    xc = dict(base_x)
                    if rp:
                        xc["rating"] = max(-0.15, min(0.15, cov(rp[0], Lc) - pc))
                    if g.get("wp"):
                        m_e = sd * N.inv_cdf(clamp(g["wp"], 0.01, 0.99))
                        xc["espn"] = max(-0.10, min(0.10, cov(m_e, Lc) - pc))
                    results.append({"date": g["date"], "lg": lg, "m": "spread", "src": "hist", "y": (1 if v > 0 else 0) - pc, "x": xc, "base": pc})
        if g.get("tot"):
            Lo, Lc, co, cu, oo, ou = g["tot"]
            try:
                po, pc = nv(oo, ou), nv(co, cu)
            except (ZeroDivisionError, ValueError):
                po = pc = None
            if po is not None:
                sd = SD_TOTAL[lg]
                ov = lambda mu, L: N.cdf((mu - L) / sd)
                xo = {"over": 1.0}
                if rp and rp[1] is not None:
                    xo["rating"] = max(-0.15, min(0.15, ov(rp[1], Lo) - po))
                    rat_err[lg].append(("tot", rp[1] - Lc))
                moves.append({"date": g["date"], "lg": lg, "m": "total", "src": "hist", "y": at_line(pc, Lc, Lo, "total", "over", lg) - po, "x": xo, "tau": 1.0})
                v = res_total - Lc
                if v != 0:
                    xc = {"over": 1.0}
                    if rp and rp[1] is not None:
                        xc["rating"] = max(-0.15, min(0.15, ov(rp[1], Lc) - pc))
                    results.append({"date": g["date"], "lg": lg, "m": "total", "src": "hist", "y": (1 if v > 0 else 0) - pc, "x": xc, "base": pc})
    return moves, results, rat_err


def live_rows(calib, moves_live):
    """Rows from the sheet's own tracked games (all inputs available, consensus as the base)."""
    mv, rs = [], []
    cols = moves_live.get("cols") or []
    for r in (moves_live.get("rows") or {}).values():
        d = dict(zip(cols, r))
        x = {k: d.get(k) for k in MOVE_FEATS if d.get(k)}
        mv.append({"date": d["date"], "lg": d["league"], "m": d["market"], "src": "live", "y": d["y"], "x": x,
                   "tau": clamp((d.get("hours") or 0) / TAU_HOURS)})
    cols = calib.get("cols") or []
    for r in (calib.get("rows") or {}).values():
        d = dict(zip(cols, r))
        base = d.get("cons") if d.get("cons") is not None else d.get("dk")
        if base is None or d.get("res") is None:
            continue
        clip = lambda v, c=0.15: max(-c, min(c, v))
        x = {"home" if d["market"] == "spread" else "over": 1.0}
        if d.get("kalshi") is not None:
            x["kalshi"] = clip(d["kalshi"] - base)
        if d.get("cons") is not None and d.get("dk") is not None:
            x["dk"] = clip(d["dk"] - base)
        if d.get("espn") is not None:
            x["espn"] = clip(d["espn"] - base, 0.10)
        if d.get("rating") is not None:
            x["rating"] = clip(d["rating"] - base)
        for k in ("rest", "health", "wind"):
            if d.get(k):
                x[k] = d[k]
        rs.append({"date": d["date"], "lg": d["league"], "m": d["market"], "src": "live", "y": d["res"] - base, "x": x, "base": base})
    return mv, rs


# ---------- the shrunk regression ----------
def ridge(rows, feats, prior, n0, scale_tau=False):
    k = len(feats)
    X = [[r["x"].get(f, 0.0) * (r["tau"] if scale_tau else 1.0) for f in feats] for r in rows]
    y = [r["y"] for r in rows]
    n = len(rows)
    b0 = [prior.get(f, 0.0) for f in feats]
    # prior strength: n0 rows' worth of evidence at the feature's typical size, measured only over the rows where the
    # feature exists, so an input seen in four games stays at its prior until there are hundreds
    ex2 = []
    for j in range(k):
        nz = [x[j] ** 2 for x in X if x[j]]
        ex2.append(max(1e-10, sum(nz) / len(nz)) if nz else 1e-10)
    lam = [n0 * e * (INTERCEPT_STRENGTH if f in ("home", "over") else 1.0) for f, e in zip(feats, ex2)]
    A = [[sum(x[i] * x[j] for x in X) + (lam[i] if i == j else 0.0) for j in range(k)] for i in range(k)]
    b = [sum(x[i] * yy for x, yy in zip(X, y)) + lam[i] * b0[i] for i in range(k)]
    beta = solve(A, b)
    resid = [yy - sum(bj * xj for bj, xj in zip(beta, x)) for x, yy in zip(X, y)]
    s2 = sum(e * e for e in resid) / max(1, n - 1)
    se = [math.sqrt(max(0.0, s2 * d)) for d in inv_diag(A)]
    # how far the data moved each weight from its prior (0 = all prior, 1 = all data)
    pull = [(sum(x[j] ** 2 for x in X)) / (sum(x[j] ** 2 for x in X) + lam[j]) for j in range(k)]
    return beta, se, pull


def predict(beta, feats, r, scale_tau=False):
    return sum(b * r["x"].get(f, 0.0) for b, f in zip(beta, feats)) * (r["tau"] if scale_tau else 1.0)


def evaluate(rows, feats, prior, n0, scale_tau):
    """Fit on the oldest 75% of rows, score on the newest 25%: mean squared error of the model, of the priors alone,
    and of predicting nothing (no move / no edge beyond the base)."""
    rows = sorted(rows, key=lambda r: r["date"])
    if len(rows) < 40:
        return None
    cut = int(len(rows) * 0.75)
    train, test = rows[:cut], rows[cut:]
    beta, _, _ = ridge(train, feats, prior, n0, scale_tau)
    b0 = [prior.get(f, 0.0) for f in feats]
    mse = lambda bb: sum((r["y"] - predict(bb, feats, r, scale_tau)) ** 2 for r in test) / len(test)
    m_fit, m_prior, m_zero = mse(beta), mse(b0), mse([0.0] * len(feats))
    out = {"train": len(train), "test": len(test), "from": test[0]["date"], "mseModel": m_fit, "msePrior": m_prior, "mseZero": m_zero,
           "skillVsZero": round(1 - m_fit / m_zero, 4) if m_zero else None, "skillVsPrior": round(1 - m_fit / m_prior, 4) if m_prior else None}
    big = [(predict(beta, feats, r, scale_tau), r["y"]) for r in test]
    big = [(p, yy) for p, yy in big if abs(p) >= 0.005]
    if big:
        out["direction"] = {"n": len(big), "hit": round(sum(1 for p, yy in big if p * yy > 0) / len(big), 3)}
    for k in ("mseModel", "msePrior", "mseZero"):
        out[k] = round(out[k], 6)
    return out


def fit(rows, feats, prior, n0, scale_tau):
    if not rows:
        return {"coef": dict(prior), "se": {}, "pull": {}, "n": 0, "check": None, "bySource": {}}
    beta, se, pull = ridge(rows, feats, prior, n0, scale_tau)
    return {"coef": {f: round(b, 4) for f, b in zip(feats, beta)}, "se": {f: round(s, 4) for f, s in zip(feats, se)},
            "pull": {f: round(p, 3) for f, p in zip(feats, pull)}, "n": len(rows),
            "bySource": {s: sum(1 for r in rows if r["src"] == s) for s in ("hist", "live")},
            "check": evaluate(rows, feats, prior, n0, scale_tau)}


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def main():
    root = sys.argv[1]
    hist = (load(os.path.join(root, "hist.json"), {}) or {}).get("games") or {}
    calib = load(os.path.join(root, "calib.json"), {}) or {}
    moves_live = load(os.path.join(root, "moves.json"), {}) or {}
    now = datetime.now(timezone.utc)
    today, tomorrow = now.strftime("%Y-%m-%d"), (now + timedelta(days=1)).strftime("%Y-%m-%d")

    games = list(hist.values())
    ratings, rating_check = {}, {}
    hmv, hrs, rat_err = hist_rows(hist)
    for lg in PRO + COLLEGE:
        if not any(g["lg"] == lg for g in games):
            continue
        rt = SparseRatings(games, lg).at(tomorrow) if lg in COLLEGE else fit_ratings(games, lg, tomorrow)
        if rt:
            ratings[lg] = rt
        e = rat_err[lg]
        sp = [abs(v) for k, v in e if k == "sp"]
        tt = [abs(v) for k, v in e if k == "tot"]
        res_sp = [g["hs"] - g["as"] + g["sp"][1] for g in games if g["lg"] == lg and g.get("sp")]
        res_tt = [g["hs"] + g["as"] - g["tot"][1] for g in games if g["lg"] == lg and g.get("tot")]
        rating_check[lg] = {
            "games": sum(1 for g in games if g["lg"] == lg),
            "spreadMAE": round(sum(sp) / len(sp), 2) if sp else None, "spreadN": len(sp),
            "totalMAE": round(sum(tt) / len(tt), 2) if tt else None, "totalN": len(tt),
            # how far final results land from the closing number (the bell curve's width the simulator assumes)
            "marginSD": round(math.sqrt(sum(v * v for v in res_sp) / len(res_sp)), 1) if len(res_sp) >= 20 else None,
            "totalSD": round(math.sqrt(sum(v * v for v in res_tt) / len(res_tt)), 1) if len(res_tt) >= 20 else None,
            "assumedMarginSD": SD_MARGIN[lg], "assumedTotalSD": SD_TOTAL[lg],
        }
    lmv, lrs = live_rows(calib, moves_live)
    move = fit([r for r in hmv + lmv if r["lg"] in PRO], MOVE_FEATS, MOVE_PRIOR, MOVE_N0, True)
    result = fit([r for r in hrs + lrs if r["lg"] in PRO], RESULT_FEATS, RESULT_PRIOR, RESULT_N0, False)
    # college: its own move and result models from its own history and its own tracked games
    cmv, crs = live_rows(load(os.path.join(root, "college_calib.json"), {}) or {}, load(os.path.join(root, "college_moves.json"), {}) or {})
    college = {}
    for lg in COLLEGE:
        if not any(g["lg"] == lg for g in games):
            continue
        cm = fit([r for r in hmv + cmv if r["lg"] == lg], MOVE_FEATS, MOVE_PRIOR, MOVE_N0, True)
        cr = fit([r for r in hrs + crs if r["lg"] == lg], RESULT_FEATS, RESULT_PRIOR, RESULT_N0, False)
        college[lg] = {"move": dict(cm, prior=MOVE_PRIOR, n0=MOVE_N0), "result": dict(cr, prior=RESULT_PRIOR, n0=RESULT_N0)}
    model = {"version": 3, "fittedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "asof": today,
             "ratings": ratings, "ratingCheck": rating_check,
             "move": dict(move, prior=MOVE_PRIOR, n0=MOVE_N0), "result": dict(result, prior=RESULT_PRIOR, n0=RESULT_N0),
             "college": college}
    with open(os.path.join(root, "model.json"), "w") as f:
        json.dump(model, f, separators=(",", ":"))
    print("model.json: ratings", {k: v["n"] for k, v in ratings.items()}, "| move rows", move["n"], move["bySource"], "| result rows", result["n"], result["bySource"])
    print("move coef", move["coef"])
    print("move check", move["check"])
    print("result coef", result["coef"])
    print("result check", result["check"])
    print("rating check", rating_check)
    for lg, c in college.items():
        print("college", lg, "| move rows", c["move"]["n"], c["move"]["bySource"], "coef", c["move"]["coef"], "check", c["move"]["check"])
        print("college", lg, "| result rows", c["result"]["n"], c["result"]["bySource"], "coef", c["result"]["coef"], "check", c["result"]["check"])


if __name__ == "__main__":
    main()
