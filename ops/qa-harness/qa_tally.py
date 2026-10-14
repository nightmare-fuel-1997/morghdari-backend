import json, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = {}
for line in open(os.path.join(HERE, "..", "qa-test-plan.html"), encoding="utf-8"):
    for m in re.finditer(r'\{id:"([\d.]+[a-z]?)",', line):
        MODEL[m.group(1)] = 1

OBSOLETE = {"18.2[farm-999]", "18.2[farm_1]", "18.2[farm%2E1]", "16.13", "5.18a", "5.18b", "6.9b2",
            "15.2a-hs", "15.2b-hs", "15.2c-hs", "15.2d-hs", "8.11b", "9.9d",
            "4.14b", "4.15a", "4.15c", "4.15d", "4.17a",
            # 15.7a-hs was the wave-4 record OF the BUG-7 crash itself (upgrade
            # expected, handshake 500). The fix removed that failure mode; fresh
            # post-fix verdicts are 15.7 / 15.7a / 15.7b (all PASS).
            "15.7a-hs"}

def to_model(hid):
    if hid in MODEL:
        return hid
    m = re.match(r"^(\d+\.\d+)", hid)
    if m and m.group(1) in MODEL:
        return m.group(1)
    return None

last = {}
for line in open(os.path.join(HERE, "qa_results.jsonl"), encoding="utf-8"):
    try:
        r = json.loads(line)
    except Exception:
        continue
    if r["id"] in OBSOLETE:
        continue
    last[r["id"]] = r["verdict"]

mapped = {}
for hid, v in last.items():
    m = to_model(hid)
    if m:
        mapped.setdefault(m, []).append(v)

agg = {}
for m, vs in mapped.items():
    if any(v == "FAIL" for v in vs):
        agg[m] = "FAIL"
    elif any(v == "PASS" for v in vs):
        agg[m] = "PASS"
    else:
        agg[m] = "NA"

def key(x):
    return [int(p) for p in re.match(r"^(\d+)\.(\d+)", x).groups()]
print(f"model tests: {len(MODEL)} | mapped: {len(mapped)}")
for k in ("PASS", "FAIL", "NA"):
    ids = sorted([m for m, v in agg.items() if v == k], key=key)
    print(f"{k}: {len(ids)}")
    if k == "FAIL":
        print("  " + ", ".join(ids))
todo = sorted(set(MODEL) - set(mapped), key=key)
print(f"NOT-YET-RUN: {len(todo)}")
grps = {}
for t in todo:
    grps.setdefault(t.split(".")[0], []).append(t)
for g in sorted(grps, key=int):
    print(f"  section {g}: {len(grps[g])} pending")
