"""One scheduled run of the Sharp Money Sheet, start to finish.

python3 pipeline/run.py refresh   # pull lines, prices, bets and context; score; write reads; update data.json
python3 pipeline/run.py grade     # pull final scores; grade open picks; accuracy and move rows; refit model.json
python3 pipeline/run.py backfill  # one-off: every finished game this season into hist.json, then refit model.json
Run from the repository root. Uses only the Python standard library.

College basketball (watch-only) runs as its own track after the NBA/NFL sheet: same scoring and grading code, its own
state file (college.json), its own accuracy and move tables (college_calib.json, college_moves.json) and its own
model (model.json "college"). A college failure is logged and never stops the main sheet.
"""
import json, os, shutil, subprocess, sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORK = os.path.join(ROOT, "work")


def py(script, *args):
    print(">", script, *args, flush=True)
    subprocess.run([sys.executable, os.path.join(HERE, script), *args], check=True)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "refresh"
    skip_fetch = "--skip-fetch" in sys.argv
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not skip_fetch and os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK, exist_ok=True)
    w = lambda *p: os.path.join(WORK, *p)
    data = os.path.join(ROOT, "data.json")

    if mode == "backfill":
        if not skip_fetch:
            py("fetch.py", "backfill", w("in"))
        update_hist(w("in", "hist_new.json"), now)
        py("learn.py", ROOT)
        return
    py("pack.py", "unpack", data, w("state"))
    if not skip_fetch:
        py("fetch.py", mode, w("in"))
    py("merge.py", w("in"), w("raw.json"), w("finals.json"), now)
    split_raw(w("raw.json"), w("raw_pro.json"), w("raw_college.json"))
    if mode == "grade":
        py("grade.py", w("finals.json"), w("state"), w("gradeout"), os.path.join(ROOT, "results.json"))
        py("pack.py", "pack", w("state"), w("none"), w("gradeout"), data, now)
        update_calib(w("gradeout", "calib"), now)
        update_table("moves.json", MOVES_COLS, w("gradeout", "moves"), now, MOVES_DAYS)
    else:
        py("score.py", w("raw_pro.json"), w("out"), "--prev", w("state"))
        update_snaps(w("out", "snaps_new.json"), now)
        py("reads.py", w("out", "slates"))
        py("pack.py", "pack", w("state"), w("out"), w("none"), data, now)
        src = w("in", "props.json")
        if os.path.exists(src):  # players, Kalshi prop ladders and team ratings for the Test-a-bet tab
            with open(src) as f:
                pr = json.load(f)
            if not pr.get("players") and os.path.exists(os.path.join(ROOT, "props.json")):
                print("props.json kept: this run found no players (a source probably failed)")
                src = None
        if src and os.path.exists(src):
            pr["updatedAt"] = now
            pr["closed"] = closed_ladders(now)
            with open(os.path.join(ROOT, "props.json"), "w") as f:
                json.dump(pr, f, separators=(",", ":"))
            print("props.json", len(pr.get("players", [])), "players")
    college(mode, now, w)
    update_results(w("in", "results_new.json"), now)
    update_hist(w("in", "hist_new.json"), now)
    if mode == "grade":
        py("learn.py", ROOT)


COLLEGE_LGS = ("NCAAB",)
COLLEGE_KEEP_DAYS = 2   # college slates are big (dozens of games a day): keep enough to grade, not three weeks


def split_raw(src, pro, college_dst):
    raw = read_json(src, None)
    if not raw:
        return
    for dst, sel in ((pro, lambda g: g["league"] not in COLLEGE_LGS), (college_dst, lambda g: g["league"] in COLLEGE_LGS)):
        with open(dst, "w") as f:
            json.dump(dict(raw, games=[g for g in raw["games"] if sel(g)]), f)


def college(mode, now, w):
    """The watch-only college track: score (refresh) or grade (nightly) into college.json."""
    dst = os.path.join(ROOT, "college.json")
    try:
        py("pack.py", "unpack", dst, w("cstate"))
        if mode == "grade":
            py("grade.py", w("finals.json"), w("cstate"), w("cgradeout"))
            py("pack.py", "pack", w("cstate"), w("none"), w("cgradeout"), dst, now, str(COLLEGE_KEEP_DAYS))
            update_calib(w("cgradeout", "calib"), now, "college_calib.json")
            update_table("college_moves.json", MOVES_COLS, w("cgradeout", "moves"), now, MOVES_DAYS)
        else:
            py("score.py", w("raw_college.json"), w("cout"), "--prev", w("cstate"))
            update_snaps(w("cout", "snaps_new.json"), now)
            py("pack.py", "pack", w("cstate"), w("cout"), w("none"), dst, now, str(COLLEGE_KEEP_DAYS))
    except Exception as e:  # noqa: BLE001
        print("college track failed (watch-only; the NBA/NFL sheet is unaffected):", e)


RESULTS_DAYS = 14
SNAPS_DAYS = 4   # snapshots are turned into move rows the first night a game is graded
MOVES_DAYS = 240
sys.path.insert(0, HERE)
from score import MOVE_FEATS  # noqa: E402
MOVES_COLS = ["date", "league", "market", "bucket", "hours", "y"] + MOVE_FEATS


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def write_json(name, obj):
    with open(os.path.join(ROOT, name), "w") as f:
        json.dump(obj, f, separators=(",", ":"))


def update_hist(src, now):
    """Fold newly finished games into hist.json (DraftKings open/close, score, ESPN's pregame chance) for learn.py."""
    new = read_json(src, None)
    if not new:
        return
    cur = read_json(os.path.join(ROOT, "hist.json"), {"games": {}})
    cur.setdefault("games", {}).update(new)
    cur["updatedAt"] = now
    write_json("hist.json", cur)
    print("hist.json", len(cur["games"]), "games,", len(new), "new")


def update_snaps(src, now):
    """Keep the first pre-game snapshot of each game in each time window (12h+, 3-12h, under 3h) for the move model."""
    new = read_json(src, None)
    cur = read_json(os.path.join(ROOT, "snaps.json"), {"games": {}})
    games = cur.setdefault("games", {})
    for gid, v in (new or {}).items():
        for k, snap in v.items():
            games.setdefault(gid, {}).setdefault(k, snap)
    cutoff = (datetime.strptime(now[:10], "%Y-%m-%d") - timedelta(days=SNAPS_DAYS)).strftime("%Y-%m-%d")
    cur["games"] = {g: v for g, v in games.items() if max(s["t"] for s in v.values())[:10] >= cutoff}
    cur["updatedAt"] = now
    write_json("snaps.json", cur)
    print("snaps.json", len(cur["games"]), "games,", sum(len(v) for v in (new or {}).values()), "new snapshots")


def update_table(name, cols, src, now, days):
    """Merge a folder of row files (one JSON array each, first field the date) into a {cols, rows} table."""
    if not os.path.isdir(src):
        return
    cur = read_json(os.path.join(ROOT, name), {"cols": cols, "rows": {}})
    rows, n = cur.get("rows") or {}, 0
    for f in os.listdir(src):
        if f.endswith(".json"):
            rows[f[:-5]] = read_json(os.path.join(src, f), None)
            n += 1
    cutoff = (datetime.strptime(now[:10], "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
    write_json(name, {"cols": cols, "updatedAt": now, "rows": {k: v for k, v in rows.items() if v and v[0] >= cutoff}})
    print(name, len(rows), "rows,", n, "from this run")
CLOSED_DAYS = 5
CALIB_DAYS = 365
CALIB_COLS = ["date", "league", "market", "line", "res", "cons", "dk", "kalshi", "espn", "power", "blend", "rating", "fair", "rest", "health", "wind"]


def closed_ladders(now):
    """Closing Kalshi prop ladders: the last snapshot taken before each player's game started.
    My trades measures closing-line value on prop legs against these."""
    old = {}
    p = os.path.join(ROOT, "props.json")
    if os.path.exists(p):
        with open(p) as f:
            old = json.load(f)
    closed = dict(old.get("closed") or {})
    taken = old.get("updatedAt") or ""
    for pl in old.get("players") or []:
        st = pl.get("start") or ""
        if pl.get("ladders") and taken < st <= now:
            closed.setdefault(f"{pl['id']}|{pl.get('gameId')}", {"start": st, "lg": pl.get("lg"), "ladders": pl["ladders"]})
    cutoff = (datetime.strptime(now[:10], "%Y-%m-%d") - timedelta(days=CLOSED_DAYS)).strftime("%Y-%m-%d")
    return {k: v for k, v in closed.items() if v.get("start", "") >= cutoff}


def update_calib(src, now, name="calib.json"):
    """Fold the night's accuracy rows (every finished game, every source's pre-game probability) into calib.json."""
    if not os.path.isdir(src):
        return
    dst = os.path.join(ROOT, name)
    cur = {"cols": CALIB_COLS, "rows": {}}
    if os.path.exists(dst):
        with open(dst) as f:
            cur = json.load(f)
    rows, n = cur.get("rows") or {}, 0
    for f in os.listdir(src):
        if f.endswith(".json"):
            with open(os.path.join(src, f)) as fh:
                r = json.load(fh)
            rows[f[:-5]] = [r.get(c) for c in CALIB_COLS]
            n += 1
    cutoff = (datetime.strptime(now[:10], "%Y-%m-%d") - timedelta(days=CALIB_DAYS)).strftime("%Y-%m-%d")
    cur = {"cols": CALIB_COLS, "updatedAt": now, "rows": {k: v for k, v in rows.items() if v[0] >= cutoff}}
    with open(dst, "w") as f:
        json.dump(cur, f, separators=(",", ":"))
    print(name, len(cur["rows"]), "rows,", n, "from this run")


def update_results(src, now):
    """Fold newly finished games into results.json (final scores and box scores the app grades paper trades with)."""
    if not os.path.exists(src):
        return
    with open(src) as f:
        new = json.load(f)
    dst = os.path.join(ROOT, "results.json")
    cur = {"games": {}}
    if os.path.exists(dst):
        with open(dst) as f:
            cur = json.load(f)
    cur.setdefault("games", {}).update(new)
    cutoff = (datetime.strptime(now[:10], "%Y-%m-%d") - timedelta(days=RESULTS_DAYS)).strftime("%Y-%m-%d")
    cur["games"] = {k: v for k, v in cur["games"].items() if v.get("date", "9999") >= cutoff}
    cur["updatedAt"] = now
    with open(dst, "w") as f:
        json.dump(cur, f, separators=(",", ":"))
    print("results.json", len(cur["games"]), "games,", len(new), "new")


if __name__ == "__main__":
    main()
