# -*- coding: utf-8 -*-
"""T70 回归：① 年月区间控件（那个「日期选择栏」）② 别名抢字段导致「填错」。

背景（全志科技 / MokaHR 投递页实测）：
  · 日期 = `date_info` 行里的**4 个独立文本框**（年/月 - 年/月）+ 独立「至今」勾选，
    输入框**不是 readonly**，`page.fill` 直接生效。但它们只有 placeholder 年/月，
    **没有任何可匹配文案** → `_hay` 里没有 `period` 别名 → `_item_pairs` 配不上
    → 日期栏**一个字都没填**（用户报的「日期选择栏」）。
  · 「毕业院校所在专业成绩排名」hay 含「毕业院校」(len4)，而真正的「学校名称」
    只含「学校」(len2) → `education.school` 被抢到**成绩排名下拉**上（选项是 TOP 5%…）
    → 用户报的「很多信息填写不正确」。

本套件钉住：A `parse_period` · B 否定规则（含**反向断言**：合法字段不许被误杀）·
C section 条目归一 · D 真浏览器：合成标签让日期框配上 `period` · 区间填写与回读 ·
E 证伪（去掉合成标签 = 复现「配不上、不填」）· F 源码结构。

隔离：`RESUME_PROFILES`/`RESUME_STATE` 指向临时目录，CDP 指向死端口。
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="t70_")
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
print("== A. parse_period：把 period 原文解析成起止年月 ==")
CASES = [
    # (原文, 起点, 终点, 至今)  —— 前 4 条是 profile.json 里的**真实原文**
    ("2026.07 – 2026.09", (2026, 7), (2026, 9), False),
    ("2025.09 - 2026.03", (2025, 9), (2026, 3), False),
    ("2024.06 - 2024.12", (2024, 6), (2024, 12), False),
    ("2023.9-2027.6", (2023, 9), (2027, 6), False),
    ("2026.2-2026.6", (2026, 2), (2026, 6), False),
    ("2024.02-2025.02", (2024, 2), (2025, 2), False),
    ("2024-02 ~ 2025-02", (2024, 2), (2025, 2), False),
    ("2026年7月-2026年9月", (2026, 7), (2026, 9), False),
    ("2023.09至今", (2023, 9), None, True),
    ("2023.09 - 至今", (2023, 9), None, True),
    ("2027", (2027, None), None, False),
    ("2026.5", (2026, 5), None, False),
]
for raw, want_s, want_e, want_on in CASES:
    got = filler.parse_period(raw)
    ok = bool(got) and got[0] == want_s and got[1] == want_e and got[2] is want_on
    ck("A parse %r → %s" % (raw, want_s), ok, got)
ck("A 空串/None → None", filler.parse_period("") is None and filler.parse_period(None) is None)
ck("A 非时间文本 → None", filler.parse_period("负责产品设计") is None,
   filler.parse_period("负责产品设计"))
ck("A 反向：不许把「2026.07」解析出终点", (filler.parse_period("2026.07") or (None,))[1] is None)

# =========================================================================
print("== B. 否定规则：别名子串匹配不许抢字段 ==")
# label 用实测原文（全志/MokaHR 页面）
LBL_RANK = "毕业院校所在专业成绩排名"
LBL_CNT = "毕业院校所在专业人数（数字）"
LBL_CAT = "最高学历学校类别"
LBL_SCHOOL_OK = "学校名称"
LBL_MAJOR_OK = "专业名称"
LBL_DEG_OK = "最高学历"


def sc(path, label, ph=""):
    return filler._score(path, {"label": label, "ph": ph, "nm": "", "id": ""})


ck("B1 成绩排名**不许**接 school", sc("education.school", LBL_RANK) == 0,
   sc("education.school", LBL_RANK))
ck("B2 专业人数**不许**接 school", sc("education.school", LBL_CNT) == 0)
ck("B3 成绩排名**不许**接 major", sc("education.major", LBL_RANK) == 0)
ck("B4 学校类别**不许**接 degree", sc("education.degree", LBL_CAT) == 0)
ck("B5 专业人数**不许**接 grad_year", sc("education.grad_year", LBL_CNT) == 0)
# ---- 反向断言：合法字段必须照样命中（护栏不许过度收紧）----
ck("B6 反向：学校名称仍命中 school", sc("education.school", LBL_SCHOOL_OK) > 0,
   sc("education.school", LBL_SCHOOL_OK))
ck("B7 反向：专业名称仍命中 major", sc("education.major", LBL_MAJOR_OK) > 0)
ck("B8 反向：最高学历仍命中 degree", sc("education.degree", LBL_DEG_OK) > 0)
ck("B9 反向：就读时间命中 education.period",
   sc("education.period", "就读时间") > 0)
ck("B10 反向：只有 label 含禁用词才拦（空 label 不拦）",
   sc("education.school", "") == 0 and sc("education.school", LBL_SCHOOL_OK) > 0)

# =========================================================================
print("== C. section 条目归一：education 是 dict，不是 list ==")
ck("C1 list 型照旧", len(filler._section_items({"experiences": [{"a": 1}, "x"]}, "experiences")) == 1)
ck("C2 education(dict) → 1 条", len(filler._section_items({"education": {"school": "S"}}, "education")) == 1)
ck("C3 education 空 dict → 0 条", filler._section_items({"education": {}}, "education") == [])
ck("C4 缺失 → 0 条", filler._section_items({}, "education") == [])
flat = {"education.school": "浙江工商大学", "education.major": "电子信息", "name": "张"}
filler._blank_section_flat(flat, "education", ["school", "major"])
ck("C5 清空的是**子路径**", flat["education.school"] == "" and flat["education.major"] == "")
ck("C6 不误伤别的路径", flat["name"] == "张")

# =========================================================================
FIXTURE = """<!doctype html><html><head><meta charset="utf-8"><style>
body{font:14px sans-serif;padding:8px} input,textarea{display:inline-block;width:110px;height:26px}
button{padding:6px 10px}</style></head><body>
<div class="apply-fields-BzcXI4i2Pm">
  <div class="title">申请信息</div>
  <div class="apply-field-Q2iJ7AtQGX string_info-UOJxKN5mtC"><div>学校名称</div>
     <input type="text" placeholder="请输入就读学校"></div>
  <div class="apply-field-Q2iJ7AtQGX select_info-zjdod05hST"><div>毕业院校所在专业成绩排名</div>
     <input type="text" placeholder="请选择"></div>
  <div class="apply-field-Q2iJ7AtQGX select_info-zjdod05hST"><div>最高学历学校类别</div>
     <input type="text" placeholder="请选择"></div>
  <div class="apply-fields-BzcXI4i2Pm"><div class="title">教育背景</div>
    <div class="multi-NTCDdF2lci">
      <div class="apply-field-Q2iJ7AtQGX date_info-nCj8tT_zjw">
        <div class="month-range-select date_info wrapper-Wil">
          <span>就读时间</span>
          <input type="text" placeholder="年"><input type="text" placeholder="月">
          <span>-</span>
          <input type="text" placeholder="年"><input type="text" placeholder="月">
        </div>
      </div>
      <input type="text" placeholder="请输入就读学校">
      <input type="text" placeholder="请输入专业名称">
      <div class="apply-field-Q2iJ7AtQGX select_info-zjdod05hST"><div>学历</div>
        <input type="text" placeholder="请选择"></div>
    </div>
  </div>
  <div class="apply-fields-BzcXI4i2Pm"><div class="title">实习经历</div>
    <button>添加</button>
    <div class="multi-NTCDdF2lci">
      <div class="apply-field-Q2iJ7AtQGX date_info-nCj8tT_zjw">
        <div class="month-range-select date_info wrapper-Wil">
          <span>起止时间</span>
          <input type="text" placeholder="年"><input type="text" placeholder="月">
          <span>-</span>
          <input type="text" placeholder="年"><input type="text" placeholder="月">
          <label><input type="checkbox"> 至今</label>
        </div>
      </div>
      <input type="text" placeholder="公司名称">
      <input type="text" placeholder="职位名称">
      <input type="text" placeholder="工作地点">
      <textarea placeholder="内容"></textarea>
    </div>
  </div>
</div></body></html>"""

FF = os.path.join(_TMP, "fixture.html")
open(FF, "w", encoding="utf-8").write(FIXTURE)

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox"])
    page = b.new_page()
    page.goto("file:///" + FF.replace("\\", "/"))
    page.wait_for_timeout(300)

    fields = filler._collect_fields(page)
    by_ph = {}
    for f in fields:
        by_ph.setdefault((f.get("ph") or "").strip(), []).append(f)

    print("== D. 合成标签：日期框现在能配上 period ==")
    ym = [f for f in fields if (f.get("ph") or "").strip() in ("年", "月")]
    ck("D1 采集到 8 个年月框（教育 4 + 实习 4）", len(ym) == 8, len(ym))
    # T71 起：年月框**优先取真实标题**（兄弟 div / 同层 span 里的「就读时间」「起止时间」），
    # 只有完全取不到标题时才补合成 token「起止时间」。两者都算「可识别为 period」——
    # 旧断言「一律是起止时间」已过时（真实标题更准，不该被降级）。
    _PT = ("起止时间", "就读时间", "时间", "起止", "期间", "日期")
    ck("D2 8 个年月框都带上了可识别的 period 标签（真标题或合成 token）",
       all((f.get("label") or "").strip() and any(t in f["label"] for t in _PT) for f in ym),
       [f.get("label") for f in ym])
    ck("D2b 教育那 4 框取到**真实标题**「就读时间」（T71 标题兜底，不再一律合成）",
       all("就读时间" in (f.get("label") or "") for f in ym[:4]),
       [f.get("label") for f in ym[:4]])
    ck("D2c 实习那 4 框取到「起止时间」",
       all("起止时间" in (f.get("label") or "") for f in ym[4:]),
       [f.get("label") for f in ym[4:]])
    # 合成 token 这条路径**仍要单独钉住**：完全没有标题文本的裸日期行必须补「起止时间」，
    # 否则它既不是 period、又会被当成普通框（旧 D2 顺带覆盖了它，改文案后就漏了）。
    _bare = ('<!doctype html><meta charset="utf-8"><div class="month-range-select date_info">'
             '<input type="text" placeholder="年"><input type="text" placeholder="月">'
             '<span>-</span><input type="text" placeholder="年"><input type="text" placeholder="月">'
             '</div>')
    _bp = b.new_page()
    _bp.set_content(_bare)
    _bl = [f.get("label") for f in filler._collect_fields(_bp) if (f.get("ph") or "") in ("年", "月")]
    _bp.close()
    ck("D2d 裸日期行（无任何标题）仍补合成「起止时间」",
       bool(_bl) and all("起止时间" in (l or "") for l in _bl), _bl)

    # 实习经历块：用块内控件做配对，period 必须配到年月框上
    intern = [f for f in fields if (f.get("ph") or "") in
              ("年", "月", "公司名称", "职位名称", "工作地点", "内容")]
    intern = intern[4:] if len(intern) > 5 else intern     # 取后 8 个（实习那半）
    pairs = filler._item_pairs("experiences", intern)
    pk = {k: (cf.get("ph") or cf.get("label")) for k, cf, _s in pairs}
    ck("D3 experiences 配到 period", "period" in pk, pk)
    ck("D4 period 配的是年月框（不是别的）", pk.get("period") in ("年", "月"), pk.get("period"))
    ck("D5 company 仍配 公司名称", pk.get("company") == "公司名称", pk)
    ck("D6 role 仍配 职位名称", pk.get("role") == "职位名称", pk)
    ck("D7 location 已进别名表（工作地点能配上）", pk.get("location") == "工作地点", pk)
    ck("D8 highlights 配 工作职责(textarea)", pk.get("highlights") == "内容", pk)

    print("== D9-D14 区间填写（真实交互）==")
    target = [f for f in ym if any((x.get("ph") or "") == "公司名称" for x in [])] or ym
    # 直接拿「实习经历」那行的第一个「年」框（它后面跟着 至今 勾选框）
    cand = None
    for f in ym:
        st = page.evaluate("""(i) => {
            const e = document.querySelector('[data-filler-idx="' + i + '"]');
            let r = e, hops = 0;
            while (r && hops < 6) {
              if (Array.from(r.querySelectorAll('input')).filter(x =>
                  /^(年|月)$/.test((x.getAttribute('placeholder')||'').trim())).length >= 2) break;
              r = r.parentElement; hops++;
            }
            return r ? r.innerText.replace(/\\s+/g, ' ') : '';
        }""", f["idx"])
        if "至今" in (st or ""):
            cand = f
            break
    ck("D9 找到带「至今」的那行日期框", cand is not None)
    if cand:
        ok, how = filler._fill_date_range(page, cand, "2026.07 – 2026.09")
        ck("D10 填 2026.07 – 2026.09 成功", ok is True, how)
        vals = page.evaluate("""() => {
            const r = document.querySelector('[data-daterange-till]').closest('*');
            return null;
        }""")
        row_vals = page.evaluate("""(i) => {
            const el = document.querySelector('[data-daterange="' + i + ':0"]');
            let r = el, hops = 0;
            while (r && hops < 6) {
              if (Array.from(r.querySelectorAll('input')).filter(x =>
                  /^(年|月)$/.test((x.getAttribute('placeholder')||'').trim())).length >= 2) break;
              r = r.parentElement; hops++;
            }
            return Array.from(r.querySelectorAll('input')).map(e => e.value);
        }""", cand["idx"])
        ck("D11 4 个框都进了值", row_vals[:4] == ["2026", "07", "2026", "09"], row_vals)
        ck("D12 不是只填第 1 个（老 bug）", len([v for v in row_vals[:4] if v]) == 4, row_vals)
        ck("D13 ndash/空格（en dash）也能解析", filler.parse_period("2026.07 – 2026.09") ==
           ((2026, 7), (2026, 9), False))

        # 至今：勾上、不填右半边
        ok2, how2 = filler._fill_date_range(page, cand, "2023.09 至今")
        ck("D14 「至今」填成功", ok2 is True, how2)
        till = page.evaluate("""(i) => {
            const el = document.querySelector('[data-daterange-till="' + i + '"]');
            return el ? el.checked : null;
        }""", cand["idx"])
        ck("D15 「至今」勾选框被勾上", till is True, till)

        # 反向：错值回读必须判失败
        ck("D16 反向：回读比对按数字（07 与 7 等价）",
           filler._date_part_ok("7", "07") and filler._date_part_ok("2026", "2026"))
        ck("D17 反向：错月份必须判失败", not filler._date_part_ok("08", "07"))
        ck("D18 反向：错年份必须判失败", not filler._date_part_ok("2025", "2026"))
        ck("D19 反向：空值必须判失败（不许把没填当成功）",
           not filler._date_part_ok("", "2026"))

        # 教育背景那行（没有 至今）：同样能填
        edu = [f for f in ym if f is not cand][0]
        ok3, how3 = filler._fill_date_range(page, edu, "2023.9-2027.6")
        ck("D20 教育背景就读时间也能填", ok3 is True, how3)

        # 非区间控件必须明确返回「不是年月区间控件」→ 调用方才会回落
        plain = [f for f in fields if (f.get("ph") or "") == "公司名称"][-1]
        ok4, how4 = filler._fill_date_range(page, plain, "2026.07")
        ck("D21 非区间控件返回明确原因（供回落）", ok4 is False and how4 == "不是年月区间控件",
           how4)
        ok5, how5 = filler._fill_field_smart(page, plain, "杭州潜渊之心")
        ck("D22 _fill_field_smart 对普通字段照常填成功", ok5 is True, how5)
        ck("D23 普通字段值已写入", page.input_value(
            '[data-filler-idx="%s"]' % plain["idx"]) == "杭州潜渊之心")

    print("== G. 证伪：收紧前后「日期行识别」对照 ==")
    plain_all = [f for f in fields if (f.get("ph") or "") == "公司名称"]
    plain_f = plain_all[-1]
    ck("G1 普通字段（公司名称）不再被判成日期行",
       page.evaluate(filler.DATE_RANGE_JS, str(plain_f["idx"])) is None)
    ck("G2 反向：日期字段仍判得出日期行（收紧 ≠ 一律返回空）",
       (page.evaluate(filler.DATE_RANGE_JS, str(ym[0]["idx"])) or {}).get("n") == 4)
    # 证伪对照：把**旧**判据（只要祖先里含 >=2 个年月框就算日期行）单独跑一遍
    LOOSE_JS = """(idx) => {
      const el = document.querySelector('[data-filler-idx="' + idx + '"]');
      const isYM = e => e.tagName === 'INPUT' &&
            /^(年|月|year|month|yyyy|mm)$/i.test((e.getAttribute('placeholder') || '').trim());
      let r = el, k = 0;
      while (r && k < 6) {
        if (Array.from(r.querySelectorAll('input')).filter(isYM).length >= 2) return r;
        r = r.parentElement; k++;
      }
      return null;
    }"""
    ck("G3 证伪：旧判据**确实会**把普通字段误判成日期行（否则这段收紧是多余的）",
       page.evaluate(LOOSE_JS, str(plain_f["idx"])) is not None)

    print("== E. 证伪：去掉合成标签 → 复现「日期框配不上、一个字都不填」==")
    stripped = [dict(f, label="") for f in intern]
    pk2 = {k for k, _c, _s in filler._item_pairs("experiences", stripped)}
    ck("E1 没有合成标签时 period **配不上**（复现原 bug）", "period" not in pk2, pk2)
    ck("E2 但 company 等文本字段照旧能配（说明差别就在日期这一处）",
       "company" in pk2, pk2)
    b.close()

# =========================================================================
print("== F. 源码结构断言 ==")
src = open(os.path.join(HERE, "filler.py"), "rb").read().decode("utf-8")
ck("F1 _LIST_PATHS 含 education", '"education"' in
   [l for l in src.split("\n") if l.startswith("_LIST_PATHS")][0])
ck("F2 PRIMARY_ALIASES 有 education 且**不含裸「学校」**",
   '"education": ["学校名称"' in src)
ck("F3 否定规则存在", "_NEGATIVE_RULES" in src and "def _rejected" in src)
ck("F4 _score 里用了 _rejected", "if _rejected(path, field):" in src)
ck("F5 _fill_repeat_section 用 _section_items", "_section_items(profile, path)" in src)
ck("F6 两处填值都走 _fill_field_smart",
   src.count("= _fill_field_smart(page, f, val") == 2,
   src.count("= _fill_field_smart(page, f, val"))
ck("F7 不再直接对 section 里的控件调 _robust_fill_field",
   "ok, how = _robust_fill_field(page, f, val, note=note)" not in src)
ck("F8 年月框合成标签写进了 COLLECT_JS 与 ANALYZE_JS", src.count("起止时间'") >= 2 or
   src.count("label = (label || '') + ' 起止时间'") == 1)
ck("F9 _blank_section_flat 处理子路径", "def _blank_section_flat" in src)

print("\n===== T70 日期区间 + 别名护栏：%d/%d %s =====" %
      (N[0] - len(BAD), N[0], "PASSED" if not BAD else ("FAILED -> %s" % BAD)))
sys.exit(1 if BAD else 0)
