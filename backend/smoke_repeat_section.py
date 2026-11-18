# -*- coding: utf-8 -*-
"""「可重复区块（项目经历 / 工作经历）」离线自测 —— T68。

背景（用户 2026-09-23 报）：
  「简历信息填写页**只会填写一个项目**，**需要点击新增项目**添加更多项目经历」。
根因两层：
  ① `match_fields` 是「一个 profile 路径**最多用一次**」的贪心一对一 → `projects` 只命中第一个
     区块里的一个控件，第 2/3 个项目**无处可去**；
  ② 招聘站的「项目经历」是**可重复区块**：默认只有 1 块，要**点站点的「新增项目」按钮**
     才会长出第 2、3 块。

覆盖：
  A) 纯函数：`_section_aliases` / `_item_pairs`（含**反向断言**：项目时间必须判给 period 而不是 name）
  B) 真浏览器 + 本地 HTML 6 种形态：套 wrapper / 平铺 / 按钮点了不长块 / 按钮在块内部 /
     只有 1 条 / 页面没有代表字段（大 textarea）
  C) 源码结构断言（`_collect_fields` 复用 / 计数列表初始化顺序 / 摘要接线 / 上限 / 防误点）
  D) **证伪**：禁用 `_LIST_PATHS` → 复现「第 2、3 条项目无处可去」；清空 `PRIMARY_ALIASES` → 不瞎动

用法（cwd=backend）：<venv-python> smoke_repeat_section.py
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ["RESUME_CDP_ENDPOINT"] = "http://127.0.0.1:59999"   # 死端口：别碰真实浏览器

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


PROJECTS = [
    {"name": "AI 短剧分镜创作工作台", "role": "产品负责人", "period": "2026.2-2026.6",
     "desc": "设计剧情输入—人物场景资产—镜头拆解工作流"},
    {"name": "基于 OAK-D 多模态感知的智能家庭机器人「千寻」", "role": "产品预研与项目协调负责人",
     "period": "2025.2-2025.12", "desc": "主导基于 OAK-D 深度视觉的感知方案设计"},
    {"name": "智能疲劳驾驶监测系统（DMS）", "role": "产品预研与项目协调负责人",
     "period": "2024.2-2025.2", "desc": "设计端侧轻量推理加云侧模型迭代协同架构"},
]
PROFILE = {"name": "张三", "projects": PROJECTS}

# 一块里的四个子字段（label 用 for 关联，才能被 el.labels 读到）
FIELDS = ('<label for="pn{0}">项目名称</label><input id="pn{0}">'
          '<label for="pr{0}">担任角色</label><input id="pr{0}">'
          '<label for="pt{0}">项目时间</label><input id="pt{0}">'
          '<label for="pd{0}">项目描述</label><textarea id="pd{0}"></textarea>')

# 点「新增项目」→ 克隆第一个 .item、清值、重指 label（模拟真实站点 v-for 追加）
ADD_GROW_JS = """
<script>
let seq = 0;
const btn = document.getElementById('addBtn');
if (btn) btn.onclick = function () {
  seq++;
  const wrap = document.getElementById('items') || document.getElementById('sec');
  const tpl = document.querySelector('.item');
  const div = tpl.cloneNode(true);
  div.querySelectorAll('input,textarea').forEach(function (e, i) {
    e.value = ''; e.id = 'c' + seq + '_' + i;
  });
  div.querySelectorAll('label').forEach(function (l, i) {
    l.setAttribute('for', 'c' + seq + '_' + i);
  });
  wrap.appendChild(div);
};
</script>"""

# 点了但什么都不长（复现「按钮不生效」的站点）
ADD_NOOP_JS = """
<script>
const btn = document.getElementById('addBtn');
if (btn) btn.onclick = function () { window.__noopClicked = (window.__noopClicked || 0) + 1; };
</script>"""

# 「新增」按钮放在**块内部**（应被判为不是 section 入口，绝不能点它）
ADD_INSIDE_JS = """
<script>
const b = document.getElementById('addInside');
if (b) b.onclick = function () { window.__insideClicked = true; };
const s = document.getElementById('addBtn');
if (s) s.onclick = function () { window.__insideClicked = true; };
</script>"""


def page_html(mode="wrapper", add="grow", inside=False, big_textarea=False):
    """mode: wrapper（多一层 #items 容器，真实站点最常见）| flat（条目直接挂在 section 下）
    add: grow | noop | none ; inside: 是否在块内部也放一个「新增」按钮"""
    item = '<div class="item">%s</div>' % FIELDS.format(0)
    if inside:
        item = item[:-len("</div>")] + '<button id="addInside">+ 新增一行</button></div>'
    if big_textarea:
        # 「项目经历」整段就是一个大 textarea（老形态）→ 页面没有代表字段
        body = '<label for="pexp">项目经历</label><textarea id="pexp"></textarea>'
    elif mode == "wrapper":
        body = '<div id="items">%s</div>' % item
    else:
        body = item
    btn = "" if add == "none" else '<button id="addBtn">+ 新增项目</button>'
    js = {"grow": ADD_GROW_JS, "noop": ADD_NOOP_JS,
          "none": ADD_INSIDE_JS if inside else ""}.get(add, "")
    return ("<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
            "<div class='section' id='sec'><h3>项目经历</h3>%s%s</div>%s</body></html>"
            % (body, btn, js))


def run(pw, d, name, html, profile=None):
    """跑一个夹具，返回 (summary, elapsed, page, browser)。"""
    import filler
    p = os.path.join(d, name + ".html")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(html)
    b = pw.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
    pg = b.new_page()
    pg.goto("file://" + p, wait_until="domcontentloaded")
    notes = []
    t0 = time.time()
    try:
        s = filler.fill_page(pg, profile or PROFILE, note=notes.append)
    except Exception as e:                            # noqa: BLE001
        s = "EXC %s: %s" % (type(e).__name__, e)
    return s, time.time() - t0, pg, b, notes


def val(pg, sel):
    """读控件值；元素不存在时返回哨兵串。

    ⚠️ **必须给短超时**：Playwright 的 `input_value` 找不到元素会**等满默认 30s** 才抛，
    而证伪段恰恰要查「元素不存在」→ 不设超时整套自测被拖到 2 分钟以上（首跑实测 2m17s）。
    """
    try:
        return pg.input_value(sel, timeout=800)
    except Exception:                                 # noqa: BLE001
        return "<无此元素>"


def main():
    import filler

    # ---------------------------------------------------------------- A 纯函数
    print("=== A. 纯函数：section 别名 / 块内子字段分配 ===")
    sa_p = filler._section_aliases("projects")
    check("A1 projects 别名含 section 名与子字段名",
          "项目经历" in sa_p and "项目名称" in sa_p and "项目描述" in sa_p)
    sa_e = filler._section_aliases("experiences")
    check("A2 experiences 别名含「工作经历」「公司名称」",
          "工作经历" in sa_e and "公司名称" in sa_e)

    ctrls = [{"idx": 0, "label": "项目名称", "ph": "", "nm": "", "id": "pn0"},
             {"idx": 1, "label": "担任角色", "ph": "", "nm": "", "id": "pr0"},
             {"idx": 2, "label": "项目时间", "ph": "", "nm": "", "id": "pt0"},
             {"idx": 3, "label": "项目描述", "ph": "", "nm": "", "id": "pd0"}]
    pairs = filler._item_pairs("projects", ctrls)
    got = {k: f["label"] for k, f, _s in pairs}
    check("A3 四个子字段各就各位",
          got.get("name") == "项目名称" and got.get("role") == "担任角色"
          and got.get("period") == "项目时间" and got.get("desc") == "项目描述", str(got))
    check("A4 反向断言：「项目时间」必须判给 period（不能判给 name 的裸「项目」）",
          got.get("period") == "项目时间" and got.get("name") != "项目时间")
    check("A5 反向断言：「项目描述」判给 desc（不是 name）",
          got.get("desc") == "项目描述" and got.get("name") != "项目描述")
    check("A6 同 key 只分配一次（两个「项目描述」只吃一个）",
          len(filler._item_pairs("projects", ctrls + [
              {"idx": 9, "label": "项目描述", "ph": "", "nm": "", "id": "pd9"}])) == 4)
    check("A7 PRIMARY_ALIASES 不含裸「项目」（否则会把项目时间/描述也数成一块）",
          "项目" not in filler.PRIMARY_ALIASES["projects"]
          and "项目名称" in filler.PRIMARY_ALIASES["projects"])

    # ------------------------------------------------------------ B 真浏览器
    print("\n=== B. 真浏览器 + 本地 HTML ===")
    from playwright.sync_api import sync_playwright
    import tempfile
    with tempfile.TemporaryDirectory() as d, sync_playwright() as pw:
        # B1 套 wrapper（真实站点最常见）：3 条 → 应点 2 次「新增」→ 3 块各填 4 项
        s, el, pg, b, _n = run(pw, d, "b1", page_html("wrapper", "grow"))
        check("B1 摘要报「3 条 → 3 块（点新增 2 次）」",
              "可重复区块 projects：3 条 → 3 块" in s and "点「新增」2 次" in s, s[:160])
        check("B1 第 1 块填对",
              val(pg, "#pn0") == PROJECTS[0]["name"] and val(pg, "#pr0") == PROJECTS[0]["role"]
              and val(pg, "#pt0") == PROJECTS[0]["period"]
              and val(pg, "#pd0") == PROJECTS[0]["desc"],
              "%s|%s|%s" % (val(pg, "#pn0"), val(pg, "#pt0"), val(pg, "#pd0")))
        check("B1 第 2 块（新长出来的）填对",
              val(pg, "#c1_0") == PROJECTS[1]["name"] and val(pg, "#c1_2") == PROJECTS[1]["period"],
              "%s|%s" % (val(pg, "#c1_0"), val(pg, "#c1_2")))
        check("B1 第 3 块填对",
              val(pg, "#c2_0") == PROJECTS[2]["name"] and val(pg, "#c2_2") == PROJECTS[2]["period"],
              "%s|%s" % (val(pg, "#c2_0"), val(pg, "#c2_2")))
        check("B1 摘要含 12 项已填（3 块 × 4 子字段）", "已填 12 项" in s, s[:160])
        check("B1 耗时正常（< 20s）", el < 20, "%.1fs" % el)
        b.close()

        # B2 平铺（条目直接挂 section 下，按钮是兄弟）
        s, el, pg, b, _n = run(pw, d, "b2", page_html("flat", "grow"))
        check("B2 平铺形态也点 2 次、3 块填对",
              "3 条 → 3 块" in s and val(pg, "#c1_0") == PROJECTS[1]["name"]
              and val(pg, "#c2_0") == PROJECTS[2]["name"],
              "%s|%s|%s" % (s[:90], val(pg, "#c1_0"), val(pg, "#c2_0")))
        b.close()

        # B3 按钮点了不长块 → 收手、如实报「只补出 1 块」，且不超时
        s, el, pg, b, _n = run(pw, d, "b3", page_html("wrapper", "noop"))
        check("B3 按钮不生效时如实报「只补出 1 块」", "只补出 1 块" in s, s[:200])
        check("B3 按现有区块=1 块正常填", "3 条 → 1 块" in s and val(pg, "#pn0") == PROJECTS[0]["name"],
              s[:120])
        check("B3 不超时（< 20s）", el < 20, "%.1fs" % el)
        b.close()

        # B4「新增」按钮在块内部 → 绝不能点它（不是 section 入口）
        s, el, pg, b, _n = run(pw, d, "b4", page_html("wrapper", "none", inside=True))
        check("B4 块内的「新增一行」没被点", pg.evaluate("() => !!window.__insideClicked") is False)
        check("B4 不点新增（clicks=0）", "点「新增」0 次" in s, s[:160])
        b.close()

        # B5 只有 1 条项目 → 不点新增，也能把子字段填对
        s, el, pg, b, _n = run(pw, d, "b5", page_html("wrapper", "grow"),
                               profile={"name": "张三", "projects": [PROJECTS[0]]})
        check("B5 单条目：clicks=0 且填对子字段",
              "1 条 → 1 块" in s and "点「新增」0 次" in s
              and val(pg, "#pn0") == PROJECTS[0]["name"] and val(pg, "#pt0") == PROJECTS[0]["period"],
              s[:160])
        b.close()

        # B6 页面没有代表字段（整段一个大 textarea）→ 回落旧逻辑（不崩、把内容填进去）
        s, el, pg, b, _n = run(pw, d, "b6", page_html("wrapper", "grow", big_textarea=True))
        check("B6 无代表字段时回落旧逻辑（不报可重复区块）", "可重复区块" not in s, s[:160])
        check("B6 旧逻辑把 3 条项目拼进 textarea",
              all(p["name"] in val(pg, "#pexp") for p in PROJECTS), val(pg, "#pexp")[:80])
        b.close()

    # ------------------------------------------------------------ C 源码结构
    print("\n=== C. 源码结构断言 ===")
    src = open(os.path.join(HERE, "filler.py"), encoding="utf-8").read()
    fp = src[src.index("def fill_page("):src.index("def _playwright_fill(")]
    check("C1 fill_page 复用 _collect_fields（不再内联一份采集 JS）",
          "_collect_fields(page)" in fp and "data-filler-idx" not in fp)
    check("C2 计数列表初始化在可重复区块**之前**（顺序坑）",
          fp.index("filled, failed, uncertain = [], [], []") < fp.index("repeat_reports = []"))
    check("C3 摘要接了 repeat_reports", "repeat_reports" in fp and 'r["report"]' in fp)
    # T70：education 是 dict（扁平键是 education.school 这类**子路径**），只置 flat[_p] 清不掉；
    # 改按子路径清空 —— 意图不变：已按条目处理过的区块不许被一对一再填一遍。
    check("C4 处理完把该区块的路径清空（不让一对一再填一遍）",
          "_blank_section_flat(flat, _p" in fp and "flat[_p] = " not in fp)
    check("C5 新增点击有上限 _MAX_ADD_CLICKS", "clicks < _MAX_ADD_CLICKS" in src
          and "_MAX_ADD_CLICKS = 8" in src)
    check("C6 按钮落在块盒子内必须被拒（防误点块内「添加一行」）",
          "boxes.some(bx => bx.contains(cands[0]))" in src)
    check("C7 数不清块就放弃（no-primary-field）", "no-primary-field" in src)

    # ---------------------------------------------------------------- D 证伪
    print("\n=== D. 证伪：短路新逻辑必须复现用户报的 bug ===")
    with tempfile.TemporaryDirectory() as d, sync_playwright() as pw:
        keep = filler._LIST_PATHS
        filler._LIST_PATHS = ()            # 等价于「回到修复前」
        try:
            s, el, pg, b, _n = run(pw, d, "d1", page_html("wrapper", "grow"))
            check("D1 禁用可重复区块 → 一次「新增」都没点",
                  "点「新增」" not in s, s[:160])
            check("D1 第 2、3 条项目**无处可去**（复现「只会填写一个项目」）",
                  val(pg, "#c1_0") == "<无此元素>" and val(pg, "#c2_0") == "<无此元素>",
                  "c1=%s c2=%s" % (val(pg, "#c1_0"), val(pg, "#c2_0")))
            body = pg.content()
            check("D1 整页都找不到第 2、3 条项目的内容",
                  PROJECTS[1]["name"] not in body and PROJECTS[2]["name"] not in body)
            b.close()
        finally:
            filler._LIST_PATHS = keep

        keep_prim = dict(filler.PRIMARY_ALIASES)
        filler.PRIMARY_ALIASES["projects"] = []      # 数不清块 → 应整体放弃
        try:
            s, el, pg, b, _n = run(pw, d, "d2", page_html("wrapper", "grow"))
            check("D2 清空 PRIMARY_ALIASES → 不瞎点、也不报可重复区块",
                  "可重复区块" not in s and val(pg, "#c1_0") == "<无此元素>", s[:160])
            b.close()
        finally:
            filler.PRIMARY_ALIASES.update(keep_prim)

    print("\n汇总：PASS=%d FAIL=%d" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
