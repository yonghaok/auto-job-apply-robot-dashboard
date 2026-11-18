"""离线验证：新增的 6 个个人信息字段（籍贯/现居地址/学校地址/身份证号/意向城市/是否接受调剂）
在「真实填写页」能否被 match_fields 找到选择入口。

直接用 filler.match_fields + _flatten（与真实投递同一套匹配逻辑），喂入一份
「典型校招投递表」的字段清单（label/placeholder/name/id），验证每个 profile 字段
能一对一分配到表单字段。无需浏览器/登录。

两个场景：
  A 完整表（含「学校地址」字段）—— 证别名能命中
  B 典型真实表（无「学校地址」字段）—— 如实展示 school_addr 通常无入口
"""
import sys
import filler


def F(label, ph="", nm="", tid=""):
    return {"label": label, "ph": ph, "nm": nm, "id": tid, "val": ""}


profile = {
    "name": "张三", "gender": "男", "phone": "13800000000", "email": "a@b.com",
    "birth": "2000-01", "location": "上海",
    "intended_city": "深圳", "native_place": "湖南", "current_addr": "上海市浦东新区",
    "school_addr": "广州市番禺区", "id_card": "440000199001010011", "accept_adjust": True,
    "education": {"school": "XX大学", "degree": "本科", "major": "计算机",
                  "grad_year": 2027, "period": "2023-2027", "gpa": "3.8"},
    "resume_pdf": "x.pdf", "self_intro": "…",
    "experiences": [{"company": "A"}], "projects": [{"name": "P"}],
    "skills": ["Figma"], "awards": ["x"],
}
flat = filler._flatten(profile)

# 场景 A：较完整的校招表（含学校地址字段）
formA = [
    F("姓名"), F("性别"), F("手机号", ph="请输入手机号"), F("邮箱"), F("出生日期"),
    F("籍贯"), F("证件号码"), F("现居住地址"), F("通讯地址"), F("期望工作城市"),
    F("学校地址"), F("是否服从调剂"), F("自我评价"), F("工作经历"), F("项目经历"),
    F("技能特长"), F("获奖情况"), F("毕业院校"), F("学历"), F("专业"), F("毕业年份"),
]
# 场景 B：典型真实校招表（很多站没有「学校地址」这一项）
formB = [
    F("姓名"), F("性别"), F("手机号"), F("邮箱"), F("出生日期"),
    F("籍贯"), F("证件号码"), F("现居住地址"), F("期望工作城市"),
    F("是否服从调剂"), F("自我评价"), F("工作经历"), F("项目经历"),
    F("技能特长"), F("获奖情况"), F("毕业院校"), F("学历"), F("专业"), F("毕业年份"),
]

a_assigned, a_unfilled = filler.match_fields(flat, formA)
b_assigned, b_unfilled = filler.match_fields(flat, formB)


def show(title, assigned, unfilled):
    print("\n=== %s ===" % title)
    for path, f, s in assigned:
        print("  %-14s -> %-14s (score=%s)" % (path, f.get("label") or f.get("ph"), s))
    print("  UNFILLED: %s" % (unfilled or "（无）"))


show("场景A 完整表（含学校地址）", a_assigned, a_unfilled)
show("场景B 典型真实表（无学校地址）", b_assigned, b_unfilled)

need = ["intended_city", "native_place", "current_addr", "school_addr", "id_card", "accept_adjust"]
a_paths = {p for p, _, _ in a_assigned}
b_paths = {p for p, _, _ in b_assigned}

ok = True
# A：6 个都应匹配
for p in need:
    if p not in a_paths:
        print("FAIL A: %s 未匹配到任何表单字段" % p); ok = False
# B：除 school_addr 外都应匹配；school_addr 应如实落在 unfilled
for p in need:
    if p == "school_addr":
        if p not in b_unfilled:
            print("NOTE B: school_addr 竟匹配到了（与预期不符，需复核）")
    else:
        if p not in b_paths:
            print("FAIL B: %s 未在典型真实表匹配" % p); ok = False

adj = [f.get("label") for p, f, _ in a_assigned if p == "accept_adjust"]
print("\naccept_adjust 命中字段:", adj)
if not adj or "是否服从调剂" not in adj:
    print("FAIL: accept_adjust 未命中调剂字段"); ok = False

print("\nRESULT:", "ALL PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
