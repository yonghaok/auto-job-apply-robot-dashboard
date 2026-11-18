# -*- coding: utf-8 -*-
"""标签页归属（防「抢别家岗位的窗口」）回归（T65）。

用户报：「手机验证码分发错误，**一号窗口的验证码填入了三号窗口**」。
根因：CDP 模式下 `ctx` 是用户**真实浏览器**的默认 context，一键投递的并发岗位共享
同一份 `ctx.pages`；而 `_adopt_new_tab` 只按「差集 + 取最后一个」认新标签页
→ A 岗位把 B 岗位刚打开的标签页抢过来（还把 A 自己那页关了）。
登录闸门挡不住：闸门只串行「谁在登录」，挡不住「谁的页面被换掉」。

覆盖
 A) 归属登记原语：claim / claimable / try_claim（原子）/ release / 当前岗位 key
 B) `_adopt_new_tab` 行为：别家的页不抢 · 无主同站页收养并关旧页 · 非本站在页忽略且归还
 C) **证伪**：把归属门禁短路掉 → 必须重现「抢过来还关掉自己那页」
 D) 并发原子性：N 个线程抢同一个无主页 → 恰好 1 个赢
 E) 源码结构：收养看归属 · 主流程登记 · 收尾解除

用法（cwd=backend）：<venv-python> smoke_page_owner.py
"""
import inspect
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["RESUME_CDP_ENDPOINT"] = "http://127.0.0.1:59999"

import filler                      # noqa: E402

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


class FakePage:
    """只实现 `_adopt_new_tab` 会用到的接口。"""

    def __init__(self, url, name="p"):
        self.url = url
        self.name = name
        self.closed = False
        self.handlers = []

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True

    def on(self, ev, fn):
        self.handlers.append((ev, fn))

    def wait_for_timeout(self, _ms):
        pass

    def wait_for_load_state(self, *_a, **_k):
        pass

    def __repr__(self):
        return "<%s %s%s>" % (self.name, self.url[:34], " closed" if self.closed else "")


class FakeCtx:
    def __init__(self, pages):
        self.pages = list(pages)


def reset():
    filler._PAGE_OWNER.clear()
    filler.set_task_key("")


def main():
    print("\n=== A. 归属登记原语 ===")
    reset()
    pg = FakePage("https://a.example.com/x", "pg")
    filler.set_task_key("A")
    check("A1 未登记时是「无主」→ 任何岗位都可收养", filler._claimable(pg, "B"))
    filler.claim_page(pg)
    check("A2 claim 后归属到本岗位（key 缺省取线程当前岗位）",
          filler._PAGE_OWNER.get(pg) == "A")
    check("A3 自己仍然可收养自己的页", filler._claimable(pg, "A"))
    check("A4 **别的岗位不可收养**", not filler._claimable(pg, "B"))
    check("A5 try_claim 对别家的页返回 False（抢不到）", not filler._try_claim(pg, "B"))
    check("A6 抢失败后归属没被改写", filler._PAGE_OWNER.get(pg) == "A")
    filler.release_page(pg)
    check("A7 release 后回到无主", filler._claimable(pg, "B"))
    check("A8 _cur_key 跟随线程当前岗位",
          (filler.set_task_key("Z"), filler._cur_key())[1] == "Z")

    print("\n=== B. _adopt_new_tab 行为 ===")
    # B1 别家岗位刚开的页 → 不许抢，也不许关自己那页
    reset()
    filler.set_task_key("A")
    mine = FakePage("https://job.example.com/list", "mine")
    theirs = FakePage("https://job.example.com/form", "theirs")
    filler.claim_page(mine)
    filler.claim_page(theirs, "C")          # C 岗位先占了
    ctx = FakeCtx([mine])
    before = [mine]
    ctx.pages.append(theirs)                # 差集里出现 C 的页
    notes = []
    got = filler._adopt_new_tab(ctx, mine, before, notes.append, "测试")
    check("B1 **不抢别家岗位的标签页**（返回自己那页）", got is mine, repr(got))
    check("B1 自己那页**没有被关掉**", not mine.closed)
    check("B1 归属没被改写（仍是 C 的）", filler._PAGE_OWNER.get(theirs) == "C")
    check("B1 备注说明「不抢别人的窗口」",
          any("不抢" in x for x in notes), repr(notes))

    # B2 无主同站页 → 收养，关掉旧页，并解除旧页登记
    reset()
    filler.set_task_key("A")
    mine = FakePage("https://job.example.com/list", "mine")
    fresh = FakePage("https://job.example.com/form", "fresh")
    filler.claim_page(mine)
    ctx = FakeCtx([mine])
    before = [mine]
    ctx.pages.append(fresh)
    got = filler._adopt_new_tab(ctx, mine, before, lambda m: None, "测试")
    check("B2 收养无主同站新页", got is fresh, repr(got))
    check("B2 旧页被关掉（不再两个页面同时投递）", mine.closed)
    check("B2 旧页登记已解除（不留垃圾）", mine not in filler._PAGE_OWNER)
    check("B2 新页登记为本岗位", filler._PAGE_OWNER.get(fresh) == "A")

    # B3 无主但非本站在页（广告/第三方）→ 忽略，且归还登记
    reset()
    filler.set_task_key("A")
    mine = FakePage("https://job.example.com/list", "mine")
    ads = FakePage("https://ads.other-cdn.net/x", "ads")
    filler.claim_page(mine)
    ctx = FakeCtx([mine])
    before = [mine]
    ctx.pages.append(ads)
    notes = []
    got = filler._adopt_new_tab(ctx, mine, before, notes.append, "测试")
    check("B3 非本站在页被忽略（返回自己那页）", got is mine, repr(got))
    check("B3 自己那页没被关", not mine.closed)
    check("B3 该页登记已归还（不占着别人的页）", ads not in filler._PAGE_OWNER)
    check("B3 备注说明「忽略非本站在页」",
          any("非本站在页" in x for x in notes), repr(notes))

    # B4 没有新页 → 原样返回
    reset()
    filler.set_task_key("A")
    mine = FakePage("https://job.example.com/list", "mine")
    ctx = FakeCtx([mine])
    check("B4 没有新标签页时原样返回、不关页",
          filler._adopt_new_tab(ctx, mine, [mine], lambda m: None, "测试") is mine
          and not mine.closed)

    print("\n=== C. 证伪：短路归属门禁 → 必须重现抢窗 ===")
    reset()
    filler.set_task_key("A")
    real_claimable, real_try = filler._claimable, filler._try_claim
    filler._claimable = lambda pg, key: True          # 等价于旧逻辑「谁都行」
    filler._try_claim = lambda pg, key: True          # 等价于旧逻辑「直接抢到」
    try:
        mine = FakePage("https://job.example.com/list", "mine")
        theirs = FakePage("https://job.example.com/form", "theirs")
        filler.claim_page(mine)
        filler.claim_page(theirs, "C")
        ctx = FakeCtx([mine])
        before = [mine]
        ctx.pages.append(theirs)
        got = filler._adopt_new_tab(ctx, mine, before, lambda m: None, "测试")
    finally:
        filler._claimable, filler._try_claim = real_claimable, real_try
    check("C1 短路后**重现**旧故障：抢了 C 的页", got is theirs, repr(got))
    check("C2 短路后还会**把 A 自己那页关掉**（旧代码的连带伤害）", mine.closed)
    check("C3 → 说明 B1 的断言确实钉住了这次修复（不是恒真）", not mine.closed is False)

    print("\n=== D. 抢占的原子性（并发）===")
    reset()
    target = FakePage("https://job.example.com/form", "target")
    wins = []
    lock = threading.Lock()

    def race(k):
        ok = filler._try_claim(target, "job%d" % k)
        if ok:
            with lock:
                wins.append(k)

    ts = [threading.Thread(target=race, args=(i,)) for i in range(24)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("D1 24 个岗位同时抢一个无主页 → 恰好 1 个抢到", len(wins) == 1, repr(wins))
    check("D2 最终归属就是那个赢家",
          filler._PAGE_OWNER.get(target) == "job%d" % wins[0] if wins else False,
          repr(filler._PAGE_OWNER.get(target)))

    print("\n=== E. 源码结构 ===")
    asrc = inspect.getsource(filler._adopt_new_tab)
    csrc = inspect.getsource(filler._apply_core)
    check("E1 收养时先看归属（_claimable）", "_claimable(pg, key)" in asrc)
    check("E2 收养时原子抢占（_try_claim）", "_try_claim(newpg, key)" in asrc)
    check("E3 别家页面直接返回自己那页（不抢）", "不抢别人的窗口" in asrc)
    check("E4 主流程开页后登记归属", "claim_page(page)" in csrc)
    check("E5 收尾解除登记", "release_page(page)" in csrc)
    check("E6 登记表有清理（防 page 对象被字典长期持有）",
          "def _sweep_pages" in inspect.getsource(filler) and "is_closed()" in
          inspect.getsource(filler._sweep_pages))

    print("\n===== 汇总：PASS=%d FAIL=%d =====" % (PASS, FAIL))
    reset()
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
