# -*- coding: utf-8 -*-
"""T71 复现：在真实页面上跑一遍 experiences 的区块填写，看「填漏/错位」到底怎么发生。

只填不提交；跑完请手动刷新页面丢弃这些临时值。
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="t71r_")
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
    const tag = e.tagName.toLowerCase();
    const type = (e.getAttribute('type') || '').toLowerCase();
    if (['file', 'radio', 'checkbox', 'submit', 'button', 'hidden'].indexOf(type) >= 0) return;
    const v = (e.value || '').replace(/\\s+/g, ' ').trim();
    if (!v) return;
    out.push({idx: i, tag: tag, ph: (e.getAttribute('placeholder') || '').slice(0, 20),
              v: v.slice(0, 60)});
  });
  return out;
}"""

TITLE_JS = """() => {
  const out = [];
  document.querySelectorAll('input,textarea,select').forEach((e, i) => {
    const tag = e.tagName.toLowerCase();
    const type = (e.getAttribute('type') || '').toLowerCase();
    if (['file','radio','checkbox','submit','button','hidden'].indexOf(type) >= 0) return;
    // 站点把标题写在兄弟 div
    let title = '';
    let p = e.parentElement, hops = 0;
    while (p && hops < 3) {
      let t = '';
      for (const c of p.children) {
        if (c.contains(e)) continue;
        if (c.querySelector && c.querySelector('input,textarea,select')) continue;
        if (c.tagName === 'INPUT' || c.tagName === 'TEXTAREA') continue;
        t += ' ' + (c.innerText || c.textContent || '');
      }
      t = t.replace(/\\s+/g, ' ').trim();
      if (t && t.length <= 20) { title = t; break; }
      p = p.parentElement; hops++;
    }
    out.push({idx: i, tag: tag, ph: (e.getAttribute('placeholder') || '').slice(0, 18),
              title: title.slice(0, 24)});
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
    for _ in range(8):
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(200)
    page.wait_for_timeout(800)

    print("== 站点真实字段标题（兄弟 div 提取） ==")
    for t in page.evaluate(TITLE_JS):
        print("  idx=%-3s %-8s ph=%-18s title=%s" % (t["idx"], t["tag"], repr(t["ph"]), repr(t["title"])))

    print("\n== 填写前 ANALYZE ==")
    for path in ("experiences", "projects", "education"):
        res, _ = filler._analyze_repeat(page, path)
        print("  %-12s %s" % (path, json.dumps(res, ensure_ascii=False) if res else None))

    print("\n== 跑 _fill_repeat_section(experiences) ==")
    r = filler._fill_repeat_section(page, PROFILE, "experiences")
    if r:
        print("  report:", r["report"])
        print("  filled:", json.dumps(r["filled"], ensure_ascii=False))
        print("  failed:", json.dumps(r["failed"], ensure_ascii=False))
        print("  uncertain:", json.dumps(r["uncertain"], ensure_ascii=False))
    else:
        print("  返回 None（不适用）")

    print("\n== 填写后页面上的值 ==")
    vals = page.evaluate(VAL_JS)
    for v in vals:
        print("  idx=%-3s %-8s ph=%-18s = %s" % (v["idx"], v["tag"], repr(v["ph"]), repr(v["v"])))
    json.dump(vals, open(os.path.join(OUT, "05_after_values.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    page.screenshot(path=os.path.join(OUT, "05_after.png"), full_page=True)

    print("\n== 填写后 ANALYZE ==")
    for path in ("experiences", "projects", "education"):
        res, _ = filler._analyze_repeat(page, path)
        print("  %-12s %s" % (path, json.dumps(res, ensure_ascii=False) if res else None))
    b.close()
