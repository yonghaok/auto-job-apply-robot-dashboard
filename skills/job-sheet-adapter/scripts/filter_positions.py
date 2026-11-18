#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对标准岗位表做关键词 / 城市筛选，输出带「匹配等级」列的新表。

口径（与 resume-bot/positions.py 一致）：
    strict  所有关键词都命中，且城市命中
    loose   任一关键词命中，且城市命中
    all     不做过滤

用法：
    python filter_positions.py 岗位总表.xlsx --keywords 产品经理 本科 --cities 杭州 上海
    python filter_positions.py 岗位总表.xlsx --rules match_rules.json --mode loose
    python filter_positions.py 岗位总表.xlsx --keywords 产品经理 --mode all -o 全部岗位.xlsx

退出码：0 成功；2 输入表结构不对。
"""

import argparse
import json
import os
import sys

REQUIRED = ["公司", "岗位名称"]


def load_rules(path):
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    return list(d.get("keywords") or []), list(d.get("cities") or [])


def read_rows(path):
    try:
        import openpyxl
    except ImportError:
        sys.exit("缺少 openpyxl：pip install openpyxl")
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        sys.exit("表是空的")
    header = [str(c or "").strip() for c in rows[0]]
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        sys.exit(
            "缺少必需列：%s\n当前表头：%s\n请先用 normalize_sheet.py 转成标准格式。"
            % ("、".join(missing), " | ".join(header))
        )
    idx = {name: header.index(name) for name in header if name}
    data = []
    for r in rows[1:]:
        if not any(x is not None and str(x).strip() for x in r):
            continue
        rec = {k: ("" if r[j] is None else str(r[j]).strip()) for k, j in idx.items() if j < len(r)}
        data.append(rec)
    return header, data


def classify(rec, keywords, cities):
    """返回 (level, kw_in_title, kw_in_other, city_ok)。"""
    title = rec.get("岗位名称", "")
    blob = " ".join(
        str(rec.get(k, "")) for k in ("岗位名称", "城市", "职责族", "公司")
    )
    in_title = [k for k in keywords if k and k in title]
    in_other = [k for k in keywords if k and k not in title and k in blob]
    city = rec.get("城市", "")
    city_ok = (not cities) or any(c and c in city for c in cities)

    kw_all = (len(in_title) + len(in_other)) == len(keywords)
    kw_any = bool(in_title or in_other)
    if not keywords:
        kw_all = kw_any = True

    if kw_all and city_ok:
        level = "strict"
    elif kw_any and city_ok:
        level = "loose"
    else:
        level = "none"
    return level, in_title, in_other, bool(city_ok)


def write_xlsx(header, data, path):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    out_header = list(header)
    if "匹配等级" not in out_header:
        out_header.append("匹配等级")

    wb = Workbook()
    ws = wb.active
    ws.title = "筛选结果"
    ws.append(out_header)
    for rec in data:
        ws.append([rec.get(c, "") for c in out_header])

    head_fill = PatternFill("solid", fgColor="1F3864")
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = head_fill
        c.alignment = Alignment(vertical="center")

    widths = {"公司": 26, "岗位名称": 36, "岗位页链接": 50, "城市": 14,
              "职责族": 16, "来源校招页": 22, "匹配等级": 12}
    for j, name in enumerate(out_header, start=1):
        ws.column_dimensions[get_column_letter(j)].width = widths.get(name, 18)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = "A1:%s%d" % (get_column_letter(len(out_header)), len(data) + 1)
    wb.save(path)


def main():
    ap = argparse.ArgumentParser(description="岗位表关键词/城市筛选")
    ap.add_argument("input", help="标准岗位表（.xlsx）")
    ap.add_argument("-o", "--output", default=None, help="输出路径（默认 <输入名>_筛选.xlsx）")
    ap.add_argument("--keywords", nargs="*", default=None, help="关键词，空格分隔")
    ap.add_argument("--cities", nargs="*", default=None, help="城市，空格分隔")
    ap.add_argument("--rules", default=None, help="从 JSON 读 keywords/cities（如 match_rules.json）")
    ap.add_argument("--mode", choices=["strict", "loose", "all"], default="strict",
                    help="strict=全含且城市命中；loose=任一含；all=不过滤")
    ap.add_argument("--keep-none", action="store_true", help="all 模式下也保留不匹配项（默认保留）")
    args = ap.parse_args()

    if not os.path.isfile(args.input):
        sys.exit("输入文件不存在：%s" % args.input)

    keywords, cities = [], []
    if args.rules:
        keywords, cities = load_rules(args.rules)
    if args.keywords is not None:
        keywords = args.keywords
    if args.cities is not None:
        cities = args.cities

    header, data = read_rows(args.input)

    tagged = []
    counts = {"strict": 0, "loose": 0, "none": 0}
    for rec in data:
        level, in_title, in_other, city_ok = classify(rec, keywords, cities)
        rec = dict(rec)
        rec["匹配等级"] = level
        rec["_in_title"] = in_title
        rec["_in_other"] = in_other
        rec["_city_ok"] = city_ok
        counts[level] += 1
        tagged.append(rec)

    if args.mode == "all":
        kept = tagged
    elif args.mode == "strict":
        kept = [r for r in tagged if r["匹配等级"] == "strict"]
    else:
        kept = [r for r in tagged if r["匹配等级"] in ("strict", "loose")]

    kept.sort(key=lambda r: ({"strict": 0, "loose": 1, "none": 2}[r["匹配等级"]],
                             r.get("公司", ""), r.get("岗位名称", "")))

    out = args.output or (os.path.splitext(args.input)[0] + "_筛选.xlsx")
    write_xlsx(header, kept, out)

    print("口径：%s ｜ 关键词：%s ｜ 城市：%s"
          % (args.mode, "、".join(keywords) or "(无)", "、".join(cities) or "(不限)"))
    print("全表 %d 条：strict=%d，loose=%d，none=%d" % (len(data), counts["strict"], counts["loose"], counts["none"]))
    print("输出 %d 条 → %s" % (len(kept), os.path.abspath(out)))

    # 只命中在「卡面其它文字」里的，单独提示，避免误以为标题符合
    only_other = [r for r in kept if not r["_in_title"] and r["_in_other"]]
    if only_other:
        print("提示：%d 条的关键词只出现在公司/城市/职责族里，标题本身不含关键词：" % len(only_other))
        for r in only_other[:5]:
            print("  - %s / %s（命中：%s）"
                  % (r.get("公司", ""), r.get("岗位名称", ""), "、".join(r["_in_other"])))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
