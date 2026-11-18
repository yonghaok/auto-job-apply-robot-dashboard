"""断点4 端到端冒烟：本地 computer-use（Playwright）真实驱动 Chrome 填表。

验证三件事：
  1. 表单字段真的被写入了（页面用同步 XHR 把每个 value 回传到本测试服务器，
     因此验的是**真实 DOM 里的值**，不是 filler 自己说的话）。
  2. 简历 PDF 上传成功。
  3. **没有触发提交**（form action=/submit，服务器统计提交次数必须为 0）。

运行：python backend/smoke_fill_e2e.py
"""
import json
import os
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import filler  # noqa: E402

PORT = 8731
RECORD = {"values": {}, "submit": 0}

HTML = """<!doctype html><html><head><meta charset="utf-8"><title>ATS 测试表单</title></head>
<body>
<form id="f" action="/submit" method="post">
  <p><label for="name">姓名</label><input id="name" name="name" type="text"></p>
  <p><label for="phone">手机号</label><input id="phone" name="phone" type="tel"></p>
  <p><label for="email">电子邮箱</label><input id="email" name="email" type="email"></p>
  <p><label for="school">毕业院校</label><input id="school" name="school" type="text"></p>
  <p><label for="major">所学专业</label><input id="major" name="major" type="text"></p>
  <p><label for="degree">最高学历</label>
     <select id="degree" name="degree">
       <option>请选择</option><option>博士</option><option>硕士</option>
       <option>本科</option><option>大专</option>
     </select></p>
  <p><label for="intro">自我评价</label><textarea id="intro" name="intro"></textarea></p>
  <p><span>性别</span>
     <label for="g1">男</label><input type="radio" id="g1" name="gender" value="男">
     <label for="g2">女</label><input type="radio" id="g2" name="gender" value="女"></p>
  <p><label for="resume">上传简历</label><input type="file" id="resume" name="resume"></p>
  <button type="submit" id="btn">提交申请</button>
</form>
<script>
function report(nm, val){
  try { var x = new XMLHttpRequest();
        x.open('GET','/report?name='+encodeURIComponent(nm)+'&value='+encodeURIComponent(val), false);
        x.send(); } catch(e) {}
}
document.querySelectorAll('input,textarea,select').forEach(function(e){
  if (e.type === 'file') return;
  function h(){ report(e.name || e.id, e.value); }
  e.addEventListener('input', h);
  e.addEventListener('change', h);
});
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        if p.path == "/form":
            self._send(200, HTML)
        elif p.path == "/report":
            q = urllib.parse.parse_qs(p.query)
            RECORD["values"][(q.get("name") or [""])[0]] = (q.get("value") or [""])[0]
            self._send(200, "ok", "text/plain")
        else:
            self._send(404, "not found")

    def do_POST(self):
        if self.path.startswith("/submit"):
            RECORD["submit"] += 1
            self._send(200, "ok", "text/plain")
        else:
            self._send(404, "not found")

    def log_message(self, *a):
        pass


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="rb_smoke_")
    pdf = os.path.join(tmp, "resume.pdf")
    with open(pdf, "wb") as f:
        f.write(b"%PDF-1.4 dummy resume")

    profile = json.load(open(os.path.join(HERE, "profile.json"), encoding="utf-8"))
    profile["gender"] = "男"           # 用干净值，避免占位符「男/女」干扰
    profile["resume_pdf"] = pdf

    url = "http://127.0.0.1:%d/form" % PORT
    try:
        summary = filler.fill_by_url(url, profile)
    except Exception as e:
        print("FAILED: filler 抛异常 %s: %s" % (type(e).__name__, e))
        srv.shutdown()
        return 1

    print("filler 摘要：", summary)
    print("页面实际回传的 DOM 值：")
    for k, v in RECORD["values"].items():
        print("   %-8s = %r" % (k, v))

    # 期望值**取自同一份 profile**，而不是写死占位符 —— 否则一旦 profile.json
    # 从占位数据换成真实数据，这个测试就会整片变红（曾经发生过）。
    # 这里验的是「页面上真实 DOM 的值 == 我们喂进去的 profile 值」，与数据无关。
    expect = {
        "name": profile["name"],
        "phone": profile["phone"],
        "email": profile["email"],
        "school": profile["education"]["school"],
        "major": profile["education"]["major"],
        "degree": profile["education"]["degree"],
        "intro": profile["self_intro"],
        "gender": "男",
    }
    fails = []
    for k, want in expect.items():
        got = RECORD["values"].get(k)
        if got != want:
            fails.append("字段 %s：期望 %r，实际 %r" % (k, want, got))
    if RECORD["submit"] != 0:
        fails.append("检测到提交 %d 次（断点5 必须人工提交）" % RECORD["submit"])
    if "简历上传=True" not in summary:
        fails.append("简历 PDF 未上传成功")

    print("\n=== 判定 ===")
    if fails:
        for f in fails:
            print("  FAILED:", f)
        srv.shutdown()
        return 1
    print("  PASSED：8 项字段写入真实 DOM 且值正确；简历已上传；提交次数=0（停在提交前）")
    srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
