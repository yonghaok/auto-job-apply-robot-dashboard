# -*- coding: utf-8 -*-
"""T71 回归：文远知行 / MokaHR 投递页的四处真实缺陷（+ 一处顺带修掉的 role 缺失）。

用户原文（2026-09-24）：实习经历会填漏（潜渊之心最容易漏），重复填；就读时间会填错；
实习经历职责和内容会填错；项目经历内容会部分填入职责。

实测根因（CDP 接管真实页面、只填不提交）：
  A 区块代表字段里放的是**裸「公司」** → 申请信息区的「最近公司」（最近任职公司）也冒充一块
    → `experiences` nblocks 1→2：第 1 条（潜渊之心）被写进「最近公司」、真块拿到第 2 条、
    第 3 条（杭州旺慧）整条丢失 → 用户报的「填漏 + 重复填」。
  B 站点把字段标题写在**兄弟 div**（`<div class="title"><span>公司名称</span></div>`），
    label/aria-label 全空 + placeholder 是通用「内容 / 请选择」→ 字段认不出来
    （项目描述被判成「职责」、学历整列不填）。
  C 日期行判据「第一个含 ≥2 个年月框的祖先」在 MokaHR 的 **per-input wrapper** 上提前命中
    （每对 年+月 被一个 `span` 包住）→ 只拿到 2 个框 → 终点写不进去
    → 「就读时间」只填了起始年月。
  D 项目块的 `desc` 别名里「内容」与「职责」并列，同分时按 DOM 顺序 → 短「职责」栏抢到长文本
    → 用户报的「项目经历内容部分填入职责」。
  E 项目块里「担任角色」这一栏的标题就叫「职责」（单行 `input maxlength=255`）→ role 无别名命中
    → 一直空着（手里其实有「产品负责人」）。

本套件：
  A 纯函数（**真实 48 字段表**内联）—— A 的映射 + E 的 role/desc 分工
  B 别名护栏（强/弱分离 + 否定词）结构
  C 真浏览器：兄弟 div 标题兜底 + 年月框合成标签
  D 真浏览器：日期行取最外层（4 框）· 证伪内层（2 框）· 2 框行**不半填**
  E 真浏览器：区块计数（含「最近公司」陷阱）+ 证伪旧裸「公司」判据
  F 真浏览器：「新增」按钮 Playwright 点不动时退到 JS 点击
  G 真浏览器：端到端 `_fill_repeat_section`（3 条 → 3 块、值正确、不重复）
  H 源码结构

隔离：RESUME_* 指向临时目录；CDP 指向死端口（**绝不碰用户真实浏览器**）。
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="t71_")
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


# =========================================================================
# 真实字段表（文远知行投递页 `_t71_dump/02_fields.json`，48 个控件原样内联）
REAL = json.loads(r"""[{"tag":"input","type":"","idx":0,"label":"","ph":"","nm":"","id":"moka-version","opts":[]},{"tag":"input","type":"","idx":1,"label":"","ph":"输入职位关键字","nm":"","id":"","opts":[]},{"tag":"input","type":"","idx":2,"label":"","ph":"输入职位关键字","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":3,"label":"","ph":"推荐码","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":5,"label":"","ph":"姓名","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":6,"label":"","ph":"+86","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":7,"label":"","ph":"请输入手机号","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":8,"label":"","ph":"邮箱","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":9,"label":"","ph":"请选择","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":10,"label":"","ph":"最近公司","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":11,"label":"","ph":"请输入最近毕业专业","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":12,"label":"","ph":"到岗时间","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":13,"label":"起止时间","ph":"年","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":14,"label":"起止时间","ph":"月","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":15,"label":"","ph":"期望城市","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":16,"label":"起止时间","ph":"年","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":17,"label":"起止时间","ph":"月","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":18,"label":"起止时间","ph":"年","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":19,"label":"起止时间","ph":"月","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":20,"label":"","ph":"请输入就读学校","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":21,"label":"","ph":"请输入专业名称","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":22,"label":"","ph":"请选择","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":23,"label":"起止时间","ph":"年","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":24,"label":"起止时间","ph":"月","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":25,"label":"起止时间","ph":"年","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":26,"label":"起止时间","ph":"月","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":28,"label":"","ph":"公司名称","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":29,"label":"","ph":"职位名称","nm":"","id":"","opts":[]},{"tag":"textarea","type":"","idx":30,"label":"","ph":"内容","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":31,"label":"起止时间","ph":"年","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":32,"label":"起止时间","ph":"月","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":33,"label":"起止时间","ph":"年","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":34,"label":"起止时间","ph":"月","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":36,"label":"","ph":"项目名称","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":37,"label":"","ph":"职责","nm":"","id":"","opts":[]},{"tag":"textarea","type":"","idx":38,"label":"","ph":"内容","nm":"","id":"","opts":[]},{"tag":"textarea","type":"","idx":39,"label":"","ph":"项目中职责","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":40,"label":"","ph":"语言类型","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":41,"label":"","ph":"请选择","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":42,"label":"","ph":"请选择","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":43,"label":"","ph":"请选择","nm":"","id":"","opts":[]},{"tag":"textarea","type":"","idx":44,"label":"","ph":"简介","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":45,"label":"起止时间","ph":"年","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":46,"label":"起止时间","ph":"月","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":47,"label":"","ph":"奖项名称","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":50,"label":"+86","ph":"","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":51,"label":"","ph":"请输入手机号","nm":"","id":"","opts":[]},{"tag":"input","type":"text","idx":52,"label":"","ph":"请输入验证码","nm":"","id":"","opts":[]}]""")

# 采集期若带上「兄弟 div 标题」（T71 新行为），label 会长成这些值
TITLES = {20: "学校名称", 21: "专业名称", 22: "学历", 28: "公司名称", 29: "职位名称",
          30: "工作职责", 36: "项目名称", 37: "职责", 38: "项目描述", 39: "项目中职责",
          44: "自我描述", 47: "奖项名称"}


def by_idx(ids):
    m = {f["idx"]: f for f in REAL}
    return [m[i] for i in ids]


def keys_of(pairs):
    return {k: (f.get("label") or f.get("ph")) for k, f, _s in pairs}


# =========================================================================
print("== A. 纯函数：真实字段表上的区块内配对（文远知行实测字段）==")

# 实习经历块 = 4 个年月框 + 公司名称 / 职位名称 / 工作职责(textarea)
pk = keys_of(filler._item_pairs("experiences", by_idx([23, 24, 25, 26, 28, 29, 30])))
ck("A1 company → 公司名称", pk.get("company") == "公司名称", pk)
ck("A2 role → 职位名称", pk.get("role") == "职位名称", pk)
ck("A3 highlights → 「内容」(工作职责)", pk.get("highlights") == "内容", pk)
ck("A4 period → 年月框", pk.get("period") in ("年", "月", "起止时间"), pk)

# 教育背景块 = 4 个年月框 + 就读学校 / 专业名称 / 学历下拉
pk = keys_of(filler._item_pairs("education", by_idx([16, 17, 18, 19, 20, 21, 22])))
ck("A5 school → 请输入就读学校", pk.get("school") == "请输入就读学校", pk)
ck("A6 major → 请输入专业名称", pk.get("major") == "请输入专业名称", pk)
ck("A7 period → 年月框", pk.get("period") in ("年", "月", "起止时间"), pk)

# 项目经历块 = 4 个年月框 + 项目名称 / 职责(短 input) / 内容(textarea: 项目描述) / 项目中职责
PJ = [31, 32, 33, 34, 36, 37, 38, 39]
pk = keys_of(filler._item_pairs("projects", by_idx(PJ)))
ck("A8 name → 项目名称", pk.get("name") == "项目名称", pk)
ck("A9 desc → 「内容」= 项目描述（**不是**「职责」）", pk.get("desc") == "内容", pk)
ck("A10 role → 「职责」（单行短栏）", pk.get("role") == "职责", pk)
ck("A11 period → 年月框", pk.get("period") in ("年", "月", "起止时间"), pk)
ck("A12 反向：「项目中职责」无数据源 → **不许**被 desc 抢去",
   (pk.get("desc") != "项目中职责") and (pk.get("role") != "项目中职责"), pk)

# 加上标题兜底后的 label（T71 采集期新行为）→ 映射必须保持一致
REAL_T = [dict(f, label=TITLES.get(f["idx"], f.get("label") or "")) for f in REAL]
m = {f["idx"]: f for f in REAL_T}


def by_idx_t(ids):
    return [m[i] for i in ids]


pk = keys_of(filler._item_pairs("projects", by_idx_t(PJ)))
ck("A13 带标题兜底后 desc 仍配「项目描述」", pk.get("desc") == "项目描述", pk)
ck("A14 带标题兜底后 role 仍配「职责」", pk.get("role") == "职责", pk)
pk = keys_of(filler._item_pairs("education", by_idx_t([16, 17, 18, 19, 20, 21, 22])))
ck("A15 带标题兜底后 degree 能配上「学历」（原来整列不填）", pk.get("degree") == "学历", pk)
ck("A16 带标题兜底后 school 仍配「学校名称」（不许被「学校类别」类抢）",
   pk.get("school") == "学校名称", pk)
pk = keys_of(filler._item_pairs("experiences", by_idx_t([23, 24, 25, 26, 28, 29, 30])))
ck("A17 带标题兜底后 highlights 配「工作职责」", pk.get("highlights") == "工作职责", pk)

# =========================================================================
print("== B. 别名护栏：区块代表字段必须强/弱分离（否则「最近公司」冒充一块）==")
src = open(os.path.join(HERE, "filler.py"), "rb").read().decode("utf-8")
ck("B1 experiences 强别名里**没有**裸「公司」",
   '"公司"' not in filler.PRIMARY_ALIASES["experiences"], filler.PRIMARY_ALIASES["experiences"])
ck("B2 弱别名层**已整体移除**（T71c：弱别名会让大文本框被当成可重复区块，"
   "宁可不数也不乱数 → 回到 T68「数不准就放弃」契约）",
   not hasattr(filler, "PRIMARY_WEAK_ALIASES"))
ck("B3 experiences 否定词含「最近」（挡住「最近任职公司」）",
   "最近" in filler.PRIMARY_REJECT["experiences"])
ck("B4 education 否定词含「类别/排名/人数」", {"类别", "排名", "人数"} <=
   set(filler.PRIMARY_REJECT["education"]))
ck("B5 projects 强别名含「项目名称」", "项目名称" in filler.PRIMARY_ALIASES["projects"])
ck("B6 project desc 的「内容」排在「职责」**之前**（同分靠下标决胜）",
   filler.ITEM_ALIASES["projects"]["desc"].index("内容") <
   filler.ITEM_ALIASES["projects"]["desc"].index("职责"))
ck("B7 project role 补上了「职责」（E 缺陷）",
   "职责" in filler.ITEM_ALIASES["projects"]["role"])
ck("B8 role **没有**认领「项目中职责」（那栏长文本无数据源，别乱填）",
   "项目中职责" not in filler.ITEM_ALIASES["projects"]["role"])
ck("B9 日期行判据取**最外层**（注释里钉住了 MokaHR 的 per-input wrapper 教训）",
   "继续往上找更外层" in src)
ck("B10 兄弟 div 标题兜底会**剥掉首尾装饰字符**（T71c：真实站点标题写作「就读时间 -」"
   "「* 公司名称」，不剥掉就永远匹配不上）",
   "去掉**首尾**的装饰字符" in src)

# =========================================================================
# ---------------- 合成页：忠实复刻 MokaHR 的 DOM 层级 ----------------
def _input(ph, extra=""):
    return ('<div class="item-half"><div class="sd-Tooltip"><div class="sd-Dropdown">'
            '<label class="sd-Input-container"><span class="disp"></span>'
            '<input type="text" placeholder="%s"%s></label><span></span>'
            '</div></div></div>' % (ph, extra))


def _pair():
    return _input("年") + _input("月")


def datefield(nbox=4, till=False):
    """MokaHR 真实层级：input → label → .sd-Dropdown → .sd-Tooltip → .item-half
    → span（**每对 年+月 一个 span**）→ .month-range-select → .ctrl"""
    till_html = ('<label class="till"><input type="checkbox"> 至今</label>') if till else ''
    if nbox == 2:
        body = '<span>%s</span>' % _pair()
    else:
        body = ('<span>%s</span><span class="sep">-<br></span><span>%s</span>'
                % (_pair(), _pair()))
    return ('<div class="month-range-select date_info"><label class="label-x"></label>'
            '%s%s</div>' % (body, till_html))


def field(title, ctrl, cls=""):
    """标题写在**兄弟 div** 里（label 关联为空、aria-label 全空）—— 复刻真实站点。"""
    return ('<div class="apply-field %s"><div class="title"><span>%s</span></div>'
            '<div class="ctrl">%s</div></div>' % (cls, title, ctrl))


def inp(ph, extra=""):
    return ('<label class="sd-Input-container"><input type="text" placeholder="%s"%s>'
            '</label>' % (ph, extra))


def ta(ph):
    return ('<label class="sd-Textarea-container"><textarea placeholder="%s"></textarea>'
            '</label>' % ph)


ITEM_EXP = (field("公司名称", inp("公司名称"))
            + field("职位名称", inp("职位名称"))
            + field("工作职责", ta("内容"))
            + field("在职时间", datefield(4, till=True), "date_info"))

ITEM_EDU = (field("就读时间", datefield(4), "date_info")
            + field("学校名称", inp("请输入就读学校"))
            + field("专业名称", inp("请输入专业名称"))
            + field("学历", '<label><select><option value="">请选择</option>'
                            '<option value="本科">本科</option>'
                            '<option value="硕士">硕士</option></select></label>'))

ITEM_PJ = (field("项目名称", inp("项目名称"))
           + field("职责", inp("职责", ' maxlength="255"'))
           + field("项目描述", ta("内容"))
           + field("项目中职责", ta("项目中职责"))
           + field("项目时间", datefield(4), "date_info"))

# 申请信息区：**不是**可重复区块，但含两个陷阱
#   · 「最近公司」——裸「公司」判据会让它冒充一块（缺陷 A）
#   · 「毕业时间」= **只有 2 个年月框** —— 拿它就填 = 半截日期（缺陷 C）
NOISE = ('<div id="sec-apply">'
         + field("最近公司", inp("最近公司"))
         + field("最近毕业专业", inp("请输入最近毕业专业"))
         + field("毕业时间", datefield(2), "date_info")
         + '</div>')

GROW_JS = """
<script>
function __grow(sid) {
  return function () {
    const wrap = document.getElementById('items-' + sid);
    const tpl = wrap.querySelector('.item');
    const div = tpl.cloneNode(true);
    div.querySelectorAll('input').forEach(function (e) {
      if (e.type === 'checkbox') { e.checked = false; } else { e.value = ''; }
    });
    div.querySelectorAll('textarea').forEach(function (e) { e.value = ''; });
    div.querySelectorAll('select').forEach(function (e) { e.selectedIndex = 0; });
    wrap.appendChild(div);
  };
}
document.getElementById('addExp').onclick = __grow('exp');
document.getElementById('addEdu').onclick = __grow('edu');
document.getElementById('addPj').onclick = __grow('pj');
</script>"""


def section(sid, title, item, add_id):
    return ('<div class="section" id="sec-%s"><h3>%s</h3>'
            '<div id="items-%s"><div class="item">%s</div></div>'
            '<button id="%s">+ 新增</button></div>'
            % (sid, title, sid, item, add_id))


FIXTURE = ("<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
           + NOISE
           + section("exp", "实习经历", ITEM_EXP, "addExp")
           + section("edu", "教育背景", ITEM_EDU, "addEdu")
           + section("pj", "项目经历", ITEM_PJ, "addPj")
           + GROW_JS + "</body></html>")

# Playwright 点不动的「新增」按钮（display:none）→ 只有 JS 原生 click 能生效
STUCK_HTML = ("<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
              "<div id='sec'><div id='items'><div class='item'>"
              + field("项目名称", inp("项目名称"))
              + "</div></div>"
              "<button id='stuckBtn' style='display:none' onclick=\"window.__clicks="
              "(window.__clicks||0)+1\">+ 新增</button></div></body></html>")

FF = os.path.join(_TMP, "mokahr.html")
open(FF, "w", encoding="utf-8").write(FIXTURE)
SF = os.path.join(_TMP, "stuck.html")
open(SF, "w", encoding="utf-8").write(STUCK_HTML)

IN_SEC_JS = """(a) => {
  const e = document.querySelector('[data-filler-idx="' + a.idx + '"]');
  if (!e) return false;
  let r = e;
  while (r) { if (r.id === 'sec-' + a.sid) return true; r = r.parentElement; }
  return false;
}"""
SEC_FIELDS_JS = """(sid) => {
  const root = document.getElementById('sec-' + sid);
  if (!root) return [];
  const out = [];
  root.querySelectorAll('.apply-field').forEach(fd => {
    const t = fd.querySelector('.title');
    const title = t ? ((t.innerText || '') + '').trim() : '';
    fd.querySelectorAll('input,textarea,select').forEach(e => {
      if (e.type === 'checkbox') return;
      out.push({title: title, tag: e.tagName.toLowerCase(), value: (e.value || '').trim()});
    });
  });
  return out;
}"""
SEC_NUMS_JS = """(sid) => {
  const root = document.getElementById('sec-' + sid);
  if (!root) return [];
  return Array.from(root.querySelectorAll('input,textarea,select'))
      .filter(e => e.type !== 'checkbox')
      .map(e => (e.value || '').trim());
}"""


def in_sec(pg, f, sid):
    return bool(pg.evaluate(IN_SEC_JS, {"idx": f["idx"], "sid": sid}))


def pick(pg, fields, sid, ph, nth=0):
    hits = [f for f in fields if (f.get("ph") or "") == ph and in_sec(pg, f, sid)]
    return hits[nth] if len(hits) > nth else None


with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox"])
    page = b.new_page()
    page.goto("file:///" + FF.replace("\\", "/"), wait_until="domcontentloaded")
    page.wait_for_timeout(200)

    fields = filler._collect_fields(page)
    byi = {f["idx"]: f for f in fields}

    print("== C. 兄弟 div 标题兜底 + 年月框合成标签 ==")
    f_exp = pick(page, fields, "exp", "内容")
    ck("C1 「工作职责」textarea 的 label 取到了兄弟标题",
       (f_exp or {}).get("label") == "工作职责", (f_exp or {}).get("label"))
    f_pj = pick(page, fields, "pj", "项目中职责")
    ck("C2 「项目中职责」的 label 取到了", (f_pj or {}).get("label") == "项目中职责",
       (f_pj or {}).get("label"))
    f_deg = [f for f in fields if f["tag"] == "select" and in_sec(page, f, "edu")]
    ck("C3 「学历」select 的 label 取到了", (f_deg[0].get("label") if f_deg else None) == "学历",
       f_deg[0].get("label") if f_deg else None)
    f_com = pick(page, fields, "exp", "公司名称")
    ck("C4 「公司名称」的 label 取到了", (f_com or {}).get("label") == "公司名称",
       (f_com or {}).get("label"))
    ym = [f for f in fields if (f.get("ph") or "") in ("年", "月")]
    ck("C5 年月框共 14 个（噪音 2 + 教育 4 + 实习 4 + 项目 4）",
       len(ym) == 14, len(ym))
    ck("C6 年月框被补了合成标签「起止时间」（真实页 48 字段表里也是这个值）",
       all((f.get("label") or "") == "起止时间" for f in ym),
       [f.get("label") for f in ym[:4]])
    # 反向：装饰文本（分隔符「-」）绝不能被当成标题
    ck("C7 反向：分隔符「-」没被当成标题（label 不为「-」）",
       all((f.get("label") or "").strip() != "-" for f in fields))

    print("== D. 日期行：取最外层 4 框 / 证伪内层 2 框 / 不半填 ==")
    edu_ym = pick(page, fields, "edu", "年")
    ck("D1 找到教育背景块里的日期框", edu_ym is not None)
    info = page.evaluate(filler.DATE_RANGE_JS, str(edu_ym["idx"])) if edu_ym else None
    ck("D2 4 框行判出 n=4（取到最外层，不是只装 年+月 的那个 span）",
       (info or {}).get("n") == 4, info)
    exp_ym = pick(page, fields, "exp", "年")
    info2 = page.evaluate(filler.DATE_RANGE_JS, str(exp_ym["idx"])) if exp_ym else None
    ck("D3 实习行（带「至今」）同样判出 n=4 且识别到勾选框",
       (info2 or {}).get("n") == 4 and (info2 or {}).get("till") == 1, info2)
    # 证伪：旧判据（第一个含 ≥2 个年月框的祖先）在真实层级上只会拿到 2 个框
    INNER_COUNT_JS = """(idx) => {
      const el = document.querySelector('[data-filler-idx="' + idx + '"]');
      const isYM = e => e.tagName === 'INPUT' &&
            /^(年|月|year|month|yyyy|mm)$/i.test((e.getAttribute('placeholder')||'').trim());
      let r = el, k = 0;
      while (r && k < 6) {
        const n = Array.from(r.querySelectorAll('input')).filter(isYM).length;
        if (n >= 2) return n;
        r = r.parentElement; k++;
      }
      return 0;
    }"""
    ck("D4 证伪：旧判据（第一个 ≥2 框的祖先）在同一控件上只拿到 2 个框"
       "（否则这次收紧是多余的）",
       page.evaluate(INNER_COUNT_JS, str(edu_ym["idx"])) == 2,
       page.evaluate(INNER_COUNT_JS, str(edu_ym["idx"])))
    # 真填 4 框
    ok, how = filler._fill_date_range(page, edu_ym, "2023.9-2027.6")
    ck("D5 就读时间整段填成功", ok is True, how)
    got = [v for v in page.evaluate(SEC_NUMS_JS, "edu") if v][:4]
    ck("D6 4 个框都进了值（2023/09/2027/06）", got == ["2023", "09", "2027", "06"], got)
    ck("D7 反向：不是只填了起点（老 bug 会停在 2023/09）", len(got) == 4, got)
    # 2 框行（申请信息区的「毕业时间」）→ 预检必须拦住，一个都不写
    f2 = pick(page, fields, "apply", "年")
    ok2, how2 = filler._fill_date_range(page, f2, "2023.9-2027.6")
    ck("D8 2 框行装不下 4 段 → 明确失败（不是静默半填）", ok2 is False, how2)
    ck("D9 失败原因写明「装不下」与「未写入任何值」",
       "装不下" in (how2 or "") and "未写入" in (how2 or ""), how2)
    wrote = [v for v in page.evaluate(SEC_NUMS_JS, "apply") if v]
    ck("D10 **一个值都没写进去**（防「前半截留下」）", wrote == [], wrote)
    # 反向：2 框行 + 只要求 1 段（无终点）→ 应当允许并成功
    f3 = pick(page, fields, "apply", "年")
    ok3, how3 = filler._fill_date_range(page, f3, "2027.6")
    ck("D11 反向：只有起点（2 框够用）时必须**允许**填", ok3 is True, how3)
    # 非区间控件必须明确返回原因，供调用方回落
    ok4, how4 = filler._fill_date_range(page, f_com, "2023.9-2027.6")
    ck("D12 非区间控件返回明确原因（供回落）",
       ok4 is False and how4 == "不是年月区间控件", how4)

    print("== E. 区块计数：「最近公司」不许冒充一块（缺陷 A）==")
    page.goto("file:///" + FF.replace("\\", "/"), wait_until="domcontentloaded")
    page.wait_for_timeout(200)
    res, _fl = filler._analyze_repeat(page, "experiences")
    ck("E1 申请信息区有「最近公司」时 experiences 仍然后 nblocks=1",
       (res or {}).get("nblocks") == 1, res)
    f_near = [f for f in _fl if (f.get("ph") or "") == "最近公司"]
    ck("E2 找得到「最近公司」这个控件（证伪需要它真的在页面上）", len(f_near) == 1, f_near)
    ck("E3 「最近公司」**不在**任何一块里（第 1 条经历不会写进它）",
       bool(f_near) and all(f_near[0]["idx"] not in blk
                            for blk in (res or {}).get("blocks", [])),
       (res or {}).get("blocks"))
    res_e, _fl2 = filler._analyze_repeat(page, "education")
    ck("E4 education nblocks=1（「最近毕业专业」没冒充）",
       (res_e or {}).get("nblocks") == 1, res_e)
    res_p, _fl3 = filler._analyze_repeat(page, "projects")
    ck("E5 projects nblocks=1", (res_p or {}).get("nblocks") == 1, res_p)
    # 证伪：旧判据（代表字段含裸「公司」，且无否定词）**确实会**数出 2 块
    LOOSE_PRIM_JS = """(prim) => {
      const low = a => (a || []).map(s => String(s).toLowerCase());
      const al = low(prim);
      return Array.from(document.querySelectorAll('[data-filler-idx]')).filter(e => {
        const s = ((e.getAttribute('placeholder') || '') + ' ' +
                   (e.getAttribute('name') || '') + ' ' +
                   (e.getAttribute('id') || '')).toLowerCase();
        return al.some(a => s.indexOf(a) >= 0);
      }).length;
    }"""
    n_loose = page.evaluate(LOOSE_PRIM_JS, ["公司名称", "公司"])
    ck("E6 证伪：旧裸「公司」判据在同一页上数出 2 个（多出来的就是「最近公司」）",
       n_loose == 2, n_loose)
    ck("E7 证伪对照：强别名判据只数出 1 个",
       page.evaluate(LOOSE_PRIM_JS, filler.PRIMARY_ALIASES["experiences"]) == 1)

    print("== F. 「新增」按钮 Playwright 点不动 → 退到 JS 原生 click ==")
    pg2 = b.new_page()
    pg2.goto("file:///" + SF.replace("\\", "/"), wait_until="domcontentloaded")
    pg2.wait_for_timeout(150)
    okc, howc = filler._click_add(pg2, "#stuckBtn")
    ck("F1 三种方式里最终成功", okc is True, howc)
    ck("F2 用的就是 JS 兜底（真实页面实测也是这条救回来的）", howc == "js-click", howc)
    ck("F3 按钮的 onclick 真的被触发了", pg2.evaluate("() => window.__clicks || 0") == 1)
    pg2.close()

    print("== G. 端到端：3 条实习经历 → 3 块且值正确（不填漏、不重复）==")
    PROFILE = json.load(open(os.path.join(HERE, "profile.json"), encoding="utf-8"))
    page.goto("file:///" + FF.replace("\\", "/"), wait_until="domcontentloaded")
    page.wait_for_timeout(200)
    r = filler._fill_repeat_section(page, PROFILE, "experiences")
    ck("G1 返回报告", bool(r), r)
    rep = (r or {}).get("report", "")
    ck("G2 3 条 → 3 块", "3 条 → 3 块" in rep, rep)
    ck("G3 无失败项", not (r or {}).get("failed"), (r or {}).get("failed"))
    ev = page.evaluate(SEC_NUMS_JS, "exp")
    for comp in ("杭州潜渊之心", "特斯拉", "杭州旺慧"):
        ck("G4 实习经历含 %s" % comp, comp in ev, ev)
    ck("G5 「潜渊之心」只出现 1 次（不重复填）", ev.count("杭州潜渊之心") == 1,
       ev.count("杭州潜渊之心"))
    ck("G6 3 段日期都填了（每段 4 个框 → 12 个数字）",
       len([v for v in ev if v.isdigit()]) == 12, [v for v in ev if v.isdigit()])
    ck("G7 申请信息区的「最近公司」**没有被写**（第 1 条没跑错地方）",
       "杭州潜渊之心" not in page.evaluate(SEC_NUMS_JS, "apply"),
       page.evaluate(SEC_NUMS_JS, "apply"))

    r = filler._fill_repeat_section(page, PROFILE, "projects")
    rep = (r or {}).get("report", "")
    ck("G8 项目 3 条 → 3 块且无失败", "3 条 → 3 块" in rep and not (r or {}).get("failed"), r)
    filled = " ".join((r or {}).get("filled") or [])
    ck("G9 filled 里出现 role→职责（E 缺陷已修）", "role→职责" in filled, filled)
    ck("G10 filled 里出现 desc→项目描述（D 缺陷已修）", "desc→项目描述" in filled, filled)
    pf = page.evaluate(SEC_FIELDS_JS, "pj")
    pv_t = {}
    for it in pf:
        pv_t.setdefault(it["title"], []).append(it["value"])

    def tval(title):
        return pv_t.get(title, [])

    ck("G11 「职责」栏是**短角色名**（产品负责人…），不是长描述",
       len(tval("职责")) == 3 and all(len(v) < 20 for v in tval("职责")), tval("职责"))
    ck("G12 「项目描述」栏是长文本（原来被塞进「职责」的那份内容）",
       len(tval("项目描述")) == 3 and all(len(v) > 60 for v in tval("项目描述")),
       [len(v) for v in tval("项目描述")])
    ck("G13 「项目中职责」保持为空（无数据源 → 不编造）",
       len(tval("项目中职责")) == 3 and all(v == "" for v in tval("项目中职责")),
       tval("项目中职责"))

    r = filler._fill_repeat_section(page, PROFILE, "education")
    rep = (r or {}).get("report", "")
    ck("G14 教育 1 条 → 1 块且无失败", "1 条 → 1 块" in rep and not (r or {}).get("failed"), r)
    ev2 = page.evaluate(SEC_NUMS_JS, "edu")
    ef = page.evaluate(SEC_FIELDS_JS, "edu")
    ev_t = {}
    for it in ef:
        ev_t.setdefault(it["title"], []).append(it["value"])
    ck("G15 学校 / 专业 / 学历 全部**按标题**填对（学历原来整列不填）",
       ev_t.get("学校名称") == ["浙江工商大学"] and ev_t.get("专业名称") == ["电子信息工程"]
       and ev_t.get("学历") == ["本科"],
       {k: v for k, v in ev_t.items() if k})
    ck("G16 就读时间 4 个框齐全（2023/09/2027/06）",
       [v for v in ev2 if v.isdigit()] == ["2023", "09", "2027", "06"],
       [v for v in ev2 if v.isdigit()])

    b.close()

# =========================================================================
print("== H. 源码结构断言（防后人改回去）==")
ck("H1 采集期有兄弟 div 标题兜底（COLLECT_JS）", "标题兜底（T71）" in src)
ck("H2 ANALYZE_JS 的 nearTitle 与 COLLECT_JS **两份都要有**",
   src.count("nearTitle") >= 3 or src.count("标题在**兄弟 div**") >= 1)
ck("H3 _score_detail 存在（同分按下标决胜）", "def _score_detail" in src)
ck("H4 _item_pairs 也按别名下标决胜", "-x[0], x[1], -len(ITEM_ALIASES[path]" in src)
ck("H5 _fill_date_range 有「可行性预检」（不半填）", "max(need) >= n_box" in src)
ck("H6 _click_add 三种方式（click / click+scroll / js-click）",
   '"click+scroll"' in src and '"js-click"' in src and "def _click_add" in src)
ck("H7 _fill_repeat_section 用 _click_add（不再裸 page.click）",
   "_click_add(page, res[\"addSel\"])" in src)

print("\n===== T71（文远知行 / MokaHR）：%d/%d %s =====" %
      (N[0] - len(BAD), N[0], "PASSED" if not BAD else ("FAILED -> %s" % BAD)))
sys.exit(1 if BAD else 0)
