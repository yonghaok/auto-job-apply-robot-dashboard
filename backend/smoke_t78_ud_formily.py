# -*- coding: utf-8 -*-
"""T78 自测：飞书招聘（UD / Formily）表单支持。

覆盖四件事（全部在本地夹具上跑，不访问任何真实站点）：
  A. 标题解析：标题在 `.ud-formily-item-label`，距 input **≥7 层**祖先
     （旧的「兄弟节点兜底」只走 6 层 → 整页 0 命中，这就是用户报的「自动填写未生效」）
  B. 列表下拉 `ud__select`：点选择器 → 点 `.ud__select__list__item` → 按显示值回读
  C. 树形下拉（地区）：搜索框过滤 → 点同名叶子（资料「杭州市」 vs 树里「杭州」）
  D. 起止时间：`throne-biz-date-range-picker` 一个 wrapper 两个框 → 分写起止
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import filler  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

PASS = []
FAIL = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if ok else "FAIL", name, (" | " + str(detail)) if detail else ""))


FIXTURE = """
<html><head><meta charset="utf-8"><style>
.ud__select__dropdown{position:absolute;background:#fff;border:1px solid #ccc}
.ud__select__list__item{padding:4px}
</style></head><body>
<!-- A：标题距 input 8 层（旧逻辑只走 6 层） -->
<div class="ud-formily-item">
  <div class="ud-formily-item-label"><div class="ud-formily-item-label-content">
    <span><label>姓名</label><span class="ud-formily-item-asterisk">*</span></span></div></div>
  <div class="ud-formily-item-control"><div class="ud-formily-item-control-content">
    <div class="ud-formily-item-control-content-component">
      <div class="ud__input"><div><label class="ud__input-input-wrap"><div>
        <input class="ud__native-input" id="nameInp">
      </div></label></div></div>
    </div></div></div>
</div>

<div class="ud-formily-item">
  <div class="ud-formily-item-label"><div class="ud-formily-item-label-content">
    <span><label>邮箱</label><span class="ud-formily-item-asterisk">*</span></span></div></div>
  <div class="ud-formily-item-control"><div class="ud-formily-item-control-content">
    <div class="ud-formily-item-control-content-component">
      <div class="ud__input"><div><label class="ud__input-input-wrap"><div>
        <input class="ud__native-input" id="mailInp">
      </div></label></div></div>
    </div></div></div>
</div>

<!-- B：列表下拉 -->
<div class="ud-formily-item" id="sexItem">
  <div class="ud-formily-item-label"><div class="ud-formily-item-label-content"><span><label>性别</label></span></div></div>
  <div class="ud-formily-item-control"><div class="ud-formily-item-control-content">
    <div class="ud-formily-item-control-content-component">
      <div class="ud__select">
        <div class="ud__select__selector" onclick="openList('sexDrop')">
          <div class="ud__select__selector__content">
            <div class="ud__select__selector__placeholder" id="sexShown"></div>
            <input class="ud__select__selector__search__input ud__native-input" role="combobox" readonly>
          </div>
        </div>
        <div class="ud__select__dropdown" id="sexDrop" style="display:none">
          <div class="ud__select__list">
            <div class="ud__select__list__item" onclick="pickList('sexDrop','sexShown',this)">男</div>
            <div class="ud__select__list__item" onclick="pickList('sexDrop','sexShown',this)">女</div>
          </div>
        </div>
      </div>
    </div></div></div>
</div>

<!-- C：树形下拉（地区） -->
<div class="ud-formily-item" id="cityItem">
  <div class="ud-formily-item-label"><div class="ud-formily-item-label-content"><span><label>所在地点</label></span></div></div>
  <div class="ud-formily-item-control"><div class="ud-formily-item-control-content">
    <div class="ud-formily-item-control-content-component">
      <div class="ud__select">
        <div class="ud__select__selector" onclick="openList('cityDrop')">
          <div class="ud__select__selector__content">
            <div class="ud__select__selector__placeholder" id="cityShown"></div>
            <input class="ud__select__selector__search__input ud__native-input" id="citySearch"
                   role="combobox" oninput="filterTree(this.value)">
          </div>
        </div>
        <div class="ud__select__dropdown" id="cityDrop" style="display:none">
          <div class="ud__tree">
            <div class="ud__tree__node"><div class="ud__tree__node__label" onclick="pickTree(this,'浙江')">浙江</div></div>
            <div class="ud__tree__node"><div class="ud__tree__node__label" onclick="pickTree(this,'杭州市')">杭州市</div></div>
            <div class="ud__tree__node"><div class="ud__tree__node__label" onclick="pickTree(this,'杭州')">杭州</div></div>
            <div class="ud__tree__node"><div class="ud__tree__node__label" onclick="pickTree(this,'上海')">上海</div></div>
          </div>
        </div>
      </div>
    </div></div></div>
</div>

<!-- D：起止时间（一个 wrapper 两个框） -->
<div class="ud-formily-item" id="perItem">
  <div class="ud-formily-item-label"><div class="ud-formily-item-label-content"><span><label>起止时间</label></span></div></div>
  <div class="ud-formily-item-control"><div class="ud-formily-item-control-content">
    <div class="ud-formily-item-control-content-component">
      <div class="throne-biz-date-range-picker-wrapper">
        <div class="throne-biz-date-range-picker-input"><div class="ud__input">
          <input class="ud__native-input" id="perStart">
        </div></div>
        <span>~</span>
        <div class="throne-biz-date-range-picker-input"><div class="ud__input">
          <input class="ud__native-input" id="perEnd">
        </div></div>
      </div>
    </div></div></div>
</div>

<script>
function openList(id){ document.getElementById(id).style.display = 'block'; }
function pickList(dropId, shownId, el){
  document.getElementById(shownId).textContent = el.textContent.trim();
  document.getElementById(dropId).style.display = 'none';
}
function pickTree(el, txt){
  document.getElementById('cityShown').textContent = txt;
  document.getElementById('cityDrop').style.display = 'none';
}
function filterTree(q){
  document.querySelectorAll('#cityDrop .ud__tree__node').forEach(n => {
    const t = n.textContent.trim();
    n.style.display = (!q || t.indexOf(q) >= 0) ? '' : 'none';
  });
}
</script>
</body></html>
"""


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True, args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1200, "height": 900})
        pg.set_content(FIXTURE)
        pg.wait_for_timeout(300)

        print("\n[A] 标题解析（Formily 深层标题）")
        fields = filler._collect_fields(pg)
        by_label = {}
        for f in fields:
            by_label.setdefault((f.get("label") or ""), []).append(f)
        check("采集到控件", len(fields) >= 6, len(fields))
        check("姓名 标题被识别", "姓名" in by_label, sorted(by_label)[:6])
        check("邮箱 标题被识别", "邮箱" in by_label)
        check("性别 标题被识别", "性别" in by_label)
        check("起止时间 标题被识别", "起止时间" in by_label)
        depth = pg.evaluate("""() => {
            const e = document.querySelector('#nameInp');
            const it = e.closest('.ud-formily-item');
            let d = 0; for (let p = e.parentElement; p && p !== it; p = p.parentElement) d++;
            return d;
        }""")
        check("姓名框到 item 的距离 ≥7 层（旧 6 层兜底必失败）", depth >= 7, "depth=%s" % depth)

        print("\n[B] 列表下拉（ud__select）")
        sex = by_label.get("性别", [{}])[0]
        sel = '[data-filler-idx="%s"]' % sex.get("idx")
        check("性别 是 ud__select 控件",
              "ud__select" in (pg.eval_on_selector(sel, "e => (e.className||'').toString()") or ""))
        ok = filler._ud_one_pick(pg, sel, "男")
        check("点选「男」成功", ok)
        check("选择器显示「男」", pg.inner_text("#sexShown").strip() == "男",
              pg.inner_text("#sexShown").strip())
        check("不存在的选项必须失败（反向）", filler._ud_one_pick(pg, sel, "外星人") is False)

        print("\n[C] 树形下拉（地区）")
        city = by_label.get("所在地点", [{}])[0]
        csel = '[data-filler-idx="%s"]' % city.get("idx")
        ok = filler._ud_tree_pick(pg, csel, "浙江省-杭州市")
        check("地区级联点选成功", ok)
        shown = pg.inner_text("#cityShown").strip()
        check("显示层命中地区（资料写「杭州市」）", shown in ("杭州", "杭州市"), shown)

        print("\n[D] 起止时间（一个 wrapper 两个框）")
        per = by_label.get("起止时间", [{}])[0]
        n = pg.evaluate(filler.UD_PERIOD_MARK_JS, str(per.get("idx")))
        check("配对标记数 = 2", int(n) == 2, n)
        ok, how = filler._fill_period_pair(pg, per, "2023.9-2027.6")
        check("分两段写入成功", ok, how)
        check("起始框 = 2023-09", pg.input_value("#perStart") == "2023-09", pg.input_value("#perStart"))
        check("结束框 = 2027-06", pg.input_value("#perEnd") == "2027-06", pg.input_value("#perEnd"))

        print("\n[E] 别名（飞书写法）")
        for alias, key in (("家乡", "native_place"), ("期望工作地点", "intended_city"),
                           ("所在地点", "location")):
            hit = any(alias in filler.FIELD_ALIASES.get(key, []) for _ in [0])
            check("别名表含「%s」→ %s" % (alias, key), hit)

        b.close()

    print("\n汇总：PASS=%d FAIL=%d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：", "；".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
