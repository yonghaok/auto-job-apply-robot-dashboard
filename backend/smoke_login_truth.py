# -*- coding: utf-8 -*-
"""登录结果核实 + 人工进入投递页后自动填表 回归（T66）。

修的两个 bug：
  ①「状态确认错误，未登录成功却显示等待人工提交」
  ③「人工确认进入投递页后自动输入未生效」

根因：
  ① `_login_phase` 点完「登录」就**无条件** note+return None，从不核实登录结果。
     页面明明报「验证码错误」，调用方也照走；随后识别不到投递表单，被兜底成
     「待人工提交」→ 未登录成功却显示「等待人工提交」。
  ③ 登录完成后若站点要人自己点进投递页，那条分支直接 `_park_before_submit`
     —— 那个等待器**只盯提交成功文案**，人进了表单它也不会去填。

覆盖
 A) `_login_evidence`：失败文案 → failed；登录控件消失 → ok；两边都没有 → unknown
 B) `_looks_like_login_page`：控件还在 → True；读不到(-1) → False（保守）
 C) `_wait_human_reach_form`：人进表单 → done · 人已自己投了 → submitted
    · 关窗 → closed · 取消 → cancelled · 一直不进 → timeout
 C') **证伪**：单点短路表单判据 → C 的 done 必须翻成 timeout
 D) 看板映射：`_stage_status("登录未完成"/"登录失败") == "需人工验证"`
 D') **证伪**：把映射项摘掉 → 同一 stage 会退回「待人工提交」（正是 bug ① 的现象）
 E) 源码结构：无条件宣告已删 · 核实分支在 · 分支已分辨登录页/非登录页

用法（cwd=backend）：<venv-python> smoke_login_truth.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["RESUME_CDP_ENDPOINT"] = "http://127.0.0.1:59999"
os.environ.setdefault("RESUME_PROFILES", os.path.join(HERE, "_smoke_t66_profiles"))

import filler                      # noqa: E402
import server as srv               # noqa: E402

PASS = 0
FAIL = 0

FORM_SIG = {"n": 9, "labels": ["姓名", "手机号", "学历"],
            "texts": ["提交", "取消"], "fileInputs": 1}


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


class _Loc:
    def __init__(self, n, ok=True):
        self._n = n
        self._ok = ok

    def count(self):
        if not self._ok:
            raise RuntimeError("locator boom")
        return self._n


class FakePage:
    """只实现本次要测的几个函数会用到的接口。

    sig       —— `_form_signature` 的返回值（None 表示「不是投递表单」）
    sig_after —— 第几次询问表单特征后才开始返回 sig（模拟「人后来才点进表单」）
    """

    def __init__(self, text="", widgets=0, url="https://x/list",
                 closed=False, sig=None, sig_after=1, eval_ok=True, loc_ok=True):
        self._text = text
        self._widgets = widgets
        self.url = url
        self.closed = closed
        self.sig = sig
        self.sig_after = sig_after
        self.eval_ok = eval_ok
        self.loc_ok = loc_ok
        self.sig_calls = 0

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True

    def bring_to_front(self):
        return None

    def wait_for_timeout(self, _ms):
        pass

    def evaluate(self, script="", *a, **k):
        if not self.eval_ok:
            raise RuntimeError("eval boom")
        # ⚠️ 判据必须用 querySelectorAll 先分流：`_form_signature` 的脚本里**也含**
        #    `innerText`（读 label 用），只按 innerText 分流会把表单特征脚本错判成
        #    读正文 → 恒取不到表单（本套件第一版就踩了这个坑）。
        if "querySelectorAll" in script:
            self.sig_calls += 1
            return self.sig if self.sig_calls >= self.sig_after else None
        if "document.body.innerText" in script:
            return self._text
        return ""

    def locator(self, _sel):
        return _Loc(self._widgets, self.loc_ok)


print("A) _login_evidence · 核实登录到底成没成功")
p = FakePage(text="验证码错误，请重新输入", widgets=1)
check("A1 页面报「验证码错误」→ failed", filler._login_evidence(p) == ("failed", "验证码错误"),
      repr(filler._login_evidence(p)))
p = FakePage(text="登录失败，请稍后再试", widgets=1)
check("A2 页面报「登录失败」→ failed", filler._login_evidence(p)[0] == "failed")
p = FakePage(text="投递首页", widgets=0)
check("A3 登录表单控件已消失 → ok", filler._login_evidence(p) == ("ok", "登录表单已消失"),
      repr(filler._login_evidence(p)))
p = FakePage(text="手机号登录", widgets=1)
check("A4 两边都没证据 → unknown（不吹牛）", filler._login_evidence(p)[0] == "unknown")
p = FakePage(text="", widgets=1, eval_ok=False)
check("A5 读正文失败也不误判 → unknown", filler._login_evidence(p)[0] == "unknown",
      repr(filler._login_evidence(p)))

print("B) _looks_like_login_page · 当前页是否仍是登录/验证表单")
check("B1 登录控件还在 → True", filler._looks_like_login_page(FakePage(widgets=1)) is True)
check("B2 控件为 0 → False", filler._looks_like_login_page(FakePage(widgets=0)) is False)
check("B3 控件数读不到(-1) → False（保守，沿用旧行为）",
      filler._looks_like_login_page(FakePage(widgets=1, loc_ok=False)) is False)

print("C) _wait_human_reach_form · 等人进入投递页后自动继续")
p = FakePage(sig=FORM_SIG, sig_after=2)
how, why = filler._wait_human_reach_form(p, budget=40)
check("C1 人后来点进投递表单 → done", how == "done", "%s / %s" % (how, why))
p = FakePage(text="投递成功，感谢您的投递", sig=FORM_SIG, sig_after=99)
how, why = filler._wait_human_reach_form(p, budget=40)
check("C2 我们还没填、人已自己投了 → submitted", how == "submitted", "%s / %s" % (how, why))
p = FakePage(sig=FORM_SIG, sig_after=99)
p.closed = True
how, why = filler._wait_human_reach_form(p, budget=40)
check("C3 窗口被关 → closed", how == "closed", "%s / %s" % (how, why))
p = FakePage(sig=FORM_SIG, sig_after=99)
how, why = filler._wait_human_reach_form(p, budget=40, is_cancelled=lambda: True)
check("C4 取消投递 → cancelled", how == "cancelled", "%s / %s" % (how, why))
p = FakePage(sig=FORM_SIG, sig_after=99)
how, why = filler._wait_human_reach_form(p, budget=4)
check("C5 人一直不进表单 → timeout", how == "timeout", "%s / %s" % (how, why))

print("C') 证伪：单点短路「表单判据」→ done 必须翻成 timeout")
_orig_sig = filler._form_signature
try:
    filler._form_signature = lambda page: None          # 单点短路：永远认不出表单
    p = FakePage(sig=FORM_SIG, sig_after=2)
    how2, _ = filler._wait_human_reach_form(p, budget=40)
    check("C'1 短路表单判据后不再返回 done（证明 done 真由表单判据驱动）",
          how2 == "timeout", "got=%s" % how2)
finally:
    filler._form_signature = _orig_sig
p = FakePage(sig=FORM_SIG, sig_after=2)
check("C'2 复原后同场景又是 done（排除判据本身写死）",
      filler._wait_human_reach_form(p, budget=40)[0] == "done")

print("D) 看板映射 · 登录没成功应当是「需人工验证」而不是「待人工提交」")
check("D1 「登录未完成」→ 需人工验证",
      srv._stage_status("登录未完成", needs_human=True) == "需人工验证",
      srv._stage_status("登录未完成", needs_human=True))
check("D2 「登录失败」→ 需人工验证",
      srv._stage_status("登录失败", needs_human=True) == "需人工验证",
      srv._stage_status("登录失败", needs_human=True))
check("D3 「未找到投递表单」仍 → 待人工提交（语义未被带偏）",
      srv._stage_status("未找到投递表单", needs_human=True) == "待人工提交")

print("D') 证伪：摘掉映射项 → 同一 stage 退回「待人工提交」（复现 bug ①）")
_kept = {}
for _k in ("登录未完成", "登录失败"):
    _kept[_k] = srv.STAGE_TO_STATUS.pop(_k, None)
try:
    bad = srv._stage_status("登录未完成", needs_human=True)
    check("D'1 无映射时确实显示「待人工提交」（这就是用户报的 bug ①）",
          bad == "待人工提交", "got=%s" % bad)
finally:
    srv.STAGE_TO_STATUS.update({k: v for k, v in _kept.items() if v})
check("D'2 复原映射后又是「需人工验证」",
      srv._stage_status("登录未完成", needs_human=True) == "需人工验证")

print("E) 源码结构")
src = open(os.path.join(HERE, "filler.py"), encoding="utf-8").read()
check("E1 无条件的「自动登录完成（流程已走通）」宣告已删除",
      'note("自动登录完成（流程已走通）")' not in src)
check("E2 登录段含失败核实分支 if ev == \"failed\":", 'if ev == "failed":' in src)
check("E3 登录段含正向证据分支 if ev == \"ok\":", 'if ev == "ok":' in src)
check("E4 「不是投递表单」分支已分辨登录页/非登录页",
      "if _looks_like_login_page(page):" in src)
check("E5 非登录页分支调用 _wait_human_reach_form（人进表单后自动填）",
      "_wait_human_reach_form(page, note=note, budget=budget," in src)
check("E6 登录未完成会如实返回 stage=登录未完成",
      '"stage": "登录未完成"' in src)
check("E7 新等待器的备注（含心跳）也带 tag，否则等待期间看不到「进入窗口」",
      src.count("note((tag + m) if tag else m)") == 2
      and 'tag="等待人工提交（窗口保持打开）：")' in src,
      "tagwrap=%d" % src.count("note((tag + m) if tag else m)"))

print("\n汇总：PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
