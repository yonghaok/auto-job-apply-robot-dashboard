# -*- coding: utf-8 -*-
"""smoke_t75_date_commit.py —— 锁定 T75「MokaHR 年月框必须回车才算提交 + 月份不认补零」。

背景（用户反馈：全志页「起止时间 / 就读时间」填不对）：
MokaHR 的日期框是 `sd-Select` 自定义控件，实测三件事：
  ① **只 `page.fill` 不算提交**：值停在 DOM 里、不进站点 model，一失焦就被抹回空。
     而 `input.value` 当场回读**是有值的** → 旧实现「回读通过」其实什么都没留下；
  ② **回车**才把值提交进 model（失焦 / 整页重渲染都保留）；
  ③ 月份框**只认不补零**的 `9`：写 `09` 会被拒 → 回落到站点默认的 1。

夹具复刻了这三条行为（含「重渲染按钮」把整行从 model 重建），因此能**证伪**旧实现。

覆盖：
  A 证伪（非恒真）：只 fill 不回车 → model 里没有值；且「读 input.value」会谎报已填
  B 新实现：_fill_date_range 成功提交 4 段，逐段读回正确，**重渲染后仍在**
  C 借位证伪：直写 '06' 被站点拒绝（夹具真的会拒），新实现自动改写成 '6'
  D 诚实性：sd-Select 框的读数**不得**回落 input.value（否则校验恒真）
  E 安全：行在 <form> 里 → enterSafe=0，且**不回车、如实报失败**（宁可失败也不触发隐式提交）
  F 源码结构：回车提交 / 月份补零兜底 / 每次写前重新打标 / sd-Select 只看显示层
"""
import io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="t75_")
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


def box(dom, pre):
    return (u'<span class="item-ABC"><label>'
            u'<div class="sd-Input-container sd-Select-container">'
            u'<input id="%s" type="text">'
            u'<span class="sd-Input-display-value"></span>'
            u'</div></label></span>' % (pre + dom))


ROW = (u'<div class="month-range-select date_info wrapper-W">'
       + "".join(box(d, "a_") for d in ("y1", "m1", "y2", "m2"))
       + u'</div>')
ROW_IN_FORM = (u'<form id="f2"><div class="month-range-select date_info wrapper-W">'
               + "".join(box(d, "b_") for d in ("y1", "m1", "y2", "m2"))
               + u'</div></form>')

FIXTURE = u"""<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<div class="apply-block">
  <div class="apply-field date_info">
    <div class="ttl">\u5c31\u8bfb\u65f6\u95f4</div>
    <div class="ctrl">%s</div>
  </div>
  <div class="apply-field date_info">
    <div class="ttl">\u8868\u5355\u5185\u65f6\u95f4</div>
    <div class="ctrl">%s</div>
  </div>
</div>
<button id="rerender" type="button">rerender</button>
<script>
(function(){
  var PREFIX = ['a_', 'b_'], BOX = ['y1','m1','y2','m2'];
  var MODEL = {};
  function all(){ var out = []; PREFIX.forEach(function(p){
      BOX.forEach(function(b){ out.push(p + b); }); }); return out; }
  function host(id){ var e = document.getElementById(id);
      return e ? e.closest('.sd-Input-container') : null; }
  function render(){
    all().forEach(function(id){
      var h = host(id);
      if (!h) return;
      var d = h.querySelector('.sd-Input-display-value');
      if (d) d.textContent = MODEL[id] || '';
    });
  }
  function commit(id){
    var el = document.getElementById(id);
    if (!el) return;
    var v = (el.value || '').trim();
    if (!v) return;
    if (/m\\d$/.test(id) && /^0\\d$/.test(v)) return;   // 站点拒绝补零月份
    MODEL[id] = v;
    el.value = '';                                   // 提交后 input.value 被清空
    render();
  }
  all().forEach(function(id){
    var el = document.getElementById(id);
    if (!el) return;
    el.addEventListener('keydown', function(ev){
      if (ev.key === 'Enter') { ev.preventDefault(); commit(id); } });
    el.addEventListener('blur', function(){ el.value = ''; });   // 未提交=被抹掉
  });
  document.getElementById('rerender').addEventListener('click', function(){
    all().forEach(function(id){ var e = document.getElementById(id); if (e) e.value = ''; });
    render();                                        // 整行从 model 重建
  });
  window.__model = MODEL;
})();
</script>
</body></html>
""" % (ROW, ROW_IN_FORM)

FF = os.path.join(_TMP, "fixture.html")
io.open(FF, "w", encoding="utf-8").write(FIXTURE)

# ---------------------------------------------------------------- F 源码结构
print("== F) 源码结构 ==")
fsrc = io.open(os.path.join(HERE, "filler.py"), encoding="utf-8").read()
ck("F1 写值后按回车提交", 'page.press(sel, "Enter", timeout=2000)' in fsrc)
ck("F2 月份框按 k%2==1 判定（奇数列=月）",
   "is_month=(k % 2 == 1)" in fsrc)
ck("F3 补零被拒 → 退到不补零",
   'cands.append(str(int(_s)))' in fsrc)
ck("F4 每次写值前重新打标（映射漂移防护）",
   '_i2 = page.evaluate(DATE_RANGE_JS, str(f.get("idx")))' in fsrc)
ck("F5 sd-Select 类控件只看显示层（有 is_sel 分支）",
   "if is_sel:" in fsrc and "return disp" in fsrc)
ck("F6 反向：_date_box_read 不再无条件回落 input.value",
   fsrc.count("return page.input_value('[data-daterange=") == 1)
ck("F7 DATE_RANGE_JS 回报 enterSafe",
   "enterSafe: enterSafe" in fsrc and "ym[0].form" in fsrc)
ck("F8 已修正「page.fill 直接生效」的旧注释",
   "page.fill` 直接生效 —— 不需要点面板" not in fsrc)

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox"])

    # ------------------------------------------------------------ A/D 证伪
    page = b.new_page()
    page.goto("file:///" + FF.replace("\\", "/"), wait_until="domcontentloaded")
    page.wait_for_timeout(200)
    fields = filler._collect_fields(page)
    byi = {f.get("id"): f for f in fields}
    fa = byi.get("a_y1")
    ck("A0 夹具里的年月框被采集到", fa is not None, list(byi.keys())[:8])
    idx = fa["idx"]
    page.evaluate(filler.DATE_RANGE_JS, str(idx))
    s0 = '[data-daterange="%s:0"]' % idx

    print("\n== A) 证伪：只 fill 不回车 = 没提交 ==")
    page.fill(s0, "2023")
    ck("A1 当场读 input.value **有值**（这正是旧实现会谎报的原因）",
       page.input_value(s0) == "2023", page.input_value(s0))
    ck("A2 但站点 model 里**没有**这个值（未提交）",
       not page.evaluate("() => window.__model.a_y1"),
       page.evaluate("() => window.__model.a_y1"))
    ck("A3 诚实读法 _date_box_read 返回空（不回落 input.value）",
       filler._date_box_read(page, idx, 0) == "",
       repr(filler._date_box_read(page, idx, 0)))
    page.click("#rerender")
    page.wait_for_timeout(150)
    ck("A4 重渲染后 input.value 也没了 → 旧实现彻底丢值",
       page.input_value(s0) == "", repr(page.input_value(s0)))

    print("\n== D) 诚实性：sd-Select 框不得把「未提交的输入」当已填 ==")
    page.fill(s0, "2025")
    ck("D1 未提交时 input_value=2025 而 _date_box_read=''",
       page.input_value(s0) == "2025" and filler._date_box_read(page, idx, 0) == "",
       "input=%r read=%r" % (page.input_value(s0), filler._date_box_read(page, idx, 0)))
    page.fill(s0, "")
    page.wait_for_timeout(80)

    # ------------------------------------------------------------ B 新实现
    print("\n== B) 新实现：提交 4 段 + 重渲染仍在 ==")
    notes = []
    ok, how = filler._fill_date_range(page, fa, "2023.9-2027.6",
                                      note=lambda m: notes.append(str(m)))
    ck("B1 _fill_date_range 返回成功", ok is True, "%r %r" % (ok, how))
    got = [filler._date_box_read(page, idx, k) for k in range(4)]
    ck("B2 4 段逐段读回 = 2023/9/2027/6", got == ["2023", "9", "2027", "6"], got)
    ck("B3 model 值正确",
       page.evaluate("() => [window.__model.a_y1, window.__model.a_m1,"
                     " window.__model.a_y2, window.__model.a_m2]")
       == ["2023", "9", "2027", "6"],
       page.evaluate("() => JSON.stringify(window.__model)"))
    page.click("#rerender")
    page.wait_for_timeout(150)
    got2 = [filler._date_box_read(page, idx, k) for k in range(4)]
    ck("B4 **整行重渲染后仍在**（这是回车提交的关键收益）",
       got2 == ["2023", "9", "2027", "6"], got2)

    # ------------------------------------------------------------ C 补零
    print("\n== C) 借位：站点拒绝补零月份，实现要自动改写 ==")
    page.evaluate(filler.DATE_RANGE_JS, str(idx))
    sm1 = '[data-daterange="%s:1"]' % idx
    page.fill('[data-daterange="%s:0"]' % idx, "2024")
    page.keyboard.press("Enter")
    page.wait_for_timeout(120)
    before = page.evaluate("() => window.__model.a_m1 || ''")
    page.fill(sm1, "06")
    page.keyboard.press("Enter")
    page.wait_for_timeout(150)
    after = page.evaluate("() => window.__model.a_m1 || ''")
    ck("C1 直写 '06' 被站点拒绝（夹具非恒真：model 里不是 '06'）",
       after != "06", "before=%r after=%r" % (before, after))

    notes2 = []
    ok2, how2 = filler._fill_date_range(page, fa, "2024.06 - 2024.12",
                                        note=lambda m: notes2.append(str(m)))
    ck("C2 新实现仍报成功", ok2 is True, "%r %r" % (ok2, how2))
    m1 = filler._date_box_read(page, idx, 1)
    m2 = filler._date_box_read(page, idx, 3)
    ck("C3 月份最终 = 6 / 12（补零被自动改写）", m1 == "6" and m2 == "12",
       "m1=%r m2=%r" % (m1, m2))
    ck("C4 备注如实说明「不接受补零写法，已改写」",
       any(u"补零" in x and u"改写" in x for x in notes2), notes2)

    # ------------------------------------------------------------ E form 安全
    print("\n== E) 行在 <form> 里时不回车、如实报失败 ==")
    fb = byi.get("b_y1")
    ck("E0 夹具里 form 内的年月框被采集到", fb is not None)
    idb = fb["idx"]
    info_b = page.evaluate(filler.DATE_RANGE_JS, str(idb))
    ck("E1 DATE_RANGE_JS 对 form 内的行返回 enterSafe=0",
       (info_b or {}).get("enterSafe") == 0, info_b)
    info_a = page.evaluate(filler.DATE_RANGE_JS, str(idx))
    ck("E2 不在 form 内的行 enterSafe=1", (info_a or {}).get("enterSafe") == 1, info_a)
    okb, howb = filler._fill_date_range(page, fb, "2020.01-2021.02")
    ck("E3 form 内**不回车** → 如实返回失败（宁可失败也不隐式提交）",
       okb is False and u"回读不符" in str(howb), "%r %r" % (okb, howb))
    ck("E4 且确实没有偷偷提交（model 里没有 form 那行的值）",
       not page.evaluate("() => window.__model.b_y1"),
       page.evaluate("() => JSON.stringify(window.__model)"))
    page.close()
    b.close()

print("\n通过 %d 项，失败 %d 项" % (N[0] - len(BAD), len(BAD)))
if BAD:
    print("失败项：" + "；".join(BAD))
    sys.exit(1)
print("全部 PASS：T75「年月框回车提交 + 月份不认补零 + sd-Select 只看显示层」已锁定")
