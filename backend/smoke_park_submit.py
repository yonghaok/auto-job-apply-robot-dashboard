"""smoke_park_submit.py —— 锁定「人工提交后没有终态」修复。

背景：单岗位「停在提交前」时，_park_before_submit 以前只靠每 2s 轮询成功文案判定，
人工点提交后若成功提示一闪而过 / 措辞不在白名单 / 立刻关窗，就会收不到 success，
状态卡在「待人工提交」（非终态）。修复后注入「人工点击提交」监听器，捕获点击即视为
已提交，并据此可靠进入终态。

本测试用 FakePage 模拟浏览器页面（不真正开 Playwright），覆盖：
  A 成功文案直达 → success
  B 人工点击提交(无成功文案/无报错) → 宽限期内 success
  C 点击提交后关窗 → success（不再卡待人工提交）
  D 未点击就关窗 → closed（如实记待人工提交，正确非终态）
  E 表单报错(必填未填) → 不会提前判 success（等人工修正）
"""

import filler


class FakePage:
    def __init__(self, body="", flag=False, close_after=None, click_at=None):
        self.body = body
        self.flag = flag
        self.close_after = close_after   # 第几次 wait_for_timeout 后置 closed
        self.click_at = click_at         # 第几次 wait_for_timeout 后 flag 变 True（模拟点击）
        self.ticks = 0
        self.closed = False

    def evaluate(self, js, *a, **k):
        # 注入监听器时返回 body（被忽略）；读 flag 时返回 self.flag
        if "resumeHumanSubmit" in js and "===" in js:
            return self.flag
        if "innerText" in js:
            return self.body
        return None

    def is_closed(self):
        return self.closed

    def wait_for_timeout(self, ms):
        self.ticks += 1
        if self.click_at is not None and self.ticks >= self.click_at:
            self.flag = True
        if self.close_after is not None and self.ticks >= self.close_after:
            self.closed = True


def test_A_success_text():
    # 页面已显示成功文案 → 立即 success
    p = FakePage(body="恭喜，您的简历已成功投递！")
    st, ev = filler._park_before_submit(p)
    assert st == "success", "A: 期望 success，实际 %r" % (st,)
    print("A PASS  成功文案直达 ->", st, ev)


def test_B_human_clicked_no_text():
    # 人工点了提交，但页面迟迟不出成功文案/也不报错 → 宽限期后 success
    p = FakePage(flag=True)            # 监听器已捕获点击
    st, ev = filler._park_before_submit(p, confirm_wait=0)
    assert st == "success", "B: 期望 success，实际 %r" % (st,)
    print("B PASS  人工点击提交(无成功文案) ->", st, ev)


def test_C_closed_after_submit():
    # 人工点了提交，随后关窗 → success（不再卡待人工提交）
    p = FakePage(flag=True, close_after=2)
    st, ev = filler._park_before_submit(p)
    assert st == "success", "C: 期望 success，实际 %r" % (st,)
    print("C PASS  点击提交后关窗 ->", st)


def test_D_closed_without_submit():
    # 没点提交就关窗 → closed（如实记待人工提交，这是正确的非终态）
    p = FakePage(flag=False, close_after=1)
    st, ev = filler._park_before_submit(p)
    assert st == "closed", "D: 期望 closed，实际 %r" % (st,)
    print("D PASS  未提交就关窗 ->", st)


def test_E_invalid_blocks_premature_success():
    # 表单报必填错：即便见过点击也不应立即判 success，等人工修正
    p = FakePage(body="姓名不能为空，请填写必填项", flag=True, close_after=None)
    # 用 confirm_wait 设上限，避免无限轮询；到上限应返回 timeout 而非 success
    st, ev = filler._park_before_submit(p, confirm_wait=5)
    assert st != "success", "E: 报错情况下不应提前判 success，实际 %r" % (st,)
    print("E PASS  表单报错不提前判成功 ->", st)


if __name__ == "__main__":
    test_A_success_text()
    test_B_human_clicked_no_text()
    test_C_closed_after_submit()
    test_D_closed_without_submit()
    test_E_invalid_blocks_premature_success()
    print("\n全部 PASS：人工提交终态修复已锁定")
