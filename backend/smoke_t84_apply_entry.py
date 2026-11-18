# -*- coding: utf-8 -*-
"""T84 自测：投递入口不许点到导航/页脚链接 + 不收养列表页 + page 死了别假装保窗。

背景（用户 2026-10-28 反馈）：「继续下一轮页面不会自动填写并且自动关闭」。
真机（鹰角网络 / MokaHR）实况：岗位详情页顶部有「投递指南 / 2027届应届生投递 /
日常实习投递」导航，页脚还有一个「立即投递」链接。旧代码按文本顺序盲点：

    点「立即投递」→ 跳到 #/jobs 列表页
    → `_adopt_new_tab` 把列表页当新表单收养
    → `cur.close()` 把**真正的详情页**关掉
    → 在列表页上采集字段（命中 0 项）+ 后续 TargetClosedError

用**本地 HTTP 服务**保证「同站」（`_same_site` 要求同 host），否则测不到列表页拦截分支。
"""
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import filler  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

PASS = []
FAIL = []
PORT = 8747

# 模拟 MokaHR 详情页：顶部导航 + 页脚「立即投递」链接（都跳 #/jobs）+ 主体「申请职位」
PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>岗位详情</title></head>
<body>
<div id="nav"><a href="#/jobs">投递指南</a><a href="#/jobs">2027届应届生投递</a></div>
<div id="main"><h1>交互策划</h1>
  <button id="apply">申请职位</button>
</div>
<div id="foot"><a href="#/jobs">立即投递</a></div>
<script>
  document.getElementById('apply').addEventListener('click', function () {
    location.hash = '#/job/abc-123/apply';
    document.getElementById('main').innerHTML =
      '<form><input placeholder="姓名"><input placeholder="邮箱">' +
      '<input placeholder="年"><input placeholder="月">' +
      '<input placeholder="年"><input placeholder="月">' +
      '<textarea></textarea><button>提交</button></form>';
  });
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):                                  # noqa: N802
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if ok else "FAIL", name,
                         (" | " + str(detail)) if detail else ""))


def main():
    print("== A. URL 判定（纯函数） ==")
    check("列表页 #/jobs → True", filler._url_looks_list(
        "https://a.com/x?t=1#/jobs") is True)
    check("详情页 #/job/<uuid> → False", filler._url_looks_list(
        "https://a.com/x#/job/4f38f0a0-1b7b") is False)
    check("投递表单 #/job/<uuid>/apply → False", filler._url_looks_list(
        "https://a.com/x#/job/4f38f0a0/apply") is False)
    check("职位列表 /positions → True", filler._url_looks_list(
        "https://a.com/positions?page=1") is True)
    check("空串 → False（保守，不误判）", filler._url_looks_list("") is False)

    print("\n== B. 「点坏了」判定 ==")
    d = "https://a.com/x#/job/abc"
    j = "https://a.com/x#/jobs"
    check("详情 → 列表 = 点坏了", filler._entry_went_bad(d, j) is True)
    check("详情 → apply = 正常", filler._entry_went_bad(d, d + "/apply") is False)
    check("列表 → 详情 = 不是坏（方向相反）",
          filler._entry_went_bad(j, d) is False)
    check("原地没变 = 不判坏", filler._entry_went_bad(d, d) is False)

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d/detail" % PORT

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        try:
            print("\n== C. 先点到页脚链接，要回退并改点「申请职位」 ==")
            page = b.new_page()
            page.goto(base, wait_until="load")
            page.evaluate("() => { location.hash = '#/job/abc-123'; }")
            page.wait_for_timeout(250)
            start = page.url

            notes = []
            # 故意把「立即投递」排前面 —— 旧实现点完它就收工
            hit = filler._click_apply_entry(
                page, ["立即投递", "申请职位"], note=lambda m: notes.append(m))
            check("最终命中「申请职位」", hit == "申请职位", hit)
            check("最终页面是 apply", "/apply" in page.url, page.url[-42:])
            check("页面没退回列表", not filler._url_looks_list(page.url), page.url[-42:])
            check("回退后表单控件已渲染",
                  page.evaluate("() => document.querySelectorAll('input').length") >= 6)
            check("日志如实写了「命中导航/页脚链接」",
                  any("导航" in x or "页脚" in x for x in notes), notes[:1])
            check("点错后回到了出发页", "#/job/abc-123" in start, start[-30:])

            page2 = b.new_page()
            page2.goto(base, wait_until="load")
            page2.evaluate("() => { location.hash = '#/job/abc-123'; }")
            page2.wait_for_timeout(250)
            hit2 = filler._click_apply_entry(page2, ["立即投递"])
            check("只有导航链接时返回空（不谎报已点开投递）", hit2 == "", hit2)
            check("且不许停在列表页", not filler._url_looks_list(page2.url), page2.url[-30:])
            page2.close()

            print("\n== D. _adopt_new_tab 不许收养列表页、不许关当前页（同站） ==")
            ctx = b.new_context()
            filler.set_task_key("t84")
            notes2 = []
            cur = ctx.new_page()
            cur.goto(base, wait_until="load")
            cur.evaluate("() => { location.hash = '#/job/abc-123'; }")
            filler.claim_page(cur)

            before = list(ctx.pages)
            listpg = ctx.new_page()
            listpg.goto(base + "#/jobs", wait_until="load")
            listpg.wait_for_timeout(250)
            check("前提：两个页面同站",
                  filler._same_site(listpg.url, cur.url) is True,
                  (listpg.url, cur.url))

            got = filler._adopt_new_tab(ctx, cur, before,
                                        lambda m: notes2.append(m), "测试")
            check("返回的是当前详情页（没被换走）", got is cur)
            check("当前详情页仍然活着", not cur.is_closed())
            check("列表页也没被关（只是不收养）", not listpg.is_closed())
            check("日志写明是列表页/不是投递表单",
                  any("列表" in x or "投递表单" in x for x in notes2), notes2[-2:])
            ctx.close()

            print("\n== E. page 已关闭时不许假装「保持窗口」 ==")
            pg = b.new_page()
            check("活页 _page_alive=True", filler._page_alive(pg) is True)
            pg.close()
            check("关掉的页 _page_alive=False", filler._page_alive(pg) is False)
            check("None 也安全返回 False", filler._page_alive(None) is False)

            print("\n== F. 「取消投递」必须能打断停表等待 ==")
            pg2 = b.new_page()
            pg2.set_content("<html><body><h1>表单</h1><button>提交</button></body></html>")
            t0 = time.time()
            st, _ev = filler._park_before_submit(
                pg2, confirm_wait=0, is_cancelled=lambda: True)
            cost = time.time() - t0
            check("is_cancelled=True → 立刻返回 cancelled", st == "cancelled", st)
            check("且不干等（<5s）", cost < 5, round(cost, 2))

            pg3 = b.new_page()
            pg3.set_content("<html><body><h1>表单</h1><button>提交</button></body></html>")
            t1 = time.time()
            st2, _ = filler._park_before_submit(
                pg3, confirm_wait=2, is_cancelled=lambda: False)
            cost2 = time.time() - t1
            check("未取消 + confirm_wait=2 → timeout", st2 == "timeout", st2)
            check("耗时约 2s（没被取消逻辑干扰）", 1.5 < cost2 < 6, round(cost2, 2))
            pg2.close()
            pg3.close()
        finally:
            b.close()
    srv.shutdown()

    print("\n== G. 源码护栏 ==")
    f = filler.__file__
    src = open(f, encoding="utf-8", newline="").read()
    check("投递入口走 _click_apply_entry（不再是裸 _click_by_text）",
          "_click_apply_entry(page, [" in src)
    check("「申请职位」排在裸「立即投递」之前",
          src.index('"申请职位", "投递简历"') < src.index('"立即投递", "投递"'))
    check("收养前有列表页拦截",
          "_url_looks_list(burl) and not _url_looks_list(cur_url)" in src)
    check("异常分支先判页面存活", "if _page_alive(page):" in src)
    check("上传后等解析停稳已去掉北森/飞书限定",
          "if _phoenix_active(page) or _ud_like(page):\n                _wait_parse_settle(page)"
          not in src)
    check("停表等待接入了取消信号",
          "is_cancelled=is_cancelled)" in src
          and 'if is_cancelled and is_cancelled():' in src)

    print("\n===== 汇总：PASS=%d FAIL=%d =====" % (len(PASS), len(FAIL)))
    if FAIL:
        for x in FAIL:
            print("  FAILED:", x)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
