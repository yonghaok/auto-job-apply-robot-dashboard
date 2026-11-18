# -*- coding: utf-8 -*-
"""T71 复现（第二批）：education / projects 两个区块的真实填写结果。只填不提交。"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="t71s_")
os.environ["RESUME_PROFILES"] = os.path.join(_TMP, "profiles")
os.environ["RESUME_STATE"] = os.path.join(_TMP, "state.json")
os.environ["RESUME_CDP_ENDPOINT"] = "http://127.0.0.1:9222"

import filler  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

URL = ("https://app.mokahr.com/campus-recruitment/deeproute/145894"
       "#/job/a11c6084-a843-4327-9dbe-cb6febab1d99/apply")
OUT = os.path.join(HERE, "_t71_dump")
PROFILE = json.load(open(os.path.join(HERE, "profile.json"), encoding="utf-8"))

VAL_JS = """() => {
  const out = [];
  document.querySelectorAll('input,textarea,select').forEach((e, i) => {
    const type = (e.getAttribute('type') || '').toLowerCase();
    if (['file','radio','checkbox','submit','button','hidden'].indexOf(type) >= 0) return;
    const v = (e.value || '').replace(/\\s+/g, ' ').trim();
    if (!v) return;
    out.push(i + '|' + e.tagName.toLowerCase() + '|' +
             (e.getAttribute('placeholder') || '').slice(0, 16) + '|' + v.slice(0, 50));
  });
  return out;
}"""

with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = b.contexts[0]
    page = None
    for pg in ctx.pages:
        if "mokahr" in (pg.url or ""):
            page = pg
            break
    if page is None:
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    try:
        page.reload(wait_until="domcontentloaded", timeout=60000)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(4000)
    for _ in range(6):
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(200)
    page.wait_for_timeout(600)

    for path in ("education", "projects"):
        print("== 跑 _fill_repeat_section(%s) ==" % path)
        r = filler._fill_repeat_section(page, PROFILE, path)
        if r:
            print("  report:", r["report"])
            print("  filled:", json.dumps(r["filled"], ensure_ascii=False))
            print("  failed:", json.dumps(r["failed"], ensure_ascii=False))
        else:
            print("  返回 None（不适用）")
        print("  页面值:")
        for v in page.evaluate(VAL_JS):
            print("   ", v)
        print()
        page.screenshot(path=os.path.join(OUT, "06_after_%s.png" % path), full_page=True)
    b.close()
