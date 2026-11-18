# -*- coding: utf-8 -*-
"""smoke: 2026-09-23 四项改动。

  A「阻碍性提示」识别：站点限制类文案（只允许投递一个岗位 / 次数上限 / 暂不可投递）
  B 滑块：gap_x 统一为「缺口**中心**」；拖动位移 = 缺口相对**拼图块起点**的位移
       （不是「缺口绝对 x − 手柄中心 x」，后者少拖约半个手柄宽）
  C 投递串行：并发默认 1（严格串行）
  D 每 tab「标记为未投递」端点 + Edge CDP 接管端点 + 前端渲染

**不碰真实数据**：段 D 用临时 xlsx + 临时 state + 临时 profiles 根 + 随机端口。
用法（cwd=backend）：<venv-python> smoke_serial_pending_edge.py
"""
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
sys.path.insert(0, HERE)

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "|", name, ("| " + detail) if detail else "")


def info(msg):
    print("INFO |", msg)


# ==================== A. 阻碍性提示识别 ====================
import filler  # noqa: E402


class _TextPage:
    def __init__(self, text):
        self._t = text

    def evaluate(self, _js):
        return self._t


cases = [
    ("只允许投递一个岗位", "blocked"),
    ("您的投递次数已达上限", "blocked"),
    ("今日投递次数已用完，请明天再试", "blocked"),
    ("该职位暂不可投递", "blocked"),
    ("只允许投递一个职位", "blocked"),
    ("请填写姓名", "invalid"),
    ("您已投递过该职位", "dup"),
    ("投递成功", "success"),
    ("页面上一段无关文案", ""),
]
for txt, want in cases:
    got = filler._submit_evidence(_TextPage(txt))[0]
    check("识别 %-16s -> %-8s" % (txt[:16], want or "(空)"), got == want, "got=%r" % got)

# 「已投递过」与「限制类」是两种语义：前者算已投递，后者算投递失败
check("BLOCKER_HINTS 覆盖「只允许投递一个岗位」",
      "只允许投递一个岗位" in filler.BLOCKER_HINTS)

# ==================== B. 滑块 ====================
import captcha  # noqa: E402


def make_png(w, h):
    """够 _png_size 读 IHDR 的最小 PNG（不依赖 PIL）。"""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00"
    return sig + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr \
        + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))


# B1) 模板匹配必须返回「缺口中心」而不是左边缘（源码级，cv2 缺失也能查）
src = open(os.path.join(HERE, "captcha.py"), encoding="utf-8").read()
check("captcha: 模板匹配换算成中心后返回",
      "center = best[0] + best[2] // 2" in src and '"gap_x": center' in src)
check("captcha: 不再把左边缘当 gap_x",
      '"gap_x": best[0]' not in src)

# B2) 若有 cv2，用合成图真跑一遍，验证返回的是中心
if captcha.cv_available():
    import cv2  # noqa: E402
    import numpy as np  # noqa: E402
    rng = np.random.default_rng(7)
    bg = rng.integers(30, 60, size=(180, 360, 3), dtype=np.uint8)
    cv2.rectangle(bg, (220, 60), (280, 120), (250, 250, 250), -1)   # 缺口中心 x=250
    piece = np.zeros((60, 60, 4), np.uint8)
    piece[:, :, 0:3] = 250
    piece[:, :, 3] = 255
    ok_bg, bg_png = cv2.imencode(".png", bg)
    ok_pc, piece_png = cv2.imencode(".png", piece)
    if ok_bg and ok_pc:
        try:
            r = captcha._solve_slider_cv({"bg_png": bg_png.tobytes(),
                                          "piece_png": piece_png.tobytes()})
            gx = r.get("gap_x")
            check("合成图：模板法 gap_x 落在缺口中心附近(250±8)",
                  gx is not None and abs(gx - 250) <= 8,
                  "gap_x=%s method=%s" % (gx, r.get("method")))
        except Exception as e:  # noqa: BLE001
            check("合成图：模板法可解", False, "%s: %s" % (type(e).__name__, e))
    else:
        info("cv2.imencode 失败，跳过合成图测试")
else:
    info("cv2 不可用，跳过合成图测试（已用源码级断言覆盖口径）")

# B3) 拖动位移：位移 = 缺口相对拼图块起点的位移
class _Mouse:
    def __init__(self):
        self.moves = []
        self.down_at = None
        # 注意：不能叫 self.up —— 会遮蔽同名方法 up()，调用时报
        # "'bool' object is not callable"（本次就踩了）
        self.up_called = False

    def move(self, x, y):
        self.moves.append((round(x), round(y)))

    def down(self):
        self.down_at = self.moves[-1] if self.moves else None

    def up(self):
        self.up_called = True


class _DragPage:
    def __init__(self):
        self.mouse = _Mouse()
        self.waits = []

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def is_closed(self):
        return False


class _El:
    def __init__(self, box):
        self._b = box

    def bounding_box(self):
        return dict(self._b)


BG = {"x": 100.0, "y": 200.0, "width": 360.0, "height": 180.0}
PIECE = {"x": 100.0, "y": 200.0, "width": 60.0, "height": 60.0}
HANDLE = {"x": 100.0, "y": 380.0, "width": 40.0, "height": 40.0}
FAKE_W = {"el": _El(BG), "box": dict(BG), "bg": _El(BG), "piece": _El(PIECE),
          "frame": None, "bg_png": make_png(360, 180), "piece_png": None}

_orig = (filler._find_verify_widget, filler._find_handle,
         filler.detect_human_verification, filler._code_sent, captcha.solve)
filler._find_verify_widget = lambda page: dict(FAKE_W)
filler._find_handle = lambda el, box: dict(HANDLE)
filler.detect_human_verification = lambda page: "滑块验证"   # 一直"存在"→ 判定失败即可
filler._code_sent = lambda page: False
captcha.solve = lambda ctx: {"via": "cv", "type": "drag", "kind": "slider",
                             "gap_x": 250, "method": "test"}

notes = []
dp = _DragPage()
try:
    ret = filler._attempt_auto_verify(dp, attempts=1, note=notes.append)
finally:
    (filler._find_verify_widget, filler._find_handle, filler.detect_human_verification,
     filler._code_sent, captcha.solve) = _orig

# 期望：图宽 360→scale 1；gap 页面 x = 100+250 = 350；拼图块起点 = 100+30 = 130
#       位移 = 220；手柄中心 from_x = 120 → target_x = 340
check("未通过验证时 _attempt_auto_verify 返回 False", ret is False)
x0 = dp.mouse.moves[0][0] if dp.mouse.moves else None
xN = dp.mouse.moves[-1][0] if dp.mouse.moves else None
check("按下位置 = 手柄中心 x=120", x0 == 120, "x0=%s" % x0)
check("落点 = 手柄中心 + 位移 = 340（旧实现会得到 350-120=230 的错位）",
      xN == 340, "xN=%s moves=%s" % (xN, dp.mouse.moves[:3]))
check("拖动结束有 mouse.up", dp.mouse.up_called is True)
check("日志写明了位移 220px",
      any("位移 220px" in n for n in notes), "notes=%s" % notes[:3])

# ==================== C. 投递串行 ====================
import server  # noqa: E402

check("APPLY_JOB 初始并发 = 1", server.APPLY_JOB.get("concurrency") == 1,
      str(server.APPLY_JOB.get("concurrency")))

seen = {}
_orig_collect = server._collect_apply_items
_orig_worker = server._apply_all_worker
server._collect_apply_items = lambda ids, mode: [
    {"pid": "p1", "title": "t1", "url": "u1", "company": "c1"}]
server._apply_all_worker = lambda items, conc: seen.update(conc=conc, n=len(items))


def _restore_server():
    server._collect_apply_items = _orig_collect
    server._apply_all_worker = _orig_worker
    with server.LOCK:
        server.APPLY_JOB["running"] = False


try:
    r = server.start_apply_all()
    check("start_apply_all 默认返回 concurrency=1",
          r.get("concurrency") == 1, json.dumps(r, ensure_ascii=False))
    for _ in range(60):
        if "conc" in seen:
            break
        time.sleep(0.05)
    check("_apply_all_worker 实际收到 conc=1", seen.get("conc") == 1, str(seen))
    check("串行：一个岗位一个 worker 批次", seen.get("n") == 1, str(seen))
finally:
    _restore_server()

# 交人工必须**驻留**（T64 起的新契约）：两条投递入口都**不得**再传 human_wait=0。
# 旧契约（T59 时代）恰恰相反——那时两条入口都显式传 human_wait=0，本意「别卡住」，
# 实际效果是「窗口立刻关闭、人根本没机会过验证」。不传 → 走默认预算 _handoff_budget()。
ssrc = open(os.path.join(HERE, "server.py"), encoding="utf-8").read()
# 只看**代码行**：server.py 里有一行注释专门回顾这段历史（写着 human_wait=0），
# 不能把注释也算成「传了 human_wait」（否则断言会把「讲清教训的注释」判成违规）。
_scode = "\n".join(l for l in ssrc.splitlines() if not l.lstrip().startswith("#"))
check("两条投递入口都**不再**传 human_wait=0（T64：驻留等人工）",
      "human_wait" not in _scode, "code_hits=%d" % _scode.count("human_wait"))
check("server.py 两个 apply_by_url 入口都不带 human_wait（用默认预算）",
      ssrc.count("filler.apply_by_url(") >= 2 and "human_wait=" not in _scode,
      "apply_by_url=%d" % ssrc.count("filler.apply_by_url("))
# 默认预算的语义：0 / None / 负数 **不再等于放弃**，一律回落 HUMAN_HANDOFF_WAIT；
# T77b 起默认值 = 0（不设超时）——「回落」的含义变成「回落到不设超时」。
check("_handoff_budget(0/None/负数) 回落到 HUMAN_HANDOFF_WAIT（默认 0 = 不超时）",
      filler.HUMAN_HANDOFF_WAIT == 0
      and filler._handoff_budget(0) == 0
      and filler._handoff_budget(None) == 0
      and filler._handoff_budget(-5) == 0
      and filler._handoff_budget(600) == 600,
      "budget=%s wait=%s" % (filler._handoff_budget(0), filler.HUMAN_HANDOFF_WAIT))

# ==================== D. 隔离 HTTP：mark-pending / edge-cdp ====================
CO, TITLE, URL = "烟雾测试公司", "AI产品经理", "https://example.com/job/1"
TMP = tempfile.mkdtemp(prefix="smoke_spe_")
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


def req(method, path, body=None, timeout=15):
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
        return None
    col = next((c for c in (jload(b).get("columns") or []) if c.get("company") == company), None)
    if not col or not col.get("positions"):
        return None
    return col["positions"][0]


proc = None
try:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["公司", "岗位名称", "岗位页链接", "职责族", "城市", "来源校招页"])
    ws.append([CO, TITLE, URL, "产品", "上海", "https://example.com/list"])
    wb.save(XLSX)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({}, f)

    env = dict(os.environ)
    env.update({
        "RESUME_PORT": str(PORT),
        "RESUME_STATE": STATE,
        "RESUME_XLSX": XLSX,
        "RESUME_PROFILES": os.path.join(TMP, "profiles"),   # 别碰真实 browser_profiles
        "RESUME_SMS": os.path.join(TMP, "sms.json"),
        "RESUME_SMS_HIST": os.path.join(TMP, "sms_hist.json"),
        "RESUME_SMS_REJECTED": os.path.join(TMP, "sms_rejected.jsonl"),
        "RESUME_ACCESS_LOG": os.path.join(TMP, "access.log"),
        "RESUME_POS_FILTER": json.dumps({"keywords": ["产品"], "cities": ["上海"]}),
    })
    proc = subprocess.Popen([PY, "-u", "server.py"], cwd=HERE, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    up = False
    boot = b""
    for _ in range(120):
        s, _b = req("GET", "/api/columns?mode=strict", timeout=2)
        if s == 200:
            up = True
            break
        if proc.poll() is not None:
            boot = proc.stdout.read() or b""
            break
        time.sleep(0.25)
    check("隔离服务器就绪", up, "" if up else boot.decode("utf-8", "replace")[:300])

    if up:
        pos = find_pos(CO)
        check("隔离岗位存在", pos is not None)
        if pos:
            pid = pos["id"]
            # 先置成「已投递」，再用「标记为未投递」切回
            s, b = req("POST", "/api/preselect/%s/human-done" % pid, {})
            check("先置为已投递", jload(b).get("status") == "已投递", b.decode("utf-8", "replace"))

            s, b = req("POST", "/api/preselect/%s/mark-pending" % pid, {})
            j = jload(b)
            check("mark-pending -> 200/ok/status=pending",
                  s == 200 and j.get("ok") is True and j.get("status") == "pending",
                  json.dumps(j, ensure_ascii=False))

            pos2 = find_pos(CO)
            check("回读：状态回到 pending", (pos2 or {}).get("status") == "pending",
                  str((pos2 or {}).get("status")))
            check("回读：ready=True（可重新投递）", (pos2 or {}).get("ready") is True)

            with open(STATE, "r", encoding="utf-8") as f:
                st2 = json.load(f)
            rec = ((st2.get("_pos_state") or {}).get(pid) or {})
            check("落盘：_pos_state 记为 pending 且备注已换成人工标记",
                  rec.get("status") == "pending" and "人工标记" in (rec.get("note") or ""),
                  json.dumps(rec, ensure_ascii=False)[:160])

            s, b = req("POST", "/api/preselect/__nope__/mark-pending", {})
            check("未知 id -> ok=False/not found",
                  jload(b).get("ok") is False and jload(b).get("error") == "not found",
                  b.decode("utf-8", "replace"))

        # CDP 接管状态（只读）
        s, b = req("GET", "/api/edge-cdp/status")
        j = jload(b)
        check("GET /api/edge-cdp/status -> 200", s == 200, "code=%s" % s)
        for k in ("endpoint", "port", "cdp_ready", "browser", "edge_running",
                  "edge_exe", "profile_dir", "main_edge_udd", "profile", "headless"):
            check("edge-cdp/status 含字段 %s" % k, k in j)
        info("endpoint=%s  cdp_ready=%s  edge_running=%s"
             % (j.get("endpoint"), j.get("cdp_ready"), j.get("edge_running")))
        check("默认调试端口 = 9222", j.get("port") == 9222, str(j.get("port")))
        check("endpoint 指向本机回环地址",
              str(j.get("endpoint") or "").startswith("http://127.0.0.1:"),
              str(j.get("endpoint")))
        check("不再有旧的「快照同步」字段（seed_exists 等已移除）",
              "seed_exists" not in j and "seed_cookies" not in j, str(list(j.keys())))

        # 启动端点：Edge 在跑但没开调试端口时必须**明确拒绝**，不静默降级
        s, b = req("POST", "/api/edge-cdp/launch", {"close_edge": False})
        j = jload(b)
        check("POST /api/edge-cdp/launch 返回结构化结果（含 ok 字段）", "ok" in j,
              json.dumps(j, ensure_ascii=False)[:200])
        if j.get("ok") is False and j.get("edge_running"):
            check("Edge 在跑但无调试端口时明确拒绝并说明原因",
                  "调试端口" in (j.get("error") or ""), str(j.get("error"))[:120])
        else:
            info("skip「运行时拒绝」：ok=%s edge_running=%s（本机可能已带端口运行）"
                 % (j.get("ok"), j.get("edge_running")))

    # 前端静态
    html = open(os.path.join(HERE, "..", "frontend", "index.html"), encoding="utf-8").read()
    anchor = html.find("function renderPane")
    end = html.find("return pane;", anchor) if anchor >= 0 else -1
    seg = html[anchor:end] if (anchor >= 0 and end > anchor) else html
    check("每个 tab 都渲染「标记为未投递」按钮", 'data-act="to-pending"' in seg)
    check("按钮绑定到 markPending", "markPending(p.id, p.title)" in html)
    check("前端调用 /mark-pending 端点", "/mark-pending`" in html)
    check("工具栏有「启动 Edge（继承登录态）」按钮", 'id="btn-edgecdp"' in html)
    check("前端调用 /api/edge-cdp/launch", '"/api/edge-cdp/launch"' in html)
    check("前端已彻底移除旧的「同步 cookie 快照」逻辑",
          "edge-sync" not in html and "edgeSync" not in html)
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
