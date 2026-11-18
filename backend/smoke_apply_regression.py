"""投递流程回归：短信延迟不卡死 / 验证码始终未到要中断 / **登录后继续填投递信息**。

（人机验证自动过 → 见 smoke_captcha_auto.py）

本文件的页面刻意做成**思特奇 myyule 那种三段式 SPA**：
  列表页 --点投递--> 登录门禁（#/login）--登录成功--> **回到列表页** --再点投递--> 投递表单
旧版 apply_by_url 到「自动登录完成」就 return，正是用户报的
「登录成功以后未见开启自动填写投递信息流程」；这里会断言 stage 是「投递成功」而不是「已登录」。
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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

TPL = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body><div id="app"></div><script>
const SHOW_CD=__CD__;
let LOGGED=false;
const app=document.getElementById('app');

function listView(){
  app.innerHTML='<div class="row"><div class="pos">硕士-产品经理-专职（智慧旅游）00157676</div>'+
    '<button class="apply">投递</button></div>';
  document.querySelector('.apply').onclick=function(){
    // 未登录 → 被登录门禁拦下；已登录 → 才真的打开投递表单
    if(LOGGED){ formView(); } else { loginView(); }
  };
}

function loginView(){
  app.innerHTML='<input class="van-field__control" placeholder="请输入手机号">'+
   '<div class="van-checkbox"><div class="van-checkbox__icon van-checkbox__icon--round"></div>'+
   '<div class="van-checkbox__label">我已阅读并同意 《用户协议与隐私条款》</div></div>'+
   '<div id="sendBtn">获取验证码</div><input placeholder="请输入验证码" id="codeln">'+
   '<div id="loginBtn">登录</div><div id="rec">NOT_LOGGED_IN</div>';
  document.querySelector('.van-checkbox__icon').onclick=function(){this.classList.toggle('van-checkbox__icon--checked');};
  document.getElementById('sendBtn').onclick=function(){ if(SHOW_CD){ this.textContent='59s后重发'; } };
  document.getElementById('loginBtn').onclick=function(){
    const code=document.getElementById('codeln').value;
    document.getElementById('rec').textContent='LOGGED_IN code='+code;
    if(code){ LOGGED=true; listView(); }   // 登录成功 → 退回岗位列表（myyule 的真实行为）
  };
}

// 投递表单：会被 fill_page 识别并填写
function formView(){
  app.innerHTML='<div class="form">'+
    '<input placeholder="请输入姓名"><input placeholder="请输入手机号">'+
    '<input placeholder="请输入学校"><input placeholder="请输入专业">'+
    '<input type="file" id="resumeFile">'+
    '<button id="submitBtn">提交</button><div id="msg"></div></div>';
  document.getElementById('submitBtn').onclick=function(){
    document.getElementById('msg').textContent='投递成功';
  };
}
listView();</script></body></html>"""

d = tempfile.mkdtemp()


def page(show_cd):
    p = os.path.join(d, "p_%s.html" % show_cd)
    open(p, "w", encoding="utf-8").write(TPL.replace("__CD__", "true" if show_cd else "false"))
    return p


def run(name, pg, gc, code_wait=20, human_wait=10):
    logs = []
    t0 = time.time()
    r = filler.apply_by_url("file://" + pg, title="【正文命中·AI产品/应用产品】",
                            progress=lambda m: logs.append(m), human_wait=human_wait,
                            code_wait=code_wait, headless=True, get_code=gc)
    el = time.time() - t0
    print("--- %s (%.1fs) ---" % (name, el))
    for m in logs:
        print("   ·", m)
    print("   => needs_human:", r["needs_human"], "| stage:", r["stage"])
    return r, el, logs


def gc_factory(after_calls, code="8888"):
    st = {"n": 0}

    def gc():
        st["n"] += 1
        return code if st["n"] >= after_calls else ""
    return gc


# A) 无倒计时（启发式失效）+ 短信延迟 6s 才到 → 不应卡死，且**登录后要接着填表并投出去**
a, ael, alogs = run("A 短信延迟且无验证控件（应一路走到投递成功）",
                    page(False), gc_factory(4), code_wait=30)
# B) 验证码始终不到 → 中断并提示
b, bel, blogs = run("B 验证码始终未到（应中断）", page(True), gc_factory(999), code_wait=6)

aj = "；".join(alogs)
bj = "；".join(blogs)

print()
print("A 最终 stage==投递成功    :", a["stage"] == "投递成功", "| %.1fs" % ael)
print("A 未卡死(<60s)            :", ael < 60)
print("A 无『转人工』            :", "转人工" not in a["detail"])
print("A 登录后确实开了填表流程  :", "已进入投递表单，开始自动填写投递信息" in aj)
print("A 填表有实际反馈          :", "填写结果：" in aj and "已填" in aj)
print("A 提交有成功证据          :", "投递成功 ✓" in aj)
print("A 状态不是旧的无脑已登录  :", a["stage"] != "已登录")
print("B 验证码未到 + 需人工     :", b["stage"] == "验证码未到" and b["needs_human"] is True)
# 末条提示必须**点明原因**：是「没点到发送按钮 / 没发出去」还是「一条新短信都没到」，
# 不能再用「可能转发延迟或手机端转发 APP 离线」这种把责任全推给转发的模糊说法。
print("B 末条提示点明原因        :",
      ("新短信都没到" in bj) or ("没发出去" in bj) or ("没解析出验证码" in bj))
print("B 不再笼统怪转发          :", "可能转发延迟或手机端转发 APP 离线" not in bj)
print("B 未误报已投递            :", b["stage"] != "投递成功")
