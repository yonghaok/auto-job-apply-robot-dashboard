# -*- coding: utf-8 -*-
"""smoke_t77_no_timeout.py —— 锁定「浏览器窗口不许因为等待超时被关掉」。

用户两次反馈：
  ①「停表后等人工确认」原来 60 分钟到点就关窗；
  ②「我在输入验证码完成登录后仍有异常关闭」—— 这条不是 ①，而是 `HUMAN_HANDOFF_WAIT`
     的 600s：登录段/人机验证/等进投递页都用它，10 分钟一到就 needs_human → finally 关窗。

修复（T77 / T77b）：两处默认都改成 0 = 不设超时，两个等待器（_wait_human_verify /
_wait_human_reach_form）都支持 0 不设上限；文案不再出现「最长等 0s」。

可证伪点：
  · 把 HUMAN_HANDOFF_WAIT 改回 600 → D 段「跑 400 轮才关窗」会先 timeout（300 轮）→ 红；
  · 把 _wait_human_verify 的 `limit = ... else 0` 删掉 → 0 会让 `waited < 0` 一次都不跑 → 红；
  · 把默认 human_wait 改回 600 → E 段签名断言红。
"""

import inspect
import io
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="t77_")
os.environ["RESUME_PROFILES"] = os.path.join(_TMP, "profiles")
os.environ["RESUME_STATE"] = os.path.join(_TMP, "state.json")

import filler  # noqa: E402

PY = sys.executable
PASS, FAIL = [], []


def check(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print("  PASS  %s" % name)
    else:
        FAIL.append(name)
        print("  FAIL  %s  %s" % (name, extra))


# ------------------------------------------------------------ A 两个默认常量
print("== A) 两个等待预算默认都是 0（不设超时）==")
check("HUMAN_HANDOFF_WAIT == 0（交人工不再因超时关窗）",
      filler.HUMAN_HANDOFF_WAIT == 0, "=%r" % (filler.HUMAN_HANDOFF_WAIT,))
check("PAUSE_CONFIRM_WAIT == 0（停在提交前不再因超时关窗）",
      filler.PAUSE_CONFIRM_WAIT == 0, "=%r" % (filler.PAUSE_CONFIRM_WAIT,))

# ------------------------------------------------------------ B _handoff_budget
print("\n== B) _handoff_budget 真值表 ==")
hb = filler._handoff_budget
check("None → 0", hb(None) == 0, "=%r" % (hb(None),))
check("0 → 0", hb(0) == 0, "=%r" % (hb(0),))
check("负数 → 0", hb(-30) == 0, "=%r" % (hb(-30),))
check("脏值 → 0（默认就是不超时）", hb("abc") == 0, "=%r" % (hb("abc"),))
check("显式正数仍原样（限时收尾能力保留）", hb(600) == 600, "=%r" % (hb(600),))

# ------------------------------------------------------------ C _wait_desc
print("\n== C) 文案：0 不许写成「最长等 0s」==")
wd = filler._wait_desc
check("_wait_desc 存在", callable(wd))
check("_wait_desc(0) 含「不设超时」", "不设超时" in wd(0), "=%r" % (wd(0),))
check("_wait_desc(0) 不含「0s」", "0s" not in wd(0), "=%r" % (wd(0),))
check("_wait_desc(60) 写出 60", "60" in wd(60), "=%r" % (wd(60),))
check("_wait_desc(None) 也按不超时", "不设超时" in wd(None), "=%r" % (wd(None),))


# ------------------------------------------------------------ D 行为（可证伪）
class FakePage:
    """最小假页面：只实现两个等待器用到的方法。ticks 记录 wait_for_timeout 次数。"""

    def __init__(self, close_after=None):
        self.ticks = 0
        self.close_after = close_after
        self.closed = False

    def evaluate(self, js, *a, **k):
        return ""            # innerText 等 → 空串；_form_signature 会自己兜底

    def is_closed(self):
        return self.closed

    def wait_for_timeout(self, ms):
        self.ticks += 1
        if self.close_after is not None and self.ticks >= self.close_after:
            self.closed = True
        return None


print("\n== D) 行为对照：0 = 不设上限；正数仍有上限 ==")
# 600s 预算 = 300 轮（每轮 2s）。跑 400 轮：若还有 600 上限会先 timeout。
p1 = FakePage(close_after=400)
st1, _ = filler._wait_human_verify(p1, budget=0, detector=lambda: False,
                                   what="登录")
check("_wait_human_verify(budget=0) 跑满 400 轮才因关窗结束",
      st1 == "closed" and p1.ticks == 400, "state=%r ticks=%d" % (st1, p1.ticks))

p2 = FakePage(close_after=400)
st2, _ = filler._wait_human_reach_form(p2, budget=0)
check("_wait_human_reach_form(budget=0) 跑满 400 轮才因关窗结束",
      st2 == "closed" and p2.ticks == 400, "state=%r ticks=%d" % (st2, p2.ticks))

p3 = FakePage()
st3, _ = filler._wait_human_verify(p3, budget=4, detector=lambda: False, what="登录")
check("显式 budget=4 仍会到点 timeout（上限能力没被删掉）",
      st3 == "timeout", "state=%r ticks=%d" % (st3, p3.ticks))

# 心跳文案：不设上限时不许带「/ 最长」
p4 = FakePage(close_after=filler._PARK_HEARTBEAT + 1)
notes = []
filler._wait_human_verify(p4, note=notes.append, budget=0,
                          detector=lambda: False, what="登录")
check("不设上限的心跳不带「/ 最长」",
      bool(notes) and "最长" not in notes[0], "notes=%r" % (notes,))

# ------------------------------------------------------------ E 静态接线
print("\n== E) 静态接线（三个入口默认必须是 None，否则常量改了也白改）==")
for fn in ("_login_phase", "apply_by_url", "_apply_core"):
    d = inspect.signature(getattr(filler, fn)).parameters["human_wait"].default
    check("%s 的 human_wait 默认 None" % fn, d is None, "=%r" % (d,))

fsrc = io.open(os.path.join(HERE, "filler.py"), encoding="utf-8").read()
check("filler 里已无 `while waited < budget`（旧写法会在 budget=0 时立刻退出）",
      "while waited < budget:" not in fsrc)
check("两个等待器都写了 `limit = budget if budget > 0 else 0`",
      fsrc.count("limit = budget if budget > 0 else 0") == 2,
      "count=%d" % fsrc.count("limit = budget if budget > 0 else 0"))
check("`budget = int(budget or _handoff_budget())` 旧写法已清除",
      "budget = int(budget or _handoff_budget())" not in fsrc)
check("human_wait=600 默认值已全部改掉", "human_wait=600" not in fsrc)
check("HUMAN_HANDOFF_WAIT 用环境变量声明（可恢复限时）",
      'HUMAN_HANDOFF_WAIT = _env_secs("RESUME_HUMAN_HANDOFF_WAIT"' in fsrc)

ssrc = io.open(os.path.join(HERE, "server.py"), encoding="utf-8").read()
_hw_lines = [ln for ln in ssrc.splitlines()
             if "human_wait=" in ln and not ln.lstrip().startswith("#")]
check("server 未把 human_wait 写死（不覆盖默认）", not _hw_lines,
      "lines=%r" % (_hw_lines[:3],))

# ------------------------------------------------------------ F 环境变量覆盖
print("\n== F) 环境变量可恢复限时（用户想改回来时不用改代码）==")
env = dict(os.environ)
env["RESUME_HUMAN_HANDOFF_WAIT"] = "5"
r = subprocess.run([PY, "-c", "import filler;print(filler.HUMAN_HANDOFF_WAIT)"],
                   cwd=HERE, env=env, capture_output=True, text=True, timeout=120)
check("RESUME_HUMAN_HANDOFF_WAIT=5 → 5", (r.stdout or "").strip() == "5",
      "out=%r err=%r" % (r.stdout, r.stderr[-200:]))

# ------------------------------------------------------------ 汇总
print("\n通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：" + "；".join(FAIL))
    sys.exit(1)
print("全部 PASS：窗口不再因等待超时被关闭（T77 + T77b）")
