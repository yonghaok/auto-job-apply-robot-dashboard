"""match_fields 纯函数单测（不需要 Playwright / 浏览器）。

验证「个人信息库 ↔ 表单字段」的别名匹配是否一对一且符合直觉。
运行：python backend/test_match_fields.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import filler  # noqa: E402


def run():
    profile = filler.load_profile()
    flat = filler._flatten(profile)

    # 模拟 Playwright 采集到的一份中文校招表单
    fields = [
        {"tag": "input", "type": "text", "label": "姓名", "ph": "", "nm": "name", "id": "name", "opts": []},
        {"tag": "input", "type": "tel", "label": "手机号", "ph": "", "nm": "mobile", "id": "mobile", "opts": []},
        {"tag": "input", "type": "email", "label": "电子邮箱", "ph": "", "nm": "email", "id": "email", "opts": []},
        {"tag": "input", "type": "text", "label": "毕业院校", "ph": "", "nm": "school", "id": "school", "opts": []},
        {"tag": "input", "type": "text", "label": "所学专业", "ph": "", "nm": "major", "id": "major", "opts": []},
        {"tag": "select", "type": "", "label": "最高学历", "ph": "", "nm": "degree", "id": "degree",
         "opts": ["博士", "硕士", "本科", "大专"]},
        {"tag": "textarea", "type": "", "label": "自我评价", "ph": "", "nm": "intro", "id": "intro", "opts": []},
    ]

    assigns, unfilled = filler.match_fields(flat, fields)

    print("=== 匹配结果（path → 表单 label）===")
    got = {}
    for path, f, score in assigns:
        label = f.get("label") or f.get("nm")
        got[path] = label
        print("  %-22s → %-10s (score=%d, tag=%s)" % (path, label, score, f["tag"]))

    expect = {
        "name": "姓名",
        "phone": "手机号",
        "email": "电子邮箱",
        "education.school": "毕业院校",
        "education.major": "所学专业",
        "education.degree": "最高学历",
        "self_intro": "自我评价",
    }

    fails = []
    for k, v in expect.items():
        if got.get(k) != v:
            fails.append("期望 %s → %s，实际 %s" % (k, v, got.get(k)))

    # 一对一校验：每个表单字段最多被用一个 profile 字段占用
    used_labels = [f.get("label") for _p, f, _s in assigns]
    if len(used_labels) != len(set(used_labels)):
        fails.append("存在表单字段被重复分配：%s" % used_labels)

    print("\n=== 未命中（需人工补）===")
    print(" ", "，".join(unfilled) or "无")

    print("\n=== 判定 ===")
    if fails:
        for f in fails:
            print("  FAILED:", f)
        return 1
    print("  PASSED：7 项预期映射全部命中，且无重复分配")
    return 0


if __name__ == "__main__":
    sys.exit(run())
