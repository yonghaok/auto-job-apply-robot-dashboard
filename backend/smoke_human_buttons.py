# -*- coding: utf-8 -*-
"""smoke: 人工介入按钮 —— 「进入窗口（置前）」/「确认已投递」。

两层验证，都不需要联网、不需要真实浏览器：

  A. 纯逻辑：filler._maybe_focus 的**一次性**语义。
     横线事件由 HTTP 线程置位、由投递线程在等待循环里消费；必须满足
     「没请求就不置前」「请求一次只置前一次」「已消费不会被下个岗位误用」。
     用假的 page 对象记录 bring_to_front 调用次数即可判定。

  B. 隔离 HTTP：临时 xlsx + 临时 state.json + **独立端口**起 server.py，
     验证 /api/preselect/<id>/focus 与 /api/preselect/<id>/human-done：
       · focus  -> {"ok":true,"requested":true}
       · human-done -> 状态翻转为「已投递」+ 落盘 + ready=False（不再重复排队）
       · POS_STATE 里的扫描岗位（不在 xlsx 里）也能被人工确认
       · 未知 id -> {"ok":false,"error":"not found"}
     全程 **不触碰** 真实 state.json / 真实 xlsx / 8000 端口。

用法（cwd=backend）：
  <venv-python> smoke_human_buttons.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "|", name, ("| " + detail) if detail else "")


# ============================ A. 纯逻辑：_maybe_focus ============================
sys.path.insert(0, HERE)
import filler  # noqa: E402


class _FakePage:
    """只要能记 bring_to_front 调用次数即可。"""

    def __init__(self, boom=False):
        self.calls = 0
        self.boom = boom

    def bring_to_front(self):
        self.calls += 1
        if self.boom:
            raise RuntimeError("window already gone")


filler.set_task_key("")
filler._FOCUS_PENDING.clear()
p = _FakePage()
check("未请求时 _maybe_focus 返回 False", filler._maybe_focus(p) is False)
check("未请求时不会调用 bring_to_front", p.calls == 0, "calls=%d" % p.calls)

check("request_focus('') 返回 False（空 key 宁可不动，也不去动别家窗口）",
      filler.request_focus("") is False)
check("request_focus('pid-A') 返回 True", filler.request_focus("pid-A") is True)

# 关键：并发下「别的岗位」不得消费本岗位的置前请求
p_other = _FakePage()
filler.set_task_key("pid-B")
check("别的岗位 _maybe_focus 返回 False（不串窗）", filler._maybe_focus(p_other) is False)
check("别的岗位窗口未被置前", p_other.calls == 0, "calls=%d" % p_other.calls)
check("本岗位的请求仍被保留（未被误消费）", "pid-A" in filler._FOCUS_PENDING)

# 本岗位才置前
p_a = _FakePage()
filler.set_task_key("pid-A")
check("本岗位 _maybe_focus 返回 True", filler._maybe_focus(p_a) is True)
check("本岗位 bring_to_front 恰好调用一次", p_a.calls == 1, "calls=%d" % p_a.calls)
check("一次性：请求已被消费", "pid-A" not in filler._FOCUS_PENDING)
check("一次性：第二次 _maybe_focus 返回 False", filler._maybe_focus(p_a) is False)
check("一次性：不会重复置前", p_a.calls == 1, "calls=%d" % p_a.calls)

# 未登记 task_key 的线程（旧调用方没传 pid）一律不置前
filler.request_focus("pid-C")
filler.set_task_key("")
p_nokey = _FakePage()
check("未登记 task_key 的线程不置前", filler._maybe_focus(p_nokey) is False)
check("未登记 task_key 时不调用 bring_to_front", p_nokey.calls == 0)
filler._FOCUS_PENDING.clear()

# bring_to_front 抛异常不外抛，且请求照样消费掉（不残留）
filler.request_focus("pid-D")
filler.set_task_key("pid-D")
p_boom = _FakePage(boom=True)
check("bring_to_front 抛异常时不外抛，返回 False", filler._maybe_focus(p_boom) is False)
check("异常分支也已消费请求（不残留）", "pid-D" not in filler._FOCUS_PENDING)

filler.set_task_key("")
filler._FOCUS_PENDING.clear()

# ============================ B. 隔离 HTTP ============================
CO = "烟雾测试公司"
TITLE = "AI产品经理"
URL = "https://example.com/job/1"
FAKE_ID = "isolatedfake01"   # 只存在于预置 POS_STATE 里（模拟扫描出来的岗位）

TMP = tempfile.mkdtemp(prefix="smoke_human_btn_")
STATE = os.path.join(TMP, "state.json")
XLSX = os.path.join(TMP, "pos.xlsx")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


PORT = _free_port()
BASE = "http://127.0.0.1:%d" % PORT
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def req(method, path, body=None, timeout=10):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with _opener.open(r, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001
        return -1, str(e).encode("utf-8", "replace")


def jload(b):
    try:
        return json.loads(b)
    except Exception:  # noqa: BLE001
        return {}


def find_pos(company):
    s, b = req("GET", "/api/columns?mode=strict")
    if s != 200:
        return None, None
    col = next((c for c in (jload(b).get("columns") or []) if c.get("company") == company), None)
    if not col or not col.get("positions"):
        return None, None
    return col["positions"][0], jload(b)


proc = None
try:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["公司", "岗位名称", "岗位页链接", "职责族", "城市", "来源校招页"])
    ws.append([CO, TITLE, URL, "产品", "上海", "https://example.com/list"])
    wb.save(XLSX)

    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"_pos_state": {FAKE_ID: {"status": "需人工验证", "note": "预置：等待人工核验",
                                            "updated": "2026-09-23 00:00:00"}}},
                  f, ensure_ascii=False)

    env = dict(os.environ)
    env.update({
        "RESUME_PORT": str(PORT),
        "RESUME_STATE": STATE,
        "RESUME_XLSX": XLSX,
        "RESUME_SMS": os.path.join(TMP, "sms.json"),
        "RESUME_SMS_HIST": os.path.join(TMP, "sms_hist.json"),
        "RESUME_SMS_REJECTED": os.path.join(TMP, "sms_rejected.jsonl"),
        "RESUME_ACCESS_LOG": os.path.join(TMP, "access.log"),
        # 关键词/城市走环境变量覆盖，避免依赖本机 match_rules.json
        "RESUME_POS_FILTER": json.dumps({"keywords": ["产品"], "cities": ["上海"]}),
    })
    proc = subprocess.Popen([PY, "-u", "server.py"], cwd=HERE, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    boot_log = b""
    up = False
    for _ in range(120):
        s, _b = req("GET", "/api/columns?mode=strict", timeout=2)
        if s == 200:
            up = True
            break
        if proc.poll() is not None:
            boot_log = proc.stdout.read() or b""
            break
        time.sleep(0.25)
    check("隔离服务器就绪（独立端口 %d）" % PORT, up,
          "" if up else boot_log.decode("utf-8", "replace")[:300])
    if not up:
        raise RuntimeError("isolated server did not come up")

    pos, payload = find_pos(CO)
    check("隔离 xlsx 的岗位出现在 /api/columns",
          pos is not None,
          "columns=%s" % [c.get("company") for c in (payload or {}).get("columns") or []][:5])
    if pos is None:
        raise RuntimeError("isolated position not found in columns")
    pid = pos["id"]
    check("岗位初始状态 pending", pos.get("status") == "pending", str(pos.get("status")))
    check("岗位初始 ready=True", pos.get("ready") is True)

    # --- focus ---
    s, b = req("POST", "/api/preselect/%s/focus" % pid, {})
    j = jload(b)
    check("POST /api/preselect/<id>/focus -> 200", s == 200, "code=%s" % s)
    check("focus 返回 ok=True 且 requested=True",
          j.get("ok") is True and j.get("requested") is True,
          json.dumps(j, ensure_ascii=False))

    # 空 pid 必须诚实拒绝（宁可不置前，也不能去动别家岗位的窗口）
    s, b = req("POST", "/api/preselect//focus", {})
    j = jload(b)
    check("空 pid 的 focus -> ok=False",
          s == 200 and j.get("ok") is False, json.dumps(j, ensure_ascii=False))

    # --- human-done 成功路径 ---
    s, b = req("POST", "/api/preselect/%s/human-done" % pid, {})
    j = jload(b)
    check("POST /api/preselect/<id>/human-done -> 200", s == 200, "code=%s" % s)
    check("human-done 返回 ok=True", j.get("ok") is True, json.dumps(j, ensure_ascii=False))
    check("human-done 返回 status=已投递", j.get("status") == "已投递", str(j.get("status")))

    pos2, _ = find_pos(CO)
    check("回读 /api/columns：状态翻转为 已投递",
          (pos2 or {}).get("status") == "已投递", str((pos2 or {}).get("status")))
    check("回读 /api/columns：ready=False（不会被一键投递重复排队）",
          (pos2 or {}).get("ready") is False)
    check("回读 /api/columns：note 记录人工确认来源",
          "人工确认" in ((pos2 or {}).get("note") or ""), str((pos2 or {}).get("note")))

    with open(STATE, "r", encoding="utf-8") as f:
        st2 = json.load(f)
    disk = ((st2.get("_pos_state") or {}).get(pid) or {})
    check("落盘：隔离 state.json 的 _pos_state 已写为 已投递",
          disk.get("status") == "已投递",
          json.dumps(st2.get("_pos_state", {}), ensure_ascii=False)[:180])

    # --- 扫描岗位（只在 POS_STATE、不在 xlsx）也可确认 ---
    s, b = req("POST", "/api/preselect/%s/human-done" % FAKE_ID, {})
    j = jload(b)
    check("POS_STATE 里的扫描岗位也能被人工确认",
          s == 200 and j.get("ok") is True, json.dumps(j, ensure_ascii=False))

    # --- 未知 id 必须诚实拒绝 ---
    s, b = req("POST", "/api/preselect/__nope__/human-done", {})
    j = jload(b)
    check("未知 id -> ok=False / error=not found",
          s == 200 and j.get("ok") is False and j.get("error") == "not found",
          json.dumps(j, ensure_ascii=False))

    # --- 前端静态检查：按钮必须在「需人工验证」区块内、且绑定到对应处理函数 ---
    with open(os.path.join(HERE, "..", "frontend", "index.html"), encoding="utf-8") as f:
        html = f.read()
    def branch_seg(status):
        """取 `if (p.status === "<status>") { ... }` 渲染分支块的精确切片。

        锚点必须带左花括号：文件里另有一处**统计代码**
        `if (p.status === "需人工验证") nHuman++;` 也叫这个名字，
        不带 `{` 会抓到它，导致断言恒假。
        块尾用「行首缩进 2 空格的 }」定位（块内嵌套的 } 都不在行首）。
        """
        a = html.find('if (p.status === "%s") {' % status)
        if a < 0:
            return ""
        e = html.find("\n  }", a)
        return html[a:e] if e > a else html[a:a + 1800]

    for st in ("需人工验证", "待人工提交"):
        seg = branch_seg(st)
        check("前端有「%s」渲染分支" % st, bool(seg))
        check("「%s」分支内渲染「进入窗口」+「确认已投递」两个按钮" % st,
              'data-act="focus"' in seg and 'data-act="human-done"' in seg)
    check("两按钮分别绑定到 focusWindow / humanDone",
          "focusWindow(p.id, p.title)" in html and "humanDone(p.id, p.title)" in html)
    check("前端调用 /focus 与 /human-done 两个端点",
          "/focus`" in html and "/human-done`" in html)
finally:
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
    shutil.rmtree(TMP, ignore_errors=True)

passed = sum(1 for _n, c in results if c)
total = len(results)
print("\n%d/%d passed" % (passed, total))
sys.exit(0 if passed == total else 1)
