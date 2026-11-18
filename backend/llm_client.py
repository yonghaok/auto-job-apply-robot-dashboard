"""LLM 客户端（断点2 验证码 AI 视觉 / 通用 OpenAI 兼容调用）。

供应商（默认 SenseNova，用户提供）：
  base_url : https://token.sensenova.cn/v1
  api_key  : 用户环境变量 `snova`（本进程未继承时会回读 Windows 注册表 HKCU\\Environment）
  协议     : OpenAI 兼容 chat/completions
  模型链   : kimi-k3 → glm-5.2 → sensenova-6.8-flash-lite（**无响应/报错时自动降级**）

实测结论（2026-09-21，滑块缺口定位，真值 240px / 图宽 360px）：
  kimi-k3                -> {"x":240}  **精确**（注意：该模型只接受 temperature=1）
  glm-5.2                -> {"x":150}  不准（且常无响应超时）
  sensenova-6.8-flash-lite-> 偏差 -45/-10/+503，不可用
因此默认顺序即「kimi-k3 优先，失败降级」；并把「上次成功的模型」置顶以少走慢路。

配置优先级：环境变量 > llm_config.json > 内置默认（SenseNova）。
  环境变量：RESUME_LLM_BASE_URL / RESUME_LLM_API_KEY / RESUME_LLM_MODELS（逗号分隔）
  或直接提供 snova / SNOVA_API_KEY
"""

import json
import os
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_BASE_URL = "https://token.sensenova.cn/v1"
DEFAULT_MODELS = ["kimi-k3", "glm-5.2", "sensenova-6.8-flash-lite"]

# 上次调用成功的模型（本进程内记忆，优先复用，避免每次重走慢模型）
_LAST_GOOD = None
_KEY_CACHE = None
_KEY_SOURCE = ""

# 单个模型的最长等待（秒）。glm-5.2 实测会挂住，用短超时快速降级。
DEFAULT_MODEL_TIMEOUT = 45


# ------------------------------------------------------------------ 配置
def _win_env(name):
    """本进程没继承到用户环境变量时，直接读注册表（用户改环境变量后无需重启父进程）。"""
    try:
        import winreg
    except Exception:
        return ""
    for root, path in (
        (winreg.HKEY_CURRENT_USER, "Environment"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ):
        try:
            with winreg.OpenKey(root, path) as k:
                v, _t = winreg.QueryValueEx(k, name)
                if v:
                    return str(v).strip()
        except OSError:
            continue
    return ""


def _file_cfg():
    p = os.path.join(_HERE, "llm_config.json")
    if os.path.exists(p):
        try:
            return json.load(open(p, "r", encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _api_key():
    global _KEY_CACHE, _KEY_SOURCE
    if _KEY_CACHE is not None:
        return _KEY_CACHE
    for name in ("RESUME_LLM_API_KEY", "snova", "SNOVA_API_KEY"):
        v = os.environ.get(name)
        if v and v.strip():
            _KEY_CACHE, _KEY_SOURCE = v.strip(), "env:%s" % name
            return _KEY_CACHE
    fk = (_file_cfg().get("api_key") or "").strip()
    if fk:
        _KEY_CACHE, _KEY_SOURCE = fk, "llm_config.json"
        return _KEY_CACHE
    for name in ("snova", "SNOVA_API_KEY"):
        v = _win_env(name)
        if v:
            _KEY_CACHE, _KEY_SOURCE = v, "registry:%s" % name
            return _KEY_CACHE
    _KEY_CACHE, _KEY_SOURCE = "", ""
    return ""


def _base_url():
    for name in ("RESUME_LLM_BASE_URL", "SNOVA_BASE_URL"):
        v = os.environ.get(name)
        if v and v.strip():
            return v.strip()
    b = (_file_cfg().get("base_url") or "").strip()
    return b or DEFAULT_BASE_URL


def _models():
    v = os.environ.get("RESUME_LLM_MODELS")
    if v:
        got = [m.strip() for m in v.split(",") if m.strip()]
        if got:
            return got
    fm = _file_cfg().get("models")
    if isinstance(fm, list) and fm:
        return [str(m).strip() for m in fm if str(m).strip()]
    return list(DEFAULT_MODELS)


def is_configured():
    return bool(_api_key() and _base_url())


def provider_info():
    key = _api_key()
    return {
        "configured": bool(key and _base_url()),
        "provider": "sensenova" if "sensenova" in _base_url() else "openai-compatible",
        "base_url": _base_url(),
        "models": _models(),
        "last_good": _LAST_GOOD,
        "key_source": _KEY_SOURCE,
        "key_hint": (key[:6] + "***") if key else "",
        "platform_disabled": True,
    }


# ------------------------------------------------------------------ 调用
class LLMError(RuntimeError):
    pass


def _post(body, timeout):
    key = _api_key()
    if not key:
        raise LLMError("未找到 API Key（环境变量 snova / RESUME_LLM_API_KEY / llm_config.json）")
    url = _base_url().rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer %s" % key},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _call_model(model, messages, timeout, temperature=None, stream=False):
    body = {"model": model, "messages": messages, "stream": stream}
    if temperature is not None:
        body["temperature"] = temperature
    try:
        data = _post(body, timeout)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        # 有的模型（如 kimi-k3）只接受 temperature=1 → 去掉再试一次
        if e.code == 400 and "temperature" in detail.lower():
            body.pop("temperature", None)
            if temperature != 1:
                body["temperature"] = 1
                data = _post(body, timeout)
            else:
                raise LLMError("HTTP 400: %s" % detail)
        else:
            raise LLMError("HTTP %d: %s" % (e.code, detail))
    except Exception as e:  # noqa: BLE001
        raise LLMError("%s: %s" % (type(e).__name__, e))
    try:
        return data["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001
        raise LLMError("响应结构异常: %s | %s" % (e, str(data)[:200]))


def chat(messages, model=None, temperature=None, timeout=None, stream=False):
    """按模型链调用，**任一模型无响应/报错就降级到下一个**；全部失败抛 LLMError。

    返回 (text, used_model)。
    """
    if not is_configured():
        raise LLMError("LLM 未配置：请在环境变量 snova 或 llm_config.json 里提供 api_key")
    chain = [model] if model else _models()
    if not model and _LAST_GOOD and _LAST_GOOD in chain:
        chain = [_LAST_GOOD] + [m for m in chain if m != _LAST_GOOD]
    per = timeout or DEFAULT_MODEL_TIMEOUT
    errs = []
    for m in chain:
        try:
            txt = _call_model(m, messages, per, temperature=temperature, stream=stream)
            if txt and txt.strip():
                globals()["_LAST_GOOD"] = m
                return txt, m
            errs.append("%s: 空响应" % m)
        except LLMError as e:
            errs.append("%s: %s" % (m, str(e)[:160]))
        except Exception as e:  # noqa: BLE001
            errs.append("%s: %s" % (m, str(e)[:160]))
    raise LLMError("所有模型均失败 → " + "；".join(errs))


def _extract_json(text):
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s[:4].lower() == "json":
            s = s[4:]
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end != -1:
        s = s[start:end + 1]
    return json.loads(s)


def chat_json(messages, model=None, temperature=None, timeout=None):
    """要求模型只回 JSON；返回 (dict, used_model)。"""
    txt, used = chat(messages, model=model, temperature=temperature, timeout=timeout)
    return _extract_json(txt), used


def vision(prompt, image_bytes, system="你是验证码识别助手，只输出 JSON，不要解释。",
           model=None, timeout=None):
    """多模态：把 PNG/JPEG 字节作为图片发给模型，返回 (dict, used_model)。"""
    import base64
    b64 = base64.b64encode(image_bytes).decode("ascii")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,%s" % b64}},
        ]},
    ]
    return chat_json(messages, model=model, timeout=timeout)


# 兼容旧接口
def list_models():
    return {"data": [{"id": m} for m in _models()]}
