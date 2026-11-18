# -*- coding: utf-8 -*-
"""T69 回归：一键投递的**卡死看门狗**。

背景（用户实测）：点了「取消投递」后看板一直停在「正在取消…」。
根因：Playwright 的 node 驱动因 `Protocol error (Page.handleJavaScriptDialog)`
未捕获异常**整体崩掉** → 还在等它的 worker **永久阻塞**（不抛错、不返回），
而旧代码用 `with ThreadPoolExecutor(...) as ex`（`__exit__` = `shutdown(wait=True)`）
→ `running` 恒 True。

本套件钉住：
  A 正常批次照旧跑完；
  B **慢但在进展**的批次不许被误判卡死（停滞计时必须按「done 有没有前进」重置，
    而不是按「本轮总耗时」——否则长批次会被误杀）；
  C 真卡死时到预算必须**强制收尾**：running→False / wedged>0 / 卡住的岗位复位「待投递」；
  D `_apply_stall_budget()` 取消后收紧、未取消时放宽；
  E 源码结构断言（不许再出现 `as ex` + `wait=True`）；
  F **证伪**：把修复前的写法原样跑同一个卡死场景 → running 恒 True、状态卡「投递中」，
    证明本套件确实钉住了这次修复，而不是恒真。

隔离：`RESUME_STATE` 指向临时文件（set_pos_status 会真的落盘）；不启浏览器。
"""
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ---- 先隔离落盘，再 import server ----
_TMP = tempfile.mkdtemp(prefix="t69_")
os.environ["RESUME_STATE"] = os.path.join(_TMP, "state.json")
os.environ["RESUME_CDP_ENDPOINT"] = "http://127.0.0.1:59999"   # 死端口：绝不碰用户真浏览器
os.environ["RESUME_APPLY_STUCK_GRACE"] = "2"     # 取消后的宽限（秒）
os.environ["RESUME_APPLY_HARD_STUCK"] = "3"      # 未取消时的宽限（秒）

import server as S  # noqa: E402

N = [0]
BAD = []


def ck(name, cond, extra=""):
    N[0] += 1
    if cond:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s   %s" % (name, extra))
        BAD.append(name)


RELEASE = threading.Event()      # 放行「卡死」的假 worker，好让测试进程能退出


def reset_apply_job(total):
    with S.LOCK:
        S.APPLY_JOB.update(running=True, total=total, done=0, ok=0, fail=0,
                           human=0, skipped=0, cancelled=False, wedged=0)
    S.CANCEL_EVENT.clear()


def mk_items(n, prefix="p"):
    return [{"pid": "%s%d" % (prefix, i), "company": "C", "title": "T%d" % i,
             "url": "http://x/%d" % i, "pos_key": ""} for i in range(n)]


def run_worker(items, conc, timeout):
    t = threading.Thread(target=S._apply_all_worker, args=(items, conc), daemon=True)
    t.start()
    t.join(timeout)
    return t


def st_of(pid):
    with S.LOCK:
        rec = S.POS_STATE.get(pid) or {}
        return (rec.get("status") or ""), (rec.get("note") or "")


# =========================================================================
print("== A. 正常批次：照旧跑完 ==")
itemsA = mk_items(5, "a")
reset_apply_job(len(itemsA))


def fake_ok(it):
    time.sleep(0.05)
    S.set_pos_status(it["pid"], "已投递", note="fake：页面出现成功文案", fresh=True)
    return "ok"


S._apply_one_item = fake_ok
tA = run_worker(itemsA, 2, 20)
ck("A1 批次线程正常结束（没被卡住）", not tA.is_alive())
ck("A2 running=False", S.APPLY_JOB["running"] is False, S.APPLY_JOB)
ck("A3 done=5", S.APPLY_JOB["done"] == 5, S.APPLY_JOB["done"])
ck("A4 ok=5", S.APPLY_JOB["ok"] == 5, S.APPLY_JOB["ok"])
ck("A5 wedged=0（正常批次不许记卡死）", S.APPLY_JOB["wedged"] == 0, S.APPLY_JOB["wedged"])
ck("A6 cancelled=False", S.APPLY_JOB["cancelled"] is False)
ck("A7 五个岗位都是已投递",
   all(st_of(i["pid"])[0] == "已投递" for i in itemsA),
   [st_of(i["pid"])[0] for i in itemsA])

# =========================================================================
print("== B. 慢但在进展：不许误判卡死 ==")
# 每个岗位 1.2s，3 个串行 ≈ 3.6s；未取消时预算 = APPLY_HARD_STUCK = 3s。
# 「本轮总耗时 > 预算」的朴素实现会在第 3 个岗位上误杀 → 这里必须 wedged=0 且 done=3。
itemsB = mk_items(3, "b")
reset_apply_job(len(itemsB))


def fake_slow(it):
    time.sleep(1.2)
    S.set_pos_status(it["pid"], "已投递", note="fake：慢但成功", fresh=True)
    return "ok"


S._apply_one_item = fake_slow
tB = run_worker(itemsB, 1, 30)
ck("B1 慢批次正常跑完（没被误杀）", (not tB.is_alive()) and S.APPLY_JOB["running"] is False)
ck("B2 done=3", S.APPLY_JOB["done"] == 3, S.APPLY_JOB["done"])
ck("B3 wedged=0（进展中不算卡死）", S.APPLY_JOB["wedged"] == 0, S.APPLY_JOB["wedged"])
ck("B4 总耗时已超过预算（>3s）却仍判为正常",
   S.APPLY_JOB["wedged"] == 0 and S.APPLY_JOB["done"] == 3)
ck("B5 三个岗位都是已投递", all(st_of(i["pid"])[0] == "已投递" for i in itemsB))

# =========================================================================
print("== C. 真卡死 + 已取消：必须强制收尾 ==")
itemsC = mk_items(3, "c")
reset_apply_job(len(itemsC))
# c0 正常完成；c1 永久卡死（模拟驱动崩掉后永不返回）；c2 排在后面还没轮到
S.CANDIDATES[:] = [{"id": "c0", "status": "", "note": ""},
                   {"id": "c1", "status": "", "note": ""}]      # 走 CANDIDATES 分支
S.POS_STATE["c2"] = {"status": "待人工提交", "note": "人工已进窗口但没提交（早前的结果）",
                     "updated": "2026-09-23 22:00:00"}            # 走 POS_STATE 分支 + 终态保护


EXEC = {}


def fake_mixed(it):
    EXEC[it["pid"]] = EXEC.get(it["pid"], 0) + 1
    if it["pid"] == "c1":
        S.set_pos_status("c1", "投递中", note="fake：开始打开岗位页…", fresh=True)
        RELEASE.wait(120)          # 永久卡死（直到测试放行）
        return "fail"
    if it["pid"] == "c0":
        S.set_pos_status("c0", "已投递", note="fake：成功", fresh=True)
        return "ok"
    return "skipped"               # c2：本轮不该轮到它（放行 c1 之后才可能被跑到）


S._apply_one_item = fake_mixed
S.cancel_apply_all()               # 用户点了「取消投递」→ 预算收紧到 APPLY_STUCK_GRACE=2s
tC = run_worker(itemsC, 1, 40)
ck("C1 卡死轮次能自己结束（不再永久 running）", not tC.is_alive())
ck("C2 running=False（UI 不再卡「正在取消…」）", S.APPLY_JOB["running"] is False, S.APPLY_JOB)
ck("C3 cancelled=True", S.APPLY_JOB["cancelled"] is True)
ck("C4 wedged=2（c1 卡住 + c2 没轮到）", S.APPLY_JOB["wedged"] == 2, S.APPLY_JOB["wedged"])
ck("C5 done < total（如实反映没跑完）", S.APPLY_JOB["done"] < S.APPLY_JOB["total"],
   (S.APPLY_JOB["done"], S.APPLY_JOB["total"]))
ck("C6 c0 的真实结果「已投递」没被抹掉", st_of("c0")[0] == "已投递", st_of("c0"))
s1, n1 = st_of("c1")
ck("C7 卡住的 c1 复位为「待投递」", s1 == "待投递", s1)
ck("C8 c1 备注如实写「结果未知」", "结果未知" in n1, n1)
ck("C9 c1 备注**不许**宣称已投递（只许说结果未知）",
   "已投递" not in n1.replace("确认已投递", ""), n1)
ck("C10 没轮到的 c2 保持「待人工提交」不动", st_of("c2")[0] == "待人工提交", st_of("c2"))
ck("C11 c2 备注也没被改写", "早前的结果" in st_of("c2")[1], st_of("c2")[1])
ck("C12 本轮从未轮到 c2（它一直排在队列里）", EXEC.get("c2", 0) == 0, EXEC)
RELEASE.set()                      # 放行，让卡死线程退出（放行后 c2 才可能被跑到，与本轮无关）

# =========================================================================
print("== D. 停滞预算语义 ==")
S.CANCEL_EVENT.clear()
ck("D1 未取消 → 用放宽预算 APPLY_HARD_STUCK",
   S._apply_stall_budget() == S.APPLY_HARD_STUCK == 3, S._apply_stall_budget())
S.CANCEL_EVENT.set()
ck("D2 已取消 → 收紧到 APPLY_STUCK_GRACE",
   S._apply_stall_budget() == S.APPLY_STUCK_GRACE == 2, S._apply_stall_budget())
S.CANCEL_EVENT.clear()

# =========================================================================
print("== E. 源码结构断言 ==")
src = __import__("inspect").getsource(S._apply_all_worker)
ck("E1 不再用 `with ThreadPoolExecutor(...) as ex`", "as ex:" not in src)
ck("E2 不再用 ex.map 一次性等全部", "ex.map" not in src)
ck("E3 明确 shutdown(wait=False)", "shutdown(wait=False)" in src)
ck("E4 用 wait(...) 轮询 done 进展", "wait(pending" in src)
ck("E5 收尾必置 running=False", 'APPLY_JOB["running"] = False' in src)
ck("E6 卡死时登记 wedged", 'APPLY_JOB["wedged"] = len(pending)' in src)
ck("E7 卡死时复位未落终态的岗位", "_reset_stalled_positions(" in src)
allsrc = open(os.path.join(HERE, "server.py"), "rb").read().decode("utf-8")
ck("E8 全部岗位只 submit 一次（fut_item 映射）", "fut_item = {ex.submit" in allsrc)
ck("E9 仍有旧的 ex.map（扫描段）不影响投递段", "list(ex.map(one, items))" not in allsrc)

# =========================================================================
print("== F. 证伪：修复前的写法在同一个卡死场景下确实会卡 ==")
itemsF = mk_items(2, "f")
reset_apply_job(len(itemsF))
S.POS_STATE.pop("f1", None)
RELEASE.clear()


def fake_wedge(it):
    if it["pid"] == "f1":
        S.set_pos_status("f1", "投递中", note="fake：开始打开岗位页…", fresh=True)
        RELEASE.wait(120)
    return "ok"


S._apply_one_item = fake_wedge
_old_running = {"hit": False}


def old_worker(items, conc):
    """修复前的写法（一字不改）：`__exit__` → shutdown(wait=True) → 永不返回。"""
    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(S._apply_one_item, items))
    with S.LOCK:
        S.APPLY_JOB["running"] = False
    _old_running["hit"] = True


tF = threading.Thread(target=old_worker, args=(itemsF, 1), daemon=True)
tF.start()
tF.join(9)
ck("F1 修复前写法：9s 后线程仍卡着（没有收尾）", tF.is_alive())
ck("F2 修复前写法：running 仍为 True → 正是「一直正在取消…」", S.APPLY_JOB["running"] is True)
ck("F3 修复前写法：岗位永远停在「投递中」", st_of("f1")[0] == "投递中", st_of("f1"))
RELEASE.set()
tF.join(10)
ck("F4 放行后旧写法才收尾（证明 F1-F3 是「卡住」而不是「还没跑完」）",
   (not tF.is_alive()) and _old_running["hit"])

# =========================================================================
print("\n===== T69 看门狗：%d/%d %s =====" %
      (N[0] - len(BAD), N[0], "PASSED" if not BAD else ("FAILED -> %s" % BAD)))
sys.exit(1 if BAD else 0)
