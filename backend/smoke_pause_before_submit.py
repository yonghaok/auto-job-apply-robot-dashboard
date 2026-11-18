"""端到端验证「**填完停在提交前**」（真 Playwright + 本地测试服务器）。

关键在于**证明机器人真的停住了**，而不是它自己说自己停住了：
mock 页面在「提交」被点击时用同步 XHR 把**是谁点的**回传到测试服务器，
跑完再读服务器记录，因此判定的是真实副作用。

  用例 C（有人点提交）：页面用定时器模拟「你本人在 6 秒后点了提交」
    → 期望 stage=投递成功（人工确认），且服务器记录的点击者必须是 `timer`（不是 `bot`）。
  用例 D（一直没人点提交）：跑 confirm_wait=6 秒
    → 期望 stage=已填表待确认（needs_human），且服务器记录里**一次提交都没有**。

运行：python smoke_pause_before_submit.py
"""
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 隔离：**不接管调试端口上的 Edge** —— 本机若正开着 CDP 实例（9222），自测会把标签页
# 开到用户的真实浏览器里；自测一律走项目私有的持久化会话（headless）。
os.environ["RESUME_CDP_ENDPOINT"] = "http://127.0.0.1:59999"
# 隔离持久化浏览器会话目录：apply_by_url 会按公司建 Chrome profile，
# 不隔离就会在工程里留下 browser_profiles/ 垃圾（与隔离 RESUME_STATE 同理）。
os.environ.setdefault(
    "RESUME_PROFILES",
    os.path.join(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp",
                 "resume_profiles_smoke"))

import filler  # noqa: E402

PORT = 8742
RECORD = {"who": [], "snap": []}

TPL = """<!doctype html><html><head><meta charset="utf-8"></head><body><div id="app"></div>
<script>
const PAUSE_MODE="__MODE__";
const app=document.getElementById('app');
let LOGGED=false, WHO='';

function report(kind, data){
  try { var x=new XMLHttpRequest();
        x.open('GET','/rec?kind='+kind+'&data='+encodeURIComponent(data), false);
        x.send(); } catch(e) {}
}

function listView(){
  app.innerHTML='<div class="row"><div class="pos">硕士-产品经理-专职（智慧旅游）00157676</div>'+
    '<button class="apply">投递</button></div>';
  document.querySelector('.apply').onclick=function(){ LOGGED ? formView() : loginView(); };
}

function loginView(){
  app.innerHTML='<input placeholder="请输入手机号">'+
   '<div class="van-checkbox"><div class="van-checkbox__icon van-checkbox__icon--round"></div>'+
   '<div class="van-checkbox__label">我已阅读并同意 《用户协议与隐私条款》</div></div>'+
   '<div id="sendBtn">获取验证码</div><input placeholder="请输入验证码" id="codeln">'+
   '<div id="loginBtn">登录</div><div id="rec">NOT_LOGGED_IN</div>';
  document.querySelector('.van-checkbox__icon').onclick=function(){
    this.classList.toggle('van-checkbox__icon--checked'); };
  document.getElementById('sendBtn').onclick=function(){ this.textContent='59s后重发'; };
  document.getElementById('loginBtn').onclick=function(){
    document.getElementById('rec').textContent='LOGGED_IN';
    if(document.getElementById('codeln').value){ LOGGED=true; listView(); } };
}

// 投递表单：会被 fill_page 填好；机器人应当**停在这里不提交**
function formView(){
  app.innerHTML='<div class="form">'+
    '<input placeholder="请输入姓名"><input placeholder="请输入手机号">'+
    '<input placeholder="请输入学校"><input placeholder="请输入专业">'+
    '<button id="submitBtn">提交</button><div id="msg"></div></div>';
  // 每秒把表单**当前 DOM 值**整份回传：这是「机器人真的把表填了」的硬证据
  // （不依赖 input 事件语义，也不依赖 profile 内容）
  setInterval(function(){
    var vs = Array.prototype.map.call(
      document.querySelectorAll('.form input'),
      function(e){ return (e.placeholder||'?') + '=' + (e.value||''); });
    if (vs.length) { report('snap', vs.join('|')); }
  }, 1000);
  document.getElementById('submitBtn').onclick=function(){
    report('who', WHO || 'bot');            // WHO 只有定时器会设成 'timer'
    document.getElementById('msg').textContent='投递成功';
  };
  if (PAUSE_MODE === 'timer') {
    setTimeout(function(){ WHO='timer'; document.getElementById('submitBtn').click(); }, 6000);
  }
}
listView();</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

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
        q = urllib.parse.parse_qs(p.query)
        if p.path == "/app":
            mode = (q.get("mode") or ["never"])[0]
            self._send(200, TPL.replace("__MODE__", mode))
            return
        if p.path == "/rec":
            kind = (q.get("kind") or [""])[0]
            data = (q.get("data") or [""])[0]
            if kind == "who":
                RECORD["who"].append(data)
            elif kind == "snap":
                RECORD["snap"].append(data)
            self._send(200, "ok")
            return
        self._send(404, "no")


RESULTS = []


def filled_ok(snaps):
    """最后一份快照里 4 个字段是否都非空（证明停住前表真的填了）。"""
    if not snaps:
        return False, "没收到任何快照"
    last = snaps[-1]
    parts = [p for p in last.split("|") if p]
    if len(parts) < 4:
        return False, "只看到 %d 个字段：%r" % (len(parts), last)
    empty = [p for p in parts if p.split("=", 1)[-1].strip() == ""]
    if empty:
        return False, "空字段：%r" % (empty,)
    return True, last


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print("   %-4s %s%s" % ("PASS" if cond else "FAIL", name,
                            ("  " + extra) if extra else ""))


def run(mode, pause, confirm_wait=0, timeout_s=120):
    RECORD["who"] = []
    RECORD["snap"] = []
    url = "http://127.0.0.1:%d/app?mode=%s" % (PORT, mode)
    logs = []
    t0 = time.time()
    r = filler.apply_by_url(url, title="", progress=lambda m: logs.append(m),
                            human_wait=5, code_wait=15, headless=True,
                            get_code=lambda: "8888",
                            pause_before_submit=pause, confirm_wait=confirm_wait)
    el = time.time() - t0
    if el > timeout_s:
        print("   ⚠️ 用时 %.1fs 超过预期 %ds" % (el, timeout_s))
    return r, logs, el


srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)

try:
    print("=== C) 停在提交前 + 页面定时器 6s 后模拟「你点了提交」 ===")
    rc, lc, elc = run("timer", True, confirm_wait=0)
    dc = rc.get("detail", "")
    print("   (%.1fs) stage=%s needs_human=%s" % (elc, rc["stage"], rc["needs_human"]))
    print("   服务器记录的点击者：%r" % (RECORD["who"],))
    for m in lc:
        print("   ·", m)
    check("C stage==投递成功（人工确认）", rc["stage"] == "投递成功（人工确认）", rc["stage"])
    check("C needs_human=False", rc["needs_human"] is False)
    check("C 日志写明「停在提交前」", "停在提交前" in dc)
    check("C 检测到你已提交", "检测到你已提交" in dc)
    check("C **提交是「人」点的，不是机器人**（who==['timer']）",
          RECORD["who"] == ["timer"], "who=%r" % (RECORD["who"],))
    check("C 机器人自己没点过提交（日志无「已点击「提交」」）", "已点击「提交」" not in dc)
    okC, whyC = filled_ok(RECORD["snap"])
    check("C 停住前表单确实填了（DOM 快照 4 字段全非空）", okC, whyC)

    print("\n=== D) 停在提交前 + 一直没人点提交（confirm_wait=6）===")
    rd, ld, eld = run("never", True, confirm_wait=6)
    dd = rd.get("detail", "")
    print("   (%.1fs) stage=%s needs_human=%s" % (eld, rd["stage"], rd["needs_human"]))
    print("   服务器记录的点击者：%r" % (RECORD["who"],))
    for m in ld:
        print("   ·", m)
    check("D stage==已填表待确认", rd["stage"] == "已填表待确认", rd["stage"])
    check("D needs_human=True（如实交人工）", rd["needs_human"] is True)
    check("D 日志点明「等待 6s 仍未确认提交」", "等待 6s 仍未确认提交" in dd, dd[-120:])
    check("D **一次提交都没发生**（who 为空）", RECORD["who"] == [], "who=%r" % (RECORD["who"],))
    okD, whyD = filled_ok(RECORD["snap"])
    check("D 表单已填好（不是空表就停）", okD, whyD)

    print("\n=== E) 对照组：pause_before_submit=False 时**应当自动提交** ===")
    re_, le, ele = run("never", False, confirm_wait=0)
    de = re_.get("detail", "")
    print("   (%.1fs) stage=%s" % (ele, re_["stage"]))
    print("   服务器记录的点击者：%r" % (RECORD["who"],))
    check("E stage==投递成功（自动提交）", re_["stage"] == "投递成功", re_["stage"])
    check("E 点击者是机器人（who==['bot']）", RECORD["who"] == ["bot"],
          "who=%r" % (RECORD["who"],))
    check("E 日志有「已点击「提交」」", "已点击「提交」" in de)
finally:
    srv.shutdown()

ok = sum(1 for _n, v in RESULTS if v)
print("\n================ 汇总 ================")
for n, v in RESULTS:
    if not v:
        print("  FAILED: %s" % n)
print("  %d/%d PASS" % (ok, len(RESULTS)))
sys.exit(0 if ok == len(RESULTS) else 1)
