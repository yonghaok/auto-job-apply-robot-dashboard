# -*- coding: utf-8 -*-
"""T75 端到端核验：真实 fill_page 后，用**代码同款** _date_box_read 逐块读「站点已采纳」的
4 段年月，与 profile 期望值逐条对比。只填不提交。
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="awv75_")
os.environ["RESUME_PROFILES"] = os.path.join(_TMP, "profiles")
os.environ["RESUME_STATE"] = os.path.join(_TMP, "state.json")
os.environ["RESUME_CDP_ENDPOINT"] = "http://127.0.0.1:9222"

import filler  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

URL = ("https://campus.allwinnertech.com/campus-recruitment/allwinnertech/43436"
       "/#/job/1c41bb77-5277-43fb-ac36-382db590a0c1/apply?applyFormId=204602")
PROFILE = json.load(open(os.path.join(HERE, "profile.json"), encoding="utf-8"))


def rows(page, path):
    """每块 → (块号, 日期行 idx, 4 段「已采纳」值, 该行 innerText)"""
    res, fields = filler._analyze_repeat(page, path)
    if not res:
        return []
    byi = {f["idx"]: f for f in fields}
    out = []
    for k, blk in enumerate(res.get("blocks") or []):
        di = None
        for i in blk:
            if u"\u65f6\u95f4" in ((byi.get(i) or {}).get("label") or ""):
                di = i
                break
        if di is None:
            out.append((k + 1, None, None, "(块内无日期框)"))
            continue
        info = page.evaluate(filler.DATE_RANGE_JS, str(di))
        vals = [filler._date_box_read(page, di, j) for j in range(4)]
        out.append((k + 1, di, vals, (info or {}).get("rowText") or ""))
    return out


with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = b.contexts[0]
    page = None
    for pg in ctx.pages:
        if "allwinnertech" in (pg.url or ""):
            page = pg
            break
    if page is None:
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=90000)
    print("PAGE:", page.url)
    try:
        page.reload(wait_until="domcontentloaded", timeout=90000)
    except Exception as e:  # noqa: BLE001
        print("reload warn:", type(e).__name__)
    page.wait_for_timeout(4000)
    for _ in range(14):
        page.mouse.wheel(0, 1400)
        page.wait_for_timeout(250)
    page.wait_for_timeout(800)

    print("\nBEFORE:")
    for pth in ("education", "experiences"):
        for r in rows(page, pth):
            print("  %-11s %s" % (pth, r))

    notes = []
    print("\n=== fill_page ===")
    try:
        filler.fill_page(page, PROFILE, note=lambda m: notes.append(str(m)))
        print("  (fill_page 完成)")
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
    page.wait_for_timeout(1500)

    print("\nAFTER（逐块「已采纳」值）:")
    ok = 0
    bad = []
    exp_edu = (PROFILE.get("education") or {}).get("period")
    print("  education 期望 %r" % exp_edu)
    for r in rows(page, "education"):
        print("    块%d idx=%s => %s  行=%r" % r)
    items = PROFILE.get("experiences") or []
    for r in rows(page, "experiences"):
        k = r[0]
        want = items[k - 1].get("period") if k - 1 <= len(items) - 1 else None
        print("    块%d idx=%s => %s  期望 %r  行=%r" % (r[0], r[1], r[2], want, r[3]))

    print("\n=== 逐段判定（解析成 (年,月) 逐段比，容忍 07 vs 7）===")
    checks = [("education", 1, exp_edu)]
    for i, it in enumerate(items, start=1):
        checks.append(("experiences", i, it.get("period")))

    def as_int(x):
        d = "".join(c for c in str(x or "") if c.isdigit())
        return int(d) if d else 0

    for pth, k, want in checks:
        rr = [r for r in rows(page, pth) if r[0] == k]
        if not rr:
            bad.append("%s#%d 块不存在" % (pth, k))
            continue
        got = [(x or "") for x in rr[0][2]]
        p = filler.parse_period(want) if want else None
        if not p:
            print("  SKIP %s#%d 期望值解析不出 %r" % (pth, k, want))
            continue
        (ey1, em1), end, ongoing = p
        good = (as_int(got[0]) == (ey1 or 0) and as_int(got[1]) == (em1 or 0))
        if not ongoing and end:
            good = good and as_int(got[2]) == (end[0] or 0) and as_int(got[3]) == (end[1] or 0)
        if good:
            ok += 1
            print("  PASS %s#%d %r（期望 %r）" % (pth, k, got, want))
        else:
            bad.append("%s#%d got=%r want=%r" % (pth, k, got, want))
            print("  FAIL %s#%d got=%r want=%r" % (pth, k, got, want))
    print("\n总计 PASS=%d FAIL=%d  %s" % (ok, len(bad), bad))

    print("\n----- 日期相关备注 -----")
    for m in notes:
        if u"年月" in m or u"月份" in m:
            print("  *", m)
    b.close()
print("done")
