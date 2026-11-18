# -*- coding: utf-8 -*-
"""滑块自动过验证失效 + 「登录成功却没识别出投递表单」的离线自测（T61）。

背景（用户 2026-09-23 报）：滑块人机验证失效；部分登录成功的页面没被识别成投递状态，
仍停在「等待人机验证」→ 超时 → 投不成。根因是上一版（T59）**矫枉过正**：
  ① 把「旋转/点选/网格」这类**泛词**塞进强证据 `HUMAN_VERIFY_HINTS`，且
     `classify_verify_kind` 扫**整页**文案 → 岗位页文案里的「网格/旋转」把**滑块**判成
     grid/rotate → 拒绝自动过验证 → 卡在「需人工验证」；
  ② 弱证据词无控件背书也采信 → 普通岗位页被误判成「有人机验证」，一键投递 human_wait=0
     直接返回「需人工验证」不投递；
  ③ AI 视觉通道收到裸数字 → `'int' object has no attribute 'get'` → 整条通道废掉，
     CV 又对「拖到最右端」型滑块必报「未找到缺口轮廓」→ 滑块彻底无解；
  ④ `_code_sent` 只认「59s后重发」，不认「60秒后重新获取 / 重新获取(59)」→ 验证码其实
     已发出却被判「没发出」→ 取码前的「不空等」保护误杀正常流程。

覆盖：
  A) `_code_sent` 中文/括号倒计时
  B) `classify_verify_kind` 只取**控件自身**文案 + slider_end + 默认 unknown
  C) `detect_human_verification` 弱证据词必须有**可见控件**背书
  D) `_attempt_auto_verify`：slider_end 走内置拖到底（不调 CV/AI）；unknown → 默认滑块
  E) `captcha._as_dict` 归一 + AI 通道收到裸数字不再崩
  F) `_wait_delivery_form` 轮询等页面切换
  G) `_looks_like_delivery_form` 放宽后不误判筛选页
  H) 源码结构断言

用法（cwd=backend）：<venv-python> smoke_slider_fix.py
"""
import inspect
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 隔离：不接管调试端口上的 Edge（本文件纯函数 + 假对象，不启浏览器；沿用项目约定）
os.environ["RESUME_CDP_ENDPOINT"] = "http://127.0.0.1:59999"

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


# ------------------------------------------------------------------ 假对象
class FakeMouse(object):
    def __init__(self):
        self.events = []

    def move(self, x, y):
        self.events.append(("move", x, y))

    def down(self):
        self.events.append(("down",))

    def up(self):
        self.events.append(("up",))

    def click(self, x, y):
        self.events.append(("click", x, y))


class FakeEl(object):
    """验证控件元素 / 控件内的子元素。"""

    def __init__(self, text="", visible=True, box=None):
        self._t = text
        self._v = visible
        self._box = box or {"x": 0, "y": 0, "width": 300, "height": 40}

    def inner_text(self):
        return self._t

    def is_visible(self):
        return self._v

    def bounding_box(self):
        return self._box

    def screenshot(self):
        return b"\x89PNG\r\n\x1a\n" + b"0" * 40

    def query_selector_all(self, sel):
        return []


class FakeFrame(object):
    def __init__(self, text="", widgets=None):
        self._t = text
        self._widgets = list(widgets or [])

    def inner_text(self, sel="body"):
        return self._t

    def query_selector_all(self, sel):
        return list(self._widgets)

    def query_selector(self, sel):
        return None


class FakePage(object):
    def __init__(self, text="", frames=None, widgets=None, url="https://job.example.com/list"):
        self.frames = frames if frames is not None else [FakeFrame(text, widgets)]
        self.url = url
        self.mouse = FakeMouse()
        self.waited = 0

    def wait_for_timeout(self, ms):
        self.waited += ms

    def inner_text(self, sel="body"):
        return self.frames[0].inner_text()

    def query_selector_all(self, sel):
        return self.frames[0].query_selector_all(sel)

    def evaluate(self, *a, **k):
        return {}


def main():
    import filler
    import captcha

    # ---------------------------------------------------------------- A _code_sent
    print("\n=== A. _code_sent 识别中文/括号倒计时（验证码真的发出时不误判） ===")
    for txt, want in [
        ("60秒后重新获取", True),
        ("重新获取(59)", True),
        ("重新获取 60s", True),
        ("59s后重发", True),
        ("验证码已发送", True),
        ("验证码已发至 138****0000", True),
        ("欢迎投递本岗位", False),
        ("请拖动滑块完成验证", False),
    ]:
        got = filler._code_sent(FakePage(txt))
        check("_code_sent(%r) == %s" % (txt, want), got is want, "got=%s" % got)

    # ---------------------------------------------------------------- B 形态识别
    print("\n=== B. classify_verify_kind：只认控件自身文案 + slider_end + 默认 unknown ===")
    p_mix = FakePage("职位描述：本岗位涉及网格计算、旋转机械结构设计与点选标注")
    w_slide = {"el": FakeEl("请按住滑块，拖动到最右边")}
    k, ev = filler.classify_verify_kind(p_mix, widget=w_slide)
    check("控件自身文案优先：整页「网格/旋转」不得污染形态判定（旧版在此误判 grid/rotate）",
          k == "slider_end", "%s/%s" % (k, ev))

    w_plain = {"el": FakeEl("滑动以完成验证")}
    k2, _ = filler.classify_verify_kind(p_mix, widget=w_plain)
    check("控件文案无形态词 → unknown（交调用方按滑块默认尝试，不再一律转人工）",
          k2 == "unknown", k2)

    k3, _ = filler.classify_verify_kind(p_mix, widget=w_plain, widget_piece=object())
    check("控件内有独立拼图块 → slider（结构证据最硬）", k3 == "slider", k3)

    k4, _ = filler.classify_verify_kind(FakePage("旋转图片 或 拖动滑块"), widget_piece=None)
    check("无控件时退化为整页文案：旋转+滑块 → rotate（更具体优先）", k4 == "rotate", k4)
    k5, _ = filler.classify_verify_kind(FakePage("请依次点击图中文字"))
    check("点选网格文案 → grid", k5 == "grid", k5)

    # ---------------------------------------------------------------- C 弱证据词门禁
    print("\n=== C. detect_human_verification：弱证据词需可见控件背书 ===")
    p_no = FakePage("本岗位涉及网格计算与旋转机械设计")
    check("弱证据词但**无验证控件** → ''（否则普通岗位页被误判成有人机验证→直接不投递）",
          filler.detect_human_verification(p_no) == "", repr(filler.detect_human_verification(p_no)))

    p_yes = FakePage("本岗位涉及网格计算与旋转机械设计", widgets=[FakeEl(visible=True)])
    got = filler.detect_human_verification(p_yes)
    check("弱证据词 + **有可见控件** → 采信",
          got in filler.HUMAN_VERIFY_WEAK_HINTS, repr(got))

    check("强证据词无控件也采信",
          bool(filler.detect_human_verification(FakePage("请完成安全验证"))))

    check("只有可见控件、无任何文案 → '验证控件'",
          filler.detect_human_verification(FakePage("欢迎投递", widgets=[FakeEl(visible=True)]))
          == "验证控件")
    check("控件不可见 → 不算有人机验证",
          filler.detect_human_verification(FakePage("欢迎投递", widgets=[FakeEl(visible=False)]))
          == "")

    # ---------------------------------------------------------------- D 自动过验证
    print("\n=== D. _attempt_auto_verify：slider_end 不调 solver；unknown 默认滑块 ===")
    _calls = {"n": 0}
    _orig_solve = captcha.solve

    def _spy_solve(ctx):
        _calls["n"] += 1
        return {"type": "drag", "gap_x": 100, "via": "cv", "method": "spy"}

    captcha.solve = _spy_solve
    try:
        el = FakeEl("请按住滑块，拖动到最右边", box={"x": 0, "y": 0, "width": 300, "height": 40})
        pg = FakePage(frames=[FakeFrame("请按住滑块，拖动到最右边", [el])])
        notes = []
        _calls["n"] = 0
        ok = filler._attempt_auto_verify(pg, attempts=2, note=notes.append)
        check("slider_end：**不调用** CV/AI solver（这类图 CV 必报「未找到缺口轮廓」）",
              _calls["n"] == 0, str(_calls))
        check("slider_end：走内置「拖到最右端」",
              any("拖到最右端" in m for m in notes), " | ".join(notes)[:220])
        ev_types = [e[0] for e in pg.mouse.events]
        check("slider_end：真的按下并拖动了",
              "down" in ev_types and "up" in ev_types, str(ev_types[:8]))
        mx = max([e[1] for e in pg.mouse.events if e[0] == "move"] or [0])
        check("slider_end：拖到轨道最右端（≥290px）", mx >= 290, "max_x=%.1f" % mx)
        check("slider_end：无成功证据时**如实返回 False**（不谎报成功）", ok is False, str(ok))

        el2 = FakeEl("滑动以完成验证", box={"x": 0, "y": 0, "width": 300, "height": 40})
        pg2 = FakePage(frames=[FakeFrame("职位描述：网格计算与旋转设计", [el2])])
        notes2 = []
        _calls["n"] = 0
        filler._attempt_auto_verify(pg2, attempts=1, note=notes2.append)
        check("unknown → 默认按滑块流程尝试（不再因「读不到证据」而拒投）",
              _calls["n"] >= 1, str(_calls))
        check("并显式说明「无形态证据 → 默认按滑块」",
              any("无形态证据" in m for m in notes2), " | ".join(notes2)[:220])
    finally:
        captcha.solve = _orig_solve

    # ---------------------------------------------------------------- E AI 通道归一
    print("\n=== E. captcha._as_dict：AI 通道收到非 dict 不再整条崩掉 ===")
    ad = captcha._as_dict
    check("dict 原样返回", ad({"x": 7}) == {"x": 7})
    check("裸整数 → {'x': 数字}", ad(85) == {"x": 85.0}, repr(ad(85)))
    check("裸浮点 → {'x': 数字}", ad(85.5) == {"x": 85.5})
    check("bool → {}（True/False 是 int 子类，必须排除）", ad(True) == {} and ad(False) == {})
    check("点列表 → points", ad([[1, 2], [3, 4]]).get("points") == [[1, 2], [3, 4]])
    s = ad("缺口在 x=12, y=34 附近")
    check("字符串里两个数字 → points + x", s.get("x") == 12.0 and s.get("points") == [[12.0, 34.0]], repr(s))
    check("单数字字符串 → x", ad("240").get("x") == 240.0)
    check("无数字字符串 → {}", ad("不知道") == {})
    check("None → {}", ad(None) == {})

    fake = types.ModuleType("llm_client")
    fake.is_configured = lambda: True
    fake.vision = lambda ask, img: (85, "fake-model")      # **裸整数**（老日志的故障形态）
    _had = "llm_client" in sys.modules
    _old = sys.modules.get("llm_client")
    sys.modules["llm_client"] = fake
    try:
        res = captcha._solve_ai_vision({"bg_png": b"\x89PNG\r\n\x1a\n" + b"0" * 32,
                                        "width": 360, "height": 200, "kind": "slider"})
        check("AI 视觉通道收到裸整数不再抛 AttributeError（旧日志 'int' object has no attribute 'get'）",
              res.get("gap_x") == 85, str(res))
    except Exception as e:  # noqa: BLE001
        check("AI 视觉通道收到裸整数不再抛 AttributeError", False, "%s: %s" % (type(e).__name__, e))
    finally:
        if _had and _old is not None:
            sys.modules["llm_client"] = _old
        else:
            sys.modules.pop("llm_client", None)

    # ---------------------------------------------------------------- F 表单等待
    print("\n=== F. _wait_delivery_form：等页面切换，别看一眼就放弃 ===")

    def _mk_sig(n, labels=(), texts=(), files=0):
        return {"n": n, "labels": list(labels), "texts": list(texts), "fileInputs": files}

    class SeqPage(object):
        def __init__(self, sigs):
            self.sigs = list(sigs)
            self.i = 0
            self.waited = 0

        def evaluate(self, js, arg=None):
            if "labels = inputs.map" in js:
                s = self.sigs[min(self.i, len(self.sigs) - 1)]
                self.i += 1
                return s
            return None

        def wait_for_timeout(self, ms):
            self.waited += ms

    LIST = _mk_sig(0, [], ["换一批", "下一页"], 0)
    FORM = _mk_sig(6, ["请输入姓名", "请输入手机号", "请输入学校", "请输入专业",
                       "请输入邮箱", "请输入学历"], ["提交"], 0)

    sp = SeqPage([LIST, LIST, FORM])
    notes = []
    got = filler._wait_delivery_form(sp, notes.append, tries=5, gap=10, where="登录后")
    check("轮询到表单即返回（不再看一眼就放弃投递）",
          filler._looks_like_delivery_form(got) is True, str(got)[:120])
    check("并记录「页面切换完成」", any("页面切换完成" in m for m in notes), " | ".join(notes)[:200])

    sp2 = SeqPage([LIST])
    got2 = filler._wait_delivery_form(sp2, None, tries=3, gap=10, where="登录后")
    check("始终不是表单 → 逐次重试后返回最后一次结果（不抛异常）",
          filler._looks_like_delivery_form(got2) is False and sp2.i >= 2, "i=%d" % sp2.i)

    # ---------------------------------------------------------------- G 表单识别放宽
    print("\n=== G. _looks_like_delivery_form：放宽但不得误判筛选页 ===")
    BIG_EN = _mk_sig(10, ["Full Name", "Email", "Phone", "School", "Major",
                          "Degree", "GPA", "City", "Start Date", "Remark"], ["提交"], 0)
    check("①大表单但字段全是英文（hits=0）且有提交按钮 → 是投递表单（旧规则漏判→不投递）",
          filler._looks_like_delivery_form(BIG_EN) is True)
    BIG_FILTER = _mk_sig(10, ["关键词", "职位类别", "工作地点", "月薪范围", "经验要求",
                              "学历要求", "公司规模", "行业", "融资阶段", "排序"], ["搜索", "下一页"], 0)
    check("②筛选条件堆了 10 个输入框、无提交/上传 → **不是**投递表单（防误判）",
          filler._looks_like_delivery_form(BIG_FILTER) is False)
    check("③岗位列表页 → 不是投递表单", filler._looks_like_delivery_form(LIST) is False)
    check("④登录页（手机号+验证码）→ 不是投递表单",
          filler._looks_like_delivery_form(_mk_sig(2, ["请输入手机号", "请输入验证码"],
                                                   ["获取验证码", "登录"], 0)) is False)
    check("⑤身份字段表单 → 是投递表单", filler._looks_like_delivery_form(FORM) is True)
    check("⑥只有简历上传+提交 → 是投递表单",
          filler._looks_like_delivery_form(_mk_sig(2, ["上传简历", "附件简历"],
                                                   ["提交"], 1)) is True)
    check("⑦空 sig → 非表单", filler._looks_like_delivery_form({}) is False)

    # ---------------------------------------------------------------- H 结构断言
    print("\n=== H. 源码结构断言 ===")
    check("HUMAN_VERIFY_HINTS 已**不再**含旋转/点选类泛词（避免普通页误判）",
          "旋转" not in filler.HUMAN_VERIFY_HINTS and "点选" not in filler.HUMAN_VERIFY_HINTS)
    check("弱证据词表存在且含 旋转/网格/点选",
          all(w in filler.HUMAN_VERIFY_WEAK_HINTS for w in ("旋转", "网格", "点选")))
    check("_HV_SEL 选择器存在", isinstance(filler._HV_SEL, str) and "captcha" in filler._HV_SEL)
    check("_SLIDER_END_HINTS 存在且含「拖到最右」", "拖到最右" in filler._SLIDER_END_HINTS)
    check("_widget_text 存在（控件自身文案）", callable(getattr(filler, "_widget_text", None)))

    ausrc = inspect.getsource(filler._attempt_auto_verify)
    check("_attempt_auto_verify 用控件对象做形态识别", "classify_verify_kind(page, widget=w0)" in ausrc)
    check("unknown 默认按滑块（不再拒绝）", 'kind, ev = "slider", "无形态证据' in ausrc)
    check("只拒 rotate/grid", 'if kind in ("rotate", "grid")' in ausrc)
    check("有 slider_end 分支（拖到底）", 'if kind == "slider_end"' in ausrc)
    check("drag_end 结果分支存在", 'res.get("type") == "drag_end"' in ausrc)
    check("仍保留正向证据成功判定",
          "正向证据：验证码已发出" in ausrc and "复检两次均无验证控件" in ausrc)

    csrc = inspect.getsource(filler._apply_core)
    check("_apply_core 四处表单识别都改走 _wait_delivery_form"
          "（含 T66 新增的「人工登录后」那处）",
          csrc.count("_wait_delivery_form(") == 4, "count=%d" % csrc.count("_wait_delivery_form("))

    cap_src = open(os.path.join(HERE, "captcha.py"), "r", encoding="utf-8").read()
    check("captcha 两个 AI 分支都做了 _as_dict 归一",
          cap_src.count("data = _as_dict(data)") == 2,
          "count=%d" % cap_src.count("data = _as_dict(data)"))

    print("\n===== 汇总：PASS=%d FAIL=%d =====" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
