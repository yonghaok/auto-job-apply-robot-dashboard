# -*- coding: utf-8 -*-
"""smoke: 一键投递「单窗口」（2026-10-28 用户要求：单窗口弹出，不再并发）。

背景（实测）：backend 自 2026-09-23 起 `start_apply_all` 默认 concurrency=1，
但 **frontend 固定传 3** → 看板一次弹出 3 个标签页；后端还用
`max(1, min(concurrency, 6))` 允许到 6。用户要求「单窗口弹出，不再并发」。

本套件钉死三件事：
  A 后端**强制 1**：客户端传 3/6/0/-1/"abc" 都只跑一个（返回值也是 1）
  B 前端请求体传 1，且不再出现「并发 3」类文案
  C 行为验证：同时运行中的岗位峰值 = 1

**可证伪**：先用 `_apply_all_worker(items, 3)` 直接跑 3 并发 → 断言峰值 >= 2。
放开这条就说明夹具测不出并发，后面的 peak==1 也就没有意义。

不碰真实浏览器、不发网络请求：`_apply_one_item` 与 `columns_payload` 都被替换。
用法（cwd=backend）：<venv-python> smoke_single_window.py
"""
import io
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("RESUME_CDP_ENDPOINT", "http://127.0.0.1:59999")

import server  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("  %s %s%s" % ("PASS" if ok else "FAIL", name,
                         ("  [" + str(detail) + "]") if detail else ""))


# ---------------- 夹具：统计同时在跑的岗位数 ----------------
class Peak(object):
    def __init__(self):
        self.now = 0
        self.peak = 0
        self.lock = threading.Lock()

    def enter(self):
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
            return self.now

    def leave(self):
        with self.lock:
            self.now -= 1


def make_items(n=6):
    return [{"pid": "p%d" % i, "company": "C%d" % i, "title": "T%d" % i,
             "url": "http://127.0.0.1/%d" % i, "pos_key": "", "pause_before_submit": False,
             "confirm_wait": None} for i in range(n)]


def fake_applier(peak, hold=0.18):
    def _f(it):
        peak.enter()
        try:
            time.sleep(hold)
        finally:
            peak.leave()
        return "ok"
    return _f


def reset_job():
    with server.LOCK:
        server.APPLY_JOB.update(running=False, total=0, done=0, ok=0, fail=0,
                                human=0, skipped=0, cancelled=False, concurrency=0,
                                wedged=0)
    server.CANCEL_EVENT.clear()


def wait_idle(limit=15.0):
    t0 = time.time()
    while time.time() - t0 < limit:
        with server.LOCK:
            if not server.APPLY_JOB.get("running"):
                return True
        time.sleep(0.05)
    return False


print("=" * 66)
print("A. 静态：后端强制 conc=1、前端不再请求并发")
src = io.open(os.path.join(HERE, "server.py"), encoding="utf-8").read()
fe = io.open(os.path.join(os.path.dirname(HERE), "frontend", "index.html"),
             encoding="utf-8").read()

check("server.py 不再有 min(int(concurrency or 1), 6) 的上限写法",
      "min(int(concurrency or 1), 6)" not in src)
check("server.py 出现强制 conc = 1", "    conc = 1" in src)
check("server.py 记录了被忽略的并发度", "_asked_conc" in src)
check("前端请求体传 concurrency: 1", "concurrency: 1" in fe)
check("前端不再出现 concurrency: 3", "concurrency: 3" not in fe)
check("前端不再出现「并发上限 3」", "并发上限 3" not in fe)
check("前端无「默认 3 并发」残留", "默认 3 并发" not in src)

print()
print("B. 可证伪：夹具能测出 3 并发（旧行为）")
reset_job()
old_peak = Peak()
server._apply_one_item = fake_applier(old_peak, 0.15)
server._apply_all_worker(make_items(6), 3)
check("直接以 conc=3 跑 worker → 峰值 >= 2（证明夹具能测出并发）",
      old_peak.peak >= 2, "peak=%d" % old_peak.peak)

print()
print("C. 目标行为：start_apply_all 传 3 也只跑 1 个")
for asked in (3, 6, 0, -1, "abc"):
    reset_job()
    peak = Peak()
    server._apply_one_item = fake_applier(peak)
    server.columns_payload = lambda mode, _n=4: {
        "columns": [{"company": "C%d" % i,
                     "positions": [{"id": "p%d" % i, "title": "T%d" % i, "ready": True,
                                    "url": "http://127.0.0.1/%d" % i, "pos_key": ""}]}
                    for i in range(4)]}
    r = server.start_apply_all([], "strict", asked)
    ok_start = bool(r.get("ok"))
    done = wait_idle()
    check("concurrency=%r → 返回 concurrency==1" % (asked,),
          ok_start and r.get("concurrency") == 1, str(r.get("concurrency")))
    check("concurrency=%r → 同时运行峰值 == 1" % (asked,),
          done and peak.peak == 1, "peak=%d done=%s" % (peak.peak, done))
    check("concurrency=%r → 全部岗位都跑了" % (asked,),
          server.APPLY_JOB.get("done") == 4 and server.APPLY_JOB.get("ok") == 4,
          "done=%s ok=%s" % (server.APPLY_JOB.get("done"), server.APPLY_JOB.get("ok")))

print()
print("D. 取消语义未被改坏（串行下取消 = 不再开新岗位）")
reset_job()
peak = Peak()
server._apply_one_item = fake_applier(peak, 0.05)
server.columns_payload = lambda mode: {
    "columns": [{"company": "C", "positions": [
        {"id": "q%d" % i, "title": "T", "ready": True,
         "url": "http://127.0.0.1/q%d" % i, "pos_key": ""} for i in range(6)]}]}
server.start_apply_all([], "strict", 1)
server.CANCEL_EVENT.set()
wait_idle()
check("取消后 done 不会超过 total", server.APPLY_JOB.get("done") <= 6,
      "done=%s" % server.APPLY_JOB.get("done"))

passed = sum(1 for _, ok, _ in results if ok)
failed = len(results) - passed
print()
print("RESULT: %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
