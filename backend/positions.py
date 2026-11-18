"""岗位分栏聚合：把扁平的 (公司, 岗位, 链接) 行聚成「一公司一栏 · 多岗位多 tab」。

纯函数模块（不碰网络、不碰磁盘），便于单测。被 server.py 调用。

三个筛选口径（用户指定关键词「产品经理」「本科」+ 地域命中 7 城之一）：
  strict  关键词**全部**出现 且 地域命中
  loose   关键词**任一**出现 且 地域命中
  all     不做关键词/地域过滤（表格行本来就已按城市筛选过）

表格行（来源=表格）是人工/上游筛过的，但仍按同一口径判定，保证「看到的都是
符合你说的条件」；扫出来的行（来源=扫描）一律走这个口径。
"""
import hashlib

import filler

MODE_STRICT = "strict"
MODE_LOOSE = "loose"
MODE_ALL = "all"
# 对外只提供两个口径：严格（关键词全含）/ 宽松（关键词任一）。
# MODE_ALL 仅作为「不过滤」的内部用法保留，不放给前端——否则一屏 1500+ 岗位，
# 「一键投递」会误伤一大批。
MODES = (MODE_STRICT, MODE_LOOSE, MODE_ALL)
# 已投递成功 / 正在投递的，不允许被「一键投递」重复排队
DONE_STATES = {"已投递", "已登录", "投递中", "需人工验证", "待人工提交"}


def position_id(company, title, url):
    """岗位的稳定 id（同一公司+岗位名+链接恒定，重复扫描不会产生新条目）。"""
    return hashlib.sha1(("%s|%s|%s" % (company, title, url)).encode("utf-8")).hexdigest()[:12]


def classify(title, raw, city, kws, cts):
    """判定一条岗位的口径，并说明**关键词是在哪命中**的。

    列表页的「岗位行」文本 = 表格里的 raw_title（卡面文字）。学历/城市这类信息
    常常只出现在卡面元信息里而不在岗位名里，所以关键词要在整块卡面文字上匹配；
    但为了不误导，这里把「标题里就有」和「只在卡面其它文字里」分开返回。
    """
    kws = [k for k in (kws or []) if k]
    cts = [c for c in (cts or []) if c]
    title = title or ""
    raw = raw or ""
    in_title = [k for k in kws if k in title]
    in_other = [k for k in kws if k not in title and k in raw]
    kw_all = len(in_title) + len(in_other) == len(kws)
    kw_any = bool(in_title or in_other)
    city_ok = (not cts) or any(c in (city or "") for c in cts)
    if kw_all and city_ok:
        level = MODE_STRICT
    elif kw_any and city_ok:
        level = MODE_LOOSE
    else:
        level = "none"
    return {"level": level, "kw_in_title": in_title, "kw_in_other": in_other,
            "city_ok": bool(city_ok)}


def match_level(text, city, kws, cts):
    """兼容旧签名的简化判定：text 当作卡面全文。"""
    return classify("", text, city, kws, cts)["level"]


def _level_ok(level, mode):
    if mode == MODE_ALL:
        return True
    if mode == MODE_LOOSE:
        return level in (MODE_STRICT, MODE_LOOSE)
    return level == MODE_STRICT


def _base_positions(rows, company):
    """把表格行转成岗位条目（清洗标题、抽地域），按 (标题, 链接) 去重。"""
    out = []
    seen = set()
    for r in rows:
        raw = (r.get("title") or "").strip()
        title = filler.clean_title(raw)
        if not title and filler.is_placeholder_title(raw):
            title = "（岗位名未采集）"
        url = (r.get("url") or "").strip()
        key = (title, url)
        if key in seen:
            continue
        seen.add(key)
        city = (r.get("city") or "").strip() or filler._extract_cities(raw)
        out.append({
            "id": r.get("id") or position_id(company, title, url),
            "title": title or "（未命名岗位）",
            "raw_title": raw,
            # 关键词匹配用的文本：表格行 = 卡面文字（学历/城市往往只在这里）
            "match_text": raw,
            # 投递时用于在列表页锁定该行：表格行是岗位详页/卡面文字，不做行匹配
            "pos_key": "",
            "city": city,
            "degree": "",
            "url": url,
            # 注意：候选行的 `source` 是「来源校招页」链接，不是来源标签
            "origin": "表格",
            "src_page": (r.get("source") or "").strip(),
            "status": r.get("status", "pending"),
            "note": r.get("note", ""),
            "entry_url": r.get("entry_url", ""),
            "placeholder": filler.is_placeholder_title(raw) or not title,
        })
    return out


def _scanned_positions(scan, company):
    """把某个链接的扫描结果转成岗位条目。"""
    out = []
    for p in (scan or {}).get("positions") or []:
        title = (p.get("title") or "").strip() or "（未命名岗位）"
        url = (scan or {}).get("url") or ""
        out.append({
            "id": position_id(company, title, url),
            "title": title,
            "raw_title": p.get("raw_title") or title,
            # 扫描行：整个岗位行文本（含学历/地域元信息）都可参与关键词匹配
            "match_text": "%s %s" % (p.get("raw_title") or title, p.get("meta") or ""),
            # 扫描出来的岗位要回列表页点「投递」，用岗位行首行做定位关键字
            "pos_key": (p.get("raw_title") or title),
            "city": (p.get("city") or "").strip(),
            "degree": (p.get("degree") or "").strip(),
            "url": url,
            "origin": "扫描",
            "src_page": url,
            "status": "pending",
            "note": "",
            "entry_url": "",
            "placeholder": False,
            "why": p.get("why") or "",
        })
    return out


def _norm_title(t):
    """岗位名归一化：**仅用于「是不是同一个岗位」的比对**，不改显示文本。

    空白差异（连续空格 / 全角空格 / 零宽字符）会让 (title, url) 去重失效，
    所以比对前一律折叠。
    """
    s = t or ""
    for ch in ("\u3000", "\u200b", "\ufeff"):
        s = s.replace(ch, " ")
    return " ".join(s.split()).strip().lower()


def build_columns(rows, scans=None, hidden_pos=None, hidden_co=None,
                  mode=MODE_STRICT, kws=None, cts=None, pos_state=None):
    """聚合成公司栏。

    rows:    候选行（表格 + 手动添加），每条含 company/title/url/city/status/...
    scans:   {url: {"url":..., "positions":[...], "is_list":bool, "error":str}}
    hidden_pos: 被「取消该岗位」的 position id 集合
    hidden_co:  被「取消该公司栏」的公司名集合
    mode:    strict / loose（all 仅内部用）
    pos_state: {position_id: {"status","note"}} —— 扫描出来的岗位不在表格里，
               它们的投递状态只能存这里；表格行的状态来自 rows，这里会覆盖它。

    返回 {"mode","keywords","cities","columns":[...],"totals":{...}}
      column = {"company","links":[...],"counts":{"strict","loose","all"},
                "scan":{"scanned":n,"errors":[...]},"positions":[...]}
    """
    scans = scans or {}
    pos_state = pos_state or {}
    hidden_pos = set(hidden_pos or ())
    hidden_co = set(hidden_co or ())
    _dk, _dc = (kws, cts) if (kws is not None and cts is not None) else filler.load_match_rules()
    kws = [k for k in (kws if kws is not None else _dk) if k]
    cts = [c for c in (cts if cts is not None else _dc) if c]

    order = []
    grouped = {}
    for r in rows:
        co = (r.get("company") or "").strip() or "未命名"
        if co not in grouped:
            grouped[co] = []
            order.append(co)
        grouped[co].append(r)

    # 扫描结果按 host+path 归到公司（同一链接可能被多公司引用，理论罕见）
    url2company = {}
    for co, rs in grouped.items():
        for r in rs:
            u = (r.get("url") or "").strip()
            if u:
                url2company.setdefault(u, co)

    columns = []
    totals = {m: {"positions": 0, "companies": 0} for m in MODES}

    for co in order:
        rs = grouped[co]
        if co in hidden_co:
            continue

        links = []
        src_pages = []
        for r in rs:
            u = (r.get("url") or "").strip()
            if u and u not in links:
                links.append(u)
            s = (r.get("source") or "").strip()
            if s and s not in src_pages:
                src_pages.append(s)
        # 扫描缓存既可能挂在「投递链接（来源校招页）」上，也可能挂在岗位页链接上
        scan_keys = src_pages + [x for x in links if x not in src_pages]

        # 「同一个岗位」的认定。表格行的 url 是**岗位详情页**，同页扫出来的那条 url 是
        # **列表页**，所以 (title, url) 去重永远拦不住 → 同一岗位在看板上变成两个同名
        # tab（tab 上只看得到岗位名+地域，看不出区别）。用户点 ✕ 取消其中一个，另一个
        # 还在，看起来就是「点了确认却没取消」，更糟的是「一键投递」还会把那条投出去。
        # 这里按「**同一来源列表页** + 同一岗位名」认定同一岗位：
        #   ① 渲染只保留表格行（它带详情页链接，投递定位更准、状态也在它身上）；
        #   ② 取消任一方的 id，另一方一起消失 —— 两个方向算出来的 id 都认。
        scan_titles = {}      # 扫描页 url -> {规范化标题: 扫描记录里的原始标题}
        scan_pages = {}       # 扫描页 url -> 扫描记录自报的 url（算 id 用，与 _scanned_positions 一致）
        scan_pos = {}         # (扫描页 url, 规范化标题) -> 扫描记录里的那条岗位
        for u in scan_keys:
            sc = scans.get(u)
            if not sc or sc.get("error"):
                continue
            scan_pages[u] = (sc.get("url") or u)
            m = {}
            for p in (sc.get("positions") or []):
                t = (p.get("title") or "").strip() or "（未命名岗位）"
                m[_norm_title(t)] = t
                scan_pos[(u, _norm_title(t))] = p
            scan_titles[u] = m

        positions = []
        seen = set()
        occupied = set()      # (扫描页 url, 规范化标题)：已由表格行代表的岗位
        for p in _base_positions(rs, co):
            nt = _norm_title(p["title"])
            own = (p.get("src_page") or "").strip()
            variants = {p["id"]}
            twin = None
            for u, m in scan_titles.items():
                if own == u and nt in m:
                    # 用户可能在「扫描」那条上点的取消 → 那个 id 也要认
                    variants.add(position_id(co, m[nt], scan_pages.get(u, u)))
                    twin = (u, nt)
            if twin is not None:
                # 命中「同一来源页 + 同一岗位名」→ 这个岗位只保留表格行那一条，
                # **无条件**压制扫描行（不随当前口径变化，否则同一份数据的计数会随
                # 口径漂移，且严格口径下会变回两条）。
                occupied.add(twin)
                # 但扫描那条的信息不能丢：并进表格行的匹配文本里再定级，
                # 这样「只在扫描卡面里命中关键词」的岗位在严格口径下也不会少。
                sp = scan_pos.get(twin) or {}
                extra = " ".join([x for x in [(sp.get("raw_title") or ""), (sp.get("meta") or "")] if x])
                if extra:
                    p["match_text"] = ((p.get("match_text") or "") + " " + extra).strip()
                if not (p.get("city") or "").strip() and (sp.get("city") or "").strip():
                    p["city"] = sp["city"]
                if not (p.get("degree") or "").strip() and (sp.get("degree") or "").strip():
                    p["degree"] = sp["degree"]
                p["merged_scanned"] = True     # 留痕：这条是「表格 + 扫描」合并来的
            if variants & hidden_pos:
                continue
            seen.add((p["title"], p["url"]))
            positions.append(p)

        scan_stat = {"scanned": 0, "errors": [], "lists": 0}
        for u in scan_keys:
            sc = scans.get(u)
            if not sc:
                continue
            if sc.get("error"):
                scan_stat["errors"].append("%s：%s" % (u[:60], str(sc["error"])[:60]))
                continue
            if sc.get("is_list"):
                scan_stat["lists"] += 1
            for p in _scanned_positions(sc, co):
                if (u, _norm_title(p["title"])) in occupied:
                    continue          # 同一岗位已由表格行代表（该行被取消时也不放回来）
                if p["id"] in hidden_pos:
                    continue
                if (p["title"], p["url"]) in seen:
                    continue
                seen.add((p["title"], p["url"]))
                positions.append(p)
            scan_stat["scanned"] += len(sc.get("positions") or [])

        # 逐条判定口径（关键词在「岗位名」与「卡面其它文字」上分别记录）
        for p in positions:
            rec = pos_state.get(p["id"])
            if rec:                      # 扫描岗位的状态只存在 pos_state 里
                p["status"] = rec.get("status", p["status"])
                p["note"] = rec.get("note", "")
            info = classify(p.get("title"), p.get("match_text"), p.get("city"), kws, cts)
            p.update(info)
            p.pop("match_text", None)     # 内部字段，不必回传前端
            p["ready"] = p.get("status") not in DONE_STATES

        counts = {m: len([p for p in positions if _level_ok(p["level"], m)])
                  for m in MODES}
        for m in MODES:
            if counts[m]:
                totals[m]["companies"] += 1
            totals[m]["positions"] += counts[m]

        shown = [p for p in positions if _level_ok(p["level"], mode)]
        if not shown:
            continue
        # strict 排前面，其次 loose，便于「一键投递」优先投最匹配的
        rank = {MODE_STRICT: 0, MODE_LOOSE: 1, "none": 2}
        shown.sort(key=lambda p: (rank.get(p["level"], 3), p["title"]))
        columns.append({
            "company": co,
            "links": links,
            "src_pages": src_pages,     # 「投递链接」= 该公司的岗位列表页
            "counts": counts,
            "scan": scan_stat,
            "positions": shown,
        })

    return {"mode": mode, "keywords": kws, "cities": cts,
            "columns": columns, "totals": totals}
