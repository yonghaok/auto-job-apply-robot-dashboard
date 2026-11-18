# -*- coding: utf-8 -*-
"""keep-alive 连接上「第二个 POST 必须拿到自己的正文」回归（T65）。

事故（2026-09-23 22:23，访问日志 + 磁盘可查）：
  22:20:55 POST /api/apply-all  正文 {ids(100+条), mode, concurrency}   ← 一键投递
  22:23:03 POST /api/profile    正文应是个人信息                        ← 保存个人信息
  → profile.json 内容 = {ids, mode, concurrency}，个人信息全没了。
根因：`_body()` 的结果缓存在 `self._body_cache`，而 `BaseHTTPRequestHandler` 在
keep-alive 下**复用同一个实例**处理同一条连接上的多个请求 → 缓存跨请求残留。

覆盖
 A) **同一条连接**上连续两次 POST 到不同端点 → 各自拿到自己的正文（真实隔离服务）
 B) 同一端点连续两次 POST → 第二次必须是新正文
 C) 证伪：把 `handle_one_request` 换回基类实现（= 去掉按请求重置）→ 必须重现串正文
 D) 源码结构：Handler 里有按请求重置；且不是在 do_GET/do_POST 里「顺手」清一个
    （基类在 do_* 之前就可能读 body，且 PUT/HEAD 等不走 do_POST）

用法（cwd=backend）：<venv-python> smoke_keepalive_body.py
"""
import http.client
import inspect
import json
import os
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["RESUME_CDP_ENDPOINT"] = "http://127.0.0.1:59999"

import filler                      # noqa: E402
import server as srv               # noqa: E402

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


TMP = tempfile.mkdtemp(prefix="smoke_ka_")
# 隔离：这些端点会写盘，绝不能碰真实 profile.json / match_rules.json / access.log
srv.PROFILE = os.path.join(TMP, "profile.json")
srv.ACCESS_LOG_PATH = os.path.join(TMP, "access.log")
filler.MATCH_RULES = os.path.join(TMP, "match_rules.json")
filler.PROFILE_PATH = srv.PROFILE


class _NoResetHandler(srv.Handler):
    """证伪用：去掉按请求重置（等价于修复前的行为）。"""
    handle_one_request = srv.BaseHTTPRequestHandler.handle_one_request


def start(handler_cls):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def conn(port):
    return http.client.HTTPConnection("127.0.0.1", port, timeout=10)


def post(c, path, obj):
    # 必须自己 encode：http.client 对 str body 默认按 latin-1 编码，中文会炸
    c.request("POST", path, body=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
              headers={"Content-Type": "application/json; charset=utf-8"})
    r = c.getresponse()
    raw = r.read()
    try:
        return r.status, json.loads(raw)
    except Exception:
        return r.status, raw


def get(c, path):
    c.request("GET", path)
    r = c.getresponse()
    raw = r.read()
    try:
        return r.status, json.loads(raw)
    except Exception:
        return r.status, raw


def main():
    httpd, port = start(srv.Handler)
    try:
        print("\n=== A/B. 同一条 keep-alive 连接上多个 POST ===")
        c = conn(port)   # **一条连接**连发
        st1, _ = post(c, "/api/match-rules", {"keywords": ["关键词A"], "cities": ["北京"]})
        check("A1 第一个 POST（匹配条件）成功", st1 == 200, st1)

        st2, _ = post(c, "/api/profile", {"name": "张三", "phone": "13800000000"})
        check("A2 第二个 POST（个人信息）成功", st2 == 200, st2)

        _, prof = get(c, "/api/profile")
        check("A3 个人信息**写对了**（不是上一条请求的正文）",
              isinstance(prof, dict) and prof.get("name") == "张三"
              and prof.get("phone") == "13800000000", repr(prof)[:200])
        check("A4 个人信息**没有被混入**匹配条件字段",
              isinstance(prof, dict) and "keywords" not in prof and "ids" not in prof,
              repr(prof)[:200])

        _, rules = get(c, "/api/match-rules")
        check("A5 匹配条件也没被后面的请求污染",
              rules.get("keywords") == ["关键词A"] and rules.get("cities") == ["北京"],
              repr(rules)[:200])

        st3, _ = post(c, "/api/match-rules", {"keywords": ["关键词B"], "cities": ["上海"]})
        _, rules2 = get(c, "/api/match-rules")
        check("B1 同一端点第二次 POST 也是新正文（不是第一次的）",
              st3 == 200 and rules2.get("keywords") == ["关键词B"]
              and rules2.get("cities") == ["上海"], repr(rules2)[:200])

        # 一键投递载荷形态（ids/mode/concurrency）——真实事故里就是这个串进来了
        st4, _ = post(c, "/api/profile", {"ids": ["a", "b"], "mode": "strict",
                                         "concurrency": 3})
        st5, _ = post(c, "/api/profile", {"name": "李四"})
        _, prof2 = get(c, "/api/profile")
        check("B2 连续两个 POST /api/profile：以最后一次为准，且内容正确",
              st4 == 200 and st5 == 200 and prof2.get("name") == "李四"
              and "ids" not in prof2, repr(prof2)[:200])
        c.close()

        print("\n  --- 证伪：去掉按请求重置，必须重现串正文 ---")
        httpd2, port2 = start(_NoResetHandler)
        try:
            c2 = conn(port2)
            try:
                os.remove(srv.PROFILE)
            except OSError:
                pass
            post(c2, "/api/match-rules", {"keywords": ["污染源"], "cities": ["北京"]})
            st_bad, _ = post(c2, "/api/profile", {"name": "王五"})
            c2.close()
        finally:
            httpd2.shutdown()
        # 判据用**磁盘文件**，不用 HTTP 回读：串正文会让第二个请求**不读 socket**，
        # 残留字节把同连接的后续请求冲成 400（这本身就是旧故障的第二重伤害）。
        try:
            import io as _io
            bad = json.loads(_io.open(srv.PROFILE, encoding="utf-8").read())
        except Exception as e:
            bad = {"_read_error": str(e)}
        check("C1 去掉重置后**重现**旧故障：第二个 POST 返回成功，但落盘的是上一条请求的正文",
              st_bad == 200 and isinstance(bad, dict)
              and bad.get("keywords") == ["污染源"],
              "status=%s on_disk=%s" % (st_bad, repr(bad)[:200]))
        check("C2 → 说明 A3 的断言确实钉住了这次修复（不是恒真）",
              isinstance(bad, dict) and bad.get("name") != "王五")
    finally:
        httpd.shutdown()

    print("\n=== D. 源码结构 ===")
    hsrc = inspect.getsource(srv.Handler)
    check("D1 Handler 覆写了 handle_one_request", "def handle_one_request" in hsrc)
    check("D2 里面显式把 _body_cache 置空（按请求重置）",
          "self._body_cache = None" in hsrc)
    check("D3 重置发生在调基类之前（否则本请求仍会读到旧缓存）",
          hsrc.index("self._body_cache = None")
          < hsrc.index("BaseHTTPRequestHandler.handle_one_request(self)"))

    print("\n===== 汇总：PASS=%d FAIL=%d =====" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
