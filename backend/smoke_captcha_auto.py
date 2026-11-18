"""端到端验证：投递流程中「人机验证先自动过（CV），失败才转人工」。

用合成滑块图（已知缺口位置）构造真实可拖动的验证控件：
  A 路径：拖对了 -> 控件消失 -> 继续取码并自动登录（不应出现「转人工」）
  B 路径：怎么拖都不对 -> 自动尝试 N 次 -> 才「转人工」
"""
import base64
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 隔离：**不接管调试端口上的 Edge** —— 本机若正开着 CDP 实例（9222），自测会把标签页
# 开到用户的真实浏览器里；自测一律走项目私有的持久化会话（headless）。
os.environ["RESUME_CDP_ENDPOINT"] = "http://127.0.0.1:59999"

import numpy as np  # noqa: E402

import captcha  # noqa: E402
# 隔离持久化浏览器会话目录：apply_by_url 会按公司建 Chrome profile，
# 不隔离就会在工程里留下 browser_profiles/ 垃圾（与隔离 RESUME_STATE 同理）。
os.environ.setdefault(
    "RESUME_PROFILES",
    os.path.join(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp",
                 "resume_profiles_smoke"))

import filler  # noqa: E402

cv2 = captcha._load_cv2()
assert cv2 is not None, "cv2 不可用"

rng = np.random.default_rng(11)
W, H, GX, GY, GS = 360, 200, 210, 70, 60

base = cv2.GaussianBlur(rng.integers(60, 200, size=(H, W, 3), dtype=np.uint8), (15, 15), 0)
cv2.rectangle(base, (GX, GY), (GX + GS, GY + GS), (35, 35, 35), -1)
cv2.rectangle(base, (GX, GY), (GX + GS, GY + GS), (210, 210, 210), 2)
_ok, _buf = cv2.imencode(".png", base)
BG_B64 = base64.b64encode(_buf.tobytes()).decode("ascii")
TRUE_CENTER = GX + GS / 2.0

TPL = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body><div id="app"></div><script>
const BG="data:image/png;base64,__BG__";
const TRUE_CENTER=__CENTER__, IW=__IW__, ALWAYS_FAIL=__FAIL__;
const app=document.getElementById('app');
function listView(){
  app.innerHTML='<div class="row"><div class="pos">硕士-产品经理-专职（智慧旅游）00157676</div><button class="apply">投递</button></div>';
  document.querySelector('.apply').onclick=loginView;
}
function showCaptcha(){
  document.getElementById('cap').innerHTML=
    '<div class="verify-box" id="vb"><div>请完成安全验证</div>'+
    '<img id="bgimg" src="'+BG+'" style="width:300px;height:auto;display:block">'+
    '<div id="track" style="position:relative;height:20px;background:#eee;width:300px">'+
    '<div id="hnd" style="position:absolute;left:0;top:0;width:40px;height:20px;background:#3b82f6"></div>'+
    '</div></div>';
  const hnd=document.getElementById('hnd');
  let downX=null;
  hnd.onmousedown=function(e){downX=e.clientX; e.preventDefault();};
  document.onmousemove=function(e){ if(downX!==null){ hnd.style.left=Math.max(0,e.clientX-downX)+'px'; } };
  document.onmouseup=function(e){
    if(downX===null) return;
    const dx=e.clientX-downX; downX=null;
    const bg=document.getElementById('bgimg').getBoundingClientRect();
    // 正确位移 = 「拼图块起点 → 缺口中心」（2026-09-23 修复后的口径）。
    // 旧版这里减了半个手柄宽（-20），那是按「手柄中心」算位移的老语义，早就改了。
    const expect=(TRUE_CENTER/IW)*bg.width;
    const ok = (!ALWAYS_FAIL) && Math.abs(dx-expect)<=8;
    if(ok){ document.getElementById('cap').innerHTML='';
            document.getElementById('sendBtn').textContent='59s后重发'; }
    else { hnd.style.left='0px'; }
  };
}
function loginView(){
  app.innerHTML='<input class="van-field__control" placeholder="请输入手机号">'+
   '<div class="van-checkbox"><div class="van-checkbox__icon van-checkbox__icon--round"></div>'+
   '<div class="van-checkbox__label">我已阅读并同意 《用户协议与隐私条款》</div></div>'+
   '<div id="sendBtn">获取验证码</div><input placeholder="请输入验证码" id="codeln">'+
   '<div id="loginBtn">登录</div><div id="cap"></div><div id="rec">NOT_LOGGED_IN</div>';
  document.querySelector('.van-checkbox__icon').onclick=function(){this.classList.toggle('van-checkbox__icon--checked');};
  document.getElementById('sendBtn').onclick=function(){ showCaptcha(); };
  document.getElementById('loginBtn').onclick=function(){
    document.getElementById('rec').textContent='LOGGED_IN code='+document.getElementById('codeln').value;
    if(document.getElementById('codeln').value) formView(); };
}
// 登录成功后出现投递表单（真实站点是「回列表再点投递」，见 smoke_apply_regression.py）
function formView(){
  app.innerHTML='<input placeholder="请输入姓名"><input placeholder="请输入手机号">'+
    '<input placeholder="请输入学校"><input placeholder="请输入专业">'+
    '<button id="submitBtn">提交</button><div id="msg"></div>';
  document.getElementById('submitBtn').onclick=function(){
    document.getElementById('msg').textContent='投递成功'; };
}
listView();</script></body></html>"""

d = tempfile.mkdtemp()


def make_page(always_fail):
    p = os.path.join(d, "cap_%s.html" % always_fail)
    html = (TPL.replace("__BG__", BG_B64)
               .replace("__CENTER__", str(TRUE_CENTER))
               .replace("__IW__", str(W))
               .replace("__FAIL__", "true" if always_fail else "false"))
    open(p, "w", encoding="utf-8").write(html)
    return p


def run(name, page, human_wait=8, code_wait=20, mode="auto"):
    logs = []
    calls = {"n": 0}

    def gc():
        calls["n"] += 1
        return "8888" if calls["n"] >= 2 else ""

    prev_mode = captcha.MODE
    captcha.MODE = mode
    t0 = time.time()
    try:
        r = filler.apply_by_url("file://" + page, title="【正文命中·AI产品/应用产品】",
                                progress=lambda m: logs.append(m), human_wait=human_wait,
                                code_wait=code_wait, headless=True, get_code=gc)
    finally:
        captcha.MODE = prev_mode
    el = time.time() - t0
    print("--- %s [CAPTCHA_SOLVER=%s] (%.1fs) ---" % (name, mode, el))
    for m in logs:
        print("   ·", m)
    print("   => needs_human:", r["needs_human"], "| stage:", r["stage"])
    return r, logs, el


# A：可自动过（auto → CV）
a, alogs, ael = run("A 滑块可自动过（auto/CV）", make_page(False))
# A2：强制走 AI 视觉（证明 AI 通道真的接到了拖动逻辑里）
a2, a2logs, a2el = run("A2 滑块强制走 AI 视觉", make_page(False), mode="ai_vision")
# B：拖不对 → 3 次后转人工
b, blogs, bel = run("B 滑块拖不对（应 3 次后转人工）", make_page(True), human_wait=6)

print()
ad, a2d, bd = a["detail"], a2["detail"], b["detail"]
print("A  stage==投递成功          :", a["stage"] == "投递成功")
print("A  有『自动过人机验证成功』 :", "自动过人机验证成功" in ad)
print("A  无『转人工』             :", "转人工" not in ad)
print("A  记录了 via=/method=      :", "via=" in ad and "method=" in ad)
print("A  登录后继续填表+投递成功  :", "已进入投递表单" in ad and "投递成功 ✓" in ad)
print("A2 stage==投递成功 (AI 通道):", a2["stage"] == "投递成功", "| %.1fs" % a2el)
print("A2 经由 ai_vision           :", "ai_vision" in a2d)
print("A2 无『转人工』             :", "转人工" not in a2d)
print("B  stage==人机验证          :", b["stage"] == "人机验证" and b["needs_human"] is True)
n_att = bd.count("尝试自动过人机验证（第")
print("B  自动尝试次数 == 3        :", n_att == 3, "| 实际", n_att)
print("B  有『转人工』             :", "转人工" in bd)
