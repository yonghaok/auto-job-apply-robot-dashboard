# -*- coding: utf-8 -*-
"""用户 4 项反馈的离线自测（T57~T60）。

覆盖：
  A) 取消投递（协作式取消）：`_is_cancelled` 语义 + `_apply_core` **在开浏览器之前**就短路，
     并返回 stage="已取消"（已投岗位据此回到「待投递」）
  B) 旋转图像人机验证**不再卡住**：形态识别（slider/rotate/grid/unknown）+
     非滑块形态**不碰鼠标**直接转人工 + 成功判定要求正向证据 + 不空等 code_wait
  C) 双开页面：`_same_site` / `_adopt_new_tab`（同站新标签页 → 切过去并关掉本次投递的旧页；
     非本站页 → 不碰；无新页 → 原样返回）
  D) 隐私勾选框：`_agree_terms` / `_terms_state` / `_force_check_terms`
     （真浏览器 + 本地 HTML，无网络）：①勾选框在协议文字**下方且被长文案容器包住**
     （旧实现向上找祖先时因 innerText>160 而 break → 漏检）；②协议文字是**链接**时
     不许点击跳转；③自定义样式（0×0 隐藏）checkbox 的 force 补勾；④无控件页面不误报。
  E) 结构断言：server.py 已接入 CANCEL_EVENT / `cancel_apply_all` / `/api/apply-all/cancel`

用法（cwd=backend）：<venv-python> smoke_fix4_cancel_verify_tabs.py
"""
import inspect
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

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


def info(m):
    print("  [info] %s" % m)


def read(p):
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


# ------------------------------------------------------------------ 假对象
class FakeFrame(object):
    def __init__(self, text=""):
        self._t = text

    def inner_text(self, sel="body"):
        return self._t

    def query_selector_all(self, sel):
        return []

    def query_selector(self, sel):
        return None


class FakePage(object):
    """只实现 filler 在这些路径上真正用到的方法。"""

    def __init__(self, text="", url="https://job.example.com/list"):
        self.frames = [FakeFrame(text)]
        self.url = url
        self.closed = False
        self.loaded = 0
        self.waited = 0

    def wait_for_load_state(self, *a, **k):
        self.loaded += 1

    def wait_for_timeout(self, ms):
        self.waited += ms

    def on(self, ev, fn):
        pass

    def close(self):
        self.closed = True

    def inner_text(self, sel="body"):
        return self.frames[0]._t

    def evaluate(self, *a, **k):
        return {}


class FakeCtx(object):
    def __init__(self, pages):
        self.pages = pages


def main():
    import filler

    # ---------------------------------------------------------------- A 取消
    print("\n=== A. 协作式取消（T57） ===")
    check("_is_cancelled(None) 恒 False", filler._is_cancelled(None) is False)
    check("_is_cancelled 回调抛错 → False（绝不因取消回调出错而中断投递）",
          filler._is_cancelled(lambda: 1 / 0) is False)
    check("_is_cancelled 命中 → True", filler._is_cancelled(lambda: True) is True)

    src = inspect.getsource(filler._apply_core)
    i_cancel = src.find('stage": "已取消"')
    i_pw = src.find("from playwright.sync_api import sync_playwright")
    check("_apply_core 里「取消检查」出现在开启浏览器之前", 0 < i_cancel < i_pw,
          "cancel@%d pw@%d" % (i_cancel, i_pw))

    r = filler._apply_core("https://job.example.com/x", profile={"phone": "13800000000"},
                           emit=lambda m: None, is_cancelled=lambda: True)
    check("取消态下 _apply_core 直接返回（不开浏览器）",
          r.get("stage") == "已取消" and r.get("needs_human") is False, str(r)[:160])
    check("取消返回的 stage 有对应看板状态映射",
          r.get("stage") in read(os.path.join(HERE, "server.py")))

    # ---------------------------------------------------------------- B 验证码形态
    print("\n=== B. 旋转图像人机验证不卡住（T59） ===")
    fake_rotate = FakePage("拖动下方滑块，将图片旋转至正确方向")
    k, ev = filler.classify_verify_kind(fake_rotate)
    check("旋转文案（同时含「滑块」）→ rotate", k == "rotate", "%s/%s" % (k, ev))

    k2, _ = filler.classify_verify_kind(FakePage("请依次点击图中文字"))
    check("点选网格文案 → grid", k2 == "grid", k2)
    k3, _ = filler.classify_verify_kind(FakePage("请按住滑块"))
    check("纯滑块文案 → slider", k3 == "slider", k3)
    k3b, _ = filler.classify_verify_kind(FakePage("请按住滑块，拖动到最右边"))
    check("「拖到最右端」型 → slider_end（不必识别缺口，直接拖到底）",
          k3b == "slider_end", k3b)
    k4, _ = filler.classify_verify_kind(FakePage("欢迎投递本岗位"))
    check("无验证文案 → unknown", k4 == "unknown", k4)
    k5, _ = filler.classify_verify_kind(FakePage("旋转图片 或 拖动滑块"), widget_piece=None)
    check("旋转+滑块共同命中、无拼图块 → rotate（更具体优先）", k5 == "rotate", k5)
    k6, _ = filler.classify_verify_kind(FakePage("旋转图片 或 拖动滑块"),
                                        widget_piece=object())
    check("旋转+滑块共同命中、有独立拼图块 → slider", k6 == "slider", k6)

    notes = []
    import captcha
    _orig_solve = captcha.solve

    def _boom(ctx):
        raise AssertionError("旋转形态下**绝不允许**调用 captcha.solve（会拖坏控件）")

    captcha.solve = _boom
    try:
        ok = filler._attempt_auto_verify(fake_rotate, attempts=3, note=notes.append)
    finally:
        captcha.solve = _orig_solve
    check("旋转验证码：_attempt_auto_verify 直接返回 False（不硬试）", ok is False, str(ok))
    check("并显式告知「转人工」", any("转人工" in m for m in notes), " | ".join(notes)[:200])

    usrc = inspect.getsource(filler._attempt_auto_verify)
    check("成功判定含正向证据（验证码已发出）", "正向证据：验证码已发出" in usrc)
    check("成功判定需复检两次（防「控件刷新瞬间」误判成功）",
          "复检两次均无验证控件" in usrc)
    lsrc = inspect.getsource(filler._login_phase)
    # 2026-09-23 T64：守卫本身没变（仍然是「验码未发出 + 仍挂人机验证 → 不等满 code_wait」），
    # 但那时起**不再一返回就关窗**，而是把窗口交给人（那条注释改写了）。所以这里改成
    # 直接钉住**守卫条件**（比找注释字样强得多），否则每次改文案都会假红。
    check("取码前有不空等保护（验码未发出 + 仍有人机验证 → 不等满 code_wait，交人工）",
          "6.0)" in lsrc
          and ("if get_code and not _code_sent(page) and detect_human_verification(page)"
               in lsrc)
          and "_wait_human_verify(page" in lsrc)
    check("旋转类词是**弱证据**（移入 HUMAN_VERIFY_WEAK_HINTS；需可见控件背书才采信）",
          "旋转" in filler.HUMAN_VERIFY_WEAK_HINTS and
          "rotate" in filler.HUMAN_VERIFY_WEAK_HINTS)

    # ---------------------------------------------------------------- C 双开页面
    print("\n=== C. 双开页面（T58） ===")
    t = [
        ("https://job.example.com/a", "https://job.example.com/b", True),
        ("https://www.job.example.com/a", "https://job.example.com/b", True),
        ("https://apply.example.com/x", "https://job.example.com/y", True),
        ("https://job.example.com/a", "https://ads.other-cdn.net/x", False),
        ("about:blank", "https://job.example.com/a", False),
    ]
    for a, b, want in t:
        got = filler._same_site(a, b)
        check("_same_site(%s, %s) == %s" % (a[-18:], b[-18:], want), got == want, str(got))

    cur = FakePage(url="https://job.example.com/list")
    new = FakePage(url="https://job.example.com/apply/form")
    ctx = FakeCtx([cur, new])
    n1 = []
    got = filler._adopt_new_tab(ctx, cur, [cur], n1.append, "首次点击投递后的页面")
    check("同站新标签页 → 切过去", got is new)
    check("并关掉本次投递自己的旧标签页（不再两个页面同时投递）", cur.closed is True)
    check("日志说明了切换", any("已切换过去" in m for m in n1), " | ".join(n1)[:160])

    cur2 = FakePage(url="https://job.example.com/list")
    ad = FakePage(url="https://ads.other-cdn.net/x")
    n2 = []
    got2 = filler._adopt_new_tab(FakeCtx([cur2, ad]), cur2, [cur2], n2.append, "页面")
    check("非本站新页 → 不切换", got2 is cur2 and cur2.closed is False)
    check("且明确记录「已忽略」", any("已忽略" in m for m in n2), " | ".join(n2)[:160])

    cur3 = FakePage(url="https://job.example.com/list")
    n3 = []
    got3 = filler._adopt_new_tab(FakeCtx([cur3]), cur3, [cur3], n3.append, "页面")
    check("没有新标签页 → 原样返回（行为与旧版一致）",
          got3 is cur3 and cur3.closed is False and not n3)

    asrc = inspect.getsource(filler._apply_core)
    check("首次点击投递后也收养新标签页（旧实现只在登录后那一处做）",
          'before1 = list(ctx.pages)' in asrc and
          '_adopt_new_tab(ctx, page, before1' in asrc)
    check("登录后重投也走同一入口",
          '_adopt_new_tab(ctx, page, before, note, "投递表单")' in asrc)

    # ---------------------------------------------------------------- D 协议勾选
    print("\n=== D. 隐私勾选框（T60，真浏览器 + 本地 HTML） ===")
    check("AGREE_ROW_TEXTS 含「隐私声明 / 同意并阅读」",
          "隐私声明" in filler.AGREE_ROW_TEXTS and "同意并阅读" in filler.AGREE_ROW_TEXTS)
    js = filler._AGREE_FIND_JS
    check("协议行 JS 含链接保护（不点《隐私声明》链接）", "isLink" in js)
    check("协议行 JS 含相邻/兄弟控件兜底", "②b 兜底" in js)

    # 长文案容器：逼出「向上找祖先时 innerText>160 就 break」的漏检路径
    filler_txt = "本岗位招聘流程说明。" * 30
    html_down = """<div class="agree-wrap">
      <p>同意并阅读<a href="https://job.example.com/privacy">《隐私声明》</a></p>
      <p>%s</p>
      <div class="cb-row"><input type="checkbox" id="ag1"></div>
    </div>""" % filler_txt
    html_link_only = """<div><a href="https://job.example.com/p.html">《隐私声明》</a></div>"""
    html_hidden = """<label class="cb"><input type="checkbox" id="h1"
        style="width:0;height:0;opacity:0">我已阅读并同意《隐私条款》</label>"""
    html_none = "<div><span>隐私协议</span></div>"

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
        try:
            pg = b.new_page()

            pg.set_content(html_down)
            check("①前置：勾选框是未勾选的", pg.is_checked("#ag1") is False)
            msg = filler._agree_terms(pg)
            check("①「勾选框在长文案容器里、文字下方」也能被勾上",
                  pg.is_checked("#ag1") is True, "%s | %s" % (pg.is_checked("#ag1"), msg))
            check("①状态读为 checked", filler._terms_state(pg) == "checked",
                  filler._terms_state(pg))

            pg.set_content(html_link_only)
            before_url = pg.url
            filler._agree_terms(pg)
            check("②协议文字是链接时**不跳转**（点它=「勾选框不会被勾选」的元凶之一）",
                  pg.url == before_url, "%s -> %s" % (before_url, pg.url))
            check("②无勾选框时状态为 none（不误报 unchecked）",
                  filler._terms_state(pg) == "none", filler._terms_state(pg))

            pg.set_content(html_hidden)
            f = filler._force_check_terms(pg)
            check("③0×0 隐藏样式的 checkbox 也能 force 补勾",
                  pg.is_checked("#h1") is True, "%r" % f)
            check("③且返回了被勾选的行文案", bool(f) and "我已阅读" in f, repr(f))

            pg.set_content(html_none)
            filler._agree_terms(pg)      # 不应抛异常
            check("④纯文案页不崩，状态 ∈ {none, unchecked}",
                  filler._terms_state(pg) in ("none", "unchecked"), filler._terms_state(pg))
        finally:
            b.close()

    fsrc = inspect.getsource(filler._apply_core)
    check("提交前补勾协议（表单下方那句是提交门禁）",
          "6.5)" in fsrc and "已处理投递表单的同意声明" in fsrc)
    check("登录段补勾也接了 _force_check_terms",
          "_force_check_terms(page)" in inspect.getsource(filler._login_phase))

    # ---------------------------------------------------------------- E server 接线
    print("\n=== E. server.py 接线断言 ===")
    s = read(os.path.join(HERE, "server.py"))
    check("有 CANCEL_EVENT（threading.Event）", "CANCEL_EVENT = threading.Event()" in s)
    check("有 cancel_apply_all()", "def cancel_apply_all(" in s)
    check("有路由 /api/apply-all/cancel", '"/api/apply-all/cancel"' in s)
    check("stage「已取消」→ 看板「待投递」", '"已取消": "待投递"' in s)
    i = s.find("def _apply_one_item")
    j = s.find("\ndef ", i + 1)
    body = s[i:j if j > 0 else len(s)]
    check("_apply_one_item 跳过时把岗位放回「待投递」（并计 skipped）",
          ('"待投递"' in body) and ("skipped" in body), str(len(body)))
    check("取消时先查 CANCEL_EVENT（未开始投递的岗位直接放回队列）",
          "CANCEL_EVENT.is_set()" in body)
    check("投递调用传了 is_cancelled", "is_cancelled=CANCEL_EVENT.is_set" in s)

    print("\n===== 汇总：PASS=%d FAIL=%d =====" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
