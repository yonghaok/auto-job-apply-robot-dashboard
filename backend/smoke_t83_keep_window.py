# -*- coding: utf-8 -*-
"""T83 自测：填表过程中窗口不许被自己关掉。

背景（用户 2026-10-28 反馈）：「填表过程中就关窗口」。
两条独立成因，各锁一条：
  A. `_apply_core` 原先没有 except —— 任何 Playwright/JS 异常都直落 `finally` 关窗，
     看板上只剩一句笼统失败。现在必须记录异常、把窗口交给人、不关。
  B. 关自己的标签页前若浏览器里只剩这一页，`page.close()` 会把**整个窗口**关掉
     （CDP 接管与私有持久会话都一样）。必须先 `_ensure_spare_tab()`。

全部本地跑，不访问真实站点。
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import filler  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

PASS = []
FAIL = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if ok else "FAIL", name,
                         (" | " + str(detail)) if detail else ""))


def src(name):
    return io.open(os.path.join(HERE, name), encoding="utf-8", newline="").read()


def main():
    print("== A. 源码护栏：异常不许直落关窗 ==")
    f = src("filler.py")
    check("存在 except 分支标记 _keep_window = True",
          "_keep_window = True" in f)
    check("异常会写进备注（含异常类型）",
          "填写过程出现异常：%s: %s" in f)
    check("异常返回 stage=填写异常", '"stage": "填写异常"' in f)
    check("异常后驻留等待（窗口保持打开）",
          re.search(r'except Exception as _exc:.*?_park_before_submit\(page, note=note, confirm_wait=0',
                    f, re.S) is not None)
    check("finally 里按 _keep_window 决定是否关闭",
          re.search(r"if not _keep_window:\s*\n\s*if _via == \"cdp\":", f) is not None)
    check("关闭前先补一个常驻标签页",
          "_ensure_spare_tab(ctx, page)" in f)
    check("反向：不存在「无条件 _closer()」的旧写法",
          re.search(r"_sweep_pages\(\)\s*\n\s*if _via == \"cdp\":\s*\n\s*try:\s*\n\s*page\.close\(\)",
                    f) is None)

    print("== B. server：一键投递默认停在提交前 ==")
    s = src("server.py")
    check("start_apply_all 默认 pause_before_submit=True",
          re.search(r"def start_apply_all\([^)]*pause_before_submit=True", s, re.S) is not None)
    check("/api/apply-all 读取默认 True",
          'data.get("pause_before_submit", True)' in s)
    check("反向：apply-all 不再默认 False",
          re.search(r"def start_apply_all\([^)]*pause_before_submit=False", s, re.S) is None)

    print("== C. _ensure_spare_tab 真行为 ==")
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        try:
            ctx = b.new_context()
            page = ctx.new_page()
            page.goto("data:text/html,<h1>only</h1>")
            check("初始只有 1 页", len(ctx.pages) == 1, len(ctx.pages))
            filler._ensure_spare_tab(ctx, page)
            n1 = len(ctx.pages)
            check("补页后 = 2（窗口不会因为关掉唯一页而消失）", n1 == 2, n1)
            check("原有页仍活着（没被误关）", not page.is_closed())
            filler._ensure_spare_tab(ctx, page)
            check("已有别的页时不再多开（第二次仍 2 页）", len(ctx.pages) == 2,
                  len(ctx.pages))
            page.close()
            left = [p for p in ctx.pages if not p.is_closed()]
            check("关掉自己的页后还剩 1 页", len(left) == 1, len(left))
            filler._ensure_spare_tab(ctx, left[0])
            check("剩下的是唯一页 → 又补一页", len([p for p in ctx.pages
                                                  if not p.is_closed()]) == 2)
        finally:
            b.close()

    print("== D. 默认预算仍是不超时（别把 T77 改回去） ==")
    check("PAUSE_CONFIRM_WAIT == 0（不超时）", filler.PAUSE_CONFIRM_WAIT == 0,
          filler.PAUSE_CONFIRM_WAIT)
    check("HUMAN_HANDOFF_WAIT == 0（不超时）", filler.HUMAN_HANDOFF_WAIT == 0,
          filler.HUMAN_HANDOFF_WAIT)
    check("_pause_budget(0) == 0", filler._pause_budget(0) == 0)
    check("_pause_budget(None) == 0", filler._pause_budget(None) == 0)

    print("\n===== 汇总：PASS=%d FAIL=%d =====" % (len(PASS), len(FAIL)))
    if FAIL:
        for x in FAIL:
            print("  FAILED:", x)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
