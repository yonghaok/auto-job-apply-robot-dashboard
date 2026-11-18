"""候选岗位加载器（断点3 岗位预选与匹配）。

直接从 Task C 产出的「六城_岗位总表.xlsx」读取已匹配岗位，
每条 (公司, 岗位名称, 岗位页链接) 生成一个候选投递任务。
"""
import os
import hashlib
import openpyxl

DEFAULT_XLSX = os.environ.get(
    "RESUME_XLSX",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "岗位总表.xlsx"),
)


def _make_id(company, title, url):
    key = "{}|{}|{}".format(company, title, url)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _first_city(city):
    if not city:
        return ""
    # 多值如 "上海、北京" 或 "北京/上海"
    for sep in ("、", "/", ","):
        if sep in city:
            return city.split(sep)[0].strip()
    return city.strip()


def load_candidates(xlsx_path=DEFAULT_XLSX):
    """返回候选任务列表，每条带默认 status=pending。"""
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    header = next(rows)
    idx = {name: i for i, name in enumerate(header)}

    out = []
    for r in rows:
        if r is None:
            continue
        company = r[idx["公司"]] if "公司" in idx else None
        title = r[idx["岗位名称"]] if "岗位名称" in idx else None
        url = r[idx["岗位页链接"]] if "岗位页链接" in idx else None
        family = r[idx["职责族"]] if "职责族" in idx else None
        city = r[idx["城市"]] if "城市" in idx else None
        source = r[idx["来源校招页"]] if "来源校招页" in idx else None
        if not company or not title:
            continue
        out.append({
            "id": _make_id(company, title, url),
            "company": company,
            "title": (title or "").replace("\n", " ").replace("\r", " ").strip(),
            "url": url or "",
            "family": family or "",
            "city": city or "",
            "city_first": _first_city(city),
            "source": source or "",
            "status": "pending",
            "entry_url": "",
            "decision": "",
            "note": "",
        })
    wb.close()
    return out


if __name__ == "__main__":
    c = load_candidates()
    print("loaded", len(c), "candidates")
    for x in c[:3]:
        print(x["id"], x["company"], x["title"][:30], x["city_first"], x["status"])
