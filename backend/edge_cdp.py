# -*- coding: utf-8 -*-
"""用**远程调试端口（CDP）**接管一个**独立 Edge 实例** —— 投递页因此长期保持登录态。

## 两条被堵死的路（都实测过，别再浪费时间去试）
1) **复制主 Edge 的资料快照**，再用 msedge 打开副本：
   Edge 154 的 cookie 全是 `v20`（App-Bound Encryption），副本里**一条都解不开**
   （`ctx.cookies()` 恒为 0），而且副本 `Cookies` 表被 Edge **从 2920 行清成 0 行**。
   四组对照（base / +`Default/Preferences` / +`Default/Network/*` / headed）全为 0，
   版本（真实 `Last Version` == `browser.version` == 154.0.4258.32）与安装路径也已排除。
   → `app_bound_encrypted_key` **只在原 user-data-dir 路径下可解**，副本必失败。
2) **用主 Edge 的「默认」资料目录 + 调试端口**：
   Chromium ≥136 的安全策略规定：`--remote-debugging-port` 与**默认**资料目录组合会被
   **静默忽略**（实测：Edge 能起来，但 9222 在 45s 内始终不就绪；同一个 exe 换成
   临时/非默认目录启动则端口正常就绪 —— 对照成立）。这是官方有意为之：
   防止第三方程序用 CDP 读走用户默认 profile 的 cookie。

## 所以本模块走第三条路：**独立 profile + 调试端口**
用一个**非默认**的资料目录（默认 `<backend>/browser_profiles/_edge_cdp`）启动 Edge，
带 `--remote-debugging-port`，投递时用 `connect_over_cdp` 接管它。
  · 与用户**日常 Edge 完全隔离**：不同 user-data-dir → **可同时运行、互不影响**；
  · 用户在**这个窗口里登录一次**目标站点，之后所有投递共享该登录态；
    **人工可介入**（验证码 / 滑块 / 二次确认都在同一个你可见的窗口里）；
  · 代价（必须如实说明）：这**不是**「自动继承主 Edge 的历史 cookie」（那已被官方禁止），
    而是「**一次登录、长期复用**」。

## 环境变量
  RESUME_CDP_ENDPOINT   直接指定端点（形如 http://127.0.0.1:9222），优先级最高
  RESUME_CDP_PORT       调试端口（默认 9222）
  RESUME_CDP_UDD        CDP 实例的资料目录（默认 `backend/browser_profiles/_edge_cdp`）
  RESUME_CDP_PROFILE    附加 `--profile-directory=`（默认 `Default`）
  RESUME_CDP_HEADLESS   "1" 时加 `--headless=new`（默认**不加**：人工接管要靠可见窗口）
  RESUME_EDGE_BIN       msedge.exe 路径（默认自动探测）
  RESUME_EDGE_UDD       主 Edge 的 User Data（**仅用于展示/诊断**，不参与启动）
"""
import json
import os
import subprocess
import time
import urllib.request

CREATE_NO_WINDOW = 0x08000000
DEFAULT_PORT = 9222


def port():
    try:
        return int((os.environ.get("RESUME_CDP_PORT") or DEFAULT_PORT))
    except Exception:  # noqa: BLE001
        return DEFAULT_PORT


def endpoint():
    ep = (os.environ.get("RESUME_CDP_ENDPOINT") or "").strip()
    if ep:
        return ep.rstrip("/")
    return "http://127.0.0.1:%d" % port()


def endpoint_alive(timeout=1.5):
    """调试端口是否可用（CDP 实例已就绪）。"""
    try:
        with urllib.request.urlopen(endpoint() + "/json/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def endpoint_version(timeout=3):
    """取 /json/version（含 Browser 版本）。失败返回 {}。"""
    try:
        with urllib.request.urlopen(endpoint() + "/json/version", timeout=timeout) as r:
            return json.load(r)
    except Exception:  # noqa: BLE001
        return {}


def edge_bin():
    """msedge.exe 路径（找不到返回 ""）。"""
    env = (os.environ.get("RESUME_EDGE_BIN") or "").strip()
    if env and os.path.isfile(env):
        return env
    pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
    pfx = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    for c in (os.path.join(pfx, "Microsoft", "Edge", "Application", "msedge.exe"),
              os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe")):
        if os.path.isfile(c):
            return c
    return ""


def main_edge_udd():
    """主 Edge 的 User Data（**仅供展示/诊断，不参与启动**）。

    它不能用来启动调试实例：默认资料目录会被 Chromium 的安全策略拒绝开启调试端口。
    """
    env = (os.environ.get("RESUME_EDGE_UDD") or "").strip()
    if env and os.path.isdir(env):
        return env
    la = os.environ.get("LOCALAPPDATA") or ""
    for c in (os.path.join(la, "Microsoft", "Edge", "User Data"),
              os.path.join(la, "Microsoft", "Edge Beta", "User Data")):
        if os.path.isdir(c):
            return c
    return ""


def profile_dir():
    """CDP 独立实例的资料目录（**必定是非默认路径**，否则调试端口会被忽略）。"""
    env = (os.environ.get("RESUME_CDP_UDD") or "").strip()
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "browser_profiles", "_edge_cdp")


def udd_is_default():
    """当前 CDP 资料目录是否**等于**主 Edge 默认目录（等于就一定会失败，需显式警告）。"""
    pd = os.path.abspath(profile_dir()).lower().rstrip("\\/")
    md = os.path.abspath(main_edge_udd()).lower().rstrip("\\/") if main_edge_udd() else ""
    return bool(md) and pd == md


def edge_running():
    """是否有 msedge.exe 在跑（注意：**包含**我们自己的 CDP 实例，以及用户的日常 Edge）。"""
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq msedge.exe", "/NH"],
                             capture_output=True, text=True, timeout=20,
                             creationflags=CREATE_NO_WINDOW)
        return "msedge.exe" in (out.stdout or "").lower()
    except Exception:  # noqa: BLE001
        return False


def kill_edge(wait=20, log=None):
    """强制结束所有 msedge.exe 进程，并**轮询确认真已退出**。

    返回 `(ok, output)`：ok=False 时 output 带 taskkill 原始输出，便于诊断。
    **不静默失败**：实测「后端进程内执行 taskkill」曾出现命令似乎成功、进程却仍在的情况，
    所以这里必须轮询确认 + 重试一次，而不是固定 sleep 后一次性判断。
    """
    def say(m):
        if log:
            try:
                log(m)
            except Exception:  # noqa: BLE001
                pass

    out = ""
    for attempt in (1, 2):
        try:
            r = subprocess.run(["taskkill", "/F", "/IM", "msedge.exe"],
                               capture_output=True, text=True, timeout=40,
                               creationflags=CREATE_NO_WINDOW)
            out += (r.stdout or "") + (r.stderr or "")
        except Exception as e:  # noqa: BLE001
            out += "%s: %s\n" % (type(e).__name__, e)
        t0 = time.time()
        while time.time() - t0 < max(3.0, wait / 2.0):
            if not edge_running():
                return True, out
            time.sleep(0.5)
        if attempt == 1:
            say("第一次结束 Edge 后仍有残留进程，重试一次…")
    return (not edge_running()), out


def launch(close_edge=False, log=None, wait_ready=45):
    """启动**带调试端口**的独立 Edge 实例（专用非默认资料目录）。

    返回 {"ok":bool, ...}：
      · 端口已就绪        → ok=True, already=True
      · 资料目录误配成默认 → ok=False + 明确原因（否则会静默等不到端口）
      · 启动成功          → ok=True，带 endpoint / browser 版本 / profile_dir / 等待秒数
    """
    def say(m):
        if log:
            try:
                log(m)
            except Exception:  # noqa: BLE001
                pass

    if endpoint_alive():
        v = endpoint_version()
        return {"ok": True, "already": True, "endpoint": endpoint(),
                "browser": v.get("Browser", ""), "profile_dir": profile_dir()}

    if udd_is_default():
        return {"ok": False, "error":
                "CDP 资料目录被配置成了 Edge 的**默认**目录（%s）。Chromium 会静默忽略调试端口，"
                "必须先改成非默认目录（或清掉 RESUME_CDP_UDD）。" % profile_dir()}

    exe = edge_bin()
    if not exe:
        return {"ok": False, "error": "找不到 msedge.exe（可用 RESUME_EDGE_BIN 指定）"}

    pd = profile_dir()
    try:
        os.makedirs(pd, exist_ok=True)
    except OSError as e:
        return {"ok": False, "error": "无法创建 CDP 资料目录 %s：%s" % (pd, e)}

    if close_edge and edge_running():
        say("按请求先结束现有 Edge 进程（会丢未保存的网页状态）…")
        ok, out = kill_edge(log=say)
        if not ok:
            return {"ok": False, "edge_running": True,
                    "error": "尝试结束 Edge 进程失败（重试后仍有残留）。",
                    "taskkill_output": out[-400:]}
        say("Edge 已退出，继续启动调试实例。")

    args = [exe,
            "--remote-debugging-port=%d" % port(),
            "--user-data-dir=%s" % pd,
            "--no-first-run", "--no-default-browser-check", "--start-maximized",
            "--disable-features=Translate"]
    prof = (os.environ.get("RESUME_CDP_PROFILE") or "Default").strip()
    if prof:
        args.append("--profile-directory=%s" % prof)
    if (os.environ.get("RESUME_CDP_HEADLESS") or "").strip() == "1":
        args.append("--headless=new")

    try:
        subprocess.Popen(args, creationflags=CREATE_NO_WINDOW, close_fds=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "启动 Edge 失败：%s: %s" % (type(e).__name__, e)}

    t0 = time.time()
    while time.time() - t0 < wait_ready:
        if endpoint_alive(timeout=1.0):
            v = endpoint_version()
            took = round(time.time() - t0, 1)
            say("调试实例已就绪（%s，用时 %ss）" % (v.get("Browser", "?"), took))
            return {"ok": True, "already": False, "endpoint": endpoint(),
                    "profile_dir": pd, "exe": exe,
                    "browser": v.get("Browser", ""), "waited": took}
        time.sleep(1)
    return {"ok": False, "endpoint": endpoint(), "profile_dir": pd,
            "error": "Edge 已启动，但 %ds 内调试端口未就绪。"
                     "常见原因：资料目录是默认目录（策略会静默忽略端口）、端口被占用、"
                     "或被安全软件拦截。可换端口重试（RESUME_CDP_PORT）。" % wait_ready}


def connect(pw, log=None):
    """连到调试实例。返回 `(browser, context, err)`；成功时 err 为 ""。

    context 取该实例的默认 context（就是你在那个窗口里登录后的那套 cookie）。
    """
    if not endpoint_alive():
        return None, None, "调试端口未就绪（%s）" % endpoint()
    try:
        b = pw.chromium.connect_over_cdp(endpoint())
    except Exception as e:  # noqa: BLE001
        return None, None, "connect_over_cdp 失败：%s: %s" % (type(e).__name__, e)
    try:
        ctx = b.contexts[0] if b.contexts else b.new_context()
    except Exception as e:  # noqa: BLE001
        return b, None, "取默认 context 失败：%s: %s" % (type(e).__name__, e)
    return b, ctx, ""


def status():
    """给 /api/edge-cdp/status 用的只读状态。"""
    v = endpoint_version() if endpoint_alive() else {}
    pd = profile_dir()
    md = main_edge_udd()
    return {
        "endpoint": endpoint(),
        "port": port(),
        "cdp_ready": bool(v),
        "browser": v.get("Browser", ""),
        "edge_running": edge_running(),
        "edge_exe": edge_bin(),
        "profile_dir": pd,
        "profile_dir_exists": os.path.isdir(pd),
        "profile_dir_is_default": udd_is_default(),
        "main_edge_udd": md,
        "profile": (os.environ.get("RESUME_CDP_PROFILE") or "Default").strip(),
        "headless": (os.environ.get("RESUME_CDP_HEADLESS") or "").strip() == "1",
    }
