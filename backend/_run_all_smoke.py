# -*- coding: utf-8 -*-
"""全量跑 smoke_*.py，逐个硬超时，结果写 _smoke_results.txt。一次性脚本。"""
import glob
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
SKIP = set()          # 想跳过某个套件就写进来，例如 {"smoke_captcha_auto.py"}
TIMEOUT = int(os.environ.get("SMOKE_TIMEOUT", "240"))

env = dict(os.environ)
for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    env.pop(k, None)

out_path = os.path.join(HERE, "_smoke_results.txt")
files = sorted(os.path.basename(p) for p in glob.glob(os.path.join(HERE, "smoke_*.py")))
rows = []
with open(out_path, "w", encoding="utf-8") as fh:
    fh.write("")
for f in files:
    if f in SKIP:
        continue
    t0 = time.time()
    try:
        cp = subprocess.run([PY, "-u", f], cwd=HERE, env=env, timeout=TIMEOUT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        rc, out = cp.returncode, cp.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        rc = 999
        out = (e.stdout or b"").decode("utf-8", "replace") + "\n[超时 %ss]" % TIMEOUT
    dt = time.time() - t0
    lines = [l for l in out.splitlines() if l.strip()]
    summ = ""
    for l in reversed(lines):
        if "=====" in l or "PASS" in l.upper() or "FAIL" in l.upper():
            summ = l.strip()
            break
    if not summ and lines:
        summ = lines[-1].strip()[:120]
    rows.append((f, rc, dt, summ))
    with open(out_path, "a", encoding="utf-8") as fh:
        fh.write("%-30s rc=%-4s %5.1fs  %s\n" % (f, rc, dt, summ))
    print("%-30s rc=%-4s %5.1fs  %s" % (f, rc, dt, summ), flush=True)

bad = [r for r in rows if r[1] != 0]
print("\n总计 %d 套，失败/超时 %d 套: %s" % (len(rows), len(bad), [r[0] for r in bad]))
with open(out_path, "a", encoding="utf-8") as fh:
    fh.write("\n总计 %d 套，失败/超时 %d 套: %s\n" % (len(rows), len(bad), [r[0] for r in bad]))
