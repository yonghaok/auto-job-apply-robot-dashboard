# -*- coding: utf-8 -*-
"""smoke_t73_pause_wait.py —— 锁定「延长填表确认等待时间」（T73）修复。

背景（用户反馈）：单岗位投递「停在提交前」后，等人工点提交的等待**过去一律传 0 =
不超时（无限等）**。无限等会让看板永远停在「待人工提交」、任务永远收不了尾
（daemon 线程一直挂着、状态没有终态）。

T77（2026-10-28）用户追加要求：**不许因为等待超时把窗口关掉** —— 默认改成不设超时。
`PAUSE_CONFIRM_WAIT` 默认 0（= 不超时），要限时收尾可设 RESUME_PAUSE_CONFIRM_WAIT=<秒>。
解析仍走纯函数 `_pause_budget`：
  · None（调用方未指定）→ PAUSE_CONFIRM_WAIT（**默认 0 = 不超时**）
  · 0 / 负数            → 0 = 不超时
  · 正数                → 原样（显式上限仍生效）
  · 脏值（非数字）      → 回落到 PAUSE_CONFIRM_WAIT

覆盖：
  A `_pause_budget` 纯函数真值表（含 None / 0 / 负数 / 正数 / 字符串 / 脏值）
  B PAUSE_CONFIRM_WAIT 常量取值与环境变量覆盖（子进程验证）
  C 反向断言：默认**就是**「不超时」，但显式正数仍有上限
  D 静态接线：filler 两个入口默认 None；停表分支用 _pause_budget；server/前端同步
  E 行为对照：确 2s 上限真的会在 2s 收尾；confirm_wait=0 仍只在关窗时结束
"""

import inspect
import io
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="t73_")
os.environ["RESUME_PROFILES"] = os.path.join(_TMP, "profiles")
os.environ["RESUME_STATE"] = os.path.join(_TMP, "state.json")

import filler  # noqa: E402

PY = sys.executable
PASS = []
FAIL = []


def check(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print("  PASS  %s" % name)
    else:
        FAIL.append(name)
        print("  FAIL  %s  %s" % (name, extra))


# ---------------------------------------------------------------- A 纯函数真值表
print("== A) _pause_budget 纯函数真值表 ==")
pb = getattr(filler, "_pause_budget", None)
check("_pause_budget 存在且可调用", callable(pb))
if callable(pb):
    check("None（未指定）→ PAUSE_CONFIRM_WAIT",
          pb(None) == filler.PAUSE_CONFIRM_WAIT, "=%r" % (pb(None),))
    check("0 → 0（不超时，语义未变）", pb(0) == 0, "=%r" % (pb(0),))
    check("负数 → 0（不超时）", pb(-5) == 0, "=%r" % (pb(-5),))
    check("正数 → 原样", pb(120) == 120, "=%r" % (pb(120),))
    check("数字字符串 → int", pb("60") == 60, "=%r" % (pb("60"),))
    check("脏值(非数字) → 回落 PAUSE_CONFIRM_WAIT（不许变不超时）",
          pb("abc") == filler.PAUSE_CONFIRM_WAIT, "=%r" % (pb("abc"),))
    check("Python None 之外的空串 → 回落 PAUSE_CONFIRM_WAIT",
          pb("") == filler.PAUSE_CONFIRM_WAIT, "=%r" % (pb(""),))

# ---------------------------------------------------------------- B 常量与覆盖
print("\n== B) PAUSE_CONFIRM_WAIT 常量 + 环境变量覆盖 ==")
check("PAUSE_CONFIRM_WAIT 默认 0（不设超时，窗口不会被超时关掉）",
      filler.PAUSE_CONFIRM_WAIT == 0, "=%r" % (filler.PAUSE_CONFIRM_WAIT,))
check("PAUSE_CONFIRM_WAIT 非负（0 或正数）", int(filler.PAUSE_CONFIRM_WAIT) >= 0,
      "=%r" % (filler.PAUSE_CONFIRM_WAIT,))

env = dict(os.environ)
env["RESUME_PAUSE_CONFIRM_WAIT"] = "7"
r = subprocess.run([PY, "-c",
                    "import filler;print(filler.PAUSE_CONFIRM_WAIT)"],
                   cwd=HERE, env=env, capture_output=True, text=True, timeout=120)
check("RESUME_PAUSE_CONFIRM_WAIT 可覆盖（=7）",
      (r.stdout or "").strip() == "7", "out=%r err=%r" % (r.stdout, r.stderr[-200:]))

# ---------------------------------------------------------------- C 反向断言
print("\n== C) 反向断言：默认不设超时，但显式正数仍有上限 ==")
check("_pause_budget(None) == 0（默认不设超时）",
      callable(pb) and pb(None) == 0, "=%r" % (pb(None) if callable(pb) else None,))
check("显式 3600 仍被采纳（限时收尾能力没被删掉）",
      callable(pb) and pb(3600) == 3600, "=%r" % (pb(3600) if callable(pb) else None,))
check("apply_by_url 的 confirm_wait 默认是 None（不是 0）",
      inspect.signature(filler.apply_by_url).parameters["confirm_wait"].default is None,
      "=%r" % (inspect.signature(filler.apply_by_url).parameters["confirm_wait"].default,))
check("_apply_core 的 confirm_wait 默认是 None（不是 0）",
      inspect.signature(filler._apply_core).parameters["confirm_wait"].default is None,
      "=%r" % (inspect.signature(filler._apply_core).parameters["confirm_wait"].default,))
check("底层 _park_before_submit 默认仍是 0（0 = 不超时，底层语义不被动过）",
      inspect.signature(filler._park_before_submit).parameters["confirm_wait"].default == 0,
      "=%r" % (inspect.signature(filler._park_before_submit).parameters["confirm_wait"].default,))

# ---------------------------------------------------------------- D 静态接线
print("\n== D) 静态接线（filler / server / 前端 三处必须同步）==")
fsrc = io.open(os.path.join(HERE, "filler.py"), encoding="utf-8").read()
check("filler：停表分支改用 _pause_budget(confirm_wait)",
      "_cw = _pause_budget(confirm_wait)" in fsrc)
check("filler：_park_before_submit 处不再直接用 confirm_wait（改成 _cw）",
      "_park_before_submit(page, note=note, confirm_wait=confirm_wait," not in fsrc)
check("filler：timeout 文案用的是解析后的 _cw（否则会写「等待 0s」）",
      "% _cw)" in fsrc)
check("filler：_park_before_submit 的 confirm_wait<=0=不超时 文档仍在",
      "confirm_wait <= 0 表示**不超时**" in fsrc)
check("filler：PAUSE_CONFIRM_WAIT 用 _env_secs 声明（可配）",
      'PAUSE_CONFIRM_WAIT = _env_secs("RESUME_PAUSE_CONFIRM_WAIT"' in fsrc)

ssrc = io.open(os.path.join(HERE, "server.py"), encoding="utf-8").read()
check("server：start_apply_one 默认 confirm_wait=None",
      "def start_apply_one(pid, mode=posmod.MODE_STRICT, pause_before_submit=True," in ssrc
      and "confirm_wait=None):" in ssrc)
check("server：start_apply_one 保留 None（不再 int(... or 0) 抹平）",
      'it["confirm_wait"] = (None if confirm_wait is None else int(confirm_wait))' in ssrc)
check("server：_apply_one_item 读 items 时保留 None",
      'confirm_wait=(None if it.get("confirm_wait") is None' in ssrc)
check("server：/api/preselect/<id>/apply 保留 None",
      'confirm_wait=(None if data.get("confirm_wait") is None' in ssrc)
check("server：single_apply_default 反映真实默认（用 filler.PAUSE_CONFIRM_WAIT）",
      '"confirm_wait": filler.PAUSE_CONFIRM_WAIT}' in ssrc)
check("server：preselect 端点保留 None（apply-all 端点保持原样 or 0，无需改）",
      'confirm_wait=(None if data.get("confirm_wait") is None' in ssrc
      and 'else int(data.get("confirm_wait") or 0))))' in ssrc)

hsrc = io.open(os.path.join(HERE, "..", "frontend", "index.html"),
               encoding="utf-8").read()
check("前端：提示文案不再写「（不超时）」",
      "窗口保持打开（不超时）" not in hsrc)
check("前端：提示文案写明「不设超时 / 不会自动关闭」",
      "窗口保持打开、**不设超时**（不会因为等你而自动关闭）" in hsrc)
check("前端：旧文案「默认最长等 60 分钟」已移除",
      "默认最长等 60 分钟" not in hsrc)

# ---------------------------------------------------------------- E 行为对照
print("\n== E) 行为对照（确 2s 上限真的收尾；0 / 默认都只在关窗时结束）==")


class FakePage:
    """最小假页面：只实现 _park_before_submit 用到的那几个方法。"""

    def __init__(self, body="", flag=False, close_after=None):
        self.body = body
        self.flag = flag
        self.close_after = close_after
        self.ticks = 0
        self.closed = False

    def evaluate(self, js, *a, **k):
        if "resumeHumanSubmit" in js and "===" in js:
            return self.flag
        if "innerText" in js:
            return self.body
        return None

    def is_closed(self):
        return self.closed

    def wait_for_timeout(self, ms):
        self.ticks += 1
        if self.close_after is not None and self.ticks >= self.close_after:
            self.closed = True


p1 = FakePage()                       # 没人点提交、也不关窗
st1, _ = filler._park_before_submit(p1, confirm_wait=2)
check("confirm_wait=2 → 到点返回 timeout（真的有上限）",
      st1 == "timeout", "state=%r ticks=%d" % (st1, p1.ticks))
check("confirm_wait=2 确实只跑了 2 轮", p1.ticks == 2, "ticks=%d" % p1.ticks)

p2 = FakePage(close_after=3)          # confirm_wait=0 = 不超时 → 只在关窗时结束
st2, _ = filler._park_before_submit(p2, confirm_wait=0)
check("confirm_wait=0 → 不超时，只在关窗时结束（closed）", st2 == "closed",
      "state=%r" % (st2,))

# 默认路径（None → PAUSE_CONFIRM_WAIT → 0）必须**没有上限**：
# 若有人把默认值改回 3600，心跳会带「/ 最长 3600」，下面这条就会红。
p4 = FakePage(close_after=filler._PARK_HEARTBEAT + 1)
notes4 = []
st4, _ = filler._park_before_submit(p4, note=notes4.append,
                                    confirm_wait=filler._pause_budget(None))
check("默认（None）不设超时：一直等到关窗才结束", st4 == "closed",
      "state=%r ticks=%d" % (st4, p4.ticks))
check("默认心跳不带「最长 Ns」（证明确实没有上限）",
      bool(notes4) and "最长" not in notes4[0], "notes=%r" % (notes4,))

p3 = FakePage()                       # 有界等待跑到心跳点会留下「已等待 Ns / 最长 Ns」
notes = []
filler._park_before_submit(p3, note=notes.append, confirm_wait=filler._PARK_HEARTBEAT + 1)
check("有界等待期间有心跳（不像卡死）", len(notes) >= 1, "notes=%r" % (notes,))
check("心跳写明「已等待 Ns」", bool(notes) and "已等待" in notes[0], "notes=%r" % (notes,))
check("心跳写明有界上限（有界等待才带「/ 最长」）",
      bool(notes) and "最长" in notes[0], "notes=%r" % (notes,))

# ---------------------------------------------------------------- 汇总
print("\n通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：" + "；".join(FAIL))
    sys.exit(1)
print("全部 PASS：T73「停在提交前」的长等待已锁定")
