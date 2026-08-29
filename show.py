import json, glob, sys, re

f = sorted(glob.glob("data/*.json"))[-1]
d = json.load(open(f, encoding="utf-8"))
want = sys.argv[1] if len(sys.argv) > 1 else "domestic"

rows = [n for n in d["news"] if n["scope"] == want]
print(f"{f}  scope={want}  count={len(rows)}")
print()

for n in rows:
    date = (n["published"] or "no-date")[:10]
    late = " [late]" if n.get("late") else ""
    print(f"[{n['primary_topic']:8}] {n['source'][:14]:14} {date}{late}")
    print(f"    {n['title'][:70]}")
    print(f"    matched: {','.join(n['matched'][:4])}")
    if n.get("tr_markers"):
        print(f"    tr: {','.join(n['tr_markers'])}")
    for w in n["matched"][:3]:
        stem = w.rstrip("*")
        for m in re.finditer(re.escape(stem), n["summary"], re.I):
            a, b = max(0, m.start()-35), m.end()+35
            print(f"      ~{w}~ ...{n['summary'][a:b]}...")
            break
    print()
