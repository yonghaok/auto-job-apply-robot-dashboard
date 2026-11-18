# -*- coding: utf-8 -*-
"""smoke: /api/parse-resume 路由 + /vendor 静态托管（PDF.js）+ 防目录穿越。

纯标准库直跑 HTTP（去代理）。默认不真调 LLM（避免耗 token / 网络抖动）；
需要验证端到端 LLM 抽取时：设 SMOKE_LLM=1 再跑。

用法（cwd=backend）：
  <venv-python> smoke_parse_resume.py
  SMOKE_LLM=1 <venv-python> smoke_parse_resume.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("RESUME_BASE", "http://127.0.0.1:8000")
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

results = []


def req(method, path, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with _opener.open(r, timeout=25) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001
        return -1, str(e).encode("utf-8", "replace")


def check(name, cond, detail=""):
    results.append((name, cond))
    print(("PASS" if cond else "FAIL"), "|", name, ("| " + detail) if detail else "")


# ---- 静态托管 ----
s, b = req("GET", "/")
check("GET / -> 200", s == 200, "code=%s" % s)
check("index.html 引用 pdf.min.js", b"vendor/pdf.min.js" in b)
check("index.html 引用 pdf.worker.min.js", b"vendor/pdf.worker.min.js" in b)

s, b = req("GET", "/vendor/pdf.min.js")
check("GET /vendor/pdf.min.js -> 200", s == 200, "code=%s" % s)
check("pdf.min.js 内容非空(>100KB)", len(b) > 100000, "len=%d" % len(b))

s, b = req("GET", "/vendor/pdf.worker.min.js")
check("GET /vendor/pdf.worker.min.js -> 200", s == 200, "code=%s len=%d" % (s, len(b)))

# ---- 防目录穿越 / 不存在文件 ----
s, _ = req("GET", "/vendor/..%2fserver.py")
check("防穿越 /vendor/..%2fserver.py -> 404", s == 404, "code=%s" % s)
s, _ = req("GET", "/vendor/nope.js")
check("不存在的 vendor 文件 -> 404", s == 404, "code=%s" % s)

# ---- 参数校验 ----
s, _ = req("POST", "/api/parse-resume", {})
check("缺 text -> 400", s == 400, "code=%s" % s)
s, _ = req("POST", "/api/parse-resume", {"text": "   "})
check("空白 text -> 400", s == 400, "code=%s" % s)
# 注：_body()/_parse_payload 对非 dict JSON（数组/字符串）会「宽容包装」成 {"text": 原文}
# （为短信上报设计），故数组 body 不会 4xx，而是被当文本走 LLM —— 既有设计，非本接口缺陷。
# 这里改测真正可控的边界：无 body。
s, _ = req("POST", "/api/parse-resume")
check("空 body -> 400", s == 400, "code=%s" % s)

# ---- 可选：真实 LLM 抽取 ----
if os.environ.get("SMOKE_LLM") == "1":
    sample = ("张三，男。2023.9-2027.6 某某大学 计算机科学 本科。"
              "工作经历：2025.6-2025.9 字节跳动 后端开发实习生，负责推荐系统优化。"
              "项目：校园二手交易平台，前端负责人。技能：Python, Java。奖项：国家奖学金。")
    s, b = req("POST", "/api/parse-resume", {"text": sample})
    d = {}
    try:
        d = json.loads(b)
    except Exception:
        pass
    check("LLM 抽取 HTTP 200", s == 200, "code=%s" % s)
    check("LLM 抽取 ok=true", d.get("ok") is True,
          json.dumps(d, ensure_ascii=False)[:220])
    for k in ("experiences", "projects", "skills", "awards"):
        check("抽取字段 %s 为 list" % k, isinstance(d.get(k), list))

passed = sum(1 for _n, c in results if c)
total = len(results)
print("\n%d/%d passed" % (passed, total))
sys.exit(0 if passed == total else 1)
