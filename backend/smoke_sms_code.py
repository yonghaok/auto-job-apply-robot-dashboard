"""验证码转发链路的回归自测（针对「还是有验证码未转发」这个报障）。

覆盖：
  A. _extract_code_from_text —— 手机端 APP 没解析出 code 时，后端从正文兜底
  B. latest_code + 快照 —— 同秒到达 / 同发件人又来一条 / 旧码不能复用
  C. sms_snapshot / sms_info
  D. _merge_note —— 进度**追加**而不是覆盖（旧版正是因此丢失了排查线索）
  E. reset_all —— 复位所有已投递内容（保留取消选择与扫描缓存）
  F. 端到端：验证码迟到 ~6s → 仍能自动登录，且 note 里写明「等待 Ns」
  G. 页面上没有「获取验证码」按钮 → 必须**明说没点到**，不能静默走到等短信
  H. 等待期内一条新短信都没来 → 提示要点明「没发出去 / 被限流」而不是笼统说转发延迟
  I. 等待期内来了新短信但没解析出验证码 → 提示要区分开

不联网、不开浏览器访问真实站点（只用 file:// 假页面）。
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 隔离：**不接管调试端口上的 Edge** —— 本机若正开着 CDP 实例（9222），自测会把标签页
# 开到用户的真实浏览器里；自测一律走项目私有的持久化会话（headless）。
os.environ["RESUME_CDP_ENDPOINT"] = "http://127.0.0.1:59999"

TMP = tempfile.mkdtemp(prefix="resume-sms-")
os.environ["RESUME_STATE"] = os.path.join(TMP, "state.json")
os.environ["RESUME_SMS"] = os.path.join(TMP, "sms.json")
os.environ["RESUME_SMS_HIST"] = os.path.join(TMP, "sms_hist.json")

import server   # noqa: E402
# 隔离持久化浏览器会话目录：apply_by_url 会按公司建 Chrome profile，
# 不隔离就会在工程里留下 browser_profiles/ 垃圾（与隔离 RESUME_STATE 同理）。
os.environ.setdefault(
    "RESUME_PROFILES",
    os.path.join(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp",
                 "resume_profiles_smoke"))

import filler   # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print("   %-4s %s%s" % ("PASS" if cond else "FAIL", name,
                            ("  " + str(extra)) if extra else ""))


def reset_sms(hist=None, dic=None):
    server.SMS_HIST = list(hist or [])
    server.SMS = dict(dic or {})


# ============================ A. 正文兜底提码 ============================
print("=== A) _extract_code_from_text ===")
cases = [
    ("验证码：2182，请勿泄露，点击查看详情。", "2182"),
    ("您的验证码是 889900，5分钟内有效", "889900"),
    ("【税友】动态码 4321，请勿转发", "4321"),
    ("杭州大中小学秋假寒假放假时间安排出炉！查看详情>>", ""),
    ("", ""),
    ("订单 123456789012 已发货", ""),          # 无关键词，不能乱抓
]
for text, want in cases:
    got = server._extract_code_from_text(text)
    check("提码 %r -> %r" % (text[:18], want), got == want, "got=%r" % got)

# ============================ B. latest_code + 快照 ============================
print("\n=== B) latest_code / 快照 ===")
reset_sms(hist=[
    {"phone": "106900", "code": "1111", "text": "验证码：1111", "received": "2026-09-21 10:00:00"},
])
check("旧码（早于 after）不复用", server.latest_code(after="2026-09-21 10:00:05") == "")
check("旧码在有 after 之前可用", server.latest_code(after="") == "1111")

# 同秒到达：received == after，但内容与快照不同 → 必须认
snap_before = {"106900": ("2026-09-21 10:00:05", "")}
reset_sms(hist=[
    {"phone": "106900", "code": "2222", "text": "验证码：2222", "received": "2026-09-21 10:00:05"},
])
check("同秒到达也要认（快照变了）",
      server.latest_code(after="2026-09-21 10:00:05", snapshot=snap_before) == "2222")
snap_same = {"106900": ("2026-09-21 10:00:05", "2222")}
check("同秒但内容与快照一致 → 视为旧码",
      server.latest_code(after="2026-09-21 10:00:05", snapshot=snap_same) == "")

# 同一发件人又发一条：内容变了，但**时间早于请求时刻** → 硬下限优先，不许认。
# （旧实现允许「内容变了」绕过 after；实测就是把上一条自检短信当新码填了进去。）
reset_sms(hist=[
    {"phone": "106900", "code": "3333", "text": "验证码：3333", "received": "2026-09-21 09:59:00"},
])
check("内容变了但时间早于请求时刻 → 不认（硬下限不可被绕过）",
      server.latest_code(after="2026-09-21 10:00:00",
                         snapshot={"106900": ("2026-09-21 09:59:00", "1111")}) == "")
check("内容变了且时间不早于请求时刻 → 认（同一发件人又来一条）",
      server.latest_code(after="2026-09-21 09:00:00",
                         snapshot={"106900": ("2026-09-21 09:00:00", "1111")}) == "3333")

# 取最新：多条命中时给 received 最大的
reset_sms(hist=[
    {"phone": "a", "code": "4444", "text": "", "received": "2026-09-21 10:00:01"},
    {"phone": "b", "code": "5555", "text": "", "received": "2026-09-21 10:00:09"},
])
check("多命中取最新", server.latest_code(after="2026-09-21 10:00:00") == "5555")
check("无 code 的记录被跳过",
      server.latest_code(after="", snapshot={}) == "5555")

# 历史为空 → 退回 sms.json 字典
reset_sms(hist=[], dic={"106900": {"code": "6666", "text": "", "received": "2026-09-21 10:10:00"}})
check("历史为空时退回 sms.json 字典",
      server.latest_code(after="2026-09-21 10:00:00") == "6666")

# ============== B2. 真实故障复现：TEST 自检短信不能被当新码 ==============
# 现场（2026-09-21 23:17–23:19，手机实测数据）：
#   手机连发 3 条自检短信（发件人都是 TEST，各带一个码），用户之后才点「获取验证码」，
#   机器人却把 TEST 的码填了进去 —— 因为同一个发件人有**多条历史**时，旧实现拿
#   「值 != 快照」判新到达，把更早的那几条也判成「新的」，于是绕过了 after 过滤。
print("\n=== B2) 自检短信（TEST）被当新码的故障复现 ===")
reset_sms(hist=[
    {"phone": "TEST", "code": "802776", "text": "【链路自检】验证码 802776", "received": "2026-09-21 23:17:32"},
    {"phone": "TEST", "code": "477325", "text": "【链路自检】验证码 477325", "received": "2026-09-21 23:17:52"},
    {"phone": "TEST", "code": "999831", "text": "【链路自检】验证码 999831", "received": "2026-09-21 23:18:22"},
])
snap_task = server.sms_snapshot()
check("快照里同一发件人只留最新一条（这正是误判的来源）",
      snap_task.get("TEST") == ("2026-09-21 23:18:22", "999831"), str(snap_task))
check("窗口起点晚于全部自检短信 → 一条都不取（硬下限）",
      server.latest_code(after="2026-09-21 23:19:00", snapshot=snap_task) == "")
# 用户点完「获取验证码」后真码才到（不是同一发件人 —— 同一号码也会给多家公司发码）
reset_sms(hist=[
    {"phone": "TEST", "code": "999831", "text": "【链路自检】验证码 999831", "received": "2026-09-21 23:18:22"},
    {"phone": "106900801534293", "code": "4163", "text": "验证码：4163，请勿泄露。", "received": "2026-09-21 23:19:03"},
])
check("新码到了就取新码",
      server.latest_code(after="2026-09-21 23:19:00", snapshot=snap_task) == "4163")
check("公司名不命中要回退，但绝不能回退到早于请求时刻的自检短信",
      server.latest_code(after="2026-09-21 23:19:00", snapshot=snap_task,
                         match=server.sms_matcher("思特奇")) == "4163")
reset_sms(hist=[{"phone": "x", "code": "7777", "text": "", "received": ""}])
check("没有到达时间的记录不予取用（无法校验时间规则）",
      server.latest_code(after="") == "")

# ============================ C. snapshot / info ============================
print("\n=== C) sms_snapshot / sms_info ===")
reset_sms(hist=[
    {"phone": "a", "code": "1", "text": "", "received": "2026-09-21 10:00:00"},
    {"phone": "b", "code": "", "text": "营销", "received": "2026-09-21 10:01:00"},
])
sn = server.sms_snapshot()
check("快照含全部记录（含无码的）", set(sn.keys()) == {"a", "b"}, str(sn))
inf = server.sms_info()
check("sms_info.total 正确", inf["total"] == 2, str(inf))
check("sms_info.newest 取最新时间", inf["newest"] == "2026-09-21 10:01:00", str(inf))

# ============================ D. _merge_note ============================
print("\n=== D) _merge_note（进度追加） ===")
n = server._merge_note("已打开岗位页", "已填手机号")
check("不同步骤追加", n == "已打开岗位页；已填手机号", n)
n2 = server._merge_note(n, "等待短信转发到达（已等待 20s / 最长 300s）…")
n3 = server._merge_note(n2, "等待短信转发到达（已等待 40s / 最长 300s）…")
check("心跳只保留最新一条（不刷屏）", n3.count("等待短信转发到达") == 1, n3)
check("心跳之外的轨迹都还在",
      n3.startswith("已打开岗位页；已填手机号；") and "已等待 40s" in n3, n3)
big = "；".join("步骤%d" % i for i in range(600))
capped = server._merge_note(big, "最后一步")
check("超长 note 会截断但保头保尾",
      len(capped) <= 2000 and "步骤0" in capped and "最后一步" in capped, "len=%d" % len(capped))

# ============================ E. reset_all ============================
print("\n=== E) reset_all ===")
server.CANDIDATES = [
    {"id": "c1", "company": "A", "title": "t1", "status": "已登录", "note": "n1"},
    {"id": "c2", "company": "B", "title": "t2", "status": "需人工验证", "note": "n2"},
]
server.CUSTOM = [
    {"id": "c1", "company": "A", "title": "t1", "status": "已登录", "note": "n1"},
    {"id": "c3", "company": "C", "title": "t3", "status": "已填表", "note": "n3"},
]
server.STATE = {
    "c1": {"status": "已登录", "note": "n1", "updated": "x"},
    "c2": {"status": "需人工验证", "note": "n2", "updated": "x"},
    "c3": {"status": "已填表", "note": "n3", "updated": "x"},
    "_hidden_pos": ["ppp"],
    "_hidden_co": ["CO"],
    "_scans": {"u": {"positions": []}},
    "_custom": server.CUSTOM,
    "_pos_state": {"c1": {"status": "已登录", "note": "n1"}},
}
server.POS_STATE = {"c1": {"status": "已登录", "note": "n1"}}
server.HIDDEN_POS = ["ppp"]
server.HIDDEN_CO = ["CO"]
server.SCANS = {"u": {"positions": []}}

r = server.reset_all()
check("reset_all 返回 ok", r.get("ok") is True, str(r))
leftover = [k for k in server.STATE if not k.startswith("_")]
check("顶层逐 id 状态全清", leftover == [], str(leftover))
check("POS_STATE 清空", server.POS_STATE == {})
check("候选状态全部回 pending",
      all(c["status"] == "pending" for c in server.CANDIDATES))
check("自定义候选回 pending",
      all(c["status"] == "pending" for c in server.CUSTOM))
check("取消选择被保留", server.STATE["_hidden_pos"] == ["ppp"]
      and server.STATE["_hidden_co"] == ["CO"])
check("扫描缓存被保留", server.STATE["_scans"] == {"u": {"positions": []}})
onfile = json.load(open(os.environ["RESUME_STATE"], encoding="utf-8"))
check("落盘文件里也没有逐 id 状态",
      [k for k in onfile if not k.startswith("_")] == [], str(list(onfile.keys())))

# ============================ F~I. 端到端（file:// 假页面） ============================
POS_ROW = "硕士-产品经理-专职（智慧旅游）00157676"

TPL = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body><div id="app"></div><script>
const SHOW_CD=__CD__; const HAS_BTN=__HAS_BTN__; const SEND_OK=__SEND_OK__;
const app=document.getElementById('app');
function listView(){
  app.innerHTML='<div class="row"><div class="pos">""" + POS_ROW + """</div><button class="apply">投递</button></div>';
  document.querySelector('.apply').onclick=loginView;
}
function loginView(){
  let h='<input class="van-field__control" placeholder="请输入手机号">'+
   '<div class="van-checkbox"><div class="van-checkbox__icon van-checkbox__icon--round"></div>'+
   '<div class="van-checkbox__label">我已阅读并同意 《用户协议与隐私条款》</div></div>';
  if(HAS_BTN){ h+='<div id="sendBtn">获取验证码</div>'; }
  h+='<input placeholder="请输入验证码" id="codeln"><div id="loginBtn">登录</div><div id="rec">NOT_LOGGED_IN</div>';
  app.innerHTML=h;
  document.querySelector('.van-checkbox__icon').onclick=function(){this.classList.toggle('van-checkbox__icon--checked');};
  if(HAS_BTN){ document.getElementById('sendBtn').onclick=function(){ if(SEND_OK){ this.textContent='59s后重发'; } }; }
  document.getElementById('loginBtn').onclick=function(){
    document.getElementById('rec').textContent='LOGGED_IN code='+document.getElementById('codeln').value;
    if(document.getElementById('codeln').value){ formView(); }};
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


def page(has_btn=True, cd=True, send_ok=True, name="p"):
    p = os.path.join(TMP, "%s_%s_%s.html" % (name, int(has_btn), int(cd)))
    open(p, "w", encoding="utf-8").write(
        TPL.replace("__CD__", "true" if cd else "false")
           .replace("__HAS_BTN__", "true" if has_btn else "false")
           .replace("__SEND_OK__", "true" if send_ok else "false"))
    return "file://" + p


def run(pg, gc, code_wait=20, sms_info=None, human_wait=8):
    logs = []
    r = filler.apply_by_url(pg, title="【正文命中·产品经理】",
                            progress=lambda m: logs.append(m), human_wait=human_wait,
                            code_wait=code_wait, headless=True, get_code=gc,
                            sms_info=sms_info)
    return r, logs


print("\n=== F) 验证码迟到 ~6s → 仍要自动登录，并写明等待时长 ===")
calls = {"n": 0}


def late():
    calls["n"] += 1
    return "" if calls["n"] <= 3 else "8888"


t0 = time.time()
rf, lf = run(page(name="f"), late, code_wait=40)
print("   (%.1fs) %s" % (time.time() - t0, [x for x in lf if "验证码" in x]))
check("F 最终登录后继续填表并投递成功", rf["stage"] == "投递成功", rf["stage"])
check("F 日志里写明「等了 Ns」",
      any(("等待" in x and "s）" in x) for x in lf),
      [x for x in lf if "取到验证码" in x])
check("F 轨迹保留（能看到前面点过获取验证码）",
      any("已点击：获取验证码" in x for x in lf), str(lf[:4]))

print("\n=== G) 页面没有「获取验证码」按钮 → 必须明说没点到 ===")
rg, lg = run(page(has_btn=False, name="g"), lambda: "", code_wait=6)
check("G 明确报告没点到按钮",
      any("没点到「获取验证码」按钮" in x for x in lg),
      [x for x in lg if "获取验证码" in x])
check("G 失败提示指向「短信很可能没发出去」",
      any("没点到" in x and "没发出去" in x for x in lg), str(lg[-1])[:120])

print("\n=== H) 等待期内一条新短信都没来 → 不要笼统说转发延迟 ===")
same = {"total": 5, "newest": "2026-09-21 10:00:00"}
rh, lh = run(page(name="h"), lambda: "", code_wait=6, sms_info=lambda: same)
check("H 提示「一条新短信都没到」",
      any("一条新短信都没到" in x for x in lh), str(lh[-1])[:130])
check("H 不再出现旧的模糊说法「可能转发延迟或手机端转发 APP 离线」",
      not any("可能转发延迟或手机端转发 APP 离线" in x for x in lh), str(lh[-1])[:130])

print("\n=== I) 来了新短信但没解析出验证码 → 要区分开 ===")
box = {"total": 5}


def info_grow():
    return {"total": box["total"], "newest": "2026-09-21 10:00:00"}


def bump():
    box["total"] = 7
    return ""


ri, li = run(page(name="i"), bump, code_wait=6, sms_info=info_grow)
check("I 提示「收到 N 条新短信但没解析出验证码」",
      any("收到 2 条新短信，但都没解析出验证码" in x for x in li), str(li[-1])[:130])

ok = sum(1 for _n, v in RESULTS if v)
print("\n================ 汇总 ================")
for n, v in RESULTS:
    if not v:
        print("  FAILED: %s" % n)
print("  %d/%d PASS" % (ok, len(RESULTS)))
sys.exit(0 if ok == len(RESULTS) else 1)
