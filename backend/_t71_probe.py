# -*- coding: utf-8 -*-
"""T71 探针（只读）：文远知行 / MokaHR 投递页真实结构。
不填任何字段、不提交；只采集字段 + 分析可重复区块 + 落盘 HTML 片段。"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="t71_")
os.environ["RESUME_PROFILES"] = os.path.join(_TMP, "profiles")
os.environ["RESUME_STATE"] = os.path.join(_TMP, "state.json")
os.environ["RESUME_CDP_ENDPOINT"] = "http://127.0.0.1:9222"

import filler  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

URL = ("https://app.mokahr.com/campus-recruitment/deeproute/145894"
       "#/job/a11c6084-a843-4327-9dbe-cb6febab1d99/apply")
OUT = os.path.join(HERE, "_t71_dump")
os.makedirs(OUT, exist_ok=True)

BLOCK_JS = """() => {
  const out = {titles: [], blocks: [], addBtns: []};
  // 区块容器：常见 class 片段
  document.querySelectorAll('[class*=multi]').forEach((el, i) => {
    const ins = Array.from(el.querySelectorAll('input,textarea'));
    out.blocks.push({
      i: i, cls: (el.getAttribute('class') || '').slice(0, 90),
      vis: !!(el.offsetWidth || el.offsetHeight),
      nIn: ins.length,
      txt: ((el.innerText || '').replace(/\\s+/g, ' ')).slice(0, 120),
      insDesc: ins.slice(0, 14).map(e => ({
        tag: e.tagName.toLowerCase(),
        ph: (e.getAttribute('placeholder') || '').slice(0, 24),
        lbl: (() => { try { return (e.labels && e.labels[0] ? e.labels[0].innerText : '') || ''; }
                      catch(_) { return ''; } })().slice(0, 24),
        ro: !!e.readOnly, vis: !!(e.offsetWidth || e.offsetHeight)
      }))
    });
  });
  document.querySelectorAll('button,a,[role=button],span,div,i').forEach(el => {
    const t = ((el.innerText || '') + '').trim();
    if (!t || t.length > 8) return;
    if (!/添加|新增|\\+/.test(t)) return;
    if (el.querySelector && el.querySelector('input,textarea')) return;
    const r = el.getBoundingClientRect();
    if (!r.width) return;
    out.addBtns.push({tag: el.tagName.toLowerCase(), txt: t,
                      cls: (el.getAttribute('class') || '').slice(0, 60),
                      x: Math.round(r.x), y: Math.round(r.y)});
  });
  // 区块标题（教育背景/实习经历/项目经历…）及其所在的 section 容器
  document.querySelectorAll('[class*=apply-fields]').forEach((el, i) => {
    const t = ((el.innerText || '').replace(/\\s+/g, ' ')).slice(0, 60);
    if (!t) return;
    out.titles.push({i: i, cls: (el.getAttribute('class') || '').slice(0, 70), txt: t,
                     nMulti: el.querySelectorAll('[class*=multi]').length});
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
    print("PAGE:", page.url)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception as e:  # noqa: BLE001
        print("networkidle timeout:", type(e).__name__)
    page.wait_for_timeout(2500)
    # 滚到底把懒加载的区块都拉出来
    for _ in range(8):
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(250)
    page.wait_for_timeout(800)

    html = page.content()
    open(os.path.join(OUT, "01_page.html"), "w", encoding="utf-8").write(html)
    page.screenshot(path=os.path.join(OUT, "01_page.png"), full_page=True)

    fields = filler._collect_fields(page)
    json.dump(fields, open(os.path.join(OUT, "02_fields.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("fields:", len(fields))

    blk = page.evaluate(BLOCK_JS)
    json.dump(blk, open(os.path.join(OUT, "03_blocks.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    for path in ("experiences", "projects", "education", "awards"):
        res, _f = filler._analyze_repeat(page, path)
        print("ANALYZE %-12s -> %s" % (path, json.dumps(res, ensure_ascii=False) if res else None))
        json.dump(res, open(os.path.join(OUT, "04_analyze_%s.json" % path), "w",
                            encoding="utf-8"), ensure_ascii=False, indent=1)
    b.close()
print("dump ->", OUT)
