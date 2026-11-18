"""对**真实站点**验证「我已阅读并同意《隐私协议》」的勾选逻辑（需要联网）。

线上现象：卡在「未勾选隐私政策」——「获取验证码」按钮一直禁用。
本探针复现到「投递弹窗弹出」为止，只做两件事：
  ① 读出协议勾选状态（应为 unchecked）；
  ② 自动勾选一次，再读状态（应为 checked）。
**不会点「获取验证码」，因此不会发短信、不会提交任何东西。**

用法：python smoke_agree_real.py
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import filler  # noqa: E402

URL = os.environ.get("RESUME_PROBE_URL",
                     "https://campus.servyou.com.cn/campus-recruitment/servyou/102033#/jobs")
ROW_KW = os.environ.get("RESUME_PROBE_KW", "AI产品经理")


def main():
    from playwright.sync_api import sync_playwright
    print("站点: %s" % URL)
    print("定位行的关键词: %s" % ROW_KW)
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
        try:
            pg = b.new_context().new_page()
            pg.goto(URL, wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(7000)

            hit = filler._click_apply_in_row(pg, [ROW_KW])
            print("点击投递：%r" % hit)
            if not hit:
                h = filler._click_by_text(pg, ["立即投递", "投递简历", "申请职位", "投递"])
                print("兜底点击：%r" % h)
            pg.wait_for_timeout(5000)

            body = pg.inner_text("body")
            print("\n----- 弹窗附近的页面文本（含协议的行）-----")
            for line in body.split("\n"):
                s = line.strip()
                if s and any(k in s for k in filler.AGREE_ROW_TEXTS):
                    print("   | %s" % s[:110])
            print("   页面是否出现「获取验证码」：", "获取验证码" in body)

            st0 = filler._terms_state(pg)
            print("\n[1] 勾选前状态: %s" % st0)
            d = filler._agree_terms(pg)
            print("[2] _agree_terms 返回: %s" % d)
            pg.wait_for_timeout(600)
            st1 = filler._terms_state(pg)
            print("[3] 勾选后状态: %s" % st1)

            # 幂等：再调一次不应把勾点掉
            filler._agree_terms(pg)
            pg.wait_for_timeout(400)
            st2 = filler._terms_state(pg)
            print("[4] 再次调用后状态（应仍为 checked）: %s" % st2)

            # 按钮可点性（用 disabled 属性判断，不点）
            dis = pg.evaluate("""() => {
                const vis = e => { const r=e.getBoundingClientRect(); return r.width>0&&r.height>0; };
                const els = Array.from(document.querySelectorAll('button,div,span,a'))
                    .filter(e => vis(e) && (e.innerText||'').trim().includes('获取验证码'));
                return els.map(e => ({tag:e.tagName.toLowerCase(),
                                      cls:String(e.className||'').slice(0,50),
                                      dis: !!e.disabled || /disabled|is-disabled/.test(String(e.className||''))}));
            }""")
            print("\n[5] 「获取验证码」按钮：", dis)

            ok = (st0 != "checked") and (st1 == "checked") and (st2 == "checked")
            print("\n结果：%s" % ("PASS 真实站点协议勾选可用" if ok else "FAIL 仍未勾选成功"))
            return 0 if ok else 1
        finally:
            b.close()


if __name__ == "__main__":
    sys.exit(main())
