"""可插拔验证码 solver（断点2）。

模式（环境变量 CAPTCHA_SOLVER）：
  auto（默认）  : 依次尝试 cv → ai_vision，任一成功即返回
  cv            : 只用 OpenCV 解滑块拼图（边缘缺口法 / 拼图块模板匹配）
  ai_vision     : 截屏送多模态大模型（SenseNova，OpenAI 兼容 chat/completions）
  human         : 直接交人工（不尝试自动）

**打码平台已按用户要求停用**（不再出现在 auto 链路里，也不再读取 CAPTCHA_API_KEY）。

对外只有一个入口：
  solve(ctx) -> {"type": "drag", "gap_x": int, "via": "cv|ai_vision", ...} 或
                {"type": "click", "points": [[x, y], ...], "via": "ai_vision"}

**gap_x 的统一口径（重要）**：一律是「缺口**中心点**」在**背景图内**的像素 x。
2026-09-23 修复：模板匹配分支原本返回的是匹配区域**左上角**，而边缘法返回的是**中心**，
两者混用会让其中一路稳定偏半个缺口宽（约 25~40px）→ 滑块怎么都拖不到位。
现在两个分支都换算成中心再返回，调用方（filler）只需按同一口径换算位移。
  自动全失败则抛 NeedHuman（调用方据此暂停并交接人工）。

ctx 允许包含（按可用性传入）：
  bg_png: bytes        背景图（滑块拼图底图 / 图形验证码）
  piece_png: bytes     拼图块图（可含 alpha 通道；没有则用边缘法）
  kind: str            "slider" | "grid" | "unknown"
  width/height: int    图尺寸（PNG 头解析失败时的兜底）
"""
import base64
import json
import os

MODE = os.environ.get("CAPTCHA_SOLVER", "auto")

# 缺口 x 相对图宽的合理区间（缺口不会贴在最左/最右）
_GAP_MIN_RATIO = 0.05
_GAP_MAX_RATIO = 0.95


class NeedHuman(Exception):
    """验证码需要人工处理时抛出，调用方据此暂停并交接人工。"""


class Unavailable(Exception):
    """该通道不可用（依赖缺失 / 未配置 / 识别越界），可继续尝试下一个通道。"""


# ---------------------------------------------------------------- 工具
def _png_size(raw):
    """极简 PNG 尺寸解析（读 IHDR），免 PIL。返回 (w, h) 或 None。"""
    if not raw or len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    import struct
    try:
        return struct.unpack(">II", raw[16:24])
    except Exception:
        return None


# ---------------------------------------------------------------- cv2 加载
_CV2 = None
_CV2_TRIED = False


def _cv2_candidate_paths():
    """本 venv 的 cv2 是坏的 namespace 包（只有 misc/，无 __init__.py/cv2.pyd），
    因此把「系统 CPython 的用户 site-packages」候选项加进来兜底（同为 3.13，abi3 可用）。"""
    import sys
    out = []
    v = "Python%d%d" % sys.version_info[:2]
    for env, parts in (
        ("APPDATA", ("Python", v, "site-packages")),
        ("LOCALAPPDATA", ("Programs", "Python", v, "Lib", "site-packages")),
        ("LOCALAPPDATA", ("Programs", "Python", v, "site-packages")),
    ):
        base = os.environ.get(env)
        if base:
            out.append(os.path.join(base, *parts))
    for base in (r"C:\Python313", r"C:\Program Files\Python313"):
        out.append(os.path.join(base, "Lib", "site-packages"))
    return out


def _load_cv2():
    """返回可用的 cv2；不可用返回 None（不抛异常，交给下一个通道）。"""
    global _CV2, _CV2_TRIED
    if _CV2_TRIED:
        return _CV2
    _CV2_TRIED = True

    def _usable(mod):
        return mod is not None and hasattr(mod, "getStructuringElement") \
            and hasattr(mod, "matchTemplate")

    try:
        import cv2
        if _usable(cv2):
            _CV2 = cv2
            return _CV2
    except Exception:
        pass

    import sys
    for name in [n for n in list(sys.modules) if n == "cv2" or n.startswith("cv2.")]:
        sys.modules.pop(name, None)
    for p in _cv2_candidate_paths():
        if not os.path.isdir(p) or p in sys.path:
            continue
        sys.path.insert(0, p)
        try:
            import cv2
        except Exception:
            continue
        if _usable(cv2):
            _CV2 = cv2
            return _CV2
    _CV2 = None
    return None


def cv_available():
    return _load_cv2() is not None


def _decode(cv2, raw, flag=None):
    import numpy as np
    arr = np.frombuffer(raw, dtype=np.uint8)
    flag = cv2.IMREAD_UNCHANGED if flag is None else flag
    return cv2.imdecode(arr, flag)


# ---------------------------------------------------------------- 通道1：CV
def _solve_slider_cv(ctx):
    """滑块拼图：优先用拼图块做带 mask 的模板匹配，失败退回 Canny 缺口法。返回 gap_x（中心）。"""
    cv2 = _load_cv2()
    if cv2 is None:
        raise Unavailable("cv2 不可用")
    import numpy as np

    bg_raw = ctx.get("bg_png")
    if not bg_raw:
        raise Unavailable("缺少背景图")
    bg = _decode(cv2, bg_raw, cv2.IMREAD_COLOR)
    if bg is None:
        raise Unavailable("背景图解码失败")
    h, w = bg.shape[:2]
    bg_gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)

    lo, hi = int(w * _GAP_MIN_RATIO), int(w * _GAP_MAX_RATIO)

    # A) 有拼图块 → 带 alpha mask 的归一化互相关匹配（多尺度）
    piece_raw = ctx.get("piece_png")
    if piece_raw:
        piece = _decode(cv2, piece_raw)
        if piece is not None and piece.size:
            if piece.ndim == 3 and piece.shape[2] == 4:
                pmask = piece[:, :, 3]
                pimg = piece[:, :, :3]
            else:
                pimg = piece[:, :, :3] if piece.ndim == 3 else cv2.cvtColor(piece, cv2.COLOR_GRAY2BGR)
                pmask = np.full(pimg.shape[:2], 255, dtype=np.uint8)
            best = None
            for scale in (1.0, 0.9, 1.1, 0.8, 1.2, 0.7, 1.3):
                if abs(scale - 1.0) > 1e-6:
                    p2 = cv2.resize(pimg, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                    m2 = cv2.resize(pmask, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
                else:
                    p2, m2 = pimg, pmask
                if p2.shape[0] >= bg.shape[0] or p2.shape[1] >= bg.shape[1]:
                    continue
                if m2.shape != p2.shape[:2]:
                    continue
                try:
                    res = cv2.matchTemplate(bg, p2, cv2.TM_CCORR_NORMED, mask=m2)
                except Exception:
                    continue
                _mn, mx, _ml, ml = cv2.minMaxLoc(res)
                if best is None or mx > best[1]:
                    # 一并记下该尺度下**拼图块宽度**：matchTemplate 给的是左上角，
                    # 本模块对外统一用「缺口中心」，必须在这里换算（见文件头 gap_x 说明）。
                    best = (int(ml[0]), float(mx), int(p2.shape[1]))
            if best and best[1] >= 0.90:
                center = best[0] + best[2] // 2
                if lo <= center <= hi:
                    return {"gap_x": center, "score": round(best[1], 3),
                            "method": "template", "box_left": best[0], "piece_w": best[2]}

    # B) 边缘缺口法：Canny → 膨胀 → 找「近方形」缺口轮廓
    edges = cv2.Canny(bg_gray, 90, 200)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k, iterations=1)
    edges = cv2.dilate(edges, k, iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    cands = []
    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        if bw < w * 0.03 or bh < h * 0.15:
            continue
        if bh < bw * 0.5 or bh > h * 1.05:
            continue
        if not (lo <= x <= hi):
            continue
        cands.append((x, bw, bh, cv2.contourArea(c)))
    if not cands:
        raise Unavailable("未找到缺口轮廓")
    cands.sort(key=lambda t: (t[0], t[3]), reverse=True)
    x, bw, _bh, _area = cands[0]
    return {"gap_x": int(x + bw / 2), "score": None, "method": "edge"}


# ---------------------------------------------------------------- 通道2：AI 视觉
def _as_dict(data):
    """把模型返回的**非 dict** 结果归一成 dict。

    实测故障：`llm_client.vision()` 有时回一个裸数字/裸字符串，调用方直接
    `data.get(...)` → `AttributeError: 'int' object has no attribute 'get'`，
    整个 AI 通道就**整条废掉**（老日志里就是这个错），滑块在 CV 失败后没有任何退路。
    """
    if isinstance(data, dict):
        return data
    if isinstance(data, bool):
        return {}
    if isinstance(data, (int, float)):
        return {"x": float(data)}
    if isinstance(data, (list, tuple)):
        return {"points": [list(p) for p in data if isinstance(p, (list, tuple))]}
    if isinstance(data, str):
        import re as _re
        nums = _re.findall(r"-?\d+\.?\d*", data)
        if len(nums) >= 2:
            return {"points": [[float(nums[0]), float(nums[1])]], "x": float(nums[0])}
        if len(nums) == 1:
            return {"x": float(nums[0])}
    return {}


def _solve_ai_vision(ctx):
    """截屏送多模态大模型（SenseNova，模型无响应自动降级）。

    实测（真值 240px / 图宽 360px）：
      kimi-k3 精确；glm-5.2 偏差大；flash-lite 不可用 → 故默认链 kimi-k3 优先。
    提示词里**必须带上图片真实尺寸并指明「中心点」**，否则偏差可达数百像素。
    """
    try:
        import llm_client
    except Exception as e:
        raise Unavailable("llm_client 不可用: %s" % e)
    if not llm_client.is_configured():
        raise Unavailable("LLM 未配置（缺少环境变量 snova / RESUME_LLM_API_KEY）")
    img = ctx.get("bg_png")
    if not img:
        raise Unavailable("缺少图片")
    kind = ctx.get("kind") or "slider"
    sz = _png_size(img)
    if not sz or not sz[0]:
        w0, h0 = ctx.get("width"), ctx.get("height")
        sz = (int(w0), int(h0)) if w0 and h0 else None

    if kind == "slider":
        if sz:
            ask = ("这是一张滑块拼图验证码的**背景图**（宽 %d 像素，高 %d 像素）。"
                   "图中偏右位置有一个深色缺口方块。请给出该缺口方块**中心点**的横坐标 x"
                   "（以图片像素为单位，取 0~%d 之间的整数）。只输出 JSON：{\"x\": 数字}"
                   % (sz[0], sz[1], sz[0]))
        else:
            ask = ("这是一张滑块拼图验证码的背景图。请给出深色缺口方块**中心点**的横坐标 x"
                   "（图片像素，整数，从图片左边缘起算）。只输出 JSON：{\"x\": 数字}")
        try:
            data, used = llm_client.vision(ask, img)
        except Exception as e:  # noqa: BLE001
            raise Unavailable("AI 视觉调用失败: %s" % e)
        data = _as_dict(data)
        gx = data.get("x", data.get("gap_x"))
        if isinstance(gx, str) and gx.strip().isdigit():
            gx = int(gx.strip())
        if not isinstance(gx, (int, float)):
            raise Unavailable("AI 未返回可用坐标: %s" % str(data)[:120])
        gx = int(round(float(gx)))
        if sz and not (0 <= gx <= sz[0]):
            raise Unavailable("AI 返回越界 x=%d（图宽 %s），丢弃" % (gx, sz[0]))
        return {"gap_x": gx, "score": None, "method": "ai_vision:%s" % used}

    ask = ("这是一张图形验证码。请给出需要依次点选的字符/图案中心坐标（图片像素，"
           "从图片左上角起算）。只输出 JSON：{\"points\": [[x, y], ...]}")
    try:
        data, used = llm_client.vision(ask, img)
    except Exception as e:  # noqa: BLE001
        raise Unavailable("AI 视觉调用失败: %s" % e)
    data = _as_dict(data)
    pts = [[int(p[0]), int(p[1])] for p in (data.get("points") or [])
           if isinstance(p, (list, tuple)) and len(p) >= 2]
    if not pts:
        raise Unavailable("AI 未返回 points")
    return {"points": pts, "method": "ai_vision:%s" % used}


# 打码平台已按用户要求停用（保留说明，避免后人误接）
def _solve_platform(ctx):
    raise Unavailable("打码平台已停用（用户要求不使用）")


_CHANNELS = {
    "cv": _solve_slider_cv,
    "ai_vision": _solve_ai_vision,
    # 注意：auto 链路**不含** captcha_platform
}


def solve(ctx):
    """按 CAPTCHA_SOLVER 尝试自动过验证。全部失败抛 NeedHuman。

    返回 {"type":"drag","gap_x":int,"via":str,...} 或 {"type":"click","points":[[x,y]],"via":str}
    """
    ctx = ctx or {}
    kind = ctx.get("kind") or "slider"
    mode = (MODE or "auto").strip().lower()
    if mode == "captcha_platform":
        raise NeedHuman("打码平台已停用（用户要求不使用），请用 cv / ai_vision / auto")
    if mode == "human":
        raise NeedHuman("CAPTCHA_SOLVER=human：直接交人工")

    order = ["cv", "ai_vision"] if mode == "auto" else [mode]
    errs = []
    for name in order:
        fn = _CHANNELS.get(name)
        if fn is None:
            errs.append("%s: 未知通道" % name)
            continue
        try:
            r = fn(ctx)
        except Unavailable as e:
            errs.append("%s: %s" % (name, e))
            continue
        except NeedHuman:
            raise
        except Exception as e:  # noqa: BLE001
            errs.append("%s: %s" % (name, e))
            continue
        out = {"via": name, "type": "drag" if kind == "slider" else "click", "kind": kind}
        out.update(r)
        return out

    raise NeedHuman("自动过验证失败 → 交人工。尝试记录：" + "；".join(errs))


def mode_info():
    """给 /api/captcha/status 用的自检信息。"""
    cv2 = _load_cv2()
    info = {
        "mode": MODE,
        "cv_available": cv2 is not None,
        "cv_version": getattr(cv2, "__version__", None) if cv2 else None,
        "platform_disabled": True,
        "channels": ["cv", "ai_vision"],
    }
    try:
        import llm_client
        info["llm"] = llm_client.provider_info()
        info["ai_configured"] = bool(info["llm"].get("configured"))
    except Exception as e:  # noqa: BLE001
        info["llm"] = {"error": str(e)}
        info["ai_configured"] = False
    return info
