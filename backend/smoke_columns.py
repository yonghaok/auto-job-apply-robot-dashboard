"""看板（一公司一栏 · 多岗位多 tab）端到端自测。

分两部分：
  A. 走 HTTP 打真实后端：口径分栏、取消岗位/整栏（持久化）。
  B. **同进程**调用 server 的模块函数：一键投递的并发上限、单岗位投递能作用于
     「扫描出来的岗位」。必须同进程，否则 monkeypatch 换不掉 filler.apply_by_url，
     会真的去开浏览器投递真实站点。

  B 部分用独立的临时 state.json，避免污染真实状态。

用法（不联网、不开浏览器）：
    python smoke_columns.py            # **只跑同进程部分**（默认，绝不碰 8000 的真实看板）
    python smoke_columns.py --live     # 额外跑 A 部分：会 hide/unhide 真实看板（危险）
    python smoke_columns.py --offline  # 跳过 A 部分（等价于默认）

⚠️ A 部分是**破坏性**的：它会对真实看板做「取消岗位 / 取消整栏 / 恢复已取消」。
2026-10-28 事故：默认跑 A 段，收尾的 `POST /api/unhide` 把用户手工取消的 15 条记录
一把清空、无法重建。所以现在默认不跑 A；必须显式 `--live`，且收尾按
`GET /api/hidden` 的快照**原样放回**（不再一把清空）。
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 隔离：**不接管调试端口上的 Edge** —— 本机若正开着 CDP 实例（9222），自测会把标签页
# 开到用户的真实浏览器里；自测一律走项目私有的持久化会话（headless）。
os.environ["RESUME_CDP_ENDPOINT"] = "http://127.0.0.1:59999"

PORT = int(os.environ.get("RESUME_PORT", "8000"))
BASE = "http://127.0.0.1:%d" % PORT
OFFLINE = "--offline" in sys.argv
# 打真实看板的开关：必须显式 --live。默认 False —— 自测绝不能改动用户的取消记录。
LIVE = "--live" in sys.argv

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print("   %-4s %s%s" % ("PASS" if cond else "FAIL", name,
                            ("  " + extra) if extra else ""))


def post(path, body=None):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body or {}).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode())


def wait_server(timeout=20):
    for _ in range(timeout * 2):
        try:
            get("/api/stats")
            return True
        except Exception:
            time.sleep(0.5)
    return False


# ============================ A. HTTP 部分 ============================

def restore_hidden(snap):
    """把测试开始前的「取消」名单原样放回（绝不一把清空就收工）。"""
    post("/api/unhide")
    for co in snap.get("companies") or []:
        post("/api/hide", {"company": co})
    for pid in snap.get("positions") or []:
        post("/api/hide", {"id": pid})
    now = get("/api/hidden")
    same = (set(now.get("companies") or []) == set(snap.get("companies") or [])
            and set(now.get("positions") or []) == set(snap.get("positions") or []))
    print("   取消记录复原：%s（公司 %d / 岗位 %d）" % (
        "OK" if same else "不一致！",
        len(now.get("companies") or []), len(now.get("positions") or [])))
    return same


def part_a():
    print("=== A1) 看板口径 & 分栏（HTTP） ===")
    snap = get("/api/hidden")      # 先拍快照：结束时按它原样放回
    print("   开始前已有取消记录：公司 %d / 岗位 %d" % (
        len(snap.get("companies") or []), len(snap.get("positions") or [])))
    post("/api/reload")
    st = get("/api/columns?mode=strict")
    lo = get("/api/columns?mode=loose")
    n_st_co = len(st["columns"])
    n_st_pos = sum(len(c["positions"]) for c in st["columns"])
    n_lo_co = len(lo["columns"])
    n_lo_pos = sum(len(c["positions"]) for c in lo["columns"])
    print("   严格：%d 家公司栏 / %d 个岗位" % (n_st_co, n_st_pos))
    print("   宽松：%d 家公司栏 / %d 个岗位" % (n_lo_co, n_lo_pos))
    check("严格模式有岗位", n_st_pos > 0)
    check("宽松 ⊇ 严格（公司数）", n_lo_co >= n_st_co)
    check("每栏都有至少一个岗位 tab", all(len(c["positions"]) > 0 for c in st["columns"]))
    check("每个岗位都带岗位名与地域字段",
          all("title" in p and "city" in p for c in st["columns"] for p in c["positions"]))
    check("每个岗位都有 id（可单独取消）",
          all(p.get("id") for c in st["columns"] for p in c["positions"]))
    col0 = st["columns"][0]
    check("公司栏带「投递链接」src_pages/links",
          bool(col0.get("src_pages") or col0.get("links")),
          "src_pages=%d links=%d" % (len(col0.get("src_pages") or []),
                                     len(col0.get("links") or [])))
    check("严格口径下每个岗位 level==strict",
          all(p["level"] == "strict" for c in st["columns"] for p in c["positions"]))
    check("宽松模式含非 strict 条目",
          any(p["level"] == "loose" for c in lo["columns"] for p in c["positions"]))
    check("每个岗位带 origin（表格/扫描）",
          all(p.get("origin") in ("表格", "扫描")
              for c in st["columns"] for p in c["positions"]))

    print("\n=== A2) 取消岗位 / 取消整栏 → 移除且持久 ===")
    co0 = col0["company"]
    pid0 = col0["positions"][0]["id"]
    before = len(st["columns"])
    post("/api/hide", {"id": pid0})
    s2 = get("/api/columns?mode=strict")
    aft = [c for c in s2["columns"] if c["company"] == co0]
    check("取消岗位后该岗位已移除",
          (not aft) or all(p["id"] != pid0 for c in aft for p in c["positions"]))
    check("取消岗位后公司栏仍在（还有别的岗位）", len(s2["columns"]) == before)

    post("/api/hide", {"company": co0})
    s3 = get("/api/columns?mode=strict")
    check("取消整栏后该公司不再出现", all(c["company"] != co0 for c in s3["columns"]))
    check("取消整栏后公司栏数 -1", len(s3["columns"]) == before - 1,
          "%d -> %d" % (before, len(s3["columns"])))
    post("/api/reload")
    s4 = get("/api/columns?mode=strict")
    check("重新加载后取消记录仍生效", all(c["company"] != co0 for c in s4["columns"]))
    post("/api/unhide")
    s5 = get("/api/columns?mode=strict")
    check("恢复已取消后公司栏回来", len(s5["columns"]) == before,
          "%d -> %d" % (len(s3["columns"]), len(s5["columns"])))
    check("取消名单原样复原", restore_hidden(snap))

    # 扫描状态端点可用
    sc = get("/api/scan/status")
    check("/api/scan/status 可用", "job" in sc and "summary" in sc)
    aj = get("/api/apply-all/status")
    check("/api/apply-all/status 可用", "running" in aj)


# ============================ B. 同进程部分 ============================

def part_b():
    tmp = tempfile.mkdtemp(prefix="resume-smoke-")
    os.environ["RESUME_STATE"] = os.path.join(tmp, "state.json")
    import server
    import filler
    import positions as posmod
    server.reload()
    print("\n   （同进程，state.json 已隔离到 %s）" % tmp)

    real = filler.apply_by_url
    guard = {"patched": False}

    def make_fake(rec, sleep=0.5, human=False):
        # 签名必须跟 filler.apply_by_url 保持一致（含 sms_info / company / on_code_request /
        # is_cancelled / task_key），否则 server._apply_one_item 传新参数会 TypeError，
        # 被它自己的 except 吞掉、岗位记成「投递失败」，测试只看到「ok=0」而看不出原因。
        # 2026-09-21 漏过 company / on_code_request；2026-09-23 漏过 is_cancelled / task_key。
        def fake(url, title="", profile=None, progress=None, human_wait=600,
                 headless=False, get_code=None, code_wait=300, pos_title="",
                 sms_info=None, auto_submit=True, pause_before_submit=False,
                 confirm_wait=0, company="", on_code_request=None,
                 is_cancelled=None, task_key=""):
            if not guard["patched"]:
                raise AssertionError("fake 未生效，拒绝真的投递！")
            with rec["lock"]:
                rec["cur"] += 1
                rec["peak"] = max(rec["peak"], rec["cur"])
                rec["titles"].append(title)
                rec["pos_keys"].append(pos_title)
                rec["urls"].append(url)
                rec.setdefault("companies", []).append(company)
            if progress:
                progress("已打开岗位页")
                progress("已点击：投递")
            time.sleep(sleep)
            with rec["lock"]:
                rec["cur"] -= 1
            if human:
                return {"needs_human": True, "stage": "人机验证", "detail": "等待人工"}
            return {"needs_human": False, "stage": "已登录", "detail": "假投递完成"}
        return fake

    # **把「假实现签名过期」变成一条显式断言**：这个坑已经炸过两次，而且炸的时候
    # 现象是「ok=0 / 投递失败」，与测试想验证的东西完全无关，极难定位。
    import inspect as _inspect
    _need = set(_inspect.signature(real).parameters)
    _have = set(_inspect.signature(make_fake({})).parameters)
    check("假实现签名覆盖真实 apply_by_url 的全部参数（漏一个就会静默记成投递失败）",
          not (_need - _have), "缺 %s" % sorted(_need - _have))

    # ---------------- B1: 一键投递并发上限 ----------------
    print("\n=== B1) 一键投递：3 并发上限（假实现，绝不开浏览器） ===")
    rec = {"cur": 0, "peak": 0, "titles": [], "pos_keys": [], "urls": [],
           "lock": threading.Lock()}
    filler.apply_by_url = make_fake(rec)
    guard["patched"] = True
    try:
        d = server.columns_payload("strict")
        ids = [p["id"] for c in d["columns"] for p in c["positions"] if p.get("ready")][:7]
        n_co = len(d["columns"])
        print("   发起 %d 个岗位的一键投递（跨 %d 家公司，请求 concurrency=3）" % (len(ids), n_co))
        r = server.start_apply_all(ids, "strict", 3)
        check("一键投递启动成功", r.get("ok"), str(r)[:120])
        # 2026-10-28 单窗口：即便请求传 3，也必须被压成 1（用户要求「不再并发」）
        check("请求 concurrency=3 被压成 1（单窗口）", r.get("concurrency") == 1,
              "r.concurrency=%s" % r.get("concurrency"))
        t0 = time.time()
        while time.time() - t0 < 60 and server.APPLY_JOB.get("running"):
            time.sleep(0.2)
        j = server.APPLY_JOB
        print("   任务结果：total=%s done=%s ok=%s fail=%s human=%s" %
              (j["total"], j["done"], j["ok"], j["fail"], j["human"]))
        print("   并发峰值=%d，实际投递=%d" % (rec["peak"], len(rec["titles"])))
        check("并发峰值恰好为 1（单窗口生效）", rec["peak"] == 1, "peak=%d" % rec["peak"])
        check("全部 7 个岗位都被投递", j["done"] == len(ids),
              "done=%s ids=%d" % (j["done"], len(ids)))
        check("全部记为成功", j["ok"] == len(ids), "ok=%s" % j["ok"])
        check("投递用到了「投递链接」而非空 url", all(u for u in rec["urls"]),
              "样例=%s" % (rec["urls"][0][:60] if rec["urls"] else "-"))

        cols = server.columns_payload("strict")
        hit = [p for c in cols["columns"] for p in c["positions"] if p["id"] in ids]
        check("投递后岗位状态变为已登录",
              all(p["status"] == "已登录" for p in hit),
              "样例=%s" % (hit[0]["status"] if hit else "-"))
        check("已登录岗位 ready=False（不会重复投）",
              all(p["ready"] is False for p in hit))
        r2 = server.start_apply_all(ids, "strict", 3)
        check("重复一键投递被拒绝", not r2.get("ok"), str(r2)[:90])
    finally:
        filler.apply_by_url = real
        guard["patched"] = False

    # ---------------- B2: 人工验证路径计入 human ----------------
    print("\n=== B2) 遇到人机验证 → 该岗位标「需人工验证」 ===")
    rec2 = {"cur": 0, "peak": 0, "titles": [], "pos_keys": [], "urls": [],
            "lock": threading.Lock()}
    filler.apply_by_url = make_fake(rec2, sleep=0.2, human=True)
    guard["patched"] = True
    try:
        d = server.columns_payload("strict")
        ids2 = [p["id"] for c in d["columns"] for p in c["positions"] if p.get("ready")][:2]
        if ids2:
            server.start_apply_all(ids2, "strict", 3)
            t0 = time.time()
            while time.time() - t0 < 30 and server.APPLY_JOB.get("running"):
                time.sleep(0.2)
            check("需人工验证计数正确", server.APPLY_JOB["human"] == len(ids2),
                  "human=%s" % server.APPLY_JOB["human"])
            cols = server.columns_payload("strict")
            hit = [p for c in cols["columns"] for p in c["positions"] if p["id"] in ids2]
            check("岗位状态为需人工验证",
                  all(p["status"] == "需人工验证" for p in hit))
            check("需人工验证的岗位不会再次排队",
                  all(p["ready"] is False for p in hit))
        else:
            check("B2 前置：有可投递岗位", False)
    finally:
        filler.apply_by_url = real
        guard["patched"] = False

    # ---------------- B3: 单岗位投递作用于「扫描出来的岗位」 ----------------
    print("\n=== B3) 单岗位投递可作用于「扫描出来的岗位」 ===")
    server.reload()
    row = server.CANDIDATES[0]
    url = (row.get("source") or row.get("url") or "").strip()
    co = row.get("company", "")
    with server.LOCK:
        server.SCANS[url] = {
            "url": url, "company": co, "ts": server._now(), "is_list": True,
            "count": 2, "raw_count": 2, "dropped": 0,
            "positions": [
                {"title": "产品经理（自测）", "raw_title": "产品经理（自测）",
                 "degree": "本科", "city": "北京", "meta": "本科 北京"},
                {"title": "AI产品经理（自测）", "raw_title": "AI产品经理（自测）",
                 "degree": "本科", "city": "杭州", "meta": "本科 杭州"},
            ],
        }
        server._save_board_locked()
    pid = posmod.position_id(co, "产品经理（自测）", url)
    cols = server.columns_payload("strict")
    scanned = [p for c in cols["columns"] if c["company"] == co
               for p in c["positions"] if p["origin"] == "扫描"]
    check("扫描岗位出现在公司栏里", any(p["id"] == pid for p in scanned),
          "扫到 %d 个扫描岗位" % len(scanned))
    check("扫描岗位带地域与学历",
          all(p.get("city") for p in scanned) and all(p.get("degree") for p in scanned))

    rec3 = {"cur": 0, "peak": 0, "titles": [], "pos_keys": [], "urls": [],
            "lock": threading.Lock()}
    filler.apply_by_url = make_fake(rec3, sleep=0.1)
    guard["patched"] = True
    try:
        r = server.start_apply_one(pid, "strict")
        check("扫描岗位可以单独投递（不再是 not found）", r.get("ok"), str(r)[:110])
        t0 = time.time()
        while time.time() - t0 < 20:
            cs = server.columns_payload("strict")
            me = [p for c in cs["columns"] for p in c["positions"] if p["id"] == pid]
            if me and me[0]["status"] == "已登录":
                break
            time.sleep(0.2)
        cs = server.columns_payload("strict")
        me = [p for c in cs["columns"] for p in c["positions"] if p["id"] == pid]
        check("扫描岗位状态已更新为已登录", bool(me) and me[0]["status"] == "已登录",
              "status=%s" % (me[0]["status"] if me else "-"))
        check("投递时带上了「岗位行定位关键字」",
              rec3["pos_keys"] and rec3["pos_keys"][0] == "产品经理（自测）",
              "pos_title=%r" % (rec3["pos_keys"][0] if rec3["pos_keys"] else None))
        check("投递链接用的是公司列表页", rec3["urls"] and rec3["urls"][0] == url,
              "url=%s" % (rec3["urls"][0][:60] if rec3["urls"] else "-"))
    finally:
        filler.apply_by_url = real
        guard["patched"] = False


def part_c():
    """stage → 看板状态 的映射（含「停在提交前」新增的两种状态）。"""
    import server
    import positions as posmod
    print("\n=== C) apply_by_url 的 stage 如何落到看板状态 ===")
    m = server._stage_status
    check("'投递成功' → 已投递", m("投递成功") == "已投递", m("投递成功"))
    check("'投递成功（人工确认）' → 已投递（你点的提交也算投出去了）",
          m("投递成功（人工确认）") == "已投递", m("投递成功（人工确认）"))
    check("'已投递过' → 已投递", m("已投递过") == "已投递", m("已投递过"))
    check("'已填表待确认' → 待人工提交（**不是**已投递，也不是需人工验证）",
          m("已填表待确认", needs_human=True) == "待人工提交",
          m("已填表待确认", needs_human=True))
    check("无映射 + needs_human → 需人工验证", m("验证码未到", needs_human=True) == "需人工验证")
    check("无映射 + 不算人工 → 已登录（旧行为保留）", m("已登录") == "已登录")
    check("DONE_STATES 含「待人工提交」→ 不会重复排队",
          "待人工提交" in posmod.DONE_STATES, str(sorted(posmod.DONE_STATES)))
    check("DONE_STATES 含「已投递」", "已投递" in posmod.DONE_STATES)
    check("PRESELECT_STATES 含「待人工提交」",
          "待人工提交" in server.PRESELECT_STATES)


def main():
    if not OFFLINE and LIVE:
        if not wait_server():
            print("后端未运行（%d），只跑同进程部分" % PORT)
        else:
            part_a()
    elif not OFFLINE:
        print("跳过 A 部分（默认不打真实看板；需要时显式加 --live）")
    part_b()
    part_c()
    ok = sum(1 for _n, v in RESULTS if v)
    print("\n================ 汇总 ================")
    for n, v in RESULTS:
        if not v:
            print("  FAILED: %s" % n)
    print("  %d/%d PASS" % (ok, len(RESULTS)))
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
