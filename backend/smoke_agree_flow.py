"""协议勾选 → 获取验证码 → 填码 → 登录 的**整链路**自测（真 Playwright，本地 mock 页）。

针对线上故障：协议没勾上 → 「获取验证码」按钮禁用 → 旧流程静默卡到超时。
本测试用两个 mock 页复现这两种情形，验证新流程：

  E  能勾上：按钮由禁用变可用，应走到「验证码已发出 ✓」→ 自动填码 → 已登录
     （且协议**只点一次**，不会点了又取消）
  F  勾不上（人为做成点击无效）：应**如实报告未勾选**、**不重复点击**、
     并且**快速返回**（needs_human=验证码未到），而不是无限等

用法：python smoke_agree_flow.py
"""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 隔离：**不接管调试端口上的 Edge** —— 本机若正开着 CDP 实例（9222），自测会把标签页
# 开到用户的真实浏览器里；自测一律走项目私有的持久化会话（headless）。
os.environ["RESUME_CDP_ENDPOINT"] = "http://127.0.0.1:59999"
# 隔离持久化浏览器会话目录：apply_by_url 会按公司建 Chrome profile，
# 不隔离就会在工程里留下 browser_profiles/ 垃圾（与隔离 RESUME_STATE 同理）。
os.environ.setdefault(
    "RESUME_PROFILES",
    os.path.join(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp",
                 "resume_profiles_smoke"))

import filler  # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print("   %-4s %s%s" % ("PASS" if cond else "FAIL", name,
                            ("  " + extra) if extra else ""))


def page_html(works):
    """works=True：点协议里的 i.ic 会勾上并把按钮解禁；False：点了没反应。"""
    toggle = """
        agreed = true;
        ic.classList.add('on');
        btn.disabled = false;
    """ if works else """
        /* 故意什么都不做，模拟勾不上 */
    """
    return """<html><body style="padding:24px;font-family:sans-serif">
  <div class="box" style="border:1px solid #ccc;padding:16px;width:360px">
    <div style="margin-bottom:10px">手机号登录</div>
    <input placeholder="手机号" style="padding:6px;width:100%%;margin-bottom:10px">
    <div class="agree-line" style="display:flex;align-items:center;gap:6px;margin-bottom:10px">
      <i class="ic" style="display:inline-block;width:16px;height:16px;border:1px solid #999"></i>
      <span>首次登录会自动创建新账号，且阅读并同意《隐私协议》</span>
    </div>
    <button class="sendbtn" disabled style="padding:6px 12px">获取验证码</button>
    <div id="cd" style="margin:8px 0"></div>
    <input placeholder="验证码" style="padding:6px;width:100%%;margin-bottom:10px">
    <button class="loginbtn" style="padding:6px 12px">登录</button>
  </div>
  <script>
    var ic  = document.querySelector('i.ic');
    var btn = document.querySelector('.sendbtn');
    var cd  = document.getElementById('cd');
    window.__icClicks = 0;
    window.__sent = 0;
    var agreed = false;
    ic.addEventListener('click', function () {
      window.__icClicks++;
      %s
    });
    btn.addEventListener('click', function () {
      if (btn.disabled) return;
      window.__sent++;
      cd.innerText = '60s后重发';
    });
    document.querySelector('.loginbtn').addEventListener('click', function () {
      window.__logged = true;
      // 登录成功后出现投递表单，让流程能继续走下去（真实站点是「回列表再点投递」）
      document.querySelector('.box').innerHTML =
        '<input placeholder="请输入姓名"><input placeholder="请输入手机号">' +
        '<input placeholder="请输入学校"><input placeholder="请输入专业">' +
        '<button class="subbtn">提交</button><div id="msg"></div>';
      document.querySelector('.subbtn').addEventListener('click', function () {
        document.getElementById('msg').innerText = '投递成功';
      });
    });
  </script>
</body></html>""" % toggle


def run_case(tag, works, tmp, code_wait=6, human_wait=2, code="8888"):
    p = os.path.join(tmp, "flow_%s.html" % tag)
    with open(p, "w", encoding="utf-8") as f:
        f.write(page_html(works))
    url = "file:///" + p.replace("\\", "/")
    logs = []
    t0 = time.time()
    r = filler.apply_by_url(url, title="", progress=lambda m: logs.append(m),
                            human_wait=human_wait, code_wait=code_wait,
                            headless=True, get_code=lambda: code)
    el = time.time() - t0
    print("\n   --- 用例 %s（%.1fs）---" % (tag, el))
    for m in logs:
        print("      · %s" % m)
    print("      => needs_human=%s stage=%s" % (r["needs_human"], r["stage"]))
    return r, logs, el


def main():
    tmp = tempfile.mkdtemp(prefix="agree-flow-")

    # ---------------- E：能勾上 ----------------
    r, logs, el = run_case("E", True, tmp)
    detail = r.get("detail", "")
    check("E 协议被识别为已勾选（不再卡住）", "协议已勾选 ✓" in detail)
    check("E 验证码按钮确实被点动、已发出", "验证码已发出 ✓" in detail)
    check("E 取到验证码并自动填入", "已自动填入验证码" in detail)
    check("E 登录后继续填表并投递成功", r["stage"] == "投递成功" and not r["needs_human"],
          "stage=%s" % r["stage"])
    check("E 确实开了自动填表流程", "已进入投递表单，开始自动填写投递信息" in detail)
    import json
    import urllib.parse
    check("E 没有出现「协议勾选未生效」告警", "协议勾选未生效" not in detail)

    # 通过 file:// 页面的 window 变量核对真实副作用
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
        try:
            pg = b.new_context().new_page()
            # 手动复现一遍同样的调用顺序，数一下协议被点了几次
            pg.goto("file:///" + os.path.join(tmp, "flow_E.html").replace("\\", "/"))
            pg.wait_for_timeout(200)
            for _ in range(3):
                filler._agree_terms(pg)
            check("E 连续调用 3 次协议只被点 1 次（幂等）",
                  pg.evaluate("() => window.__icClicks") == 1,
                  "icClicks=%s" % pg.evaluate("() => window.__icClicks"))
        finally:
            b.close()

    # ---------------- F：勾不上（线上的最坏情形）----------------
    # 协议勾不上 → 按钮禁用 → 短信发不出 → 后端也收不到码（get_code 返回空）
    r2, logs2, el2 = run_case("F", False, tmp, code_wait=4, human_wait=2, code="")
    d2 = r2.get("detail", "")
    check("F 如实报告「协议勾选未生效」", "协议勾选未生效" in d2)
    check("F 识别出按钮禁用并跳过无效点击",
          "仍处于禁用态" in d2 and "跳过点击" in d2)
    check("F 明确停下并交人工（不是一直等）", r2["needs_human"] is True,
          "stage=%s" % r2["stage"])
    check("F 原因是「验证码未到」（说清卡在哪）", r2["stage"] == "验证码未到",
          "stage=%s" % r2["stage"])
    check("F 快速返回（<45s，不会无限挂住）", el2 < 45, "%.1fs" % el2)

    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
        try:
            pg = b.new_context().new_page()
            pg.goto("file:///" + os.path.join(tmp, "flow_F.html").replace("\\", "/"))
            pg.wait_for_timeout(200)
            for _ in range(5):
                filler._agree_terms(pg)
            n = pg.evaluate("() => window.__icClicks")
            check("F 反复重试也只点 1 次（不会把勾点掉/狂点）", n == 1, "icClicks=%s" % n)
            check("F 未发出任何短信（发送次数=0）", pg.evaluate("() => window.__sent") == 0,
                  "sent=%s" % pg.evaluate("() => window.__sent"))
        finally:
            b.close()

    ok = sum(1 for _n, v in RESULTS if v)
    print("\n================ 汇总 ================")
    for n, v in RESULTS:
        if not v:
            print("  FAILED: %s" % n)
    print("  %d/%d PASS" % (ok, len(RESULTS)))
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
