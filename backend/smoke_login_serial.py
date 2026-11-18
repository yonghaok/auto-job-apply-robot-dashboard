# -*- coding: utf-8 -*-
"""smoke_login_serial.py —— 验证「登录串行化 + 会话持久化 + 取码窗口」三项修复。

覆盖：
  A profile_path_for 确定性 / 按公司唯一 / 名字清洗
  B _lock_for 同键同锁、异键异锁
  C _acquire 抢不到时返回 False 且有心跳；释放后可再取
  D **登录段全局串行**：3 个不同公司并发 → 登录段互不重叠          ★核心
  D2 同一断言的**证伪对照组**：把闸门换成 no-op → 必须能测出重叠   ★证明断言有牙
  E **同公司会话复用**：已是登录态 → 整段跳过登录（不占闸门、不发短信）
  F 同公司并发 → 会话锁保证串行（不会两个实例抢同一个 profile 目录）
  G on_code_request 在「点了获取验证码」之后被真实调用
  H 页面上没有登录表单 → 不再白等 300s，直接返回继续投递
  I 人机验证转人工 → 闸门必须释放（否则后续所有登录被堵死）
  J server.latest_code(skip_consumed=True) 跳过已消费的验证码
  K **取码硬时间下限**：早于「点了获取验证码」的短信一律不取（思特奇把 TEST 自检
    短信当新码的真实故障复现）+ on_code_request 推后窗口且「只推第一次」（重发沿用）

跑法（必须用 venv，需 openpyxl）：
  export PATH="/usr/bin:/bin:/c/Windows/System32:/c/Windows:$PATH"
  python smoke_login_serial.py
"""
import io
import json
import os
import sys
import tempfile
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 隔离：**不接管调试端口上的 Edge** —— 本机若正开着 CDP 实例（9222），自测会把标签页
# 开到用户的真实浏览器里；自测一律走项目私有的持久化会话（headless）。
os.environ["RESUME_CDP_ENDPOINT"] = "http://127.0.0.1:59999"

PASS, FAIL = [], []


def ck(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name, ("  — " + extra) if extra else ""))


# ==========================================================================
# 0. 假 Playwright + 假 page（不启动真浏览器）
# ==========================================================================
class FakePage:
    def __init__(self, **kw):
        self.has_phone = kw.get("has_phone", True)
        self.sent = kw.get("sent", True)
        self.captcha = kw.get("captcha", False)
        self.waits = []
        self.notes = []

    def on(self, *a, **k):
        pass

    def goto(self, url, **k):
        self.url = url

    def wait_for_timeout(self, ms):
        self.waits.append(ms)
        # 真睡一点点：让并发交错真的可能发生，否则串行断言没有意义
        time.sleep(min(ms / 1000.0, 0.004))


class FakeCtx:
    def __init__(self, page):
        self._page = page
        self.pages = []

    def new_page(self):
        return self._page

    def close(self):
        pass


def install_fakes(page, code_after=2):
    """把 filler 里所有依赖真实页面的助手替换成假实现。"""
    import filler

    state = {"code_calls": 0, "caps": []}

    def fake_get_code():
        state["code_calls"] += 1
        time.sleep(0.015)          # 模拟「等短信」占用登录段，制造真实竞争
        return "654321" if state["code_calls"] > code_after else ""

    filler._fill_first = lambda pg, labels, val, *a, **k: pg.has_phone
    filler._agree_terms = lambda pg: "已勾选《隐私协议》"
    filler._terms_state = lambda pg: "checked"
    filler._code_btn_disabled = lambda pg: False
    filler._click_by_text = lambda pg, texts, timeout_ms=3000: (
        "获取验证码" if (pg.sent and any("获取验证码" in t for t in texts)) else "")
    filler._click_apply_in_row = lambda pg, kws: ""      # 不碰真实 DOM
    filler._code_sent = lambda pg: pg.sent
    filler._click_exact = lambda pg, kw: True
    filler._attempt_auto_verify = lambda pg, attempts=3, note=None: False
    filler.detect_human_verification = lambda pg: ("滑块验证" if pg.captcha else "")
    filler._launch_persistent = lambda p, pdir, headless, note: FakeCtx(page)
    filler.load_profile = lambda: {"phone": "13800138000", "name": "测试"}
    filler.fill_page = lambda pg, prof, note=None: (_ for _ in ()).throw(
        RuntimeError("fake fill_page 未实现（本测试不覆盖填表）"))

    # _apply_core 内部是局部 import，必须替换模块属性而不是 filler 的属性
    import playwright.sync_api as psa

    class _CM:
        def __enter__(self):
            return types.SimpleNamespace()

        def __exit__(self, *a):
            return False

    psa.sync_playwright = lambda *a, **k: _CM()
    return state, fake_get_code


class RecGate:
    """可记录持有区间的闸门替身。noop=True 时不做互斥（用于证伪对照）。"""

    def __init__(self, noop=False):
        self.noop = noop
        self._l = threading.Lock()
        self.spans = []            # [(tid, t0, t1)]
        self._t0 = {}

    def acquire(self, blocking=True, timeout=-1):
        if self.noop:
            ok = True
        elif timeout is not None and timeout != -1:
            ok = self._l.acquire(timeout=timeout)
        else:
            ok = self._l.acquire(blocking)
        if ok:
            self._t0[threading.get_ident()] = time.perf_counter()
        return ok

    def release(self):
        tid = threading.get_ident()
        self.spans.append((tid, self._t0.pop(tid), time.perf_counter()))
        if not self.noop:
            self._l.release()

    @staticmethod
    def overlaps(spans):
        s = sorted(spans, key=lambda x: x[1])
        return any(s[i][1] < s[i - 1][2] for i in range(1, len(s)))


import filler  # noqa: E402

print("=" * 72)
print("A/B/C 基础件")
print("=" * 72)

p1 = filler.profile_path_for("税友软件")
p2 = filler.profile_path_for("税友软件")
p3 = filler.profile_path_for("思特奇")
ck("A1 同公司路径确定", p1 == p2)
ck("A2 不同公司不同目录", p1 != p3, "%s / %s" % (os.path.basename(p1), os.path.basename(p3)))
ck("A3 公司名被清洗且可读", "税友软件" in os.path.basename(p1), os.path.basename(p1))
ck("A4 空公司名不炸", bool(filler.profile_path_for("")), filler.profile_path_for(""))
ck("A5 含路径分隔符的公司名被清洗",
   os.sep not in os.path.basename(filler.profile_path_for("a/b\\c:d")),
   os.path.basename(filler.profile_path_for("a/b\\c:d")))

ck("B1 同键同锁", filler._lock_for("k1") is filler._lock_for("k1"))
ck("B2 异键异锁", filler._lock_for("k1") is not filler._lock_for("k2"))

lk = threading.Lock()
lk.acquire()
notes = []
t0 = time.perf_counter()
got = filler._acquire(lk, notes.append, "测试闸门", tick=1, max_wait=2)
el = time.perf_counter() - t0
ck("C1 抢不到 → 返回 False", got is False)
ck("C2 等待期间有心跳 note", len(notes) >= 1, "notes=%r" % notes[:2])
ck("C3 确实等过而不是立刻返回", el >= 1.0, "elapsed=%.2fs" % el)
lk.release()
ctr = threading.Lock()
ck("C4 释放后可再取", filler._acquire(ctr, notes.append, "x", tick=1, max_wait=2) is True)

# ==========================================================================
print()
print("=" * 72)
print("D 登录段全局串行（核心）")
print("=" * 72)


def run_concurrent(gate, companies, page_kw=None, code_after=2):
    filler._LOGIN_GATE = gate
    page = FakePage(**(page_kw or {}))
    state, fake_get_code = install_fakes(page, code_after)
    filler._form_signature = lambda pg: {"fields": [], "kind": "none"}
    filler._looks_like_delivery_form = lambda sig: False   # 强制走登录段
    filler._sig_desc = lambda sig: "非表单页"

    req_hits = []
    results = {}

    def worker(i, company):
        results[i] = filler.apply_by_url(
            "http://example.invalid/job/%d" % i, "产品经理", company=company,
            progress=None, get_code=fake_get_code, code_wait=60,
            on_code_request=lambda: req_hits.append((threading.get_ident(), time.perf_counter())))

    ths = [threading.Thread(target=worker, args=(i, c))
           for i, c in enumerate(companies)]
    t0 = time.perf_counter()
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    return time.perf_counter() - t0, gate.spans, results, req_hits


el, spans, results, req = run_concurrent(RecGate(), ["公司甲", "公司乙", "公司丙"])
ck("D1 三个岗位都进了登录段", len(spans) == 3, "spans=%d" % len(spans))
ck("D2 登录段**互不重叠**（真串行）", not RecGate.overlaps(spans),
   "spans=%s" % [(round(a, 3), round(b, 3)) for _, a, b in spans])
ck("D3 三个岗位来自三个不同线程", len({s[0] for s in spans}) == 3)
total_login = sum(b - a for _, a, b in spans)
ck("D4 总耗时 ≥ 各登录段之和的多数（串行必然累加）", el >= total_login * 0.9,
   "elapsed=%.3fs, sum_login=%.3fs" % (el, total_login))

# ---- D2 证伪对照：把闸门换成 no-op，必须能测出重叠，否则 D2 的断言是空的 ----
el2, spans2, _, _ = run_concurrent(RecGate(noop=True), ["公司甲", "公司乙", "公司丙"])
ck("D5 对照组（无互斥）**确实能测出重叠** → D2 的断言有牙",
   RecGate.overlaps(spans2),
   "spans=%s" % [(round(a, 3), round(b, 3)) for _, a, b in spans2])

# ==========================================================================
print()
print("=" * 72)
print("E/F 会话复用 与 同公司串行")
print("=" * 72)

filler._LOGIN_GATE = gate_e = RecGate()
page = FakePage()
state, fake_get_code = install_fakes(page)
filler._form_signature = lambda pg: {"fields": [{"name": "姓名"}], "kind": "form"}
filler._looks_like_delivery_form = lambda sig: True    # 一开始就是投递表单 = 已是登录态
filler._sig_desc = lambda sig: "投递表单"

res_e = filler.apply_by_url("http://example.invalid/job/1", "产品经理",
                            company="公司甲", get_code=fake_get_code, code_wait=10)
ck("E1 已是登录态 → 未占用登录闸门", len(gate_e.spans) == 0,
   "spans=%d" % len(gate_e.spans))
ck("E2 未请求任何验证码", state["code_calls"] == 0, "code_calls=%d" % state["code_calls"])
ck("E3 结果里能看出跳过登录（含「已是登录态」）",
   "已是登录态" in json.dumps(res_e, ensure_ascii=False),
   res_e.get("stage", ""))

# 同公司并发：会话（profile）锁必须让两个调用串行 —— 换掉锁为可记录替身
filler._LOGIN_GATE = RecGate()
prof_gate = RecGate()
filler._lock_for = lambda key: prof_gate
page2 = FakePage(has_phone=False, sent="")     # 无登录表单 → 登录段立刻返回，快
state2, fgc2 = install_fakes(page2)
filler._form_signature = lambda pg: {"fields": [], "kind": "none"}
filler._looks_like_delivery_form = lambda sig: False
filler._sig_desc = lambda sig: "非表单页"

enter = []


def probe_worker(i):
    filler.apply_by_url("http://example.invalid/job/%d" % i, "岗位%d" % i,
                        company="同一家公司", get_code=fgc2, code_wait=10)
    enter.append(i)


ths = [threading.Thread(target=probe_worker, args=(i,)) for i in range(2)]
for t in ths:
    t.start()
for t in ths:
    t.join()
ck("F1 同公司两个岗位都跑完（未死锁/未崩）", len(enter) == 2, "enter=%d" % len(enter))
ck("F2 同公司两次调用的**会话区间不重叠**（Chromium 不允许同一目录双开）",
   len(prof_gate.spans) == 2 and not RecGate.overlaps(prof_gate.spans),
   "spans=%s" % [(round(a, 3), round(b, 3)) for _, a, b in prof_gate.spans])

# ==========================================================================
print()
print("=" * 72)
print("G/H/I 登录段内部行为")
print("=" * 72)

# G: on_code_request 必须被调用
gate_g = RecGate()
filler._LOGIN_GATE = gate_g
page_g = FakePage()
state_g, fgc_g = install_fakes(page_g)
filler._form_signature = lambda pg: {"fields": [], "kind": "none"}
filler._looks_like_delivery_form = lambda sig: False
filler._sig_desc = lambda sig: "非表单页"
hits = []
filler.apply_by_url("http://example.invalid/j", "产品经理", company="公司甲",
                    get_code=fgc_g, code_wait=10, on_code_request=lambda: hits.append(1))
ck("G1 点了「获取验证码」后回调了 on_code_request", len(hits) >= 1, "hits=%d" % len(hits))

# H: 没有登录表单 → 不白等，直接返回
gate_h = RecGate()
filler._LOGIN_GATE = gate_h
page_h = FakePage(has_phone=False, sent="")
state_h, fgc_h = install_fakes(page_h)
filler._form_signature = lambda pg: {"fields": [], "kind": "none"}
filler._looks_like_delivery_form = lambda sig: False
filler._sig_desc = lambda sig: "非表单页"
t0 = time.perf_counter()
res_h = filler.apply_by_url("http://example.invalid/j", "产品经理", company="公司甲",
                            get_code=fgc_h, code_wait=300)
el_h = time.perf_counter() - t0
ck("H1 无登录表单 → 未请求验证码", state_h["code_calls"] == 0,
   "code_calls=%d" % state_h["code_calls"])
ck("H2 未等到 code_wait 超时（<2s 就返回）", el_h < 2.0, "elapsed=%.3fs" % el_h)
ck("H3 闸门已释放", len(gate_h.spans) == 1 and gate_h._l.locked() is False)

# I: 人机验证转人工 → 闸门必须释放
gate_i = RecGate()
filler._LOGIN_GATE = gate_i
page_i = FakePage(captcha=True)
state_i, fgc_i = install_fakes(page_i)
filler._form_signature = lambda pg: {"fields": [], "kind": "none"}
filler._looks_like_delivery_form = lambda sig: False
filler._sig_desc = lambda sig: "非表单页"
res_i = filler.apply_by_url("http://example.invalid/j", "产品经理", company="公司甲",
                            get_code=fgc_i, code_wait=10, human_wait=4)
ck("I1 转人工 → 如实返回 needs_human", bool(res_i.get("needs_human")),
   "stage=%s" % res_i.get("stage"))
ck("I2 早退后闸门已释放（不会堵死后续登录）", gate_i._l.locked() is False)
t0 = time.perf_counter()
ok_i = filler._acquire(gate_i, lambda m: None, "复取", tick=1, max_wait=2)
ck("I3 闸门可被下一个岗位立刻取到（无死锁）", ok_i is True,
   "elapsed=%.3fs" % (time.perf_counter() - t0))
ck("I4 LOGIN_HOLDER 已清空", filler.LOGIN_HOLDER == "", repr(filler.LOGIN_HOLDER))

# ==========================================================================
print()
print("=" * 72)
print("J 已消费验证码不再发给别的岗位")
print("=" * 72)

_tmp = tempfile.mkdtemp(prefix="smoke_serial_")
os.environ["RESUME_STATE"] = os.path.join(_tmp, "state.json")
os.environ["RESUME_SMS"] = os.path.join(_tmp, "sms.json")
os.environ["RESUME_SMS_HIST"] = os.path.join(_tmp, "sms_hist.json")
os.environ["RESUME_SMS_REJECTED"] = os.path.join(_tmp, "sms_rejected.jsonl")
os.environ["RESUME_ACCESS_LOG"] = os.path.join(_tmp, "access.log")
os.environ["RESUME_XLSX"] = os.path.join(_tmp, "none.xlsx")

import server as S  # noqa: E402

S.SMS_HIST[:] = []
S.CONSUMED_CODES[:] = []
S.SMS.clear()
# 快照必须在短信到达**之前**拍（真实时序：任务开始拍快照，之后才有短信）。
# 若在短信已入 History 后才拍，新加的「快照判重」会把这些记录本身判为「已存在」
# 而排除掉 —— 那是另一条规则，见 K 段。
snap = S.sms_snapshot()
S.SMS_HIST.append({"phone": "10690080", "code": "111111", "text": "【甲】验证码111111",
                   "received": "2026-09-21 10:00:00"})
S.SMS_HIST.append({"phone": "10690080", "code": "222222", "text": "【乙】验证码222222",
                   "received": "2026-09-21 10:00:01"})
first = S.latest_code(after="2026-09-21 09:00:00", snapshot=snap, skip_consumed=True)
ck("J1 先取到最新那条", first == "222222", "got=%s" % first)
S.mark_code_consumed(first)
second = S.latest_code(after="2026-09-21 09:00:00", snapshot=snap, skip_consumed=True)
ck("J2 已消费的码被跳过，取到更早那条", second == "111111", "got=%s" % second)
ck("J3 消费台账有记录", len(S.CONSUMED_CODES) == 1)
# 关掉 skip_consumed 应保持旧行为（兼容）
legacy = S.latest_code(after="2026-09-21 09:00:00", snapshot=snap)
ck("J4 skip_consumed=False 保持旧行为（仍返回最新）", legacy == "222222", "got=%s" % legacy)
ck("J5 _rec_key 三元组", S._rec_key(S.SMS_HIST[0]) == ("10690080", "2026-09-21 10:00:00", "111111"),
   repr(S._rec_key(S.SMS_HIST[0])))

# ==========================================================================
print()
print("=" * 72)
print("K 取码硬时间下限：早于「点了获取验证码」的短信一律不取（真实故障复现）")
print("=" * 72)

import re  # noqa: E402

S.SMS_HIST[:] = []
S.CONSUMED_CODES[:] = []
S.SMS.clear()
# 现场：手机连发 3 条自检短信（发件人都是 TEST），用户之后才点「获取验证码」
for _t, _c in (("2026-09-21 23:17:32", "802776"),
               ("2026-09-21 23:17:52", "477325"),
               ("2026-09-21 23:18:22", "999831")):
    S.SMS_HIST.append({"phone": "TEST", "code": _c,
                       "text": "【链路自检】验证码 %s" % _c, "received": _t})
snap_task = S.sms_snapshot()
ck("K1 快照同一发件人只留最新一条（旧实现据此把更早那条误判成「新到达」）",
   snap_task.get("TEST") == ("2026-09-21 23:18:22", "999831"), str(snap_task.get("TEST")))
_got = S.latest_code(after="2026-09-21 23:19:00", snapshot=snap_task, skip_consumed=True)
ck("K2 窗口起点晚于全部自检短信 → 一个码都取不到（硬下限生效）", _got == "", "got=%r" % _got)
S.SMS_HIST.append({"phone": "106900801534293", "code": "4163",
                   "text": "验证码：4163，请勿泄露。", "received": "2026-09-21 23:19:03"})
_got2 = S.latest_code(after="2026-09-21 23:19:00", snapshot=snap_task, skip_consumed=True)
ck("K3 新码到达后取到的是新码（不是自检短信）", _got2 == "4163", "got=%r" % _got2)
_got3 = S.latest_code(after="2026-09-21 23:19:00", snapshot=snap_task,
                      match=S.sms_matcher("思特奇"), skip_consumed=True)
ck("K4 公司名不命中要回退，但不会回退到早于请求时刻的自检短信",
   _got3 == "4163", "got=%r" % _got3)
# 源码级防回归：on_code_request 必须**无条件**推后窗口，
# 不能再出现「先用 latest_code 探测、有码就不推后」那种写法（那会保留旧起点）。
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py"),
            encoding="utf-8").read()
ck("K5 不再用「探测式取码」决定是否推后窗口（旧写法会保留旧起点）",
   re.search(r"latest_code\([^)]*match=matcher\)\s*:\s*\n\s*return", _src) is None)
ck("K6 两处 on_code_request 都把窗口推到「此刻」（win[\"req_at\"] = _now() ×2）",
   _src.count('win["req_at"] = _now()') == 2,
   "count=%d" % _src.count('win["req_at"] = _now()'))
ck("K6b 推后是「只推第一次」（两处都判 win.get(\"req_at\")）",
   _src.count('if win.get("req_at"):') == 2,
   "count=%d" % _src.count('if win.get("req_at"):'))
ck("K8 重发后仍能取到「第一次请求之后到达」的码（不被自己的新下限挡住）",
   S.latest_code(after="2026-09-21 23:19:00", snapshot={}) == "4163")
ck("K7 推后后的新窗口仍然挡住旧自检短信",
   S.latest_code(after=S._now(), snapshot=S.sms_snapshot()) == "")

print()
print("=" * 72)
print("结果：%d PASS / %d FAIL" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  - " + x)
print("=" * 72)
sys.exit(1 if FAIL else 0)
