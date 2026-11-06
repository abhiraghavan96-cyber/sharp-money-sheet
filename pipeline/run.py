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
        py("grade.py", w("finals.json"), w("state"), w("gradeout"))
        py("pack.py", "pack", w("state"), w("none"), w("gradeout"), data, now)
    else:
        py("score.py", w("raw.json"), w("out"), "--prev", w("state"))
        py("reads.py", w("out", "slates"))
        py("pack.py", "pack", w("state"), w("out"), w("none"), data, now)
        src = w("in", "props.json")
        if os.path.exists(src):  # players, Kalshi prop ladders and team ratings for the Test-a-bet tab
            with open(src) as f:
                pr = json.load(f)
            pr["updatedAt"] = now
            with open(os.path.join(ROOT, "props.json"), "w") as f:
                json.dump(pr, f, separators=(",", ":"))
            print("props.json", len(pr.get("players", [])), "players")
    update_results(w("in", "results_new.json"), now)


RESULTS_DAYS = 14


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
