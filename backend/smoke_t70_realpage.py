# -*- coding: utf-8 -*-
"""T70 真机回归：在**全志科技 / MokaHR 投递页真实采集到的控件**上验「填错 / 日期栏不填」。

数据来路：2026-09-24 用 CDP 接管真实页面
`campus.allwinnertech.com/campus-recruitment/allwinnertech/43436/#/job/<id>/apply?applyFormId=204602`
采集的 48 个可见控件的 tag/placeholder/label（原样内联在下面的 REAL 里，见 _t70_dump/）。
**不联网、不开浏览器** —— 纯匹配逻辑验证，可反复跑。

真实数据里的关键事实：
  · 教育背景块 = 4 个**只有 placeholder 年/月**的日期框 + 学校名称 / 专业名称 / 学校类别 / 学历
  · 实习经历块 = 4 个日期框 + 「至今」勾选框 + 公司名称 / 职位名称 / 工作地点 / 工作职责(textarea)
  · 顶层还有两个**同族干扰字段**：「毕业院校所在专业成绩排名」「毕业院校所在专业人数（数字）」

本套件钉住：A 一对一分派（不许被同族字段抢）· B 教育块配对 · C 实习块配对 ·
D 用 profile.json 里**真实的** period 原文验解析 · E 源码结构。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import filler  # noqa: E402

BAD, N = [], [0]


def ck(name, cond, extra=""):
    N[0] += 1
    print("  %s  %s%s" % ("PASS" if cond else "FAIL", name,
                          "" if cond else "   %s" % (extra,)))
    if not cond:
        BAD.append(name)


REAL = json.loads(r"""[{"tag": "input", "ph": "输入职位关键字", "label": "登录", "nm": "", "id": ""}, {"tag": "input", "ph": "输入职位关键字", "label": "", "nm": "", "id": ""}, {"tag": "input", "ph": "选择意向工作城市", "label": "意向工作城市", "nm": "", "id": ""}, {"tag": "input", "ph": "推荐码", "label": "推荐码", "nm": "", "id": ""}, {"tag": "input", "ph": "姓名", "label": "姓名", "nm": "", "id": ""}, {"tag": "input", "ph": "", "label": "+86", "nm": "", "id": ""}, {"tag": "input", "ph": "请输入手机号", "label": "+86", "nm": "", "id": ""}, {"tag": "input", "ph": "邮箱", "label": "邮箱", "nm": "", "id": ""}, {"tag": "input", "ph": "请选择", "label": "性别", "nm": "", "id": ""}, {"tag": "input", "ph": "出生日期 (年龄)", "label": "出生日期 (年龄)", "nm": "", "id": ""}, {"tag": "input", "ph": "请选择", "label": "最高学历", "nm": "", "id": ""}, {"tag": "input", "ph": "请选择", "label": "最高学历学校类别", "nm": "", "id": ""}, {"tag": "input", "ph": "请选择", "label": "全志校招信息获知来源", "nm": "", "id": ""}, {"tag": "input", "ph": "请输入籍贯", "label": "", "nm": "", "id": ""}, {"tag": "input", "ph": "请选择省市区", "label": "", "nm": "", "id": ""}, {"tag": "input", "ph": "期望就业城市（学生自填）", "label": "期望就业城市（学生自填）", "nm": "", "id": ""}, {"tag": "input", "ph": "请选择", "label": "毕业院校所在专业成绩排名", "nm": "", "id": ""}, {"tag": "input", "ph": "毕业院校所在专业人数（数字）", "label": "毕业院校所在专业人数（数字）", "nm": "", "id": ""}, {"tag": "input", "ph": "与过往经历最匹配且心仪的职位方向", "label": "请根据岗位描述中的“职位方向”选择一个填写", "nm": "", "id": ""}, {"tag": "input", "ph": "年", "label": "", "nm": "", "id": ""}, {"tag": "input", "ph": "月", "label": "", "nm": "", "id": ""}, {"tag": "input", "ph": "年", "label": "-", "nm": "", "id": ""}, {"tag": "input", "ph": "月", "label": "-", "nm": "", "id": ""}, {"tag": "input", "ph": "请输入就读学校", "label": "学校名称", "nm": "", "id": ""}, {"tag": "input", "ph": "请输入专业名称", "label": "专业名称", "nm": "", "id": ""}, {"tag": "input", "ph": "请选择", "label": "学校类别", "nm": "", "id": ""}, {"tag": "input", "ph": "请选择", "label": "学历", "nm": "", "id": ""}, {"tag": "input", "ph": "年", "label": "", "nm": "", "id": ""}, {"tag": "input", "ph": "月", "label": "", "nm": "", "id": ""}, {"tag": "input", "ph": "年", "label": "- | 至今", "nm": "", "id": ""}, {"tag": "input", "ph": "月", "label": "- | 至今", "nm": "", "id": ""}, {"tag": "input", "ph": "", "label": "至今", "nm": "", "id": ""}, {"tag": "input", "ph": "公司名称", "label": "公司名称", "nm": "", "id": ""}, {"tag": "input", "ph": "职位名称", "label": "职位名称", "nm": "", "id": ""}, {"tag": "input", "ph": "工作地点", "label": "工作地点", "nm": "", "id": ""}, {"tag": "textarea", "ph": "内容", "label": "工作职责", "nm": "", "id": ""}, {"tag": "input", "ph": "奖学金项目名称", "label": "奖学金项目名称", "nm": "", "id": ""}, {"tag": "input", "ph": "请选择", "label": "奖学金类别", "nm": "", "id": ""}, {"tag": "input", "ph": "请选择", "label": "获奖等级", "nm": "", "id": ""}, {"tag": "input", "ph": "竞赛项目名称", "label": "竞赛项目名称", "nm": "", "id": ""}, {"tag": "input", "ph": "请选择", "label": "竞赛类别", "nm": "", "id": ""}, {"tag": "input", "ph": "请选择", "label": "获奖等级", "nm": "", "id": ""}, {"tag": "input", "ph": "", "label": "同步更新在线简历", "nm": "", "id": ""}, {"tag": "input", "ph": "", "label": "我已阅读并同意《隐私协议》", "nm": "", "id": ""}, {"tag": "input", "ph": "", "label": "+86", "nm": "", "id": ""}, {"tag": "input", "ph": "请输入手机号", "label": "+86", "nm": "", "id": ""}, {"tag": "input", "ph": "请输入验证码", "label": "获取验证码", "nm": "", "id": ""}, {"tag": "input", "ph": "", "label": "首次登录会自动创建新账号，且阅读并同意《隐私协议》", "nm": "", "id": ""}]""")


def by_label(lbl):
    for f in REAL:
        if (f.get("label") or "") == lbl:
            return f
    return None


def ck_found(name, obj):
    ck(name, obj is not None)
    return obj


print("== A. 一对一分派：同族字段不许抢（用户报的「很多信息填写不正确」）==")
F_RANK = ck_found("A0 找到「毕业院校所在专业成绩排名」", by_label("毕业院校所在专业成绩排名"))
F_CNT = ck_found("A0 找到「毕业院校所在专业人数（数字）」", by_label("毕业院校所在专业人数（数字）"))
F_CAT = ck_found("A0 找到「最高学历学校类别」", by_label("最高学历学校类别"))
F_DEG_TOP = ck_found("A0 找到「最高学历」", by_label("最高学历"))
F_SCHOOL = ck_found("A0 找到「学校名称」", by_label("学校名称"))
F_MAJOR = ck_found("A0 找到「专业名称」", by_label("专业名称"))
F_CAT_IN = ck_found("A0 找到块内「学校类别」", by_label("学校类别"))
F_DEG_IN = ck_found("A0 找到块内「学历」", by_label("学历"))

ck("A1 学校名称命中 education.school",
   filler._score("education.school", F_SCHOOL) > 0,
   filler._score("education.school", F_SCHOOL))
ck("A2 **成绩排名不再**接到 education.school（否则学校名被塞进 TOP 5% 下拉）",
   filler._score("education.school", F_RANK) == 0,
   filler._score("education.school", F_RANK))


def loose_score(path, field):
    """**旧**规则（只做别名子串、没有否定规则）—— 用来证伪护栏不是多余的。"""
    hay = filler._hay(field)
    best = 0
    for a in filler.FIELD_ALIASES.get(path, []):
        al = a.lower()
        if al in hay:
            s = len(al)
            if hay.strip() == al or (field.get("label") or "").strip().lower() == al:
                s += 10
            best = max(best, s)
    return best


ck("A3 证伪：旧规则**确实会**把 education.school 打到成绩排名上（所以 A2 的护栏在干活）",
   loose_score("education.school", F_RANK) > 0,
   loose_score("education.school", F_RANK))
ck("A4 专业人数不再接 education.school",
   filler._score("education.school", F_CNT) == 0)
ck("A5 学校类别不接 education.degree（它是类别，不是学历）",
   filler._score("education.degree", F_CAT) == 0)
ck("A6 反向：最高学历仍接 education.degree",
   filler._score("education.degree", F_DEG_TOP) > 0)
ck("A6b 块内「学校类别」也不接 education.school（类别 ≠ 校名）",
   filler._score("education.school", F_CAT_IN) == 0,
   filler._score("education.school", F_CAT_IN))


def argmax(path):
    scored = [(filler._score(path, f), i) for i, f in enumerate(REAL)]
    s, i = max(scored)
    return s, REAL[i]


s, f = argmax("education.school")
ck("A7 全 48 个真实字段里，education.school 的最高分就是「学校名称」",
   s > 0 and f.get("label") == "学校名称", (s, f.get("label")))
s, f = argmax("education.major")
ck("A8 同理 education.major 的最高分就是「专业名称」",
   s > 0 and f.get("label") == "专业名称", (s, f.get("label")))

# --- 合成标签：新 COLLECT_JS 会给「只有 年/月 占位符」的框补上「 起止时间」 ---
YM_PH = ("年", "月")
ym = [f for f in REAL if (f.get("ph") or "").strip() in YM_PH]
ck("A9 真实页面采集到 8 个年月框（教育 4 + 实习 4）", len(ym) == 8, len(ym))
for f in ym:
    f["label"] = ((f.get("label") or "") + " 起止时间").strip()

i_sch = REAL.index(F_SCHOOL)
EDU = REAL[i_sch - 4:i_sch + 4]
ck("A10 教育块 = 4 个日期框 + 学校名称/专业名称/学校类别/学历",
   len(EDU) == 8 and all((x.get("ph") or "").strip() in YM_PH for x in EDU[:4]),
   [(x.get("ph"), x.get("label")) for x in EDU])
F_COM = ck_found("A0 找到「公司名称」", by_label("公司名称"))
i_com = REAL.index(F_COM)
INT = REAL[i_com - 5:i_com + 4]
ck("A11 实习块 = 4 个日期框 + 至今 + 公司名称/职位名称/工作地点/工作职责",
   len(INT) == 9 and all((x.get("ph") or "").strip() in YM_PH for x in INT[:4]),
   [(x.get("ph"), x.get("label")) for x in INT])

print("== B. 教育背景块：配对（period / school / major / degree）==")
pk = {k: f for k, f, _s in filler._item_pairs("education", EDU)}
ck("B1 period 配到真实的年月框（T70 前：一个字都填不上）",
   pk.get("period") is not None and (pk["period"].get("ph") or "").strip() in YM_PH,
   pk.get("period"))
ck("B2 school 配到「学校名称」而**不是**「学校类别」",
   (pk.get("school") or {}).get("label") == "学校名称",
   (pk.get("school") or {}).get("label"))
ck("B3 major 配到「专业名称」", (pk.get("major") or {}).get("label") == "专业名称",
   (pk.get("major") or {}).get("label"))
ck("B4 degree 配到「学历」", (pk.get("degree") or {}).get("label") == "学历",
   (pk.get("degree") or {}).get("label"))
ck("B5 学校类别没被任何子字段抢（如实留给人工/留空，不许乱填）",
   all((v.get("label") != "学校类别") for v in pk.values()),
   {k: (v.get("label")) for k, v in pk.items()})
stripped = [dict(f, label=(f.get("ph") or "")) for f in EDU]
pk2 = {k for k, _f, _s in filler._item_pairs("education", stripped)}
ck("B6 证伪：没有合成标签时 period **配不上**（复现原 bug；差异只在日期这一处）",
   "period" not in pk2, pk2)

print("== C. 实习经历块：配对 ==")
pk = {k: f for k, f, _s in filler._item_pairs("experiences", INT)}
ck("C1 period 配到真实的年月框", (pk.get("period") or {}).get("ph") in YM_PH,
   pk.get("period"))
ck("C2 company 配到「公司名称」", (pk.get("company") or {}).get("label") == "公司名称")
ck("C3 role 配到「职位名称」", (pk.get("role") or {}).get("label") == "职位名称")
ck("C4 location 配到「工作地点」（T70 新增别名）",
   (pk.get("location") or {}).get("label") == "工作地点",
   (pk.get("location") or {}).get("label"))
ck("C5 highlights 配到「工作职责」", (pk.get("highlights") or {}).get("label") == "工作职责")
stripped = [dict(f, label=(f.get("ph") or "")) for f in INT]
pk2 = {k for k, _f, _s in filler._item_pairs("experiences", stripped)}
ck("C6 证伪：没有合成标签时 period 配不上", "period" not in pk2, pk2)

print("== D. 用 profile.json 里**真实的** period 原文验解析 ==")
REAL_PERIODS = [
    ("2026.07 \u2013 2026.09", (2026, 7), (2026, 9), False),   # 杭州潜渊之心（实习）
    ("2025.09 - 2026.03", (2025, 9), (2026, 3), False),        # 特斯拉
    ("2024.06 - 2024.12", (2024, 6), (2024, 12), False),       # 杭州旺慧
    ("2023.9-2027.6", (2023, 9), (2027, 6), False),            # 教育背景 education.period
    ("2026.2-2026.6", (2026, 2), (2026, 6), False),            # 项目 1
    ("2025.2-2025.12", (2025, 2), (2025, 12), False),          # 项目 2
    ("2024.2-2025.2", (2024, 2), (2025, 2), False),            # 项目 3
]
for raw, ws, we, on in REAL_PERIODS:
    got = filler.parse_period(raw)
    ck("D 真实原文 %r → %s~%s" % (raw, ws, we),
       bool(got) and got[0] == ws and got[1] == we and got[2] is on, got)
ck("D 写进输入框的月份补零（7 → 07）", filler._fmt_month(7) == "07")
ck("D 年份四位", filler._fmt_year(2026) == "2026")
ck("D 回读比对：07 与 7 等价", filler._date_part_ok("7", "07"))
ck("D 反向：08 与 07 必须判失败", not filler._date_part_ok("08", "07"))
ck("D 反向：空值必须判失败", not filler._date_part_ok("", "2026"))

print("== E. 源码结构 ==")
src = open(os.path.join(HERE, "filler.py"), "rb").read().decode("utf-8")
ck("E1 _item_pairs 内也过否定规则", 'if _rejected("%s.%s" % (path, key), f):' in src)
ck("E2 _LIST_PATHS 含 education", '"education", "experiences", "projects"' in src.replace("'", '"'))
# 两处写法不同、判据不同：COLLECT_JS 补 [label] 字段，ANALYZE_JS 把 token 拼进 [s] 串
ck("E3 COLLECT_JS 给年月框补了合成 label",
   "label = (label || '') + ' 起止时间'" in src)
ck("E4 ANALYZE_JS 给年月框补了合成 token",
   "s += ' 起止时间 period time date';" in src)
ck("E5 education.school 的具体别名排在裸「学校」之前（否则与「学校类别」打平）",
   '"education.school": ["学校名称", "院校名称", "就读学校", "学校名",' in src)

print("\n===== T70 真机字段回归：%d/%d %s =====" %
      (N[0] - len(BAD), N[0], "PASSED" if not BAD else ("FAILED -> %s" % BAD)))
sys.exit(1 if BAD else 0)
