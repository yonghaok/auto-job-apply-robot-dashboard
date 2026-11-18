# -*- coding: utf-8 -*-
"""「取消本栏 / 取消岗位 / 恢复已取消」的**页面级**回归（T76）。

背景（2026-10-28 用户报障）：
  · 点「取消本栏」后浏览器弹窗的「确定」点不动，取消动作悬空；
  · 用户手工取消的记录被自测收尾的 unhide_all 一把清空。

本套件**自建隔离实例**（临时 state.json + 空闲端口），不碰 8000 的真实看板：
  1. 点「取消本栏」弹的是**页内** #askModal，不是浏览器原生 dialog；
  2. 点「确定移除」后该栏**立即**从 DOM 消失（不等下一次轮询），并落库；
  3. 服务端返回 ok:false 时**不关窗**、就地报错、按钮恢复可点（可重试）；
  4. 「恢复已取消」先确认并报准数，恢复后可用提示条「撤销」把名单原样拿回来。

用法：python smoke_cancel_ui.py        （需要本机有 Edge/Chromium 供 Playwright 用）
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS %s %s" % (name, extra))
    else:
        FAIL += 1
        print("  FAIL %s %s" % (name, extra))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def get(base, path):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(base + path, timeout=10) as r:
        return json.loads(r.read().decode())


def wait_up(base, timeout=25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            get(base, "/api/stats")
            return True
        except Exception:
            time.sleep(0.3)
    return False


def main():
    tmp = tempfile.mkdtemp(prefix="resume-cancel-ui-")
    port = free_port()
    base = "http://127.0.0.1:%d" % port
    env = dict(os.environ)
    env.update({
        "RESUME_PORT": str(port),
        "RESUME_STATE": os.path.join(tmp, "state.json"),
        "RESUME_ACCESS_LOG": os.path.join(tmp, "access.log"),
        # 自测绝不开用户的真浏览器：指向一个没人监听的端口
        "RESUME_CDP_ENDPOINT": "http://127.0.0.1:59999",
    })
    proc = subprocess.Popen([PY, "-u", "server.py"], cwd=HERE, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_up(base):
            print("隔离实例没起来（端口 %d），跳过" % port)
            return 1
        state = os.path.join(tmp, "state.json")

        def hidden():
            d = get(base, "/api/hidden")
            return set(d["companies"]), set(d["positions"])

        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            pg = b.new_page()
            native = []
            pg.on("dialog", lambda d: (native.append(d.type), d.dismiss()))
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(base + "/", wait_until="networkidle")
            pg.wait_for_selector(".job", timeout=20000)
            n0 = pg.eval_on_selector_all(".job", "e => e.length")
            co = pg.eval_on_selector(".job .job-head .co", "e => e.textContent")
            print("隔离看板：%d 栏，第一栏=%s" % (n0, co))

            print("\n=== 1) 页内确认框（不是原生 dialog） ===")
            pg.click(".job .job-head button.no")
            pg.wait_for_selector("#askModal.on", timeout=5000)
            desc = pg.inner_text("#ask-desc")
            check("不弹原生 dialog", not native, str(native))
            check("确认框含公司名", co in desc, repr(desc[:36]))
            check("确认框含岗位数", "个岗位" in desc)
            check("确定键是危险样式", pg.get_attribute("#ask-ok", "class") == "no")

            print("\n=== 2) 确定后立即移除并落库 ===")
            pg.click("#ask-ok")
            pg.wait_for_timeout(1500)
            n1 = pg.eval_on_selector_all(".job", "e => e.length")
            check("栏数 -1", n1 == n0 - 1, "%d -> %d" % (n0, n1))
            check("确认框已关闭", "on" not in (pg.get_attribute("#askModal", "class") or ""))
            check("出现 toast 提示", pg.eval_on_selector_all(".toast", "e => e.length") >= 1)
            hc, hp = hidden()
            check("公司已记入取消名单", co in hc, "%d 家" % len(hc))
            saved = json.load(open(state, encoding="utf-8"))
            check("已落盘（重启不回来）", co in (saved.get("_hidden_co") or []))

            print("\n=== 3) 服务端失败：不关窗、就地报错、可重试 ===")
            pg.route("**/api/hide", lambda route: route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"ok": False, "error": "company required"})))
            pg.click(".job .job-head button.no")
            pg.wait_for_selector("#askModal.on", timeout=5000)
            pg.click("#ask-ok")
            pg.wait_for_timeout(800)
            check("失败时窗口仍开着", "on" in (pg.get_attribute("#askModal", "class") or ""))
            check("就地显示失败原因", "company required" in pg.inner_text("#ask-err"))
            check("确定键恢复可点", pg.is_enabled("#ask-ok"))
            check("没有误删该栏", pg.eval_on_selector_all(".job", "e => e.length") == n1)
            pg.click("#ask-cancel")
            pg.wait_for_timeout(200)
            check("「取消」可关窗", "on" not in (pg.get_attribute("#askModal", "class") or ""))
            pg.unroute("**/api/hide")

            print("\n=== 4) 恢复已取消：确认 + 可撤销 ===")
            co_before, pos_before = hidden()
            pg.click("button.no[onclick='unhideAll()']")
            pg.wait_for_selector("#askModal.on", timeout=5000)
            d = pg.inner_text("#ask-desc")
            check("确认框报准数", ("%d 家公司" % len(co_before)) in d, repr(d[:30]))
            pg.click("#ask-ok")
            pg.wait_for_timeout(1500)
            co_after, pos_after = hidden()
            check("已清空", not co_after and not pos_after,
                  "co=%d pos=%d" % (len(co_after), len(pos_after)))
            btn = pg.query_selector(".toast button")
            check("toast 带「撤销」", btn is not None)
            if btn:
                btn.click()
                pg.wait_for_timeout(1500)
                co2, pos2 = hidden()
                check("撤销后名单原样", (co2, pos2) == (co_before, pos_before),
                      "co %d/%d pos %d/%d" % (len(co2), len(co_before),
                                              len(pos2), len(pos_before)))
            check("无 JS 报错", not errs, str(errs[:2]))
            b.close()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n===== 汇总：PASS=%d FAIL=%d =====" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
