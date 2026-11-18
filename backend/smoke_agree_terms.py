"""「我已阅读并同意《隐私协议》」勾选逻辑的真浏览器自测（可证伪）。

背景：真实运行卡在「未勾选隐私政策」——旧实现只按 class 猜容器
（`[class*="checkbox"]` 等），遇到 class 不规则的站点整个漏掉，于是
「获取验证码」按钮一直禁用。本测试用 4 种真实世界 DOM 形态验证新实现：

  A 原生 checkbox 包在 label 里                 （旧实现能找到）
  B Vant 风格 .van-checkbox__icon              （旧实现能找到）
  C **class 不规则**：<div><i class="ic"></i>我已阅读并同意《隐私协议》</div>
      点 i.ic 加 class "on"                     （旧实现漏掉 ← 线上就是这个）
  D 协议行里的 checkbox 点了**没有任何反应**（模拟被禁用/只读）
      要求：如实报「未勾选」，且**绝不重复点击**（避免把勾选又点掉）

用法：python smoke_agree_terms.py
"""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import filler  # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print("   %-4s %s%s" % ("PASS" if cond else "FAIL", name,
                            ("  " + extra) if extra else ""))


PAGES = {
    # A 原生 checkbox（label 包裹）
    "A": """<html><body><div style="padding:20px">
      <label style="display:block"><input type="checkbox" id="cb">
        我已阅读并同意《用户协议》</label>
      <div id="state">unchecked</div>
      <script>document.getElementById('cb').addEventListener('change',function(){
        document.getElementById('state').textContent = this.checked ? 'checked':'unchecked';});
      </script></div></body></html>""",
    # B Vant 风格
    "B": """<html><body><div style="padding:20px">
      <div class="van-checkbox" style="display:flex;align-items:center;gap:6px">
        <div class="van-checkbox__icon" style="width:20px;height:20px;border:1px solid #999"></div>
        <span class="van-checkbox__label">我已阅读并同意《隐私协议》</span>
      </div>
      <script>document.querySelector('.van-checkbox__icon').addEventListener('click',function(){
        this.classList.toggle('van-checkbox__icon--checked');});</script>
      </div></body></html>""",
    # C class 不规则（线上失败形态）：只有一个 i.ic，点了加 "on"
    "C": """<html><body><div style="padding:20px">
      <div class="agree-line" style="display:flex;align-items:center;gap:6px">
        <i class="ic" style="display:inline-block;width:16px;height:16px;border:1px solid #999"></i>
        <span>我已阅读并同意《隐私协议》</span>
      </div>
      <script>document.querySelector('i.ic').addEventListener('click',function(){
        this.classList.add('on');});</script>
      </div></body></html>""",
    # D 点了没反应（禁用/只读）
    "D": """<html><body><div style="padding:20px">
      <div class="x" style="display:flex;align-items:center;gap:6px">
        <i class="rd" style="display:inline-block;width:16px;height:16px;border:1px solid #999"></i>
        <span>我已阅读并同意《隐私协议》</span>
      </div>
      <script>window.__clicks = 0;
        document.querySelector('i.rd').addEventListener('click',function(){ window.__clicks++; });</script>
      </div></body></html>""",
}


def main():
    from playwright.sync_api import sync_playwright
    tmp = tempfile.mkdtemp(prefix="agree-")
    paths = {}
    for k, html in PAGES.items():
        p = os.path.join(tmp, "page_%s.html" % k)
        with open(p, "w", encoding="utf-8") as f:
            f.write(html)
        paths[k] = "file:///" + p.replace("\\", "/")

    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
        try:
            # ---------------- A 原生 checkbox ----------------
            pg = b.new_context().new_page()
            pg.goto(paths["A"])
            check("A 初始状态为 unchecked", filler._terms_state(pg) == "unchecked")
            d = filler._agree_terms(pg)
            print("      A 描述: %s" % d)
            check("A 勾选后状态为 checked", filler._terms_state(pg) == "checked")
            check("A 原生 checkbox 确实被选中",
                  pg.evaluate("() => document.getElementById('cb').checked") is True)

            # ---------------- B Vant ----------------
            pg = b.new_context().new_page()
            pg.goto(paths["B"])
            check("B 初始状态为 unchecked", filler._terms_state(pg) == "unchecked")
            d = filler._agree_terms(pg)
            print("      B 描述: %s" % d)
            check("B 勾选后状态为 checked", filler._terms_state(pg) == "checked")
            check("B 图标带上了 --checked class",
                  "checked" in (pg.evaluate(
                      "() => document.querySelector('.van-checkbox__icon').className") or ""))

            # ---------------- C class 不规则（线上失败形态）----------------
            pg = b.new_context().new_page()
            pg.goto(paths["C"])
            check("C 初始状态为 unchecked", filler._terms_state(pg) == "unchecked")
            d = filler._agree_terms(pg)
            print("      C 描述: %s" % d)
            cls = pg.evaluate("() => document.querySelector('i.ic').className")
            check("C 点击后图标 class 变为 ic on（勾上）", "on" in cls.split(), "class=%r" % cls)
            check("C 状态识别为 checked", filler._terms_state(pg) == "checked")

            # 关键回归：再调一次不能把勾点掉
            filler._agree_terms(pg)
            cls2 = pg.evaluate("() => document.querySelector('i.ic').className")
            check("C 重复调用不会取消勾选（幂等）", "on" in cls2.split(), "class=%r" % cls2)

            # ---------------- D 点了没反应 ----------------
            pg = b.new_context().new_page()
            pg.goto(paths["D"])
            d = filler._agree_terms(pg)
            print("      D 描述: %s" % d)
            n1 = pg.evaluate("() => window.__clicks")
            check("D 第一次尝试点击了 1 次", n1 == 1, "clicks=%d" % n1)
            check("D 如实报告未勾选", filler._terms_state(pg) == "unchecked")
            for _ in range(3):
                filler._agree_terms(pg)          # 模拟上层重试 3 次
            n2 = pg.evaluate("() => window.__clicks")
            check("D 重试 3 次后总点击仍为 1（绝不重复点）", n2 == 1, "clicks=%d" % n2)
        finally:
            b.close()

    # ---------------- 关键词覆盖 ----------------
    print("\n=== 关键词覆盖 ===")
    for kw in ("隐私协议", "隐私政策", "用户协议", "我已阅读并同意",
               "已阅读并同意《隐私条款》"):
        check("协议行关键词覆盖：%s" % kw,
              any(k in kw for k in filler.AGREE_ROW_TEXTS))

    ok = sum(1 for _n, v in RESULTS if v)
    print("\n================ 汇总 ================")
    for n, v in RESULTS:
        if not v:
            print("  FAILED: %s" % n)
    print("  %d/%d PASS" % (ok, len(RESULTS)))
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
