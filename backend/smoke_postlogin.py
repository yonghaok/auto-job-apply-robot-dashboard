"""登录后「继续投递」这一环的纯函数自测（**不需要浏览器**，秒级返回）。

覆盖：
  1) _looks_like_delivery_form —— **登录页 / 岗位列表页不能被当成投递表单**（否则会误填）；
  2) _sig_desc                 —— 诊断行可读；
  3) _submit_evidence         —— 「列表页『已投递』角标」不算成功（旧版最容易误判的点）；
  4) _submit_application      —— notfound / success / dup / invalid 四条分支；
  5) apply_by_url / fill_page 的签名（auto_submit 开关存在，防止被后续改动删掉）。

用法：python smoke_postlogin.py
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 隔离：**不接管调试端口上的 Edge** —— 本机若正开着 CDP 实例（9222），自测会把标签页
# 开到用户的真实浏览器里；自测一律走项目私有的持久化会话（headless）。
os.environ["RESUME_CDP_ENDPOINT"] = "http://127.0.0.1:59999"

import filler  # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print("   %-4s %s%s" % ("PASS" if cond else "FAIL", name,
                            ("  " + extra) if extra else ""))


class FakeLocator:
    def count(self):
        return 0


class FakePage:
    """只实现 _form_signature / _click_exact / _submit_evidence 用到的那几个调用。"""

    def __init__(self, sig=None, body="", on_click=None):
        self.sig = sig or {}
        self.body = body
        self.on_click = on_click or {}
        self.clicked = []

    def wait_for_timeout(self, ms):
        pass

    def locator(self, sel):
        return FakeLocator()

    def evaluate(self, js, arg=None):
        if "document.body ? document.body.innerText" in js:
            return self.body
        if "innerText||'').trim() === kw" in js:      # _click_exact 的 JS 点击
            kw = arg
            if kw in self.on_click:
                self.clicked.append(kw)
                self.body = self.on_click[kw]
                return True
            return False
        if "labels = inputs.map" in js:                # _form_signature
            return self.sig
        return None


def sig(n=0, labels=(), texts=(), files=0):
    return {"n": n, "labels": list(labels), "texts": list(texts), "fileInputs": files}


print("== 1) 页面归类：_looks_like_delivery_form ==")
LIST_PAGE = sig(0, [], ["投递", "立即投递", "换一批", "下一页"], 0)
LOGIN_PAGE = sig(2, ["请输入手机号", "请输入验证码"], ["获取验证码", "登录"], 0)
FORM_PAGE = sig(4, ["请输入姓名", "请输入手机号", "请输入学校", "请输入专业"], ["提交"], 0)
RESUME_FORM = sig(2, ["上传简历", "附件简历"], ["提交", "提交申请"], 1)
MISC_PAGE = sig(6, ["搜索职位", "选择城市", "筛选"], ["搜索", "全部"], 0)

check("岗位列表页 → 不是投递表单", filler._looks_like_delivery_form(LIST_PAGE) is False)
check("登录页（手机号+验证码）→ 不是投递表单",
      filler._looks_like_delivery_form(LOGIN_PAGE) is False)
check("4 个身份字段的投递表单 → 是投递表单",
      filler._looks_like_delivery_form(FORM_PAGE) is True)
check("只有简历上传+提交的表单 → 是投递表单",
      filler._looks_like_delivery_form(RESUME_FORM) is True)
check("搜索/筛选控件堆 → 不是投递表单",
      filler._looks_like_delivery_form(MISC_PAGE) is False)
check("读取失败（空 sig）→ 保守判为非表单",
      filler._looks_like_delivery_form({}) is False)

print("\n== 2) _sig_desc 诊断行 ==")
d = filler._sig_desc(sig(3, [], ["提交"], 1))
check("含输入框/文件控件/提交按钮三个信息",
      "可见输入框 3" in d and "文件控件 1" in d and "提交类按钮 有" in d, d)
check("空 sig 也有兜底文案", filler._sig_desc({}) == "读取页面失败")

print("\n== 3) _submit_evidence：成功/重复/必填/无 ==")
for body, want, why in [
    ("恭喜您，投递成功！", "success", "强成功文案"),
    ("您已投递过该职位，请勿重复投递", "dup", "重复投递提示"),
    ("请填写姓名", "invalid", "必填校验"),
    ("职位列表 已投递 立即投递", "", "**列表页「已投递」角标不算成功**"),
    ("提交简历", "", "只有按钮文字不算结果"),
]:
    got, ev = filler._submit_evidence(FakePage(body=body))
    check("body=%-28r → %s（%s）" % (body[:26], want or "无结果", why), got == want,
          "got=%s ev=%s" % (got, ev))

print("\n== 4) _submit_application 四条分支 ==")
sub, ev = filler._submit_application(FakePage(body="填写信息", on_click={}))
check("没有可点提交按钮 → notfound", sub == "notfound" and ev == "", "state=%s" % sub)

fp = FakePage(body="填写信息", on_click={"提交": "投递成功"})
sub, ev = filler._submit_application(fp)
check("点「提交」后出现成功文案 → success（证据=页面文案）",
      sub == "success" and ev == "投递成功",
      "state=%s ev=%s clicked=%s" % (sub, ev, fp.clicked))

fp2 = FakePage(body="填写信息", on_click={"提交": "请填写学校"})
sub, ev = filler._submit_application(fp2)
check("点「提交」后被必填拦下 → invalid", sub == "invalid", "state=%s ev=%s" % (sub, ev))

fp3 = FakePage(body="填写信息", on_click={"提交": "您已投递过该职位"})
sub, ev = filler._submit_application(fp3)
check("点「提交」后提示已投递过 → dup", sub == "dup", "state=%s ev=%s" % (sub, ev))

fp4 = FakePage(body="填写信息", on_click={"提交": "提交中，请稍候"})
sub, ev = filler._submit_application(fp4)
check("点到了但拿不到结果文案 → clicked（不谎报成功）",
      sub == "clicked" and ev == "提交", "state=%s ev=%s" % (sub, ev))

print("\n== 5) 接口签名（防止被人手改回去）==")
ap = inspect.signature(filler.apply_by_url).parameters
check("apply_by_url 有 auto_submit 且默认 True",
      "auto_submit" in ap and ap["auto_submit"].default is True)
check("apply_by_url 仍保留 code_wait 默认 300",
      ap["code_wait"].default == 300, "code_wait=%s" % ap["code_wait"].default)
check("fill_page 接受 note（供进度上报）",
      "note" in inspect.signature(filler.fill_page).parameters)
check("_click_exact / _form_signature / _submit_evidence 都存在",
      all(hasattr(filler, n) for n in
          ("_click_exact", "_form_signature", "_submit_evidence",
           "_submit_application", "_accept_dialog")))

print("\n== 6) 停在提交前：_park_before_submit ==")


class ParkPage:
    """_park_before_submit 用的假页面：按轮次给出 body 文案（不真等）。"""

    def __init__(self, bodies, closed_after=None):
        self.bodies = list(bodies)
        self.i = 0
        self.polls = 0
        self.closed_after = closed_after      # 第 N 次轮询后视为「窗口被关掉」

    def is_closed(self):
        return self.closed_after is not None and self.polls > self.closed_after

    def wait_for_timeout(self, ms):
        self.polls += 1

    def evaluate(self, js, arg=None):
        if "document.body ? document.body.innerText" in js:
            b = self.bodies[min(self.i, len(self.bodies) - 1)] if self.bodies else ""
            self.i += 1
            return b
        return None


class RaisingParkPage(ParkPage):
    """模拟「关窗口后 wait_for_timeout 抛异常」这条路径（Playwright 实际就是这样）。"""

    def wait_for_timeout(self, ms):
        self.polls += 1
        if self.closed_after is not None and self.polls > self.closed_after:
            raise RuntimeError("Target page, context or browser has been closed")


st, ev = filler._park_before_submit(ParkPage(["恭喜您，投递成功！"]))
check("进 park 时已提交 → 立刻 success（不白等）", st == "success" and ev == "投递成功",
      "state=%s ev=%s" % (st, ev))

st, ev = filler._park_before_submit(ParkPage(["填写信息", "填写信息", "投递成功"]))
check("你点了提交后 → success", st == "success" and ev == "投递成功",
      "state=%s ev=%s" % (st, ev))

st, ev = filler._park_before_submit(ParkPage(["填写信息"], closed_after=0))
check("窗口被关掉 → closed（不谎报成功/失败）", st == "closed", "state=%s" % st)

st, ev = filler._park_before_submit(RaisingParkPage(["填写信息"], closed_after=0))
check("关窗口致 wait 抛异常 → closed", st == "closed", "state=%s" % st)

st, ev = filler._park_before_submit(ParkPage(["填写信息"]), confirm_wait=4)
check("confirm_wait=4 且一直没提交 → timeout", st == "timeout", "state=%s" % st)

notes = []
st, ev = filler._park_before_submit(ParkPage(["填写信息"]), note=notes.append, confirm_wait=22)
hb = [m for m in notes if "等你在浏览器窗口核对并点「提交」" in m]
check("驻留期间有心跳（不然看起来像卡死）", st == "timeout" and len(hb) == 1,
      "state=%s notes=%r" % (st, hb))
check("心跳写明已等待秒数 + 「不设超时」的语义",
      st == "timeout" and "已等待 20s" in hb[0], hb[0] if hb else "")

print("\n== 7) 「停在提交前」的开关默认值 ==")
check("pause_before_submit 默认 False（一键投递全自动）",
      ap["pause_before_submit"].default is False)
check("confirm_wait 默认 None（未指定 → 用有界长预算，不再无限等）",
      ap["confirm_wait"].default is None)
check("PAUSE_CONFIRM_WAIT 是正数（默认 1 小时有界长等待）",
      filler.PAUSE_CONFIRM_WAIT > 0, "=%s" % getattr(filler, "PAUSE_CONFIRM_WAIT", None))
check("_park_before_submit 存在且可调用", callable(getattr(filler, "_park_before_submit", None)))

print("\n== 8) 字段定位（防「只有 placeholder 的表单静默填错」）==")
check("_selector_for 优先用 data-filler-idx",
      filler._selector_for({"idx": 3, "id": "a", "nm": "b"}) == "[data-filler-idx='3']",
      filler._selector_for({"idx": 3, "id": "a", "nm": "b"}))
check("idx=0 也要用（0 是合法下标，不能当假值跳过）",
      filler._selector_for({"idx": 0}) == "[data-filler-idx='0']",
      filler._selector_for({"idx": 0}))
check("无 idx 时回退 id",
      filler._selector_for({"id": "name"}) == "#name")
check("无 idx/id 时回退 name",
      "name='phone'" in filler._selector_for({"nm": "phone"}))
check("兜底选择器仍在（最后手段）",
      filler._selector_for({}) == "input,textarea,select")

ok = sum(1 for _n, v in RESULTS if v)
print("\n================ 汇总 ================")
for n, v in RESULTS:
    if not v:
        print("  FAILED: %s" % n)
print("  %d/%d PASS" % (ok, len(RESULTS)))
sys.exit(0 if ok == len(RESULTS) else 1)
