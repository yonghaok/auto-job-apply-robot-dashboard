# -*- coding: utf-8 -*-
"""T81 自测：飞书招聘（atsx）「实习经历 / 项目经历」空模块建行 + 逐行填。

覆盖用户报的两个问题（全部本地夹具，不访问真实站点）：
  A. 「实习经历 / 项目经历」在站上是**空模块**：只有一个「添加」按钮，没有 input
     → 按锚点找卡片永远 notfound，必须按模块 id 点「添加」建行；
  B. 建行后是**并排卡片**，每行 4~5 个字段（含 textarea「描述」）
     → 行数不能数容器类名，行号必须按「行容器」算，否则 3 条全写进第 1 行。

同时钉住两个真实踩过的坑（都是「静默填不上」型）：
  · 标签选择器只写 `form-item-label` 匹配不到 `ud-formily-item-label` → label 全空；
  · 只查 input 会漏掉 textarea → 「描述」拿不到行号，永远不填。
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
<html><head><meta charset="utf-8"></head><body>
<!-- 工作经历：带「没有工作经历」勾选（默认勾上），用来验证不会被误取消 -->
<div class="applyFormModuleWrapper" id="workModule">
  <p class="applyFormModuleWrapper-text">工作经历</p>
  <label class="ud__checkbox"><input type="checkbox" id="noWork" checked>没有工作经历</label>
</div>

<!-- 实习经历：空模块，只有一个「添加」 -->
<div class="applyFormModuleWrapper">
  <p class="applyFormModuleWrapper-text">实习经历</p>
  <div id="formily-item-internship_list" class="ud-formily-item">
    <div class="ud-formily-item-control"><div class="ud-formily-item-control-content">
      <div class="ud-formily-item-control-content-component">
        <button type="button" onclick="addRow('internship_list')">添加</button>
      </div>
    </div></div>
  </div>
</div>

<!-- 项目经历：空模块 -->
<div class="applyFormModuleWrapper">
  <p class="applyFormModuleWrapper-text">项目经历</p>
  <div id="formily-item-project_list" class="ud-formily-item">
    <div class="ud-formily-item-control"><div class="ud-formily-item-control-content">
      <div class="ud-formily-item-control-content-component">
        <button type="button" onclick="addRow('project_list')">添加</button>
      </div>
    </div></div>
  </div>
</div>

<script>
function ctrl(kind, name) {
  var e;
  if (kind === 'textarea') { e = document.createElement('textarea'); }
  else { e = document.createElement('input'); e.className = 'ud__native-input'; }
  e.setAttribute('data-name', name);
  return e;
}
function item(lab, kind) {
  var box = document.createElement('div');
  box.className = 'ud-formily-item';
  var html = '<div class="ud-formily-item-label"><div class="ud-formily-item-label-content">' +
             '<span><label>' + lab + '</label></span></div></div>' +
             '<div class="ud-formily-item-control"><div class="ud-formily-item-control-content">' +
             '<div class="ud-formily-item-control-content-component"><div class="ud__input">' +
             '<div><label class="ud__input-input-wrap"><div class="ud__input-input__placeholder__wrapper"></div>' +
             '</label></div></div></div></div></div>';
  box.innerHTML = html;
  var host = box.querySelector('.ud__input-input__placeholder__wrapper');
  if (kind === 'period') {
    var wrap = document.createElement('div');
    wrap.className = 'throne-biz-date-range-picker-wrapper';
    wrap.appendChild(ctrl('input', lab + ':start'));
    wrap.appendChild(ctrl('input', lab + ':end'));
    host.appendChild(wrap);
  } else {
    host.appendChild(ctrl(kind, lab));
  }
  return box;
}
function addRow(mod) {
  var host = document.getElementById('formily-item-' + mod);
  var card = document.createElement('div');
  card.className = 'apply-form-array-card';
  var fields = (mod === 'internship_list')
    ? [['公司名称', 'input'], ['职位名称', 'input'], ['起止时间', 'period'], ['描述', 'textarea']]
    : [['项目名称', 'input'], ['项目角色', 'input'], ['起止时间', 'period'],
       ['描述', 'textarea'], ['项目链接', 'input']];
  fields.forEach(function (f) { card.appendChild(item(f[0], f[1])); });
  host.appendChild(card);
}
</script>
</body></html>
"""

PROFILE = {
    "experiences": [
        {"company": "杭州潜渊之心", "role": "AI 产品经理", "period": "2026.07-2026.09",
         "highlights": "海外 SaaS 增长"},
        {"company": "特斯拉", "role": "诊断 Agent 产品经理", "period": "2025.09-2026.03",
         "highlights": "智能诊断 Agent"},
        {"company": "杭州旺慧", "role": "硬件产品经理助理", "period": "2024.06-2024.12",
         "highlights": "能源路由器 UI"},
    ],
    "projects": [
        {"name": "AI 短剧分镜工作台", "role": "产品负责人", "period": "2026.02-2026.06",
         "desc": "人物一致性"},
        {"name": "家庭机器人千寻", "role": "预研负责人", "period": "2025.02-2025.12",
         "desc": "多模态感知"},
    ],
}

ROW_JS = r"""([mod, anchor]) => {
  const host = document.querySelector(mod);
  const T = n => ((n.innerText || n.textContent || '') + '').replace(/\s+/g, '');
  const LABSEL = '[class*="form-item-label"],[class*="formily-item-label"]';
  const lab = (e) => {
    for (let p = e.parentElement, h = 0; p && h < 14; p = p.parentElement, h++) {
      let lbs = [];
      try { lbs = Array.from(p.querySelectorAll(LABSEL)); } catch (_) { continue; }
      for (const lb of lbs) {
        if (lb.contains(e) || lb.querySelector('input,textarea,select')) continue;
        const t = T(lb).replace(/\*/g, '');
        if (t) return t;
      }
    }
    return '';
  };
  const els = Array.from(host.querySelectorAll('input,textarea'));
  return {rows: els.filter(e => lab(e) === anchor).length,
          vals: els.map(e => lab(e) + '=' + (e.value || ''))};
}"""


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True, args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1200, "height": 900})
        pg.set_content(FIXTURE)
        pg.wait_for_timeout(200)

        print("\n[A] 空模块：建行前")
        st = pg.evaluate(filler.UD_CARD_JS, {"anchor": "公司名称", "key": "experiences",
                                             "module": filler.UD_EXP_MODULE, "mark": False})
        check("模块识别成功", st.get("ok") is True, st)
        check("建行前 rows=0（旧逻辑在这里直接放弃）", int(st.get("rows") or 0) == 0, st.get("rows"))
        check("模块内有 1 个「添加」", int(st.get("btns") or 0) == 1, st.get("btns"))

        print("\n[B] 点「添加」建行")
        ok = pg.evaluate(filler.UD_CARD_ADD_JS, {"module": filler.UD_EXP_MODULE, "anchor": "公司名称"})
        check("点「添加」返回 True", ok is True)
        pg.wait_for_timeout(120)
        st = pg.evaluate(filler.UD_CARD_JS, {"anchor": "公司名称", "key": "experiences",
                                             "module": filler.UD_EXP_MODULE, "mark": False})
        check("建行后 rows=1", int(st.get("rows") or 0) == 1, st.get("rows"))
        # 4 个标题、5 个控件（起止时间是两个框）—— 与真机 probe27 一致
        check("一行 5 个控件（起止时间占两个框）", int(st.get("ctrls") or 0) == 5, st.get("ctrls"))

        print("\n[C] 逐行填（3 段实习 + 2 个项目）")
        done, keys = filler._ud_fill_arrays(pg, PROFILE, note=lambda m: print("      [note]", m))
        check("摘要用区块名（实习经历）", any("实习经历" in d for d in done), done)
        check("摘要用区块名（项目经历）", any("项目经历" in d for d in done), done)
        check("返回已填 section 名", keys == ["experiences", "projects"], keys)

        d = pg.evaluate(ROW_JS, ["#formily-item-internship_list", "公司名称"])
        check("实习经历 3 行", d["rows"] == 3, d["rows"])
        vals = d["vals"]
        check("第1行 公司名", "公司名称=杭州潜渊之心" in vals, vals[:5])
        check("第2行 公司名", "公司名称=特斯拉" in vals, vals[5:10])
        check("第3行 公司名", "公司名称=杭州旺慧" in vals, vals[10:15])
        check("第2行 职位不同（没串行）", "职位名称=诊断 Agent 产品经理" in vals)
        check("描述(textarea) 第3行已填", "描述=能源路由器 UI" in vals, vals[-1:])
        check("起止时间 分两段（第1行）", "起止时间=2026-07" in vals and "起止时间=2026-09" in vals,
              [v for v in vals if v.startswith("起止时间")][:4])
        check("起止时间 第3行结束段", "起止时间=2024-12" in vals)

        d2 = pg.evaluate(ROW_JS, ["#formily-item-project_list", "项目名称"])
        check("项目经历 2 行", d2["rows"] == 2, d2["rows"])
        check("项目第1行名称", "项目名称=AI 短剧分镜工作台" in d2["vals"], d2["vals"][:6])
        check("项目第2行角色", "项目角色=预研负责人" in d2["vals"], d2["vals"][6:])
        check("项目描述(textarea) 已填", "描述=多模态感知" in d2["vals"], d2["vals"][-2:])
        check("资料没有链接 → 项目链接保持空", "项目链接=" in d2["vals"], d2["vals"][-1:])

        print("\n[D] 反向断言（宽容度不能变恒真）")
        check("「没有工作经历」没被误取消", pg.is_checked("#noWork") is True)
        none = filler._ud_fill_section(pg, "experiences", "实习经历", PROFILE["experiences"],
                                       "#formily-item-nope", "公司名称", ())
        check("模块不存在 → 返回 None（不会被当成已填）", none is None)
        st = pg.evaluate(filler.UD_CARD_JS, {"anchor": "不存在的标题", "key": "x",
                                             "module": filler.UD_EXP_MODULE, "mark": False})
        check("锚点不存在 → rows=0（不是 1 也不是报错）", int(st.get("rows") or 0) == 0, st)
        n_before = pg.evaluate(ROW_JS, ["#formily-item-internship_list", "公司名称"])["rows"]
        filler._ud_fill_arrays(pg, {"experiences": PROFILE["experiences"]})   # 再跑一遍
        n_after = pg.evaluate(ROW_JS, ["#formily-item-internship_list", "公司名称"])["rows"]
        check("已够行数时不再点「添加」（行数不变）", n_after == n_before, "%s→%s" % (n_before, n_after))

        print("\n[E] 源码护栏（踩过的坑写成断言）")
        src = open(os.path.join(HERE, "filler.py"), encoding="utf-8").read()
        check("UD_CARD_JS 同时匹配 formily-item-label", 'formily-item-label' in filler.UD_CARD_JS)
        check("UD_CARD_JS 覆盖 textarea", "input,textarea" in filler.UD_CARD_JS)
        check("按模块 id 定位（internship_list）", filler.UD_EXP_MODULE == "#formily-item-internship_list")
        check("按模块 id 定位（project_list）", filler.UD_PROJ_MODULE == "#formily-item-project_list")
        check("不再用「数容器类名」当行数", "apply-form-array-card-content" not in filler.UD_CARD_JS)
        check("_ud_fill_arrays 返回 (摘要, keys)", "return done, keys" in src)

        b.close()

    print("\n汇总：PASS=%d FAIL=%d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：", "；".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
