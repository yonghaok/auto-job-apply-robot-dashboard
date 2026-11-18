# -*- coding: utf-8 -*-
"""人工交接（人机验证 / 取不到验证码 / 表单必填 / 提交结果）的离线自测（T64）。

用户报（2026-09-23 晚）：「人工验证失败问题仍旧存在，滑块与旋转验证都不行；
状态仍卡错，人工验证通过后没有进入投递状态；明明已经进入最后确认投递阶段却关闭窗口；
阶段提示卡在需要人工过验证。」

**根因（都在真实 state.json 的 note 里复现过）**
 R1 `server.py` 两个投递入口都传 `human_wait=0` → `_login_phase` 的
    `while waited < human_wait` **一次都不进** → 立刻返回 needs_human → `finally`
    关窗。而 note 写着「请在已打开的浏览器窗口中手动完成验证，完成后自动继续」——
    **这句是假的**：窗口在写下它的下一秒就被关掉。→ 人没机会过验证 =「人工验证失败」。
 R2 `_stage_status()` 把**一切** needs_human 的未知 stage 兜底成「需人工验证」，
    于是「必填未填」「提交结果未知」「未找到提交按钮」全显示成「需要人工过验证」。
 R3 提交阶段 invalid/clicked/未找到提交按钮 分支写着「不驻留，直接进入下一个岗位」
    → 返回 → 关窗 =「明明已经进入最后确认投递阶段却关闭窗口」。
 R4（本轮自测追加）「**取不到验证码**」分支同样写完「可在浏览器窗口手动输入验证码」
    下一秒就关窗；而前端「需人工验证」面板正是照这句写的操作指引。→ 这才是
    「阶段提示卡在需要人工过验证」的主因（短信没到时必然走这条）。
 R5（本轮自测追加）「登录后未识别到投递表单」也关窗，但看板上它映射成「待人工提交」，
    前端于是给出「进入窗口（置前）」按钮 —— 点开是空窗口。

**覆盖**
 A) `_handoff_budget`：**`human_wait=0` 不再等于「放弃」**（0/None/负数 → 默认预算）
 B) `_wait_human_verify` **真浏览器**行为：人做完 → done；不做 → timeout；取消 / 关窗
 C) `_login_phase` 人机验证交接端到端（真浏览器 + 本地 HTML，直击用户原话）
    + **证伪对照**（预算短路回 0 必须重现「立刻返回 needs_human」）
 D) 状态语义：`需人工验证` **只**给人机验证/验证码交接；表单/提交类一律 `待人工提交`
 E) 接线与源码结构（含「旧关窗文案只剩注释」的逐行判定）
 F) 「取不到验证码」交接端到端 + **证伪对照**（这是 R4）

用法（cwd=backend）：<venv-python> smoke_human_handoff.py
"""
import inspect
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# 隔离 CDP 接管：否则 _open_session 会把标签页开进用户真实的 Edge
os.environ["RESUME_CDP_ENDPOINT"] = "http://127.0.0.1:59999"

import filler                      # noqa: E402
import server as srv               # noqa: E402

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


# 假登录页：手机号框 / 协议框 / 获取验证码 / 一个静态的「安全验证」控件。
#   __JS__ 处由各 fixture 填一段定时脚本。
#   注意：**不能**用别的线程去 page.evaluate 移除控件 —— Playwright 同步 API 跨线程不安全。
PAGE_HTML = """<html><body>
  <input id="phone" placeholder="请输入手机号">
  <label><input type="checkbox" id="ag">我已阅读并同意《隐私协议》</label>
  <button id="getcode">获取验证码</button>
  <div id="hv" style="width:260px;height:60px;background:#eee">
    安全验证：请按住滑块，拖动到最右边完成验证
  </div>
  <script>__JS__</script>
</body></html>"""

# 取不到验证码场景：页面上**有**取码框 + 登录按钮（人要在窗口里手动输码点登录）
PAGE_HTML_CODE = """<html><body>
  <input id="phone" placeholder="请输入手机号">
  <label><input type="checkbox" id="ag">我已阅读并同意《隐私协议》</label>
  <button id="getcode">获取验证码</button>
  <input id="code" placeholder="请输入短信验证码">
  <button id="login">登录</button>
  <script>__JS__</script>
</body></html>"""


class Fixture:
    """写一个本地 HTML，供真浏览器访问。"""

    def __init__(self, tmpdir, name, html, js):
        self.html = html.replace("__JS__", js)
        self.path = os.path.join(tmpdir, name)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(self.html)

    def url(self):
        return "file:///" + self.path.replace("\\", "/")


def main():
    tmpdir = tempfile.mkdtemp(prefix="smoke_hv_")

    def rm(sel, ms):
        return ("setTimeout(function(){var e=document.querySelector('%s');"
                "if(e)e.remove();},%d);" % (sel, ms))

    # 「人在窗口里做完」= 控件稍后消失。B1 直接用，故按**载入后**计时。
    fx_hv_short = Fixture(tmpdir, "hv_b1.html", PAGE_HTML, rm("#hv", 8000))
    # C1：载入后 15s 才消失 —— 保证「第 5 步检测（≈载入后 5s）时控件还在」，
    #     从而一定走进「自动失败 → 转人工驻留」这条路，且驻留期间人把它做完。
    fx_hv_long = Fixture(tmpdir, "hv_c1.html", PAGE_HTML, rm("#hv", 15000))
    # 人始终不做
    fx_hv_never = Fixture(tmpdir, "hv_never.html", PAGE_HTML, "")
    # F：人手动输码并点登录 → 取码框/登录按钮消失（在**点过「获取验证码」之后** 12s）
    fx_code = Fixture(tmpdir, "code_login.html", PAGE_HTML_CODE,
                      "document.getElementById('getcode').addEventListener('click',"
                      "function(){setTimeout(function(){"
                      "['code','login'].forEach(function(i){"
                      "var e=document.getElementById(i);if(e)e.remove();});},12000);});")

    # ------------------------------------------------------------------ A
    print("\n=== A. _handoff_budget：0 不再等于「放弃」 ===")
    check("HUMAN_HANDOFF_WAIT == 0（默认不设超时，窗口不因等待超时被关）",
          filler.HUMAN_HANDOFF_WAIT == 0, repr(filler.HUMAN_HANDOFF_WAIT))
    check("_SUBMIT_CONFIRM_WAIT 是正数", filler._SUBMIT_CONFIRM_WAIT > 0)
    check("human_wait=0 / None / 负数 / 脏值 → 都回落到默认（0 = 不超时）",
          filler._handoff_budget(0) == 0 and filler._handoff_budget(None) == 0
          and filler._handoff_budget(-5) == 0 and filler._handoff_budget("abc") == 0,
          repr([filler._handoff_budget(x) for x in (0, None, -5, "abc")]))
    check("显式正数照用（120 → 120）", filler._handoff_budget(120) == 120)

    env = dict(os.environ)
    env["RESUME_HUMAN_HANDOFF_WAIT"] = "42"
    env["RESUME_SUBMIT_CONFIRM_WAIT"] = "7"
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, r'%s'); import filler;"
         "print(filler.HUMAN_HANDOFF_WAIT, filler._SUBMIT_CONFIRM_WAIT)" % HERE],
        capture_output=True, text=True, env=env, cwd=HERE).stdout.split()
    check("预算可用 RESUME_HUMAN_HANDOFF_WAIT / RESUME_SUBMIT_CONFIRM_WAIT 覆盖",
          out == ["42", "7"], repr(out))

    from playwright.sync_api import sync_playwright
    real_wait = filler.HUMAN_HANDOFF_WAIT
    real_auto = filler._attempt_auto_verify
    real_budget_fn = filler._handoff_budget
    real_hb = filler._PARK_HEARTBEAT

    def run_login(page, human_wait, notes, code_wait=2, code="123456"):
        """`code=None` = 后端**取不到**验证码（F 段用）；否则给一个能取到的假验证码，
        以便验「过完人机验证后流程**继续往下走**」（用户原话：过完验证没进投递态）。"""
        return filler._login_phase(page, "13800000000", emit=notes.append,
                                   get_code=(lambda: code) if code else None,
                                   code_wait=code_wait, human_wait=human_wait)

    try:
        filler._attempt_auto_verify = lambda *a, **k: False   # 隔离：只测「交接」本身
        with sync_playwright() as pw:
            br = pw.chromium.launch(headless=True)

            # ---------------------------------------------------------- B
            print("\n=== B. _wait_human_verify 真浏览器行为 ===")
            pg = br.new_page()
            pg.goto(fx_hv_short.url())
            notes = []
            t0 = time.time()
            how, why = filler._wait_human_verify(pg, note=notes.append, budget=30)
            el = time.time() - t0
            check("B1 人在窗口里做完验证 → done", how == "done", "%s / %s" % (how, why))
            check("B1 确实在等人（耗时 ≥ 4s，不是立刻返回）", el >= 4.0, "%.1fs" % el)
            check("B1 done 的原因是「连续两次复检通过」", "连续两次复检通过" in why, why)

            pg2 = br.new_page()
            pg2.goto(fx_hv_never.url())
            t0 = time.time()
            how2, why2 = filler._wait_human_verify(pg2, budget=6)
            el2 = time.time() - t0
            check("B2 人不做 → timeout（有界，不无限等）",
                  how2 == "timeout" and 5.0 <= el2 <= 9.0, "%s %.1fs" % (how2, el2))

            t0 = time.time()
            how3, why3 = filler._wait_human_verify(pg2, budget=30,
                                                   is_cancelled=lambda: True)
            el3 = time.time() - t0
            check("B3 用户取消 → cancelled 且立即返回",
                  how3 == "cancelled" and el3 < 1.5, "%s %.1fs" % (how3, el3))

            pg3 = br.new_page()
            pg3.goto(fx_hv_never.url())
            pg3.close()
            how4, why4 = filler._wait_human_verify(pg3, budget=30)
            check("B4 窗口被关 → closed（不误判成功）", how4 == "closed", "%s %s" % (how4, why4))

            filler._PARK_HEARTBEAT = 2
            pg4 = br.new_page()
            pg4.goto(fx_hv_never.url())
            n4 = []
            filler._wait_human_verify(pg4, note=n4.append, budget=5,
                                      is_cancelled=None, what="测试事项")
            check("B5 心跳备注写明「浏览器窗口保持打开」+ 等待事项",
                  any("浏览器窗口保持打开" in x for x in n4)
                  and any("测试事项" in x for x in n4), repr(n4[:2]))
            filler._PARK_HEARTBEAT = real_hb

            # ---------------------------------------------------------- C
            print("\n=== C. _login_phase 人机验证交接（直击用户原话）===")
            filler.HUMAN_HANDOFF_WAIT = 30
            pg5 = br.new_page()
            pg5.goto(fx_hv_long.url())
            notes5 = []
            t0 = time.time()
            r5 = run_login(pg5, human_wait=0, notes=notes5)     # ← 旧代码就是传 0
            el5 = time.time() - t0
            joined5 = "；".join(notes5)
            check("C0 真的走到了人机验证这一步（不是从别的分支提前返回）",
                  "检测到人机验证" in joined5, joined5[-200:])
            check("C1 自动失败后确实驻留等人（耗时 ≥ 12s，控件 15s 才消失）",
                  el5 >= 12.0, "%.1fs" % el5)
            check("C1 人做完验证后**自动继续投递**（返回 None，不是 needs_human）",
                  r5 is None, repr(r5)[:160])
            check("C1 备注如实写明「浏览器窗口保持打开」",
                  "浏览器窗口保持打开" in joined5, joined5[-160:])
            check("C1 备注记录了「人机验证已完成…自动继续投递」",
                  "人机验证已完成" in joined5 and "自动继续投递" in joined5)

            print("  --- 证伪对照：把预算压到 2s，必须重现「等不到就放弃」 ---")
            # 注意：不能短路成 0 —— 0 现在是「不设超时」，会真的无限等（钉死卡住）。
            filler._handoff_budget = lambda *a, **k: 2
            pg6 = br.new_page()
            pg6.goto(fx_hv_never.url())
            notes6 = []
            t0 = time.time()
            r6 = run_login(pg6, human_wait=0, notes=notes6)
            el6 = time.time() - t0
            filler._handoff_budget = real_budget_fn
            check("C2 短路后**重现**旧故障：不等就返回 needs_human(stage=人机验证)",
                  isinstance(r6, dict) and r6.get("stage") == "人机验证",
                  "%s %.1fs" % (repr(r6)[:80], el6))
            check("C2 → 说明 C1 的断言确实钉住了这次修复（不是恒真）", el6 < el5 - 5,
                  "%.1fs vs %.1fs" % (el6, el5))

            filler.HUMAN_HANDOFF_WAIT = 6
            pg7 = br.new_page()
            pg7.goto(fx_hv_never.url())
            notes7 = []
            t0 = time.time()
            r7 = run_login(pg7, human_wait=0, notes=notes7)
            el7 = time.time() - t0
            check("C3 人始终不做 → 有界超时后如实返回 stage=人机验证",
                  isinstance(r7, dict) and r7.get("stage") == "人机验证" and el7 >= 5.0,
                  "%s %.1fs" % (repr(r7)[:80], el7))
            check("C3 超时时备注说明「人机验证仍未通过」+ 后续处置",
                  any("人机验证仍未通过" in x for x in notes7))
            filler.HUMAN_HANDOFF_WAIT = real_wait

            # ---------------------------------------------------------- F
            print("\n=== F. 「取不到验证码」也要交窗口给人（R4）===")
            filler.HUMAN_HANDOFF_WAIT = 30
            pg8 = br.new_page()
            pg8.goto(fx_code.url())
            n8 = []
            t0 = time.time()
            r8 = run_login(pg8, human_wait=0, notes=n8, code_wait=2, code=None)
            el8 = time.time() - t0
            j8 = "；".join(n8)
            check("F0 走到了「取不到验证码」这一步", "未取得验证码" in j8, j8[-200:])
            check("F1 驻留等人（点过取码按钮 12s 后控件才消失；总耗时 ≥ 13s）",
                  el8 >= 13.0, "%.1fs" % el8)
            check("F1 人手动输码登录后**自动继续投递**（返回 None）", r8 is None,
                  repr(r8)[:160])
            check("F1 备注写明「浏览器窗口保持打开」+ 手动输码指引",
                  "浏览器窗口保持打开" in j8 and "手动输入验证码" in j8, j8[-160:])
            check("F1 备注记录了「检测到你已完成登录…自动继续投递」",
                  "检测到你已完成登录" in j8 and "自动继续投递" in j8)

            print("  --- 证伪对照：预算压到 2s → 必须重现「取不到码就放弃」 ---")
            filler._handoff_budget = lambda *a, **k: 2
            pg9 = br.new_page()
            pg9.goto(fx_code.url())
            n9 = []
            t0 = time.time()
            r9 = run_login(pg9, human_wait=0, notes=n9, code_wait=2, code=None)
            el9 = time.time() - t0
            filler._handoff_budget = real_budget_fn
            check("F2 短路后重现旧故障：立刻返回 needs_human(stage=验证码未到)",
                  isinstance(r9, dict) and r9.get("stage") == "验证码未到",
                  "%s %.1fs" % (repr(r9)[:80], el9))
            check("F2 → F1 的断言确实钉住了修复", el9 < el8 - 4, "%.1fs vs %.1fs" % (el9, el8))
            # 注意：那句「浏览器窗口保持打开」的 note 是在**调用等待器之前**无条件写下的，
            # 所以「预算短路」不会让它消失 —— 能验的是「有没有真的完成交接」。
            check("F2 旧行为下**没有**「检测到你已完成登录」（交接被跳过）",
                  "检测到你已完成登录" not in "；".join(n9))
            filler.HUMAN_HANDOFF_WAIT = real_wait
            br.close()
    finally:
        filler._attempt_auto_verify = real_auto
        filler._handoff_budget = real_budget_fn
        filler.HUMAN_HANDOFF_WAIT = real_wait
        filler._PARK_HEARTBEAT = real_hb

    # ------------------------------------------------------------------ D
    print("\n=== D. 状态语义：需人工验证只给人机验证/验证码 ===")
    f = srv._stage_status
    for st in ("人机验证", "验证码未到"):
        check("D %s → 需人工验证" % st, f(st, needs_human=True) == "需人工验证",
              f(st, needs_human=True))
    for st in ("必填未填", "提交结果未知", "未找到提交按钮", "填表未命中",
               "已填表待确认", "填表失败", "未找到投递表单", "会话排队超时",
               "已填表未提交"):
        check("D %s → 待人工提交（不是「需要人工过验证」）" % st,
              f(st, needs_human=True) == "待人工提交", f(st, needs_human=True))
    check("D 未知 stage + needs_human 的兜底 = 待人工提交（不再是需人工验证）",
          f("某个未来的stage", needs_human=True) == "待人工提交")
    check("D 非人工路径不变：未知 stage + 非 needs_human → 已登录",
          f("某stage", needs_human=False) == "已登录")
    check("D 投递受限 → 投递失败（没投成）", f("投递受限") == "投递失败")
    check("D 已取消 → 待投递（可重投）", f("已取消") == "待投递")
    check("D 投递成功（人工确认）→ 已投递", f("投递成功（人工确认）") == "已投递")

    # ------------------------------------------------------------------ E
    print("\n=== E. 接线与源码结构 ===")
    ssrc = open(os.path.join(HERE, "server.py"), encoding="utf-8").read()
    fsrc = open(os.path.join(HERE, "filler.py"), encoding="utf-8").read()
    srv_src = inspect.getsource(srv)
    check("E server 两个投递入口都不再传 human_wait=0",
          "human_wait=0," not in ssrc and "human_wait=0)" not in ssrc,
          "残留调用点")
    check("E progress() 会把「等待人工提交」映射成待人工提交（否则等待期间看不到按钮）",
          '"等待人工提交" in msg' in srv_src and 'st = "待人工提交"' in srv_src)
    check("E _login_phase 不再用旧循环（while waited < human_wait）",
          "while waited < human_wait" not in fsrc)
    check("E _login_phase 用人手介入预算 + 统一等待器",
          "_handoff_budget(human_wait)" in fsrc and "_wait_human_verify(page" in fsrc)
    check("E 自动过验证失败后明确写「浏览器窗口保持打开」",
          "**浏览器窗口保持打开**，请在窗口内手动" in fsrc)
    check("E 等待器的通过判据可替换（detector）—— 否则无法用于取码场景",
          "detector=None, what=" in fsrc and "_passed()" in fsrc)
    check("E 取码场景有「登录表单特征」计数助手", "def _login_widget_count" in fsrc)
    check("E 「取不到验证码」已改为交窗口（写明保持打开 + 手动输码）",
          "**浏览器窗口保持打开**，请在窗口内手动输入验证码并" in fsrc
          and "已改为请你在" not in fsrc)
    check("E 「未找到投递表单」已改为交窗口 + 人工进入表单后**自动填表**（T66 bug③）",
          "请在窗口内自行进入投递页" in fsrc
          and "_wait_human_reach_form(page, note=note" in fsrc
          and 'tag="等待人工提交（窗口保持打开）：")' in fsrc)
    check("E 自动提交关闭时（auto_submit=False）仍如实说「窗口随后会关闭」",
          "已按配置跳过自动提交（仅填表，未提交）；浏览器窗口随后会关闭" in fsrc)
    tags = fsrc.count('tag="等待人工提交（窗口保持打开）："')
    check("E 6 处提交/表单类交接点全部带 tag（否则状态被刷回投递中）", tags >= 6,
          "count=%d" % tags)
    check("E _park_before_submit 的心跳也带 tag（否则状态被刷回投递中）",
          "note((tag + m) if tag else m)" in fsrc)
    check("E 单岗位入口不再硬编码「需人工验证」",
          'set_status(cid, "需人工验证",' not in ssrc)
    check("E filler 里残留的人机验证等待预算可调（RESUME_HUMAN_HANDOFF_WAIT）",
          "RESUME_HUMAN_HANDOFF_WAIT" in fsrc)
    check("E server 启动时会复位中断残留（否则「投递中」永久卡住）",
          "def heal_interrupted_statuses" in ssrc
          and ssrc.count("heal_interrupted_statuses()") >= 2,     # 定义处 + 调用处
          "count=%d" % ssrc.count("heal_interrupted_statuses()"))

    # 「不驻留，直接进入下一个岗位」只允许留在**注释**里（说明历史行为），
    # 不允许再出现在任何 note 文案 / 代码路径上。
    old_lines = [(i + 1, ln) for i, ln in enumerate(fsrc.split("\n"))
                 if "不驻留" in ln]
    check("E 旧文案「不驻留…」只剩注释（不再是任何代码路径的说明）",
          bool(old_lines) and all(ln.strip().startswith("#") for _, ln in old_lines),
          repr(old_lines))
    check("E 关窗式文案「窗口随后会关闭」只剩「仅填表」与 park 超时两处",
          fsrc.count("窗口随后会关闭") == 2, "count=%d" % fsrc.count("窗口随后会关闭"))

    print("  --- 行为断言：_park_before_submit 的 tag 前缀 ---")
    with sync_playwright() as pw2:
        br2 = pw2.chromium.launch(headless=True)
        pg = br2.new_page()
        pg.goto(fx_hv_never.url())
        filler._PARK_HEARTBEAT = 1          # 缩短心跳，3s 内一定能收到带 tag 的备注
        got = []
        st, ev = filler._park_before_submit(pg, note=got.append, confirm_wait=3,
                                           tag="等待人工提交（窗口保持打开）：")
        filler._PARK_HEARTBEAT = real_hb
        br2.close()
    check("E _park_before_submit 超时返回 timeout", st == "timeout", "%s/%s" % (st, ev))
    check("E 它的每条备注都带上了 tag 前缀（含心跳）",
          bool(got) and all(x.startswith("等待人工提交") for x in got), repr(got[:2]))

    # ------------------------------------------------------------------ G
    print("\n=== G. 启动自愈：中断残留的「投递中」→「待投递」 ===")
    real_cands, real_ps, real_state = srv.CANDIDATES, srv.POS_STATE, srv.STATE
    real_save = srv.statemod.save_state
    srv.statemod.save_state = lambda *a, **k: None      # 隔离：绝不写真盘
    try:
        srv.CANDIDATES = [
            {"id": "p_stuck", "status": "投递中", "note": "已进入投递表单，开始自动填写…"},
            {"id": "p_ok", "status": "已投递", "note": "投递成功"},
            {"id": "p_pending", "status": "pending", "note": ""},
        ]
        srv.POS_STATE = {"p_stuck2": {"status": "投递中", "note": "检测到人机验证…"},
                         "p_ok2": {"status": "需人工验证", "note": "x"}}
        srv.STATE = {}
        n = srv.heal_interrupted_statuses()
        check("G 只复位「投递中」的岗位（2 个）", n == 2, "n=%d" % n)
        check("G 岗位行被复位为「待投递」",
              srv.CANDIDATES[0]["status"] == "待投递", srv.CANDIDATES[0]["status"])
        check("G POS_STATE 里的也被复位",
              srv.POS_STATE["p_stuck2"]["status"] == "待投递",
              srv.POS_STATE["p_stuck2"]["status"])
        check("G 新备注如实说明「中途被中断 / 结果未知」",
              "中途被中断" in srv.CANDIDATES[0]["note"]
              and "结果未知" in srv.CANDIDATES[0]["note"],
              srv.CANDIDATES[0]["note"][:140])
        check("G 保留中断前的轨迹（fresh=False，不覆盖）",
              "已进入投递表单" in srv.CANDIDATES[0]["note"])
        check("G 已投递 / 需人工验证 / pending 一律不动",
              srv.CANDIDATES[1]["status"] == "已投递"
              and srv.POS_STATE["p_ok2"]["status"] == "需人工验证"
              and srv.CANDIDATES[2]["status"] == "pending")
        check("G 幂等：再跑一次 n=0（不会反复改写）",
              srv.heal_interrupted_statuses() == 0)
        check("G 不把中断残留写成「已投递」（结果未知就不许说投成功）",
              srv.CANDIDATES[0]["status"] != "已投递")
    finally:
        srv.CANDIDATES, srv.POS_STATE, srv.STATE = real_cands, real_ps, real_state
        srv.statemod.save_state = real_save

    print("\n===== 汇总：PASS=%d FAIL=%d =====" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
