"""简历机器人后端（MVP / P0+P1+P2 入口发现）。

零新依赖：仅用标准库 + 已装好的 openpyxl / playwright。
绑定 0.0.0.0 以便同网段 Android 短信转发（断点1）能访问 /sms。

端点：
  看板（主界面：一公司一栏 · 多岗位多 tab）
    GET  /api/columns?mode=strict|loose    公司栏 + 岗位（含各口径计数、扫描/投递进度）
    POST /api/hide              {id} 取消岗位 | {company} 取消整栏公司（持久化）
    POST /api/unhide            清空全部「取消」记录
    POST /api/reset-all         复位**所有已投递内容**（状态全部回到 pending）
    POST /api/scan              {limit,force} 后台扫描各公司链接（3 并发，结果缓存）
    GET  /api/scan/status       扫描进度 + 缓存汇总
    POST /api/scan/stop         停止扫描
    POST /api/apply-all         {ids?,mode?,concurrency?} 一键投递（**单窗口严格串行**；
                                concurrency 仅为兼容旧客户端，取值被忽略）
    GET  /api/apply-all/status  批量投递进度
    POST /api/preselect/<id>/apply        单岗位投递
    POST /api/preselect/<id>/focus        「进入窗口」把该岗位浏览器窗口置前
    POST /api/preselect/<id>/human-done   「确认已投递」人工登记为终态「已投递」
  候选 / 预选（旧接口，保留）
    GET  /api/candidates?status=&city=&family=
    GET  /api/preselect
    POST /api/preselect/<id>/refind
    POST /api/preselect/<id>/decide  {decision}
    POST /api/preselect/add  {company,title,url}
    POST /api/preselect/<id>/fill
    GET  /api/preselect/<id>/mapping
    POST /api/candidates/<id>/confirm  /  /reject  /  /reset
      （/reset 把该候选的状态清回 pending，供测试收尾/人工复位用）
  其它
    GET  /api/stats  /api/profile  /api/captcha/status  /api/fill-mode
    SMS 转发（手机端 APP → PC）——**入口刻意宽容**：
      POST /sms             推荐；JSON / form-urlencoded / 纯文本 都收；字段名走
                            SMS_FIELD_ALIASES（phone|sender|from… / code|verify_code…
                            / text|msg|content|body…）；chunked、带 BOM、GBK 也都收
      GET  /sms?phone=&code= 有些 APP 只会用 GET 上报；不带字段时＝连通性自检（不写库）
      OPTIONS /sms          CORS 预检（WebView 型 APP 必须）
      GET  /api/sms         按发件人存的最新一条   GET /api/sms/<phone>  指定发件人
      GET  /api/sms/history 追加式历史（回看「到底来没来/来了什么」看这个）
      GET  /api/sms/rejected 被拒上报台账（含原始报文；旧版拒收不留痕）
      GET  /api/access      访问日志尾巴（含客户端 IP → 查「手机到底有没有连上来」）
    POST /api/reload
    POST /api/parse-resume   {text} resume PDF text -> LLM structured extraction of 4 JSON arrays
"""
import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
import datetime
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import candidates
import state as statemod
import entry_finder
import filler
import captcha
import positions as posmod
import llm_client

PORT = int(os.environ.get("RESUME_PORT", "8000"))
XLSX = candidates.DEFAULT_XLSX
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND = os.path.join(BASE, "frontend", "index.html")
PROFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profile.json")
SMS_PATH = os.environ.get(
    "RESUME_SMS",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sms.json"),
)
# 短信历史（**append-only**）。sms.json 是「按发件人存最新一条」的字典，
# 同一发件人再来一条就把上一条**覆盖**掉——实测同一个号码（106900801534293）
# 会同时给多家公司发验证码，一旦被后来的营销短信覆盖，未取走的验证码就永久丢了。
# 所以另存一份历史列表，取码只看历史列表。
SMS_HIST_PATH = os.environ.get(
    "RESUME_SMS_HIST",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sms_hist.json"),
)
SMS_HIST_MAX = 80

# ===== 短信入口的宽容度配置（2026-09-21 修复「收短信慢 / 短信极易丢失」）=====
# 事故：手机端转发 APP 的报文形态五花八门，旧 `_body()` 只做
# `json.loads(raw.decode("utf-8"))`，于是 form-urlencoded / chunked / 带 BOM / GBK /
# 字段名不是 phone-code-text 的上报**一律 400 且不留痕** —— 表现为「短信极易丢失」，
# 事后连「到底来没来过」都无法查证。下面这张别名表 + 拒收台账就是修这个。
SMS_FIELD_ALIASES = {
    "phone": ("phone", "sender", "from", "from_phone", "number", "mobile", "address",
              "src", "send_num", "originatingaddress", "发件人", "号码"),
    "code": ("code", "sms_code", "verify_code", "vcode", "captcha", "otp",
             "verification_code", "验证码"),
    "text": ("text", "msg", "message", "content", "body", "sms", "sms_content",
             "short_message", "短信内容", "正文"),
}
SMS_REJECTED_PATH = os.environ.get(
    "RESUME_SMS_REJECTED",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sms_rejected.jsonl"),
)
SMS_REJECTED_MAX = 200
SMS_REJECTED = []   # [{"ts","reason","method","client","how","len","raw","seen"}]
# 访问日志：**每条请求记一行**（含客户端 IP 与请求行）。手机端「已发但界面没更新」时，
# 它是唯一能区分「请求根本没到 PC」和「到了但走错路径/被拒」的证据。
ACCESS_LOG_PATH = os.environ.get(
    "RESUME_ACCESS_LOG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "access.log"),
)
ACCESS_LOG_MAX = 2 * 1024 * 1024   # 超过就轮转成 access.log.1

PRESELECT_STATES = {"preselected", "待审批", "入口失败", "已确定", "已取消",
                    "填表中", "已填表", "填表失败",
                    "投递中", "需人工验证", "已登录", "已投递", "待人工提交", "投递失败"}

LOCK = threading.RLock()   # 可重入：save_* 可能在已持锁的调用链里再次加锁，普通 Lock 会死锁
CANDIDATES = []
STATE = {}
CUSTOM = []   # 手动添加的自定义预选（不在 xlsx 中，持久化于 state.json["_custom"]）
SMS = {}
SMS_HIST = []     # [{"phone","code","text","received"}] 追加式，最新的在末尾
# ---- 看板（一公司一栏 · 多岗位多 tab）相关状态 ----
HIDDEN_POS = []   # 被「取消该岗位」的 position id
HIDDEN_CO = []    # 被「取消该公司栏」的公司名
SCANS = {}        # {url: {"url","company","positions","is_list","error","ts"}}
POS_STATE = {}    # {position_id: {"status","note","updated"}} —— 扫描岗位的投递状态
SCAN_JOB = {"running": False, "total": 0, "done": 0, "lists": 0, "errors": 0,
            "current": "", "started": "", "stopped": False, "last": []}
APPLY_JOB = {"running": False, "total": 0, "done": 0, "ok": 0, "fail": 0,
             "human": 0, "skipped": 0, "cancelled": False, "started": "",
             "concurrency": 1, "wedged": 0}
# 一键投递的取消信号：worker 在**派发下一个岗位前**检查；filler 的等待/取码循环里也检查
# —— 所以正在跑的那个岗位也能被尽快中断，而不是只能干等它自己走完。
CANCEL_EVENT = threading.Event()

# 一键投递的**看门狗**：`done` 连续多久没前进就判定「卡死」，停止等待、收尾。
# 为什么必须有：Playwright 的 node 驱动一旦因 `Page.handleJavaScriptDialog` 之类的
# uncaughtException **崩掉**，还在等驱动的 worker 会**永久阻塞**（不抛错、不返回），
# 而 `ThreadPoolExecutor.__exit__` 是 `shutdown(wait=True)` → `running` 永远 True
# → 看板卡在「正在取消…」，也起不了下一轮（2026-09-24 实测就是这个）。
# 取消后可以更早收手（用户已经在等着关）；没取消时给足余量，避免误杀正常的长等待
# （人工交接预算默认 600s，必须明显大于它）。
APPLY_STUCK_GRACE = int(os.environ.get("RESUME_APPLY_STUCK_GRACE", "180"))
APPLY_HARD_STUCK = int(os.environ.get("RESUME_APPLY_HARD_STUCK", "1800"))


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _decode_any(raw):
    """把正文解成 str：依次试 utf-8-sig（顺带吃掉 BOM）→ utf-8 → gbk → gb18030。

    gbk 必须有：部分转发 APP 用系统默认编码（GBK/GB2312）发中文短信，
    旧版硬编码 utf-8 时这类上报 100% 丢。
    """
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc), enc
        except Exception:
            continue
    return raw.decode("utf-8", "replace"), "utf-8(replace)"


def _pick_fields(data):
    """按别名表从上报数据里取 phone / code / text（键名大小写不敏感）。"""
    flat = {}
    if isinstance(data, dict):
        for k, v in data.items():
            key = str(k).strip().lower()
            if isinstance(v, (str, int, float)):
                flat[key] = str(v)
            elif isinstance(v, (list, tuple)) and v and isinstance(v[0], (str, int, float)):
                flat[key] = str(v[0])
    out = {}
    for canon, names in SMS_FIELD_ALIASES.items():
        for n in names:
            if flat.get(n):
                out[canon] = flat[n]
                break
    return out


def _parse_payload(raw, ctype):
    """把请求正文解析成 dict，返回 (data, how)。**刻意宽容**，理由见 SMS_FIELD_ALIASES 注释。"""
    ctype = (ctype or "").lower()
    text, enc = _decode_any(raw or b"")
    s = text.strip()
    if not s:
        return {}, "empty"
    looks_json = ("json" in ctype) or s[:1] in ("{", "[")
    if looks_json:
        try:
            d = json.loads(s)
            if isinstance(d, dict):
                return d, "json/" + enc
            if isinstance(d, list) and d and isinstance(d[0], dict):
                return d[0], "json-list/" + enc
        except Exception:
            pass
    if ("x-www-form-urlencoded" in ctype) or ("=" in s):
        try:
            d = {k: v[0] for k, v in urllib.parse.parse_qs(
                s, keep_blank_values=True).items()}
            if d:
                return d, "form/" + enc
        except Exception:
            pass
    # 兜底：没有键值的纯文本上报 → 自己从正文里捞一次验证码，不白丢
    code = _extract_code_from_text(s)
    if code:
        return {"text": s, "code": code}, "text/" + enc
    return {"text": s}, "text-nocode/" + enc


def _log_rejected(reason, raw, how="", client="", method="POST", data=None):
    """**被拒的 /sms 请求必须留痕**（内存 + jsonl）。

    历史故障：静默 400 让「短信来过但没被记住」这件事在事后完全无法查证。
    """
    raw = raw or b""
    rec = {"ts": _now(), "reason": reason, "method": method, "client": client,
           "how": how, "len": len(raw),
           "raw": raw[:2000].decode("utf-8", "replace"),
           "seen": ({str(k): str(v)[:300] for k, v in data.items()}
                    if isinstance(data, dict) else {})}
    with LOCK:
        SMS_REJECTED.append(rec)
        del SMS_REJECTED[:-SMS_REJECTED_MAX]
    try:
        with open(SMS_REJECTED_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print("[SMS] rejected log failed: %s" % e, flush=True)
    print("[SMS] REJECTED reason=%s method=%s how=%s len=%d raw=%r"
          % (reason, method, how, rec["len"], rec["raw"][:300]), flush=True)


def load_sms():
    global SMS
    if os.path.exists(SMS_PATH):
        try:
            with open(SMS_PATH, "r", encoding="utf-8") as f:
                SMS = json.load(f)
        except Exception:
            SMS = {}


def _atomic_write_json(path, obj, attempts=3):
    """原子写 JSON，返回 (ok, err)。

    **三个关键点（都是修过的真实故障）**：
    ① `.tmp` 名字带 pid + 线程号。旧版所有线程共用 `path + ".tmp"`，并发上报时互相
       open/remove/replace 打架 —— 实测 12 条并发里 **11 次抛 WinError 32**，落盘全废。
    ② **不做「就地覆写」兜底**。旧版失败后直接 `open(path,"w")` 覆写目标文件，写一半
       崩掉就把 sms.json 截断成半截 JSON，`load_sms()` 判为损坏后 `SMS = {}` —— 整库清零。
       现在失败只记日志：宁可这一次没落盘，也不能毁掉已有内容。
    ③ 对**瞬时占用**（杀软扫描 / 云盘同步 / 另一个进程刚释放句柄）重试 3 次并退避，
       把「瞬时冲突」与「真的写不进去」区分开，收窄「磁盘落后于内存」的窗口。
    """
    err = ""
    for i in range(max(1, attempts)):
        # 每次尝试换一个 .tmp 名字：上一次失败留下的句柄/残留文件不会连累这一次
        tmp = "%s.%d.%d.%d.tmp" % (path, os.getpid(), threading.get_ident(), i)
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            return True, ""
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, e)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            if i + 1 < attempts:
                time.sleep(0.05 * (i + 1))
    return False, err


def save_sms():
    """持久化短信 store。**绝不抛异常**（/sms 端点必须始终成功，否则手机会重试/丢短信）。

    全程持 LOCK 并先做快照再写：`SMS` 会被并发 POST 改动，边改边 `json.dump`
    可能直接抛 `dictionary changed size during iteration`。
    """
    try:
        with LOCK:
            payload = dict(SMS)
            ok, err = _atomic_write_json(SMS_PATH, payload)
    except Exception as e:
        ok, err = False, "%s: %s" % (type(e).__name__, e)
    if not ok:
        print("[SMS] save failed: %s" % err, flush=True)
    return ok


def load_sms_hist():
    global SMS_HIST
    if os.path.exists(SMS_HIST_PATH):
        try:
            with open(SMS_HIST_PATH, "r", encoding="utf-8") as f:
                h = json.load(f)
            SMS_HIST = h if isinstance(h, list) else []
        except Exception:
            SMS_HIST = []
    # 老库兼容：历史为空但 sms.json 里已有记录 → 用字典内容回填一次
    if not SMS_HIST:
        SMS_HIST = [dict(v, phone=k) for k, v in (SMS or {}).items()
                    if isinstance(v, dict)]
    if SMS_HIST and not os.path.exists(SMS_HIST_PATH):
        save_sms_hist()      # 让回填的历史也落盘，避免每次启动都重新回填


def save_sms_hist():
    """持久化短信历史。**绝不抛异常**（同 save_sms）。"""
    try:
        with LOCK:
            payload = list(SMS_HIST[-SMS_HIST_MAX:])
            ok, err = _atomic_write_json(SMS_HIST_PATH, payload)
    except Exception as e:
        ok, err = False, "%s: %s" % (type(e).__name__, e)
    if not ok:
        print("[SMS] hist save failed: %s" % err, flush=True)
    return ok


def _extract_code_from_text(text):
    """从短信正文兜底提取验证码。

    手机端转发 APP 有时解析不出验证码（`code` 为空，实测它甚至会把短信标题塞进
    `phone` 字段），此时后端必须自己从正文里捞一次，否则会一直「验证码未到」。
    **只认关键词锚定的数字**（验证码/校验码/动态码…），不做「随便找个 4-6 位数」的
    兜底——否则会把营销短信里的数字误当验证码填进去。
    """
    if not text:
        return ""
    m = re.search(r"(?:验证码|校验码|检验码|动态码|口令|验证数字)[^\d]{0,10}(\d{4,8})", text)
    return m.group(1) if m else ""


def _sms_seq(rec):
    return (str(rec.get("received") or ""), str(rec.get("code") or ""))


CONSUMED_CODES = []   # [{"phone","received","code"}]，最近 200 条


def _rec_key(rec):
    """短信记录的唯一键（发件人 + 到达时刻 + 验证码）。"""
    return (str(rec.get("phone") or ""), str(rec.get("received") or ""),
            str(rec.get("code") or "").strip())


def mark_code_consumed(code):
    """把「已经交给某个岗位的验证码」记账。

    为什么必须记账：`latest_code` 只读取短信库、**不会把用过的码从库里删掉**，
    所以并发时同一个码会被另一个岗位再取一次 —— 这是串码的另一条路径。
    记账后被消费的码不再发给别的岗位。
    """
    code = str(code or "").strip()
    if not code:
        return
    with LOCK:
        src = list(SMS_HIST) if SMS_HIST else [
            dict(v, phone=k) for k, v in (SMS or {}).items() if isinstance(v, dict)]
        for rec in reversed(src):
            if str(rec.get("code") or "").strip() == code:
                CONSUMED_CODES.append(_rec_key(rec))
                del CONSUMED_CODES[:-200]
                return


def _consumed_set():
    with LOCK:
        return set(CONSUMED_CODES)


def latest_code(after="", snapshot=None, match=None, skip_consumed=False):
    """取短信历史中最新的验证码。

    after:    **硬下限**：只接受 received **不早于**该时刻的记录 —— 即「本条短信的
              到达时间不得早于本次请求短信的时刻」。这条**不允许被快照判定绕过**：
              实测故障就是旧实现允许绕过，把库里躺着的自检短信（TEST）当新码填了。
    snapshot: 取码前拍的 {phone: (received, code)} 快照，用于判「是否新到达」：
              只有「比快照里**同一发件人**那条更新」才算新的（同秒到达也兜得住）。
              历史为空则退回 sms.json 字典。
    match:    **优先**只取「本站在等的那条」的可调用对象（见 sms_matcher）。
              若一条都没命中则**回退**到不过滤 —— 宁可偶尔串码，也不能因为公司名
              没出现在短信正文里就永远取不到码。
    skip_consumed: 跳过**已经发给过别的岗位**的验证码（防并发重复取用，见
              mark_code_consumed）。默认 False 保持旧行为。

    received 为 "YYYY-MM-DD HH:MM:SS" 字符串，字典序即时序。
    """
    has_snap = snapshot is not None
    snapshot = snapshot or {}
    skip = _consumed_set() if skip_consumed else None
    src = list(SMS_HIST) if SMS_HIST else [
        dict(v, phone=k) for k, v in (SMS or {}).items() if isinstance(v, dict)]

    def _pass(require_match):
        newest, newest_code = "", ""
        for rec in src:
            if not isinstance(rec, dict):
                continue
            if skip is not None and _rec_key(rec) in skip:
                continue     # 这个码已经交给过别的岗位了，别再重复取用
            code = str(rec.get("code") or "").strip()
            if not code:
                continue
            if match is not None:
                try:
                    hit = bool(match(rec))
                except Exception:
                    hit = False
                if hit != require_match:
                    continue
            ts = str(rec.get("received") or "").strip()
            # ① 硬下限（用户明确规则）：**早于「本次请求短信的时刻」的短信一律不要**，
            #    并且**不允许被「内容变了」绕过**。实测故障：库里躺着几条很旧的 TEST
            #    自检短信，同一个发件人有**多条历史**时，旧实现拿「值 != 快照」判新到达，
            #    把更早的那几条也判成「新的」→ 绕过 after 过滤 → 投递时把 TEST 的码
            #    填了进去。received 是后端收到上报的时刻，字典序即时序。
            if not ts:
                continue                    # 没有到达时间就无法校验时间规则 → 不用
            if after and ts < after:
                continue
            # ② 快照判重：只有「比快照里**同一发件人**那条更新」才算新到达。
            #    · 与快照那条完全相同 → 旧码
            #    · 比快照那条还早 → 更早的历史（不是新到的），内容不同也不要
            #    · 快照里没有这个发件人 → 不据此排除（① 的时间下限仍然管着它）
            if has_snap:
                prev = snapshot.get(str(rec.get("phone") or ""))
                if prev is not None:
                    if prev == _sms_seq(rec):
                        continue
                    if ts < str(prev[0] or ""):
                        continue
            if ts >= newest:
                newest, newest_code = ts, code
        return newest_code

    if match is None:
        return _pass(False)
    return _pass(True) or _pass(False)


def sms_matcher(*keywords):
    """构造「优先取本站在等的那条短信」的匹配器；没有可用关键词时返回 None。

    短信正文里常带站点/公司名（如「【XX招聘】验证码1234…」），拿它当锚点就能避开
    同时在登录的**别家站点**的码。实测旧逻辑：A 站 0001 先到、无关 B 站 9999 后到 →
    返回 9999（串码）。只用长度 >=2 的关键词，避免单字误命中。
    """
    kws = []
    for k in keywords:
        k = str(k or "").strip()
        if len(k) >= 2 and k not in kws:
            kws.append(k)
    if not kws:
        return None

    def _m(rec):
        hay = "%s %s" % (rec.get("phone") or "", rec.get("text") or "")
        return any(k in hay for k in kws)

    return _m


def sms_pick_detail(code):
    """「这个码是从哪条短信来的」一句话描述，写进岗位备注便于一眼看出串码。"""
    code = str(code or "").strip()
    if not code:
        return ""
    with LOCK:
        src = list(SMS_HIST)
    hit = None
    for rec in reversed(src):
        if str(rec.get("code") or "").strip() == code:
            hit = rec
            break
    if not hit:
        return code
    who = str(hit.get("phone") or "?")
    body = str(hit.get("text") or "").strip().replace("\n", " ")
    return "%s（来自 %s 的「%s」）" % (code, who, body[:40])


def sms_snapshot():
    """取码前拍快照：{phone: (received, code)}。"""
    if SMS_HIST:
        return {str(r.get("phone") or ""): _sms_seq(r)
                for r in SMS_HIST if isinstance(r, dict)}
    return {k: _sms_seq(v) for k, v in (SMS or {}).items() if isinstance(v, dict)}


def sms_info():
    """短信库概况，给 filler 区分「一条都没来」和「来了但没解析出验证码」。"""
    if SMS_HIST:
        newest = ""
        for r in SMS_HIST:
            if isinstance(r, dict):
                t = str(r.get("received") or "")
                if t >= newest:
                    newest = t
        return {"total": len(SMS_HIST), "newest": newest}
    return {"total": len(SMS), "newest": ""}


def reload():
    global CANDIDATES, STATE, CUSTOM, HIDDEN_POS, HIDDEN_CO, SCANS, POS_STATE
    with LOCK:
        cands = candidates.load_candidates(XLSX)
        st = statemod.load_state()
        statemod.apply_status(cands, st)
        # 合并手动添加的自定义预选（xlsx 里没有，需从 state 还原）
        custom = list(st.get("_custom", []))
        for c in custom:
            rec = st.get(c.get("id", ""))
            if rec:
                c["status"] = rec.get("status", c.get("status", "preselected"))
                c["entry_url"] = rec.get("entry_url", c.get("entry_url", ""))
                c["note"] = rec.get("note", c.get("note", ""))
                c["decision"] = rec.get("decision", c.get("decision", ""))
            cands.append(c)
        CANDIDATES = cands
        STATE = st
        CUSTOM = custom
        HIDDEN_POS = list(st.get("_hidden_pos", []))
        HIDDEN_CO = list(st.get("_hidden_co", []))
        SCANS = dict(st.get("_scans", {}))
        POS_STATE = dict(st.get("_pos_state", {}))


def _persist_custom_locked():
    """在已持锁时把自定义预选落盘到 state.json["_custom"]。"""
    STATE["_custom"] = CUSTOM
    statemod.save_state(STATE)


# ==========================================================================
# 看板：一公司一栏 · 多岗位多 tab
# ==========================================================================

def _save_board_locked():
    """在已持锁时把看板状态落盘（隐藏项 / 扫描缓存 / 岗位投递状态）。"""
    STATE["_hidden_pos"] = HIDDEN_POS
    STATE["_hidden_co"] = HIDDEN_CO
    STATE["_scans"] = SCANS
    STATE["_pos_state"] = POS_STATE
    statemod.save_state(STATE)


def reset_all():
    """复位**所有已投递内容**：逐岗位状态 + 手动预选状态全部回到 pending。

    保留：取消（_hidden_*）选择、扫描缓存（_scans）、短信库、profile。
    供「重投一轮」用（UI 的复位按钮 / 测试收尾都走这里，避免手改 state.json）。
    """
    with LOCK:
        cleared = 0
        for k in [k for k in list(STATE.keys()) if not k.startswith("_")]:
            v = STATE.get(k)
            if isinstance(v, dict) and "status" in v:
                del STATE[k]
                cleared += 1
        n_pos = len(POS_STATE)
        POS_STATE.clear()
        n_custom = 0
        for c in CUSTOM:
            if c.get("status") not in (None, "pending"):
                n_custom += 1
            c["status"] = "pending"
            c["note"] = ""
        for c in CANDIDATES:
            c["status"] = "pending"
            c["note"] = ""
        _save_board_locked()
        return {"ok": True, "cleared_status": cleared, "cleared_pos_state": n_pos,
                "custom_reset": n_custom}


def columns_payload(mode=posmod.MODE_STRICT):
    """看板数据：公司栏 + 岗位 tab（带各口径计数 + 扫描/投递任务进度）。"""
    with LOCK:
        rows = list(CANDIDATES)
        scans = dict(SCANS)
        hp = list(HIDDEN_POS)
        hc = list(HIDDEN_CO)
        ps = dict(POS_STATE)
    kws, cts = filler.load_match_rules()
    data = posmod.build_columns(rows, scans, hidden_pos=hp, hidden_co=hc,
                                mode=(mode or posmod.MODE_STRICT), pos_state=ps,
                                kws=kws, cts=cts)
    data["scan_job"] = dict(SCAN_JOB)
    data["apply_job"] = dict(APPLY_JOB)
    # 当前「取消」条数：前端据此在确认框里报准数，不靠猜
    data["hidden"] = {"positions": len(hp), "companies": len(hc)}
    return data


def _merge_note(old, new):
    """把新进度**追加**到已有 note 上，保留完整轨迹。

    旧版每次都整体覆盖 note，导致事后只剩最后一条「等待短信转发到达…」，
    完全看不出前面到底有没有点到「获取验证码」——这正是排查慢转发很困难的原因。
    重复的心跳进度（同前缀，如「等待短信转发到达」）只保留最新一条，避免刷屏。
    """
    if not new:
        return old or ""
    if not old:
        return new
    segs = old.split("；")
    pref = (new.split("（")[0] or "").strip()
    if pref and segs and (segs[-1].split("（")[0] or "").strip() == pref:
        segs[-1] = new
    else:
        segs.append(new)
    s = "；".join(segs)
    if len(s) > 2000:                      # 保头（起点线索）+ 保尾（失败原因）
        s = s[:400] + "…（中间省略）…" + s[-1500:]
    return s


# 简历解析结果的**结构归一**（T67）。
# 为什么必须归一：LLM 抽取是概率性的，`period` 这类**非正文语义字段**最容易漏
# —— 用户报的「项目 json 解析后没有 period（项目时间）」就是这么来的：prompt 里
# experiences 有 period、projects 漏了，且解析结果是「模型返回啥就存啥」。
# 归一之后，下游（前端 JSON 文本框 / 填表 `_stringify` / 简历导出）拿到的永远是
# **定长 schema**，不会出现「有的项目有时间、有的没有」或整个 key 缺失。
_EXP_KEYS = ("company", "role", "period", "highlights")
_PROJ_KEYS = ("name", "role", "period", "desc")


def _norm_resume_items(items, keys, list_key=None):
    """把 LLM 返回的数组归一成「每项都有固定 keys」的 dict 列表。

    · 缺的键补 ""（`list_key` 指定的键补 []）；
    · **模型多给的字段保留**（不丢信息，用户可在文本框里自己整理）；
    · 非 dict 项（模型偶发返回纯字符串）按首列兜底，**不静默丢弃**
      —— 解析时凭空少一条，比多留一条糟糕得多。
    """
    out = []
    if not isinstance(items, list):
        return out
    for it in items:
        if isinstance(it, dict):
            row = {}
            for k in keys:
                v = it.get(k)
                if list_key and k == list_key:
                    if isinstance(v, list):
                        row[k] = [str(x) for x in v if str(x).strip()]
                    elif v:
                        row[k] = [str(v)]
                    else:
                        row[k] = []
                elif v is None:
                    row[k] = ""
                elif isinstance(v, str):
                    row[k] = v
                elif isinstance(v, (int, float, bool)):
                    row[k] = str(v)
                else:
                    row[k] = ""
            for k, v in it.items():          # 保留额外字段
                if k not in row:
                    row[k] = v
            out.append(row)
        elif isinstance(it, str) and it.strip():
            row = dict((k, "") for k in keys)
            row[keys[0]] = it.strip()
            if list_key:
                row[list_key] = []
            out.append(row)
    return out


# apply_by_url 的 stage → 看板状态。
# **不能把「流程走通」一律写成「已登录」**：登录成功但表单没填/没提交成功，
# 和真的投出去了是两件事。只有拿到明确成功文案（或「已投递过」）才算「已投递」；
# 其余非人工 stage（理论上只有旧的「已登录」）保持原样。
STAGE_TO_STATUS = {
    "投递成功": "已投递",
    "投递成功（人工确认）": "已投递",
    "已投递过": "已投递",
    # **只有真的人机验证 / 验证码交接**才是「需人工验证」（项目口径）。
    "人机验证": "需人工验证",
    "验证码未到": "需人工验证",
    # 下面这些是「表单/提交要人在浏览器窗口里处理」→ 待人工提交。
    # 旧代码把它们一律兜底成「需人工验证」，于是「必填项没填」也显示成
    # 「需要人工过验证」—— 用户报的「阶段提示卡在需要人工过验证」就是这个。
    "已填表待确认": "待人工提交",
    "必填未填": "待人工提交",
    "提交结果未知": "待人工提交",
    "未找到提交按钮": "待人工提交",
    "已填表未提交": "待人工提交",
    "填表未命中": "待人工提交",
    "填表失败": "待人工提交",
    "未找到投递表单": "待人工提交",
    # 登录本身没成功（验证码错 / 页面仍停在登录表单）→ 这是**登录交接**，不是
    # 「表单/提交交接」，所以归「需人工验证」。旧版不分辨，一律落到「待人工提交」，
    # 正是 bug ①「未登录成功却显示等待人工提交」的现象。
    "登录未完成": "需人工验证",
    "登录失败": "需人工验证",
    "会话排队超时": "待人工提交",
    # 站点不允许投递本岗位（「只允许投递一个岗位」「投递次数已达上限」等）→ 没投成
    "投递受限": "投递失败",
    # 被「取消投递」中断 → 没投成，回到「待投递」以便随时重新排队
    "已取消": "待投递",
}


def _stage_status(stage, needs_human=False):
    """把 apply_by_url 返回的 stage 映射为看板状态。

    有专门映射的用映射（如「已填表待确认」→「待人工提交」）；
    没映射的：needs_human=True → 「待人工提交」，否则 → 「已登录」。

    ⚠️ 兜底**不再**是「需人工验证」：按项目口径「需人工验证」**专指**人机验证/验证码
    交接（都已在上面的映射里显式列出）。未知的「需要人工」场景一律算「待人工提交」，
    否则又会把「表单必填没填」「没找到提交按钮」这类问题显示成「需要人工过验证」。
    """
    s = STAGE_TO_STATUS.get(stage or "")
    if s:
        return s
    return "待人工提交" if needs_human else "已登录"


def set_pos_status(pid, status, note="", fresh=False):
    """写单个岗位的投递状态。

    扫描出来的岗位不在 xlsx 里（没有候选行），状态只能存在 POS_STATE；
    表格行则两边都写，保证「重新加载」后不丢。
    fresh=True 表示「新一轮投递开始」，note 直接覆盖而不是追加。
    """
    with LOCK:
        old = ""
        if not fresh:
            # 必须连**候选行自己的 note** 一起看：投递行与 POS_STATE 是两份存储，
            # 出现过「行上有轨迹、POS_STATE 里没有」的情况（从旧版 state.json 恢复、
            # 或旧代码只写了单边）。只读后两者会把已有轨迹**静默丢掉**（自测 G 段抓到）。
            old = ((POS_STATE.get(pid) or {}).get("note")
                   or (STATE.get(pid) or {}).get("note")
                   or ((_get(pid) or {}).get("note") or ""))
        merged = note if fresh else _merge_note(old, note)
        rec = {"status": status, "note": merged, "updated": _now()}
        POS_STATE[pid] = rec
        c = _get(pid)
        if c is not None:
            c["status"] = status
            c["note"] = merged
        STATE[pid] = rec
        _save_board_locked()


def heal_interrupted_statuses():
    """进程重启后：把上一轮**中断残留**的「投递中」复位成「待投递」。

    为什么必须有这一步：`投递中` 在 `ready` 判定里算「已占用 / 已完成」，看板既不给
    重投按钮、也不会自己变回来 —— 后端只要在投递中途被 kill（或崩），那个岗位就
    **永久卡在「投递中」**。用户报「状态仍卡错」，一部分就是这个。
    启动时不可能有在跑的投递，所以此刻的「投递中」一定是残留，可以安全复位。

    **结果未知就不写「已投递」**（那是假话）：如实写「中途被中断、结果未知，已复位」，
    真投出去了由用户点「确认已投递」兜底。
    """
    with LOCK:
        pids = []
        for c in CANDIDATES:
            if (c.get("status") or "").strip() == "投递中":
                pids.append(c["id"])
        for pid, rec in POS_STATE.items():
            if (rec or {}).get("status") == "投递中" and pid not in pids:
                pids.append(pid)
    msg = ("⚠️ 上一轮投递在**中途被中断**（后端进程重启过）：本轮结果未知，已复位为"
           "「待投递」。若你确认它其实已经投出去，请点「确认已投递」覆盖。")
    for pid in pids:
        set_pos_status(pid, "待投递", msg)     # fresh=False：保留中断前的轨迹
        print("[heal] %s：投递中 → 待投递（中断残留）" % pid, flush=True)
    if pids:
        print("[heal] 共复位 %d 个中断残留岗位" % len(pids), flush=True)
    return len(pids)


def hide_position(pid):
    """取消单个岗位 → 从看板移除（持久化，重新加载也不会回来）。"""
    if not pid:
        return {"ok": False, "error": "id required"}
    with LOCK:
        if pid not in HIDDEN_POS:
            HIDDEN_POS.append(pid)
        _save_board_locked()
    return {"ok": True, "hidden_positions": len(HIDDEN_POS)}


def hide_company(company):
    """取消整栏公司 → 该公司所有岗位一并移除。"""
    company = (company or "").strip()
    if not company:
        return {"ok": False, "error": "company required"}
    with LOCK:
        if company not in HIDDEN_CO:
            HIDDEN_CO.append(company)
        _save_board_locked()
    return {"ok": True, "hidden_companies": len(HIDDEN_CO)}


def unhide_all():
    """清空所有「取消」记录（误点了可一键恢复）。

    清空前把两份名单存进 `_hidden_prev`：误点（或测试脚本收尾误调）之后还能
    `POST /api/unhide/undo` 一条不差地放回去 —— 用户的「取消」是手工点出来的，
    丢了无法重建。
    """
    with LOCK:
        n_pos, n_co = len(HIDDEN_POS), len(HIDDEN_CO)
        STATE["_hidden_prev"] = {"pos": list(HIDDEN_POS), "co": list(HIDDEN_CO)}
        del HIDDEN_POS[:]
        del HIDDEN_CO[:]
        _save_board_locked()
    print("[hide] 恢复已取消：清空 %d 家公司 / %d 个岗位（快照存 _hidden_prev）" % (n_co, n_pos), flush=True)
    return {"ok": True, "restored_positions": n_pos, "restored_companies": n_co}


def unhide_undo():
    """把上一次「恢复已取消」清掉的记录原样放回去（一次性，用过即作废）。"""
    with LOCK:
        prev = STATE.get("_hidden_prev") or {}
        pos = [p for p in (prev.get("pos") or []) if p]
        co = [c for c in (prev.get("co") or []) if c]
        if not pos and not co:
            return {"ok": False, "error": "没有可撤销的恢复记录"}
        for p in pos:
            if p not in HIDDEN_POS:
                HIDDEN_POS.append(p)
        for c in co:
            if c not in HIDDEN_CO:
                HIDDEN_CO.append(c)
        STATE.pop("_hidden_prev", None)
        _save_board_locked()
    print("[hide] 撤销恢复：放回 %d 家公司 / %d 个岗位" % (len(co), len(pos)), flush=True)
    return {"ok": True, "restored_positions": len(pos), "restored_companies": len(co)}


# ---------------------------- 岗位扫描（后台，3 并发） ----------------------------

def _is_list_like(url):
    """粗判「列表页」。岗位详页不用扫（扫出来是垃圾），直接跳过省时间。"""
    u = (url or "").lower()
    if not u:
        return False
    if "/detail" in u:
        return False
    if re.search(r"/position/\d+", u):
        return False
    return True


def _scan_targets(limit, force=False):
    """扫描目标 = 各公司的「投递链接」。

    表格里的 `来源校招页` 就是该公司的岗位**列表页**（380 个 distinct），而
    `岗位页链接` 多是个别岗位详页（1292 个）——扫详页没意义。所以优先用来源校招页，
    没有时退回岗位页链接。
    """
    by_url = {}
    for c in CANDIDATES:
        row_url = (c.get("url") or "").strip()
        link = (c.get("source") or "").strip() or row_url
        if not link or not _is_list_like(link):
            continue
        d = by_url.setdefault(link, {"url": link, "company": c.get("company", ""), "rows": 0})
        d["rows"] += 1
    n_row_urls = len({(c.get("url") or "").strip() for c in CANDIDATES
                      if (c.get("url") or "").strip()})
    todo = [d for d in by_url.values() if force or d["url"] not in SCANS]
    # 岗位数多的公司优先（更可能是真列表页、也更值得扫）
    todo.sort(key=lambda d: -d["rows"])
    return todo[:max(1, limit)], n_row_urls, len(by_url)


def scan_summary():
    n_cached = len(SCANS)
    n_list = len([1 for v in SCANS.values() if v.get("is_list")])
    n_err = len([1 for v in SCANS.values() if v.get("error")])
    n_pos = sum(len(v.get("positions") or []) for v in SCANS.values())
    return {"cached_urls": n_cached, "list_pages": n_list, "errors": n_err,
            "scanned_positions": n_pos}


def start_scan(limit=40, force=False):
    with LOCK:
        if SCAN_JOB.get("running"):
            return {"ok": False, "error": "扫描正在进行中"}
        todo, n_url, n_list = _scan_targets(int(limit or 40), force)
        if not todo:
            return {"ok": True, "started": 0, "total_urls": n_url, "list_urls": n_list,
                    "message": "没有待扫描的列表页链接（缓存已覆盖）"}
        SCAN_JOB.update(running=True, total=len(todo), done=0, lists=0, errors=0,
                        current="", started=_now(), stopped=False, last=[])
    threading.Thread(target=_scan_worker, args=(todo,), daemon=True).start()
    return {"ok": True, "started": len(todo), "total_urls": n_url, "list_urls": n_list}


def _scan_worker(todo):
    def one(t):
        with LOCK:
            SCAN_JOB["current"] = t["url"]
            if SCAN_JOB.get("stopped"):
                SCAN_JOB["done"] += 1
                return
        try:
            r = filler.scan_positions(t["url"], keywords=[], cities=[], max_more=2)
            good = [p for p in r["all"] if filler.is_usable_scanned_row(p)]
            dropped = len(r["all"]) - len(good)
            # 至少 2 个「像样的岗位行」才当列表页；否则是岗位详页或扫不动
            is_list = len(good) >= 2
            rec = {"url": t["url"], "company": t["company"], "ts": _now(),
                   "is_list": is_list, "count": len(good), "raw_count": len(r["all"]),
                   "dropped": dropped,
                   "positions": (good if is_list else [])}
        except Exception as e:
            rec = {"url": t["url"], "company": t["company"], "ts": _now(),
                   "is_list": False, "count": 0, "raw_count": 0, "dropped": 0,
                   "positions": [],
                   "error": "%s: %s" % (type(e).__name__, str(e)[:140])}
        with LOCK:
            SCANS[t["url"]] = rec
            SCAN_JOB["done"] += 1
            if rec.get("error"):
                SCAN_JOB["errors"] += 1
                line = "%s ✗ %s" % (t["company"][:12], rec["error"])
            elif rec["is_list"]:
                SCAN_JOB["lists"] += 1
                line = "%s ✓ %d 个岗位%s" % (
                    t["company"][:12], rec["count"],
                    ("（过滤掉 %d 个非岗位块）" % rec["dropped"]) if rec["dropped"] else "")
            else:
                line = "%s · 未识别为列表页（%d 行，可用 %d）" % (
                    t["company"][:12], rec["raw_count"], rec["count"])
            SCAN_JOB["last"] = ([line] + SCAN_JOB["last"])[:8]
            _save_board_locked()

    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(one, todo))
    with LOCK:
        SCAN_JOB["running"] = False
        SCAN_JOB["current"] = ""
        SCAN_JOB["stopped"] = False


# ---------------------------- 一键投递（后台，单窗口严格串行） ----------------------------

def _collect_apply_items(ids, mode):
    data = columns_payload(mode)
    want = set(ids or [])
    items = []
    for col in data["columns"]:
        for p in col["positions"]:
            if want and p["id"] not in want:
                continue
            if not p.get("ready"):       # 已登录/投递中/等人工的跳过，不重复排队
                continue
            if not (p.get("url") or ""):
                continue
            items.append({"pid": p["id"], "company": col["company"],
                          "title": p.get("title") or "", "url": p["url"],
                          "pos_key": p.get("pos_key") or ""})
    return items


def _apply_one_item(it):
    """投递单个岗位（供单岗位 / 一键投递共用）。返回 'ok' / 'human' / 'fail'。"""
    pid = it["pid"]
    if CANCEL_EVENT.is_set():
        # 取消后**不再开新岗位**（连浏览器窗口都不开）：留在「待投递」可重新排队
        set_pos_status(pid, "待投递", note="已取消：该岗位尚未开始投递", fresh=True)
        return "skipped"
    # fresh=True：新一轮投递，note 从头写（不要接上一轮的轨迹）
    set_pos_status(pid, "投递中", note="%s：开始打开岗位页…" % (it["title"] or "")[:40],
                   fresh=True)
    # 取码时间窗。防串码靠四层：① filler 里的全局登录闸门（登录互斥）
    # ② 本窗口收窄（on_code_request 把起点推到「真的点了获取验证码」之后）
    # ③ latest_code 的**硬时间下限**（早于请求时刻的短信一律不要，不可被绕过）
    # ④ latest_code 的 skip_consumed（已发出去的码不再发给别人）
    win = {"start": _now(), "snap": sms_snapshot()}
    # 优先取「本公司在等的那条」短信（正文/发件人里带公司名时），避开同时登录别家站点时串码
    matcher = sms_matcher(it.get("company"), it.get("title"))
    picked = {"rec": ""}

    def progress(msg):
        # 人工交接有**两种**语义，看板状态不同（前端按状态渲染「进入窗口 / 确认已投递」）：
        #   · 人机验证 / 验证码交接        → 需人工验证
        #   · 表单/提交要人在窗口里补填或点提交 → 待人工提交
        # 「等待人工提交」由 filler 在**驻留等待**时写进备注（含心跳）——不加这个标记
        # 的话，心跳会把状态刷回「投递中」，用户在等待期间看不到「进入窗口」按钮。
        st = "投递中"
        if "等待人工提交" in msg:
            st = "待人工提交"
        elif ("转人工" in msg) or ("等待人工" in msg) or ("手动完成" in msg):
            st = "需人工验证"
        set_pos_status(pid, st, note=msg)

    def get_code():
        code = latest_code(after=win["start"], snapshot=win["snap"], match=matcher,
                           skip_consumed=True)
        if code:
            picked["rec"] = sms_pick_detail(code)
            mark_code_consumed(code)   # 记账：已交给本岗位，别再发给别的岗位
        return code

    def on_code_request():
        """filler 真的点了「获取验证码」时回调：把取码窗口**推到此刻**。

        旧版这里「已有码可取就不动窗口」是错的：那时窗口起点还是**任务开始**时刻，
        它会连「任务开始前就躺在库里」的旧码一起认 —— 实测用某公司做测试时，
        把上一条自检短信 TEST 的验证码填了进去。规则改成硬性的：
        **读到的短信时间不得早于本次「获取验证码」发出的时刻**，早于此的短信
        （上一轮遗留、自检短信、别家迟到码）一律不再考虑。

        但**只推第一次**：人机验证通过后流程会再点一次「获取验证码」，若每次都推后，
        第一次点击后已经到达的那条码会被自己的新下限挡在窗外 → 白等到超时。
        沿用首次请求时刻依然满足上面的硬规则（那条码并不早于首次请求）。
        """
        if win.get("req_at"):
            progress("本岗位已请求过验证码：沿用首次请求的时间下限（不重复收窄）")
            return
        win["req_at"] = _now()
        win["start"] = win["req_at"]
        win["snap"] = sms_snapshot()
        progress("取码窗口已收窄到本次请求之后（更早的短信一律不考虑）")

    try:
        # 人机验证要**驻留等人工**：窗口保持打开 + 完成后自动继续。旧代码这里传
        # human_wait=0（本意「别卡住」），实际效果是「窗口立刻关闭、人根本没机会过验证」
        # —— 用户实测反馈「转人工后人工验证失败 / 状态卡在需人工验证」。
        # 现在留空用默认预算（filler._handoff_budget()，可用 RESUME_HUMAN_HANDOFF_WAIT 调）；
        # 等待期间可随时点「取消投递」中断。取码 code_wait 保留（登录必要步骤）。
        r = filler.apply_by_url(it["url"], it["title"], progress=progress,
                                get_code=get_code, pos_title=it.get("pos_key") or "",
                                sms_info=sms_info,
                                pause_before_submit=bool(it.get("pause_before_submit")),
                                confirm_wait=(None if it.get("confirm_wait") is None
                                              else int(it.get("confirm_wait"))),
                                company=it.get("company") or "",
                                on_code_request=on_code_request,
                                is_cancelled=CANCEL_EVENT.is_set,
                                task_key=it.get("pid") or "")
        st = _stage_status(r.get("stage"), needs_human=bool(r.get("needs_human")))
        # 把「取到的是哪条短信」写进备注：真发生串码时一眼能看出来串到哪去了
        tail = ("；取到验证码 %s" % picked["rec"]) if picked["rec"] else ""
        if r.get("stage") == "已取消":
            # 被「取消投递」打断：没投成，如实记回「待投递」以便重新排队
            # （既不算 ok 也不算 fail，单独计入 skipped）
            set_pos_status(pid, "待投递", note=(r.get("detail") or "已取消") + tail)
            return "skipped"
        if r.get("needs_human") and st == "待人工提交":
            # 停在提交前 → 不算失败，也不算投递成功，如实记「待人工提交」
            set_pos_status(pid, st,
                           note=r.get("detail", "表单已填好，等待人工确认提交") + tail)
            return "human"
        if r.get("needs_human"):
            set_pos_status(pid, st, note=r.get("detail", "等待人工完成人机验证") + tail)
            return "human"
        set_pos_status(pid, st, note=r.get("detail", "") + tail)
        return "ok"
    except Exception as e:
        set_pos_status(pid, "投递失败", note="投递失败: %s" % str(e)[:220])
        return "fail"


def start_apply_one(pid, mode=posmod.MODE_STRICT, pause_before_submit=True,
                    confirm_wait=None):
    """单岗位投递。扫描出来的岗位不在 xlsx 里，所以走的是「看板岗位」这条路，
    只有真找不到时，才退回旧的候选行投递逻辑。

    pause_before_submit 默认 **True**：单岗位投递是「你想自己确认一次」的场景，
    填完表停在提交前，窗口保持打开等你点提交。
    """
    items = _collect_apply_items([pid], mode)
    if items:
        it = items[0]
    else:
        c = _get(pid)
        if c is None:
            return {"ok": False, "error": "not found"}
        url = c.get("entry_url") or c.get("url") or ""
        if not url:
            return {"ok": False, "error": "无投递链接"}
        it = {"pid": pid, "company": c.get("company", ""), "title": c.get("title", ""),
              "url": url, "pos_key": ""}
    it["pause_before_submit"] = bool(pause_before_submit)
    it["confirm_wait"] = (None if confirm_wait is None else int(confirm_wait))
    # 单岗位投递 = 一次**全新的**意图：清掉上一轮可能残留的取消信号。
    # 不清的话，「取消过一键投递」之后点任何单岗位投递都会被当成已取消
    # （`_apply_one_item` 一进门就 skipped → 岗位被打回「待投递」，用户看到「点了没反应」）。
    CANCEL_EVENT.clear()
    threading.Thread(target=_apply_one_item, args=(it,), daemon=True).start()
    return {"ok": True, "started": True, "status": "投递中",
            "pause_before_submit": it["pause_before_submit"]}


def start_apply_all(ids=None, mode=posmod.MODE_STRICT, concurrency=1,
                    pause_before_submit=True, confirm_wait=None):
    with LOCK:
        if APPLY_JOB.get("running"):
            return {"ok": False, "error": "一键投递正在进行中"}
    CANCEL_EVENT.clear()      # 新一轮开始 → 清掉上一轮残留的取消信号
    items = _collect_apply_items(ids, mode)
    if not items:
        return {"ok": False, "error": "预选栏内没有可投递的岗位（已投递/正在投递的不会重复投）"}
    # **单窗口（严格串行）**：2026-09-23 后端默认已改成 1，但前端固定传 3 →
    # 看板一次弹出 3 个标签页，人工确认时分不清哪个窗口属于哪个岗位；
    # CDP 接管模式下并发岗位还共享同一个 ctx，会互相抢页面/抢焦点。
    # 2026-10-28 用户要求「单窗口弹出，不再并发」→ 这里**强制 1**，
    # 无论客户端传什么（3 / 6 / 0 / 负数 / 非法值）都只跑一个。
    # 参数 `concurrency` 保留只为兼容旧客户端，**取值被忽略**。
    try:
        _asked_conc = int(concurrency or 1)
    except Exception:
        _asked_conc = 1
    conc = 1
    if _asked_conc != 1:
        print("[APPLY] 忽略请求的并发度 %r → 强制单窗口（1）" % (concurrency,),
              flush=True)
    with LOCK:
        APPLY_JOB.update(running=True, total=len(items), done=0, ok=0, fail=0,
                         human=0, skipped=0, cancelled=False, started=_now(),
                         concurrency=conc, wedged=0)
    # 一键投递**默认停在提交前**（2026-10-28 用户要求：填完停在提交前等我核对）。
    # 每个岗位填完就把窗口交给你，你点「提交」后流程自动继续下一个；
    # 想恢复全自动（填完即自动提交），显式传 pause_before_submit=False。
    for it in items:
        it["pause_before_submit"] = bool(pause_before_submit)
        it["confirm_wait"] = (None if confirm_wait is None else int(confirm_wait))
    threading.Thread(target=_apply_all_worker, args=(items, conc), daemon=True).start()
    return {"ok": True, "total": len(items), "concurrency": conc,
            "companies": len({it["company"] for it in items})}


def _apply_stall_budget():
    """当前允许「done 连续多久没前进」的秒数。

    取消后大幅收紧：用户已经在等着关窗，正常岗位会在下一个检查点很快中断，
    这么久还没进展就是真卡死了。没取消时给足余量（必须明显大于人工交接预算）。
    """
    return APPLY_STUCK_GRACE if CANCEL_EVENT.is_set() else APPLY_HARD_STUCK


def _reset_stalled_positions(items, reason):
    """把「看门狗判定卡死」轮次里**还停在「投递中」**的岗位复位为「待投递」。

    只动「投递中」：
      · 已落终态（已投递 / 投递失败 / 待人工提交 / 需人工验证）的是**真实结果**，
        绝不许被看门狗抹掉；
      · 状态为空的是排在后面**根本没轮到**的岗位，也不动 —— 它本来就还没投。
    **结果未知绝不写「已投递」。**
    """
    n = 0
    for it in items:
        pid = (it or {}).get("pid")
        if not pid:
            continue
        with LOCK:
            st = ""
            for c in CANDIDATES:
                if c.get("id") == pid:
                    st = (c.get("status") or "").strip()
                    break
            else:
                st = ((POS_STATE.get(pid) or {}).get("status") or "").strip()
        if st != "投递中":
            continue
        try:
            set_pos_status(pid, "待投递",
                           note=("⚠️ %s：本轮结果未知，已复位为「待投递」。"
                                 "若确认它其实已经投出去，请点「确认已投递」覆盖。" % reason))
            n += 1
        except Exception as e:
            print("[APPLY] 复位 %s 失败：%s" % (pid, e), flush=True)
    if n:
        print("[APPLY] 看门狗：%d 个未落终态的岗位已复位为「待投递」" % n, flush=True)
    return n


def _apply_all_worker(items, conc):
    def one(it):
        try:
            res = _apply_one_item(it)
        except Exception as e:
            res = "fail"
            print("[APPLY] unexpected: %s" % e, flush=True)
            # **异常也必须落终态**：否则该岗位永远停在「投递中」，看板上看不出原因。
            # 实测过的真实异常：Playwright 的 node 驱动因
            # `Protocol error (Page.handleJavaScriptDialog): No dialog is showing`
            # 崩溃 → 后续调用全部抛 Connection closed while reading from the driver。
            try:
                set_pos_status(it["pid"], "投递失败",
                               note="投递中断（%s: %s）" % (type(e).__name__, e))
            except Exception:
                pass
        with LOCK:
            APPLY_JOB["done"] += 1
            if res in APPLY_JOB:
                APPLY_JOB[res] += 1

    # **不能再用 `with ThreadPoolExecutor(...) as ex`**：`__exit__` 是 `shutdown(wait=True)`，
    # 只要有一个 worker 被卡住（Playwright 驱动崩掉后它就永不返回），这里就永远不返回
    # → `running` 恒 True → 看板一直停在「正在取消…」，也起不了下一轮。
    # 改成：自己 submit + `wait()` 轮询「done 有没有前进」，超预算就收尾走人。
    ex = ThreadPoolExecutor(max_workers=conc)
    fut_item = {ex.submit(one, it): it for it in items}
    pending = set(fut_item)
    last_done, last_move, stalled = -1, time.time(), False
    try:
        while pending:
            _, pending = wait(pending, timeout=5, return_when=FIRST_COMPLETED)
            with LOCK:
                cur = APPLY_JOB["done"]
            now = time.time()
            if cur != last_done:            # 有进展 → 重置停滞计时，继续等
                last_done, last_move = cur, now
                continue
            budget = _apply_stall_budget()
            if now - last_move >= budget:
                stalled = True
                print("[APPLY] 看门狗：done=%d/%d 已停滞 %.0fs（预算 %.0fs）→ 停止等待并收尾"
                      % (cur, APPLY_JOB.get("total"), now - last_move, budget), flush=True)
                break
    finally:
        if stalled:
            _reset_stalled_positions([fut_item[f] for f in pending],
                                     "投递长时间无进展（疑似浏览器驱动已崩）")
        with LOCK:
            APPLY_JOB["running"] = False
            APPLY_JOB["cancelled"] = bool(CANCEL_EVENT.is_set())
            if stalled:
                APPLY_JOB["wedged"] = len(pending)
        # **绝不能 wait=True**：卡住的那个 worker 可能永远不返回，等它就等于没修。
        ex.shutdown(wait=False)


def cancel_apply_all():
    """请求取消一键投递队列。

    语义：**不再派发新岗位**；正在跑的那个由 filler 在等待/取码循环的检查点
    尽快中断（所以不用干等它自己走完）。幂等：没在跑时也返回 ok=True。
    """
    with LOCK:
        running = bool(APPLY_JOB.get("running"))
        APPLY_JOB["cancelled"] = True
    CANCEL_EVENT.set()
    return {"ok": True, "running": running,
            "hint": ("已请求取消：不再派发新岗位；正在投递的岗位会在下一个检查点中断"
                     if running else "当前没有正在运行的投递队列（取消信号已置位，下次投递前会清掉）")}


def _get(cid):
    for c in CANDIDATES:
        if c["id"] == cid:
            return c
    return None


def set_status(cid, status, note="", entry_url=None, decision=None, fresh=False):
    with LOCK:
        c = _get(cid)
        if c is None:
            return False
        # note 追加式累积（fresh=True 时重开）；保留完整轨迹便于事后排查
        old = "" if fresh else ((STATE.get(cid) or {}).get("note") or "")
        merged = None if note is None else (note if fresh else _merge_note(old, note))
        c["status"] = status
        if merged is not None:
            c["note"] = merged
        if entry_url is not None:
            c["entry_url"] = entry_url
        if decision is not None:
            c["decision"] = decision
        rec = {"status": status, "updated": _now()}
        if merged is not None:
            rec["note"] = merged
        if entry_url is not None:
            rec["entry_url"] = entry_url
        if decision is not None:
            rec["decision"] = decision
        STATE[cid] = rec
        # 同步到看板的岗位状态（否则旧的投递状态会盖住新状态）；
        # 注意要写**合并后**的 note，否则会把 set_pos_status 攒下的轨迹清掉
        POS_STATE[cid] = {"status": status, "note": merged or "", "updated": _now()}
        _save_board_locked()
        return True


def do_preselect(cid):
    """预选：触发 Playwright 找入口，置 待审批 / 入口失败。"""
    c = _get(cid)
    if c is None:
        return None
    set_status(cid, "preselected")
    try:
        entry = entry_finder.find_entry(c["url"], c.get("company", ""), c.get("title", ""))
        set_status(cid, "待审批", entry_url=entry)
        return {"ok": True, "status": "待审批", "entry_url": entry}
    except entry_finder.EntryNotFound as e:
        set_status(cid, "入口失败", note=str(e))
        return {"ok": True, "status": "入口失败", "entry_url": ""}


def do_refind(cid):
    c = _get(cid)
    if c is None:
        return {"ok": False}
    try:
        entry = entry_finder.find_entry(c["url"], c.get("company", ""), c.get("title", ""))
        set_status(cid, "待审批", entry_url=entry)
        return {"ok": True, "status": "待审批", "entry_url": entry}
    except entry_finder.EntryNotFound as e:
        set_status(cid, "入口失败", note=str(e))
        return {"ok": True, "status": "入口失败", "entry_url": ""}


def do_decide(cid, decision):
    if decision == "确定":
        ok = set_status(cid, "已确定", decision="确定")
    elif decision == "取消":
        ok = set_status(cid, "已取消", decision="取消")
    else:
        return {"ok": False, "error": "decision 必须为 确定/取消"}
    return {"ok": ok}


def mapping_preview(cid):
    """返回 个人信息库字段 → 表单字段 的精准匹配预览。"""
    c = _get(cid)
    if c is None:
        return {"ok": False}
    try:
        profile = filler.load_profile()
    except Exception:
        profile = {}
    rows = []
    for label, path in filler.ATS_TEMPLATES["default"].items():
        val = filler._get_path(profile, path)
        rows.append({"form_field": label, "profile_path": path, "value": val})
    return {"ok": True, "company": c["company"], "title": c["title"],
            "entry_url": c.get("entry_url", ""), "rows": rows}


def do_add(company, title, url):
    """手动添加预选：创建自定义候选 + 跑 Playwright 找入口，持久化到 state.json _custom。"""
    company = (company or "").strip()
    title = (title or "").strip()
    url = (url or "").strip()
    if not url:
        return {"ok": False, "error": "url required"}
    cid = candidates._make_id(company or title or "未命名", title, url)
    with LOCK:
        if _get(cid) is None:
            c = {
                "id": cid,
                "company": company or title or "未命名",
                "title": title,
                "url": url,
                "family": "",
                "city": "",
                "city_first": "",
                "source": "手动添加",
                "status": "preselected",
                "entry_url": "",
                "decision": "",
                "note": "",
            }
            CANDIDATES.append(c)
            CUSTOM.append(c)
            _persist_custom_locked()
    # 跑 Playwright 找入口（不持锁，避免阻塞其它请求）
    try:
        entry = entry_finder.find_entry(url, company, title)
        set_status(cid, "待审批", entry_url=entry)
        return {"ok": True, "id": cid, "status": "待审批", "entry_url": entry}
    except entry_finder.EntryNotFound as e:
        set_status(cid, "入口失败", note=str(e))
        return {"ok": True, "id": cid, "status": "入口失败", "entry_url": ""}


def do_fill(cid):
    """断点4：对投递入口自动填表。置 填表中 → 已填表 / 填表失败。"""
    c = _get(cid)
    if c is None:
        return {"ok": False, "error": "not found"}
    entry = c.get("entry_url") or c.get("url") or ""
    if not entry:
        set_status(cid, "入口失败", note="无投递入口链接，无法填表")
        return {"ok": False, "error": "无入口链接", "status": "入口失败"}
    set_status(cid, "填表中")
    try:
        detail = filler.fill_by_url(entry)
        set_status(cid, "已填表", note=detail)
        return {"ok": True, "detail": detail, "status": "已填表"}
    except Exception as e:
        set_status(cid, "填表失败", note="填表失败: %s" % e)
        return {"ok": False, "error": "填表失败: %s" % e, "status": "填表失败"}


def do_apply(cid):
    """投递流程（断点3+4）：后台线程里用可见浏览器跑 apply_by_url，
    实时把阶段写入状态；命中人机验证→置「需人工验证」并由人工在窗口完成。"""
    c = _get(cid)
    if c is None:
        return {"ok": False, "error": "not found"}
    url = c.get("entry_url") or c.get("url") or ""
    if not url:
        return {"ok": False, "error": "无投递链接"}
    title = c.get("title", "")

    set_status(cid, "投递中", note="正在打开岗位页并自动投递…", fresh=True)
    win = {"start": _now(), "snap": sms_snapshot()}   # 取码时间窗（见 _apply_one_item 注释）
    matcher = sms_matcher(c.get("company"), title)   # 优先取本站在等的那条短信

    def run():
        picked = {"rec": ""}

        def progress(msg):
            # 只有「真的交接人工」的提示才置「需人工验证」；自动过验证阶段仍是「投递中」
            st = "投递中"
            if ("转人工" in msg) or ("等待人工" in msg) or ("手动完成" in msg):
                st = "需人工验证"
            set_status(cid, st, note=msg)

        def get_code():
            code = latest_code(after=win["start"], snapshot=win["snap"], match=matcher,
                               skip_consumed=True)
            if code:
                picked["rec"] = sms_pick_detail(code)
                mark_code_consumed(code)
            return code

        def on_code_request():
            """真的点了「获取验证码」→ **总是**把取码窗口推到此刻。

            不再判断「已有码可取就不动」：那会让任务开始前就躺在库里的旧码
            （上一轮遗留 / 自检短信）被当成新码取走。硬规则：读到的短信时间
            不得早于本次请求发出的时刻。**只推第一次**（重发时沿用首次下限，
            免得把第一次点击后已到达的码挡在窗外）。见 _apply_one_item。
            """
            if win.get("req_at"):
                progress("本岗位已请求过验证码：沿用首次请求的时间下限（不重复收窄）")
                return
            win["req_at"] = _now()
            win["start"] = win["req_at"]
            win["snap"] = sms_snapshot()
            progress("取码窗口已收窄到本次请求之后（更早的短信一律不考虑）")

        try:
            # 人机验证驻留等人工（同上）：窗口保持打开、完成后自动继续
            r = filler.apply_by_url(url, title, progress=progress, get_code=get_code,
                                    sms_info=sms_info,
                                    company=c.get("company") or "",
                                    on_code_request=on_code_request,
                                    task_key=cid)
            tail = ("；取到验证码 %s" % picked["rec"]) if picked["rec"] else ""
            # 不再把「需要人工」一律写成「需人工验证」：状态由 stage 映射决定
            # （表单必填/提交结果未知 → 待人工提交；人机验证/验证码未到 → 需人工验证）。
            set_status(cid, _stage_status(r.get("stage"),
                                          needs_human=bool(r.get("needs_human"))),
                       note=r.get("detail", "") + tail)
        except Exception as e:
            set_status(cid, "投递失败", note="投递失败: %s" % e)

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "started": True, "status": "投递中"}


def sms_intake(data, raw=b"", how="", client="", method="POST"):
    """**所有短信入口的唯一处理函数**（POST /sms、GET /sms 共用），返回 (http_code, resp)。

    宽容点（都是修真实故障）：
      · 字段名走别名表（sender / msg / content / body … 都认）
      · 没给 code 时从正文兜底提取
      · **连发件人都没有也不丢**：只要拿到了正文就记为 (unknown) 保留 ——
        「正文里有验证码」比「字段规整」重要得多（旧版直接 400 丢掉）。
    """
    f = _pick_fields(data)
    phone = (f.get("phone") or "").strip()
    code = (f.get("code") or "").strip()
    text = (f.get("text") or "").strip()
    if not phone and (text or code):
        phone = "(unknown)"
    if not phone:
        _log_rejected("no-phone", raw, how, client, method, data)
        return 400, {"error": "phone required",
                     "hint": "接受 phone/code/text，或 sender/msg/content 等别名；"
                             "也接受 form 编码、chunked 与纯文本正文"}
    if not code:
        code = _extract_code_from_text(text)
    ts = _now()
    with LOCK:
        SMS[phone] = {"code": code, "text": text, "received": ts}
        SMS_HIST.append({"phone": phone, "code": code, "text": text, "received": ts})
        del SMS_HIST[:-SMS_HIST_MAX]
        hist_n = len(SMS_HIST)
    save_sms()
    save_sms_hist()
    print("[SMS] received phone=%s code=%s how=%s at %s" % (phone, code, how, ts), flush=True)
    return 200, {"ok": True, "phone": phone, "code": code, "text": text,
                 "received": ts, "how": how, "hist": hist_n}


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 → 允许 keep-alive，突发上报不必每条新建 TCP（旧版未设 = HTTP/1.0）
    protocol_version = "HTTP/1.1"
    timeout = 60            # 空闲 keep-alive 连接 60s 后回收，避免线程被挂死

    def handle_one_request(self):
        """**每个请求都必须重新读正文**：keep-alive 复用同一个 handler 实例。

        `_body()` 把解析结果缓存在 `self._body_cache` 上，本意是「一个请求只读一次」。
        但 `BaseHTTPRequestHandler.handle()` 在 keep-alive 下**复用同一个实例**处理同一条
        连接上的多个请求 —— 于是同一连接上的**第二个 POST 会拿到第一个 POST 的正文**。

        真实事故（2026-09-23 22:23，访问日志可查）：22:20:55 点「一键投递」
        （POST /api/apply-all，正文 `{ids,mode,concurrency}`），22:23:03 在同一连接上点
        「个人信息 → 保存」（POST /api/profile）→ profile 处理器拿到的却是那份**投递载荷**，
        `profile.json` 被覆盖成 `{ids,mode,concurrency}` = 用户报的「个人信息保存后消失」。
        同理 `/api/parse-resume` 也可能收到上一次请求的正文。缓存必须**按请求**清，
        不能按连接。
        """
        self._body_cache = None
        self._body_raw = b""
        self._body_how = ""
        return BaseHTTPRequestHandler.handle_one_request(self)

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        # 禁止浏览器缓存，确保前端改动即时生效（否则会看到旧页面）
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        # CORS：WebView / 浏览器型转发 APP 会先发 OPTIONS 预检，旧版没这个方法 → 501，
        # 真实 POST 根本发不出去（实测）。
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Requested-With")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _read_chunked(self):
        """手工解 chunked（标准库不会自动解）。

        旧版 `_body()` 只看 Content-Length，`Transfer-Encoding: chunked` 的上报
        拿到 `{}` → 400 丢掉（实测确认）。
        """
        out = []
        while True:
            line = self.rfile.readline(65536)
            if not line:
                break
            line = line.split(b";", 1)[0].strip()
            try:
                n = int(line, 16)
            except Exception:
                break
            if n <= 0:
                self.rfile.readline(65536)      # 吃掉末尾的 CRLF / trailer
                break
            out.append(self.rfile.read(n))
            self.rfile.read(2)                  # 分块之间的 CRLF
        return b"".join(out)

    def _body(self):
        """读并解析请求正文。**一个请求只读一次**（结果缓存，keep-alive 下不残留正文）。

        宽容策略见 `_parse_payload`：form / chunked / BOM / GBK / 缺 Content-Length
        都要能收 —— 旧版只认「UTF-8 的 JSON + Content-Length」，其余静默 400。
        """
        if getattr(self, "_body_cache", None) is not None:
            return self._body_cache
        raw = b""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            try:
                raw = self._read_chunked()
            except Exception:
                raw = b""
        if not raw:
            try:
                ln = int(self.headers.get("Content-Length", 0) or 0)
            except Exception:
                ln = 0
            if ln > 0:
                try:
                    raw = self.rfile.read(ln)
                except Exception:
                    raw = b""
        if not raw and (getattr(self, "close_connection", False)
                        or self.request_version == "HTTP/1.0"):
            # 客户端声明会关连接、却给了个不可用的长度（Content-Length 缺失或为 0）：
            # 把缓冲区里可能存在的正文捞回来，别把这条短信白丢。
            # 两个坑（都是本轮实测踩出来的）：
            #   ① 不能用 read(n)：它要等到凑满 n 字节或 EOF，超时还会把已读到的字节一起丢掉；
            #   ② 不能用 select 当门控：请求头解析时的 readline() 早已把正文读进
            #      BufferedReader 的**用户态缓冲**，内核缓冲里「没数据」→ select 报 not ready，
            #      于是永远跳过。read1() 会先吐出用户态缓冲里的存货，无需门控。
            # 代价：仅当「声明关连接 + 长度不可用」时才多花最多 0.3s。
            try:
                self.connection.settimeout(0.3)
            except Exception:
                pass
            try:
                while len(raw) < 65536:
                    chunk = self.rfile.read1(4096)
                    if not chunk:
                        break
                    raw += chunk
            except Exception:
                pass
            finally:
                try:
                    self.connection.settimeout(self.timeout)
                except Exception:
                    pass
        data, how = _parse_payload(raw, self.headers.get("Content-Type"))
        self._body_raw = raw
        self._body_how = how
        self._body_cache = data
        return data

    def do_OPTIONS(self):
        """CORS 预检。旧版没有这个方法 → `501 Unsupported method`，WebView 型
        转发 APP 的真实 POST 连发都发不出去（实测确认）。"""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower() \
                or (self.headers.get("Content-Length") or "0") not in ("", "0"):
            self._body()      # keep-alive 下必须读干净，否则残留正文串到下一个请求
        self._send(200, {"ok": True, "cors": "*"})

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        path = p.path
        q = urllib.parse.parse_qs(p.query)

        # keep-alive 下必须把正文读干净，否则残留字节会被当成下一个请求
        try:
            _cl = int(self.headers.get("Content-Length") or 0)
        except Exception:
            _cl = 0
        if _cl > 0 or "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            self._body()

        # GET /sms?phone=&code=&text= —— 有些转发 APP 只会用 GET 上报。
        # 不带任何上报字段时当「连通性自检」，只回报概况（不写库）。
        if path == "/sms":
            low = {k.lower(): v for k, v in q.items()}
            report = any(low.get(k) for k in
                         ("phone", "sender", "from", "number", "mobile", "code",
                          "text", "msg", "message", "content", "body"))
            if report:
                d = self._body()
                merged = dict(d) if isinstance(d, dict) else {}
                merged.update({k: v[0] for k, v in q.items()})
                c, resp = sms_intake(merged, getattr(self, "_body_raw", b""),
                                     getattr(self, "_body_how", ""),
                                     client=self.client_address[0], method="GET")
                self._send(c, resp)
            else:
                info = dict(sms_info())
                info.update({"ok": True, "hint": "POST /sms 上报短信；?phone=&code= 亦可"})
                self._send(200, info)
            return

        if path in ("/", "/index.html"):
            if os.path.exists(FRONTEND):
                with open(FRONTEND, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            else:
                self._send(404, "frontend not found")
            return

        if path.startswith("/vendor/"):
            # PDF.js 等静态资源：只允许 frontend/vendor/ 下的单层文件名，防目录穿越
            rel = path[len("/vendor/"):]
            if (not rel) or (os.path.basename(rel) != rel) or (".." in rel):
                self._send(404, "not found")
                return
            fp = os.path.join(BASE, "frontend", "vendor", rel)
            if os.path.exists(fp):
                ctype = "application/javascript; charset=utf-8" if fp.endswith(".js") else "application/octet-stream"
                with open(fp, "rb") as f:
                    self._send(200, f.read(), ctype)
            else:
                self._send(404, "not found")
            return

        if path == "/api/candidates":
            status = q.get("status", [""])[0]
            city = q.get("city", [""])[0]
            family = q.get("family", [""])[0]
            res = CANDIDATES
            if status:
                res = [c for c in res if c["status"] == status]
            if city:
                res = [c for c in res if city in c["city"]]
            if family:
                res = [c for c in res if family in c["family"]]
            self._send(200, res)
            return

        if path == "/api/preselect":
            res = [c for c in CANDIDATES if c["status"] in PRESELECT_STATES]
            self._send(200, res)
            return

        if path.startswith("/api/preselect/") and path.endswith("/mapping"):
            cid = path[len("/api/preselect/"):-len("/mapping")]
            self._send(200, mapping_preview(cid))
            return

        if path == "/api/stats":
            s = {}
            for c in CANDIDATES:
                s[c["status"]] = s.get(c["status"], 0) + 1
            self._send(200, {"total": len(CANDIDATES), "by_status": s})
            return

        if path == "/api/fill-mode":
            # 断点4 由 WorkBuddy 本地 computer-use（Playwright）完成，不接入外部 AI 模型
            self._send(200, {
                "mode": "local-computer-use",
                "engine": "playwright",
                "external_ai": False,
                # 如实反映：单岗位与一键投递都默认停在提交前（2026-10-28 用户要求）
                "submits_automatically": "单岗位投递=停在提交前；一键投递=也停在提交前（逐个核对）",
                "single_apply_default": {"pause_before_submit": True,
                                         "confirm_wait": filler.PAUSE_CONFIRM_WAIT},
                "batch_apply_default": {"pause_before_submit": True},
                "note": "单岗位与一键投递**都**是填完表停在提交前，浏览器窗口保持打开等人工点提交；"
                        "你点提交后流程自动继续下一个岗位。两处**默认不设超时**，窗口不会因为"
                        "等待超时被关闭，也不会因为填写异常自行关闭"
                        "（当前 confirm_wait=%d，0 = 不超时；可用 RESUME_PAUSE_CONFIRM_WAIT "
                        "设正数恢复限时收尾）" % filler.PAUSE_CONFIRM_WAIT,
            })
            return

        if path == "/api/captcha/status":
            # 断点2：人机验证自动过（CV → AI 视觉）的可用性自检
            self._send(200, captcha.mode_info())
            return

        if path == "/api/edge-cdp/status":
            # CDP 接管状态（只读）：调试端口是否就绪 / Edge 是否在跑 / 用的资料目录
            try:
                import edge_cdp
                self._send(200, edge_cdp.status())
            except Exception as e:  # noqa: BLE001
                self._send(200, {"error": "%s: %s" % (type(e).__name__, e)})
            return

        if path == "/api/columns":
            # 看板主数据：一公司一栏 · 多岗位多 tab
            mode = q.get("mode", [posmod.MODE_STRICT])[0]
            self._send(200, columns_payload(mode))
            return

        if path == "/api/hidden":
            # 只读快照：当前被「取消」的公司 / 岗位。自测脚本收尾按它**原样**放回，
            # 不再用「一把清空」收尾（2026-10-28 事故：自测 unhide_all 冲掉了用户
            # 手工取消的 15 条记录，无法重建）。
            with LOCK:
                self._send(200, {"ok": True,
                                 "companies": list(HIDDEN_CO),
                                 "positions": list(HIDDEN_POS),
                                 "prev": STATE.get("_hidden_prev") or {}})
            return

        if path == "/api/scan/status":
            self._send(200, {"job": dict(SCAN_JOB), "summary": scan_summary()})
            return

        if path == "/api/apply-all/status":
            payload = dict(APPLY_JOB)
            # 排查「并发取码串码」时最关键的两个字段：
            #   login_holder  —— 此刻正在登录哪一家（登录闸门被谁持有）
            #   consumed_codes—— 已经发给过岗位、不会再被重复取用的验证码条数
            payload["login_holder"] = getattr(filler, "LOGIN_HOLDER", "")
            payload["consumed_codes"] = len(_consumed_set())
            self._send(200, payload)
            return

        if path == "/api/profile":
            if os.path.exists(PROFILE):
                with open(PROFILE, "r", encoding="utf-8") as f:
                    self._send(200, f.read(), "application/json; charset=utf-8")
            else:
                self._send(404, {"error": "no profile"})
            return

        if path == "/api/match-rules":
            # 前端「匹配条件」窗口读取当前生效的关键词/城市
            kws, cts = filler.load_match_rules()
            self._send(200, {"ok": True, "keywords": kws, "cities": cts})
            return

        if path == "/api/sms":
            with LOCK:
                self._send(200, dict(SMS))
            return

        # 短信历史（追加式，最近的在后）。sms.json 按发件人存「最新一条」会互相覆盖，
        # 想回看「到底有没有来短信/来了什么」要看这个。
        if path == "/api/sms/history":
            with LOCK:
                self._send(200, {"total": len(SMS_HIST), "items": list(SMS_HIST)[-30:]})
            return

        # 访问日志：查「手机到底有没有连上来、走的是哪个路径」
        if path == "/api/access":
            try:
                want = max(1, min(int(q.get("n", ["80"])[0] or 80), 500))
            except Exception:
                want = 80
            all_lines, total = [], 0
            if os.path.exists(ACCESS_LOG_PATH):
                try:
                    with open(ACCESS_LOG_PATH, "r", encoding="utf-8",
                              errors="replace") as f:
                        all_lines = [ln.rstrip("\n") for ln in f]
                    total = len(all_lines)
                except Exception as e:
                    all_lines = ["read failed: %s" % e]
            # **peers / alien / posts 必须扫全文件**：前端每 3s 轮询两个 /api/*，
            # 只看尾巴会被 127.0.0.1 灌满，从而把「手机其实连上来过」误判成没连上
            #（本轮真实踩到：手机 21:32:06 的 GET /sms 被挤出最后 60 行）。
            peers, alien, posts = set(), [], 0
            for ln in all_lines:
                parts = ln.split("|")
                if len(parts) < 3:
                    continue
                who = parts[1].strip()
                peers.add(who)
                if who not in ("127.0.0.1", "::1", "localhost"):
                    alien.append(ln)
                if "POST /sms" in ln:
                    posts += 1
            self._send(200, {"path": ACCESS_LOG_PATH, "total": total,
                             "peers": sorted(peers),
                             "posts": posts,
                             "alien_total": len(alien),
                             "alien": alien[-want:],
                             "lines": all_lines[-want:]})
            return

        # 被拒上报台账：查「到底有没有短信来过、来的什么报文」（旧版拒收不留痕）
        if path == "/api/sms/rejected":
            with LOCK:
                items = list(SMS_REJECTED)[-50:]
            total = 0
            if os.path.exists(SMS_REJECTED_PATH):
                try:
                    with open(SMS_REJECTED_PATH, "r", encoding="utf-8") as f:
                        total = sum(1 for _ in f)
                except Exception:
                    total = -1
            self._send(200, {"total": total, "path": SMS_REJECTED_PATH, "items": items})
            return

        if path.startswith("/api/sms/"):
            phone = urllib.parse.unquote(path[len("/api/sms/"):])
            with LOCK:
                self._send(200, SMS.get(phone, {}))
            return

        self._send(404, {"error": "not found"})

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        path = p.path
        data = self._body()   # 先读干净：keep-alive 下不读正文会让下一个请求解析错位

        if path == "/api/reload":
            reload()
            self._send(200, {"ok": True, "total": len(CANDIDATES)})
            return

        if path == "/api/profile":
            # 前端「个人信息填写窗口」保存：原子写回 profile.json。
            # filler.load_profile() 每次读盘，保存后立即对后续投递生效。
            data = self._body()
            if not isinstance(data, dict):
                self._send(400, {"ok": False, "error": "body 必须是 JSON 对象"})
                return
            try:
                # **先滚动备份**：profile.json 是用户手输的唯一副本，误写一次就整份没了。
                # 2026-09-23 的「keep-alive 串正文」事故正是把它覆盖成了投递载荷，
                # 事后全盘找不到任何副本 → 只能让用户重填。保留最近 3 份（.bak-1 最新）。
                if os.path.exists(PROFILE):
                    for _i in (2, 1):
                        _s = "%s.bak-%d" % (PROFILE, _i)
                        if os.path.exists(_s):
                            try:
                                os.replace(_s, "%s.bak-%d" % (PROFILE, _i + 1))
                            except OSError:
                                pass
                    try:
                        with open(PROFILE, "rb") as _fr:
                            _prev = _fr.read()
                        with open(PROFILE + ".bak-1", "wb") as _fw:
                            _fw.write(_prev)
                    except OSError:
                        pass
                tmp = PROFILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, PROFILE)
                self._send(200, {"ok": True, "keys": list(data.keys()),
                                 "backup": "profile.json.bak-1"})
            except Exception as e:
                self._send(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
            return

        if path == "/api/sms/clear":
            # 前端「短信接收框」清除按钮：清空当前展示的短信（内存 SMS + sms.json），
            # 保留追加历史 /api/sms/history 与拒收台账（审计用，用户没要求清历史）。
            try:
                with LOCK:
                    SMS.clear()
                    save_sms()
                self._send(200, {"ok": True, "cleared": True})
            except Exception as e:
                self._send(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
            return

        if path == "/api/match-rules":
            # 前端「匹配条件」窗口保存：原子写回 match_rules.json。
            # 看板每次 columns_payload 都重新读盘，保存后立即对看板/投递生效。
            data = self._body()
            if not isinstance(data, dict):
                self._send(400, {"ok": False, "error": "body 必须是 JSON 对象"})
                return
            try:
                payload = filler.save_match_rules(data)
                self._send(200, {"ok": True, "keywords": payload["keywords"],
                                "cities": payload["cities"]})
            except Exception as e:
                self._send(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
            return

        if path == "/api/parse-resume":
            # front-end picks PDF -> PDF.js extracts text -> backend calls LLM to structure 4 JSON arrays
            data = self._body()
            if not isinstance(data, dict):
                self._send(400, {"ok": False, "error": "body must be JSON object"})
                return
            text = (data.get("text") or "").strip()
            if not text:
                self._send(400, {"ok": False, "error": "missing resume text"})
                return
            if not llm_client.is_configured():
                self._send(200, {"ok": False, "error": "LLM not configured (set env snova / RESUME_LLM_API_KEY)", "need_config": True})
                return
            try:
                MAX = int(data.get("max_chars") or 8000)
            except Exception:
                MAX = 8000
            if len(text) > MAX:
                text = text[:MAX]
            messages = [
                {"role": "system", "content":
                 "You are a resume parser. Input is Chinese resume plain text. Output a JSON object "
                 "with exactly these 4 keys: experiences (array of {company, role, period, highlights:[str]}), "
                 "projects (array of {name, role, period, desc}), skills (array of str), awards (array of str). "
                 "IMPORTANT: every item of experiences AND projects MUST carry a period field, copied "
                 "verbatim from the resume (format \"YYYY.MM – YYYY.MM\", e.g. 2026.02 – 2026.06). "
                 "If the resume really has no date for an item, set period to \"\" — never invent a date. "
                 "Output ONLY JSON, no explanation, no markdown code fence."},
                {"role": "user", "content": text},
            ]
            try:
                d, used = llm_client.chat_json(messages, timeout=90)
                out = {
                    "experiences": _norm_resume_items(d.get("experiences"), _EXP_KEYS,
                                                      list_key="highlights"),
                    "projects": _norm_resume_items(d.get("projects"), _PROJ_KEYS),
                    "skills": d.get("skills") or [],
                    "awards": d.get("awards") or [],
                }
                for k in out:
                    if not isinstance(out[k], list):
                        out[k] = []
                self._send(200, {"ok": True, "used_model": used, **out})
            except Exception as e:
                self._send(200, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
            return

        if path == "/api/hide":
            # 取消一个岗位（body.id）或整栏公司（body.company）→ 从看板移除
            data = self._body()
            if str(data.get("company", "")).strip():
                self._send(200, hide_company(str(data["company"])))
            else:
                self._send(200, hide_position(str(data.get("id", ""))))
            return

        if path == "/api/unhide":
            # 误点「取消」后可一键恢复全部（并发一份可撤销快照）
            self._send(200, unhide_all())
            return

        if path == "/api/unhide/undo":
            # 撤销上一次「恢复已取消」
            self._send(200, unhide_undo())
            return

        if path == "/api/reset-all":
            # 复位所有已投递内容（全部回到「待投递」），便于整轮重投
            self._send(200, reset_all())
            return

        if path == "/api/scan":
            data = self._body()
            self._send(200, start_scan(int(data.get("limit", 40) or 40),
                                       bool(data.get("force"))))
            return

        if path == "/api/scan/stop":
            with LOCK:
                SCAN_JOB["stopped"] = True
            self._send(200, {"ok": True})
            return

        if path == "/api/apply-all/cancel":
            # 取消一键投递队列（前端「取消投递」按钮）
            self._send(200, cancel_apply_all())
            return

        if path == "/api/apply-all":
            # 一键投递：对当前预选栏内所有「未投递」岗位**单窗口串行**投递（并发度强制 1）
        # 默认每个岗位**填完停在提交前**等人工点提交（pause_before_submit 默认 True）。
            data = self._body()
            self._send(200, start_apply_all(
                list(data.get("ids") or []),
                str(data.get("mode") or posmod.MODE_STRICT),
                int(data.get("concurrency", 1) or 1),
                pause_before_submit=bool(data.get("pause_before_submit", True)),
                confirm_wait=int(data.get("confirm_wait") or 0)))
            return

        if path.startswith("/api/candidates/") and path.endswith("/preselect"):
            cid = path[len("/api/candidates/"):-len("/preselect")]
            self._send(200, do_preselect(cid) or {"ok": False, "error": "not found"})
            return

        if path.startswith("/api/preselect/") and path.endswith("/refind"):
            cid = path[len("/api/preselect/"):-len("/refind")]
            self._send(200, do_refind(cid))
            return

        if path.startswith("/api/preselect/") and path.endswith("/focus"):
            # 前端「进入窗口」：请求把**该岗位**正在人工等待的浏览器窗口置前。
            # 窗口是否仍开着只有投递线程知道 → 如实只回「已请求」。
            # **必须带 pid**：提交段是并发的，只置全局标志会让别家岗位的窗口被置前
            # （见 filler._FOCUS_PENDING 注释）。
            cid = urllib.parse.unquote(path[len("/api/preselect/"):-len("/focus")])
            self._body()
            ok = filler.request_focus(cid)
            self._send(200, {"ok": bool(ok), "requested": bool(ok)})
            return

        if path.startswith("/api/preselect/") and path.endswith("/human-done"):
            # 前端「确认已投递」：人工确认该岗位已完成 → 登记为终态「已投递」。
            cid = urllib.parse.unquote(path[len("/api/preselect/"):-len("/human-done")])
            self._body()
            if _get(cid) is None and cid not in POS_STATE:
                self._send(200, {"ok": False, "error": "not found"})
                return
            set_pos_status(cid, "已投递", note="人工确认：已投递（前端确认按钮）")
            self._send(200, {"ok": True, "status": "已投递"})
            return

        if path.startswith("/api/preselect/") and path.endswith("/mark-pending"):
            # 前端「标记为未投递」（每个岗位 tab 常显）：把该岗位切回可投递状态便于重投。
            # 会**覆盖**已有的投递结果备注（用户已确认这一语义）。
            cid = urllib.parse.unquote(path[len("/api/preselect/"):-len("/mark-pending")])
            self._body()
            if _get(cid) is None and cid not in POS_STATE:
                self._send(200, {"ok": False, "error": "not found"})
                return
            set_pos_status(cid, "pending", fresh=True,
                           note="人工标记：未投递（已恢复可重新投递）")
            self._send(200, {"ok": True, "status": "pending"})
            return

        if path == "/api/edge-cdp/launch":
            # 以**调试端口**启动主 Edge（真实资料目录）→ 投递页即可继承登录态。
            # 不搬运 cookie：cookie 由 Edge 自己解密、经 CDP 返回（见 edge_cdp 模块说明）。
            data = self._body()
            want_close = bool(data.get("close_edge", False)) if isinstance(data, dict) else False
            try:
                import edge_cdp
                self._send(200, edge_cdp.launch(close_edge=want_close))
            except Exception as e:  # noqa: BLE001
                self._send(200, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
            return

        if path.startswith("/api/preselect/") and path.endswith("/decide"):
            cid = path[len("/api/preselect/"):-len("/decide")]
            data = self._body()
            self._send(200, do_decide(cid, str(data.get("decision", ""))))
            return

        if path == "/api/preselect/add":
            data = self._body()
            self._send(200, do_add(str(data.get("company", "")),
                                   str(data.get("title", "")),
                                   str(data.get("url", ""))))
            return

        if path.startswith("/api/preselect/") and path.endswith("/fill"):
            cid = path[len("/api/preselect/"):-len("/fill")]
            self._send(200, do_fill(cid))
            return

        if path.startswith("/api/preselect/") and path.endswith("/apply"):
            cid = urllib.parse.unquote(path[len("/api/preselect/"):-len("/apply")])
            data = self._body()
            # 单岗位投递**默认停在提交前**（填完表等你在窗口里点提交）
            self._send(200, start_apply_one(
                cid, str(data.get("mode") or posmod.MODE_STRICT),
                pause_before_submit=bool(data.get("pause_before_submit", True)),
                confirm_wait=(None if data.get("confirm_wait") is None
                              else int(data.get("confirm_wait") or 0))))
            return

        if path.startswith("/api/candidates/") and path.endswith("/confirm"):
            cid = path[len("/api/candidates/"):-len("/confirm")]
            ok = set_status(cid, "confirmed")
            self._send(200 if ok else 404, {"ok": ok})
            return

        if path.startswith("/api/candidates/") and path.endswith("/reject"):
            cid = path[len("/api/candidates/"):-len("/reject")]
            data = self._body()
            ok = set_status(cid, "rejected", note=str(data.get("note", "")))
            self._send(200 if ok else 404, {"ok": ok})
            return

        if path.startswith("/api/candidates/") and path.endswith("/reset"):
            cid = path[len("/api/candidates/"):-len("/reset")]
            ok = set_status(cid, "pending", note="")
            self._send(200 if ok else 404, {"ok": ok, "status": "pending"})
            return

        if path == "/sms":
            c, resp = sms_intake(data, getattr(self, "_body_raw", b""),
                                 getattr(self, "_body_how", ""),
                                 client=self.client_address[0], method="POST")
            self._send(c, resp)
            return

        self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        """**每条请求都记一行**到 access.log；非本机客户端同时打到 stdout。

        旧版这里是 `pass`，于是「手机到底有没有连上来」完全不可查 —— 排查时只能猜。
        只记请求行（不含正文），正文与拒收原因在 `_log_rejected` / `sms_intake` 里记。
        """
        try:
            cli = self.client_address[0] if self.client_address else "-"
            line = "%s | %-15s | %s\n" % (
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), cli,
                (fmt % args) if args else str(fmt))
            try:
                if (os.path.exists(ACCESS_LOG_PATH)
                        and os.path.getsize(ACCESS_LOG_PATH) > ACCESS_LOG_MAX):
                    os.replace(ACCESS_LOG_PATH, ACCESS_LOG_PATH + ".1")
            except Exception:
                pass
            with open(ACCESS_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
            if cli not in ("127.0.0.1", "::1", "localhost"):
                print("[REQ] " + line.rstrip(), flush=True)
        except Exception:
            pass


if __name__ == "__main__":
    load_sms()
    load_sms_hist()
    reload()
    # 上一轮若在投递中途被 kill，岗位会永久卡在「投递中」→ 启动时复位
    heal_interrupted_statuses()
    print("resume-bot running at http://0.0.0.0:%d  (candidates=%d)"
          % (PORT, len(CANDIDATES)))
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
