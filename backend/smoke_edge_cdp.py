# -*- coding: utf-8 -*-
"""CDP 接管机制的离线自测（秒级，不需要真实 Edge 参与启动）。

覆盖三块：
  A) `edge_cdp` 纯逻辑：端口/端点解析、未监听时 endpoint_alive=False、
     **资料目录必须是「非默认」路径**（否则 Chromium 会静默忽略调试端口）、status 字段
  B) `filler` 源码级断言：快照注入已彻底移除；CDP 优先；**CDP 模式下必须新开标签页**
     （不劫持用户正在看的页面）；收尾只关标签页 + 断开，绝不关浏览器
  C) `server.py` 端点已切换（有 /api/edge-cdp/*，无 /api/edge-sync）
  D) 条件性在线断言：若本机此刻已有调试实例，则验证 `connect` 真能连上并能开页面

用法（cwd=backend）：<venv-python> smoke_edge_cdp.py
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


def main():
    import edge_cdp

    print("\n=== A. edge_cdp 纯逻辑 ===")
    saved = {k: os.environ.get(k) for k in
             ("RESUME_CDP_PORT", "RESUME_CDP_ENDPOINT", "RESUME_CDP_UDD", "RESUME_CDP_PROFILE")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        check("默认端口 = 9222", edge_cdp.port() == 9222, str(edge_cdp.port()))
        check("默认端点 = http://127.0.0.1:9222",
              edge_cdp.endpoint() == "http://127.0.0.1:9222", edge_cdp.endpoint())
        os.environ["RESUME_CDP_PORT"] = "9333"
        check("RESUME_CDP_PORT 生效", edge_cdp.port() == 9333, str(edge_cdp.port()))
        os.environ["RESUME_CDP_ENDPOINT"] = "http://127.0.0.1:9999/"
        check("RESUME_CDP_ENDPOINT 优先级最高且去掉尾斜杠",
              edge_cdp.endpoint() == "http://127.0.0.1:9999", edge_cdp.endpoint())
        check("未监听的端口 endpoint_alive=False（不误报）",
              edge_cdp.endpoint_alive(timeout=0.6) is False)
        check("未监听时 endpoint_version 返回 {}", edge_cdp.endpoint_version() == {})
        os.environ.pop("RESUME_CDP_PORT", None)
        os.environ.pop("RESUME_CDP_ENDPOINT", None)

        pd = edge_cdp.profile_dir()
        md = edge_cdp.main_edge_udd()
        info("profile_dir   = %s" % pd)
        info("main_edge_udd = %s" % md)
        check("CDP 资料目录在 backend/browser_profiles/_edge_cdp（非 Edge 默认目录）",
              os.path.basename(pd) == "_edge_cdp"
              and os.path.basename(os.path.dirname(pd)) == "browser_profiles", pd)
        check("本机能找到主 Edge 的 UDD（仅展示/诊断用）",
              bool(md) and os.path.isdir(md), md)
        check("**CDP 资料目录 != 主 Edge 默认目录**（否则调试端口会被静默忽略）",
              os.path.abspath(pd).lower() != os.path.abspath(md).lower(),
              "%s vs %s" % (pd, md))
        check("默认配置下 udd_is_default() = False", edge_cdp.udd_is_default() is False)
        os.environ["RESUME_CDP_UDD"] = md
        check("误配成默认目录时 udd_is_default() = True（可被 launch 拦下）",
              edge_cdp.udd_is_default() is True)
        os.environ.pop("RESUME_CDP_UDD", None)
        os.environ["RESUME_CDP_UDD"] = r"C:\tmp\_resume_cdp_probe"
        check("RESUME_CDP_UDD 可覆盖资料目录",
              edge_cdp.profile_dir() == r"C:\tmp\_resume_cdp_probe", edge_cdp.profile_dir())
        check("覆盖成非默认目录后 udd_is_default() = False",
              edge_cdp.udd_is_default() is False)
        os.environ.pop("RESUME_CDP_UDD", None)

        exe = edge_cdp.edge_bin()
        info("edge_bin = %s" % exe)
        check("能找到 msedge.exe", bool(exe) and os.path.isfile(exe))

        st = edge_cdp.status()
        for k in ("endpoint", "port", "cdp_ready", "browser", "edge_running", "edge_exe",
                  "profile_dir", "profile_dir_exists", "profile_dir_is_default",
                  "main_edge_udd", "profile", "headless"):
            check("status 含字段 %s" % k, k in st)
        check("status 不含旧快照字段", "seed_exists" not in st and "seed_cookies" not in st)
        check("status 不含易误用的 user_data_dir（已改名 main_edge_udd/profile_dir）",
              "user_data_dir" not in st)
        check("status.profile 默认 Default", st.get("profile") == "Default",
              str(st.get("profile")))
        check("status.profile_dir_is_default = False", st.get("profile_dir_is_default") is False)

        sig = inspect.signature(edge_cdp.kill_edge).parameters
        check("kill_edge 支持 wait（轮询确认，而非固定 sleep）", "wait" in sig and "log" in sig)
        check("kill_edge 不再直接返回 bool（改为 (ok, output) 便于诊断）",
              "ok" in edge_cdp.kill_edge.__doc__ and "output" in edge_cdp.kill_edge.__doc__)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print("\n=== B. filler 源码级：快照移除 + CDP 优先 + 不劫持用户标签页 ===")
    fsrc = read(os.path.join(HERE, "filler.py"))
    check("filler 已彻底不引用 edge_sync", "edge_sync" not in fsrc)
    check("filler 定义 _open_session", "def _open_session(" in fsrc)
    check("filler 定义 _make_cdp_closer", "def _make_cdp_closer(" in fsrc)
    check("_open_session 调用 edge_cdp.connect", "edge_cdp.connect(" in fsrc)
    check("_apply_core 用 _open_session 打开会话（不再直接 _launch_persistent）",
          "ctx, _via, _closer = _open_session(" in fsrc)
    check("存在 CDP 专属的「新开标签页」分支（不劫持用户正在看的页面）",
          'if _via == "cdp":' in fsrc and "page = ctx.new_page()" in fsrc)
    check("收尾区分 CDP：只关标签页 + 断开连接",
          "page.close()      # 只关本次投递标签页" in fsrc and "_closer()" in fsrc)
    check("CDP closer 只断开不杀浏览器",
          "对 CDP 连接 = 断开，不会杀掉 Edge 进程" in fsrc)
    check("回退路径 _launch_persistent 仍存在（CDP 不可用时不至于瘫痪）",
          "def _launch_persistent(" in fsrc)
    check("回退路径不再声称继承登录态",
          "它**不会**继承主 Edge 的登录态" in fsrc)

    print("\n=== C. server.py 端点已切换 ===")
    ssrc = read(os.path.join(HERE, "server.py"))
    check("有 GET /api/edge-cdp/status", '"/api/edge-cdp/status"' in ssrc)
    check("有 POST /api/edge-cdp/launch", '"/api/edge-cdp/launch"' in ssrc)
    check("已无 /api/edge-sync", "edge-sync" not in ssrc and "edge_sync" not in ssrc)
    check("server 引用 edge_cdp 模块", "import edge_cdp" in ssrc)

    print("\n=== D. 条件性在线断言（仅当本机已有调试实例时）===")
    if edge_cdp.endpoint_alive(timeout=1.0):
        info("检测到调试实例已就绪：%s" % edge_cdp.endpoint())
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                b, ctx, err = edge_cdp.connect(p)
                check("connect_over_cdp 成功", ctx is not None, err)
                if ctx is not None:
                    n = len(ctx.cookies())
                    info("接管后 ctx.cookies() = %d 条（专用 profile，登录后会增长）" % n)
                    pg = ctx.new_page()
                    pg.goto("data:text/html,<title>cdp-ok</title>ok", timeout=20000)
                    check("接管后可开页面并取标题", pg.title() == "cdp-ok", pg.title())
                    pg.close()
                    b.close()
        except Exception as e:  # noqa: BLE001
            check("在线 connect 未抛异常", False, "%s: %s" % (type(e).__name__, e))
    else:
        info("当前无调试实例，跳过（点前端「启动 Edge（继承登录态）」后再跑本段）")

    print("\n================ %d passed, %d failed ================" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
