"""Move the app's state between its published data.json and the folders score.py and grade.py use.

python3 pack.py unpack data.json state/
    -> state/slates/<date>.json, state/picks/<id>.json
python3 pack.py pack state/ out/ gradeout/ data.json CAPTURED_AT_ISO
    -> data.json with slates from out/ replacing state/, new picks from out/, limit-order fill updates from out/pickupd/ and
       grading updates from gradeout/ merged in, slates older than 21 days dropped
(Pass a folder that doesn't exist for out/ or gradeout/ when a run has none.)
"""
import json, os, sys
from datetime import datetime, timedelta

KEEP_DAYS = 21


def read_dir(d):
    if not d or not os.path.isdir(d):
        return {}
    return {f[:-5]: json.load(open(os.path.join(d, f))) for f in os.listdir(d) if f.endswith(".json")}


def unpack(src, state):
    data = json.load(open(src)) if os.path.exists(src) else {}
    for sub in ("slates", "picks"):
        os.makedirs(os.path.join(state, sub), exist_ok=True)
        for k, v in (data.get(sub) or {}).items():
            json.dump(v, open(os.path.join(state, sub, f"{k}.json"), "w"))
    print("unpacked", {k: len(data.get(k) or {}) for k in ("slates", "picks")})


def pack(state, out, gradeout, dst, now):
    slates = read_dir(os.path.join(state, "slates"))
    slates.update(read_dir(os.path.join(out, "slates")))
    picks = read_dir(os.path.join(state, "picks"))
    for k, v in read_dir(os.path.join(out, "picks")).items():
        picks.setdefault(k, v)
    for k, v in read_dir(os.path.join(out, "pickupd")).items():  # paper Kalshi limit orders that filled or expired
        if k in picks:
            picks[k] = dict(picks[k], **v)
    for k, v in read_dir(os.path.join(gradeout, "picks")).items():
        if k in picks:
            picks[k] = dict(picks[k], **v)
    cutoff = (datetime.strptime(now[:10], "%Y-%m-%d") - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    slates = {k: v for k, v in slates.items() if k >= cutoff}
    data = {"version": 2, "updatedAt": now, "slates": slates, "picks": picks,
            "status": {"lastRun": now, "dates": sorted(slates), "picks": len(picks),
                       "graded": sum(1 for p in picks.values() if p.get("result"))}}
    json.dump(data, open(dst, "w"), separators=(",", ":"))
    print("packed", len(slates), "slates,", len(picks), "picks,", os.path.getsize(dst), "bytes")


if __name__ == "__main__":
    if sys.argv[1] == "unpack":
        unpack(sys.argv[2], sys.argv[3])
    else:
        pack(*sys.argv[2:7])
