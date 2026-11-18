# -*- coding: utf-8 -*-
"""smoke_t72_labels.py —— 锁定「字段标题读到『必填项未填写』」（T72/T72b）修复。

背景（用户反馈，全志 allwinnertech / MokaHR 投递页「匹配字段仍错误」）：
MokaHR 把校验提示渲染在 `.sd-Input-message.sd-Input-error` 里，而**这个节点就在包着
input 的那一个 `<label>` 内部** → 直接读 `label.innerText` 会拿到「必填项未填写」，
该字段从此再也匹配不上（实测：现家庭住址 / 期望就业城市 的 label 变成「必填项未填写」）。
已选值同理渲染在同层 `[class*=display-value]` 里，也会被当标题。

修复：三份 JS（COLLECT_JS / ANALYZE_JS / inspect-JS）统一先**摘掉**
option/select + display/提示/校验类节点，再读文本；整段只是提示语 → 视为「没有标题」。

本套件的关键设计：**先证明夹具真的复现了 bug**（用「旧口径」读同一份 DOM 必须能读到
『必填项未填写』），再断言新口径读不到 —— 否则测试可能只是恒真（vacuous）。

隔离：RESUME_* 指向临时目录；CDP 指向死端口；夹具写临时文件；用本地 headless chromium
（**绝不碰用户真实浏览器**）。
"""
import io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="t72_")
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


FIXTURE = u"""<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<!-- 1) label 内含校验提示：T72b 的核心场景 -->
<label class="fld"><span class="ttl">现家庭住址</span>
  <input id="addr" type="text" placeholder="请选择省市区">
  <div class="sd-Input-message sd-Input-error">\u5fc5\u586b\u9879\u672a\u586b\u5199</div>
</label>

<!-- 2) label 内含已选值 span：值文本不许当标题 -->
<label class="fld"><span class="ttl">\u610f\u5411\u5de5\u4f5c\u57ce\u5e02</span>
  <input id="city" type="text" placeholder="\u9009\u62e9\u610f\u5411\u5de5\u4f5c\u57ce\u5e02">
  <span class="sd-Input-display-value">\u676d\u5dde\u3001\u4e0a\u6d77</span>
</label>

<!-- 3) 裸 label 包 select：选项文案不许当标题 -->
<label class="fld"><span class="ttl">\u6700\u9ad8\u5b66\u5386</span>
  <select id="deg"><option>\u8bf7\u9009\u62e9</option><option>\u672c\u79d1</option><option>\u7855\u58eb</option></select>
</label>

<!-- 4) 只有提示语、没有真标题 → 不许把提示语当标题 -->
<div class="fld"><input id="solo" type="text" placeholder="">
  <div class="sd-Input-message sd-Input-error">\u5fc5\u586b\u9879\u672a\u586b\u5199</div>
</div>

<!-- 5) 兄弟 div 标题（主流程，必须不受影响） -->
<div class="apply-field"><div class="title"><span>\u516c\u53f8\u540d\u79f0</span></div>
  <div class="ctrl"><input id="co" type="text" placeholder="\u516c\u53f8\u540d\u79f0"></div></div>

<!-- 6) 兄弟 div 标题 + 提示语混合（最狠的组合） -->
<div class="apply-field"><div class="title"><span>\u9879\u76ee\u540d\u79f0</span></div>
  <div class="ctrl"><input id="pj" type="text" placeholder="\u9879\u76ee\u540d\u79f0"></div>
  <div class="sd-Input-message sd-Input-error">\u5fc5\u586b\u9879\u672a\u586b\u5199</div></div>
</body></html>
"""

# 「更早口径」读标题：label 直接 innerText、兄弟遍历也直接 innerText（**完全不筛**）。
# 用它来**证明夹具确实复现了 bug**（否则本套件可能恒真）。
OLD_JS = r"""(sel) => {
  const e = document.querySelector(sel);
  if (!e) return null;
  try {
    if (e.labels && e.labels[0]) {
      return ((e.labels[0].innerText || e.labels[0].textContent || '') + '')
        .replace(/\s+/g, ' ').trim();
    }
  } catch (_) {}
  for (let p = e.parentElement, h = 0; p && h < 6; p = p.parentElement, h++) {
    let t = '';
    for (const c of p.children) {
      try { if (c.contains(e)) continue; } catch (_) { continue; }
      if (c.tagName === 'INPUT' || c.tagName === 'TEXTAREA' || c.tagName === 'SELECT') continue;
      try { if (c.querySelector && c.querySelector('input,textarea,select')) continue; } catch (_) {}
      t += ' ' + (c.innerText || c.textContent || '');
    }
    t = t.replace(/\s+/g, ' ').replace(/^[^\w\u4e00-\u9fa5]+|[^\w\u4e00-\u9fa5]+$/g, '').trim();
    if (t && t.length <= 20) return t;
  }
  return '';
}"""

HINT = u"\u5fc5\u586b\u9879\u672a\u586b\u5199"   # 必填项未填写

FF = os.path.join(_TMP, "fixture.html")
io.open(FF, "w", encoding="utf-8").write(FIXTURE)

# ---------------------------------------------------------------- D 源码结构
print("== D) 三份 JS 必须同步（摘值/提示 + 提示语不当标题）==")
fsrc = io.open(os.path.join(HERE, "filler.py"), encoding="utf-8").read()
ck("D1 三处都摘掉 [class*=error]（校验提示容器）",
   fsrc.count("[class*=error]") >= 3, "count=%d" % fsrc.count("[class*=error]"))
ck("D2 三处都摘掉 [class*=display]（已选值容器）",
   fsrc.count("[class*=display]") >= 3, "count=%d" % fsrc.count("[class*=display]"))
ck("D3 COLLECT_JS：提示语不当标题（if (_HINT.test(label)) label = '';）",
   "if (_HINT.test(label)) label = '';" in fsrc)
ck("D4 COLLECT_JS：_cleanNode 定义在 labelOf 之前（否则 TDZ 报错）",
   0 <= fsrc.index("const _cleanNode = (node) => {") < fsrc.index("const labelOf = (lb) =>"))
ck("D5 ANALYZE_JS：nearTitle 之前已有 _cleanNode",
   0 <= fsrc.index("const nearTitle = (el) => {")
   and fsrc.rindex("const _cleanNode = (node) => {", 0, fsrc.index("const nearTitle = (el) => {")) > 0)
ck("D6 ANALYZE_JS hayOf：label 也走 _cleanNode 且提示语不算标题",
   "const _l = _cleanNode(el.labels[0]);" in fsrc and "if (_l && !_HINT.test(_l))" in fsrc)
ck("D7 第三份 inspect-JS：读 label 后同样剔除提示语",
   "if (/^(\u5fc5\u586b\u9879\u672a\u586b\u5199|\u6682\u65e0\u9009\u9879|\u8bf7\u9009\u62e9|\u8bf7\u8f93\u5165|\u8bf7\u586b\u5199|\u672a\u586b\u5199|\u5fc5\u586b|\u9009\u586b)$/.test(label)) label = '';" in fsrc)
ck("D8 COLLECT_JS / ANALYZE_JS 各有一份 _KILL（常量声明）",
   fsrc.count("const _KILL =") >= 2, "count=%d" % fsrc.count("const _KILL ="))

# ---------------------------------------------------------------- 真浏览器
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox"])
    page = b.new_page()
    page.goto("file:///" + FF.replace("\\", "/"), wait_until="domcontentloaded")
    page.wait_for_timeout(200)

    fields = filler._collect_fields(page)
    byi = {f.get("id"): f for f in fields}
    # 用 id 定位更稳（_collect_fields 返回的 dict 带 id）
    def lab(i):
        return (byi.get(i) or {}).get("label")

    print("\n== A) 夹具确实复现了 bug（更早口径必须读到污染文本）==")
    for dom, tag in (("addr", "label 内含校验提示"), ("solo", "只有提示语没标题")):
        old = page.evaluate(OLD_JS, "#" + dom)
        ck("A/%s：更早口径读到『必填项未填写』（证明夹具非恒真）" % dom,
           isinstance(old, str) and HINT in old, "old=%r" % (old,))
    old_city = page.evaluate(OLD_JS, "#city")
    ck("A/city：更早口径把已选值当标题（杭州、上海）",
       isinstance(old_city, str) and "\u676d\u5dde" in old_city, "old=%r" % (old_city,))
    old_deg = page.evaluate(OLD_JS, "#deg")
    ck("A/deg：更早口径把选项文案当标题（请选择/本科）",
       isinstance(old_deg, str) and ("\u8bf7\u9009\u62e9" in old_deg or "\u672c\u79d1" in old_deg),
       "old=%r" % (old_deg,))

    print("\n== B) 新口径：标题干净 ==")
    ck("B1 「现家庭住址」的 label 不再是『必填项未填写』，而是真标题",
       lab("addr") == "\u73b0\u5bb6\u5ead\u4f4f\u5740", "label=%r" % (lab("addr"),))
    ck("B2 「意向工作城市」的 label 不夹带已选值（杭州、上海）",
       lab("city") == "\u610f\u5411\u5de5\u4f5c\u57ce\u5e02", "label=%r" % (lab("city"),))
    ck("B3 「最高学历」的 label 不夹带选项文案",
       lab("deg") == "\u6700\u9ad8\u5b66\u5386", "label=%r" % (lab("deg"),))
    ck("B4 只有提示语的控件 → label 为空（提示语绝不当标题）",
       (lab("solo") or "") == "", "label=%r" % (lab("solo"),))
    ck("B5 反向：任何控件的 label 都不含『必填项未填写』",
       all(HINT not in (f.get("label") or "") for f in fields),
       [(f.get("id"), f.get("label")) for f in fields if HINT in (f.get("label") or "")])

    print("\n== C) 主流程护栏：兄弟 div 标题兜底不能被误伤 ==")
    ck("C1 兄弟 div 标题「公司名称」仍能取到", lab("co") == "\u516c\u53f8\u540d\u79f0",
       "label=%r" % (lab("co"),))
    ck("C2 兄弟标题 + 提示语混合时，取标题而不是提示语",
       lab("pj") == "\u9879\u76ee\u540d\u79f0", "label=%r" % (lab("pj"),))
    b.close()

print("\n通过 %d 项，失败 %d 项" % (N[0] - len(BAD), len(BAD)))
if BAD:
    print("失败项：" + "；".join(BAD))
    sys.exit(1)
print("全部 PASS：T72/T72b「标题读到校验提示 / 已选值」已锁定")
