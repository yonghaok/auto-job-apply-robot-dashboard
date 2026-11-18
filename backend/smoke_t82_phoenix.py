# -*- coding: utf-8 -*-
"""T82：北森 Beisen / Phoenix 组件族（虹软 career.arcsoft.com.cn 实测）。

锁定的都是「静默填不进去」的坑，每条都配反向断言：
  · 标题在 9 层深处（`label.form-item__text`）→ 读不到就整页字段匹配不上；
  · 下拉是 `.phoenix-select`（点内部 input 不展开），面板分列表 / 月 / 日三种；
  · 单选是纯 div，没有 input[type=radio]；
  · 「添加X经历」按钮在**区块容器内部**，且多行时字段属性必须带 path（否则串行覆盖）。
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import filler  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s  %s" % (name, detail))


FIELD = """
<div class="form-item form-item--phoenix">
  <div class="form-item__title"><label class="form-item__text">%(label)s</label></div>
  <div class="form-item__control">
    <div class="phoenix-unmodeled-layer"><div class="phoenix-unmodeled-layer__inner">
      <div class="phoenix-unmodeled-layer__protect"><div class="phoenix-unmodeled-layer__content">
        %(body)s
      </div></div>
    </div></div>
  </div>
</div>
"""


def text_field(label):
    return FIELD % {"label": label,
                    "body": '<input class="phoenix-input__input">'}


def textarea_field(label):
    return FIELD % {"label": label,
                    "body": '<textarea class="phoenix-textarea__realTextarea"></textarea>'}


def select_field(label, kind, options="", test=""):
    attr = ' data-test="%s"' % test if test else ""
    return FIELD % {
        "label": label,
        "body": ('<div class="phoenix-select" data-kind="%s" data-options="%s"%s>'
                 '<input class="phoenix-select__input">'
                 '<div class="phoenix-select__placeHolder">请选择</div></div>'
                 % (kind, options, attr)),
    }


def radio_field(label, items, test):
    inner = "".join(
        '<div class="phoenix-radio-group__radioItem">'
        '<div class="phoenix-radio"><span class="phoenix-radio__radio-text">%s</span></div>'
        '</div>' % x for x in items)
    return FIELD % {"label": label,
                    "body": '<div class="phoenix-radio-group" data-test="%s">%s</div>'
                            % (test, inner)}


EDU_ROW = """
<div class="row" data-row>
  %s %s %s %s %s
</div>
""" % (text_field("学校名称"), text_field("专业名称"),
       select_field("学历", "list", "大专|本科|硕士研究生"),
       select_field("开始时间", "month"), select_field("结束时间", "month"))

PROJ_ROW = """
<div class="row" data-row>
  %s %s %s %s %s %s
</div>
""" % (text_field("项目名称"), text_field("职务"),
       select_field("开始时间", "month"), select_field("结束时间", "month"),
       textarea_field("项目描述"), textarea_field("项目中职责"))

HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
body{font:14px sans-serif}.form-item{margin:6px 0;display:flex}
.form-item__title{width:110px}.phoenix-select{width:180px;height:30px;border:1px solid #ccc;
 display:flex;align-items:center;position:relative}
.phoenix-select__input{border:0;width:150px}.phoenix-input__input,.phoenix-textarea__realTextarea{
 width:180px;height:26px}.phoenix-radio__radio-text{display:inline-block;padding:2px 8px;
 border:1px solid #ccc;margin-right:6px;cursor:pointer}
.phoenix-radio--checked .phoenix-radio__radio-text{background:#0a7;color:#fff}
.phoenix-selectList,.phoenix-calendar-month-panel,.phoenix-calendar-panel{
 position:absolute;z-index:9;background:#fff;border:1px solid #888;padding:4px}
.phoenix-selectList__listItem,.phoenix-calendar-month-panel-cell{padding:2px 8px;cursor:pointer}
.sec{margin-top:14px;border-top:1px solid #ddd;padding-top:8px}
.add-btn{display:inline-block;margin:6px 0;color:#06c;cursor:pointer}
</style></head><body>
<div class="form-root">
%(name)s %(gender)s %(adjust)s %(degree)s
<div class="sec">
  <div class="sec-title">教育经历</div>
  <div class="sec-body" id="edu">
    <template id="edu-tpl">%(edu_row)s</template>
    <span class="add-btn">添加教育经历</span>
  </div>
</div>
<div class="sec">
  <div class="sec-title">项目经历</div>
  <div class="sec-body" id="proj">
    <template id="proj-tpl">%(proj_row)s</template>
    <span class="add-btn">添加项目经历</span>
  </div>
</div>
<div class="phoenix-selectList" id="listPanel" style="display:none"></div>
<div class="phoenix-calendar-month-panel" id="monthPanel" style="display:none">
  <div class="phoenix-calendar-month-panel-header">
    <a class="phoenix-calendar-month-panel-prev-year-btn" onclick="shiftYear(-1)">&lt;</a>
    <span class="phoenix-calendar-month-panel-year-select">2026</span>
    <a class="phoenix-calendar-month-panel-next-year-btn" onclick="shiftYear(1)">&gt;</a>
  </div>
  <div class="phoenix-calendar-month-panel-body" id="monthCells"></div>
</div>
<div class="phoenix-calendar-panel" id="datePanel" style="display:none">
  <input class="phoenix-calendar-input">
</div>
</div>
<script>
var owner = null, curYear = 2026;
function openPicker(el) {
  owner = el;
  ['listPanel','monthPanel','datePanel'].forEach(function (id) {
    document.getElementById(id).style.display = 'none';
  });
  var kind = el.getAttribute('data-kind');
  if (kind === 'list') {
    var p = document.getElementById('listPanel');
    p.innerHTML = '';
    (el.getAttribute('data-options') || '').split('|').forEach(function (t) {
      if (!t) return;
      var d = document.createElement('div');
      d.className = 'phoenix-selectList__listItem';
      d.textContent = t;
      d.onclick = function () { el.querySelector('input').value = t; p.style.display = 'none'; };
      p.appendChild(d);
    });
    p.style.display = 'block';
  } else if (kind === 'month') {
    curYear = 2026;
    paintMonth();
    document.getElementById('monthPanel').style.display = 'block';
  } else if (kind === 'date') {
    document.getElementById('datePanel').style.display = 'block';
  }
}
function paintMonth() {
  document.querySelector('.phoenix-calendar-month-panel-year-select').textContent = curYear;
  var box = document.getElementById('monthCells');
  box.innerHTML = '';
  for (var m = 1; m <= 12; m++) {
    (function (mm) {
      var d = document.createElement('div');
      d.className = 'phoenix-calendar-month-panel-cell';
      d.textContent = mm + '月';
      d.onclick = function () {
        if (owner) owner.querySelector('input').value = curYear + '-' + (mm < 10 ? '0' + mm : mm);
        document.getElementById('monthPanel').style.display = 'none';
      };
      box.appendChild(d);
    })(m);
  }
}
function shiftYear(d) { curYear += d; paintMonth(); }
function addRow(secId, tplId) {
  var tpl = document.getElementById(tplId);
  var row = tpl.content.firstElementChild.cloneNode(true);
  var sec = document.getElementById(secId);
  sec.insertBefore(row, sec.querySelector('.add-btn'));
}
document.addEventListener('click', function (ev) {
  var sel = ev.target.closest ? ev.target.closest('.phoenix-select') : null;
  if (sel) { ev.stopPropagation(); openPicker(sel); return; }
  var add = ev.target.closest ? ev.target.closest('.add-btn') : null;
  if (add) {
    var t = (add.textContent || '').trim();
    if (t === '添加教育经历') addRow('edu', 'edu-tpl');
    if (t === '添加项目经历') addRow('proj', 'proj-tpl');
  }
  var radio = ev.target.closest ? ev.target.closest('.phoenix-radio') : null;
  if (radio) {
    var g = radio.closest('.phoenix-radio-group');
    Array.prototype.forEach.call(g.querySelectorAll('.phoenix-radio'), function (r) {
      r.classList.remove('phoenix-radio--checked');
    });
    radio.classList.add('phoenix-radio--checked');
  }
});
document.addEventListener('keydown', function (ev) {
  if (ev.key === 'Enter' && ev.target.classList.contains('phoenix-calendar-input')) {
    if (owner) owner.querySelector('input').value = ev.target.value;
    document.getElementById('datePanel').style.display = 'none';
  }
});
</script></body></html>
"""

PROFILE = {
    "gender": "女",
    "accept_adjust": False,
    "education": {"school": "浙江工商大学", "major": "电子信息工程",
                  "degree": "本科", "period": "2023.9-2027.6"},
    "projects": [
        {"name": "项目A", "role": "产品负责人", "period": "2026.2-2026.6", "desc": "描述A"},
        {"name": "项目B", "role": "开发", "period": "2025.1-2025.6", "desc": "描述B"},
    ],
}

READ_ROWS = r"""(secSel) => {
  return Array.from(document.querySelectorAll(secSel + ' [data-row]')).map(
    (r) => Array.from(r.querySelectorAll('input,textarea')).map((e) => e.value));
}"""


def main():
    page_html = HTML % {
        "name": text_field("姓名"),
        "gender": radio_field("性别", ["男", "女", "保密"], "gender"),
        "adjust": radio_field("是否接受地点调剂", ["是", "否"], "adjust"),
        "degree": select_field("学历", "list", "大专|本科|硕士研究生", "degree"),
        "edu_row": EDU_ROW,
        "proj_row": PROJ_ROW,
    }
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        page = b.new_page()
        page.set_content(page_html)
        page.wait_for_timeout(200)

        print("== 探测与标签 ==")
        check("_phoenix_active 认得出北森表单", filler._phoenix_active(page) is True)
        fields = filler._collect_fields(page)
        by_label = {f["label"]: f for f in fields}
        check("9 层深处的标题能读到（姓名）", "姓名" in by_label, list(by_label))
        check("下拉也能读到标题（学历）", "学历" in by_label)

        # 反向：把 label 选择器改成旧版（只认 form-item-label）→ 读不到标题
        old = page.evaluate("""() => {
            const lbl = document.querySelector('label.form-item__text');
            lbl.className = 'x-form-item-text';
            return true;
        }""")
        miss = {f["label"] for f in filler._collect_fields(page)}
        check("反向：旧选择器读不到 9 层标题", old and "姓名" not in miss, miss)
        page.evaluate("""() => {
            document.querySelector('.x-form-item-text').className = 'form-item__text';
        }""")

        print("== 下拉 / 月份 / 日期 ==")
        deg = page.locator('[data-test="degree"] input').first
        check("列表下拉选中「本科」", filler._phx_pick(page, '[data-test="degree"] input', "本科"))
        check("回读=本科", deg.input_value() == "本科", deg.input_value())

        check("反向：不存在的选项 → 不选中",
              filler._phx_pick(page, '[data-test="degree"] input', "博士") is False)

        # 教育行月份：先插一条模板行，再按 form-item 顺序取「开始时间」
        page.evaluate("openPicker(document.querySelector('#edu .add-btn'))")  # 关掉上一次的面板
        page.evaluate("""() => {
            const r = document.getElementById('edu-tpl').content.firstElementChild.cloneNode(true);
            const sec = document.getElementById('edu');
            sec.insertBefore(r, sec.querySelector('.add-btn'));
        }""")
        try:
            start_input = page.locator("#edu [data-row] .form-item").nth(3).locator("input").first
            start_input.scroll_into_view_if_needed()
            okm = page.evaluate(
                """() => { const el = document.querySelectorAll('#edu [data-row] .form-item')[3]
                     .querySelector('input'); el.setAttribute('data-test','edu-start'); return true; }""")
            check("月份选择器翻到 2023-09",
                  bool(okm) and filler._phx_pick(page, '[data-test="edu-start"]', "2023-09"))
            check("回读=2023-09",
                  page.locator('[data-test="edu-start"]').input_value() == "2023-09",
                  page.locator('[data-test="edu-start"]').input_value())
        except Exception as e:                                   # noqa: BLE001
            check("月份选择器翻到 2023-09", False, repr(e))

        print("== 单选（纯 div，无 input[type=radio]）==")
        check("页面里确实没有原生 radio（旧逻辑会 0 组）",
              page.evaluate("() => document.querySelectorAll('input[type=radio]').length") == 0)
        rf, rk = filler._phx_fill_radios(page, PROFILE)
        check("识别并点了 2 组（gender / accept_adjust）",
              sorted(rk) == ["accept_adjust", "gender"], rk)
        check("性别选中「女」",
              page.evaluate("""() => { const c = document.querySelector(
                  '[data-test=gender] .phoenix-radio--checked');
                  return c ? c.innerText.trim() : ''; }""") == "女")
        check("是否接受调剂选中「否」",
              page.evaluate("""() => { const c = document.querySelector(
                  '[data-test=adjust] .phoenix-radio--checked');
                  return c ? c.innerText.trim() : ''; }""") == "否")


        print("== 可重复区块 ==")
        page.evaluate("() => { document.querySelectorAll('#edu [data-row]').forEach(r => r.remove()); }")
        notes = []
        reports, handled = filler._phx_fill_sections(page, PROFILE, note=notes.append)
        check("教育 1 条 + 项目 2 条都写了报告", len(reports) == 2, reports)
        check("handled = {education, projects}", handled == {"education", "projects"}, handled)
        check("报告里记录了「添加」次数", any("「添加」" in r for r in reports), reports)

        edu_rows = page.evaluate(READ_ROWS, "#edu")
        proj_rows = page.evaluate(READ_ROWS, "#proj")
        check("教育行数=1", len(edu_rows) == 1, edu_rows)
        check("教育行内容正确", edu_rows and edu_rows[0] ==
              ["浙江工商大学", "电子信息工程", "本科", "2023-09", "2027-06"], edu_rows)
        check("项目行数=2", len(proj_rows) == 2, proj_rows)
        check("项目第 1 行正确", proj_rows and proj_rows[0] ==
              ["项目A", "产品负责人", "2026-02", "2026-06", "描述A", "描述A"], proj_rows)
        check("项目第 2 行正确", len(proj_rows) > 1 and proj_rows[1] ==
              ["项目B", "开发", "2025-01", "2025-06", "描述B", "描述B"], proj_rows)
        # 关键反向：项目第 1 行的日期**不许**覆盖教育的日期（曾因 data-phx-field 不带 path 串行）
        check("反向：教育日期没被项目第 1 行覆盖", edu_rows and edu_rows[0][3] == "2023-09", edu_rows)

        print("== 反向：不该动的时候不动 ==")
        before = page.evaluate("() => document.querySelectorAll('#proj [data-row]').length")
        reports2, _h2 = filler._phx_fill_sections(page, PROFILE)
        after = page.evaluate("() => document.querySelectorAll('#proj [data-row]').length")
        check("行数已够 → 不再点「添加」", before == after == 2, (before, after))
        check("二次填充不再重复计数行数", all("「添加」" not in r for r in reports2), reports2)

        check("反向：本站没有实习经历 → 返回空、不谎报",
              filler._phx_section_state(page, "experiences", ("实习经历",),
                                        ("公司名称",), "添加实习经历", {}) == {"found": False})

        # 非选择器控件：不该被当成下拉
        check("反向：文本框不会被当选择器",
              filler._phx_pick(page, "#edu [data-row] input:nth-of-type(1)", "x") is False)

        b.close()

    print("\n汇总：PASS=%d FAIL=%d" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
