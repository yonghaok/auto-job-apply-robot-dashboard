"""短信入口取证探针（第二轮）：补第一轮没覆盖的真实丢短信路径。**只诊断，不改代码。**

第一轮 smoke_sms_intake.py 已证实：form / chunked / BOM / 别名字段 / GBK 全部 400 丢弃，
且被拒请求不留痕。本轮补：
  M) OPTIONS 预检（WebView/浏览器型转发 APP 必发）返回什么
  N) GET /sms?phone=&code=（部分 APP 用 GET 上报）会不会被当静态页/404
  O) Content-Type: text/plain 但正文是合法 JSON（APP 常见）
  P) 有正文但 Content-Length 缺失/为 0 + Connection: close
  Q) 跨发件人串码：两个号码先后到，latest_code 会不会取到「另一个站」的码
  R) 落盘失败时，磁盘是否落后于内存（重启即丢）

用法：python smoke_sms_intake2.py
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="sms-audit2-")
os.environ["RESUME_SMS"] = os.path.join(TMP, "sms.json")
os.environ["RESUME_SMS_HIST"] = os.path.join(TMP, "sms_hist.json")
# 拒收台账也要隔离：probe 1 曾漏了这行，把 `{}` 的拒收记录写进了真实 backend 目录
os.environ["RESUME_SMS_REJECTED"] = os.path.join(TMP, "sms_rejected.jsonl")
os.environ["RESUME_ACCESS_LOG"] = os.path.join(TMP, "access.log")
sys.path.insert(0, HERE)

import server  # noqa: E402

PORT = 8792
BASE = "http://127.0.0.1:%d" % PORT
RESULTS = []


def note(name, verdict, detail=""):
    RESULTS.append((name, verdict, detail))
    print("   [%s] %s%s" % (verdict, name, ("  " + detail) if detail else ""))


def post(payload_bytes, ctype, raw_headers=None):
    """走原生 socket，方便精确控制 Content-Type / Content-Length / method。"""
    s = socket.create_connection(("127.0.0.1", PORT), timeout=8)
    try:
        head = "POST /sms HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n"
        head += "Content-Type: %s\r\n" % ctype
        if raw_headers:
            for k, v in raw_headers:
                head += "%s: %s\r\n" % (k, v)
        else:
            head += "Content-Length: %d\r\n" % len(payload_bytes)
        s.sendall(head.encode("ascii") + b"\r\n" + payload_bytes)
        s.settimeout(8)
        buf = b""
        while True:
            try:
                b = s.recv(4096)
            except socket.timeout:
                break
            if not b:
                break
            buf += b
        return buf.split(b"\r\n", 1)[0].decode("ascii", "replace"), buf
    finally:
        s.close()


def req(method, path, body=b"", headers=None):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=8)
    try:
        head = "%s %s HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n" % (method, path)
        for k, v in (headers or []):
            head += "%s: %s\r\n" % (k, v)
        if body:
            head += "Content-Length: %d\r\n" % len(body)
        s.sendall(head.encode("ascii") + b"\r\n" + body)
        s.settimeout(8)
        buf = b""
        while True:
            try:
                b = s.recv(4096)
            except socket.timeout:
                break
            if not b:
                break
            buf += b
        return buf.split(b"\r\n", 1)[0].decode("ascii", "replace"), buf
    finally:
        s.close()


def hist_len():
    return len(server.SMS_HIST)


def post_ok(n):
    body = json.dumps({"phone": "106900801534293", "code": "1%03d" % n,
                       "text": "【某某】验证码1%03d" % n}).encode("utf-8")
    post(body, "application/json")


print("短信库（临时）：%s" % TMP)
threading.Thread(
    target=lambda: server.ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
    .serve_forever(), daemon=True).start()
time.sleep(0.4)

# 先建立对照组：确认基线可写
b0 = hist_len()
post_ok(1)
note("基线：标准 JSON 可入库", "OK" if hist_len() == b0 + 1 else "异常",
     "hist %d→%d" % (b0, hist_len()))

print("\n=== M) OPTIONS 预检（WebView / 浏览器型 APP 必发）===")
line, _ = req("OPTIONS", "/sms", headers=[("Origin", "http://localhost"),
                                          ("Access-Control-Request-Method", "POST"),
                                          ("Access-Control-Request-Headers", "content-type")])
st = line.split(" ")[1] if " " in line else line
note("OPTIONS 预检被正确处理", "OK" if st == "200" else "**丢（预检被拒，真实 POST 不会发出）**",
     line.strip())

print("\n=== N) GET /sms?phone=&code=（部分 APP 用 GET 上报）===")
before = hist_len()
line, buf = req("GET", "/sms?phone=106900801534293&code=2525&text=viaGET")
after = hist_len()
body_l = buf.decode("utf-8", "replace")
note("GET 上报能被接收", "OK" if after > before else "**丢**",
     "%s hist %d→%d 响应体前 60 字=%r" % (line.strip(), before, after, body_l[-160:][:60]))

print("\n=== O) Content-Type: text/plain 但正文是合法 JSON ===")
before = hist_len()
body = json.dumps({"phone": "106900801534293", "code": "2626",
                   "text": "【某某】验证码2626"}).encode("utf-8")
line, _ = post(body, "text/plain;charset=UTF-8")
after = hist_len()
note("text/plain + JSON 正文能被接收", "OK" if after > before else "**丢**",
     "%s hist %d→%d" % (line.strip(), before, after))

print("\n=== P) 正文在，但 Content-Length 缺失（chunked 之外的上报方式）===")
before = hist_len()
body = json.dumps({"phone": "106900801534293", "code": "2727",
                   "text": "【某某】验证码2727"}).encode("utf-8")
line, _ = post(body, "application/json", raw_headers=[("Content-Length", "0")])
after = hist_len()
note("Content-Length 为 0 时正文仍被读", "OK" if after > before else "**丢**",
     "%s hist %d→%d" % (line.strip(), before, after))

print("\n=== Q) 跨发件人串码（另一个站的验证码会不会被取走）===")
server.SMS_HIST.clear()
server.SMS.clear()
# t1: A 站（本项目要登录的站）先到
post(json.dumps({"phone": "106900801534293", "code": "0001",
                 "text": "【A站】验证码0001"}).encode("utf-8"), "application/json")
time.sleep(1.1)   # 保证 received 时间戳不同（秒级）
# t2: B 站（完全无关，用户同时在别处登录）后到
post(json.dumps({"phone": "106955588888", "code": "9999",
                 "text": "【B站】验证码9999"}).encode("utf-8"), "application/json")
# 模拟机器人取码：snapshot 在两条都到之前拍（started 之前）
got = server.latest_code(after="", snapshot={})
note("latest_code 只取「本站在等」的码",
     "OK" if got == "0001" else "**串码**",
     "取到 code=%s（期望 0001；9999 是无关站点的码）" % got)

print("\n=== R) 落盘失败时磁盘是否落后于内存（重启即丢）===")


def broken_replace(a, b):
    raise PermissionError("[simulated] 文件被占用/只读")


real_replace = os.replace
os.replace = broken_replace
try:
    mem_before = hist_len()
    disk_before = None
    try:
        with open(server.SMS_HIST_PATH, "r", encoding="utf-8") as f:
            disk_before = len(json.load(f))
    except Exception:
        disk_before = -1
    post(json.dumps({"phone": "106900801534293", "code": "3131",
                     "text": "落盘必失败"}).encode("utf-8"), "application/json")
    time.sleep(0.3)
    mem_after = hist_len()
    try:
        with open(server.SMS_HIST_PATH, "r", encoding="utf-8") as f:
            disk_after = len(json.load(f))
    except Exception as e:
        disk_after = "读不出: %s" % e
    note("落盘失败后，磁盘是否仍与内存一致",
         "OK" if isinstance(disk_after, int) and disk_after == mem_after else "**落后（重启即丢）**",
         "内存 %d→%d / 磁盘 %s→%s" % (mem_before, mem_after, disk_before, disk_after))
finally:
    os.replace = real_replace

print("\n================ 汇总 ================")
for n, v, d in RESULTS:
    print("  %-4s %s" % (v, n))
bad = [r for r in RESULTS if r[1] != "OK"]
print("\n  可疑 %d / 共 %d 项" % (len(bad), len(RESULTS)))
