# -*- coding: utf-8 -*-
"""取消类按钮「点了确认却没取消」的离线自测（T63）。

背景（用户 2026-09-23 报）：取消类按钮点确认后取消动作未执行。

根因（本地按真实数据复现，非猜测）：**同一岗位在看板上变成了两个同名条目**。
  表格行的 `url` 是**岗位详情页**（.../job/fa8bb107-...），
  同页扫出来的那条 `url` 是**列表页**（.../jobs?...）。
  两者岗位名完全相同 → `build_columns` 的 `(title, url)` 去重**天然拦不住**
  → 看板同一个公司栏里出现两个名字一模一样的 tab（tab 上只看得到岗位名+地域，
  看不出区别）。点 ✕ 只会隐藏其中一个 id，另一个还在 →
  用户看到的就是「点了确认却没取消」。
  更严重的是：「一键投递」按看板可见项派发，会把用户已经取消掉的那个岗位**投出去**。

实测（2026-09-23 线上看板，loose 94 家/187 岗）：**13 组**同题多条目，其中 9 组
是「表格 + 扫描」成对，且 9/9 满足 `表格行.src_page == 扫描行.url`（合并规则的前提）。

覆盖：
  A) `_norm_title` 归一化（空白/全角空格/零宽字符/大小写）
  B) 同一来源页 + 同一岗位名 → 合并为 1 条，且保留表格行（详情页链接 + 状态都在它身上）
  C) **取消必须生效**：取消表格行 id、取消扫描行 id（两个方向）都要让岗位彻底消失
  D) **不许过度合并**：来源页不同仍是两条；表格行在当前口径不显示时不压制扫描行
  E) 真实线上抓到的 4 组重复样本回放（FunPlus / 作业帮 x2 / 国机数科）
  F) 源码结构断言（前端取消不再静默失败；合并只在「本行自己的来源页」上生效）

用法（cwd=backend）：<venv-python> smoke_cancel_dup.py
"""
import inspect
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import positions as posmod

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


FRONTEND = os.path.join(HERE, "..", "frontend", "index.html")


def row(company, title, url, src, rid, raw=None, city="", status="pending"):
    return {"company": company, "title": title, "url": url, "source": src,
            "id": rid, "raw_title": raw if raw is not None else title,
            "city": city, "status": status, "note": ""}


def scan(url, titles, meta="", city="北京"):
    return {url: {"url": url, "is_list": True,
                  "positions": [{"title": t, "raw_title": t, "meta": meta,
                                 "city": city} for t in titles]}}


KWS = ["产品经理", "本科"]
CTS = ["北京"]


def main():
    # ------------------------------------------------------------------ A
    print("\n=== A. _norm_title 归一化 ===")
    nt = posmod._norm_title
    check("连续空格折叠", nt("产品  经理") == "产品 经理", repr(nt("产品  经理")))
    check("全角空格当空白", nt("产品\u3000经理") == nt("产品 经理"), repr(nt("产品\u3000经理")))
    check("零宽字符当空白", nt("产品\u200b经理") == nt("产品 经理"), repr(nt("产品\u200b经理")))
    check("首尾空白去掉", nt("  产品经理  ") == "产品经理", repr(nt("  产品经理  ")))
    check("大小写折叠", nt("SDK方向") == nt("sdk方向"), repr(nt("SDK方向")))
    check("None / 空串不炸", nt(None) == "" and nt("") == "")

    # ------------------------------------------------------------------ B
    print("\n=== B. 同一来源页 + 同一岗位名 → 合并 1 条 ===")
    P = "https://app.mokahr.com/campus-recruitment/funplus01/147931#/jobs?page=1"
    D = "https://app.mokahr.com/campus-recruitment/funplus01/147931#/job/fa8bb107-98ab-45a8-bf46-0e54e83f8393"
    T = "27校招-广告产品经理"
    base = row("FunPlus", T, D, P, "a04b698480c5", city="上海、北京、广州")
    scs = scan(P, [T], meta="全职 | 产品类 | 北京市")
    scan_id = posmod.position_id("FunPlus", T, P)

    d1 = posmod.build_columns([base], scs, mode=posmod.MODE_LOOSE, kws=KWS, cts=CTS)
    ps = d1["columns"][0]["positions"] if d1["columns"] else []
    check("合并后只有 1 条（原来 2 条同名 tab）", len(ps) == 1, "len=%d" % len(ps))
    check("保留的是表格行（带详情页链接）",
          bool(ps) and ps[0].get("origin") == "表格" and ps[0]["url"] == D,
          repr(ps[0].get("origin")) if ps else "none")
    check("列表计数不再虚高（counts.loose == 1）",
          bool(d1["columns"]) and d1["columns"][0]["counts"]["loose"] == 1,
          repr(d1["columns"][0]["counts"]) if d1["columns"] else "no col")

    # 旧键为什么拦不住：证明根因真实存在（不是 url 写错）
    check("根因复现：(title,url) 旧去重键对这两条**不相等**",
          (T, D) != (T, P), "旧键应当不同")

    # ------------------------------------------------------------------ C
    print("\n=== C. 取消必须生效（两个方向的 id 都认） ===")
    d2 = posmod.build_columns([base], scs, hidden_pos=[base["id"]],
                              mode=posmod.MODE_LOOSE, kws=KWS, cts=CTS)
    n2 = sum(len(c["positions"]) for c in d2["columns"])
    check("取消【表格行】id → 整条消失（扫描那条不许回填）", n2 == 0, "剩 %d 条" % n2)

    d3 = posmod.build_columns([base], scs, hidden_pos=[scan_id],
                              mode=posmod.MODE_LOOSE, kws=KWS, cts=CTS)
    n3 = sum(len(c["positions"]) for c in d3["columns"])
    check("取消【扫描行】id（历史遗留 id）→ 表格行也一起消失", n3 == 0, "剩 %d 条" % n3)

    # 反例守卫：不取消时必须还在（防止「恒空」式假修复）
    check("未取消时该岗位仍在（不是恒空）", len(ps) == 1)

    # ------------------------------------------------------------------ D
    print("\n=== D. 不许过度合并 ===")
    Q = "https://other.example.com/xyz"
    # (1) 扫描缓存挂在**该公司根本没引用**的来源页上 → 那条扫描与本公司无关，不参与合并
    base_q = row("FunPlus", T, D, Q, "a04b698480c5", city="北京")
    d4 = posmod.build_columns([base_q], scs, mode=posmod.MODE_LOOSE, kws=KWS, cts=CTS)
    n4 = sum(len(c["positions"]) for c in d4["columns"])
    check("扫描页未被该公司引用 → 不参与合并（该行原样保留 1 条）", n4 == 1, "剩 %d 条" % n4)

    # (2) 同一家公司两行来自不同来源页，其中一行与扫描页同源 →
    #     只压制「同源那一对」的扫描行，另一行不受连累（合并是**按行**生效的）
    DA = "https://other.example.com/xyz/job/AAA"
    DB = "https://app.mokahr.com/campus-recruitment/funplus01/147931#/job/BBB"
    rA = row("X公司", T, DA, Q, "idA", city="北京")
    rB = row("X公司", T, DB, P, "idB", city="北京")
    d4b = posmod.build_columns([rA, rB], scs, mode=posmod.MODE_LOOSE, kws=KWS, cts=CTS)
    psb = d4b["columns"][0]["positions"] if d4b["columns"] else []
    check("两行分属不同来源页 → 各自保留（2 条），扫描那条被压制",
          len(psb) == 2 and all(p.get("origin") == "表格" for p in psb),
          "n=%d origins=%s" % (len(psb), [p.get("origin") for p in psb]))

    # 表格行自身在 strict 下不达标（raw 里没有「本科」），但扫描那条的卡面里有 →
    # 合并后按**并集**定级：严格口径下这个岗位必须还在（不许丢），且只有一条。
    base_loose = row("作业帮", "产品经理-27秋招", D, P, "a433de1e5157",
                     raw="产品经理-27秋招 发布 全职 产品类", city="北京")
    scs2 = scan(P, ["产品经理-27秋招"], meta="本科 | 产品 | 北京市")
    ds = posmod.build_columns([base_loose], scs2, mode=posmod.MODE_STRICT, kws=KWS, cts=CTS)
    pss = ds["columns"][0]["positions"] if ds["columns"] else []
    check("strict 下也只有 1 条（合并与请求口径无关）", len(pss) == 1, "n=%d" % len(pss))
    _kws_hit = (pss[0].get("kw_in_title") or []) + (pss[0].get("kw_in_other") or []) if pss else []
    check("strict 下岗位没丢（扫描卡面的「本科」并进来了）",
          "本科" in _kws_hit and pss[0].get("origin") == "表格",
          "kw=%s origin=%s" % (_kws_hit, pss[0].get("origin") if pss else "-"))
    check("合并条目标了留痕 merged_scanned",
          bool(pss) and pss[0].get("merged_scanned") is True)
    dl = posmod.build_columns([base_loose], scs2, mode=posmod.MODE_LOOSE, kws=KWS, cts=CTS)
    psl = dl["columns"][0]["positions"] if dl["columns"] else []
    check("loose 下同样是 1 条（以表格行为代表）",
          len(psl) == 1 and psl[0].get("origin") == "表格",
          "n=%d origin=%s" % (len(psl), psl[0].get("origin") if psl else "-"))
    check("totals 与请求口径无关（两种口径下计数完全一致）",
          ds["totals"] == dl["totals"], "%s vs %s" % (ds["totals"], dl["totals"]))

    # 整栏取消不受影响
    d5 = posmod.build_columns([base], scs, hidden_co=["FunPlus"],
                              mode=posmod.MODE_LOOSE, kws=KWS, cts=CTS)
    check("整栏取消（hidden_co）仍有效", d5["columns"] == [],
          repr(d5["columns"])[:60])

    # ------------------------------------------------------------------ E
    print("\n=== E. 真实线上重复样本回放（2026-09-23 抓取自运行中的看板） ===")
    L = "https://app.mokahr.com/campus-recruitment/funplus01/147931#/jobs?276338%5B0%5D=2027%E5%B1%8A%E6%A0%A1%E6%8B%9B&page=1&anchorName=jobsList"
    Y = "https://app.mokahr.com/campus_apply/zuoyebang/144908#/jobs"
    G = "https://gjshk.zhaopin.com/xJobs/index.html"
    SAMPLES = [
        ("FunPlus", "27校招-广告产品经理",
         "https://app.mokahr.com/campus-recruitment/funplus01/147931#/job/fa8bb107-98ab-45a8-bf46-0e54e83f8393",
         L, "a04b698480c5", "163189b80147", "上海、北京、广州", "北京"),
        ("作业帮", "产品经理-27秋招",
         "https://app.mokahr.com/campus_apply/zuoyebang/144908#/job/7990b555-c4a5-4923-840c-67c53b2a7d20",
         Y, "a433de1e5157", "2634be217dc7", "北京", "北京"),
        ("作业帮", "平台产品经理（作业帮）-27秋招",
         "https://app.mokahr.com/campus_apply/zuoyebang/144908#/job/a3745bc1-b121-4bdd-8b13-27bf2d2dcb79",
         Y, "b27cb4353886", "6dd2201968ae", "北京", "北京"),
        ("国机数科", "产品经理助理",
         "https://xiaoyuan.zhaopin.com/job/CCL1472344680J40960875508",
         G, "d1aa2a91e15d", "445afbbc19d5", "北京、南京", "南京"),
    ]
    for co, ti, durl, spage, rid, sid, bcity, scity in SAMPLES:
        # 线上那条扫描 id 是不是就是 position_id(公司, 岗位名, 列表页)？
        check("%s / %s：线上扫描 id 与本模块算法一致" % (co, ti[:12]),
              posmod.position_id(co, ti, spage) == sid,
              "%s != %s" % (posmod.position_id(co, ti, spage), sid))
        r = row(co, ti, durl, spage, rid, city=bcity)
        s = scan(spage, [ti], meta="全职 | 本科 | 产品", city=scity)
        e1 = posmod.build_columns([r], s, mode=posmod.MODE_LOOSE, kws=KWS, cts=CTS)
        np1 = sum(len(c["positions"]) for c in e1["columns"])
        check("%s / %s：合并为 1 条" % (co, ti[:12]), np1 == 1, "n=%d" % np1)
        e2 = posmod.build_columns([r], s, hidden_pos=[rid],
                                  mode=posmod.MODE_LOOSE, kws=KWS, cts=CTS)
        np2 = sum(len(c["positions"]) for c in e2["columns"])
        check("%s / %s：点 ✕ 取消后彻底消失" % (co, ti[:12]), np2 == 0, "n=%d" % np2)

    # ------------------------------------------------------------------ F
    print("\n=== F. 源码结构断言 ===")
    psrc = inspect.getsource(posmod.build_columns)
    check("合并只在「本行自己的来源页」上生效（own == u）", "own == u" in psrc)
    check("两个方向的 id 都认（把扫描 id 也算进来）",
          "position_id(co, m[nt], scan_pages.get(u, u))" in psrc)
    check("被取消的表格行会把扫描那条一起压制（occupied 在 continue 之前登记）",
          psrc.index("occupied.add(twin)") < psrc.index('seen.add((p["title"], p["url"]))\n            positions.append(p)'))
    check("压制**与口径无关**（base 循环里不再引用 mode，计数不会随口径漂移）",
          "mode" not in psrc.split("for p in _base_positions(rs, co):")[1].split("scan_stat = ")[0])
    check("扫描那条的信息并进表格行（并集定级，岗位不丢）",
          'p["match_text"] = ((p.get("match_text") or "") + " " + extra).strip()' in psrc)
    check("扫描行按 (来源页, 归一化岗位名) 判重", '(u, _norm_title(p["title"])) in occupied' in psrc)

    html = open(FRONTEND, encoding="utf-8").read()
    check("前端新增 hideRequest 并在失败时弹错（不再静默失败）",
          "async function hideRequest(" in html and 'askError("取消失败：' in html)
    check("取消岗位 / 取消整栏 都校验了 ok",
          html.count('askError("取消失败：') >= 2,
          "count=%d" % html.count('askError("取消失败：'))
    check("两次取消都走 hideRequest", "hideRequest({ id })" in html and "hideRequest({ company })" in html)
    # 2026-10-28 事故：原生 confirm 在某些宿主里「弹窗在、点确定没反应」→ 破坏性操作悬空。
    check("取消不再依赖原生 confirm（改页内确认框）",
          "confirm(`不投递" not in html and 'id="askModal"' in html)
    check("确认框失败后不关窗（可重试）",
          "function askError(" in html and "askBusy(false)" in html)

    print("\n===== 汇总：PASS=%d FAIL=%d =====" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
