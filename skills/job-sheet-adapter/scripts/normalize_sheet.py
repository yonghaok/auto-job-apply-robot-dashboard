#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把任意格式的职位 Excel / CSV 转成 resume-bot 可识别的标准岗位表。

标准列（顺序固定）：
    公司 | 岗位名称 | 岗位页链接 | 城市 | 职责族 | 来源校招页

用法：
    python normalize_sheet.py 输入.xlsx
    python normalize_sheet.py 输入.xlsx -o 岗位总表.xlsx
    python normalize_sheet.py 输入.xlsx --sheet "岗位列表" --header-row 3
    python normalize_sheet.py 输入.xlsx --strict-url      # 无链接的行直接丢弃
    python normalize_sheet.py 输入.csv -o 岗位总表.xlsx

退出码：0 成功（可能有警告）；2 无法识别表头或没有任何有效行。
"""

import argparse
import csv
import json
import os
import re
import sys

# ---------------------------------------------------------------- 列名别名

ALIASES = {
    "公司": [
        "公司", "公司名称", "公司全称", "公司简称", "企业名称", "企业", "企业全称",
        "单位", "单位名称", "用人单位", "招聘单位", "雇主", "机构名称", "所属公司",
        "company", "companyname", "employer", "organization",
    ],
    "岗位名称": [
        "岗位名称", "岗位", "岗位名", "职位名称", "职位", "职位名", "招聘岗位",
        "招聘职位", "岗位/职位", "jobtitle", "position", "positionname", "title", "job",
    ],
    "岗位页链接": [
        "岗位页链接", "岗位链接", "岗位地址", "职位链接", "详情链接", "投递链接",
        "网申链接", "网申地址", "申请链接", "报名链接", "链接", "网址", "url",
        "link", "applyurl", "joburl", "detailurl", "href",
    ],
    "城市": [
        "城市", "工作城市", "工作地点", "地点", "所在地", "工作地址", "上班地点",
        "base地", "base", "city", "location", "worklocation", "工作地区", "地区",
    ],
    "职责族": [
        "职责族", "职能", "职能类别", "岗位类别", "职位类别", "职位族", "岗位族",
        "方向", "大类", "类别", "category", "function", "jobfamily", "jobfunction",
    ],
    "来源校招页": [
        "来源校招页", "来源", "来源网站", "来源页面", "招聘渠道", "渠道", "信息来源",
        "source", "channel", "frompage",
    ],
}

STANDARD_COLUMNS = ["公司", "岗位名称", "岗位页链接", "城市", "职责族", "来源校招页"]

# 明显不是数据行的行（合计/表头重复）
JUNK_PATTERNS = [
    re.compile(r"^(合计|小计|总计|共计|累计|以上合计)"),
    re.compile(r"^(备注|说明|注[:：])"),
]

_PUNCT = re.compile(r"[\s\u3000\-_/\\|·,，、:：;；()（）\[\]【】<>《》\"'`~]+")
_URL_RE = re.compile(r"https?://[^\s\"'<>）)】\]]+", re.I)


def _norm(text):
    """列名归一化：去空白/标点/括号内容，转小写。"""
    if text is None:
        return ""
    s = str(text).strip()
    s = re.sub(r"[（(][^）)]*[）)]", "", s)   # 去掉括号注释
    s = _PUNCT.sub("", s)
    return s.lower()


_ALIAS_INDEX = {}
for _canon, _names in ALIASES.items():
    for _n in _names:
        _ALIAS_INDEX[_norm(_n)] = _canon
    _ALIAS_INDEX[_norm(_canon)] = _canon


def map_header(cell):
    """单列名 → 标准列名；认不出返回 None。"""
    key = _norm(cell)
    if not key:
        return None
    if key in _ALIAS_INDEX:
        return _ALIAS_INDEX[key]
    # 包含匹配：如「公司名称（全称）」已被去括号处理；再兜一层子串
    for alias, canon in _ALIAS_INDEX.items():
        if len(alias) >= 2 and (alias in key or key in alias):
            return canon
    return None


# ---------------------------------------------------------------- 读取

def _read_excel(path, sheet=None):
    try:
        import openpyxl
    except ImportError:
        sys.exit("缺少 openpyxl：pip install openpyxl")
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet] if sheet else _pick_sheet(wb)
    rows, links = [], []
    for row in ws.iter_rows():
        vals = []
        row_links = []
        for c in row:
            v = c.value
            vals.append("" if v is None else str(v).strip())
            hl = getattr(c, "hyperlink", None)
            row_links.append(hl.target if hl and hl.target else "")
        rows.append(vals)
        links.append(row_links)
    wb.close()
    return rows, links, ws.title


def _pick_sheet(wb):
    """优先选「看起来像岗位表」的 sheet：行数多且表头命中多。"""
    best, best_score = None, -1
    for ws in wb.worksheets:
        score = ws.max_row or 0
        for row in ws.iter_rows(min_row=1, max_row=min(10, ws.max_row or 1), values_only=True):
            hits = sum(1 for v in row if v and map_header(v))
            if hits:
                score += hits * 100
        if score > best_score:
            best, best_score = ws, score
    return best


def _read_csv(path):
    encodings = ["utf-8-sig", "gbk", "utf-8", "latin-1"]
    last = None
    for enc in encodings:
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                rows = [["" if v is None else str(v).strip() for v in r] for r in csv.reader(f)]
            return rows, [[""] * (len(rows[0]) if rows else 0) for _ in rows], enc
        except UnicodeDecodeError as e:
            last = e
    sys.exit("无法解码 CSV（试过 utf-8-sig / gbk / utf-8）：%s" % last)


# ---------------------------------------------------------------- 解析

def detect_header(rows, forced=None):
    """返回 (表头行下标, {标准列: 列下标})。"""
    if forced is not None:
        idx = forced - 1
        if not (0 <= idx < len(rows)):
            sys.exit("--header-row 超出范围")
        return idx, _map_row(rows[idx])
    best_idx, best_map = None, {}
    for i, row in enumerate(rows[:15]):
        m = _map_row(row)
        if len(m) > len(best_map):
            best_idx, best_map = i, m
        if len(m) >= 3:
            return i, m
    if best_idx is None or len(best_map) < 2:
        sys.exit(
            "无法识别表头：前 15 行里没有找到至少 2 个可识别的列名。\n"
            "请用 --header-row N 指定表头所在行（从 1 开始），"
            "或在 references/column-aliases.md 里补充你的列名。"
        )
    return best_idx, best_map


def _map_row(row):
    out = {}
    for j, cell in enumerate(row):
        canon = map_header(cell)
        if canon and canon not in out:      # 同名列取最左
            out[canon] = j
    return out


def extract_url(value, fallback=""):
    """从单元格文本或超链接里取出 URL。"""
    if fallback and re.match(r"https?://", fallback, re.I):
        return fallback.strip()
    if not value:
        return ""
    m = _URL_RE.search(value)
    if m:
        return m.group(0).rstrip(".,;")
    s = str(value).strip()
    if re.match(r"^(www\.|[\w-]+\.(com|cn|net|org|io|co)\b)", s, re.I):
        return "http://" + s
    return ""


def clean_text(value, limit=200):
    s = str(value or "")
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s[:limit]


def is_junk(row_text):
    t = row_text.strip()
    if not t:
        return True
    return any(p.match(t) for p in JUNK_PATTERNS)


def build(rows, links, header_idx, colmap, strict_url=False):
    warnings = []
    out, seen = [], set()
    dropped = {"空行": 0, "合计行": 0, "缺公司": 0, "缺岗位名": 0, "重复": 0, "无链接": 0}

    for i in range(header_idx + 1, len(rows)):
        row = rows[i]
        rlinks = links[i] if i < len(links) else [""] * len(row)
        text_all = " ".join(x for x in row if x)
        if not text_all.strip():
            dropped["空行"] += 1
            continue
        if is_junk(text_all):
            dropped["合计行"] += 1
            continue

        def get(col):
            j = colmap.get(col)
            return row[j] if j is not None and j < len(row) else ""

        def get_link(col):
            j = colmap.get(col)
            return rlinks[j] if j is not None and j < len(rlinks) else ""

        company = clean_text(get("公司"), 120)
        title = clean_text(get("岗位名称"), 160)
        url = extract_url(get("岗位页链接"), get_link("岗位页链接"))

        if not company:
            dropped["缺公司"] += 1
            continue
        if not title:
            dropped["缺岗位名"] += 1
            continue
        if strict_url and not url:
            dropped["无链接"] += 1
            continue

        key = (company, title, url)
        if key in seen:
            dropped["重复"] += 1
            continue
        seen.add(key)

        out.append({
            "公司": company,
            "岗位名称": title,
            "岗位页链接": url,
            "城市": clean_text(get("城市"), 60),
            "职责族": clean_text(get("职责族"), 60),
            "来源校招页": clean_text(get("来源校招页"), 120),
            "_row": i + 1,
        })

    if not colmap.get("城市"):
        warnings.append("未识别到「城市」列：地域筛选将不可用，可在输出后手工补该列。")
    if not colmap.get("岗位页链接"):
        warnings.append("未识别到「岗位页链接」列：该表只能用于浏览，无法扫描/投递。")
    no_url = sum(1 for r in out if not r["岗位页链接"])
    if no_url:
        warnings.append("%d 条岗位没有链接，投递时会被跳过。" % no_url)

    return out, dropped, warnings


# ---------------------------------------------------------------- 输出

def write_xlsx(rows, path):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        sys.exit("缺少 openpyxl：pip install openpyxl")

    wb = Workbook()
    ws = wb.active
    ws.title = "岗位"
    ws.append(STANDARD_COLUMNS)
    for r in rows:
        ws.append([r.get(c, "") for c in STANDARD_COLUMNS])

    head_fill = PatternFill("solid", fgColor="1F3864")
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = head_fill
        c.alignment = Alignment(vertical="center")

    widths = {"公司": 28, "岗位名称": 38, "岗位页链接": 52, "城市": 14, "职责族": 16, "来源校招页": 24}
    for j, name in enumerate(STANDARD_COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(j)].width = widths.get(name, 18)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = "A1:F%d" % (len(rows) + 1)

    wb.save(path)


def main():
    ap = argparse.ArgumentParser(description="职位表 → resume-bot 标准岗位表")
    ap.add_argument("input", help="输入 .xlsx / .xlsm / .csv")
    ap.add_argument("-o", "--output", default="岗位总表.xlsx", help="输出路径（默认 岗位总表.xlsx）")
    ap.add_argument("--sheet", default=None, help="指定工作表名")
    ap.add_argument("--header-row", type=int, default=None, help="表头所在行（从 1 开始）")
    ap.add_argument("--strict-url", action="store_true", help="没有链接的行直接丢弃")
    ap.add_argument("--json", action="store_true", help="只输出 JSON 摘要")
    args = ap.parse_args()

    path = args.input
    if not os.path.isfile(path):
        sys.exit("输入文件不存在：%s" % path)

    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        rows, links, sheet = _read_csv(path)[0], None, "(csv)"
        rows, links, _enc = _read_csv(path)
    elif ext in (".xlsx", ".xlsm"):
        rows, links, sheet = _read_excel(path, args.sheet)
    else:
        sys.exit("只支持 .xlsx / .xlsm / .csv，收到：%s" % ext)

    header_idx, colmap = detect_header(rows, args.header_row)
    items, dropped, warnings = build(rows, links, header_idx, colmap, args.strict_url)

    if not items:
        sys.exit("没有解析出任何有效岗位（丢弃统计：%s）" % json.dumps(dropped, ensure_ascii=False))

    write_xlsx(items, args.output)

    summary = {
        "input": os.path.abspath(path),
        "sheet": sheet,
        "header_row": header_idx + 1,
        "recognized_columns": {k: ("第%d列" % (v + 1)) for k, v in sorted(colmap.items())},
        "missing_columns": [c for c in STANDARD_COLUMNS if c not in colmap],
        "kept": len(items),
        "dropped": {k: v for k, v in dropped.items() if v},
        "with_url": sum(1 for r in items if r["岗位页链接"]),
        "cities": sorted({r["城市"] for r in items if r["城市"]})[:12],
        "output": os.path.abspath(args.output),
        "warnings": warnings,
    }

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return

    print("输入：%s（sheet=%s，表头第 %d 行）" % (summary["input"], sheet, summary["header_row"]))
    print("识别到的列：%s" % "、".join("%s←%s" % (k, v) for k, v in summary["recognized_columns"].items()))
    if summary["missing_columns"]:
        print("缺失的标准列：%s" % "、".join(summary["missing_columns"]))
    print("保留 %d 条，丢弃 %s" % (summary["kept"], json.dumps(summary["dropped"], ensure_ascii=False)))
    if summary["cities"]:
        print("覆盖城市：%s" % "、".join(summary["cities"]))
    for w in warnings:
        print("⚠ " + w)
    print("已写入：%s" % summary["output"])


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
