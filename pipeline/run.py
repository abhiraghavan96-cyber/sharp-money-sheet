"""One scheduled run of the Sharp Money Sheet, start to finish.

python3 pipeline/run.py refresh   # pull lines, prices, bets and context; score; write reads; update data.json
python3 pipeline/run.py grade     # pull final scores; grade open picks; update data.json
Run from the repository root. Uses only the Python standard library.
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

    py("pack.py", "unpack", data, w("state"))
    if not skip_fetch:
        py("fetch.py", mode, w("in"))
    py("merge.py", w("in"), w("raw.json"), w("finals.json"), now)
    if mode == "grade":
        py("grade.py", w("finals.json"), w("state"), w("gradeout"), os.path.join(ROOT, "results.json"))
        py("pack.py", "pack", w("state"), w("none"), w("gradeout"), data, now)
        update_calib(w("gradeout", "calib"), now)
    else:
        py("score.py", w("raw.json"), w("out"), "--prev", w("state"))
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
    update_results(w("in", "results_new.json"), now)


RESULTS_DAYS = 14
CLOSED_DAYS = 5
CALIB_DAYS = 365
CALIB_COLS = ["date", "league", "market", "line", "res", "cons", "dk", "kalshi", "espn", "power", "blend"]


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


def update_calib(src, now):
    """Fold the night's accuracy rows (every finished game, every source's pre-game probability) into calib.json."""
    if not os.path.isdir(src):
        return
    dst = os.path.join(ROOT, "calib.json")
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
    print("calib.json", len(cur["rows"]), "rows,", n, "from this run")


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
