"""短信入口修复回归测试（断言式，硬 PASS/FAIL）。

对应「收短信慢 / 短信极易丢失」的 P0+P1 修复。每条断言都对应一个**修前实测会失败**的
路径（对照 smoke_sms_intake.py / smoke_sms_intake2.py 的探针结论）：

  A 标准 JSON                                  基线
  B form-urlencoded                            修前 400 丢
  C chunked（无 Content-Length）               修前 400 丢
  D 带 UTF-8 BOM 的 JSON                       修前 400 丢
  E GBK 正文（中文短信常见）                    修前 400 丢
  F 别名字段 sender/msg/content                修前 400 丢
  G Content-Length:0 + Connection: close 带正文 修前 400 丢
  H GET /sms?phone=&code=                     修前 404 丢
  I OPTIONS 预检 + CORS 头                     修前 501
  J 空 body → 400 且**必须留痕**                 修前不留痕
  K 12 条并发落盘                              修前 11/12 抛 WinError 32
  L 落盘失败**不得毁掉已有文件**                  修前会就地覆写 → 可能截断整库清零
  M HTTP/1.1                                   修前 HTTP/1.0
  N latest_code 按站点优先取码（含回退）          修前串码取到别站的码
  O 纯文本含验证码（无键值）                    修前 400 丢
  P /api/sms 与 /api/sms/rejected 端点

用法：python smoke_sms_intake_fix.py
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
TMP = tempfile.mkdtemp(prefix="sms-fix-")
os.environ["RESUME_SMS"] = os.path.join(TMP, "sms.json")
os.environ["RESUME_SMS_HIST"] = os.path.join(TMP, "sms_hist.json")
os.environ["RESUME_SMS_REJECTED"] = os.path.join(TMP, "sms_rejected.jsonl")
os.environ["RESUME_ACCESS_LOG"] = os.path.join(TMP, "access.log")
sys.path.insert(0, HERE)

import server  # noqa: E402

PORT = 8793
BASE = "http://127.0.0.1:%d" % PORT
OK = []
BAD = []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print("   [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                            ("  " + detail) if detail else ""))


def raw_post(headers, body, method="POST"):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=8)
    try:
        head = "%s /sms HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n" % method
        for k, v in headers:
            head += "%s: %s\r\n" % (k, v)
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


def http(method, path, body=None, ctype=None, headers=None):
    req = urllib.request.Request(BASE + path, data=body, method=method)
    if ctype:
        req.add_header("Content-Type", ctype)
    for k, v in (headers or []):
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)
    except Exception as e:
        return -1, "%s: %s" % (type(e).__name__, e), {}


def jbody(d):
    return json.dumps(d, ensure_ascii=False).encode("utf-8")


def reset():
    with server.LOCK:
        server.SMS_HIST.clear()
        server.SMS.clear()
        server.SMS_REJECTED.clear()


def hist():
    with server.LOCK:
        return list(server.SMS_HIST)


def disk_hist():
    try:
        with open(server.SMS_HIST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return "ERR: %s" % e


print("临时短信库：%s" % TMP)
threading.Thread(
    target=lambda: server.ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
    .serve_forever(), daemon=True).start()
time.sleep(0.4)

print("\n=== A) 标准 JSON（基线）===")
reset()
st, body, _ = http("POST", "/sms", jbody({"phone": "106900801534293", "code": "1111",
                                          "text": "【某某】验证码1111"}), "application/json")
h = hist()
check("标准 JSON 入库且 code 正确", st == 200 and len(h) == 1 and h[0]["code"] == "1111",
      "http=%s hist=%d" % (st, len(h)))

print("\n=== B) form-urlencoded（修前 400 丢）===")
reset()
st, body, _ = http("POST", "/sms", b"phone=106900801534293&code=2222&text=hi",
                   "application/x-www-form-urlencoded")
h = hist()
check("form 编码入库且 phone/code 正确",
      st == 200 and len(h) == 1 and h[0]["phone"] == "106900801534293" and h[0]["code"] == "2222",
      "http=%s hist=%d" % (st, len(h)))

print("\n=== C) chunked（无 Content-Length，修前 400 丢）===")
reset()
p = jbody({"phone": "106900801534293", "code": "3333", "text": "chunked"})
data = b"%x\r\n%s\r\n0\r\n\r\n" % (len(p), p)
line, _ = raw_post([("Transfer-Encoding", "chunked")], data)
h = hist()
check("chunked 入库", len(h) == 1 and h[0]["code"] == "3333",
      "%s hist=%d" % (line.strip(), len(h)))

print("\n=== D) 带 UTF-8 BOM 的 JSON（修前 400 丢）===")
reset()
p = b"\xef\xbb\xbf" + jbody({"phone": "106900801534293", "code": "4444", "text": "bom"})
line, _ = raw_post([("Content-Type", "application/json"),
                    ("Content-Length", str(len(p)))], p)
h = hist()
check("带 BOM 入库", len(h) == 1 and h[0]["code"] == "4444",
      "%s hist=%d" % (line.strip(), len(h)))

print("\n=== E) GBK 正文（修前 400 丢）===")
reset()
p = json.dumps({"phone": "106900801534293", "code": "5555",
                "text": "【某某】验证码5555，请勿泄露"}, ensure_ascii=False).encode("gbk")
line, _ = raw_post([("Content-Type", "application/json"),
                    ("Content-Length", str(len(p)))], p)
h = hist()
check("GBK 入库且中文正文未乱码",
      len(h) == 1 and h[0]["code"] == "5555" and "请勿泄露" in (h[0]["text"] or ""),
      "%s text=%r" % (line.strip(), (h[0]["text"] if h else "")))

print("\n=== F) 别名字段 sender/msg/content（修前 400 丢）===")
reset()
st, body, _ = http("POST", "/sms",
                   jbody({"sender": "106900801534293", "msg": "【某某】验证码6666",
                          "content": "验证码6666"}), "application/json")
h = hist()
check("别名字段被识别（phone 取 sender）",
      st == 200 and len(h) == 1 and h[0]["phone"] == "106900801534293",
      "http=%s phone=%s" % (st, (h[0]["phone"] if h else "")))
check("别名字段：code 从 msg 正文兜底提取", bool(h) and h[0]["code"] == "6666",
      "code=%s" % (h[0]["code"] if h else ""))

print("\n=== G) Content-Length:0 + Connection: close 但带正文（修前 400 丢）===")
reset()
p = jbody({"phone": "106900801534293", "code": "7777", "text": "cl0"})
line, _ = raw_post([("Content-Type", "application/json"), ("Content-Length", "0")], p)
h = hist()
check("CL:0 但读到 EOF → 入库", len(h) == 1 and h[0]["code"] == "7777",
      "%s hist=%d" % (line.strip(), len(h)))

print("\n=== H) GET /sms?phone=&code=（修前 404 丢）===")
reset()
st, body, _ = http("GET", "/sms?phone=106900801534293&code=8888&text=viaGET")
h = hist()
check("GET 上报入库", st == 200 and len(h) == 1 and h[0]["code"] == "8888",
      "http=%s hist=%d body=%s" % (st, len(h), body[:60]))
st2, body2, _ = http("GET", "/sms")
check("GET /sms 无上报字段时＝自检（不写库）",
      st2 == 200 and len(hist()) == 1 and "total" in body2,
      "http=%s body=%s" % (st2, body2[:80]))

print("\n=== I) OPTIONS 预检 + CORS（修前 501）===")
st, body, hdrs = http("OPTIONS", "/sms")
check("OPTIONS 返回 200", st == 200, "http=%s" % st)
check("带 Access-Control-Allow-Origin",
      hdrs.get("Access-Control-Allow-Origin") == "*",
      "ACAO=%r" % hdrs.get("Access-Control-Allow-Origin"))
check("带 Access-Control-Allow-Methods",
      "POST" in (hdrs.get("Access-Control-Allow-Methods") or ""),
      "ACAM=%r" % hdrs.get("Access-Control-Allow-Methods"))

print("\n=== J) 空 body → 400 且必须留痕（修前静默不留痕）===")
reset()
rej_path = server.SMS_REJECTED_PATH
if os.path.exists(rej_path):
    os.remove(rej_path)
st, body, _ = http("POST", "/sms", b"", "application/json")
check("空 body 返回 400", st == 400, "http=%s body=%s" % (st, body[:60]))
check("内存里有拒收记录", len(server.SMS_REJECTED) >= 1,
      "SMS_REJECTED=%d" % len(server.SMS_REJECTED))
check("磁盘 sms_rejected.jsonl 有行", os.path.exists(rej_path) and
      sum(1 for _ in open(rej_path, encoding="utf-8")) >= 1,
      "path=%s" % rej_path)

print("\n=== K) 12 条并发落盘（修前 11/12 抛 WinError 32）===")
reset()
real_replace = os.replace
state = {"fail": 0}
lk = threading.Lock()


def wrapped(a, b, *rest):
    try:
        return real_replace(a, b, *rest)
    except Exception:
        with lk:
            state["fail"] += 1
        raise


os.replace = wrapped
errs = []


def one(i):
    try:
        st, _, _ = http("POST", "/sms", jbody({"phone": "106900801534293",
                                               "code": "9%03d" % i,
                                               "text": "并发%s" % i}), "application/json")
        if st != 200:
            errs.append("http=%s" % st)
    except Exception as e:
        errs.append(type(e).__name__)


ts = [threading.Thread(target=one, args=(i,)) for i in range(12)]
t0 = time.time()
for t in ts:
    t.start()
for t in ts:
    t.join()
el = (time.time() - t0) * 1000
os.replace = real_replace
time.sleep(0.5)
mem = len(hist())
d = disk_hist()
disk = len(d) if isinstance(d, list) else -1
check("12 条并发全部入库（内存）", mem == 12 and not errs,
      "内存=%d errs=%s" % (mem, errs[:3]))
check("12 条并发全部落盘（磁盘）", disk == 12, "磁盘=%d" % disk)
check("落盘零冲突（修前 11 次 WinError 32）", state["fail"] == 0,
      "replace 失败=%d，12 条耗时 %.0fms" % (state["fail"], el))
check("落盘文件仍是合法 JSON", isinstance(d, list), "%s" % str(d)[:80])

print("\n=== L) 落盘失败不得毁掉已有文件（修前就地覆写 → 可能整库清零）===")
reset()
http("POST", "/sms", jbody({"phone": "106900801534293", "code": "0001", "text": "先落一条"}),
     "application/json")
time.sleep(0.3)
with open(server.SMS_HIST_PATH, "r", encoding="utf-8") as f:
    before_txt = f.read()


def boom(a, b, *rest):
    raise PermissionError("[simulated] 文件被占用")


os.replace = boom
try:
    http("POST", "/sms", jbody({"phone": "106900801534293", "code": "0002",
                                "text": "这条落盘必失败"}), "application/json")
    time.sleep(0.4)
finally:
    os.replace = real_replace
with open(server.SMS_HIST_PATH, "r", encoding="utf-8") as f:
    after_txt = f.read()
try:
    still_ok = isinstance(json.loads(after_txt), list)
except Exception:
    still_ok = False
check("落盘失败后文件仍是合法 JSON", still_ok, "")
check("落盘失败后**原有内容未被破坏**", after_txt == before_txt,
      "变化=%d 字节" % (len(after_txt) - len(before_txt)))
check("内存里有这条（内存副本仍可供取码）", len(hist()) == 2, "内存=%d" % len(hist()))

print("\n=== Q) 瞬时落盘失败会重试（收窄「磁盘落后于内存」的窗口）===")
reset()
calls = {"n": 0}


def flaky(a, b, *rest):
    calls["n"] += 1
    if calls["n"] == 1:
        raise PermissionError("[simulated-transient] 第一次被占用")
    return real_replace(a, b, *rest)


os.replace = flaky
try:
    http("POST", "/sms", jbody({"phone": "106900801534293", "code": "0003",
                                "text": "瞬时冲突"}), "application/json")
    time.sleep(0.4)
finally:
    os.replace = real_replace
d2 = disk_hist()
check("第 1 次失败、第 2 次成功 → 磁盘追上内存",
      isinstance(d2, list) and len(d2) == len(hist()) and len(d2) >= 1,
      "磁盘=%s 内存=%d replace 尝试=%d"
      % (len(d2) if isinstance(d2, list) else d2, len(hist()), calls["n"]))

print("\n=== M) HTTP/1.1（修前 HTTP/1.0，无 keep-alive）===")
check("protocol_version 为 HTTP/1.1",
      getattr(server.Handler, "protocol_version", "") == "HTTP/1.1",
      "protocol_version=%s" % getattr(server.Handler, "protocol_version", ""))

print("\n=== N) latest_code 按站点优先取码（修前串码）===")
reset()
http("POST", "/sms", jbody({"phone": "106900801534293", "code": "0001",
                            "text": "【A站】验证码0001"}), "application/json")
time.sleep(1.1)
http("POST", "/sms", jbody({"phone": "106955588888", "code": "9999",
                            "text": "【B站】验证码9999"}), "application/json")
got = server.latest_code(after="", snapshot={}, match=server.sms_matcher("A站"))
check("有站点锚点时只取本站在等的码", got == "0001", "取到=%s（期望 0001）" % got)
got2 = server.latest_code(after="", snapshot={})
check("不给 match 时行为不变（向后兼容）", got2 == "9999", "取到=%s" % got2)
got3 = server.latest_code(after="", snapshot={}, match=server.sms_matcher("不存在的站点名"))
check("锚点一条都没命中 → 回退不过滤（不能永远取不到码）", got3 == "9999",
      "取到=%s" % got3)
check("sms_matcher 关键词太短时返回 None", server.sms_matcher("A", "") is None, "")
det = server.sms_pick_detail("9999")
check("sms_pick_detail 给出码的来源", "106955588888" in det and "B站" in det, det)

print("\n=== O) 纯文本含验证码（无键值，修前 400 丢）===")
reset()
st, body, _ = http("POST", "/sms", "您的验证码是 4321，请勿泄露".encode("utf-8"),
                   "text/plain; charset=utf-8")
h = hist()
check("纯文本正文也能收且提取到码", st == 200 and len(h) == 1 and h[0]["code"] == "4321",
      "http=%s hist=%d code=%s" % (st, len(h), (h[0]["code"] if h else "")))

print("\n=== P) /api/sms 与 /api/sms/rejected 端点 ===")
reset()
http("POST", "/sms", jbody({"phone": "106900801534293", "code": "1234",
                            "text": "【某某】验证码1234"}), "application/json")
st, body, _ = http("GET", "/api/sms")
try:
    dd = json.loads(body)
except Exception:
    dd = {}
check("/api/sms 返回按发件人的字典且含 code 1234",
      st == 200 and dd.get("106900801534293", {}).get("code") == "1234",
      "http=%s keys=%s" % (st, list(dd)[:3]))
st, body, _ = http("GET", "/api/sms/rejected")
try:
    rr = json.loads(body)
except Exception:
    rr = {}
check("/api/sms/rejected 返回台账结构", st == 200 and "items" in rr and "total" in rr,
      "http=%s keys=%s" % (st, list(rr)[:4]))

print("\n================ 汇总 ================")
print("  PASS %d / FAIL %d" % (len(OK), len(BAD)))
for n in BAD:
    print("  FAIL: %s" % n)
sys.exit(1 if BAD else 0)
