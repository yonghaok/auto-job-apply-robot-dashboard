# -*- coding: utf-8 -*-
"""smoke_t74_ym.py —— 锁定「年月框识别不能只看 placeholder」（T74）修复。

背景（用户反馈：全志页「起止时间」填错）：
MokaHR 的日期框在**填了值之后会把 placeholder（年/月）抹掉**。三处 JS 过去都只按
placeholder 判「这是不是年月框」，于是：
  · `DATE_RANGE_JS` 认不出已填过的日期行 → `_fill_date_range` 退化成**单值填写**
    → 整串日期写进某一个框（实测「就读时间」变成 2027.6 - 2027.6，起始年丢失）；
  · `COLLECT_JS`/`ANALYZE_JS` 丢掉「起止时间」合成 token → 该框掉出区块 → 配对配不上
    （实测：全志 experiences 的 blocks 从 [[30-33,35-38],[…]] 退化成 [[35-38],[44-47],[48-51,53-56]]，
     日期行被算进隔壁条目）。

修法：三处统一用**结构性补证** `_ymPos(e)` —— placeholder 有效就直接采信；否则落在
`[class*=month-range]` / `[class*=date_info]` 行内时，按该行文本框顺序第 1/3 个=年、第 2/4 个=月。

覆盖：
  A 夹具证伪：用「只看 placeholder」的旧口径，无 placeholder 的行**认不出**年月框（证明非恒真）
  B 新口径：无 placeholder 的行也认得出 4 个年月框；DATE_RANGE_JS 返回 n=4
  C 合成标签：无 placeholder 的年月框仍带「起止时间」token（不掉出区块）
  D 主流程护栏：带 placeholder 的行行为不变（不误伤）
  E 源码结构：三处 JS 都改用了 _ymPos
"""
import io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="t74_")
os.environ["RESUME_PROFILES"] = os.path.join(_TMP, "profiles")
os.environ["RESUME_STATE"] = os.path.join(_TMP, "state.json")
os.environ["RESUME_CDP_ENDPOINT"] = "http://127.0.0.1:59999"

import filler  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

N = [0]
BAD = []


def ck(name, cond, extra=""):
    N[0] += 1
    if cond:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s   %s" % (name, extra))
        BAD.append(name)


YM = u"\u5e74"   # 年
MO = u"\u6708"   # 月

# 行 1：**已填过** → placeholder 被站点抹掉；行 2：未填 → placeholder 还在
FIXTURE = u"""<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<div class="apply-block">
  <div class="apply-field date_info">
    <div class="ttl">\u5c31\u8bfb\u65f6\u95f4</div>
    <div class="ctrl">
      <div class="month-range-select date_info wrapper-Wilqy7sjl0">
        <span><input id="y1" type="text"></span><span><input id="m1" type="text"></span>
        <span><input id="y2" type="text"></span><span><input id="m2" type="text"></span>
      </div>
    </div>
  </div>
  <div class="apply-field date_info">
    <div class="ttl">\u8d77\u6b62\u65f6\u95f4</div>
    <div class="ctrl">
      <div class="month-range-select date_info wrapper-Wilqy7sjl0">
        <span><input id="z1" type="text" placeholder="\u5e74"></span>
        <span><input id="z2" type="text" placeholder="\u6708"></span>
        <span><input id="z3" type="text" placeholder="\u5e74"></span>
        <span><input id="z4" type="text" placeholder="\u6708"></span>
      </div>
    </div>
  </div>
  <div class="apply-field string_info">
    <div class="ttl">\u516c\u53f8\u540d\u79f0</div>
    <div class="ctrl"><input id="co" type="text" placeholder="\u516c\u53f8\u540d\u79f0"></div>
  </div>
</div>
</body></html>
"""

# 「只看 placeholder」的旧口径：在同一个行里数年月框
OLD_YM_JS = r"""(id) => {
  const e = document.getElementById(id);
  if (!e) return null;
  const rng = e.closest('[class*=month-range],[class*=date_info]');
  if (!rng) return null;
  return Array.from(rng.querySelectorAll('input')).filter(x =>
    /^(年|月|year|month|yyyy|mm)$/i.test((x.getAttribute('placeholder') || '').trim())
  ).length;
}"""

FF = os.path.join(_TMP, "fixture.html")
io.open(FF, "w", encoding="utf-8").write(FIXTURE)

# ---------------------------------------------------------------- E 源码结构
print("== E) 三处 JS 都改用结构性 _ymPos ==")
fsrc = io.open(os.path.join(HERE, "filler.py"), encoding="utf-8").read()
ck("E1 _ymPos 共 3 处定义（COLLECT_JS / ANALYZE_JS / DATE_RANGE_JS）",
   fsrc.count("const _ymPos = (node) => {") == 2 and fsrc.count("const _ymPos = (e) => {") == 1,
   "node=%d e=%d" % (fsrc.count("const _ymPos = (node) => {"),
                     fsrc.count("const _ymPos = (e) => {")))
ck("E2 DATE_RANGE_JS 的 isYM 走 _ymPos",
   "const isYM = e => !!_ymPos(e);" in fsrc)
ck("E3 COLLECT_JS 合成标签走 _ymPos",
   "if (_ymPos(e) && !/时间|起止|期间|日期/.test(label || ''))" in fsrc)
ck("E4 ANALYZE_JS hayOf 的日期 token 走 _ymPos",
   "if (_ymPos(el)) s += ' 起止时间 period time date';" in fsrc)
ck("E5 三处都带结构性证据（month-range / date_info）",
   fsrc.count("[class*=month-range],[class*=date_info]") == 3,
   "count=%d" % fsrc.count("[class*=month-range],[class*=date_info]"))
ck("E6 反向：已无「只看 placeholder 就判年月框」的 isYM 老写法",
   "/^(年|月|year|month|yyyy|mm)$/i.test((e.getAttribute('placeholder') || '').trim());"
   not in fsrc)

# ---------------------------------------------------------------- 真浏览器
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox"])
    page = b.new_page()
    page.goto("file:///" + FF.replace("\\", "/"), wait_until="domcontentloaded")
    page.wait_for_timeout(200)

    print("\n== A) 夹具证伪：旧口径认不出「已填过」的行 ==")
    for dom in ("y1", "m1", "y2", "m2"):
        old = page.evaluate(OLD_YM_JS, dom)
        ck("A/%s：只看 placeholder 时该行 0 个年月框（证明夹具非恒真）" % dom,
           old == 0, "old=%r" % (old,))
    old_ok = page.evaluate(OLD_YM_JS, "z1")
    ck("A/z1：带 placeholder 的行旧口径仍能数出 4 个（对比项）", old_ok == 4, "old=%r" % (old_ok,))

    fields = filler._collect_fields(page)
    byi = {f.get("id"): f for f in fields}
    idx_of = lambda dom: (byi.get(dom) or {}).get("idx")  # noqa: E731

    print("\n== B) 新口径：无 placeholder 的行也认得出 4 个年月框 ==")
    got = page.evaluate(filler.DATE_RANGE_JS, str(idx_of("y1")))
    ck("B1 DATE_RANGE_JS 对「已填过」的行返回 n=4", (got or {}).get("n") == 4, got)
    got2 = page.evaluate(filler.DATE_RANGE_JS, str(idx_of("z1")))
    ck("B2 DATE_RANGE_JS 对「未填」的行仍返回 n=4（不误伤）", (got2 or {}).get("n") == 4, got2)

    print("\n== C) 合成标签：年月框 label 不为空（不掉出区块）==")
    # 关键回归是「label 变空 → 掉出区块」。正常情况 label 要么是行里的真标题
    # （「就读时间」），要么是合成 token（「起止时间」）—— 两者都算对，都含「时间」。
    for dom in ("y1", "m1", "y2", "m2"):
        lb = (byi.get(dom) or {}).get("label") or ""
        ck("C/%s 的 label 非空且指向时间行（%r）" % (dom, lb),
           bool(lb.strip()) and u"\u65f6\u95f4" in lb, "label=%r" % (lb,))

    print("\n== D) 护栏：普通字段不受影响 ==")
    ck("D1 「公司名称」的 label 仍是「公司名称」",
       (byi.get("co") or {}).get("label") == u"\u516c\u53f8\u540d\u79f0",
       "label=%r" % ((byi.get("co") or {}).get("label"),))
    ck("D2 年月框不会被误判成「年」「月」标题（label 不是纯 年/月）",
       all((byi.get(d) or {}).get("label") not in (YM, MO) for d in ("y1", "m1")),
       [(d, (byi.get(d) or {}).get("label")) for d in ("y1", "m1")])
    b.close()

print("\n通过 %d 项，失败 %d 项" % (N[0] - len(BAD), len(BAD)))
if BAD:
    print("失败项：" + "；".join(BAD))
    sys.exit(1)
print("全部 PASS：T74「年月框识别不依赖 placeholder」已锁定")
