"""短信入口取证探针：**只诊断，不改代码**。

目的：回答「为什么收短信慢、为什么短信极易丢」。做法是把 `server` 模块指向
**临时短信库**、在本地起一个真的 `ThreadingHTTPServer`，然后按各种真实可能出现的
报文形态打 `/sms`，看后端到底记了什么、丢了什么。

刻意包含的报文形态（都来自「转发 APP 常见行为」）：
  ok-json      标准 JSON（拿它当对照组）
  form         application/x-www-form-urlencoded（很多 APP 默认这种）
  chunked      Transfer-Encoding: chunked（无 Content-Length）
  bom          正文前带 UTF-8 BOM 的 JSON
  alias        JSON 但字段名不是 phone/code/text（sender/msg/content/body…）
  gbk          GBK 编码的 JSON 正文
  nodedup      同一号码同一内容重发（看会不会被当成两条）
  burst        并发 12 条（看会不会互相踩 .tmp → 丢写/半截文件）

用法：python smoke_sms_intake.py
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
TMP = tempfile.mkdtemp(prefix="sms-audit-")
# **必须在 import server 之前**设置，否则会写到真实库
os.environ["RESUME_SMS"] = os.path.join(TMP, "sms.json")
os.environ["RESUME_SMS_HIST"] = os.path.join(TMP, "sms_hist.json")
# 拒收台账也要隔离：probe 1 曾漏了这行，把 `{}` 的拒收记录写进了真实 backend 目录
os.environ["RESUME_SMS_REJECTED"] = os.path.join(TMP, "sms_rejected.jsonl")
os.environ["RESUME_ACCESS_LOG"] = os.path.join(TMP, "access.log")
sys.path.insert(0, HERE)

import server  # noqa: E402

PORT = 8791
BASE = "http://127.0.0.1:%d" % PORT
RESULTS = []


def note(name, verdict, detail=""):
    RESULTS.append((name, verdict, detail))
    print("   [%s] %s%s" % (verdict, name, ("  " + detail) if detail else ""))


def post_json(payload, ctype="application/json"):
    req = urllib.request.Request(
        BASE + "/sms", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": ctype}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, "%s: %s" % (type(e).__name__, e)


def post_raw(headers, body_bytes, method="POST"):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=8)
    try:
        head = "%s /sms HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n" % method
        for k, v in headers:
            head += "%s: %s\r\n" % (k, v)
        s.sendall(head.encode("ascii") + b"\r\n" + body_bytes)
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
        first = buf.split(b"\r\n", 1)[0].decode("ascii", "replace")
        return first, buf.decode("utf-8", "replace")
    finally:
        s.close()


def hist_len():
    return len(server.SMS_HIST)


def disk_hist():
    p = server.SMS_HIST_PATH
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return "CORRUPT: %s: %s" % (type(e).__name__, e)


print("短信库（临时）：%s" % TMP)
th = threading.Thread(
    target=lambda: server.ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
    .serve_forever(), daemon=True)
th.start()
time.sleep(0.4)

print("\n=== A) 标准 JSON（对照组，应当成功）===")
st, body = post_json({"phone": "106900801534293", "code": "1111",
                      "text": "【某某】验证码1111"})
n = hist_len()
note("标准 JSON → 200 且入库", "OK" if (st == 200 and n == 1) else "异常",
     "http=%s hist=%d body=%s" % (st, n, body[:80]))

print("\n=== B) form-urlencoded（很多转发 APP 的默认格式）===")
before = hist_len()
try:
    req = urllib.request.Request(
        BASE + "/sms", data=b"phone=106900801534293&code=2222&text=hello",
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(req, timeout=8) as r:
        st = r.status
except urllib.error.HTTPError as e:
    st = e.code
after = hist_len()
note("form 编码能被接收", "OK" if after > before else "**丢**",
     "http=%s hist %d→%d" % (st, before, after))

print("\n=== C) chunked（无 Content-Length）===")
before = hist_len()
payload = json.dumps({"phone": "106900801534293", "code": "3333", "text": "chunked"})
chunked = b"%x\r\n%s\r\n0\r\n\r\n" % (len(payload), payload.encode("utf-8"))
line, _ = post_raw([("Transfer-Encoding", "chunked")], chunked)
after = hist_len()
note("chunked 能被接收", "OK" if after > before else "**丢**",
     "%s hist %d→%d" % (line.strip(), before, after))

print("\n=== D) 正文带 UTF-8 BOM 的 JSON ===")
before = hist_len()
raw = b"\xef\xbb\xbf" + json.dumps({"phone": "106900801534293", "code": "4444",
                                    "text": "bom"}).encode("utf-8")
line, _ = post_raw([("Content-Type", "application/json"),
                    ("Content-Length", str(len(raw)))], raw)
after = hist_len()
note("带 BOM 的 JSON 能被接收", "OK" if after > before else "**丢**",
     "%s hist %d→%d" % (line.strip(), before, after))

print("\n=== E) 字段名不是 phone/code/text（sender/msg/body…）===")
before = hist_len()
st, body = post_json({"sender": "106900801534293", "msg": "【某某】验证码5555",
                      "content": "验证码5555"})
after = hist_len()
note("别名字段能被识别", "OK" if after > before else "**丢**",
     "http=%s hist %d→%d body=%s" % (st, before, after, body[:60]))

print("\n=== F) GBK 编码正文 ===")
before = hist_len()
raw = json.dumps({"phone": "106900801534293", "code": "6666", "text": "验证码6666"},
                 ensure_ascii=False).encode("gbk")
line, _ = post_raw([("Content-Type", "application/json"),
                    ("Content-Length", str(len(raw)))], raw)
after = hist_len()
note("GBK 正文能被接收", "OK" if after > before else "**丢**",
     "%s hist %d→%d" % (line.strip(), before, after))

print("\n=== G) 400 时是否留下痕迹（事后能否查出「有短信来过」）===")
before = hist_len()
st, _ = post_json({})
after = hist_len()
rejected = getattr(server, "SMS_REJECTED", None)
note("被拒请求是否落盘可查",
     "OK" if (rejected is not None and len(rejected) > 0) else "**无痕迹**",
     "hist %d→%d SMS_REJECTED=%s" % (before, after, rejected))

print("\n=== H) 并发 12 条（同一发件人，看会不会互相踩 .tmp）===")
base_n = hist_len()
codes = ["7%03d" % i for i in range(12)]
errs = []
lock = threading.Lock()
real_replace = os.replace
state = {"replace_fail": 0}


def wrapped_replace(a, b):
    try:
        return real_replace(a, b)
    except Exception as e:
        with lock:
            state["replace_fail"] += 1
        raise


os.replace = wrapped_replace


def one(c):
    try:
        st, _ = post_json({"phone": "106900801534293", "code": c,
                           "text": "【某某】验证码%s" % c})
        if st != 200:
            with lock:
                errs.append("http=%s" % st)
    except Exception as e:
        with lock:
            errs.append("%s" % type(e).__name__)


ts = [threading.Thread(target=one, args=(c,)) for c in codes]
t0 = time.time()
for t in ts:
    t.start()
for t in ts:
    t.join()
el = time.time() - t0
os.replace = real_replace
time.sleep(0.5)

mem_n = hist_len() - base_n
dh = disk_hist()
disk_n = len(dh) - base_n if isinstance(dh, list) else -1
note("12 条并发全部入库（内存）", "OK" if mem_n == 12 else "**丢**", "内存新增 %d/12" % mem_n)
note("12 条并发全部落盘", "OK" if disk_n == 12 else "**丢**",
     "磁盘新增 %s/12，replace 失败 %d 次" % (disk_n, state["replace_fail"]))
note("落盘文件仍是合法 JSON", "OK" if isinstance(dh, list) else "**损坏**",
     str(dh)[:90] if not isinstance(dh, list) else "ok")

print("\n=== I) 延迟：单条 /sms 的端到端耗时（后端处理 + 落盘）===")
lat = []
for i in range(5):
    t0 = time.time()
    post_json({"phone": "106900801534293", "code": "9%03d" % i, "text": "lat"})
    lat.append((time.time() - t0) * 1000)
note("单条 /sms 处理耗时", "OK" if max(lat) < 300 else "偏慢",
     "min=%.0fms max=%.0fms avg=%.0fms" % (min(lat), max(lat), sum(lat) / len(lat)))

print("\n=== J) 重复上报（APP 重试）会不会变成两条 ===")
base_n = hist_len()
post_json({"phone": "106900801534293", "code": "8888", "text": "【某某】验证码8888"})
post_json({"phone": "106900801534293", "code": "8888", "text": "【某某】验证码8888"})
d = hist_len() - base_n
note("重复上报被去重", "OK" if d == 1 else "**重复入库**", "入库 %d 条" % d)

print("\n=== K) received 是「PC 收到时刻」，能否区分手机侧延迟 ===")
rec = server.SMS_HIST[-1] if server.SMS_HIST else {}
keys = sorted(rec.keys())
note("记录里是否含手机侧时间戳（sent_at 等）",
     "OK" if any(k in rec for k in ("sent_at", "phone_ts", "client_ts")) else "**没有**",
     "字段=%s" % keys)

print("\n=== L) HTTP 协议版本（影响 APP 能否复用连接）===")
ver = getattr(server.Handler, "protocol_version", "HTTP/1.0")
note("服务端协议版本", "OK" if ver == "HTTP/1.1" else "偏旧（每次新建 TCP）",
     "protocol_version=%s" % ver)

print("\n================ 汇总 ================")
bad = [r for r in RESULTS if r[1] != "OK"]
for n, v, d in RESULTS:
    print("  %-4s %s" % (v, n))
print("\n  可疑 %d / 共 %d 项" % (len(bad), len(RESULTS)))
