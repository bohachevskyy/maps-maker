"""Print Codex 5h/weekly usage from the newest session log (spends no quota).

Exit 1 when the 5-hour window is at or above the cap (default 75%), 2 if no
record is found. Non-ephemeral `codex exec` runs write the logs this reads.
"""
import glob, json, os, re, sys, time

cap = float(sys.argv[1]) if len(sys.argv) > 1 else 75.0
files = sorted(glob.glob(os.path.expanduser("~/.codex/sessions/**/*.jsonl"), recursive=True),
               key=os.path.getmtime)
for f in reversed(files[-20:]):
    hits = re.findall(r'"rate_limits":(\{.*?"plan_type":"[^"]*"[^}]*\})', open(f, errors="ignore").read())
    if hits:
        r = json.loads(hits[-1]); p, s = r["primary"], r["secondary"]
        age = (time.time() - os.path.getmtime(f)) / 60
        print(f"5h {p['used_percent']}% (resets {time.strftime('%H:%M', time.localtime(p['resets_at']))}) | "
              f"week {s['used_percent']}% | measured {age:.0f} min ago")
        sys.exit(1 if p["used_percent"] >= cap else 0)
print("no rate-limit record found"); sys.exit(2)
