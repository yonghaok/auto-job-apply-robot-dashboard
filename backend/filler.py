"""详情页自动录入引擎（断点4）—— WorkBuddy 本地 computer-use。

按用户要求：**不接入任何外部 AI 模型**（不适用额外 ai 模型），
直接用 WorkBuddy 的 computer-use 能力（Playwright 驱动 Chrome）代替 AI 接入。

实现方式（确定性，非 LLM 推理）：
  1. 用 Playwright 打开投递入口页，采集全部可填控件（input / textarea / select / radio 组 / file）。
  2. 用「个人信息库字段 ↔ 表单字段」的**中英文别名表**做打分匹配（越长越具体的别名权重越高）。
  3. 贪心一对一分配（match_fields，纯函数、可单测），逐项填入；
     `<select>` 选最优 option；radio 组按文案选中。
  4. 上传简历 PDF（若存在）。
  5. **停在可提交前的状态**——绝不点击提交 / 发送 / 确认投递等按钮。
  6. 返回「已填字段」与「未命中字段」清单，未命中部分交人工补。

与旧版的区别：不再依赖 llm_client / browser-use / langchain（无外部 AI），
未知表单不再报「LLM 未配置」，而是尽最大努力匹配后把未命中项如实列出。
"""

import hashlib
import json
import os
import re
import threading

# Playwright 的 **node 驱动**一旦出现「未被捕获的 Promise 拒绝」就会**整个进程退出**
# （Node >= 15 默认 `--unhandled-rejections=throw`；自带驱动实测就是 Node v24.21.0）。
# 实测触发点：
#   Protocol error (Page.handleJavaScriptDialog): No dialog is showing
# —— 「dialog 事件到了、但对话框已经被我们自己关掉」这个竞态在驱动内部抛出且无人接管。
# 驱动一死，所有还在等它的 worker **永久阻塞**（不抛错、不返回），看板就永远停在
# 「正在取消…」（server.py 的看门狗负责兜住这一层的**后果**，这里负责**不让它发生**）。
# 把未捕获拒绝降级成告警，驱动就不会为这种竞态整体崩掉。
# 注意用 `not get(...)` 而不是 `setdefault`：空字符串也算「已设置」，setdefault 会跳过。
if not os.environ.get("NODE_OPTIONS"):
    os.environ["NODE_OPTIONS"] = "--unhandled-rejections=warn"

PROFILE_PATH = os.environ.get(
    "RESUME_PROFILE",
    os.path.join(os.path.dirname(__file__), "profile.json"),
)

# ============================ 并发安全：两个闸门 ============================
#
# 2026-09-21 用户反馈的两个真实缺陷，都出在「并发 + 每岗位全新会话」上：
#
# 缺陷 1 · 多并发登录会**串码**
#   一键投递 3 并发时，多家公司几乎同时点「获取验证码」，验证码几乎同时到达。
#   而**不同厂商的发送延迟本来就不一样**，所以「按时间取最新一条」必然串码。
#   更糟的是 sms_matcher 在一条都没命中公司名时会**回退到不过滤**，等于放弃防护。
#   → 解法：全局登录闸门 _LOGIN_GATE。同一时刻**只允许一个岗位处于登录/取码阶段**，
#     前一个登录结束（成功或失败）后，下一个岗位才开始登录。
#     填表与提交**不占**该闸门，仍可并发。
#
# 缺陷 2 · 同一公司多岗位要**反复登录**
#   原来每条岗位都 browser.new_context()，cookie/localStorage 用完即丢。
#   → 解法：按**公司**用 launch_persistent_context(profile_dir) 持久化会话磁盘落盘；
#     同公司下一个岗位打开时若已是登录态，直接跳过整个登录段（不发短信、不取码）。
#
# 注意：Chromium 不允许同一个 user_data_dir 被两个实例同时打开（SingletonLock 会报错），
# 所以每个 profile 目录还要一把**按公司**的互斥锁，保证同公司岗位串行。

_LOGIN_GATE = threading.Lock()      # 全局：同一时刻只有一个站点在登录/取码


# 「进入窗口」：HTTP 线程登记「某岗位要置前」，投递线程在等待循环里消费。
#
# 为什么不是单个全局 Event（2026-09-23 修）：提交段（park）是 **3 并发**的，
# 三个岗位可能同时停在各自的等待窗口里。若只用一个全局布尔，用户点 A 岗位的
# 「进入窗口」后，先跑到检查点的线程（可能是 B）会把**自己的**窗口置前并把请求
# 消费掉 —— A 永远等不到，用户却看到别家站点跳到前台。
# 故改为**按岗位 key（pid）登记**，并用 thread-local 记录「本线程正在处理哪个岗位」，
# 只有 key 命中该线程的 page 才置前。
_FOCUS_PENDING = set()              # 待置前的岗位 key（pid）
_FOCUS_LOCK = threading.Lock()
_CUR_TASK = threading.local()       # 投递线程当前处理的岗位 key

# ---- 标签页归属：谁开的标签页归谁（防「抢别家岗位的窗口」）----------------------
# 为什么必须有：CDP 模式下 `ctx` 是**用户真实浏览器**的默认 context，一键投递的并发
# 岗位**共享同一份 `ctx.pages`**。旧 `_adopt_new_tab` 只按「差集 + 取最后一个」认新
# 标签页，于是 A 岗位会把 B 岗位刚打开的标签页抢过来（还要把 A 自己那页关掉）——
# 实测症状就是用户报的「一号窗口的验证码被填进了三号窗口」。登录闸门挡不住这件事：
# 闸门只串行「谁在登录」，挡不住「谁的页面被换掉」。
_PAGE_OWNER = {}                    # page 对象 -> 岗位 key（"" = 无主）
_PAGE_LOCK = threading.Lock()


def _cur_key():
    """本线程当前岗位 key（未登记时返回空串）。"""
    return getattr(_CUR_TASK, "key", "") or ""


def claim_page(page, key=None):
    """登记「这个标签页是本岗位打开的」。key 缺省取线程当前的岗位 key。"""
    if page is None:
        return
    k = _cur_key() if key is None else str(key or "")
    with _PAGE_LOCK:
        _PAGE_OWNER[page] = k


def release_page(page):
    """解除登记（关页/收尾时调用，避免字典无限增长）。"""
    if page is None:
        return
    with _PAGE_LOCK:
        _PAGE_OWNER.pop(page, None)


def _claimable(page, key):
    """能不能收养这个新标签页：**无主**、或**本来就是我开的**。"""
    with _PAGE_LOCK:
        cur = _PAGE_OWNER.get(page, "")
    return (cur == "") or (cur == key)


def _try_claim(page, key):
    """原子抢占。并发下两个岗位同时看到一个无主新标签页时，只有一个能拿到。"""
    with _PAGE_LOCK:
        cur = _PAGE_OWNER.get(page, "")
        if cur == "" or cur == key:
            _PAGE_OWNER[page] = key
            return True
        return False


def _sweep_pages():
    """清掉已关闭页面的登记（否则 page 对象一直被字典持有）。"""
    with _PAGE_LOCK:
        for pg in list(_PAGE_OWNER):
            try:
                if pg.is_closed():
                    _PAGE_OWNER.pop(pg, None)
            except Exception:
                _PAGE_OWNER.pop(pg, None)


def request_focus(key=""):
    """前端「进入窗口」按钮：登记「该岗位的窗口要置前」。

    Playwright 的 page 只能在其所属线程操作，故这里只登记，
    真正的 bring_to_front 由该岗位的等待循环在同一线程内执行。
    返回 False 表示 key 为空（宁可不动，也不能去动别家的窗口）。"""
    key = str(key or "")
    if not key:
        return False
    with _FOCUS_LOCK:
        _FOCUS_PENDING.add(key)
    return True


def set_task_key(key):
    """投递线程登记「我此刻在处理哪个岗位」，供 _maybe_focus 精确匹配（防串窗）。"""
    _CUR_TASK.key = str(key or "")


def _maybe_focus(page):
    """等待循环内调用：**本岗位**有置前请求就把它的窗口带到最前。

    须在 page 所属线程执行；且只在 key 命中时才消费请求 ——
    否则并发投递下会把别家岗位的置前请求用在自己的窗口上。"""
    key = getattr(_CUR_TASK, "key", "")
    if not key:
        return False
    with _FOCUS_LOCK:
        if key not in _FOCUS_PENDING:
            return False
        _FOCUS_PENDING.discard(key)
    try:
        page.bring_to_front()
        return True
    except Exception:
        return False

_PROFILE_LOCKS = {}                 # profile_dir -> Lock
_PROFILE_LOCKS_GUARD = threading.Lock()
LOGIN_HOLDER = ""                   # 当前持锁的公司名（给看板显示「正在登录谁」）


def _lock_for(key):
    """取（必要时创建）某个 key 对应的锁。"""
    with _PROFILE_LOCKS_GUARD:
        lk = _PROFILE_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _PROFILE_LOCKS[key] = lk
        return lk


def profile_path_for(company):
    """按公司名得到**持久化浏览器会话**目录（根目录可用 RESUME_PROFILES 覆盖）。

    目录名 = 清洗后的公司名 + 公司名 md5 前 8 位，兼顾可读性与唯一性（避免同名撞车）。
    """
    base = os.environ.get(
        "RESUME_PROFILES",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_profiles"))
    key = (company or "").strip() or "unknown"
    safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", key)[:40].strip("_") or "unknown"
    return os.path.join(base, "%s_%s" % (safe, hashlib.md5(key.encode("utf-8")).hexdigest()[:8]))


def _clean_singleton(profile_dir):
    """清掉 Chromium 的单例锁残留（上次异常退出会留下，导致本轮打不开会话目录）。"""
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            os.unlink(os.path.join(profile_dir, name))
        except OSError:
            pass


def _acquire(lock, note, what, tick=2, max_wait=1800):
    """带心跳地抢锁：**不静默卡住**，等待期间持续 note，让看板看得见在排队。"""
    if lock.acquire(blocking=False):
        return True
    note("等待%s（同一时刻只允许一个，前面还有岗位在跑）…" % what)
    waited = 0
    while waited < max_wait:
        if lock.acquire(timeout=tick):
            note("%s 已轮到本岗位（等待 %ds）" % (what, waited))
            return True
        waited += tick
        if waited % 20 == 0:
            note("仍在等待%s（已等待 %ds）…" % (what, waited))
    return False


def _is_cancelled(cb):
    """统一的「取消」检查：cb 为 None 时恒为 False。

    取消是**协作式**的 —— 只在明确的检查点生效，绝不硬杀线程，
    避免在「表单填了一半 / 提交点下去一半」这种不可控状态下突然断掉。
    """
    if not cb:
        return False
    try:
        return bool(cb())
    except Exception:      # 取消回调自身出错绝不能影响投递
        return False


def _browser_channel():
    """用哪个浏览器内核打开**回退**会话：默认 `msedge`。

    ⚠️ 回退路径用的是**项目私有会话目录**，它**不会**继承主 Edge 的登录态 ——
    Edge 的 v20(App-Bound Encryption) cookie 无法跨目录搬运（实测证伪：副本里一条都
    解不开，还会被清库；详见 `edge_cdp` 模块说明）。
    想继承登录态要用 **CDP 接管**（`edge_cdp.connect`），见 `_open_session`。
    可用 RESUME_BROWSER_CHANNEL 覆盖（chrome / msedge / msedge-beta / chromium）。
    """
    return (os.environ.get("RESUME_BROWSER_CHANNEL", "msedge") or "msedge").strip()


def _launch_persistent(p, profile_dir, headless, note):
    """打开**项目私有**的持久化浏览器上下文（按公司复用登录态）。

    ⚠️ 这条路径**不继承主 Edge 的登录态**：Edge 的 cookie 是 v20(App-Bound Encryption)，
    实测无法跨目录搬运（副本里一条都解不开，且会被 Edge 清库）——详见 `edge_cdp` 模块说明。
    要继承主 Edge 登录态请用 CDP 接管（`_open_session` 会**优先**尝试）。
    本函数只作**回退**：CDP 不可用时仍能按公司持久化会话（首次登录后同公司岗位免登录）。
    """
    try:
        os.makedirs(profile_dir, exist_ok=True)
    except OSError:
        pass
    args = ["--no-sandbox", "--start-maximized",
            "--no-first-run", "--no-default-browser-check"]
    ch = _browser_channel()
    try:
        return p.chromium.launch_persistent_context(
            profile_dir, channel=ch, headless=headless,
            args=args, no_viewport=True)
    except Exception as e:
        note("持久化会话打开失败（%s: %s），清理 Chromium 单例锁后重试一次"
             % (type(e).__name__, e))
        _clean_singleton(profile_dir)
        return p.chromium.launch_persistent_context(
            profile_dir, channel=ch, headless=headless,
            args=args, no_viewport=True)


def _make_cdp_closer(browser):
    """CDP 模式收尾：只**断开连接**，绝不关闭用户真实浏览器。"""
    def closer():
        try:
            browser.close()      # 对 CDP 连接 = 断开，不会杀掉 Edge 进程
        except Exception:
            pass
    return closer


def _profile_ready(profile_dir):
    """该公司是否**已有可复用会话**（登录态就落在这个目录里）。

    判据保守：目录存在且已有 Chromium 的资料结构（`Default/`）。
    宁可真机多登录一次，也不要把「空目录」当已登录 → 那会白等登录段。
    """
    try:
        return os.path.isdir(profile_dir) and os.path.isdir(
            os.path.join(profile_dir, "Default"))
    except Exception:
        return False


def _open_session(p, profile_dir, headless, note, company=""):
    """打开投递用的浏览器会话，返回 `(ctx, via, closer)`；`via` ∈ {"cdp","persistent"}。

    **优先「按公司的私有持久会话」**（2026-10-28 修正）：
      登录态（cookie/localStorage）就落在这个目录里，同公司第 2 个岗位直接免登录。
      ⚠️ 旧注释说「CDP 继承主 Edge 登录态」是**错的**：CDP 实例用独立资料目录
      （`browser_profiles/_edge_cdp`），拿不到主 Edge 的 v20 加密 cookie。
      所以若优先 CDP，**所有公司都要重新登录一遍**（28 个岗位逐个短信，不可接受）。

    **CDP 只作新公司的入口**：该公司还没私有会话时，接管调试实例
    （`via="cdp"` 时绝不关 context，只关本次标签页 —— 窗口不会因为收尾而消失）。
    两者都不可用时仍回落到私有会话（会现场新建一个）。
    """
    if not _profile_ready(profile_dir):
        try:
            import edge_cdp
            b, ctx, err = edge_cdp.connect(p)
            if ctx is not None:
                note("该公司还没有私有会话 → 先用调试端口接管 Edge（%s）"
                     % edge_cdp.endpoint())
                return ctx, "cdp", _make_cdp_closer(b)
            if err:
                note("调试端口未就绪（%s）→ 改开项目私有会话（本岗位需登录一次）" % err)
        except Exception as e:      # CDP 失败绝不影响投递
            note("调试端口不可用（%s: %s）→ 改开项目私有会话" % (type(e).__name__, e))
    else:
        note("复用「%s」已保存的会话与登录态（同公司后续岗位免重复登录）"
             % (company or os.path.basename(profile_dir)))
    ctx = _launch_persistent(p, profile_dir, headless, note)
    return ctx, "persistent", ctx.close

# 个人信息库字段路径 -> 表单字段别名（中英文皆可；越长越具体、权重越高）
FIELD_ALIASES = {
    "name": ["姓名", "名字", "真实姓名", "您的姓名", "full name", "name"],
    "gender": ["性别", "gender", "sex"],
    "phone": ["手机号", "手机号码", "移动电话", "联系电话", "手机", "phone", "mobile", "tel"],
    "email": ["电子邮箱", "电子邮件", "邮箱", "email", "e-mail", "mail"],
    "birth": ["出生年月", "出生日期", "生日", "date of birth", "birth"],
    "location": ["所在城市", "现居城市", "期望城市", "城市", "居住地", "所在地点",
                  "所在地区", "现居地", "city", "location"],
    # T72：真实站点会写「意向工作城市 / 期望就业城市」（全志校招页）——原来的别名一个都不命中，
    # 而 `location` 里的**裸「城市」**会把它抢走（结果填成现居城市）。补齐别名 + 给 location 加否定词。
    "intended_city": ["首选工作城市", "首选城市", "意向城市", "期望工作城市", "期望城市",
                      "求职意向城市", "目标城市", "工作城市意向",
                      "意向工作地", "期望工作地", "意向工作城市", "就业城市",
                      "期望就业城市", "意向就业城市", "期望工作地点", "意向工作地点",
                      "意向地点", "期望地点"],
    # T78：飞书招聘写「家乡」「所在地点」「期望工作地点」——原别名一个都不命中
    "native_place": ["籍贯", "祖籍", "户口所在地", "户籍", "户籍所在地", "生源地", "生源所在地",
                     "家乡", "老家", "native place"],
    "current_addr": ["现居地址", "现居住地址", "居住地址", "居住城市", "通讯地址", "家庭住址", "联系地址", "邮寄地址", "收件地址"],
    "school_addr": ["学校地址", "院校地址", "学校所在地址", "院校所在地址", "校区地址"],
    "id_card": ["身份证号", "身份证", "证件号码", "证件号", "id number"],
    "accept_adjust": ["是否接受调剂", "是否服从调剂", "接受调剂", "服从调剂", "调剂意向", "调剂", "接受岗位调剂"],
    # T70：**具体别名放前面**。原先只有裸「学校」(len2)，与同页的「学校类别」打平手，
    # greedy 会把学校名分给「学校类别」→ 用户报的「很多信息填写不正确」。
    "education.school": ["学校名称", "院校名称", "就读学校", "学校名",
                         "毕业院校", "毕业学校", "就读院校", "院校", "学校",
                         "school", "university"],
    "education.degree": ["最高学历", "学历", "学位", "教育程度", "degree", "education level"],
    "education.major": ["专业名称", "就读专业", "所学专业", "主修专业", "专业方向",
                        "专业", "major"],
    "education.grad_year": ["毕业年份", "毕业时间", "预计毕业", "毕业年月", "graduation year"],
    "education.gpa": ["平均绩点", "绩点", "gpa", "GPA"],
    # 就读时间：全志/MokaHR 的「教育背景」必填项，原先**根本没有路径接它**
    "education.period": ["就读时间", "在校时间", "就读起止", "在校起止", "教育起止",
                        "就读期间", "起止时间"],
    "self_intro": ["自我评价", "个人优势", "个人简介", "自我介绍", "个人陈述", "summary", "about me"],
    "skills": ["专业技能", "技能特长", "技能", "擅长", "skills"],
    "experiences": ["工作经历", "实习经历", "工作经验", "实习/工作经历", "项目与经历"],
    "projects": ["项目经历", "项目经验", "projects"],
    "awards": ["获奖情况", "奖项荣誉", "荣誉奖项", "awards"],
}

# ============ 可重复区块（项目经历 / 工作经历 的「新增项目」）—— T68 ============
# 用户报（2026-09-23）：「简历信息填写页**只会填写一个项目**，**需要点击新增项目**添加更多项目经历」。
# 根因两层：
#   ① `match_fields` 是「一个 profile 路径**最多用一次**」的贪心一对一 → `projects` 只命中
#      **第一个**区块里的一个控件，第 2/3 个项目无处可去；
#   ② 招聘站的「项目经历」是**可重复区块**：默认只有 1 块，要**点站点的「新增项目」按钮**
#      才会长出第 2、3 块。
# 修法：从命中该 section 的控件往上找**「最近的、含新增按钮且按钮不在本块内」的祖先**当区块容器；
#   点「新增」按钮把区块数补到与 profile 条目数一致；再把**第 k 条**填进**第 k 块**。
#   ⚠️ 区块数量补不齐 / 找不到新增按钮 → **如实收手并回落到旧逻辑**，绝不谎报。
_LIST_PATHS = ("education", "experiences", "projects")
_MAX_ADD_CLICKS = 8          # 最多点几次「新增」（防站点按钮永远不长出区块时死循环）
_ADD_SETTLE_MS = 400         # 点完「新增」等站点渲染新块

# 列表条目**内部**各子字段的表单别名（越具体越靠前；打分取**最长命中**，
# 所以「项目时间」会被判给 period 而不是 name 的「项目」）
ITEM_ALIASES = {
    "experiences": {
        "company": ["公司名称", "公司", "单位", "企业", "company"],
        # 实习经历里的「工作地点」（T70 补；个人信息库目前没有逐条 location → 会如实报未填）
        "location": ["工作地点", "工作城市", "所在地", "实习地点"],
        "role": ["职位名称", "职位", "岗位", "职务", "role", "title"],
        "period": ["在职时间", "起止时间", "工作时间", "时间", "period", "date"],
        "highlights": ["工作内容", "工作描述", "工作职责", "职责描述", "职责", "业绩",
                       "描述", "内容"],
    },
    "projects": {
        "name": ["项目名称", "项目名", "项目", "name"],
        # T71（文远知行 / MokaHR）：项目块里「担任角色」这一栏的标题就是**单行**「职责」
        # （`<input maxlength=255>`），另一栏「项目中职责」才是长文本。
        # 原来 role 只认「角色/担任/职位」→ 这一栏**永远空着**（我们手里其实有「产品负责人」）。
        # 补上「职责」不会伤 desc：desc 靠更长的「项目描述」取胜（且 desc 的「职责」下标与 role 相同，
        # 同分时按控件顺序稳定排序，role 先取到它自己的栏）。
        "role": ["担任角色", "项目角色", "角色", "担任", "职位", "role", "职责"],
        "period": ["项目时间", "起止时间", "项目周期", "时间", "period", "date"],
        "desc": ["项目描述", "项目内容", "项目职责", "项目介绍", "描述", "内容", "职责"],
    },
    # 教育背景：个人信息库里是**单个 dict**（不是 list）→ 由 `_section_items` 归一成 1 条
    "education": {
        "school": ["学校名称", "院校名称", "就读学校", "毕业院校", "学校", "school"],
        "major": ["专业名称", "所学专业", "就读专业", "专业", "major"],
        "degree": ["学历", "最高学历", "学位", "degree"],
        "period": ["就读时间", "在校时间", "起止时间", "在校起止", "时间", "period"],
    },
}
# 「新增/添加」类按钮文案（找可重复区块的添加入口；宁可多列，命中后再用 DOM 结构校验）
ADD_BTN_TEXTS = ["新增项目", "添加项目", "新增经历", "添加经历", "新增工作", "添加工作",
                 "新增一条", "添加一条", "继续添加", "再添加", "新增", "添加", "＋", "+"]

# 采集文本类控件（每次调用都**重新**打 data-filler-idx；SPA 重渲染会抹掉旧标记，
# 所以「点新增 → 重采集 → 重分析」是循环里必须做的一步）
COLLECT_JS = """els => els.map((e, i) => {
    const tag = e.tagName.toLowerCase();
    const type = (e.getAttribute('type') || '').toLowerCase();
    // 打唯一标记，供 _selector_for 精确定位（见那里的注释：只有 placeholder
    // 的控件用兜底选择器会永远命中第 1 个 → 静默填错）
    try { e.setAttribute('data-filler-idx', String(i)); } catch (_) {}
    // T72：**先把「值文本 / 校验提示」从要读的文本里彻底摘掉**。
    // MokaHR 这类站把已选值或「必填项未填写 / 暂无选项」渲染在 `[class*=display-value]`
    // 或 `.sd-Input-message.sd-Input-error` 里，而后者**往往就在包着 input 的那个 `<label>` 内部**
    // → 直接读 `label.innerText` 会拿到「必填项未填写」，该字段从此再也匹配不上
    // （实测：现家庭住址 / 期望就业城市 的 label 变成「必填项未填写」，项目块变成「2024 起止时间」）。
    // 规则：① 摘掉 option/select 文案（T71b）；② 摘掉 display/提示/校验类节点；
    //      ③ 剥首尾装饰字符；④ 整段就是一个提示语 → 视为「没有标题」，继续往上找（T72）。
    const _KILL = 'option,select,[class*=display],[class*=placeholder],[class*=error],' +
      '[class*=hint],[class*=tip],[class*=help],[class*=validat],[class*=feedback],[class*=required]';
    const _HINT = /^(必填项未填写|暂无选项|请选择|请输入|请填写|未填写|必填|选填|\\d+\\s*\\/\\s*\\d+)$/;
    const _cleanNode = (node) => {
      try {
        if (node.matches && node.matches(_KILL)) return '';    // 节点**自身**就是提示/值容器
        const c = node.cloneNode(true);
        c.querySelectorAll(_KILL).forEach(x => { try { x.remove(); } catch(_){} });
        const t = ((c.innerText || c.textContent || '') + '').replace(/\\s+/g, ' ');
        return t.replace(/^[^\\w\\u4e00-\\u9fa5]+|[^\\w\\u4e00-\\u9fa5]+$/g, '').trim();
      } catch (_) { return ''; }
    };
    // 标题优先取「剥掉 option 文案 + 提示/值节点」的 label 文本（T71b/T72）：
    // 原生 <select> 被裸 <label> 包住时 `label.innerText` 会把**选项文案**
    // （「请选择 / 本科 / 硕士」）混进来；而校验失败时又会把「必填项未填写」混进来 ——
    // 两者都会污染 `_hay`（抢别名），并让下面的兄弟 div 兜底**误以为已有标题**而不再往上找
    // → 这类字段永远填不上。
    const labelOf = (lb) => _cleanNode(lb);
    let label = '';
    try { if (e.labels && e.labels[0]) label = labelOf(e.labels[0]); } catch(_) {}
    if (!label) label = e.getAttribute('aria-label') || '';
    if (_HINT.test(label)) label = '';          // T72：提示语不是标题
    // T78：Formily / UD（飞书招聘 atsx）—— 标题写在 `.ud-formily-item-label` 里，
    // 距 input **6~9 层**祖先，而下面的兄弟兜底只走 6 层 → 整页字段一个都认不出来
    // （实测蔚来：采集 18 个控件、命中 0 项）。这里**只认「标题节点」**，
    // 不像兄弟兜底那样可能把区块标题（「基本信息」）当成字段名。
    if (!label) {
      for (let _p = e.parentElement, _h = 0; _p && _h < 14; _p = _p.parentElement, _h++) {
        let _lb = null;
        try { _lb = _p.querySelector('label.form-item__text,[class*="form-item__text"],[class*="form-item-label"],[class*="formily-item-label"]'); } catch (_) {}
        if (!_lb || _lb.contains(e)) continue;
        const _t = _cleanNode(_lb);
        if (_t && !_HINT.test(_t)) { label = _t; break; }
      }
    }
    // 标题兜底（T71）：站点常把字段标题写在**兄弟 div**
    // （`<div class="apply-field"><div class="title"><span>公司名称</span></div><div class="ctrl"><input></div></div>`），
    // 而 label/aria-label 全为空、placeholder 又是通用文案（「内容」「请选择」）。
    // 不补这层 → 字段根本认不出来（实测：项目描述被判成「职责」、学历整列不填、手机区号被当标题）。
    if (!label) {
      for (let _p = e.parentElement, _h = 0; _p && _h < 6; _p = _p.parentElement, _h++) {
        let _t = '';
        for (const _c of _p.children) {
          try { if (_c.contains(e)) continue; } catch (_) { continue; }
          if (_c.tagName === 'INPUT' || _c.tagName === 'TEXTAREA' || _c.tagName === 'SELECT') continue;
          // 兄弟里若还装着别的表单控件，说明这一层是**区块**而不是单个字段的容器
          try { if (_c.querySelector && _c.querySelector('input,textarea,select')) continue; } catch (_) {}
          const _cleanTxt = _cleanNode(_c);
          if (!_cleanTxt || _HINT.test(_cleanTxt)) continue;   // 值文本/提示语一律不算标题（T72）
          _t += ' ' + _cleanTxt;
        }
        _t = _t.replace(/\\s+/g, ' ');
        // 去掉**首尾**的装饰字符（「-」「：」「*」（必填星号）这类），标题才干净
        _t = _t.replace(/^[^\\w\\u4e00-\\u9fa5]+|[^\\w\\u4e00-\\u9fa5]+$/g, '').trim();
        // 标题必须短、且含实义字符（否则会把「-」「至今」这类装饰当成标题）
        if (_t && _t.length <= 20 && /[\\w\\u4e00-\\u9fa5]/.test(_t)) { label = _t; break; }
      }
    }
    // 「年月区间」控件（T70）：4 个日期框只有 placeholder 年/月 → 补合成标签；
    // 若上面已取到真标题（「就读时间」这类），就不必再补（免得标签变成「就读时间 起止时间」）。
    // T74：年月框识别**不能只看 placeholder** —— 站点填过值后会把 placeholder 抹掉
    // （MokaHR 实测），只按 placeholder 判会让已填过的年月框丢掉合成标签 → 掉出区块、
    // 再也配不上（repair pass 反而把它写坏）。结构性补证：落在
    // `[class*=month-range]` / `[class*=date_info]` 行内时，按该行文本框顺序
    // 第 1/3 个视为「年」、第 2/4 个视为「月」。
    const _ymPos = (node) => {
      if (!node || node.tagName !== 'INPUT') return '';
      const p = (node.getAttribute('placeholder') || '').trim();
      if (/^(年|月|year|month|yyyy|mm)$/i.test(p)) return p;
      try {
        const rng = node.closest && node.closest('[class*=month-range],[class*=date_info]');
        if (!rng) return '';
        const ins = Array.prototype.filter.call(rng.querySelectorAll('input'),
          (x) => !x.type || x.type === 'text');
        const pos = ins.indexOf(node);
        return pos < 0 ? '' : ((pos % 2 === 0) ? '年' : '月');
      } catch (_) { return ''; }
    };
    if (_ymPos(e) && !/时间|起止|期间|日期/.test(label || ''))
      label = (label || '') + ' 起止时间';
    let opts = [];
    if (tag === 'select') {
        try { opts = Array.from(e.options).map(o => (o.textContent || '').trim()); } catch(_) {}
    }
    const _ph = e.getAttribute('placeholder') || '';
    return {
        tag, type, idx: i,
        label: (label || '').trim(),
        ph: _ph,
        nm: e.getAttribute('name') || '',
        id: e.getAttribute('id') || '',
        // T82：北森的标签在 9 层深处、下拉/日期是自定义控件 —— 必须把**类名**带回 Python，
        // 由 `_robust_fill_field` 分流（否则只会 page.fill 往显示层 input 里写，站点不认）。
        cls: (e.className || '').toString(),
        // 顶部「搜索职位关键词」既不是字段也不是标题：不标出来会被当成「职位/职务」填进去
        // （实测虹软：projects#1.role 的值被写进了这个搜索框）。
        search: !label && /搜索/.test(_ph),
        opts
    };
})"""

# 分析「可重复区块」：数现在有几块 + 找「新增」按钮 + 回报每块内的控件（按 data-filler-idx）。
#
# 区块怎么数（关键）：用 `PRIMARY_ALIASES`（「一块里只有一条」的代表字段，如「项目名称」）
# 的控件个数。**不要**数「容器的子元素」——真实站点常有一层 wrapper 包住条目列表
# （`section > .items > .item ×N`），按子元素数会永远数到 1 块，点多少「新增」都不长。
#
# 每一块的边界用「块盒子 boxOf」：从该块的代表控件往上走，**第一个含 ≥2 个 section 子字段
# 控件的祖先**就是这一块的盒子。第 k 块的控件 = 盒子 k 里的全部 section 子字段控件
# （这样「套 wrapper / 平铺」都对，也自动把区块外的同名控件排除掉）。
#
# 盒子的第二个用途：**别点错按钮**。有些站点把「新增」放在**最后一块内部**（「+ 添加一行」），
# 那不是 section 的入口 —— 按钮只要落在**任何盒子里**就跳过、换上一层祖先再找。
# ⚠️ 早先版本用「只含 1 个代表控件的祖先」判按钮位置，只有 1 块时会把 section 级按钮
#    误判成块内按钮 → 永远点不动（本地夹具实测），故改为盒子判定。
ANALYZE_JS = """(arg) => {
  const path = arg.path, addTexts = arg.addTexts;
  const low = (arr) => (arr || []).map(s => String(s).toLowerCase()).filter(Boolean);
  const primAliases = low(arg.primaryAliases), secAliases = low(arg.secAliases);
  const primaryReject = low(arg.primaryReject);
  const all = Array.from(document.querySelectorAll('[data-filler-idx]'));
  // T72：与 COLLECT_JS 同一条规则 —— **值文本 / 校验提示不算标题**。
  // 两份 JS 各自独立执行，改一处不改另一处会变成「区块认得、配对配不上」，所以必须同步。
  const _KILL = 'option,select,[class*=display],[class*=placeholder],[class*=error],' +
    '[class*=hint],[class*=tip],[class*=help],[class*=validat],[class*=feedback],[class*=required]';
  const _HINT = /^(必填项未填写|暂无选项|请选择|请输入|请填写|未填写|必填|选填|\\d+\\s*\\/\\s*\\d+)$/;
  const _cleanNode = (node) => {
    try {
      if (node.matches && node.matches(_KILL)) return '';
      const c = node.cloneNode(true);
      c.querySelectorAll(_KILL).forEach(x => { try { x.remove(); } catch(_){} });
      const t = ((c.innerText || c.textContent || '') + '').replace(/\\s+/g, ' ');
      return t.replace(/^[^\\w\\u4e00-\\u9fa5]+|[^\\w\\u4e00-\\u9fa5]+$/g, '').trim();
    } catch (_) { return ''; }
  };
  // T74：与 COLLECT_JS 同一条规则（两份 JS 各自独立执行，必须同步）。
  const _ymPos = (node) => {
    if (!node || node.tagName !== 'INPUT') return '';
    const p = (node.getAttribute('placeholder') || '').trim();
    if (/^(年|月|year|month|yyyy|mm)$/i.test(p)) return p;
    try {
      const rng = node.closest && node.closest('[class*=month-range],[class*=date_info]');
      if (!rng) return '';
      const ins = Array.prototype.filter.call(rng.querySelectorAll('input'),
        (x) => !x.type || x.type === 'text');
      const pos = ins.indexOf(node);
      return pos < 0 ? '' : ((pos % 2 === 0) ? '年' : '月');
    } catch (_) { return ''; }
  };
  // 标题兜底（T71）：与 COLLECT_JS 里那份必须一致 —— 标题在**兄弟 div** 里。
  const nearTitle = (el) => {
    // T82：北森 Phoenix 的标题是 `label.form-item__text`，距输入框 **9 层**
    // （兄弟兜底只走 6 层）→ 学历/城市/日期/学校名称 全读成空，可重复区块连代表字段
    // 都数不出来（实测虹软：education 直接 ok:false）。与 COLLECT_JS 保持同一套解析。
    for (let p = el.parentElement, h = 0; p && h < 14; p = p.parentElement, h++) {
      let lb = null;
      try { lb = p.querySelector('label.form-item__text,[class*="form-item__text"],'
        + '[class*="form-item-label"],[class*="formily-item-label"]'); } catch (_) {}
      if (!lb || lb.contains(el)) continue;
      const t = _cleanNode(lb);
      if (t && !_HINT.test(t)) return t;
    }
    for (let p = el.parentElement, h = 0; p && h < 6; p = p.parentElement, h++) {
      let t = '';
      for (const c of p.children) {
        try { if (c.contains(el)) continue; } catch (_) { continue; }
        if (c.tagName === 'INPUT' || c.tagName === 'TEXTAREA' || c.tagName === 'SELECT') continue;
        try { if (c.querySelector && c.querySelector('input,textarea,select')) continue; } catch (_) {}
        const _ct = _cleanNode(c);
        if (!_ct || _HINT.test(_ct)) continue;                 // T72
        t += ' ' + _ct;
      }
      t = t.replace(/\\s+/g, ' ');
      t = t.replace(/^[^\\w\\u4e00-\\u9fa5]+|[^\\w\\u4e00-\\u9fa5]+$/g, '').trim();
      if (t && t.length <= 20 && /[\\w\\u4e00-\\u9fa5]/.test(t)) return t;
    }
    return '';
  };
  const hayOf = (el) => {
    let s = '';
    let hasLbl = false;
    try {
      if (el.labels && el.labels[0]) {
        // T71b/T72：与 COLLECT_JS 一致 —— 剥掉 option 文案**和值/提示节点**，否则
        // 「裸 label 包 select」或「label 内含『必填项未填写』」会被当成「已经有标题」，
        // 标题兜底不再生效（两份 JS 必须同步）。
        const _l = _cleanNode(el.labels[0]);
        if (_l && !_HINT.test(_l)) { s += ' ' + _l; hasLbl = true; }
      }
    } catch(_){}
    const _al = ((el.getAttribute('aria-label') || '') + '').trim();
    if (_al) { s += ' ' + _al; hasLbl = true; }
    if (!hasLbl) s += ' ' + nearTitle(el);      // T71：兄弟 div 里的标题
    const _ph = (el.getAttribute('placeholder') || '').trim();
    s += ' ' + _ph;
    // 同上：年月框补合成 token，才能被算进本 section 的子字段（T70）；
    // T74：改用 _ymPos —— 已填过的年月框 placeholder 已被抹掉，只能靠结构补证。
    if (_ymPos(el)) s += ' 起止时间 period time date';
    s += ' ' + (el.getAttribute('name') || '');
    s += ' ' + (el.getAttribute('id') || '');
    return s.toLowerCase();
  };
  const hit = (hay, alist) => alist.some(a => hay.indexOf(a) >= 0);
  // 代表字段＝**具体标题**命中，且标题含否定词的一律不算
  // （T71：原来别名里放了裸「公司」→ 申请信息区的「最近公司」冒充一块 → 第 3 条经历整条丢失）。
  // ⚠️ **不要**再加「裸词回退」这一层：一整段「项目经历」大 textarea 会在弱层命中而被当成 1 块，
  // 把项目名填进大 textarea、第 2/3 条丢掉（smoke_repeat_section B6/D2 钉住这条）。
  const primHit = (e) => {
    const h = hayOf(e);
    if (hit(h, primaryReject)) return 0;
    return hit(h, primAliases) ? 1 : 0;
  };
  const prim = all.filter(e => primHit(e) === 1);
  if (!prim.length) return {ok: false, why: 'no-primary-field'};   // 数不清块 → 不敢动
  const secAll = all.filter(e => hit(hayOf(e), secAliases));
  const boxOf = (anchorEl) => {
    for (let p = anchorEl; p; p = p.parentElement) {
      let n = 0;
      secAll.forEach(e => { if (p.contains(e)) n++; });
      if (n >= 2) return p;
    }
    return anchorEl;                 // 块里只有一个子字段 → 盒子就是它自己
  };
  const boxes = prim.map(boxOf);
  const txt = (el) => (((el.innerText || el.textContent) || '') + '').trim();
  const depth = (el) => { let d = 0; for (let q = el; q; q = q.parentElement) d++; return d; };
  const isAdd = (el) => {
    const t = txt(el);
    if (!t || t.length > 12) return false;
    // 包着输入控件的容器不是按钮（避免点到整块表单容器）
    try { if (el.querySelector && el.querySelector('input,textarea,select')) return false; } catch(_){}
    return addTexts.some(a => t.indexOf(a) >= 0);
  };
  let btn = null;
  for (let p = prim[0].parentElement; p && p !== document.body; p = p.parentElement) {
    const cands = Array.from(p.querySelectorAll('button,a,[role="button"],span,div,i'))
      .filter(e => isAdd(e) && !e.contains(prim[0]) && !prim[0].contains(e));
    if (!cands.length) continue;
    // 文案最短 + DOM 最深 → 最可能是那个真按钮，而不是包着它的大容器
    cands.sort((x, y) => (txt(x).length - txt(y).length) || (depth(y) - depth(x)));
    if (boxes.some(bx => bx.contains(cands[0]))) continue;  // 落在某块内部 → 不是 section 入口
    btn = cands[0];
    break;
  }
  const blocks = boxes.map(bx => secAll.filter(e => bx.contains(e))
                                       .map(e => parseInt(e.getAttribute('data-filler-idx'), 10)));
  if (!btn) return {ok: true, nblocks: prim.length, blocks: blocks,
                    addSel: null};       // 找不到「新增」→ 仍可按现有区块填，只是长不出新块
  try { btn.setAttribute('data-filler-add', path); } catch(_){}
  return {ok: true, nblocks: prim.length, blocks: blocks,
          addSel: '[data-filler-add="' + path + '"]'};
}"""


def _section_aliases(path):
    """该列表 section 在表单里可能出现的全部别名（section 名 + 各子字段名）。"""
    out = list(FIELD_ALIASES.get(path, []))
    for _k, als in ITEM_ALIASES.get(path, {}).items():
        out.extend(als)
    return out


def _collect_fields(page):
    """采集页面文本类控件（radio/checkbox/file/hidden/按钮都排除）。"""
    fields = page.eval_on_selector_all("input,textarea,select", COLLECT_JS)
    return [f for f in fields
            if not f.get("search")
            and (f["tag"] == "textarea"
                 or f["tag"] == "select"
                 or (f["tag"] == "input" and f["type"] not in
                     ("file", "radio", "checkbox", "submit", "button", "hidden")))]


def _section_items(profile, path):
    """取某 section 的条目列表。

    `education` 在个人信息库里是**单个 dict**（不是 list），直接 `profile.get(path)`
    会拿到 dict 的 keys（字符串）→ 全被过滤掉 → 白白不处理。统一在这里归一。
    """
    v = (profile or {}).get(path)
    if isinstance(v, list):
        return [it for it in v if isinstance(it, dict)]
    if isinstance(v, dict):
        return [v] if any(v.values()) else []
    return []


def _blank_section_flat(flat, path, keys=()):
    """section 已按条目逐块填完 → 把对应的一对一路径清空，别让旧逻辑再填一遍。

    ⚠️ `education` 是 dict 型 section，它的 path 本身不在 `FIELD_ALIASES` 里，
    真正要清的是 `education.school` 这种**子路径**（原来只清 `flat[path]`）。
    """
    for k in keys:
        p = "%s.%s" % (path, k)
        if p in flat:
            flat[p] = ""
    if path in flat:
        flat[path] = ""


def _item_pairs(path, controls):
    """块内控件 → 子字段 key 的贪心一对一（分数=最长别名命中长度）。纯函数。"""
    pairs = []
    for f in controls:
        hay = _hay(f)
        if not hay.strip():
            continue
        for key, als in ITEM_ALIASES.get(path, {}).items():
            # T70：块内同样要过**否定规则** —— 块里也可能混着「…成绩排名 / …人数」这类
            # 同族字段（实测「学校类别」会跟「学校名称」抢 school），一命中就会填错。
            if _rejected("%s.%s" % (path, key), f):
                continue
            best, rank = 0, 0
            for _i, a in enumerate(als):
                al = a.lower()
                if al in hay:
                    s = len(al)
                    if hay.strip() == al or (f.get("label") or "").strip().lower() == al:
                        s += 10
                    if s > best:            # 同分保留**更靠前**的别名（别名表就是优先级表）
                        best, rank = s, _i
            if best:
                pairs.append((best, rank, key, f))
    # 分数 → 别名表内先后 → 别名条数（越具体越优先）。T71 教训：同分时按 DOM 顺序会让
    # 「职责」抢在「内容」前面（两者都恰好等于 placeholder）→ 项目描述被写进「职责」栏。
    pairs.sort(key=lambda x: (-x[0], x[1], -len(ITEM_ALIASES[path][x[2]])))
    used_k, used_f, out = set(), set(), []
    for s, _rank, key, f in pairs:
        if key in used_k or id(f) in used_f:
            continue
        used_k.add(key)
        used_f.add(id(f))
        out.append((key, f, s))
    return out


# 「一块里只有一条」的代表字段 —— 用它数「现在有几块」。
# 为什么不数「容器的子元素」：真实站点常有一层 wrapper 把条目列表包起来
# （`section > .items > .item ×N`），按子元素数会永远数到 1 块（点多少新增都不长）。
# 找不到代表字段 → **整体放弃并回落旧逻辑**（宁可不动，也不要瞎点）。
# ⚠️ T71 教训（文远知行 / MokaHR）：**裸词会冒充代表字段**。
# 原来 `experiences` 里放了裸「公司」→ 申请信息区的「**最近公司**」（最近任职公司）也算一块，
# 于是 nblocks 从 1 变成 2：第 1 条（潜渊之心）被写进「最近公司」、真区块拿到第 2 条
# → 页面上表现为「实习经历**填漏** + 错位一格」，而且「新增」按钮因此点错位置。
# 所以分两层：**强**别名（具体标题）优先；一个都没有时才退到**弱**（裸词），
# 且任何一层都要过 `PRIMARY_REJECT`（标题含「最近/期望/类别」这类词的一律不算）。
PRIMARY_ALIASES = {
    "experiences": ["公司名称", "公司名", "单位名称", "单位名", "企业名称", "任职公司",
                    "所在公司", "实习公司"],
    "projects": ["项目名称", "项目名", "项目主题", "课题名称"],
    # ⚠️ 代表字段必须**足够独特**：不能放裸「学校」——「最高学历学校类别」也含「学校」，
    #    会把块数数错（这就是 T68 那条「数不清就放弃」的教训）。
    "education": ["学校名称", "院校名称", "就读学校", "毕业院校", "学校名"],
}
# ⚠️ **不要**再给代表字段加「弱层」（裸词回退）。T71 试过一版「强别名 + 裸词回退」，
# `smoke_repeat_section` B6/D2 立刻报出回归：一整段「项目经历」大 textarea（label 含「项目」）
# 在弱层命中 → 被当成 1 个可重复区块 → 把**项目名**填进大 textarea、第 2/3 条丢掉
# （旧回落逻辑本来是「3 条拼进一个 textarea」）。
# **原则（T68）：找不到可靠代表字段 → 整体放弃并回落旧逻辑，宁可不动也不瞎点。**
PRIMARY_REJECT = {
    "experiences": ["最近", "上一", "前一份", "当前", "目前", "期望", "意向", "目标",
                    "即将", "未来", "部门"],
    # ⚠️T72（全志 / MokaHR 实测）：这个页面的可重复区块是「**奖学金 / 竞赛**」，
    # 字段名是「奖学金项目名称 / 竞赛项目名称」——含「项目名称」四个字 → 被强别名命中
    # → projects「看似适用」→ 把**项目经历**写进奖学金栏；更糟的是代表控件各自算盒子
    # 时上溯到**整张表单**（>4 个控件的那层），于是项目数据连带覆盖了「实习#1 的工作职责」
    # 并把项目时间写进**教育行**的日期框。加否定词后 prim=0 → `_analyze_repeat` 返回 None
    # → 该 section 如实交人工（「数不准就放弃」），不会再乱填。
    "projects": ["最近", "当前", "目前", "期望", "意向", "目标",
                 "奖学金", "奖助", "竞赛", "获奖", "荣誉", "大赛", "比赛"],
    "education": ["类别", "最近", "当前", "性质", "层次", "排名", "人数"],
}


def _analyze_repeat(page, path):
    """在当前页面上分析 `path` 的**可重复区块**：找「新增」按钮 + 数区块 + 每块内的控件。

    每次调用都**重新采集**（点过「新增」后页面变了、旧 `data-filler-idx` 全废）。
    返回 (res, fields)：res 为 None 表示「不适用 / 不敢动」。
    """
    fields = _collect_fields(page)
    try:
        res = page.evaluate(ANALYZE_JS, {
            "path": path, "addTexts": ADD_BTN_TEXTS,
            "primaryAliases": PRIMARY_ALIASES.get(path, []),
            "primaryReject": PRIMARY_REJECT.get(path, []),
            "secAliases": _section_aliases(path)})
    except Exception:                                 # noqa: BLE001
        return None, fields
    if not res or not res.get("ok"):
        return None, fields
    return res, fields


def _click_add(page, sel):
    """点站点的「新增/添加」。返回 (True, 用的方式) / (False, 失败原因)。

    为什么要三种方式：真实站点上这个按钮常在**固定头/折叠区**里，或只是个 `<span>` 外壳，
    `page.click` 会以 `TimeoutError` 收场 —— 实测（文远知行）就是这样：clicks=0、
    第 3 条经历整条没填。滚动后再点、再退到 JS 原生 `click()`，多数站点能救回来。
    """
    for how, fn in (
            ("click", lambda: page.click(sel, timeout=2500)),
            ("click+scroll", lambda: (page.locator(sel).scroll_into_view_if_needed(timeout=1500),
                                      page.locator(sel).click(timeout=2500))),
            ("js-click", lambda: page.eval_on_selector(
                sel, "el => { try { el.scrollIntoView({block:'center'}); } catch(_){} el.click(); }")),
    ):
        try:
            fn()
            return True, how
        except Exception:                             # noqa: BLE001
            continue
    return False, "click/scroll/js 都失败"


def _fill_repeat_section(page, profile, path, note=None):
    """点站点「新增」把 `path` 的区块补齐，再**逐条目**填入第 k 块。

    返回 None（不适用 / 失败 → 让旧一对一逻辑照常处理）或
    dict(report, fields, consumed, filled, failed, uncertain)。
    """
    items = _section_items(profile, path)
    if not items:                      # 没条目 → 不用管
        return None
    # 单条目**也**走这里：只要页面上有「项目名称」这类代表字段，就能把子字段填对；
    # 页面上没有代表字段（例如整段就一个大 textarea）→ ANALYZE_JS 返回 ok:false → 回落旧逻辑。
    res, fields = _analyze_repeat(page, path)
    if not res:
        return None

    want = len(items)
    cur = int(res.get("nblocks") or 0)
    clicks = 0
    # 没有「新增」按钮（addSel 为空）→ 不点，只按现有区块填
    add_ways, add_err = [], ""
    while cur < want and res.get("addSel") and clicks < _MAX_ADD_CLICKS:
        okc, howc = _click_add(page, res["addSel"])
        if not okc:
            add_err = howc                            # 点不动 → 如实记下，别假装补出了块
            break
        add_ways.append(howc)
        page.wait_for_timeout(_ADD_SETTLE_MS)
        clicks += 1
        nxt, fields = _analyze_repeat(page, path)
        if not nxt:
            break                       # 点了但按钮/区块结构变了 → 收手，别死循环
        res = nxt
        grown = int(res.get("nblocks") or 0)
        if grown <= cur:                # 点了却没长出新区块 → 收手
            cur = grown
            break
        cur = grown

    # 收尾用**最新页面状态**再采集一次（点击后 DOM 一定变了）
    fin, fields = _analyze_repeat(page, path)
    if fin:
        res = fin
    cur = int(res.get("nblocks") or 0)
    if cur < 1:
        return None

    # ---- 逐块填第 k 条 ----
    by_idx = {f["idx"]: f for f in fields}
    blocks = res.get("blocks") or []
    filled, failed, uncertain, consumed = [], [], [], set()
    for k in range(min(cur, want)):
        ctrls = [by_idx[i] for i in (blocks[k] if k < len(blocks) else []) if i in by_idx]
        if not ctrls:
            continue
        for key, f, _s in _item_pairs(path, ctrls):
            val = _stringify(items[k].get(key))
            if not val:
                continue
            label = f.get("label") or f.get("ph") or f.get("nm") or f.get("id")
            try:
                ok, how = _fill_field_smart(page, f, val, note=note)
            except Exception as e:                    # noqa: BLE001
                failed.append("%s#%d.%s(%s)" % (path, k + 1, key, type(e).__name__))
                continue
            consumed.add(f["idx"])
            if ok is True:
                filled.append("%s#%d.%s→%s" % (path, k + 1, key, label))
            elif ok == "uncertain":
                uncertain.append("%s#%d.%s→%s(%s)" % (path, k + 1, key, label, how))
            else:
                failed.append("%s#%d.%s→%s(%s)" % (path, k + 1, key, label, how))

    # 一条都没填上（块找到了、但子字段一个都没匹配上）→ **回落旧逻辑**，
    # 否则 fill_page 会把 flat[path] 清掉、等于把「本来还能拼成一段文本」的机会也弄丢了。
    if not filled and not uncertain:
        return None

    short = "" if cur >= want else "（⚠️ 只补出 %d 块，还有 %d 条没填）" % (cur, want - cur)
    report = ("可重复区块 %s：%d 条 → %d 块（点「新增」%d 次%s），已填 %d 项、失败 %d 项"
              "、待确认 %d 项%s"
              % (path, want, cur, clicks,
                 "，方式 %s" % "/".join(add_ways) if add_ways else "",
                 len(filled), len(failed), len(uncertain), short))
    if add_err:
        report += "；⚠️「新增」点不动（%s），第 %d 条起没有容器可填" % (add_err, cur + 1)
    if failed:
        report += "；失败明细 [%s]" % "，".join(failed)
    if uncertain:
        report += "；待确认明细 [%s]" % "，".join(uncertain)
    # 明细回传给 fill_page 汇总，让「已填 N 项」的口径保持完整（不是只算扁平字段）
    return {"report": report, "fields": fields, "consumed": consumed, "blocks": cur,
            "clicks": clicks, "addWays": add_ways,
            "filled": filled, "failed": failed, "uncertain": uncertain}


# 岗位筛选默认值（用户指定）：关键词**全部**命中 + 地域**至少一个**命中
POS_KEYWORDS_DEFAULT = ["产品经理", "本科"]
POS_CITIES_DEFAULT = ["北京", "上海", "杭州", "苏州", "南京", "广州", "深圳"]
# 可编辑的「匹配条件」持久化文件（前端「匹配条件」窗口保存到这里）
MATCH_RULES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "match_rules.json")
# 学历标识（岗位行元信息形如「上市公司硕士永州市」）
DEGREES = ["本科", "硕士", "博士", "大专", "专科", "中专", "不限"]
# 「投递」类控件文案（不同 ATS 站不同；用于定位岗位行，纯读取不点击）
POS_APPLY_TEXTS = ["投递", "立即投递", "投递简历", "申请职位", "立即申请",
                   "马上申请", "在线申请", "申请"]
# 岗位行里常见的「噪声尾缀」：截掉后即得到干净的岗位名。
# 只放**强噪声**（出现即说明岗位名已结束）；「校招/实习/正式」这类会出现在
# 岗位名内部的词**不放**，否则会把「【27届校招】AI产品经理」截成「【27届」。
POS_NOISE_MARKERS = ["发布于", "发布时间", "更新时间", "薪资", "面议", "待遇",
                     "工作地点", "岗位职责", "任职要求", "我们提供"]
# 独立成段的纯装饰词（切完之后若落在尾巴上就丢掉）
POS_DECOR_WORDS = {"热招", "急聘", "急招", "火爆", "推荐", "置顶", "最新",
                   "NEW", "new", "New", "HOT", "hot"}
_PLACEHOLDER_TITLE_RE = re.compile(r"^【(正文命中|未命中|占位)[^】]*】$")


def _known_cities():
    """已知城市名（默认 7 城 + 常见补充），用于从岗位行文本里抽取地域。"""
    extra = ["成都", "武汉", "西安", "重庆", "天津", "长沙", "合肥", "青岛",
             "厦门", "大连", "无锡", "宁波", "济南", "福州", "郑州", "东莞"]
    out = []
    for c in list(POS_CITIES_DEFAULT) + extra:
        if c not in out:
            out.append(c)
    return out


_CITY_STOP = {"城市", "都市", "本市", "该市", "地市", "省城", "上市", "集市",
              "超市", "夜市", "股市", "楼市", "车市", "市场", "市区"}


def _extract_cities(text):
    """从岗位行文本里抽取地域，返回「北京、上海」这样的串（可能为空）。

    策略：**先匹配已知城市名**（这是筛选真正关心的），再回退到「xx市/xx省」的
    通用写法并过一遍停用词。不做通用中文分词——没有词典时「上市公司硕士永州市」
    这类无分隔文本无法正确切出「永州市」，但这类值本来就不会命中筛选。
    """
    if not text:
        return ""
    found = []
    # ① 已知城市名（长名优先，避免「苏州」被前面的短名干扰）
    for c in sorted(_known_cities(), key=len, reverse=True):
        if c in text and c not in found:
            found.append(c)
    # ② 通用 xx市 / xx省
    if not found:
        for m in re.finditer(r"([\u4e00-\u9fa5]{2,4})([市省])", text):
            base = m.group(1)
            if base in _CITY_STOP or base.endswith("城"):
                continue
            cand = base + m.group(2)
            if cand not in found:
                found.append(cand)
            break
    bases = []
    norm = []
    for f in found:
        base = f.rstrip("市省")
        if base and base not in bases:
            bases.append(base)
            norm.append(f)
    return "、".join(norm)


def is_placeholder_title(t):
    return bool(_PLACEHOLDER_TITLE_RE.match((t or "").strip()))


# 扫描时容易误当成「岗位行」的字段标签/说明块（只在扫描结果里过滤，不影响表格行）
_SCAN_BAD_TITLES = {"职位职责", "岗位职责", "工作职责", "岗位名称", "岗位描述",
                    "职位描述", "职位名称", "任职要求", "岗位要求", "工作内容",
                    "岗位介绍", "职位详情", "查看更多", "加载更多"}


def is_usable_scanned_row(p):
    """判断扫描出来的一行是不是**真的岗位行**（不是说明块/字段标签）。

    不同 ATS 的列表页 DOM 差异极大：同一个「找最高层只含一个投递控件的祖先」策略，
    在有的站能精确切出岗位卡片，在有的站会把展开的职位详情块也切出来。这里用
    结构信号兜底：标题像字段标签、太短、或「既无地域又无学历」的一律丢弃。
    """
    t = (p.get("title") or "").strip()
    raw = (p.get("raw_title") or "").strip()
    if not t or len(t) < 4:
        return False
    if t.endswith("：") or t.endswith(":") or t in _SCAN_BAD_TITLES:
        return False
    if raw.endswith("：") or raw.endswith(":") or raw in _SCAN_BAD_TITLES:
        return False
    if not re.search(r"[\u4e00-\u9fa5]", t):
        return False
    # 岗位行通常带地域或学历等元信息；说明块往往两样都没有
    if not (p.get("city") or "").strip() and not (p.get("degree") or "").strip():
        return False
    return True


def clean_title(title, limit=44):
    """把冗长的岗位行文本压成可读的岗位名。

    表格标题常带整卡片正文：
      「急 【27届校招】AI产品经理（技术方向） 发布于 2026-09-09 中小企业客群经营体」
      「校招-AI产品经理（人才专项） 热招 北京正式产品 / 策划 / 项目 - 产品经理」
    规则：① 截到强噪声标记/日期前；② 去掉开头装饰词（急/急聘/热招）；
    ③ 若剩余仍是多段且首段已足够具体，只保留首段；④ 限长。
    """
    t = (title or "").replace("\n", " ").replace("\r", " ").strip()
    if not t:
        return ""
    # ① 强噪声标记
    for mk in POS_NOISE_MARKERS:
        i = t.find(mk)
        if i > 0:
            t = t[:i]
    # ① 日期（2026-09-09 或 2026/9/9）
    m = re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", t)
    if m and m.start() > 0:
        t = t[:m.start()]
    # ② 开头装饰词
    t = re.sub(r"^(急聘|急招|急|热招|推荐|NEW|new)\s*[·\-|/]?\s*", "", t.strip())
    t = re.sub(r"\s+", " ", t).strip(" -·|/、,")
    # ③ 砍掉「分类尾缀」：表格标题形如
    #    「校招-AI Native 产品经理 北京正式产品 / 策划 / 项目 - 产品经理 团队介绍…」
    #    遇到「含城市名 / 正式 / 全职」的分段，就说明岗位名到此结束。
    if " " in t:
        parts = [p for p in t.split(" ") if p]
        if len(parts) > 1:
            cut = None
            for i, p in enumerate(parts):
                if i == 0:
                    continue
                if (any(c in p for c in _known_cities())
                        or any(w in p for w in ("正式", "全职", "兼职", "社招"))):
                    cut = i
                    break
            if cut:
                keep = parts[:cut]
                # 去掉纯装饰尾巴（「…（人才专项） 热招」→「…（人才专项）」）
                while keep and keep[-1] in POS_DECOR_WORDS:
                    keep.pop()
                t = " ".join(keep).strip(" -·|/、,")
            elif len(parts[0]) >= 6:
                t = parts[0]
    # ④ 限长
    if len(t) > limit:
        t = t[:limit].rstrip() + "…"
    return t


def load_match_rules():
    """读取可编辑的「匹配条件」（关键词 + 城市）。

    优先级：环境变量 RESUME_POS_FILTER（运维覆盖）> match_rules.json（前端窗口保存）>
    内置默认。任一来源缺失/损坏都回退默认值，不会让看板崩。
    返回 (keywords:list[str], cities:list[str])，元素已 strip、去空。
    """
    import json as _json
    # 1) 环境变量最高优先（CI / 运维一键覆盖）
    env = os.environ.get("RESUME_POS_FILTER", "").strip()
    if env:
        try:
            d = _json.loads(env)
            if isinstance(d, dict):
                k = d.get("keywords"); c = d.get("cities")
                if (isinstance(k, list) and k) or (isinstance(c, list) and c):
                    return (list(k) if isinstance(k, list) and k else list(POS_KEYWORDS_DEFAULT),
                            list(c) if isinstance(c, list) and c else list(POS_CITIES_DEFAULT))
        except Exception:
            pass
    # 2) match_rules.json（前端「匹配条件」窗口持久化）
    try:
        if os.path.exists(MATCH_RULES):
            with open(MATCH_RULES, "r", encoding="utf-8") as f:
                d = _json.load(f)
            if isinstance(d, dict):
                kws = [str(x).strip() for x in (d.get("keywords") or []) if str(x).strip()]
                cts = [str(x).strip() for x in (d.get("cities") or []) if str(x).strip()]
                return (kws or list(POS_KEYWORDS_DEFAULT), cts or list(POS_CITIES_DEFAULT))
    except Exception:
        pass
    # 3) 回退内置默认
    return list(POS_KEYWORDS_DEFAULT), list(POS_CITIES_DEFAULT)


def save_match_rules(data):
    """原子写入「匹配条件」。data 必须含 keywords/cities（列表，允许空）。

    非法输入抛 ValueError；成功返回清洗后的 {keywords, cities}（元素 strip）。
    """
    import json as _json
    if not isinstance(data, dict):
        raise ValueError("match-rules 必须是 JSON 对象 {keywords:[...], cities:[...]}")
    kws = [str(x).strip() for x in (data.get("keywords") or []) if str(x).strip()]
    cts = [str(x).strip() for x in (data.get("cities") or []) if str(x).strip()]
    payload = {"keywords": kws, "cities": cts}
    tmp = MATCH_RULES + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        _json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MATCH_RULES)
    return payload


def _load_pos_filter():
    """兼容旧调用（scan_positions 用）：返回当前生效的 (keywords, cities)。"""
    return load_match_rules()


# 保留旧接口名（server.mapping_preview 用到 ATS_TEMPLATES["default"])
ATS_TEMPLATES = {
    "default": {
        "姓名": "name",
        "手机": "phone",
        "邮箱": "email",
        "学校": "education.school",
        "专业": "education.major",
        "GPA": "education.gpa",
        "自我评价": "self_intro",
    }
}


def load_profile():
    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _get_path(obj, path):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return ""
    return cur


def _stringify(val):
    if val is None:
        return ""
    if isinstance(val, (str, int, float)):
        return str(val)
    if isinstance(val, list):
        parts = []
        for item in val:
            if isinstance(item, dict):
                parts.append(" / ".join(
                    "%s:%s" % (k, _stringify(v)) for k, v in item.items() if v
                ))
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    if isinstance(val, dict):
        return " / ".join("%s:%s" % (k, _stringify(v)) for k, v in val.items() if v)
    return str(val)


def _flatten(profile):
    """把 profile.json 摊平成 {字段路径: 字符串值}，复合结构拼成可读文本。"""
    return {path: _stringify(_get_path(profile, path)) for path in FIELD_ALIASES}


def fill_by_url(url, profile=None, ats="default"):
    """对投递入口执行详情页录入（本地 computer-use），返回摘要字符串。"""
    profile = profile or load_profile()
    return _playwright_fill(url, profile)


def inspect_by_url(url, profile=None):
    """诊断（不填写、不提交）：打开投递入口页，读取各控件当前值，与个人信息库做匹配，
    返回填表状态报告，供前端「打开→检查填表状态」排查卡点。"""
    profile = profile or load_profile()
    from playwright.sync_api import sync_playwright
    flat = _flatten(profile)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", args=["--no-sandbox"])
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(3000)

            raw = page.eval_on_selector_all(
                "input,textarea,select",
                """els => els.map(e => {
                    const tag = e.tagName.toLowerCase();
                    const type = (e.getAttribute('type') || '').toLowerCase();
                    // T82：原来 `_HINT` 是在下面 `if (!label)` 块里用 const 声明的，
                    // 但上面 T78 的「标题节点」解析**先引用了它** → 整段 evaluate 抛
                    // ReferenceError: _HINT is not defined（检查填表状态直接失败）。
                    // 提到回调顶部，并补上「0/2000」这类字数计数器（北森 textarea 的标题就是它）。
                    const _HINT = /^(必填项未填写|暂无选项|请选择|请输入|请填写|未填写|必填|选填|\\d+\\s*\\/\\s*\\d+)$/;
                    let label = '';
                    // T72：读 label 时先摘掉「值 / 提示」节点（`.sd-Input-message.sd-Input-error`
                    // 常常就在包着 input 的 <label> 里面），否则会读到「必填项未填写」。
                    try { if (e.labels && e.labels[0]) {
                        const _c0 = e.labels[0].cloneNode(true);
                        _c0.querySelectorAll('option,select,[class*=display],[class*=placeholder],' +
                          '[class*=error],[class*=hint],[class*=tip],[class*=help],[class*=validat],' +
                          '[class*=feedback],[class*=required]').forEach(x => { try { x.remove(); } catch(_){} });
                        label = (((_c0.innerText || _c0.textContent || '') + '') + '').trim();
                    } } catch(_) {}
                    if (!label) label = e.getAttribute('aria-label') || '';
                    if (/^(必填项未填写|暂无选项|请选择|请输入|请填写|未填写|必填|选填)$/.test(label)) label = '';
                    // T78：与 COLLECT_JS 同步 —— Formily/UD 标题节点。
                    if (!label) {
                        for (let _p = e.parentElement, _h = 0; _p && _h < 14; _p = _p.parentElement, _h++) {
                            let _lb = null;
                            try { _lb = _p.querySelector('label.form-item__text,[class*="form-item__text"],[class*="form-item-label"],[class*="formily-item-label"]'); } catch (_) {}
                            if (!_lb || _lb.contains(e)) continue;
                            const _t = _clean(_lb);
                            if (_t && !_HINT.test(_t)) { label = _t; break; }
                        }
                    }
                    // 标题兜底（T71/T72）：与 COLLECT_JS 一致 —— 标题常写在**兄弟 div** 里，
                    // 但**值文本 / 校验提示不算标题**（否则 label 会变成「必填项未填写」）。
                    if (!label) {
                        const _clean = (node) => {
                            try {
                                const c = node.cloneNode(true);
                                c.querySelectorAll('option,select,[class*=display],[class*=placeholder],' +
                                  '[class*=error],[class*=hint],[class*=tip],[class*=help],[class*=validat],' +
                                  '[class*=feedback],[class*=required]').forEach(x => { try { x.remove(); } catch(_){} });
                                const s = ((c.innerText || c.textContent || '') + '').replace(/\\s+/g, ' ');
                                return s.replace(/^[^\\w\\u4e00-\\u9fa5]+|[^\\w\\u4e00-\\u9fa5]+$/g, '').trim();
                            } catch (_) { return ''; }
                        };
                        for (let _p = e.parentElement, _h = 0; _p && _h < 6; _p = _p.parentElement, _h++) {
                            let _t = '';
                            for (const _c of _p.children) {
                                try { if (_c.contains(e)) continue; } catch (_) { continue; }
                                if (_c.tagName === 'INPUT' || _c.tagName === 'TEXTAREA' ||
                                    _c.tagName === 'SELECT') continue;
                                try { if (_c.querySelector && _c.querySelector('input,textarea,select')) continue; } catch (_) {}
                                const _ct = _clean(_c);
                                if (!_ct || _HINT.test(_ct)) continue;
                                _t += ' ' + _ct;
                            }
                            _t = _t.replace(/\\s+/g, ' ').trim();
                            if (_t && _t.length <= 20 && /[\\w\\u4e00-\\u9fa5]/.test(_t)) {
                                label = _t; break;
                            }
                        }
                    }
                    let val = '';
                    try {
                        if (tag === 'select') {
                            val = (e.selectedOptions && e.selectedOptions[0]) ? (e.selectedOptions[0].textContent || e.value) : e.value;
                        } else if (type === 'radio' || type === 'checkbox') {
                            val = e.checked ? (e.value || 'true') : '';
                        } else {
                            val = e.value || '';
                        }
                    } catch(_) {}
                    return { tag, type,
                        label: (label||'').trim(),
                        ph: e.getAttribute('placeholder')||'',
                        nm: e.getAttribute('name')||'',
                        id: e.getAttribute('id')||'',
                        val };
                })""",
            )
            fields = [f for f in raw
                      if f["tag"] == "textarea" or f["tag"] == "select"
                      or (f["tag"] == "input" and f["type"] not in
                          ("file", "radio", "checkbox", "submit", "button", "hidden"))]

            assignments, unfilled = match_fields(flat, fields)

            out_fields = []
            for path, f, s in assignments:
                cur = (f.get("val") or "").strip()
                out_fields.append({
                    "label": f.get("label") or f.get("ph") or f.get("nm") or f.get("id"),
                    "profile": path,
                    "profile_value": flat[path],
                    "current": cur,
                    "filled": bool(cur),
                    "score": s,
                })

            file_inputs = page.eval_on_selector_all(
                "input[type=file]",
                """els => els.map(e => {
                    let label=''; try{ if(e.labels&&e.labels[0]) label=e.labels[0].innerText||''; }catch(_){}
                    if(!label) label=e.getAttribute('aria-label')||'';
                    return { id:e.getAttribute('id')||'', nm:e.getAttribute('name')||'', label:(label||'').trim() };
                })""",
            )

            return {
                "fields": out_fields,
                "unmatched": unfilled,
                "file_inputs": file_inputs,
                "note": "仅读取页面当前状态，未做任何填写/提交",
            }
        finally:
            browser.close()


def _hay(f):
    """表单字段的可匹配文本（label/placeholder/name/id）。"""
    return (" ".join([f.get("label", ""), f.get("ph", ""), f.get("nm", ""),
                      f.get("id", "")])).lower()


# 别名是**子串**匹配，而 `_hay` 把 label+placeholder+name+id 全拼在一起 →
# 标题里提到「毕业院校」的**别的字段**会把 `education.school` 抢走。
# 实测（全志科技 / MokaHR，「毕业院校所在专业成绩排名」「毕业院校所在专业人数（数字）」）：
#   · 「毕业院校所在专业成绩排名」hay 含「毕业院校」(len 4)
#   · 真正的「学校名称」只含「学校」(len 2) → 分更低 → **学校名被塞进成绩排名下拉**
#     （而那个下拉的选项是 TOP 5% / TOP 10% / …，根本对不上）
# 所以给这些路径加**否定规则**：label 命中这些词就判 0 分 —— 宁可不填，也不填错。
_NEGATIVE_RULES = {
    # T72：`location`（现居城市）的别名里有**裸「城市」**，会把「意向工作城市 / 期望就业城市」
    # 抢走（实测填成了「浙江省-杭州市」而不是「杭州、上海…」）。带这些词的字段明确归 intended_city。
    "location": ["意向", "期望", "目标", "就业"],
    "education.school": ["成绩", "排名", "名次", "人数", "比例", "类别"],
    "education.major": ["成绩", "排名", "名次", "人数", "比例"],
    "education.degree": ["类别", "性质", "排名", "人数"],
    "education.grad_year": ["排名", "人数"],
    "education.gpa": ["排名", "人数"],
}


def _rejected(path, field):
    """该表单字段的**标题**是否明确不该接这个 profile 路径（见 `_NEGATIVE_RULES`）。"""
    lbl = (field.get("label") or "").strip().lower()
    if not lbl:
        return False
    for bad in _NEGATIVE_RULES.get(path, []):
        if bad in lbl:
            return True
    return False


def _score_detail(path, field):
    """(匹配分, 命中别名在别名表里的下标)。

    分数 = 最长命中别名长度（+label 精确相等的 10 分）；同分时用**下标**决胜 ——
    `FIELD_ALIASES` / `ITEM_ALIASES` 里的顺序就是语义优先级
    （T71：「内容」排在「职责」前面，避免项目描述被写进职责栏）。
    """
    if _rejected(path, field):
        return 0, 0
    hay = _hay(field)
    if not hay.strip():
        return 0, 0
    best, rank = 0, 0
    for _i, alias in enumerate(FIELD_ALIASES.get(path, [])):
        a = alias.lower()
        if a in hay:
            s = len(a)
            # label 精确相等额外加分
            if hay.strip() == a or field.get("label", "").strip().lower() == a:
                s += 10
            if s > best:
                best, rank = s, _i
    return best, rank


def _score(path, field):
    """该 profile 字段与某表单字段的匹配分（见 `_score_detail`）。"""
    return _score_detail(path, field)[0]


def match_fields(flat, fields):
    """纯函数（可单测）：把 profile 字段贪心一对一分配到表单字段。

    返回 (assignments, unfilled)：
      assignments = [(path, field, score), ...]  每个表单字段 / 每个 profile 字段最多用一次
      unfilled    = 有值但没匹配到任何表单字段的 profile 路径列表
    """
    pairs = []
    for f in fields:
        for path in flat:
            if not flat[path]:
                continue
            s, rank = _score_detail(path, f)
            if s > 0:
                pairs.append((s, rank, path, f))
    # 分数高的优先；同分时按别名表内先后（越靠前越优先）；再同分看别名条数（更具体）
    pairs.sort(key=lambda x: (-x[0], x[1], -len(FIELD_ALIASES.get(x[2], []))))

    used_profile, used_fields, assignments = set(), set(), []
    for s, _rank, path, f in pairs:
        if path in used_profile:
            continue
        fid = id(f)
        if fid in used_fields:
            continue
        used_profile.add(path)
        used_fields.add(fid)
        assignments.append((path, f, s))

    unfilled = [p for p in FIELD_ALIASES if flat.get(p) and p not in used_profile]
    return assignments, unfilled


def _selector_for(f):
    """字段 → 唯一 CSS 选择器。

    **优先用采集时打上的 `data-filler-idx`**：很多站点的表单控件只有 placeholder，
    既没有 id 也没有 name；旧版此时退回兜底选择器 `input,textarea,select`，
    而它总是命中**第 1 个**匹配元素 —— 于是 4 个字段的值全写进第 1 个输入框，
    其余留空，且 `page.fill` **不会报错**（静默填错、却报告「已填 N 项」）。
    """
    if f.get("idx") is not None:
        return "[data-filler-idx='%s']" % f["idx"]
    if f.get("id"):
        return "#" + f["id"]
    if f.get("nm"):
        return "input[name='%s'],textarea[name='%s'],select[name='%s']" % (
            f["nm"], f["nm"], f["nm"])
    return "input,textarea,select"


def _gender_token(v):
    """把「男/女」「female」等写法归一到 男 / 女，便于 radio 文案匹配。"""
    s = (v or "").strip()
    low = s.lower()
    if "女" in s or "female" in low:
        return "女"
    if "男" in s or "male" in low:
        return "男"
    return s


# ============ 自定义控件填值（级联 / 日期面板 / 隐藏真值）—— 2026-09-23 T62 ============
# 用户报「级联填表失败」：税友/高途 `native_place→请输入籍贯(TimeoutError)`、
# 货拉拉 `birth→出生日期(年龄)(TimeoutError)`。本地按真实形态复现，结论：
#   · readonly input + 点击才展开的面板（籍贯级联 / 出生日期）→ `page.fill` 的可操作性检查
#     **一直重试到超时**（Playwright 默认 30s/字段）才抛 TimeoutError；
#   · 真值塞在 `display:none` 的原生 input 里（用户看到的是自定义 div）→ 同样 30s 超时；
#   · SPA 在采集之后重渲染，会抹掉我们打的 `data-filler-idx` → **每个字段**都 30s 超时
#     （复现：3 个字段 90s 且全部失败）。
# 修法：不干等（显式 4s）+ 填前重新定位选择器 + 分层填值（真实点选 > JS 写值，后者如实标注待确认）。

_PICKER_HINTS = ("picker", "cascader", "select", "date", "calendar", "dropdown",
                 "van-field", "el-select", "el-date", "ant-select",
                 "popup", "province", "region")

_PANEL_SEL = (".van-popup,.van-picker,.van-cascader,.el-popper,.el-picker-panel,"
              "[class*='cascader-panel'],[class*='picker-panel'],[class*='picker__panel'],"
              "[class*='date-picker'],[class*='datepicker'],[class*='dropdown-menu'],"
              "[class*='select-dropdown'],[class*='picker'],[class*='panel'],"
              "[class*='popup'],[class*='dropdown'],[role='listbox'],[role='dialog'],"
              "[role='menu']")

_CONFIRM_TEXTS = ("确定", "确认", "完成", "保存", "OK")


def _split_levels(val):
    """把值拆成「需要逐级选择的词」。

    「福建省-龙岩市-新罗区」→ ['福建省','龙岩市','新罗区']；「2004-12-05」→ ['2004','12','05']。
    分隔符支持 `-` `/` `、` 逗号与空格。级联只有 2 级值时就只能填到第 2 级，如实报未完成。
    """
    s = str(val or "").strip()
    if not s:
        return []
    return [p for p in re.split(r"[-/、,，\s]+", s) if p]


def _norm_token(s):
    """面板选项比对用的归一化：去空白与分隔符、去「年月日号」与行政区后缀、数字去前导零、小写。

    ⚠️ 两边（目标值与回读值）必须走**同一个**归一化函数，否则「福建省/龙岩市」与
    「福建省-龙岩市」这种纯格式差异会被误判成填错。
    """
    x = re.sub(r"[年月日号省市区县\s\-/、,，.]", "", str(s or ""))
    x = re.sub(r"\d+", lambda m: str(int(m.group())), x)
    return x.lower()


def _value_ok(got, val):
    """回读是否算「填成功」。

    普通文本框要求**完全相等**（归一化后相等也算）；面板类控件放宽到「逐级词都在显示值里」
    （站点常把显示值格式化成「福建省 / 龙岩市 / 新罗区」或「2004年12月5日」）。
    ⚠️ 放宽**有硬边界**：单值字段绝不做「包含就算成功」—— 否则把电话号码填错一位
    （`13800000001` 含 `13800000000`？不，但反向包含成立）也会被判成功。只有**多级值**
    才允许「逐级词全出现」。
    """
    v = str(val or "").strip()
    g = re.sub(r"\s+", "", str(got if got is not None else ""))
    if not v:
        return True
    if g and g == re.sub(r"\s+", "", v):
        return True
    gk, vk = _norm_token(g), _norm_token(v)
    if not gk:
        return False
    if gk == vk:
        return True
    if len(vk) >= 3 and vk in gk:
        return True
    parts = _split_levels(v)
    if len(parts) >= 2:
        ns = [_norm_token(p) for p in parts]
        if all(n and n in gk for n in ns):
            return True
    return False


def _read_value(page, sel):
    try:
        return page.eval_on_selector(sel, "e => (e.value === undefined ? '' : e.value)")
    except Exception:
        return None


def _js_field_state(page, sel):
    """一次 JS 调用拿全可操作性与类名（少往返）。读不到返回 None。"""
    try:
        return page.evaluate("""(sel) => {
            const el = document.querySelector(sel);
            if (!el) return null;
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            const cn = (el.className && el.className.toString) ? el.className.toString() : '';
            return {visible: r.width > 0 && r.height > 0 &&
                             cs.visibility !== 'hidden' && cs.display !== 'none',
                    readonly: !!el.readOnly, disabled: !!el.disabled,
                    cls: cn + ' ' + (el.getAttribute('placeholder') || '')};
        }""", sel)
    except Exception:
        return None


def _js_set_value(page, sel, val):
    """不依赖可操作性直接写值：**原生 setter + input/change/blur 事件**。

    React / Vue 受控组件只认原生 setter（直接 `el.value = v` 会被框架忽略），
    所以必须走 prototype 上的 value setter。返回写后的值；元素不存在返回 None。
    """
    try:
        return page.evaluate("""(a) => {
            const el = document.querySelector(a.sel);
            if (!el) return null;
            try { el.focus(); } catch (_) {}
            const proto = el.tagName === 'TEXTAREA'
                ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const d = Object.getOwnPropertyDescriptor(proto, 'value');
            if (d && d.set) { d.set.call(el, a.val); } else { el.value = a.val; }
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            el.dispatchEvent(new Event('blur', {bubbles: true}));
            return el.value;
        }""", {"sel": sel, "val": val})
    except Exception:
        return None


def _resolve_selector(page, f):
    """返回一个**当前确实存在**的选择器（填之前调用）。

    `data-filler-idx` 是采集时打上的，SPA 在采集之后重渲染就会抹掉它 —— 之后每次
    `page.fill('[data-filler-idx=N]')` 都要等到超时才失败（本地复现：3 字段 90s 全败）。
    这里先探一下，不存在就按 id → name → placeholder → label 重定位，并**把标记补回去**。
    """
    sel = _selector_for(f)
    try:
        if page.eval_on_selector_all(sel, "els => els.length") > 0:
            return sel
    except Exception:
        pass
    try:
        found = page.evaluate("""(a) => {
            const all = Array.from(document.querySelectorAll('input,textarea,select'));
            const same = e => e.tagName.toLowerCase() === a.tag;
            let el = null;
            if (a.id) el = document.getElementById(a.id);
            if (!el && a.nm) el = all.find(e => e.getAttribute('name') === a.nm);
            if (!el && a.ph) el = all.find(e => same(e) &&
                                   (e.getAttribute('placeholder') || '') === a.ph);
            if (!el && a.label) el = all.find(e => same(e) && e.labels && e.labels[0] &&
                                   (e.labels[0].innerText || '').trim() === a.label);
            if (!el) return false;
            if (a.idx !== null && a.idx !== undefined) {
                try { el.setAttribute('data-filler-idx', String(a.idx)); } catch (_) {}
            }
            return true;
        }""", {"idx": f.get("idx"), "id": f.get("id") or "", "nm": f.get("nm") or "",
                "ph": f.get("ph") or "", "label": f.get("label") or "",
                "tag": f.get("tag") or "input"})
    except Exception:
        found = False
    if not found:
        return None
    try:
        if page.eval_on_selector_all(_selector_for(f), "els => els.length") > 0:
            return _selector_for(f)
    except Exception:
        pass
    return None


def _panel_marks(page):
    """把当前**可见面板**里的候选选项打上 `data-filler-pick=N`，返回其文案列表。

    只在「面板类」容器里找（`.van-popup` / `.el-popper` / `*picker*` / `*dropdown*` …），
    **不退回整个 document** —— 否则可能在无关页面上点到同名文字，把人带跑偏。
    """
    try:
        return page.evaluate("""(sel) => {
            document.querySelectorAll('[data-filler-pick]').forEach(
                e => e.removeAttribute('data-filler-pick'));
            const vis = e => { const r = e.getBoundingClientRect();
                               const cs = getComputedStyle(e);
                               return r.width > 0 && r.height > 0 &&
                                      cs.visibility !== 'hidden' && cs.display !== 'none'; };
            const roots = Array.from(document.querySelectorAll(sel)).filter(vis);
            if (!roots.length) return [];
            const out = [];
            let n = 0;
            for (const r of roots) {
                for (const e of r.querySelectorAll('li,td,dd,dt,button,a,p,span,div')) {
                    if (!vis(e)) continue;
                    const t = (e.innerText || '').replace(/\\s+/g, '').trim();
                    if (!t || t.length > 24) continue;
                    e.setAttribute('data-filler-pick', String(n));
                    out.push(t);
                    n++;
                }
            }
            return out;
        }""", _PANEL_SEL)
    except Exception:
        return []


def _pick_token(texts, tok):
    """在候选文案里挑最匹配 tok 的下标：先精确（归一化后相等，短的优先），再包含。"""
    want = _norm_token(tok)
    if not want:
        return None
    exact = None
    loose = None
    for i, t in enumerate(texts):
        cand = _norm_token(t)
        if not cand:
            continue
        if cand == want:
            if exact is None or len(t) < len(texts[exact]):
                exact = i
        elif loose is None and (want in cand or cand in want):
            loose = i
    return exact if exact is not None else loose


def _click_mark(page, idx):
    try:
        return page.evaluate("""(i) => {
            const el = document.querySelector('[data-filler-pick="' + i + '"]');
            if (!el) return false;
            try { el.scrollIntoView({block: 'center'}); } catch (_) {}
            for (const t of ['mousedown', 'mouseup', 'click']) {
                try {
                    el.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true}));
                } catch (_) {}
            }
            return true;
        }""", idx)
    except Exception:
        return False


def _click_panel_confirm(page):
    """点面板里的「确定 / 完成」按钮（部分日期控件必须点它才把值写回输入框）。"""
    try:
        return page.evaluate("""(texts) => {
            const vis = e => { const r = e.getBoundingClientRect(); return r.width>0&&r.height>0; };
            const roots = Array.from(document.querySelectorAll(
                ".van-popup,.el-popper,[class*='picker'],[class*='panel'],"
                + "[class*='popup'],[role='dialog']")).filter(vis);
            for (const r of roots) {
                for (const e of r.querySelectorAll('button,span,div,a')) {
                    if (!vis(e) || e.children.length !== 0) continue;
                    const t = (e.innerText || '').replace(/\\s+/g, '').trim();
                    if (texts.indexOf(t) >= 0) { e.click(); return t; }
                }
            }
            return '';
        }""", list(_CONFIRM_TEXTS))
    except Exception:
        return ""


def _fill_custom_widget(page, sel, val, note=None):
    """自定义控件（籍贯级联 / 出生日期面板）：点开 → 按层级逐级点选。成功返回 True。

    这是**真实交互**，框架会更新自己的 model（比 JS 直接写 value 可靠得多）。
    面板没开出来时换一种触发方式再试一次；一旦面板开了就不再重复点
    （重复点会把面板点关，反而更糟）。
    """
    parts = _split_levels(val)
    if not parts:
        return False
    for trigger in ("click", "js"):
        try:
            if trigger == "click":
                page.click(sel, timeout=2500)
            else:
                page.eval_on_selector(sel, "e => e.click()")
        except Exception:
            continue
        page.wait_for_timeout(450)
        marks = _panel_marks(page)
        if not marks:
            continue                       # 面板没开 → 换触发方式重试
        for tok in parts:
            i = _pick_token(marks, tok)
            if i is None:
                break
            _click_mark(page, i)
            page.wait_for_timeout(320)
            marks = _panel_marks(page)     # 级联会换下一列
        _click_panel_confirm(page)
        page.wait_for_timeout(220)
        if _value_ok(_read_value(page, sel), val):
            return True
        if note:
            note("自定义控件已展开但未选全（目标 %s），交由后续兜底" % "/".join(parts))
        return False
    return False


# ============ 年月区间控件（T70） ============
# 用户报（全志科技 / MokaHR）：「日期选择栏」填不对。
# 实测该站日期字段是 `date_info` / `month-range-select`：
#   **4 个独立文本框**（年/月 - 年/月）＋一个独立的「至今」勾选框；
# T74：年月框的识别见 `isYM/_ymPos` —— **结构补证 + placeholder** 双保险，
#      因为站点填过值后会抹掉 placeholder（只按 placeholder 判会认不出已填的行）。
#   输入框**不是 readonly**（ro=False dis=False），但**只 `page.fill` 不算数**（T75 实测）：
#   值会停在 DOM 里、不进站点 model，一失焦就被抹回空 → 必须 `page.fill` + **回车**才算提交，
#   且月份框只认**不补零**的 `9`（写 `09` 会被拒 → 回落到站点默认的 1）。
# 而我们的模型是「一个 profile 路径 → 一个控件」，所以日期栏原本**一个字都填不上**。
_DATE_SEP = re.compile(r"\s*(?:-|－|—|–|~|～|至|to)\s*", re.I)
_YM_PH = re.compile(r"^(年|月|year|month|yyyy|mm)$", re.I)
_MONTH_PAD = os.environ.get("RESUME_DATE_MONTH_PAD", "2")     # "2"=补零两位 / "none"=原样

# 在日期行里给 4 个框打标（data-daterange="<字段idx>:<序号>"），并给「至今」框打标。
# 行怎么找：从目标输入框往上，第一个「含 ≥2 个 年/月 框、且不含别的输入框」的祖先是日期行。
# 必须排除「别的输入框」：区块容器 .multi-* 里同时装着公司名/职位等，若只判「含 ≥2 个年月框」
# 就会把普通字段也当日期行，把日期写进别人的框里。
DATE_RANGE_JS = """(idx) => {
  const el = document.querySelector('[data-filler-idx="' + idx + '"]');
  if (!el) return null;
  // T74：**不能只看 placeholder** —— MokaHR 在框里填了值后会把 placeholder 抹掉，
  // 只按 placeholder 判 → 已填过的日期行认不出 → `_fill_date_range` 退化成单值填写
  // → 整串日期写进错误的框（实测：「就读时间」变成 2027.6 - 2027.6，起始年丢失）。
  const _ymPos = (e) => {
    if (!e || e.tagName !== 'INPUT') return '';
    const p = (e.getAttribute('placeholder') || '').trim();
    if (/^(年|月|year|month|yyyy|mm)$/i.test(p)) return p;
    try {
      const rng = e.closest && e.closest('[class*=month-range],[class*=date_info]');
      if (!rng) return '';
      const ins = Array.prototype.filter.call(rng.querySelectorAll('input'),
        (x) => !x.type || x.type === 'text');
      const pos = ins.indexOf(e);
      return pos < 0 ? '' : ((pos % 2 === 0) ? '年' : '月');
    } catch (_) { return ''; }
  };
  const isYM = e => !!_ymPos(e);
  // 日期行 = 「干净」的祖先里**最外层**、且年月框数量 ≤4 的那一层。
  // 判据要求：只装年月框（勾选框/隐藏域/单选框不算别的输入）→ 否则「公司名称」也会被当日期行。
  // ⚠️ 为什么取最外层而不是第一个命中：MokaHR 每个日期框外面**还套了一层 per-input wrapper**
  //    （input → label → .sd-Dropdown → .sd-Tooltip → .item-* → span），
  //    「第一个含 ≥2 个年月框的祖先」= 那个只装 年+月 的 span → 只有 2 个框
  //    → 终点填不进去（实测「就读时间」只填了起点、第 3 段 TimeoutError）。
  //    上限 4 = 起点年/月 + 终点年/月；再往上的容器若含两行就会 >4，自动被拒。
  const isOther = e => !isYM(e) && e.type !== 'checkbox' && e.type !== 'hidden'
                     && e.type !== 'radio';
  const pick = () => {
    let best = null, r = el, k = 0;
    while (r && k < 8) {
      const ins = Array.from(r.querySelectorAll('input'));
      const nYM = ins.filter(isYM).length;
      const nOther = ins.filter(isOther).length;
      if (nYM >= 2 && nYM <= 4 && nOther === 0) best = r;   // 继续往上找更外层
      r = r.parentElement; k++;
    }
    return best;
  };
  const row = pick();
  if (!row) return null;
  const ym = Array.from(row.querySelectorAll('input')).filter(isYM);
  ym.forEach((e, k) => e.setAttribute('data-daterange', idx + ':' + k));
  let till = 0;
  const cb = Array.from(row.querySelectorAll('input[type=checkbox]'));
  if (cb.length) { cb[0].setAttribute('data-daterange-till', String(idx)); till = 1; }
  // T75：站点把这些框放在 <form> 里时**不能随便回车**（可能触发隐式提交）→
  // 把「能不能安全回车」回报给调用方；没有 form 才用回车提交。
  let enterSafe = 1;
  try { enterSafe = (ym[0] && ym[0].form) ? 0 : 1; } catch (_) { enterSafe = 1; }
  return {n: ym.length, till: till, enterSafe: enterSafe,
          rowCls: (row.getAttribute('class') || '').slice(0, 80),
          rowText: (row.innerText || '').replace(/\\s+/g, ' ').slice(0, 40)};
}"""


# 读「某个年月框**站点已采纳**的值」（T75）。
# 为什么不能只读 `input.value`：MokaHR 把值提交进 model 后会把 `input.value` 清空、
# 值搬到 `[class*=display-value]` 显示层 —— 只读 input.value 会把「已填」误判成「没填」。
DATE_BOX_VALUE_JS = """(args) => {
  const idx = args[0], k = args[1];
  const el = document.querySelector('[data-daterange="' + idx + ':' + k + '"]');
  if (!el) return ['', 0];
  // 往上取**最外层仍只装这一个文本输入框**的祖先 = 该框自己的外壳
  let host = el.parentElement, up = el.parentElement;
  for (let i = 0; i < 6 && up; i++, up = up.parentElement) {
    let n = 0;
    try { n = Array.from(up.querySelectorAll('input'))
                 .filter(x => !x.type || x.type === 'text').length; } catch (_) { n = 0; }
    if (n === 1) host = up;
  }
  let dv = '';
  if (host) {
    const nodes = host.querySelectorAll('[class*=display-value]');
    dv = Array.from(nodes).map(x => (x.innerText || '').trim())
                          .filter(Boolean).join(' ');
  }
  // 这个框是不是「值显示在显示层」的 sd-Select 类控件？是的话**只能看显示层**
  // （这类控件提交后 input.value 会被清空 → 读 input.value 会把「没采纳」看成「已填」）。
  let isSel = 0;
  for (let p = el, i = 0; i < 6 && p; i++, p = p.parentElement) {
    if (/sd-Select-container|sd-Select-single/.test(p.getAttribute('class') || '')) {
      isSel = 1; break;
    }
  }
  return [dv, isSel];
}"""


def _one_point(p):
    """'2026.07' / '2026-7' / '2026/7' / '2026年7月' / '2026' → (年, 月|None)。"""
    m = re.search(r"(\d{4})\s*(?:[.\-/年]\s*(\d{1,2}))?", (p or "").strip())
    if not m:
        return None
    y = int(m.group(1))
    mo = int(m.group(2)) if m.group(2) else None
    if mo is not None and not (1 <= mo <= 12):
        mo = None
    return (y, mo)


def parse_period(s):
    """把 `period` 原文解析成 `((年,月), (年,月)|None, 是否至今)`；解析不了返回 None。

    兼容实测到的全部写法：
      2026.07 – 2026.09 / 2025.09 - 2026.03 / 2024.06 - 2024.12（endash、空格）
      2026.2-2026.6 / 2023.9-2027.6 / 2024.02-2025.02（一位月、点分）
      2024-02 ~ 2025-02（连字符月）/ 2027（只有起点）/ 2023.09 至今
    """
    if s is None:
        return None
    t = str(s).strip()
    if not t:
        return None
    t = (t.replace("　", " ").replace("～", "~").replace("—", "-")
          .replace("–", "-").replace("－", "-").replace("~", "-"))
    ongoing = bool(re.search(r"至今|现在|进行中|present|now", t, re.I))
    core = re.sub(r"至今|现在|进行中|present|now", " ", t, flags=re.I)
    # 先把「YYYY-MM ~ YYYY-MM」这种「连字符当月份分隔」的形态单独处理
    m = re.match(r"^\s*(\d{4})-(\d{1,2})\s*(?:-|至|to)\s*(\d{4})-(\d{1,2})\s*$", core, re.I)
    if m:
        a, b = (int(m.group(1)), int(m.group(2))), (int(m.group(3)), int(m.group(4)))
        ok = all(x is None or 1 <= x <= 12 for x in (a[1], b[1]))
        if ok:
            return (a, None if ongoing else b, ongoing)
    segs = [x.strip() for x in _DATE_SEP.split(core)]
    pts = [p for p in (_one_point(x) for x in segs if x) if p]
    if not pts:
        return None
    end = pts[1] if (len(pts) > 1 and not ongoing) else None
    return (pts[0], end, ongoing)


def _fmt_year(y):
    return "%04d" % int(y) if y is not None else ""


def _fmt_month(m):
    if m is None:
        return ""
    return str(int(m)) if _MONTH_PAD == "none" else "%02d" % int(m)


def _date_part_ok(got, want):
    """年月读回比对：按**数字**比（站点可能把 07 显示成 7），但错年/错月必须判失败。"""
    if not want:
        return True
    g = re.sub(r"\D", "", str(got or ""))
    w = re.sub(r"\D", "", str(want))
    if not w or not g:
        return False
    if g == w:
        return True
    try:              # 07 == 7（补零差异不算错）；08 vs 07 / 2025 vs 2026 仍判失败
        return int(g) == int(w)
    except ValueError:
        return False


def _date_box_read(page, idx, k):
    """读该年月框『站点已采纳』的值（显示层优先，回落到 input.value）。

    ⚠️ MokaHR 提交进 model 后会把 `input.value` 清空 → 只读 `input.value` 会把
    「已填」误判成「空」（T75 实测：整页跑完后 4 个框的 input.value 全空，看着像没填过）。
    """
    disp, is_sel = "", False
    try:
        r = page.evaluate(DATE_BOX_VALUE_JS, [str(idx), int(k)])
        if isinstance(r, (list, tuple)) and len(r) == 2:
            disp, is_sel = (r[0] or ""), bool(int(r[1] or 0))
        elif isinstance(r, str):
            disp = r
    except Exception:                                 # noqa: BLE001
        pass
    # sd-Select 类控件：显示层为空 = 站点**没采纳**（不能回落 input.value —— 那里的
    # 值可能只是我们刚敲进去、尚未提交的文本，回落到它会让校验**恒真**、谎报已填）。
    if is_sel:
        return disp
    if disp:
        return disp
    try:
        return page.input_value('[data-daterange="%s:%d"]' % (idx, k), timeout=1500) or ""
    except Exception:                                 # noqa: BLE001
        return ""


def _write_date_box(page, idx, k, v, is_month=False, enter=True, note=None):
    """写一个年月框并**确证站点已采纳**。返回 (True, "") / (False, 原因)。

    实测（全志 / MokaHR，T75）三件事，缺一个都会「看着填了、其实没填」：
      ① **只 `page.fill` 不算提交**：值停在 DOM 里、不进站点 model，一失焦就被抹回空
         （而 `input.value` 当场回读是有值的 —— 这正是原来「回读通过」却什么都没留下的原因）；
      ② **回车**才提交进 model（失焦、整页重渲染都保留）；
      ③ 月份框**只认不补零**的 `9`：写 `09` 会被拒 → 回落到站点默认的 1。
    已采纳的值不重写（幂等）：免得「修补遍」把已经对的值再写一次、反而把月份重置。
    """
    sel = '[data-daterange="%s:%d"]' % (idx, k)
    if _date_part_ok(_date_box_read(page, idx, k), v):
        return True, ""
    cands = [v]
    if is_month:
        _s = str(v).strip()
        if _s.isdigit() and _s != str(int(_s)):
            cands.append(str(int(_s)))                # 补零被拒 → 退到不补零
    for _i, _t in enumerate(cands):
        try:
            page.fill(sel, _t, timeout=4000)
        except Exception as e:                        # noqa: BLE001
            return False, "写入失败(%s)" % type(e).__name__
        if enter:
            try:
                page.press(sel, "Enter", timeout=2000)
            except Exception:                         # noqa: BLE001
                pass
            page.wait_for_timeout(320)
        if _date_part_ok(_date_box_read(page, idx, k), _t):
            if _i and note:
                note("月份框不接受补零写法，已改写成 %r（站点要求）" % _t)
            return True, ""
    return False, "回读不符(%s≠%s)" % (_date_box_read(page, idx, k), v)


def _fill_date_range(page, f, val, note=None):
    """把 `period` 值填进「年月区间」控件。返回 (True, "") / (False, 原因)。

    不是区间控件时返回 (False, "不是年月区间控件")，调用方据此回落单值填写。
    """
    parsed = parse_period(val)
    if not parsed:
        return False, "时间值解析不出起止年月"
    (y1, m1), end, ongoing = parsed
    try:
        info = page.evaluate(DATE_RANGE_JS, str(f.get("idx")))
    except Exception as e:                            # noqa: BLE001
        return False, "日期行探测失败(%s)" % type(e).__name__
    if not info or not info.get("n"):
        return False, "不是年月区间控件"
    want = []
    if y1 is not None:
        want.append((0, _fmt_year(y1)))
        want.append((1, _fmt_month(m1)))
    if end is not None:
        want.append((2, _fmt_year(end[0])))
        want.append((3, _fmt_month(end[1])))
    # 先做**可行性预检**（T71）：行里的年月框装不下要写的段数时，一个都不写。
    # 否则会「前两段写进去、第 3 段 TimeoutError」→ 页面上留下**半截日期**（实测踩到）。
    need = [k for k, v in want if v]
    n_box = int(info.get("n") or 0)
    if need and max(need) >= n_box:
        return False, ("该行只有 %d 个年月框，装不下第 %d 段（未写入任何值）"
                       % (n_box, max(need) + 1))
    wrote = 0
    # T75：MokaHR 这类 sd-Select 日期框**只填不回车 = 没提交**。回车把值写进站点 model
    # （失焦 / 整页重渲染都保留）；只有站点把它放在 <form> 里时才不回（避免隐式提交）。
    enter = bool(info.get("enterSafe", 1))
    for k, v in want:
        if not v:
            continue
        # 每次写值前**重新打标**：站点在写值过程中会重渲染，而「日期行」是从控件往上
        # 找带 2~4 个年月框的祖先推出来的 → 前后两次可能落在**不同层级**，旧 data-daterange
        # 会把值写进别的框（实测：同一行 4 个框被 pick 到不同层 → 起始年丢失/月份错位）。
        try:
            _i2 = page.evaluate(DATE_RANGE_JS, str(f.get("idx")))
            if _i2 and int(_i2.get("n") or 0) >= 2:
                n_box = int(_i2.get("n") or 0)
        except Exception:                             # noqa: BLE001
            pass
        ok, how = _write_date_box(page, f.get("idx"), k, v,
                                  is_month=(k % 2 == 1), enter=enter, note=note)
        if not ok:
            return False, "第 %d 段%s" % (k + 1, how)
        wrote += 1
    if ongoing:
        # 「至今」：勾上，别去填右半边
        sel = '[data-daterange-till="%s"]' % f.get("idx")
        try:
            page.check(sel, timeout=3000)
        except Exception:                             # noqa: BLE001
            try:
                page.click(sel, timeout=3000)
            except Exception as e:                    # noqa: BLE001
                return False, "「至今」勾选失败(%s)" % type(e).__name__
    if not wrote:
        return False, "解析结果为空，未写入任何年月"
    if note:
        # 报告**站点实际采纳**的值（不是我们写了什么）：MokaHR 提交后 input.value 会清空，
        # 显示层才是真相 → 这里逐段回读显示值，避免「以写代证」。
        shown = "/".join([str(_date_box_read(page, f.get("idx"), k) or "?")
                          for k, v in want if v])
        note("年月区间已填 %d 段（提交后读回 %s）%s"
             % (wrote, shown, "＋已勾「至今」" if ongoing else ""))
    return True, ""


# ============ 飞书招聘（UD / Formily）—— T78 ============
# 站点特征（nio.jobs.feishu.cn 等飞书招聘/atsx 页面）：
#   · 文本：`input.ud__native-input` —— 普通 input，`page.fill` 可用，**失焦不回滚**；
#   · 下拉：`input.ud__select__selector__search__input`（常 readonly）—— 点 `.ud__select__selector`
#           展开 `.ud__select__dropdown`，选项是 `.ud__select__list__item`。
#           ⚠️ 页面上**同时存在多个 dropdown**（关掉的不销毁）→ 必须用 `:visible` 限定，
#           否则会点到上一次那张面板（实测点「家乡」读到了「男/女/保密」）。
#   · 日期：`input.ud__picker-input`（placeholder `YYYY-MM`）—— fill + 回车即提交，失焦保留。
# 标题：`.ud-formily-item-label`（见 COLLECT_JS 的 T78 分支）。
UD_TRIGGER_JS = r"""(sel) => {
  const e = document.querySelector(sel);
  if (!e) return false;
  const box = e.closest('.ud__select') || e.parentElement;
  const trg = (box && box.querySelector('.ud__select__selector')) || box;
  if (!trg) return false;
  trg.click();
  return true;
}"""

UD_SHOWN_JS = r"""(sel) => {
  const e = document.querySelector(sel);
  if (!e) return '';
  const box = e.closest('.ud__select') || e.parentElement;
  const trg = (box && box.querySelector('.ud__select__selector')) || null;
  return trg ? ((trg.innerText || trg.textContent || '') + '').replace(/\s+/g, ' ').trim() : '';
}"""

UD_RANGE_MARK_JS = r"""(idx) => {
  const el = document.querySelector('[data-filler-idx="' + idx + '"]');
  if (!el) return 0;
  const box = el.closest('.ud-formily-item');
  if (!box) return 0;
  const ins = Array.from(box.querySelectorAll('input.ud__picker-input'));
  ins.forEach((e, k) => { try { e.setAttribute('data-udrange', idx + ':' + k); } catch (_) {} });
  return ins.length;
}"""


def _ud_one_pick(page, sel, val, note=None):
    """把单个 UD 下拉点成 `val`。成功返回 True。"""
    toks = [t for t in re.split(r"[-－—–/、,，>\s]+", str(val or "")) if t]
    if not toks:
        return False
    toks = toks[:3]
    try:
        page.click(sel, timeout=2500)
    except Exception:
        try:
            page.evaluate(UD_TRIGGER_JS, sel)
        except Exception:
            return False
    page.wait_for_timeout(550)
    opt_sel = ".ud__select__dropdown:visible .ud__select__list__item"
    picked = []
    for tok in toks:
        hit = None
        try:
            n = page.locator(opt_sel).count()
        except Exception:
            n = 0
        for k in range(n):
            it = page.locator(opt_sel).nth(k)
            try:
                txt = (it.inner_text() or "").strip()
            except Exception:
                continue
            if txt and (txt == tok or tok in txt or txt in tok):
                hit = it
                break
        if hit is None:
            break
        try:
            hit.click(timeout=2500)
        except Exception:
            break
        picked.append(tok)
        page.wait_for_timeout(450)
    # 多选面板常有「确定」
    try:
        page.locator(".ud__select__dropdown:visible button").filter(
            has_text=re.compile("确定|确认|完成")).first.click(timeout=1000)
        page.wait_for_timeout(250)
    except Exception:
        pass
    shown = ""
    try:
        shown = page.evaluate(UD_SHOWN_JS, sel) or ""
    except Exception:
        pass
    if picked and any(t in shown for t in picked):
        return True
    if note and picked:
        note("下拉「%s」只选中 %d 段（显示 %r）" % ("/".join(toks), len(picked), shown[:24]))
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    return False


def _fill_ud_picker(page, f, sel, val, note=None):
    """UD 日期：填 → 回车 → Escape → 回读。区间值（两框）按标记分别写起止。"""
    parsed = parse_period(str(val or ""))
    boxes = 0
    try:
        boxes = int(page.evaluate(UD_RANGE_MARK_JS, str(f.get("idx"))) or 0)
    except Exception:
        boxes = 0

    def _write(target, text):
        ph = ""
        try:
            ph = page.eval_on_selector(target, "e => e.getAttribute('placeholder') || ''") or ""
        except Exception:
            pass
        m = re.match(r"(\d{4})\s*[-./年]\s*(\d{1,2})(?:\s*[-./月]\s*(\d{1,2}))?", text)
        if m:
            y, mo, d = m.group(1), int(m.group(2)), m.group(3)
            if re.search(r"YYYY[-/]MM[-/]DD", ph, re.I):
                text = "%s-%02d-%02d" % (y, mo, int(d or 1))
            else:
                text = "%s-%02d" % (y, mo)
        try:
            page.click(target, timeout=2500)
        except Exception:
            pass
        try:
            page.fill(target, text, timeout=4000)
            page.keyboard.press("Enter")
            page.wait_for_timeout(380)
            page.keyboard.press("Escape")
            page.wait_for_timeout(180)
        except Exception as e:                       # noqa: BLE001
            return None, type(e).__name__
        got = ""
        try:
            got = page.eval_on_selector(target, "e => e.value || ''")
        except Exception:
            pass
        return got, ""

    if parsed and (parsed[1] is not None or parsed[2]) and boxes >= 2:
        (y1, m1), end, ongoing = parsed
        start = "%s-%02d" % (y1, int(m1 or 1))
        got, err = _write('[data-udrange="%s:0"]' % f.get("idx"), start)
        if err or not _value_ok(got, start):
            return False, "起始段回读不符(%s)" % (got or err)
        if end:
            tail = "%s-%02d" % (end[0], int(end[1] or 1))
            got, err = _write('[data-udrange="%s:1"]' % f.get("idx"), tail)
            if err or not _value_ok(got, tail):
                return False, "结束段回读不符(%s)" % (got or err)
        if note:
            note("起止时间已按两段写入（%s ~ %s）" % (start, "至今" if ongoing else (end and "%s-%02d" % (end[0], int(end[1] or 1)))))
        return True, ""

    got, err = _write(sel, str(val or ""))
    if err:
        return False, err
    if _value_ok(got, str(val or "")) or (got and str(val or "")[:7] == got[:7]):
        return True, ""
    if note:
        note("日期框回读不符（读回 %r）" % (got or ""))
    return False, "回读不符(%s)" % (got or "")


UD_TREE_NODE = ".ud__select__dropdown:visible .ud__tree__node__label"

UD_PERIOD_MARK_JS = r"""(idx) => {
  // 飞书招聘的「起止时间」是 `throne-biz-date-range-picker`：**一个 wrapper 两个输入框**，
  // 标题在更高的 item 层（输入框往上 8 层才看到），所以按标题配对会落空。
  // 直接按组件结构取：「日期区间 wrapper 里 type=text 的两个 input」。
  const el = document.querySelector('[data-filler-idx="' + idx + '"]');
  if (!el) return 0;
  let box = null;
  try { box = el.closest('[class*="date-range-picker-wrapper"]'); } catch (_) {}
  if (!box) return 0;
  const ins = Array.from(box.querySelectorAll('input')).filter(e => e.type === 'text');
  ins.forEach((e, k) => { try { e.setAttribute('data-period-box', idx + ':' + k); } catch (_) {} });
  return ins.length;
}"""


def _ud_panel_open(page, sel):
    """点开 UD 下拉：必须点 `.ud__select__selector`（点 input 不会展开），并确认面板真开了。"""
    for how in ("js", "click"):
        try:
            if how == "js":
                page.evaluate(UD_TRIGGER_JS, sel)
            else:
                page.click(sel, timeout=2000)
        except Exception:
            continue
        page.wait_for_timeout(600)
        try:
            if page.locator(".ud__select__dropdown:visible").count() > 0:
                return True
        except Exception:
            pass
    return False


def _ud_tree_pick(page, sel, val, note=None):
    """树形下拉（地区选择）：搜索框过滤 → 点同名叶子。

    节点文案与资料里的写法不一致：资料 `福建省-龙岩市-新罗区`，树里是 `福建/龙岩/新罗区`
    （**不带 省/市/区 后缀**）→ 搜索前先剥后缀，找不到再退回原词。
    """
    toks = [t for t in re.split(r"[-－—–/、,，>\s]+", str(val or "")) if t]
    if not toks:
        return False
    toks = toks[:3]
    picked = []
    search_box = None
    for tok in toks:
        cand = [tok]
        for suf in ("特别行政区", "自治区", "自治州", "地区", "省", "市", "区", "县", "盟"):
            if tok.endswith(suf) and len(tok) > len(suf) + 1:
                cand.append(tok[: -len(suf)])
                break
        hit = None
        for word in cand:
            if not _ud_panel_open(page, sel):
                break
            # ⚠️ 搜索框在**选择器内部**（`sel` 本身就是这个 combobox），不在 dropdown 里
            try:
                page.fill(sel, word, timeout=2500)
            except Exception:
                try:
                    page.click(sel, timeout=1500)
                    page.fill(sel, word, timeout=2500)
                except Exception:
                    search_box = None
                    continue
            page.wait_for_timeout(800)
            try:
                n = page.locator(UD_TREE_NODE).count()
            except Exception:
                n = 0
            for k in range(n):
                node = page.locator(UD_TREE_NODE).nth(k)
                try:
                    txt = (node.inner_text() or "").strip()
                except Exception:
                    continue
                if txt and (txt == word or word in txt):
                    hit = node
                    break
            if hit is not None:
                break
        if hit is None:
            break
        try:
            hit.click(timeout=2500)
        except Exception:
            break
        picked.append(tok)
        page.wait_for_timeout(650)
        search_box = None
    shown = ""
    try:
        shown = page.evaluate(UD_SHOWN_JS, sel) or ""
    except Exception:
        pass
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    # 回读比对**两边都剥后缀**：资料写「杭州市」，树里/显示是「杭州」
    def _bare(x):
        x = str(x or "").strip()
        for _s in ("特别行政区", "自治区", "自治州", "地区", "省", "市", "区", "县", "盟"):
            if x.endswith(_s) and len(x) > len(_s) + 1:
                return x[: -len(_s)]
        return x
    if picked and any(_bare(t) in _bare(shown) for t in picked):
        return True
    if note and picked:
        note("树形下拉「%s」只选中 %d 段（显示 %r）" % ("/".join(toks), len(picked), shown[:24]))
    return False


def _ud_type_box(page, target, text):
    """在一个 UD 文本框里写入并回车提交，返回读回值。"""
    try:
        page.click(target, timeout=2500)
    except Exception:
        pass
    try:
        page.fill(target, text, timeout=4000)
        page.keyboard.press("Enter")
        page.wait_for_timeout(350)
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
    except Exception as e:                       # noqa: BLE001
        return None, type(e).__name__
    try:
        return page.eval_on_selector(target, "e => e.value || ''"), ""
    except Exception:
        return "", ""


def _fill_period_pair(page, f, val, note=None):
    """成对的两个文本框（教育卡「起止时间」）：分别写起、止。"""
    parsed = parse_period(str(val or ""))
    if not parsed:
        return False, "不是成对文本框"
    (y1, m1), end, ongoing = parsed
    try:
        n = int(page.evaluate(UD_PERIOD_MARK_JS, str(f.get("idx"))) or 0)
    except Exception:
        n = 0
    if n != 2:
        return False, "不是成对文本框"
    start = "%s-%02d" % (y1, int(m1 or 1))
    got, err = _ud_type_box(page, '[data-period-box="%s:0"]' % f.get("idx"), start)
    if err or not _value_ok(got, start):
        return False, "起始段回读不符(%s)" % (got or err)
    if end:
        tail = "%s-%02d" % (end[0], int(end[1] or 1))
        got, err = _ud_type_box(page, '[data-period-box="%s:1"]' % f.get("idx"), tail)
        if err or not _value_ok(got, tail):
            return False, "结束段回读不符(%s)" % (got or err)
    if note:
        note("起止时间已分两段写入（%s ~ %s）" % (start, "至今" if ongoing else
                                              ("%s-%02d" % (end[0], int(end[1] or 1)) if end else "?")))
    return True, ""


# ---- 飞书招聘（atsx / Formily + UD）：可重复区块（T79 / T81）----
# 实测（蔚来校招 apply 页）区块结构：
#   工作经历  `#formily-item-<数字 id>`：可见，带「没有工作经历」勾选，**默认勾选** → 整块不渲染；
#   实习经历  `#formily-item-internship_list`：**空模块**，里面只有一个「添加」按钮；
#   项目经历  `#formily-item-project_list`：同上。
# 「空模块」正是上一版填不上的原因：没有 input → 既采不到字段，也没有「公司名称」锚点
# 可认（按锚点找卡片必然 notfound）。所以先按**模块 id** 点「添加」把行建出来，
# 再按「行容器」分组逐行填。
UD_EXP_MODULE = "#formily-item-internship_list"
UD_PROJ_MODULE = "#formily-item-project_list"
UD_EMPTY_EXP_LABELS = ("没有工作经历", "没有实习经历", "没有工作经验", "无工作经验")


def _ud_module_visible(page, sel):
    """模块存在且**可见**（隐藏模块点「添加」只会白建一行，故先看尺寸）。"""
    try:
        return bool(page.eval_on_selector(
            sel,
            "e => { const r = e.getBoundingClientRect(); return r.height > 0 && r.width > 0; }"))
    except Exception:
        return False


def _ud_prepare_sections(page, profile, note=None):
    """飞书招聘：确认经历区块可用；站点**没有**实习经历模块时才退回「工作经历」老路。

    建行与逐条填值都交给 `_ud_fill_arrays`，这里只做「能不能填」的前置判断。
    ⚠️ 蔚来校招的实习/项目都是**空模块**（可见、但 0 个 input），所以**不要**在这里
    取消「没有工作经历」勾选 —— 那会把一个用不上的区块打开（起止时间必填），
    白白给用户留一个半填的必填区块。
    """
    prof = profile or {}
    exps = [x for x in (prof.get("experiences") or []) if isinstance(x, dict)]
    projs = [x for x in (prof.get("projects") or []) if isinstance(x, dict)]
    if not exps and not projs:
        return
    # 兜底：没有 internship_list 的站点（只有「工作经历」卡片）→ 取消勾选让它渲染
    if exps and not _ud_module_visible(page, UD_EXP_MODULE):
        for txt in UD_EMPTY_EXP_LABELS:
            try:
                lb = page.locator("label").filter(has_text=txt).first
                if not lb.count():
                    continue
                cb = lb.locator("input[type=checkbox]").first
                if not cb.is_checked():
                    break
                lb.click(timeout=3000)
                page.wait_for_timeout(2600)   # 取消勾选后区块是**异步渲染**的
                if note:
                    note("站点没有「实习经历」模块，已取消「%s」勾选改用工作经历卡片" % txt)
                break
            except Exception:
                continue
    if note:
        for sel, name, items in ((UD_EXP_MODULE, "实习经历", exps),
                                 (UD_PROJ_MODULE, "项目经历", projs)):
            if items and _ud_module_visible(page, sel):
                note("已定位「%s」区块（资料里 %d 条）" % (name, len(items)))


# 一行 = 4~5 个字段（公司名称 / 职位名称 / 起止时间×2 / 描述）。
# 行数**不数容器类名**：实测新行是并排的 `apply-form-array-card`，外层容器永远只有 1 个
# → 按类名数会恒为 1，8 行全写进第 1 行。改为：
#   行数 = 锚点（公司名称 / 项目名称）的个数；
#   控件的行号 = 它所属「行容器」的序号（行容器 = 最小的、含 ≥2 个 input 的祖先）。
UD_ARRAY_SPECS = (
    ("experiences", "实习经历", (
        (UD_EXP_MODULE, "公司名称", (
            ("company", ("公司名称", "公司", "单位", "企业")),
            ("role", ("职位名称", "职位", "岗位", "职务")),
            ("period", ("起止时间", "在职时间", "实习时间")),
            ("highlights", ("描述", "工作内容", "工作职责", "职责")),
        )),
        # 兜底：站点没有 internship_list 时用「工作经历」卡片（整页找锚点，老路径）
        ("", "公司名称", (
            ("company", ("公司名称", "公司", "单位", "企业")),
            ("role", ("职位名称", "职位", "岗位", "职务")),
            ("period", ("起止时间", "在职时间", "实习时间")),
            ("highlights", ("描述", "工作内容", "工作职责", "职责")),
        )),
    )),
    ("projects", "项目经历", (
        (UD_PROJ_MODULE, "项目名称", (
            ("name", ("项目名称", "项目名")),
            ("role", ("项目角色", "担任角色", "项目职责", "角色", "职责")),
            ("period", ("起止时间", "项目时间", "项目周期")),
            ("desc", ("描述", "项目描述", "项目内容")),
            ("link", ("项目链接", "项目地址", "链接")),
        )),
    )),
)

# ⚠️ 标签选择器必须**同时**含 `formily-item-label`：实测类名是 `ud-formily-item-label`，
# 只写 `form-item-label` 匹配不到（`formily-item-label` 里没有 `form-item-label` 这个子串）
# → 卡内所有 label 读成空串 → 行数恒为 0，一条都填不上（probe21 的 labels: ['','','','']）。
# ⚠️ 控件必须含 textarea：「描述」是 textarea，只查 input 时它拿不到行号 → 描述永远不填。
UD_CARD_JS = r"""(arg) => {
  const key = arg.key, anchor = arg.anchor, modSel = arg.module || '';
  const T = n => ((n.innerText || n.textContent || '') + '').replace(/\s+/g, '');
  const LABSEL = 'label.form-item__text,[class*="form-item__text"],[class*="form-item-label"],[class*="formily-item-label"]';
  const KILL = 'input,textarea,select,[class*=display],[class*=placeholder],[class*=error],' +
               '[class*=hint],[class*=tip],[class*=validat]';
  const HINT = /^(必填项未填写|暂无选项|请选择|请输入|请填写|未填写|必填|选填|添加)$/;
  const clean = (node) => {
    try {
      if (node.matches && node.matches(KILL)) return '';
      const c = node.cloneNode(true);
      c.querySelectorAll(KILL).forEach(x => { try { x.remove(); } catch (_) {} });
      const t = ((c.innerText || c.textContent || '') + '').replace(/\s+/g, '');
      return t.replace(/^[^\w\u4e00-\u9fa5]+|[^\w\u4e00-\u9fa5]+$/g, '').trim();
    } catch (_) { return ''; }
  };
  const labelOf = (e) => {
    for (let p = e.parentElement, h = 0; p && h < 14; p = p.parentElement, h++) {
      let lbs = [];
      try { lbs = Array.from(p.querySelectorAll(LABSEL)); } catch (_) { continue; }
      for (const lb of lbs) {
        if (!lb || lb.contains(e) || lb.querySelector('input,textarea,select')) continue;
        const t = clean(lb);
        if (t && !HINT.test(t)) return t;
      }
    }
    return '';
  };
  const vis = e => { const t = (e.getAttribute('type') || '').toLowerCase();
                     return t !== 'checkbox' && t !== 'radio' && t !== 'hidden' && t !== 'file'; };
  const scope = modSel ? document.querySelector(modSel) : document;
  if (!scope) return {ok: false, reason: 'nomodule'};
  const rowBlock = (e) => {
    let p = e.parentElement, guard = 0;
    while (p && guard++ < 20) {
      if (p.querySelectorAll('input').length >= 2) return p;
      if (p === scope) break;
      p = p.parentElement;
    }
    return null;
  };
  const ctrls = Array.from(scope.querySelectorAll('input,textarea')).filter(vis);
  const anchors = ctrls.filter(e => e.tagName === 'INPUT' && labelOf(e) === anchor);
  const rbs = anchors.map(rowBlock);
  const rows = [];
  ctrls.forEach(e => {
    let idx = -1;
    for (let i = 0; i < rbs.length; i++) {
      if (rbs[i] && rbs[i].contains(e)) { idx = i; break; }
    }
    if (idx < 0) return;
    if (arg.mark) { try { e.setAttribute('data-udrow', key + ':' + idx); } catch (_) {} }
    rows.push(idx);
  });
  const btns = Array.from(scope.querySelectorAll('button')).filter(b => T(b) === '添加');
  return {ok: true, rows: anchors.length, ctrls: ctrls.length, btns: btns.length,
          groups: rows.reduce((m, k) => { m[k] = (m[k] || 0) + 1; return m; }, {})};
}"""

# 点区块的「添加」：有模块 id 就在模块内找（空模块里那个按钮就是唯一入口）；
# 没有模块 id 的站点退回「按锚点找行容器 → 点它所在区块的添加」。
UD_CARD_ADD_JS = r"""(arg) => {
  const T = n => ((n.innerText || n.textContent || '') + '').replace(/\s+/g, '');
  const mod = arg.module || '';
  if (mod) {
    const scope = document.querySelector(mod);
    if (!scope) return false;
    const btns = Array.from(scope.querySelectorAll('button'))
        .filter(b => T(b) === '添加' && b.getBoundingClientRect().height > 0);
    if (!btns.length) return false;
    btns[btns.length - 1].click();
    return true;
  }
  const LABSEL = 'label.form-item__text,[class*="form-item__text"],[class*="form-item-label"],[class*="formily-item-label"]';
  const lab = (e) => {
    for (let p = e.parentElement, h = 0; p && h < 14; p = p.parentElement, h++) {
      let lbs = [];
      try { lbs = Array.from(p.querySelectorAll(LABSEL)); } catch (_) { continue; }
      for (const lb of lbs) {
        if (lb.contains(e) || lb.querySelector('input,textarea,select')) continue;
        const t = T(lb).replace(/\*/g, '');
        if (t) return t;
      }
    }
    return '';
  };
  const vis = e => { const t = (e.getAttribute('type') || '').toLowerCase();
                     return t !== 'checkbox' && t !== 'radio' && t !== 'hidden' && t !== 'file'; };
  const anchors = Array.from(document.querySelectorAll('input')).filter(vis)
      .filter(e => lab(e) === arg.anchor);
  if (!anchors.length) return false;
  const rowBlock = (e) => {
    let p = e.parentElement;
    while (p) { if (p.querySelectorAll('input').length >= 2) return p; p = p.parentElement; }
    return null;
  };
  let box = rowBlock(anchors[0]), guard = 0;
  while (box && !anchors.every(a => box.contains(a)) && guard++ < 30) box = box.parentElement;
  if (!box) return false;
  let b = Array.from(box.querySelectorAll('button')).filter(x => T(x) === '添加').pop();
  if (!b) {
    let up = box.parentElement, i = 0;
    while (up && i < 3 && !b) {
      b = Array.from(up.querySelectorAll('button')).filter(x => T(x) === '添加').pop();
      up = up.parentElement; i++;
    }
  }
  if (!b) return false;
  b.click();
  return true;
}"""


def _ud_fill_arrays(page, profile, note=None):
    """按卡片行填「实习经历」「项目经历」。返回 (摘要列表, 已填 section 名列表)。"""
    prof = profile or {}
    done, keys = [], []
    for key, heading, specs in UD_ARRAY_SPECS:
        items = [x for x in (prof.get(key) or []) if isinstance(x, dict)]
        if not items:
            continue
        got = None
        for module, anchor, mapping in specs:
            got = _ud_fill_section(page, key, heading, items, module, anchor,
                                   mapping, note)
            if got:
                break
        if got:
            done.append(got)
            keys.append(key)
        elif note:
            note("「%s」区块：本站没找到可填的卡片（资料里 %d 条未写入）" % (heading, len(items)))
    return done, keys


def _ud_fill_section(page, key, heading, items, module, anchor, mapping, note=None):
    """把一类条目逐行填进一个区块。返回摘要字符串；**本站没有这个区块**返回 None。"""
    def _state(mark):
        try:
            return page.evaluate(UD_CARD_JS, {"anchor": anchor, "key": key,
                                              "module": module, "mark": mark}) or {}
        except Exception:
            return {}

    st = _state(False)
    if not st.get("ok"):
        return None                      # 没有这个模块 → 交给下一个候选
    rows = int(st.get("rows") or 0)
    clicks = 0
    while rows < len(items) and clicks < _MAX_ADD_CLICKS:
        try:
            if not page.evaluate(UD_CARD_ADD_JS, {"module": module, "anchor": anchor}):
                break
        except Exception:
            break
        page.wait_for_timeout(1200)
        clicks += 1
        rows = int((_state(False) or {}).get("rows") or 0)
    if rows < 1:
        return None
    if clicks and note:
        note("「%s」区块点了 %d 次「添加」（现有 %d 行）" % (heading, clicks, rows))
    # 打行号 → 采集控件 → 逐行按标题填
    _state(True)
    fields = _collect_fields(page)
    rows_fields = {}
    for f in fields:
        try:
            row = page.eval_on_selector(
                '[data-filler-idx="%s"]' % f.get("idx"),
                "e => { const r = e.closest('[data-udrow]'); return r ? r.getAttribute('data-udrow') : ''; }")
        except Exception:
            row = None
        if row and row.startswith(key + ":"):
            rows_fields.setdefault(row.split(":")[1], []).append(f)
    ok_names = []
    for k in range(min(rows, len(items))):
        item = items[k] if isinstance(items[k], dict) else {}
        flds = rows_fields.get(str(k)) or []
        for prop, aliases in mapping:
            val = _stringify(item.get(prop))
            if not val:
                continue
            target = None
            for a in aliases:
                for f in flds:
                    if (f.get("label") or "") == a:
                        target = f
                        break
                if target:
                    break
            if target is None:
                continue
            try:
                ok, how = _fill_field_smart(page, target, val, note=note)
            except Exception:
                continue
            if ok is True:
                flds.remove(target)
        nm = (item.get("company") or item.get("name") or "第%d条" % (k + 1))
        ok_names.append(nm)
    if not ok_names:
        return None
    return "%s %d 条[%s]" % (heading, len(ok_names), "、".join(ok_names))


def _fill_field_smart(page, f, val, note=None):
    """先判「是不是年月区间控件」：是就按**区间**填，否则走通用单值填值。

    为什么不能让它落到单值填值：那样只会把整串 `2026.07 – 2026.09` 写进 4 个框里的
    第 1 个 —— 这正是用户报的「日期栏填不对」。
    ⚠️ 只有 input 才可能是区间控件；且**值是区间形态**时才试（省掉无谓的 DOM 探测）。
    """
    # T78b：飞书招聘教育卡「起止时间」= 两个**各自独立**的文本框（同一标题），
    # 既不是 4 段年月框、也不是 picker → 必须先按标题在同一张卡里配对再分写。
    if f.get("tag") == "input" and re.search(r"起止|就读时间|在校时间|项目时间|在职时间",
                                             str(f.get("label") or "")):
        ok, how = _fill_period_pair(page, f, val, note=note)
        if ok:
            return True, ""
        if how != "不是成对文本框":
            return False, how
    if f.get("tag") == "input" and re.search(r"\d{4}", str(val or "")):
        ok, how = _fill_date_range(page, f, val, note=note)
        if ok:
            return True, ""
        if how != "不是年月区间控件":
            return False, how            # 是区间控件但没填成 → 明确失败，别乱填
    return _robust_fill_field(page, f, val, note=note)


def _robust_fill_field(page, f, val, note=None):
    """填**一个**字段，分层降级。返回 (True, "") / (False, 原因) / ("uncertain", 方式)。

    分层（可靠性由高到低）：
      1) `<select>` → `_fill_select`；
      2) 可操作（可见 / 非 readonly / 非 disabled）→ `page.fill(timeout=4000)`；
         **显式 4s**，不再让不可操作的元素干等 Playwright 默认的 30s；
      3) 自定义控件（readonly / 选择器类）→ 点开面板逐级点选（真实交互）；
      4) 兜底：JS 原生 setter 写值 —— 站点 model 可能不采纳，故返回 "uncertain"，
         日志里如实标注「待确认」，**绝不谎报成已填**。
    """
    sel = _resolve_selector(page, f)
    if not sel:
        return False, "控件已不在页面上(采集后页面被重渲染)"
    if f.get("tag") == "select":
        try:
            return (True, "") if _fill_select(page, f, val) else (False, "选项未命中")
        except Exception as e:                        # noqa: BLE001
            return False, type(e).__name__
    st = _js_field_state(page, sel) or {}
    cls = str(st.get("cls") or "").lower()
    # T82：北森 Phoenix 的选择器（列表 / 月 / 日）不是原生控件，必须点外层再选
    if "phoenix-select__input" in cls:
        return (True, "") if _phx_pick(page, sel, val, note=note) else (False, "Phoenix 选择器未选中")
    is_picker = bool(st.get("readonly")) or any(h in cls for h in _PICKER_HINTS)
    # T78：飞书招聘（UD）的选择器 / 日期都不是原生控件，走专用分支（面板交互 + 回读）
    if "ud__select" in cls:
        if _ud_one_pick(page, sel, val, note=note):
            return True, ""
        # 普通列表下拉没命中 → 再按**树形下拉**（地区选择）试一次
        if _ud_tree_pick(page, sel, val, note=note):
            return True, ""
        return False, "下拉选项未命中"
    if "ud__picker" in cls:
        ok, how = _fill_ud_picker(page, f, sel, val, note=note)
        return (True, "") if ok else (False, how)
    if st.get("visible") and not st.get("readonly") and not st.get("disabled"):
        try:
            page.fill(sel, val, timeout=4000)
            if _value_ok(_read_value(page, sel), val):
                return True, ""
        except Exception:
            pass
    if is_picker or not st.get("visible"):
        if _fill_custom_widget(page, sel, val, note=note):
            return True, ""
    wrote = _js_set_value(page, sel, val)
    if wrote is None:
        return False, "写值失败(元素不存在)"
    if _value_ok(_read_value(page, sel), val):
        return "uncertain", "JS写值(站点是否采纳待确认)"
    return False, "回读不符:%s" % (str(wrote)[:20],)


# ============ 北森 Beisen / Phoenix 组件族（T82，虹软 career.arcsoft.com.cn 实测） ============
# 这套组件和之前支持过的站点都不同，四个坑每一个都会让「跑完全程但一个字没填」：
#   ① 标题是 `label.form-item__text`，距输入框 **9 层**（选择器还多 3 层 wrapper），
#      而通用兜底只向上走 6 层 → 学历 / 首选城市 / 开始结束时间 / 学校名称 标签全空
#      → 字段匹配不上、可重复区块连「代表字段」都数不出来；
#   ② 下拉不是原生控件：输入框 `phoenix-select__input` 只是显示层（`--unText`），
#      点它**不展开**，必须点外层 `.phoenix-select`；面板有两种形态：
#      列表 `.phoenix-selectList`（虚拟滚动）/ 月面板 `.phoenix-calendar-month-panel` /
#      日面板 `.phoenix-calendar-panel`（里面有个可直接输入 `.phoenix-calendar-input`）；
#   ③ 单选是纯 div（`.phoenix-radio-group` + `.phoenix-radio__radio-text`），
#      页面里**没有** `input[type=radio]` → 旧的「0 组单选」把性别整项漏掉；
#   ④ 「添加教育经历 / 添加项目经历」按钮就在**区块容器内部**，旧规则「按钮落在块里就跳过」
#      判成块内按钮 → 永远不点 → 3 条项目只写进第 1 行。
PHX_LIST_ITEM = ".phoenix-selectList__listItem"
PHX_MONTH_PANEL = ".phoenix-calendar-month-panel"
PHX_MONTH_CELL = ".phoenix-calendar-month-panel-cell"
PHX_DATE_PANEL = ".phoenix-calendar-panel"
PHX_DATE_INPUT = ".phoenix-calendar-input"

# 区块规格：path / 标题文案 / 代表字段（数行用）/ 添加按钮文案 / 子字段别名
PHX_SECTIONS = (
    ("education", ("教育经历", "教育背景"), ("学校名称", "院校名称"), "添加教育经历",
     {"school": ("学校名称", "院校名称"),
      "major": ("专业名称", "所学专业"),
      "degree": ("学历", "最高学历"),
      "start": ("开始时间", "起始时间"),
      "end": ("结束时间", "毕业时间")}),
    ("projects", ("项目经历", "项目经验"), ("项目名称",), "添加项目经历",
     {"name": ("项目名称",),
      "role": ("担任角色", "项目角色", "职务", "角色"),
      "start": ("开始时间", "起始时间"),
      "end": ("结束时间",),
      "desc": ("项目描述", "项目内容", "项目介绍"),
      "duty": ("项目中职责", "项目职责")}),
    ("experiences", ("实习经历", "工作经历"), ("公司名称", "单位名称"), "添加实习经历",
     {"company": ("公司名称", "单位名称"),
      "role": ("职位名称", "职务", "岗位"),
      "start": ("开始时间", "起始时间"),
      "end": ("结束时间", "离职时间"),
      "highlights": ("工作内容", "工作描述", "工作职责", "职责描述")}),
)

PHX_SECTION_JS = r"""(arg) => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const bare = (s) => clean(s).replace(/^[^\w\u4e00-\u9fa5]+|[^\w\u4e00-\u9fa5]+$/g, '');
  const labelOf = (e) => {
    try {
      const fi = e.closest('.form-item');
      if (fi) {
        const t = fi.querySelector('.form-item__text');
        const s = bare(t ? t.innerText : '');
        if (s && s.length <= 30) return s;
      }
    } catch (_) {}
    for (let p = e.parentElement, h = 0; p && h < 14; p = p.parentElement, h++) {
      let lb = null;
      try { lb = p.querySelector('label.form-item__text,[class*="form-item__text"],[class*="form-item-label"]'); } catch (_) {}
      if (!lb || lb.contains(e)) continue;
      const s = bare(lb.innerText);
      if (s && s.length <= 30) return s;
    }
    return '';
  };
  const low = (a) => (a || []).map(x => String(x).toLowerCase());
  const primAliases = low(arg.primary);
  const mapLow = {};
  Object.keys(arg.aliases).forEach(k => { mapLow[k] = low(arg.aliases[k]); });
  const ctrls = (r) => Array.from(r.querySelectorAll(
    'input:not([type=file]):not([type=hidden]):not([type=checkbox]):not([type=radio]),textarea'));
  const isPrim = (e) => {
    const l = labelOf(e).toLowerCase();
    return primAliases.some(a => l.indexOf(a) >= 0);
  };
  // 1) 区块标题 + 区块根。侧边导航里也有「教育经历」这个纯文本，
  //    所以只有「根里真的含代表字段」的候选才算（否则会认定成导航而判成"没有区块"）。
  let root = null;
  const cands = Array.from(document.querySelectorAll('div,span,label,p,h1,h2,h3,h4,h5'));
  for (const e of cands) {
    if (e.children.length) continue;
    if (arg.titles.indexOf(clean(e.innerText)) < 0) continue;
    let r = e;
    for (let p = e.parentElement; p; p = p.parentElement) {
      if (ctrls(p).length >= 2) { r = p; break; }
    }
    // 空区块（一条都没有）时没有代表字段 → 再给一次机会：容器里带「添加X经历」按钮也算，
    // 否则空区块会被整块跳过，**永远建不出第一行**（实测北森新岗位的实习经历就是空的）。
    if (ctrls(r).some(isPrim) || (r.innerText || '').indexOf(arg.addText) >= 0) { root = r; break; }
  }
  if (!root) return {found: false};
  try { root.setAttribute('data-phx-sec', arg.path); } catch (_) {}
  // 2) 行数 = 代表字段个数；添加按钮在 root 及其上两层里找（北森把按钮放在区块容器内）
  const prims = ctrls(root).filter(isPrim);
  let btn = null;
  for (let p = root, h = 0; p && p !== document.body && h < 3; p = p.parentElement, h++) {
    const bs = Array.from(p.querySelectorAll('button,a,[role=button],span')).filter((e) => {
      if (e.children.length) return false;
      if (clean(e.innerText) !== arg.addText) return false;
      try { return !e.querySelector('input,textarea'); } catch (_) { return true; }
    });
    if (bs.length) { btn = bs[0]; break; }
  }
  if (btn) { try { btn.setAttribute('data-phx-add', arg.path); } catch (_) {} }
  const res = {found: true, rows: prims.length, hasAdd: !!btn};
  if (!arg.mark) return res;
  // 3) 逐行打点：行容器 =「只含 1 个代表控件」的最近祖先。
  //    只有 1 条时它会一路爬到 root —— 那也是对的（区块里就一行）。
  prims.forEach((pe, k) => {
    let box = pe;
    for (let p = pe.parentElement; p && p !== root.parentElement; p = p.parentElement) {
      let n = 0;
      prims.forEach(x => { if (p.contains(x)) n++; });
      if (n > 1) break;
      box = p;
    }
    try { box.setAttribute('data-phx-row', arg.path + ':' + k); } catch (_) {}
    ctrls(box).forEach((e) => {
      const l = labelOf(e).toLowerCase();
      let best = '', bestLen = 0;
      Object.keys(mapLow).forEach((key) => {
        mapLow[key].forEach((a) => {
          if (a.length > bestLen && l.indexOf(a) >= 0) { best = key; bestLen = a.length; }
        });
      });
      if (best) { try { e.setAttribute('data-phx-field', arg.path + ':' + k + ':' + best); } catch (_) {} }
    });
  });
  return res;
}"""

PHX_RADIO_JS = r"""(arg) => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const out = [];
  Array.from(document.querySelectorAll('.phoenix-radio-group')).forEach((g) => {
    let label = '';
    try {
      const fi = g.closest('.form-item');
      if (fi) { const t = fi.querySelector('.form-item__text'); label = clean(t ? t.innerText : ''); }
    } catch (_) {}
    if (!label) return;
    for (const w of arg.want) {
      if (!w.aliases.some(a => label.indexOf(a) >= 0)) continue;
      const items = Array.from(g.querySelectorAll('.phoenix-radio__radio-text'));
      const hit = items.find(x => clean(x.innerText) === w.value);
      if (hit) {
        try { hit.setAttribute('data-phx-radio', w.key); } catch (_) {}
        out.push(w.key);
      }
      break;
    }
  });
  return out;
}"""


_PARSE_SIG_JS = r"""() => {
  const cs = Array.from(document.querySelectorAll(
    'input.phoenix-input__input,input.phoenix-select__input,textarea'));
  let filled = 0, blob = '';
  cs.forEach((e) => { const v = e.value || ''; if (v) { filled++; blob += v.slice(0, 6) + '|'; } });
  return cs.length + '#' + filled + '#' + blob.length;
}"""


def _wait_parse_settle(page, max_ms=20000, quiet_ms=1800, step=400):
    """上传简历后等站点**自动解析回填**停稳（北森 t≈3s、MokaHR 亦同）。

    为什么必须等：北森点完上传会异步解析简历并把结果写回表单，**期间整张表单重渲染** ——
    正在填的控件被换掉（`_resolve_selector` 报「控件已不在页面上」），刚点「添加」建出来的
    行也会被抹平（实测：3 条项目只剩 1 条，教育起止被解析值覆盖成 2027-01/2027-06）。
    判据用「控件数 + 有值控件数 + 值长度」的签名，连续 quiet_ms 不变即认为稳定。
    """
    last, stable, waited, seen = None, 0, 0, False
    while waited < max_ms:
        try:
            sig = page.evaluate(_PARSE_SIG_JS)
        except Exception:
            sig = None
        if sig is not None and sig == last:
            stable += step
            # ⚠️ 必须**先看到变化**再判稳定：上传后前 2 秒页面还没动静，
            # 直接按「没变就是稳定」会赶在解析开始前放行（实测 t≈3s 才开始回填）。
            if seen and stable >= quiet_ms:
                return True
        else:
            stable = 0
            if sig is not None and last is not None:
                seen = True
        if sig is not None:
            last = sig
        try:
            page.wait_for_timeout(step)
        except Exception:
            break
        waited += step
        # 一直没变化（站点不做解析）→ 8 秒就够，别干等满 20 秒。
        # 原来是 6s：MokaHR 的解析启动有时晚于 6s，太早放行会漏掉回填（实测日期被覆盖）。
        if not seen and waited >= 8000:
            return False
    return False


def _ud_like(page):
    """页面是否是飞书招聘（Formily + UD）表单（只读探测，不点击）。"""
    try:
        return bool(page.eval_on_selector_all(
            ".ud-formily-item, .ud__select, .ud__picker, [class*='ud-formily-item']",
            "els => els.length"))
    except Exception:
        return False


def _phoenix_active(page):
    """页面是否是北森 Phoenix 表单（只读探测，不点击）。"""
    try:
        return bool(page.eval_on_selector_all(
            ".form-item--phoenix, .phoenix-input__input, .phoenix-select__input",
            "els => els.length"))
    except Exception:
        return False


def _phx_wait(page, ms=600):
    try:
        page.wait_for_timeout(ms)
    except Exception:
        pass


def _phx_visible(page, sel):
    """面板是否真的可见。

    ⚠️ 北森每打开一次就在 DOM 里留一份面板（旧的只是隐藏），`document.querySelector`
    取到的是**文档里第一个**（往往是隐藏副本）→ 读年份/点月份都点到了看不见的那份，
    实测把教育「开始时间」填成了 2027-01。必须用 Playwright 的 `:visible` 限定。
    """
    try:
        return page.locator(sel + ":visible").count() > 0
    except Exception:
        return False


def _phx_panel(page, sel):
    """当前**可见**的那个面板（找不到返回 None）。"""
    try:
        loc = page.locator(sel + ":visible")
        return loc.first if loc.count() else None
    except Exception:
        return None


def _phx_open(page, sel):
    """点开选择器：必须点**外层** `.phoenix-select`（点内部 input 不会展开面板）。"""
    try:
        page.eval_on_selector(sel, """e => {
            document.querySelectorAll('[data-phx-trigger]').forEach(
                x => { try { x.removeAttribute('data-phx-trigger'); } catch (_) {} });
            const w = e.closest('.phoenix-select') || e;
            w.setAttribute('data-phx-trigger', '1');
            try { w.scrollIntoView({block: 'center'}); } catch (_) {}
        }""")
    except Exception:
        return False
    for how in ("click", "js"):
        try:
            if how == "click":
                page.click("[data-phx-trigger='1']", timeout=3000)
            else:
                page.eval_on_selector("[data-phx-trigger='1']", "e => e.click()")
            return True
        except Exception:
            continue
    return False


def _phx_first_token(val):
    """多选值取第一个词（资料里「杭州、上海、北京…」→ 单选城市只能选一个）。"""
    parts = [p for p in re.split(r"[、,，/|;；\s]+", str(val or "").strip()) if p]
    return parts[0] if parts else ""


def _phx_iso_date(val):
    """把 2004-12-05 / 2004.12.05 / 2004年12月5日 归一成 YYYY-MM-DD。"""
    m = re.search(r"(\d{4})\D{0,2}(\d{1,2})\D{0,2}(\d{1,2})", str(val or ""))
    if not m:
        return ""
    return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _phx_click_list_item(page, want):
    """在 `.phoenix-selectList` 里点文案匹配的项；虚拟列表找不到就滚动重试。"""
    w = str(want or "").strip()
    if not w:
        return False
    for _round in range(24):
        panel = _phx_panel(page, ".phoenix-selectList")
        if panel is None:
            return False
        try:
            items = panel.locator(PHX_LIST_ITEM)
            n = items.count()
        except Exception:
            n = 0
        for i in range(n):
            try:
                t = (items.nth(i).inner_text() or "").strip()
            except Exception:
                continue
            if t == w or (len(w) >= 2 and (w in t or t in w)):
                try:
                    items.nth(i).click(timeout=2500)
                    return True
                except Exception:
                    continue
        try:
            panel.locator(".phoenix-selectList__virtualList").first.evaluate(
                "e => { e.scrollTop = e.scrollTop + 240; }")
        except Exception:
            return False
        _phx_wait(page, 320)
    return False


def _phx_pick_month(page, val):
    """月份选择器：翻年份到目标年 → 点「N月」。"""
    m = re.search(r"(\d{4})\D{0,2}(\d{1,2})", str(val or ""))
    if not m:
        return False
    year, mon = int(m.group(1)), int(m.group(2))
    panel = _phx_panel(page, PHX_MONTH_PANEL)
    if panel is None:
        return False
    for _ in range(48):
        try:
            raw = panel.locator(".phoenix-calendar-month-panel-year-select").first.inner_text()
            cur = int((raw or "").replace("年", "").strip() or 0)
        except Exception:
            return False
        if cur == year:
            break
        arrow = (".phoenix-calendar-month-panel-next-year-btn" if cur < year
                 else ".phoenix-calendar-month-panel-prev-year-btn")
        try:
            panel.locator(arrow).first.click(timeout=2000)
        except Exception:
            return False
        _phx_wait(page, 200)
    label = "%d月" % mon
    cells = panel.locator(PHX_MONTH_CELL)
    for i in range(cells.count()):
        try:
            if (cells.nth(i).inner_text() or "").strip() == label:
                cells.nth(i).click(timeout=2500)
                return True
        except Exception:
            continue
    return False


def _phx_pick_date(page, val):
    """日期选择器：面板里那个 `.phoenix-calendar-input` 可以直接输入日期，回车即提交。"""
    iso = _phx_iso_date(val)
    panel = _phx_panel(page, PHX_DATE_PANEL)
    if not iso or panel is None:
        return False
    try:
        panel.locator(PHX_DATE_INPUT).first.fill(iso, timeout=3000)
        page.keyboard.press("Enter")
        return True
    except Exception:
        return False


def _phx_pick(page, sel, val, note=None):
    """Phoenix 选择器统一入口：点开 → 判面板类型 → 选值（列表 / 月 / 日）。"""
    v = str(val or "").strip()
    if not v or not _phx_open(page, sel):
        return False
    _phx_wait(page, 700)
    ok = False
    if _phx_visible(page, PHX_MONTH_PANEL):
        ok = _phx_pick_month(page, v)
    elif _phx_visible(page, PHX_DATE_PANEL):
        ok = _phx_pick_date(page, v)
    elif _phx_visible(page, ".phoenix-selectList"):
        ok = _phx_click_list_item(page, _phx_first_token(v))
    if not ok:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False
    _phx_wait(page, 300)
    return True


def _phx_fill_radios(page, profile, note=None):
    """Phoenix 单选（性别 / 是否接受调剂）：纯 div，按组标题 + 选项文案点。

    返回 (已填明细, 已处理字段名)。
    """
    want = []
    g = (profile.get("gender") or "").strip()
    if g:
        want.append({"key": "gender", "aliases": list(FIELD_ALIASES.get("gender") or ["性别"]),
                     "value": _gender_token(g)})
    want.append({"key": "accept_adjust",
                 "aliases": ["是否接受地点调剂", "是否接受调剂", "接受调剂", "服从调剂", "地点调剂"],
                 "value": "是" if profile.get("accept_adjust") else "否"})
    try:
        keys = page.evaluate(PHX_RADIO_JS, {"want": want}) or []
    except Exception:
        return [], []
    done, filled = [], []
    for k in keys:
        try:
            page.click("[data-phx-radio='%s']" % k, timeout=3000)
        except Exception:
            continue
        _phx_wait(page, 350)
        done.append(k)
        val = next((w["value"] for w in want if w["key"] == k), "")
        filled.append("%s→%s" % (k, val))
    return filled, done


def _phx_section_state(page, path, titles, primary, add_text, mapping, mark=False):
    try:
        return page.evaluate(PHX_SECTION_JS, {
            "path": path, "titles": list(titles), "primary": list(primary),
            "aliases": {k: list(v) for k, v in mapping.items()},
            "addText": add_text, "mark": bool(mark)}) or {}
    except Exception:
        return {}


def _split_period(val):
    """「2023.9-2027.6」「2026.07 – 2026.09」→ 起 / 止 两段。"""
    s = _stringify(val)
    if not s:
        return "", ""
    parts = [p.strip() for p in
             re.split(r"\s*(?:–|—|-|~|～|至|到)\s*", s) if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return s.strip(), ""


def _phx_row_values(path, item):
    """一条经历 → 各子字段的值（教育/项目/实习各自的字段名不同）。"""
    out = {}
    s, e = _split_period(item.get("period"))
    if path == "education":
        out["school"] = str(item.get("school") or "")
        out["major"] = str(item.get("major") or "")
        out["degree"] = str(item.get("degree") or "")
    elif path == "projects":
        out["name"] = str(item.get("name") or "")
        out["role"] = str(item.get("role") or "")
        desc = _stringify(item.get("desc"))
        out["desc"] = desc
        out["duty"] = desc          # 「项目中职责」与「项目描述」在本站是两块长文本，同一段经历都适用
    else:
        out["company"] = str(item.get("company") or "")
        out["role"] = str(item.get("role") or "")
        out["highlights"] = _stringify(item.get("highlights"))
    out["start"] = s
    out["end"] = e
    return {k: v for k, v in out.items() if v}


def _phx_fill_control(page, sel, val):
    try:
        cls = page.eval_on_selector(sel, "e => (e.className || '').toString()")
    except Exception:
        return False
    if "phoenix-select" in cls:
        return _phx_pick(page, sel, val)
    try:
        page.fill(sel, str(val), timeout=4000)
    except Exception:
        return False
    got = _read_value(page, sel)
    return bool(got) and _value_ok(got, val)


def _phx_fill_sections(page, profile, note=None):
    """北森可重复区块：点「添加X经历」补足行数 → 逐行按标题填。

    返回 (人类可读报告列表, 已处理的 section 名集合)。
    """
    reports, handled = [], set()
    for path, titles, primary, add_text, mapping in PHX_SECTIONS:
        items = [x for x in (_section_items(profile, path) or []) if isinstance(x, dict)]
        if not items:
            continue
        st = _phx_section_state(page, path, titles, primary, add_text, mapping, mark=False)
        if not st.get("found"):
            if note:
                note("「%s」区块：本站没有这个区块（资料里 %d 条未写入）" % (titles[0], len(items)))
            continue
        rows = int(st.get("rows") or 0)
        clicks = 0
        # 补行：点一次重新判一次行数（站点可能异步渲染），不长行就收手
        while rows < len(items) and st.get("hasAdd") and clicks < 8:
            okc, _how = _click_add(page, "[data-phx-add='%s']" % path)
            if not okc:
                break
            _phx_wait(page, 800)
            clicks += 1
            st2 = _phx_section_state(page, path, titles, primary, add_text, mapping, mark=False)
            nr = int((st2 or {}).get("rows") or 0)
            if nr <= rows:
                st = st2 or st
                break
            rows, st = nr, st2
        if rows < 1:
            if note:
                note("「%s」区块：一条记录都没有（资料里 %d 条未写入）" % (titles[0], len(items)))
            continue
        names, fails = [], []
        for k in range(min(rows, len(items))):
            item = items[k]
            vals = _phx_row_values(path, item)
            if not vals:
                continue
            # 每行前重新打点：填值会触发 React 重渲染，上一轮打的属性可能已经被抹掉
            _phx_section_state(page, path, titles, primary, add_text, mapping, mark=True)
            for key, val in vals.items():
                sel = '[data-phx-field="%s:%d:%s"]' % (path, k, key)
                try:
                    if not page.query_selector(sel):
                        continue
                except Exception:
                    continue
                if not _phx_fill_control(page, sel, val):
                    fails.append("%s#%d.%s" % (path, k + 1, key))
            nm = item.get("company") or item.get("name") or item.get("school") or ("第%d条" % (k + 1))
            names.append(str(nm))
        if not names:
            continue
        handled.add(path)
        rep = "%s %d 条[%s]" % (titles[0], len(names), "、".join(names))
        if clicks:
            rep += "（点了 %d 次「添加」）" % clicks
        if fails:
            rep += "（⚠️ 未填上：%s）" % "，".join(fails)
        reports.append(rep)
    return reports, handled


def fill_page(page, profile, note=None):
    """在**已打开的页面**上填「投递信息」表单（不新开浏览器、**不提交**）。

    从 `_playwright_fill` 抽出来的页面级核心，现在被两处复用：
      ① 旧「断点4」入口 `_playwright_fill`：打开投递入口页 → 填表；
      ② `apply_by_url` 登录成功**之后**：在同一个页面上继续填投递表单。
    返回一句人类可读的摘要（已填 / 未命中 / 简历上传）。
    """
    flat = _flatten(profile)
    # 是否接受调剂：窗口里是 checkbox（布尔），填表时转成「是 / 否」
    if "accept_adjust" in flat:
        flat["accept_adjust"] = "是" if profile.get("accept_adjust") else "否"
    resume = profile.get("resume_pdf", "") or ""

    def n_(m):
        if note:
            try:
                note(m)
            except Exception:
                pass

    # ---- 经历区块预处理：飞书（Formily/UD）与北森（Phoenix）各一套，**按站点分流** ----
    # 早期无条件跑飞书那套：在北森页会输出「本站没找到可填的卡片」这种误导性 note，
    # 还会白采集一遍页面。
    _ud_array_done, _ud_array_keys = [], []
    _phx_reports, _phx_handled, _phx_radio_keys, _phx_radio_filled = [], set(), [], []
    if _ud_like(page):
        _ud_prepare_sections(page, profile, n_)
        _ud_array_done, _ud_array_keys = _ud_fill_arrays(page, profile, n_)

    # ---- 采集文本类控件 ----
    fields = _collect_fields(page)

    # ---- 采集 radio 组（性别等）----
    radio_groups = page.eval_on_selector_all(
        "input[type=radio]",
        """els => {
            const g = {};
            els.forEach(e => {
                const nm = e.getAttribute('name') || '';
                if (!nm) return;
                let label = '';
                try { const l = e.closest('label'); if (l) label = l.innerText || ''; } catch(_) {}
                if (!label) {
                    const id = e.getAttribute('id');
                    if (id) { const l = document.querySelector('label[for="' + id + '"]'); if (l) label = l.innerText || ''; }
                }
                if (!label) label = e.getAttribute('value') || '';
                g[nm] = g[nm] || {name: nm, options: []};
                g[nm].options.push({value: e.getAttribute('value') || '', label: (label || '').trim()});
            });
            return Object.values(g);
        }""",
    )

    # ---- 采集文件上传控件，识别「简历上传」入口（提到最前：优先上传简历附件）----
    file_inputs = page.eval_on_selector_all(
        "input[type=file]",
        """els => els.map(e => {
            let label = '';
            try { if (e.labels && e.labels[0]) label = e.labels[0].innerText || ''; } catch(_) {}
            if (!label) label = e.getAttribute('aria-label') || '';
            // T82：北森「简历附件」既无 label[for] 也无 aria-label，标题只在
            // `.form-item__text` 里 → 读不到就匹配不上，简历永远不上传。
            if (!label) {
                try {
                    const fi = e.closest('.form-item');
                    const t = fi ? fi.querySelector('.form-item__text') : null;
                    if (t) label = t.innerText || '';
                } catch (_) {}
            }
            const id = e.getAttribute('id') || '';
            const nm = e.getAttribute('name') || '';
            return { id, nm, label: (label||'').trim(),
                     ctx: ((label||'') + ' ' + (nm||'') + ' ' + (id||'')).toLowerCase() };
        })""",
    )

    # ---- 优先上传简历 PDF（仅命中「简历/附件」类入口，避免误传头像等）----
    # 顺序要求：先传附件，再按个人信息补全其余字段。简历上传通常触发站点解析、
    # 自动带出部分字段，手动填写作为补充（不覆盖站点已解析内容）。
    uploaded = False
    upload_targets = []
    if resume and os.path.exists(resume):
        hay_all = lambda f: ((f.get("label", "") or "") + " " + (f.get("ctx", "") or "")).lower()
        matched = [f for f in file_inputs
                   if any(h in hay_all(f) for h in ("简历", "resume", "cv", "履历"))]
        if not matched and len(file_inputs) == 1:
            matched = file_inputs  # 整页仅一个文件控件，通常即简历上传
        for f in matched:
            sel = ("#" + f["id"]) if f.get("id") else (
                "input[name='%s']" % f["nm"] if f.get("nm") else "input[type=file]")
            try:
                page.set_input_files(sel, resume)
                uploaded = True
                upload_targets.append(f.get("label") or f.get("id") or f.get("nm") or "file")
            except Exception:
                pass

    # ---- 简历刚上传时站点会异步解析回填（会重渲染整张表单）→ 先等它停稳 ----
    # 不等的话：姓名/邮箱报「控件已不在页面上」，刚建的 2 行经历被抹掉（实测虹软）。
    if uploaded:
        try:
            # **所有站点都要等**（2026-10-28 实测 MokaHR 同样会解析回填）：
            # 鹰角「交互策划」上传简历后站点把教育起止自动写成 2027/6 ~ 2027/6，
            # 我们写入的 2023-09 被覆盖 → `_write_date_box` 读回不符 → 整段日期判失败。
            # 该函数自身有上限（无变化时 ~8s 返回），对不做解析的站点代价可控。
            _wait_parse_settle(page)
        except Exception:
            pass

    # 北森的区块与自定义单选必须**等解析回填之后**再做：解析会重渲染整张表单，
    # 先填好的经历会被解析值覆盖、刚点出来的行会被抹掉（实测 3 条只剩 1 条）。
    if _phoenix_active(page):
        _phx_radio_filled, _phx_radio_keys = _phx_fill_radios(page, profile, n_)
        _phx_reports, _phx_handled = _phx_fill_sections(page, profile, n_)
        for _p in _phx_handled:          # 已按条目填完的 section：别让一对一逻辑再填一遍
            _blank_section_flat(flat, _p, ITEM_ALIASES.get(_p, {}).keys())

    # ---- 可重复区块（项目经历 / 工作经历）：先点站点「新增」补足区块，再**逐条目**填 ----
    # 不这样做的话：match_fields 一个路径只能用一次 → 第 2/3 个项目根本无处可去（用户报的 bug）。
    # ⚠️ 计数列表必须**在这里**初始化：下面「执行填写」原来才是第一处赋值，
    #    若沿用旧位置，这里的 extend 会 NameError，而且重复区块的成果会被二次初始化抹掉。
    filled, failed, uncertain = [], [], []
    repeat_reports = []
    _pass_state = {"added": False}

    def _run_repeat_pass(repair=False):
        """跑一遍所有可重复区块（`repair=True` 是第二遍「修补」）。

        T72：**必须留一遍修补**。站点点「新增」时会把整张表单重渲染，已经填好的
        **自定义日期控件会被清空**（实测全志：填完教育行 2023.09–2027.06，点两次
        「实习经历」的「新增」后，教育那行 4 个日期框全变回空）。第二遍时区块数已够
        → 不再点「新增」→ DOM 稳定 → 把被清空的值补回来；且**不重复计数**。
        """
        nonlocal fields
        for _p in _LIST_PATHS:
            if _p in _phx_handled:   # T82：北森区块已由 Phoenix 适配器填过，别再点一次「添加」
                continue
            try:
                _r = _fill_repeat_section(page, profile, _p, note=n_)
            except Exception as _e:                    # noqa: BLE001
                n_("可重复区块处理异常（%s）：%s" % (_p, type(_e).__name__))
                continue
            if not _r:
                continue
            # 页面已变（点过多块）→ 用**新采集**的控件，并扣掉已消费的，免得下面又填一遍
            fields = [f for f in _r["fields"] if f["idx"] not in _r["consumed"]]
            # 该 section 已按条目处理完，别让一对一逻辑再动它（education 要清的是子路径）
            _blank_section_flat(flat, _p, ITEM_ALIASES.get(_p, {}).keys())
            if _r.get("clicks"):
                _pass_state["added"] = True
            if repair:
                # 修补遍：只记「这一遍才补上的」，避免同一项被重复计入「已填 N 项」
                newf = [x for x in (_r.get("filled") or []) if x not in filled]
                if newf:
                    filled.extend(newf)
                    n_("修补：把站点重渲染时清掉的 %d 项补回来 [%s]"
                      % (len(newf), "，".join(newf)))
                for x in (_r.get("failed") or []):
                    if x not in failed:
                        failed.append(x)
                for x in (_r.get("uncertain") or []):
                    if x not in uncertain:
                        uncertain.append(x)
            else:
                repeat_reports.append(_r)
                filled.extend(_r["filled"])
                failed.extend(_r["failed"])
                uncertain.extend(_r["uncertain"])

    _run_repeat_pass(repair=False)
    if _pass_state["added"]:
        n_("「新增」触发过页面重渲染：对可重复区块**再跑一遍**，修补被站点清空的自定义控件")
        _run_repeat_pass(repair=True)

    assignments, unfilled = match_fields(flat, fields)
    for _k in _ud_array_keys:
        while _k in unfilled:
            unfilled.remove(_k)
    # T82：Phoenix 已按区块填过的 path（及其子字段）不再算「未命中」——
    # 否则摘要会自相矛盾：「项目经历 3 条已写入」+「未命中 projects」。
    for _k in _phx_handled:
        for _u in list(unfilled):
            if _u == _k or _u.startswith(_k + "."):
                unfilled.remove(_u)
    for _k in _phx_radio_keys:
        while _k in unfilled:
            unfilled.remove(_k)
    if _phx_radio_filled:
        filled.extend(_phx_radio_filled)
    n_("投递表单采集到 %d 个文本控件、%d 组单选（含自定义单选 %d 组）；"
       "字段匹配命中 %d 项、未命中 %d 项"
       % (len(fields), len(radio_groups) + len(_phx_radio_keys), len(_phx_radio_keys),
          len(assignments), len(unfilled)))
    for _r in repeat_reports:
        n_(_r["report"])

    # ---- 执行填写（filled/failed/uncertain 已在上面初始化，勿重复赋值）----
    for path, f, s in assignments:
        val = flat[path]
        label = f.get("label") or f.get("ph") or f.get("nm") or f.get("id")
        try:
            ok, how = _fill_field_smart(page, f, val, note=n_)
        except Exception as e:                        # noqa: BLE001
            failed.append("%s→%s(%s)" % (path, label, type(e).__name__))
            continue
        if ok is True:
            filled.append("%s→%s" % (path, label))
        elif ok == "uncertain":
            # 值写进去了，但**站点模型是否采纳无法确证**（JS 兜底路径）→ 单列出来，
            # 不混进「已填」：诚实优先，绝不把可能没生效的字段报成已填。
            uncertain.append("%s→%s(%s)" % (path, label, how))
        else:
            failed.append("%s→%s(%s)" % (path, label, how))

    # ---- 性别 radio ----
    gender_val = (profile.get("gender") or "").strip()
    if gender_val:
        want = _gender_token(gender_val).lower()
        for g in radio_groups:
            gtext = (g.get("name", "") + " " +
                     " ".join(o.get("label", "") for o in g.get("options", []))).lower()
            if not any(a.lower() in gtext for a in FIELD_ALIASES["gender"]):
                continue
            for o in g.get("options", []):
                otxt = (o.get("label", "") + " " + o.get("value", "")).lower()
                if want in otxt or (want == "男" and "male" in otxt) or \
                   (want == "女" and "female" in otxt):
                    try:
                        page.check(
                            "input[type=radio][name='%s'][value='%s']"
                            % (g["name"], o.get("value", ""))
                        )
                        filled.append("gender→%s" % o.get("label", ""))
                        if "gender" in unfilled:
                            unfilled.remove("gender")
                    except Exception:
                        pass
                    break

    # ---- 简历上传已在前面（优先上传简历附件）完成，这里直接进入摘要 ----

    summary = (
        "本地 computer-use 填表完成（未提交）：已填 %d 项 [%s]；"
        "简历上传=%s%s；未命中 %d 项 [%s]"
        % (len(filled), "，".join(filled) or "无", uploaded,
           ("（" + "、".join(upload_targets) + "）") if upload_targets else "",
           len(unfilled), "，".join(unfilled) or "无")
    )
    if _ud_array_done:
        summary += "；经历区块：" + "；".join(_ud_array_done)
    if _phx_reports:
        summary += "；经历区块：" + "；".join(_phx_reports)
    if failed:
        summary += "；填写失败 %d 项 [%s]" % (len(failed), "，".join(failed))
    if uncertain:
        summary += "；待确认 %d 项 [%s]" % (len(uncertain), "，".join(uncertain))
    if repeat_reports:
        # 可重复区块的结果必须写进摘要：否则用户只看到「已填 N 项」，不知道项目经历填了几条
        summary += "；" + "；".join(r["report"] for r in repeat_reports)
    return summary


def _playwright_fill(url, profile, page=None):
    """本地 computer-use：Playwright 驱动 Chrome 确定性填表（无外部 AI）。

    page 不为 None 时**复用这个已打开的页面**（不重新 goto、不新开浏览器）。
    """
    if page is not None:
        return fill_page(page, profile)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", args=["--no-sandbox"])
        try:
            pg = browser.new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=25000)
            pg.wait_for_timeout(3000)
            return fill_page(pg, profile)
        finally:
            browser.close()


def _fill_select(page, f, val):
    """`<select>`：优先选文案包含目标值的 option，其次选被包含，都不中则返回 False。"""
    opts = f.get("opts") or []
    if not opts:
        return False
    v = str(val).strip().lower()
    for o in opts:
        if v and v in o.lower():
            page.select_option(_selector_for(f), label=o)
            return True
    for o in opts:
        if o.lower() and o.lower() in v:
            page.select_option(_selector_for(f), label=o)
            return True
    return False


# ============ 投递流程（断点3+4，含人机验证人工交接） ============

HUMAN_VERIFY_HINTS = [
    "人机验证", "安全验证", "拖动滑块", "拖动下方滑块", "滑块验证", "请完成验证",
    "请完成安全验证", "图形验证", "点击验证", "向右滑动", "按住滑块", "请按住滑块",
    "滑动验证", "拼图", "请拖动", "人机识别", "行为验证",
    "验证码校验", "captcha", "verify you are human", "recaptcha", "hcaptcha",
]

# 弱证据词（2026-09-23）：新版旋转/点选类验证码的**指令词**。
# ⚠️ 这些词在普通页面文案里也会出现（职位描述、公司介绍里的「旋转/网格/点选」），
# 所以**必须配合「页面上真的挂着可见验证控件」才采信** —— 否则普通岗位页会被误判成
# 「有人机验证」，而一键投递的 human_wait=0 会让岗位**立刻**卡在「需人工验证」不投递。
HUMAN_VERIFY_WEAK_HINTS = ["旋转", "转动", "转正", "rotate", "依次点击", "按顺序点击",
                           "顺序点击", "点选", "网格", "宫格", "九宫格"]

# 验证控件元素的公共选择器（`detect_human_verification` 与「弱证据词采信」共用）
_HV_SEL = ("iframe[src*='captcha'],iframe[src*='recaptcha'],iframe[src*='hcaptcha'],"
           "iframe[src*='geetest'],iframe[src*='yidun'],iframe[src*='aliyun'],"
           "[class*='captcha'],[id*='captcha'],[class*='slider'],[class*='verify'],"
           "[class*='nc_'],[class*='geetest'],[class*='yidun'],[class*='slide-verify'],"
           "[class*='drag'],[class*='verify-wrap'],[class*='verify-box']")

# 验证码**形态**关键词（classify_verify_kind 用）。
# ⚠️ 判定**只取验证控件自身的文案**（`_widget_text`），绝不能扫整页：
# 验证码常是嵌在岗位页里的登录弹窗，整页文案里的职位描述/页脚协议会命中「网格 / 旋转」，
# 于是把**滑块**误判成 grid/rotate → 我们拒绝自动过验证 → 岗位直接卡在「需人工验证」不投递。
_ROTATE_HINTS = ["旋转", "转动", "转正", "rotate", "请将图", "将图片转", "摆正", "转一圈"]
_GRID_HINTS = ["依次点击", "按顺序点击", "按顺序点", "点选文字", "点选图中",
               "请点击图中", "九宫格", "宫格", "网格", "顺序点击"]
# 「按住滑块拖到最右端」型（geetest / 阿里行为验证常见）：**不需要识别缺口**，
# 拖到轨道最右端就行；CV 在这类图上必然报「未找到缺口轮廓」。
_SLIDER_END_HINTS = ["拖到最右", "拖到右端", "拖动到最右", "滑到最右", "滑到右端",
                     "向右滑到底", "滑到底", "最右边", "最右端", "最右侧", "拖到底部"]
_SLIDER_HINTS = ["滑块", "拖动", "向右滑", "按住", "拼图", "slide", "drag"]

_KIND_CN = {"slider": "滑块拼图(slider)", "slider_end": "拖到最右端(slider_end)",
            "rotate": "旋转图像(rotate)", "grid": "点选网格(grid)",
            "unknown": "未识别(unknown)"}


def _widget_text(page, widget):
    """取**验证控件自身**的可见文案；逐级退化：控件 → 控件所在 frame → 非主 frame → 整页。

    最后一级退化到整页是不得已（个别 iframe 型验证码读不到文案）；前三级命中时就不会
    把整页职位描述算进来，从根上避免「滑块被误判成网格/旋转」。
    """
    txt = ""
    if isinstance(widget, dict):
        try:
            el = widget.get("el")
            if el is not None:
                txt = el.inner_text() or ""
        except Exception:
            txt = ""
        if not txt.strip():
            try:
                wf = widget.get("frame")
                if wf is not None:
                    txt = wf.inner_text("body") or ""
            except Exception:
                txt = ""
    if txt.strip():
        return txt
    try:
        main = getattr(page, "main_frame", None)
        for fr in page.frames:
            if main is not None and fr is main:
                continue
            try:
                txt += " " + (fr.inner_text("body") or "")
            except Exception:
                continue
    except Exception:
        pass
    if txt.strip():
        return txt
    try:
        for fr in page.frames:                      # 最后兜底：整页（含主 frame）
            txt += " " + (fr.inner_text("body") or "")
    except Exception:
        pass
    return txt


def classify_verify_kind(page, widget=None, widget_piece=None):
    """判断验证码**形态**：slider / slider_end / rotate / grid / unknown。返回 (kind, 证据词)。

    规则（保守优先，**宁可多试一次滑块，也不要误拒**）：
      · 控件内有独立拼图块 → slider（结构证据，最硬）；
      · 控件自身文案命中「拖到最右端」→ slider_end（不需要识别缺口，直接拖到底）；
      · 命中旋转词 → rotate；命中点选/网格词 → grid → 这两种我们**不做**，直接转人工；
      · 任何证据都没有 → unknown，调用方**按 slider 默认流程尝试**（不再拒绝）。
    """
    piece = widget_piece if widget_piece is not None else (
        widget.get("piece") if isinstance(widget, dict) else None)
    if piece is not None:
        return "slider", "控件内存在独立拼图块"
    low = (_widget_text(page, widget) or "").lower()
    for k, hints in (("slider_end", _SLIDER_END_HINTS), ("rotate", _ROTATE_HINTS),
                     ("grid", _GRID_HINTS), ("slider", _SLIDER_HINTS)):
        for h in hints:
            if h.lower() in low:
                return k, h
    return "unknown", ""

# 岗位名里对「在列表页定位职位」无区分度的通用词
_TITLE_STOP = set(
    ["产品", "经理", "实习生", "工程师", "助理", "专员", "北京", "上海", "深圳",
     "广州", "杭州", "成都", "武汉", "南京", "西安", "校招", "校园", "招聘", "正式", "岗位"]
)


def detect_human_verification(page):
    """返回人机验证描述（命中则非空）。扫描所有 frame 文本 + 常见 captcha 元素。

    **顺序很重要**：① 强证据词 → ② 可见验证控件 → ③ 只有真的挂着控件时才采信弱证据词。
    弱证据词（旋转/点选/网格）在普通文案里也会出现，不加这道门禁就会把普通岗位页
    误判成「有人机验证」，而一键投递 human_wait=0 → 岗位立刻卡在「需人工验证」不投递。
    """
    low = ""
    try:
        txt = ""
        for fr in page.frames:
            try:
                txt += " " + (fr.inner_text("body") or "")
            except Exception:
                continue
        low = txt.lower()
    except Exception:
        low = ""
    for h in HUMAN_VERIFY_HINTS:
        if h.lower() in low:
            return h
    has_widget = False
    try:
        for e in page.query_selector_all(_HV_SEL):
            try:
                if e.is_visible():
                    has_widget = True
                    break
            except Exception:
                continue
    except Exception:
        pass
    if has_widget:
        for h in HUMAN_VERIFY_WEAK_HINTS:
            if h.lower() in low:
                return h
        return "验证控件"
    return ""


def _code_sent(page):
    """判断「获取验证码」是否已真正发出：按钮进入倒计时（XXs后重发/重新获取）或出现已发送提示。
    若未发出 → 通常是被「人机验证」拦下，需要人工介入。"""
    import re
    try:
        txt = page.inner_text("body")
    except Exception:
        return False
    # ⚠️ 本机实测（2026-09-23）：旧正则只认「59s后重发」，**不认**这些常见写法：
    #   「60秒后重新获取」「重新获取(59)」「重新获取 60s」
    # → 验证码明明已经发出，却被判成「没发出」→ 取码前的「不空等」保护会误杀正常流程、
    #   岗位报「需人工验证」且不投递。所以这里必须把中文/括号倒计时一起认了。
    if re.search(r"\d+\s*(?:秒|s|S)?\s*后\s*(?:重发|重新获取|再获取|重试|重新发送|再次获取)", txt):
        return True
    if re.search(r"(?:重发|重新获取|再次获取|重新发送)\s*[（(]?\s*\d+\s*[sS秒]?", txt):
        return True
    for k in ("验证码已发送", "发送成功", "验证码发送成功", "已向该手机号发送",
              "已发送至", "验证码已发至"):
        if k in txt:
            return True
    return False


def _title_keywords(title):
    """从岗位名提取用于列表页定位职位的关键词（角色词优先，过滤派生噪声）。"""
    import re
    t = re.sub(r"[（(].*?[)）]", "", title or "").strip()
    parts = re.split(r"[\s/·\-—|,，、]+", t)
    noise = ("正文", "命中", "【", "】", "(", ")", "（）")
    kws = []
    # 角色兜底：含「产品」→ 先试「产品经理」（站点职位名常为「XX-产品经理-YY」）
    if "产品" in t:
        kws.append("产品经理")
    for p in parts:
        p = p.strip()
        if len(p) >= 2 and p not in _TITLE_STOP and not any(n in p for n in noise):
            kws.append(p)
    if not kws and t:
        kws = [t[:8]]
    seen, out = set(), []
    for k in kws:
        if k not in seen:
            seen.add(k); out.append(k)
    return out


def _click_by_text(page, texts, timeout_ms=3000):
    """依次尝试点击页面中文本命中 texts 之一的元素，返回命中的关键词。
    先试 Playwright locator 点击，失败则退回 JS 直接点击（兼容非标准可点元素）。"""
    for kw in texts:
        # A) locator 点击
        try:
            loc = page.locator("text=%s" % kw)
            if loc.count() > 0:
                el = loc.first
                el.scroll_into_view_if_needed(timeout=2000)
                el.click(timeout=timeout_ms)
                return kw
        except Exception:
            pass
        # B) JS 点击兜底：点最深的、文本命中且可见的元素
        try:
            ok = page.evaluate("""(kw) => {
                const vis = e => { const r=e.getBoundingClientRect(); return r.width>0&&r.height>0; };
                const els = Array.from(document.querySelectorAll('div,span,a,button,em,strong,p,label'))
                    .filter(e => e.children.length===0 && vis(e) && (e.innerText||'').includes(kw));
                if (!els.length) return false;
                const el = els[0];
                el.scrollIntoView({block:'center'});
                el.click();
                return true;
            }""", kw)
            if ok:
                return kw
        except Exception:
            pass
    return ""


def _fill_first(page, labels, value):
    """找 placeholder/name/id 命中 labels 的输入框并填入，返回命中标签。"""
    for lab in labels:
        for sel in ("input[placeholder*='%s']" % lab,
                    "input[name*='%s']" % lab,
                    "input[id*='%s']" % lab):
            try:
                el = page.query_selector(sel)
                if el:
                    el.scroll_into_view_if_needed(timeout=2000)
                    el.fill(value)
                    return lab
            except Exception:
                continue
    return ""


AGREE_ROW_TEXTS = ["隐私协议", "隐私政策", "隐私条款", "隐私权政策", "隐私声明",
                   "用户协议", "服务协议", "投递协议", "招聘协议", "个人信息保护",
                   "我已阅读", "已阅读并同意", "同意并阅读", "同意并接受"]

# 找「协议行」并在其中定位 / 点击 checkbox。click=False 时只探测状态（不点）。
#
# 两个 DOM 标记（防止「重试 = 把已勾选的又点掉」）：
#   data-rs-agreed="1"  已有勾选成功的证据（点了之后 class 变了 / 本来就是勾的）
#   data-rs-tried="1"   已点过一次 —— 同一个框在本页面内**永不重复点**，
#                       否则「第一次点勾上、第二次点取消」会让流程反复横跳。
_AGREE_FIND_JS = """(args) => {
  const kws = args.kws, doClick = args.click;
  const A = 'data-rs-agreed', T = 'data-rs-tried';
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  const clsOf = e => ((e && e.className) ? String(e.className) : '');
  const attr = (e, k) => (e && e.getAttribute) ? e.getAttribute(k) : null;
  const boxChecked = b => {
    if (!b) return false;
    if (attr(b, A) === '1') return true;
    if (b.checked === true) return true;
    if (attr(b, 'aria-checked') === 'true') return true;
    return /checked|active|selected|--on|\\bon\\b|is-check|agree-on/i.test(
        clsOf(b) + ' ' + clsOf(b.parentElement));
  };
  const isBoxLike = c => /checkbox|checked|agree|protocol|read|radio|icon/i.test(clsOf(c));
  // 链接判定：**绝不能点协议里的《隐私声明》链接** —— 那会跳转到协议页，
  // 表现就是「勾选框不会被勾选」（旧代码的兜底会把文字元素当控件点）。
  const isLink = c => {
    if (!c) return false;
    if ((c.tagName || '').toUpperCase() === 'A') return true;
    const href = (c.getAttribute && c.getAttribute('href')) || '';
    if (href && href !== '#' && href.indexOf('javascript:') !== 0) return true;
    if (c.querySelector && c.querySelector('a[href]')) return true;
    return false;
  };
  const isSmallLeaf = c => {
    if (c.children.length !== 0) return false;
    const r = c.getBoundingClientRect();
    return r.width >= 8 && r.width <= 36 && r.height >= 8 && r.height <= 36;
  };
  // ① 最内层的「含关键词」元素（这通常只是那段文字本身，不是整行）
  const leaves = Array.from(document.querySelectorAll('span,div,p,label,li,em,strong,a,i'))
      .filter(e => {
        if (!vis(e)) return false;
        const t = norm(e.innerText);
        if (!t || !kws.some(k => t.includes(k))) return false;
        return !Array.from(e.children).some(c => {
          const ct = norm(c.innerText);
          return ct && kws.some(k => ct.includes(k));
        });
      });
  const out = { leaves: leaves.length, items: [] };
  const seen = [];
  for (const leaf of leaves.slice(0, 4)) {
    // ② 从文字向上找**最低的**「既含这段文字、又含勾选控件」的祖先
    //    （线上失败原因：只在该文字元素内部找控件，而 checkbox 是它的兄弟节点）
    let host = null, box = null, n = leaf;
    for (let up = 0; up < 6 && n; up++) {
      const t = norm(n.innerText);
      if (t.length > 160) break;
      const native = n.querySelector ? n.querySelector('input[type=checkbox]') : null;
      if (native) { host = n; box = native; break; }
      const all = (n.querySelectorAll ? Array.from(n.querySelectorAll('*')) : []);
      const cands = all.filter(c => isBoxLike(c) || isSmallLeaf(c));
      if (cands.length) { host = n; box = cands.find(isBoxLike) || cands[0]; break; }
      n = n.parentElement;
    }
    if (!host || !box) {
      // ②b 兜底：控件可能是协议文字的**兄弟 / 下方相邻**节点
      //     （用户报的「下方『同意并阅读隐私声明』勾选框不会被勾选」就是这种布局）。
      //     从文字向上放宽到 4 层，用 input[type=checkbox] / label[for] / 小方块三种证据找控件。
      let n2 = leaf;
      for (let up2 = 0; up2 < 4 && n2; up2++) {
        const scope = n2.parentElement || n2;
        if (!scope.querySelector) break;
        const found = [];
        const nat = scope.querySelector('input[type=checkbox]');
        if (nat) found.push(nat);
        for (const l of Array.from(scope.querySelectorAll('label[for]'))) {
          const fid = l.getAttribute('for');
          if (fid) { const t = document.getElementById(fid); if (t) found.push(t); }
        }
        for (const c of Array.from(scope.querySelectorAll('*'))) {
          if (isBoxLike(c) || isSmallLeaf(c)) found.push(c);
        }
        if (found.length) { host = scope; box = found[0]; break; }
        n2 = scope;
      }
    }
    let soft = false;
    if (!host || !box) {
      // 只找到「协议文字」本身、没找到任何勾选控件 → 标记 soft（供上层区分，
      // 不要把「没有控件」误报成「有控件但没勾上」）
      host = leaf.parentElement || leaf; box = leaf; soft = true;
    }
    if (seen.indexOf(host) >= 0) continue;
    seen.push(host);

    const before = clsOf(box) + '|' + (box.checked === true);
    const was = boxChecked(box);
    let tried = false;
    if (doClick && attr(box, T) !== '1' && !boxChecked(box) && !isLink(box)) {
      try { box.scrollIntoView({ block: 'center' }); } catch (e) {}
      try { box.click(); } catch (e) {}
      // 仍没勾上 → 点整行（多数站点是 label，点文字即切换）
      if (!boxChecked(box)) { try { host.click(); } catch (e) {} }
      tried = true;
      if (box.setAttribute) {
        box.setAttribute(T, '1');
        if (clsOf(box) !== before.split('|')[0]) box.setAttribute(A, '1');
      }
    }
    const after = clsOf(box) + '|' + (box.checked === true);
    out.items.push({
      row: norm(host.innerText).slice(0, 50),
      tag: (box.tagName || '').toLowerCase(),
      cls: clsOf(box).slice(0, 40),
      was: was, now: boxChecked(box), tried: tried, changed: before !== after,
      soft: soft,
    });
  }
  return out;
}"""


def _agree_terms(page):
    """勾选「我已阅读并同意《隐私协议 / 用户协议》」类控件。

    真实站点的做法（实测）：**找到文本含「隐私协议」的那一行，点该行里的 checkbox**。
    旧实现只按 `[class*="checkbox"]` 猜容器、并要求容器文本命中关键词，遇到 class
    命名不规则的站点就整个漏掉 → 「获取验证码」按钮始终禁用 → 表现为
    「卡在未勾选隐私政策」。

    新实现：
      ① 找「协议行」= 可见、文本含协议关键词、且**文本较短**（≤120 字）的最内层元素
         （防止命中整页容器而点错东西）；
      ② 在该行内依次找 checkbox：`input[type=checkbox]` → class 含
         checkbox/icon/agree/protocol 且是 8~44px 的小元素 → 行内第一个 10~36px 小方块
         → 行内第一个元素 / 行本身；
      ③ 点它；若仍未勾上，再点「行」本身（label 语义：点文字即切换）；
      ④ 返回点击前后可核对的结果（供上层判断是否真的勾上了）。
    """
    try:
        res = page.evaluate(_AGREE_FIND_JS, {"kws": AGREE_ROW_TEXTS, "click": True})
    except Exception:
        res = None
    items = (res or {}).get("items") or []
    if not items:
        # 兜底：显式「同意」类按钮/链接
        hit = _click_by_text(page, ["同意并继续", "同意并接受", "阅读并同意",
                                    "同意用户协议", "同意并勾选"])
        return ("点击「%s」" % hit) if hit else ""
    parts = []
    for it in items:
        if it.get("soft"):
            mark = "只找到协议文字、未找到勾选框"
        elif it["now"]:
            mark = "已勾选"
        elif it["tried"]:
            mark = "已点击但未确认勾选"
        else:
            mark = "未勾选（本轮不再重复点击）"
        parts.append("%s：%s" % (mark, it["row"][:26]))
    return "；".join(parts[:3])


def _terms_state(page):
    """协议勾选状态：'checked'（已勾）/ 'unchecked'（有控件但没勾上）/ 'none'（没找到）。

    用于**失败可诊断**：只有当它能明确说「有协议控件但未勾选」时，上层才会去补勾/重试，
    避免误点已勾选的行反而取消勾选，也避免无脑重发短信。
    """
    try:
        res = page.evaluate(_AGREE_FIND_JS, {"kws": AGREE_ROW_TEXTS, "click": False})
    except Exception:
        return "none"
    # 只统计**真正的勾选控件**：soft=True 表示那条只是「协议文字」、根本没找到控件，
    # 不能算「有控件但没勾上」（否则误报 unchecked → 上层来回补勾 + 无谓告警）。
    items = [it for it in ((res or {}).get("items") or []) if not it.get("soft")]
    if not items:
        return "none"
    return "checked" if any(it["now"] for it in items) else "unchecked"


def _force_check_terms(page):
    """兜底直勾：枚举页面上「可见且未勾选」的 checkbox，挑**所在行文本命中协议关键词**的调
    Playwright 的 `check()`（会校验最终态，勾不上就抛错，不会假装成功）。

    为什么需要它：`_agree_terms` 走的是「关键词 → 文字元素 → 向上找控件」，遇到
    「勾选框与协议文字上下分行」的布局（用户报的那类）可能定位不到；这里改成
    「直接枚举所有 checkbox，用所在行文案过滤」，与布局无关。
    只 check 不 uncheck —— 绝不动站点默认已勾好的授权框。
    """
    kws = AGREE_ROW_TEXTS
    try:
        boxes = page.query_selector_all("input[type=checkbox]")
    except Exception:
        return ""
    done = []
    for b in boxes:
        try:
            if b.is_checked():
                continue
            row = b.evaluate(r"""e => {
                let n = e, t = '';
                for (let i = 0; i < 4 && n; i++) {
                    n = n.parentElement; if (!n) break;
                    const s = (n.innerText || '').replace(/\s+/g, ' ').trim();
                    if (s) { t = s; if (s.length > 12) break; }
                }
                return t.slice(0, 140);
            }""") or ""
        except Exception:
            continue
        if not any(k in row for k in kws):
            continue
        try:
            b.check(timeout=2500)
        except Exception:
            try:
                b.check(timeout=2500, force=True)     # 自定义样式（0×0 隐藏态）的兜底
            except Exception:
                # 0×0 的元素连 force 都救不了（报「Element is outside of the viewport」），
                # 这时用**原生 DOM click**：它是真正的 click 事件（框架的 click/change
                # 监听照样触发），并且**事后回读 checked 校验**，绝不假装成功。
                try:
                    b.evaluate("e => e.click()")
                except Exception:
                    continue
                if not b.is_checked():
                    continue
        done.append(row.strip()[:26])
    return "；".join(done[:2])


def _code_btn_disabled(page):
    """「获取验证码」按钮是否处于**禁用态**。

    这类站点用「协议勾选」当门禁：没勾上时按钮 disabled，点了也没反应。
    识别出来就先去补勾协议，而不是傻等 Playwright 的点击超时。
    """
    try:
        return bool(page.evaluate("""() => {
            const vis = e => { const r=e.getBoundingClientRect(); return r.width>0&&r.height>0; };
            const T = ['获取验证码','发送验证码','获取短信验证码','发送校验码','获取动态码'];
            const els = Array.from(document.querySelectorAll('button,div,span,a'))
                .filter(e => vis(e) && T.indexOf((e.innerText||'').trim()) >= 0);
            if (!els.length) return false;
            for (const e of els) {
                const dis = !!e.disabled
                    || /disabled|is-disabled|btn-disabled/i.test(String(e.className||''))
                    || (e.getAttribute && e.getAttribute('aria-disabled') === 'true');
                if (dis) return true;
            }
            return false;
        }"""))
    except Exception:
        return False


def _page_alive(page):
    """页面还在不在（用于「异常后能不能真的驻留」与各处的安全判定）。"""
    try:
        return bool(page) and not page.is_closed()
    except Exception:
        return False


# 「列表页」的 URL 特征：站点把菜单/导航开成新标签时，落地页就是这些
_LIST_URL_HINTS = ("#/jobs", "/jobs?", "/jobs#", "/joblist", "/positions",
                   "/position/list", "anchorName=jobsList")


def _url_looks_list(u):
    """URL 是不是「岗位列表/首页」。用于两件事：
      ① 点投递入口后页面退回列表 → 说明点到了导航链接（不是投递按钮）；
      ② 站点新开的标签页是列表 → 不能收养它、更不能关掉当前详情页。
    `#/job/<uuid>` 这种详情页**不算**列表（虽然含 "/job"）。"""
    s_ = (u or "").lower()
    if not s_:
        return False
    if "/job/" in s_ and "/jobs" not in s_:
        return False
    return any(h.lower() in s_ for h in _LIST_URL_HINTS)


def _entry_went_bad(before_url, after_url):
    """点投递入口后，是否**退回了列表/首页**（点错导航链接的确定信号）。"""
    b = (before_url or "").split("#")[-1]
    a = (after_url or "").split("#")[-1]
    if "/job/" in b and "/job/" not in a and a:
        return True
    if _url_looks_list(after_url) and not _url_looks_list(before_url):
        return True
    return False


def _click_apply_entry(page, texts, note=None, timeout_ms=3000):
    """点「投递/申请」入口，并**校验点击结果**：点完退回列表页就回退、换下一个候选。

    为什么必须校验：真实站点（MokaHR 族：鹰角网络等）的岗位详情页顶部有
    「投递指南 / 20XX届应届生投递 / 日常实习投递」导航，页脚还有一个「立即投递」链接。
    只按文本顺序盲点会先命中它们 → 页面跳回 `#/jobs` 列表页；表面上"点了投递"，
    实际是在列表页上采集字段（字段命中 0 项），随后还会把真正的详情页关掉
    —— 用户看到的就是「一个字没填 + 窗口自己关了」。
    真正该点的是详情页主体里的「申请职位」按钮。

    返回命中的关键词（全都没命中返回 ''）。
    """
    try:
        before = page.url
    except Exception:
        before = ""
    for kw in texts:
        if not _page_alive(page):
            return ""
        hit = _click_by_text(page, [kw], timeout_ms=timeout_ms)
        if not hit:
            continue
        try:
            page.wait_for_timeout(1800)
        except Exception:
            return ""
        try:
            after = page.url
        except Exception:
            return ""
        if _entry_went_bad(before, after):
            if note:
                note("「%s」命中的是导航/页脚链接（页面退回列表 %s），回退后换下一个入口"
                     % (kw, (after or "")[-36:]))
            try:
                page.goto(before, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(1600)
            except Exception:
                pass
            continue
        return hit
    return ""


def _click_apply_in_row(page, keywords):
    """列表页：找到含目标关键词的行，点击该行内的「投递」按钮。返回行文本或 ''。"""
    js = """(kws) => {
        const vis = e => { const r=e.getBoundingClientRect(); return r.width>0&&r.height>0; };
        const leaves = Array.from(document.querySelectorAll('*')).filter(e => e.children.length===0 && vis(e));
        for (const kw of kws) {
            const leaf = leaves.find(e => (e.innerText||'').includes(kw));
            if (!leaf) continue;
            let node = leaf;
            for (let i=0;i<10 && node;i++){
                node = node.parentElement; if(!node) break;
                const btn = Array.from(node.querySelectorAll('button,[class*="van-button"],[class*="btn"]'))
                    .find(b => (b.innerText||'').trim().includes('投递'));
                if (btn) { btn.scrollIntoView({block:'center'}); btn.click(); return (leaf.innerText||'').trim().slice(0,40); }
            }
        }
        return '';
    }"""
    try:
        return page.evaluate(js, keywords) or ""
    except Exception:
        return ""


def _png_size(raw):
    """极简 PNG 尺寸解析（读 IHDR），避免引入 PIL 依赖。返回 (w, h) 或 None。"""
    if not raw or len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    import struct
    try:
        return struct.unpack(">II", raw[16:24])
    except Exception:
        return None


_VERIFY_BOX_SEL = (".verify-box,[class*='verify'],[class*='captcha'],[class*='slider'],"
                   "[class*='drag'],[class*='nc_'],[class*='geetest'],[class*='yidun'],"
                   "[class*='slide-verify']")


def _find_verify_widget(page):
    """定位人机验证控件，返回 {el, box, bg, piece, frame, bg_png, piece_png} 或 None。

    背景图/拼图块优先取控件内的 img/canvas（面积最大者为背景、次者为拼图块）；
    都没有则把整个控件截图当作背景图（交给 CV 边缘缺口法）。
    """
    for fr in list(page.frames):
        try:
            cands = fr.query_selector_all(_VERIFY_BOX_SEL)
        except Exception:
            continue
        for el in cands:
            try:
                if not el.is_visible():
                    continue
                box = el.bounding_box()
                if not box or box["width"] < 40 or box["height"] < 20:
                    continue
                sized = []
                for i in el.query_selector_all("img,canvas"):
                    try:
                        if not i.is_visible():
                            continue
                        b = i.bounding_box()
                        if b and b["width"] > 0:
                            sized.append((b["width"] * b["height"], i))
                    except Exception:
                        continue
                sized.sort(key=lambda t: t[0], reverse=True)
                bg = sized[0][1] if sized else None
                piece = sized[1][1] if len(sized) > 1 else None
                bg_png = piece_png = None
                if bg is not None:
                    try:
                        bg_png = bg.screenshot()
                    except Exception:
                        bg_png = None
                if piece is not None:
                    try:
                        piece_png = piece.screenshot()
                    except Exception:
                        piece_png = None
                if not bg_png:
                    try:
                        bg_png = el.screenshot()
                    except Exception:
                        bg_png = None
                if not bg_png:
                    continue
                return {"el": el, "box": box, "bg": bg, "piece": piece, "frame": fr,
                        "bg_png": bg_png, "piece_png": piece_png}
            except Exception:
                continue
    return None


def _find_handle(el, widget_box):
    """在验证控件内找「滑块手柄」。优先 class 命中；否则用几何兜底
    （位于控件下半部、宽度较窄的可见元素，取面积最小者）。返回 bounding_box 或 None。"""
    for sel in ("[class*='btn']", "[class*='handle']", "[class*='slider-btn']",
                "[class*='drag-btn']", "[class*='handler']", "[class*='block']",
                "[class*='thumb']", "[class*='knob']"):
        try:
            for h in el.query_selector_all(sel):
                if not h.is_visible():
                    continue
                b = h.bounding_box()
                if b and 10 <= b["width"] <= widget_box["width"] * 0.6 and b["height"] >= 8:
                    return b
        except Exception:
            continue
    best = None
    try:
        for h in el.query_selector_all("div,span,button,a,img,i,em"):
            try:
                if not h.is_visible():
                    continue
                b = h.bounding_box()
            except Exception:
                continue
            if not b or b["width"] < 10 or b["height"] < 8:
                continue
            if b["width"] > widget_box["width"] * 0.5:
                continue
            if (b["y"] - widget_box["y"]) < widget_box["height"] * 0.45:
                continue                      # 只要下半部（滑块条区域）
            area = b["width"] * b["height"]
            if best is None or area < best[0]:
                best = (area, b)
    except Exception:
        pass
    return best[1] if best else None


# 滑块「微调」序列：第 k 次尝试在原位移上叠加的像素（0 = 按算出的位移原样拖）。
# 识别出的缺口偶有 1~5px 误差，严格站点会因此判失败；小幅加减比反复重算更有效。
_NUDGE = (0, -3, 3, -6, 6)


def _drag_slider(page, from_x, to_x, y, steps=24):
    """人类化拖动：easeInOut 加减速 + 轻微纵向抖动（降低被行为风控判为机器的概率）。"""
    import math
    import random
    page.mouse.move(from_x, y)
    page.mouse.down()
    span = to_x - from_x
    for i in range(1, steps + 1):
        t = i / float(steps)
        e = 0.5 - 0.5 * math.cos(math.pi * t)
        page.mouse.move(from_x + span * e, y + random.choice([0, 0, 0, 1, -1]))
        page.wait_for_timeout(random.randint(12, 30))
    page.mouse.move(to_x, y)
    page.wait_for_timeout(180)
    page.mouse.up()


def _attempt_auto_verify(page, attempts=3, note=None):
    """用 CV+AI 尝试自动过验证。成功返回 True（控件消失或验证码已发出）。"""
    try:
        import captcha as _cap
    except Exception as e:
        if note:
            note("captcha 模块不可用（%s），转人工" % e)
        return False

    # **形态识别必须在动手之前**：拿滑块算法去拖旋转图，会把控件拖刷新；而刷新瞬间
    # 「检测不到验证控件」在旧代码里 == 「验证成功」→ 流程继续去等一条永远不会来的短信。
    w0 = _find_verify_widget(page)
    kind, ev = classify_verify_kind(page, widget=w0)
    if kind == "unknown":
        # 没有任何形态证据 → **默认按滑块流程走**（历史主流程）。宁可多试一次，
        # 也绝不因为「读不到证据」就拒投（上一版就是这里让滑块岗位卡在需人工验证）。
        kind, ev = "slider", "无形态证据 → 默认按滑块流程尝试"
    if kind in ("rotate", "grid"):
        if note:
            note("人机验证形态=%s（依据「%s」）：自动过验证**只支持滑块类**，"
                 "不硬试 → 直接转人工处理" % (_KIND_CN.get(kind, kind), ev or "无文案证据"))
        return False
    if note:
        note("人机验证形态=%s（依据「%s」）" % (_KIND_CN.get(kind, kind), ev or "无文案证据"))

    last_err = ""
    for k in range(1, attempts + 1):
        w = _find_verify_widget(page)
        if w is None:
            break
        if note:
            note("尝试自动过人机验证（第 %d/%d 次，形态=%s）…"
                 % (k, attempts, _KIND_CN.get(kind, kind)))
        if kind == "slider_end":
            # 拖到最右端型：不识别缺口，直接把滑块拖到底（这类图 CV 必定报「未找到缺口轮廓」）
            bbox = w.get("box")
            if not bbox:
                last_err = "拿不到验证控件位置"
                break
            hb = _find_handle(w["el"], bbox) if w.get("el") is not None else None
            if hb and hb["height"]:
                from_x = hb["x"] + hb["width"] / 2.0
                y = hb["y"] + hb["height"] / 2.0
            else:
                from_x = bbox["x"] + 5
                y = bbox["y"] + bbox["height"] - 8
            to_x = bbox["x"] + bbox["width"] - 3
            if note:
                note("滑块类型=拖到最右端：从 x=%.0f 拖到 x=%.0f" % (from_x, to_x))
            _drag_slider(page, from_x, to_x, y)
            res = {"type": "drag_end", "via": "builtin", "method": "drag_to_end"}
        else:
            try:
                res = _cap.solve({"bg_png": w["bg_png"], "piece_png": w["piece_png"],
                                  "kind": "slider"})
            except _cap.NeedHuman as e:
                last_err = str(e)
                break
            except Exception as e:  # noqa: BLE001
                last_err = "%s: %s" % (type(e).__name__, e)
                break

        try:
            if res.get("type") == "drag_end":
                pass                     # 拖动已在上面完成，直接进入判定
            elif res.get("type") == "drag":
                gap_x = res.get("gap_x")
                if gap_x is None:
                    last_err = "solver 未返回 gap_x"
                    break
                bbox = (w["bg"] or w["el"]).bounding_box() or w["box"]
                sz = _png_size(w["bg_png"])
                iw = sz[0] if sz else bbox["width"]
                # gap_x 是「缺口中心」在**背景图内**的像素 x（captcha 模块统一口径）。
                scale = (bbox["width"] / float(iw)) if iw else 1.0
                gap_page_x = bbox["x"] + float(gap_x) * scale
                # 拼图块的初始位置（页面坐标）；拿不到就认为它贴在背景图左边缘
                pbox = None
                try:
                    pbox = w["piece"].bounding_box() if w["piece"] is not None else None
                except Exception:
                    pbox = None
                start_page_x = (pbox["x"] + pbox["width"] / 2.0) if pbox else bbox["x"]
                # 滑块手柄起点（class 命中优先，否则按几何兜底）
                hb = _find_handle(w["el"], w["box"])
                if hb and hb["height"]:
                    from_x = hb["x"] + hb["width"] / 2.0
                    y = hb["y"] + hb["height"] / 2.0
                else:
                    from_x = bbox["x"] + 5
                    y = bbox["y"] + bbox["height"] + 12
                # **关键修复（2026-09-23）**：要拖的是「缺口相对拼图块起点的位移」，
                # 而不是「缺口绝对 x − 手柄中心 x」。手柄中心比背景图左边缘靠右约半个手柄宽，
                # 用后者会**少拖 15~30px** → 滑块永远差一截，验证必失败。
                drag = gap_page_x - start_page_x
                nudge = _NUDGE[k - 1] if (k - 1) < len(_NUDGE) else 0
                drag += nudge
                target_x = from_x + drag
                if note:
                    note("滑块拖动：缺口中心 x=%s(图内) → 页面 x=%.0f；"
                         "拼图块起点 %.0f；位移 %.0fpx%s"
                         % (gap_x, gap_page_x, start_page_x, drag,
                            ("（微调 %+dpx）" % nudge) if nudge else ""))
                _drag_slider(page, from_x, target_x, y)
            elif res.get("type") == "click":
                bbox = (w["bg"] or w["el"]).bounding_box() or w["box"]
                sz = _png_size(w["bg_png"])
                iw = sz[0] if sz else bbox["width"]
                ih = sz[1] if sz else bbox["height"]
                sx = bbox["width"] / float(iw) if iw else 1.0
                sy = bbox["height"] / float(ih) if ih else 1.0
                for pt in res.get("points") or []:
                    page.mouse.click(bbox["x"] + pt[0] * sx, bbox["y"] + pt[1] * sy)
                    page.wait_for_timeout(250)
            else:
                last_err = "未知动作类型 %r" % res.get("type")
                break
        except Exception as e:  # noqa: BLE001
            last_err = "执行失败: %s" % e
            break

        page.wait_for_timeout(1800)
        # 成功判定必须**有正向证据**：旧写法「检测不到验证控件 == 成功」会被验证码
        # **刷新瞬间**（控件短暂消失）骗过 → 误判成功后去等永不到来的短信。
        # 现在要求：① 验证码已发出（最强证据）；或 ② 连续两次复检都无验证控件。
        if _code_sent(page):
            if note:
                note("自动过人机验证成功（via=%s，method=%s；正向证据：验证码已发出）"
                     % (res.get("via"), res.get("method")))
            return True
        if not detect_human_verification(page):
            page.wait_for_timeout(1500)
            if not detect_human_verification(page):
                if note:
                    note("自动过人机验证成功（via=%s，method=%s；复检两次均无验证控件）"
                         % (res.get("via"), res.get("method")))
                return True

    if note and last_err:
        note("自动过人机验证未成功（%s）" % last_err)
    return False


def scan_positions(url, keywords=None, cities=None, max_more=8, headless=True,
                   progress=None):
    """只读扫描岗位列表页，返回该页所有岗位及**筛选后的匹配岗位**。

    筛选规则（用户指定）：
      - `keywords` 中**每一个**都必须出现在「岗位标题 + 元信息」里（默认 产品经理、本科）
      - `cities` 中**至少一个**必须命中岗位地域（默认 北京/上海/杭州/苏州/南京/广州/深圳）
    不点击「投递」，不填写，不提交 —— 纯读取。

    progress: 回调(str)，用于上报扫描进度。
    返回 {"total": n, "matched": [...], "all": [...], "url": url}
      each: {"title","degree","city","meta","matched","why"}
    """
    import json
    import re
    _json_dumps = json.dumps
    _dk, _dc = _load_pos_filter()
    kws = [k for k in (keywords if keywords is not None else _dk) if k]
    cts = [c for c in (cities if cities is not None else _dc) if c]
    # 「投递」类控件文案（各 ATS 站不一），用于定位岗位行
    apply_texts = list(POS_APPLY_TEXTS)

    def note(m):
        if progress:
            try:
                progress(m)
            except Exception:
                pass

    # 通用化：不再绑定 Vant（myyule）的 .van-button__text，改为「任意叶子上文案命中
    # 投递类词」；从叶子向上找**最高**的「只含一个投递控件」祖先作为岗位行。
    # 祖先的命中数是单调不减的，所以一旦 >1 即可 break（更快，且结果与全扫一致）。
    js = """() => {
      const vis = e => { const r=e.getBoundingClientRect(); return r.width>0&&r.height>0; };
      const APPLY = %s;
      const norm = s => (s||'').replace(/\\s+/g,' ').trim();
      const leaf = el => {
        const t = norm(el.innerText);
        if (!APPLY.includes(t)) return false;
        // 直接子元素里若还有同样文案，说明本元素是包装层，取更深的那个
        for (const c of el.children) { if (APPLY.includes(norm(c.innerText))) return false; }
        return true;
      };
      const nodes = Array.from(document.querySelectorAll('button,a,span,div,p,em,i,strong'));
      const marks = nodes.filter(e => vis(e) && leaf(e));
      const uniq = [];
      for (const m of marks) {
        let n = m, row = null;
        for (let i=0;i<12 && n;i++){
          n = n.parentElement; if (!n) break;
          const txt = norm(n.innerText);
          if (txt.length < 12) continue;
          if (txt.length > 900) break;
          let cnt = 0;
          for (const e of n.querySelectorAll('button,a,span,div,p,em,i,strong')) {
            if (leaf(e)) { cnt++; if (cnt > 1) break; }
          }
          if (cnt > 1) break;          // 再往上必然 >1，不可能更精确
          if (cnt === 1) row = n;
        }
        if (row && !uniq.includes(row)) uniq.push(row);
      }
      return uniq.map(r => ({
        lines: (r.innerText||'').split('\\n').map(s=>s.trim()).filter(Boolean),
        // 文本变化检测用（判断「加载更多」是否真的加载了新内容）
        key: (r.innerText||'').replace(/\\s+/g,'')
      }));
    }""" % _json_dumps(apply_texts)

    def parse(lines):
        if not lines:
            return None
        raw_title = lines[0]
        title = clean_title(raw_title)
        full = " ".join(lines)
        # 学历：在整行文本里找第一个学历词（各站排版不同，整行找最稳）
        degree = next((d for d in DEGREES if d in full), "")
        # 地域：整行抽取（先 xx市/xx省，再回退已知城市名）
        city = _extract_cities(full)
        hay = full
        missing = [k for k in kws if k not in hay]
        city_hit = [c for c in cts if c and c in city]
        why = []
        if missing:
            why.append("缺少关键词 " + "/".join(missing))
        if not city_hit:
            why.append("地域不在指定城市（识别为「%s」）" % (city or "未知"))
        return {"title": title, "raw_title": raw_title, "degree": degree,
                "city": city, "meta": " ".join(lines[1:3]),
                "matched": not missing and bool(city_hit),
                "why": "；".join(why)}

    from playwright.sync_api import sync_playwright
    rows = []
    seen = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=headless,
                                    args=["--no-sandbox"])
        try:
            page = browser.new_context().new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(6000)
            for rnd in range(max(1, max_more)):
                try:
                    got = page.evaluate(js)
                except Exception:
                    got = []
                added = 0
                for r in got or []:
                    k = r.get("key") or ""
                    if not k or k in seen:
                        continue
                    seen.add(k)
                    pr = parse(r.get("lines") or [])
                    if pr:
                        rows.append(pr)
                        added += 1
                note("已扫描 %d 个岗位（本轮新增 %d）" % (len(rows), added))
                if added == 0:
                    break
                # 尝试「加载更多 / 下一页」，没有就结束
                clicked = False
                for t in ("加载更多", "查看更多", "下一页", "更多岗位"):
                    try:
                        loc = page.locator("text=%s" % t)
                        if loc.count() > 0:
                            loc.first.scroll_into_view_if_needed(timeout=3000)
                            loc.first.click(timeout=3000)
                            clicked = True
                            note("点击「%s」继续加载" % t)
                            break
                    except Exception:
                        continue
                if not clicked:
                    break
                page.wait_for_timeout(2500)
        finally:
            browser.close()

    matched = [r for r in rows if r["matched"]]
    # 宽松口径（用于如实汇报：严格 AND 可能为 0，而宽口径有货）
    city_ok = [r for r in rows if any(c and c in r["city"] for c in cts)]
    any_kw = [r for r in city_ok if any(k in (r["raw_title"] + " " + r["meta"]) for k in kws)]
    loose = city_ok if len(kws) < 2 else any_kw
    note("共 %d 个岗位，严格符合条件 %d 个（宽松口径 %d 个）"
         % (len(rows), len(matched), len(loose)))
    return {"total": len(rows), "matched": matched, "loose": loose, "all": rows,
            "url": url, "keywords": kws, "cities": cts,
            # 0 行通常说明这个链接不是列表页（而是单个岗位详页）
            "is_list": len(rows) > 0}


# ------------------------------------------------------------------ 投递表单识别
# 为什么需要这一组：很多站（典型如思特奇 myyule）点「投递」会先被**登录门禁**拦下，
# 登录完成后**退回岗位列表**，必须再点一次「投递」才会弹出投递表单。
# 旧版 apply_by_url 到「自动登录完成」就 return 了，用户看到的现象就是
# 「登录成功了，但没有开始自动填写投递信息」。
_FORM_HINTS = ("姓名", "学历", "学校", "院校", "专业", "毕业", "期望", "实习",
               "简历", "resume", "工作经验", "手机", "邮箱", "email", "求职")

_SUBMIT_TEXTS = ("确认投递", "提交投递", "确认提交", "提交申请", "确定投递",
                 "立即投递", "投递简历", "提交简历", "保存并提交", "提交", "确定")

# 强成功文案（**故意不放「已投递」「已提交」**：列表页的「已投递」角标会误判成提交成功）
_STRONG_SUCCESS = ("投递成功", "提交成功", "申请成功", "报名成功", "投递已提交",
                   "感谢您的投递", "感谢投递", "简历已投递", "已成功投递",
                   # 常见变体（人工提交后部分站点措辞不同，补上避免漏判成功）
                   "已提交", "提交完成", "投递完成", "申请已提交",
                   "投递申请已提交", "简历提交成功", "简历投递成功", "提交成功！")
_DUP_HINTS = ("已投递过", "已经投递过", "重复投递", "请勿重复")
_INVALID_HINTS = ("请填写", "不能为空", "为必填", "必填项", "请输入", "请选择")
# 「阻碍性提示」：站点**不允许**本岗位被投递（限制类；既不是「已投过」，也不是表单没填全）。
# 用户实测遇到的是「只允许投递一个岗位」这种限制。命中即判该岗位不可投递 → 立刻记录原因
# 并跳到下一个岗位（不驻留等人工）。「重复投递」类交给 _DUP_HINTS（语义不同，见上）。
BLOCKER_HINTS = (
    "只允许投递一个岗位", "只能投递一个岗位", "仅可投递一个岗位", "只可投递一个岗位",
    "只允许投递一个职位", "只能申请一个职位", "只允许申请一个岗位", "只能投递一个",
    "仅限投递一个", "只可投递一个", "请先撤销原投递", "取消原投递",
    "投递次数已达上限", "投递次数已用完", "今日投递次数已用完", "已达投递上限",
    "不能重复投递", "不可重复投递", "禁止重复投递", "请勿重复投递",
    "该岗位暂不可投递", "该职位暂不可投递", "暂不支持投递", "未开放投递",
)


def _accept_dialog(d):
    """自动接受原生 confirm()/alert()。站点提交时常弹二次确认，不接管会挂死流程。"""
    try:
        d.accept()
    except Exception:
        pass


def _click_exact(page, kw):
    """点击**文本完全等于** kw 的可见元素，返回是否点到。

    比 `_click_by_text` 的「contains」更保守：避免把「提交」误点到
    「提交前请阅读协议」这类文案上。
    """
    try:
        ok = page.evaluate("""(kw) => {
            const vis = e => { const r = e.getBoundingClientRect(); return r.width>0&&r.height>0; };
            const els = Array.from(document.querySelectorAll('button,a,div,span,label,em,p,strong'))
                .filter(e => e.children.length===0 && vis(e) && (e.innerText||'').trim() === kw);
            if (!els.length) return false;
            const el = els[els.length-1];
            el.scrollIntoView({block:'center'});
            el.click();
            return true;
        }""", kw)
    except Exception:
        ok = False
    if ok:
        return True
    try:
        loc = page.locator("button:has-text('%s')" % kw)
        if loc.count() > 0:
            loc.last.click(timeout=3000)
            return True
    except Exception:
        pass
    return False


def _form_signature(page):
    """采集当前页面特征，用于判断「现在这个页面是不是投递表单」。

    返回 {n, labels, texts, fileInputs}；任何异常都返回 {}（调用方按「不是表单」处理）。
    """
    try:
        return page.evaluate("""() => {
            const vis = e => { const r = e.getBoundingClientRect(); return r.width>0&&r.height>0; };
            const inputs = Array.from(document.querySelectorAll(
                'input:not([type=hidden]),textarea,select')).filter(vis);
            const labels = inputs.map(e => {
                let l = '';
                try { if (e.labels && e.labels[0]) l = e.labels[0].innerText || ''; } catch (_) {}
                if (!l) l = e.getAttribute('aria-label') || '';
                return [(l||'').trim(), e.getAttribute('placeholder') || '',
                        e.getAttribute('name') || '', e.getAttribute('id') || ''].join(' ');
            }).filter(s => s.trim());
            const texts = Array.from(document.querySelectorAll('button,a,div,span,label'))
                .filter(e => e.children.length === 0 && vis(e))
                .map(e => (e.innerText || '').trim()).filter(Boolean);
            return { n: inputs.length, labels: labels.slice(0, 40),
                     texts: texts.slice(0, 200),
                     fileInputs: document.querySelectorAll('input[type=file]').length };
        }""") or {}
    except Exception:
        return {}


def _looks_like_delivery_form(sig):
    """页面是否像「投递信息表单」（而不是登录页 / 岗位列表页）。"""
    if not sig:
        return False
    n = int(sig.get("n") or 0)
    labels = sig.get("labels") or []
    hits = sum(1 for l in labels if any(h in l for h in _FORM_HINTS))
    has_submit = any(("提交" in t or "投递" in t or "申请" in t)
                     for t in (sig.get("texts") or []))
    if int(sig.get("fileInputs") or 0) > 0 and (has_submit or n >= 3):
        return True
    if n >= 8 and (has_submit or int(sig.get("fileInputs") or 0) > 0):
        # 8 个以上可见输入控件、**且带提交按钮或上传控件** → 基本一定是投递表单。
        # 关键场景：大表单的字段全是英文/只给 placeholder（`hits` 恒 0）时旧规则会漏判，
        # 用户报的「登录成功却没识别出处于投递状态」就卡在这里。
        # 但必须要求「有提交信号」：否则「筛选条件堆了 8 个输入框」的岗位列表页也会被误判成表单。
        return True
    return (n >= 4 and hits >= 2) or hits >= 3


def _sig_desc(sig):
    """把 _form_signature 的结果压成一行，便于写进日志。

    额外带上**样例字段名**：识别失败时，日志里能直接看出「那是什么页面」，
    而不是只看到一个输入框数量（排查「登录后没识别出表单」时非常有用）。
    """
    if not sig:
        return "读取页面失败"
    labs = [l for l in (sig.get("labels") or []) if l][:3]
    return ("可见输入框 %d 个、文件控件 %d 个、提交类按钮 %s；样例字段 %s"
            % (int(sig.get("n") or 0), int(sig.get("fileInputs") or 0),
               "有" if any(("提交" in t or "投递" in t or "申请" in t)
                           for t in (sig.get("texts") or [])) else "无",
               " | ".join(labs)[:90] or "无"))


def _wait_delivery_form(page, note=None, tries=5, gap=2000, where=""):
    """轮询等「投递表单」出现（登录/点击后常有跳转延迟）。

    旧写法是「看一眼 `_form_signature`，不是表单就放弃投递」——用户报的
    「登录成功却没识别出处于投递状态、最后超时不能成功投递」正是这么来的。
    这里最多再等 `tries*gap` 毫秒，出现即返回。
    """
    sig = _form_signature(page)
    if _looks_like_delivery_form(sig):
        return sig
    for _ in range(max(0, int(tries))):
        try:
            page.wait_for_timeout(gap)
        except Exception:
            break
        sig = _form_signature(page)
        if _looks_like_delivery_form(sig):
            if note:
                note("%s页面切换完成，已识别到投递表单（%s）" % (where, _sig_desc(sig)))
            return sig
    return sig


def _submit_evidence(page):
    """读页面文案，判断提交结果：("success"|"dup"|"invalid"|"", 命中词)。"""
    try:
        txt = page.evaluate("() => (document.body ? document.body.innerText : '')") or ""
    except Exception:
        return "", ""
    for h in _STRONG_SUCCESS:
        if h in txt:
            return "success", h
    for h in _DUP_HINTS:
        if h in txt:
            return "dup", h
    for h in _INVALID_HINTS:
        if h in txt:
            return "invalid", h
    for h in BLOCKER_HINTS:
        if h in txt:
            return "blocked", h
    return "", ""


def _submit_application(page, note=None):
    """填完表单后点提交，并**用页面文案验证**是否真的提交成功。

    返回 (state, evidence)：
      state ∈ {"success", "dup", "invalid", "blocked", "clicked", "notfound"}
      - success：页面出现「投递成功」类文案；
      - dup    ：提示「已投递过」；
      - invalid：提示「请填写/不能为空」（表单没填全，**不算成功**）；
      - blocked：站点**不允许**投递本岗位（如「只允许投递一个岗位」「投递次数已达上限」）；
      - clicked：点到了提交按钮但拿不到成功文案（可能跳转中/被风控）；
      - notfound：页面上没有可点的提交按钮。
    """
    def n_(m):
        if note:
            try:
                note(m)
            except Exception:
                pass

    st, ev = _submit_evidence(page)
    if st:
        return st, ev

    for t in _SUBMIT_TEXTS:
        if not _click_exact(page, t):
            continue
        n_("已点击「%s」" % t)
        page.wait_for_timeout(3000)
        st, ev = _submit_evidence(page)
        if st in ("success", "dup", "invalid", "blocked"):
            return st, ev
        # 提交后可能弹二次确认框 → 再点一次确认
        for t2 in ("确定", "确认提交", "确认", "是", "OK"):
            if _click_exact(page, t2):
                n_("提交后出现二次确认，已点「%s」" % t2)
                page.wait_for_timeout(2500)
                st, ev = _submit_evidence(page)
                if st:
                    return st, ev
                break
        return "clicked", t
    return "notfound", ""


_PARK_HEARTBEAT = 20


# ---------------------------------------------------------------- 人工交接（T64）
# 「需要人在窗口里做事」的场景（人机验证 / 表单必填被拦 / 提交结果未知）过去一律
# **立即返回**，紧接着 `finally: browser.close()` 把窗口关掉 —— 而 note 里却写着
# 「请在已打开的浏览器窗口中手动完成验证」。用户实测反馈：转人工后窗口立刻没了、
# 验证根本做不了、状态还卡在「需人工验证」。下面统一按**预算驻留等待**（窗口保持打开），
# 人做完就**自动继续**。
def _env_secs(name, default):
    try:
        v = int((os.environ.get(name) or "").strip() or 0)
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else default


# 交人工的总预算（秒）。**默认 0 = 不设超时**（2026-10-28 用户要求：窗口不许因为
# 等待超时而关闭 —— 实测「输入验证码完成登录后窗口还是被关掉」，根因就是这里
# 600s 到点 → needs_human → finally 关窗）。想恢复限时收尾，设
# RESUME_HUMAN_HANDOFF_WAIT=<正数>。0 在两个等待器里都表示「不设上限」。
HUMAN_HANDOFF_WAIT = _env_secs("RESUME_HUMAN_HANDOFF_WAIT", 0)
_SUBMIT_CONFIRM_WAIT = _env_secs("RESUME_SUBMIT_CONFIRM_WAIT", 60)  # 点过提交后等结果的窗口（秒）
# 「停在提交前」等人工确认的默认预算（秒）：单岗位投递填完表后，窗口保持打开等你核对
# 并手动点「提交」。**默认 0 = 不设超时**（2026-10-28 用户要求：不许因为等待超时把
# 窗口关掉——人可能几小时后才回来核对）。窗口会一直开着，直到你点提交 / 关窗 / 点
# 「取消投递」。想恢复「等满 N 秒就收尾关窗」，设 RESUME_PAUSE_CONFIRM_WAIT=<正数>。
PAUSE_CONFIRM_WAIT = _env_secs("RESUME_PAUSE_CONFIRM_WAIT", 0)


def _handoff_budget(human_wait=None):
    """交人工时的等待预算（秒）。

    **`human_wait<=0` 不等于「放弃」**：传 0 的调用方（历史上一键投递就这么传）本意是
    「别被卡住」，但那会让窗口立刻关闭、人根本没机会操作。现在 0 / None / 负数一律
    回落到 `HUMAN_HANDOFF_WAIT`（**默认 0 = 不设超时**）。
    返回 0 表示「不设上限」，由 `_wait_human_verify` / `_wait_human_reach_form` /
    `_park_before_submit` 识别。
    """
    try:
        w = int(human_wait)
    except (TypeError, ValueError):
        w = 0
    return w if w > 0 else HUMAN_HANDOFF_WAIT


def _wait_desc(secs):
    """把等待预算说成人话：0 = 不设超时（**绝不许写成「最长等 0s」**）。"""
    try:
        s = int(secs)
    except (TypeError, ValueError):
        s = 0
    return ("最长等 %ds" % s) if s > 0 else "不设超时、窗口一直保持打开"


def _pause_budget(confirm_wait):
    """把「停在提交前」的 confirm_wait 解析成**实际等待秒数**（纯函数，可单测）。

    · None（调用方未指定）→ `PAUSE_CONFIRM_WAIT`（**默认为 0 = 不超时**）；
    · 0 / 负数           → 0，表示**不超时**（一直等，直到提交 / 关窗 / 取消）；
    · 正数               → 原样（显式限时收尾）。
    非数字（脏数据）按「未指定」处理，回落到 PAUSE_CONFIRM_WAIT。
    """
    if confirm_wait is None:
        return PAUSE_CONFIRM_WAIT
    try:
        w = int(confirm_wait)
    except (TypeError, ValueError):
        return PAUSE_CONFIRM_WAIT
    return w if w > 0 else 0


def _wait_human_verify(page, note=None, budget=None, is_cancelled=None,
                       detector=None, what="人机验证"):
    """人工交接等待：**窗口保持打开**等人在窗口里把事情做完，做完就自动继续。

    「通过」的判定与 `_attempt_auto_verify` 同口径：**连续两次**复检都为真才算通过
    （验证控件刷新瞬间、页面跳转瞬间都会短暂“消失”，单次判定会被骗过）。

    detector：自定义「人已推进」判据（默认 = 人机验证控件消失）。必须能换，否则
      这个等待器只能用在验证控件场景 —— 「取不到验证码，请人手动输码登录」那里
      本来就没有验证控件，写死判据会恒真 → 交接变空转（本轮自测就是这么抓出来的）。
    what：只用于文案，让用户知道此刻在等他做什么。
    budget=0 表示**不设上限**（一直等人做完，窗口不会因等待超时被关掉）。
    返回 ("done"|"cancelled"|"closed"|"timeout", 说明)。
    """
    budget = int(budget if budget is not None else _handoff_budget())
    limit = budget if budget > 0 else 0    # 0 = 不设上限

    def _passed():
        try:
            if detector is not None:
                return bool(detector())
            return not detect_human_verification(page)
        except Exception:
            return False

    def n_(m):
        if note:
            try:
                note(m)
            except Exception:
                pass

    waited = 0
    while True:
        if limit and waited >= limit:
            break
        if _is_cancelled(is_cancelled):
            return "cancelled", "已取消：停止等待人工完成%s" % what
        _maybe_focus(page)
        try:
            if page.is_closed():
                return "closed", "窗口已被关闭"
            page.wait_for_timeout(2000)
        except Exception:
            return "closed", "窗口已被关闭"
        waited += 2
        if _passed():
            try:
                page.wait_for_timeout(1200)
                if _passed():
                    return "done", "连续两次复检通过"
            except Exception:
                return "closed", "窗口已被关闭"
        if waited % _PARK_HEARTBEAT == 0:
            n_("仍在等待人工完成%s（已等待 %ds%s）："
               "**浏览器窗口保持打开**，请在窗口内完成，完成后自动继续"
               "（点「进入窗口」可把窗口置前）…"
               % (what, waited, "" if not limit else " / 最长 %ds" % limit))
    return "timeout", "等待 %ds 后仍未检测到%s完成" % (budget, what)


def _park_before_submit(page, note=None, confirm_wait=0, tag="", is_cancelled=None):
    """**停在提交前**：表单已填好，保持浏览器窗口打开，等你手动确认并点「提交」。

    与 `human_wait` / `code_wait` 那种「等一会儿就放弃并关窗」不同，这里是
    **主动把窗口交给你**：轮询页面文案，一旦出现成功/重复提示就记录结果。

    **人工提交的可靠检测（修复「人工提交后没有终态」）**：
    仅靠每 2s 轮询成功文案不可靠——成功提示常是一闪而过的 toast、措辞不在白名单、
    或你点完提交立刻关窗，都会让循环收不到 success 而停在「待人工提交」。
    因此进入 park 时**注入点击监听器**，直接捕获「你点了提交类按钮」这个动作
    （window.__resumeHumanSubmit=True）；只要见过这次点击，就作为强证据，
    给一段宽限期等成功/重复文案出现，期间无报错即判定为已提交。关窗时若已见过
    提交点击 → 同样判 success（你点了提交，窗口随之关闭也视为投出去了）。

    confirm_wait <= 0 表示**不超时**（一直等，直到你点提交、关掉标签页或关掉浏览器）；
    传 None（未指定）表示用 PAUSE_CONFIRM_WAIT（**默认 0 = 不超时**）。
    返回 ("success"|"dup"|"closed"|"timeout", 证据文案)。

    tag：给**每一条**备注（含心跳）加前缀。用于让上层 `progress()` 认出「此刻正在
    等人操作」→ 看板状态切到「待人工提交」→ 前端才会显示「进入窗口」按钮。
    不加标记的话，心跳会把状态刷回「投递中」，用户在等待期间**看不到**那个按钮。
    """
    def n_(m):
        if note:
            try:
                note((tag + m) if tag else m)
            except Exception:
                pass

    # 注入「人工点击提交」监听器：捕获你点提交按钮的动作（先于任何成功文案/toast）
    try:
        page.evaluate("""() => { try {
          if (window.__resumeHumanSubmit !== undefined) return;
          window.__resumeHumanSubmit = false;
          var subs = ['确认投递','提交投递','提交申请','确认提交','提交简历',
                      '投递简历','保存并提交','立即投递','报名','申请','提交','确定','发送'];
          var isSubmit = function(t){ if(!t||t.length>12) return false;
            for (var i=0;i<subs.length;i++){ if(t.indexOf(subs[i])>=0) return true; } return false; };
          document.addEventListener('click', function(e){
            try {
              var el = e.target; if(!el) return;
              var t = (el.innerText||el.value||el.getAttribute('value')||'').trim();
              if (isSubmit(t)) { window.__resumeHumanSubmit = true; return; }
              var p = el.closest && el.closest('button,a,input,[role=button],.btn');
              if (p) { var pt=(p.innerText||p.value||p.getAttribute('value')||'').trim();
                       if (isSubmit(pt)) window.__resumeHumanSubmit = true; }
            } catch(_) {}
          }, true);
        } catch(_) {} }""")
    except Exception:
        pass

    # 先判一次（页面可能已显示成功）
    try:
        st, ev = _submit_evidence(page)
    except Exception:
        return "closed", ""
    if st in ("success", "dup"):
        return st, ev

    submitted_seen = False   # 本轮 park 是否见过你点提交
    since_submit = 0         # 见过点提交后、距上次「无报错」的轮询计数
    waited = 0
    while True:
        # **取消必须能打断停表等待**：否则看板 cancelled=true 但队列一直 running，
        # 用户只能重启后端（2026-10-28 实测）。返回 "cancelled" 让上层如实收尾。
        try:
            if is_cancelled and is_cancelled():
                return "cancelled", ""
        except Exception:
            pass
        _maybe_focus(page)
        # 关窗判定：你关了窗口 → 若见过提交点击则判为已投递，否则如实记待人工提交
        closed = False
        try:
            if page.is_closed():
                closed = True
        except Exception:
            closed = True
        if closed:
            return ("success" if submitted_seen else "closed"), ""

        try:
            page.wait_for_timeout(1000)
        except Exception:
            # 等待途中窗口被关 → 同上判定
            return ("success" if submitted_seen else "closed"), ""

        waited += 1
        # 读人工点击标记（捕获过即一直记为 True，避免关窗竞态丢失）
        try:
            if page.evaluate("() => window.__resumeHumanSubmit === true"):
                submitted_seen = True
        except Exception:
            pass

        # 成功 / 重复 / 报错 文案
        try:
            st, ev = _submit_evidence(page)
        except Exception:
            st, ev = "", ""
        if st == "success":
            return "success", ev
        if st == "dup":
            return "dup", ev
        if st == "invalid":
            # 表单报错（必填没填等）：你还要修正后重投，不计时、不判成功
            since_submit = 0
        elif submitted_seen:
            # 你点了提交且页面没报必填错：给 ~10s 宽限期等成功文案；
            # 仍无成功文案（toast 一闪而过/措辞未命中）即视为已提交
            since_submit += 1
            if since_submit >= 10:
                return "success", (ev or "人工点击提交按钮")
        if confirm_wait and waited >= confirm_wait:
            return "timeout", ""
        if waited % _PARK_HEARTBEAT == 0:
            n_("表单已填好，等你在浏览器窗口核对并点「提交」（已等待 %ds%s）…"
               % (waited, "" if not confirm_wait else " / 最长 %ds" % confirm_wait))


# 「登录表单特征」：用来判断人是否已在窗口里把登录做完（表单消失 / 页面跳走）。
_LOGIN_WIDGET_SELS = (
    "input[placeholder*='验证码']", "input[placeholder*='短信']",
    "input[name*='code']", "input[name*='captcha']", "input[id*='code']",
)


def _login_widget_count(page):
    """数页面上还剩几个「登录表单特征」控件；读不到返回 -1（保守，不当成 0）。"""
    n = 0
    for s in _LOGIN_WIDGET_SELS:
        try:
            n += page.locator(s).count()
        except Exception:
            return -1
    return n


def _login_fail_hints():
    """「登录失败」的**明确文案**（页面自己说的）。命中即确凿证明这一步没登录成功。"""
    return (
        "验证码错误", "验证码不正确", "验证码有误", "验证码已过期", "验证码过期",
        "验证码失效", "验证码已失效", "验证码无效", "短信验证码错误",
        "手机号或验证码错误", "手机验证码错误", "登录失败", "登录不成功",
        "登录未成功",
    )


def _login_evidence(page):
    """核实「登录到底成没成功」，返回 ("failed"|"ok"|"unknown", 证据文案)。

    **为什么非做不可**（用户报的 bug ①「未登录成功却显示等待人工提交」）：
    旧版 `_login_phase` 点完「登录」就**无条件**写下「自动登录完成（流程已走通）」
    并 return None —— 哪怕页面此刻正报「验证码错误」。调用方拿到 None 继续走，
    发现「当前页不是投递表单」，就把它兜底成「待人工提交」：于是用户看到
    「明明没登录成功，状态却是等待人工提交」。这里给出**明确判定**：
      · 页面出现失败文案               → "failed"（确凿）
      · 登录表单特征控件已消失/页面跳走 → "ok"（正向证据）
      · 两边都没读到                    → "unknown"（不冤枉、也不吹牛）
    """
    txt = ""
    try:
        txt = page.evaluate("() => (document.body ? document.body.innerText : '')") or ""
    except Exception:
        txt = ""
    for h in _login_fail_hints():
        if h in txt:
            return "failed", h
    n = _login_widget_count(page)
    if n == 0:
        return "ok", "登录表单已消失"
    return "unknown", ""


def _looks_like_login_page(page):
    """页面现在是否**仍然是登录/验证页**（登录表单特征控件还在）。

    用来在上层把「没登录成功」和「需要人手动点进投递页」分开 —— 旧代码不分辨，
    两者一律进「等人工提交」，bug ① 就是这么来的。
    读不到控件数（返回 -1）时按 False 处理，保持与旧版一致的保守行为。
    """
    try:
        return _login_widget_count(page) > 0
    except Exception:
        return False


def _wait_human_reach_form(page, note=None, budget=None, is_cancelled=None,
                           what="进入投递页", tag=""):
    """人工交接待办：**等人在窗口里把页面推进到「投递表单」**，到了就自动继续填表。

    用户报的 bug ③「人工确认进入投递页后自动输入未生效」的根因就是缺这个等待器：
    旧代码在这条分支上直接 `_park_before_submit`，而那个等待器**只盯「提交成功」文案**
    —— 人进了投递表单它也不会去填，于是用户看到「已经进了投递页，自动输入却没发生」。

    返回 ("done"|"submitted"|"cancelled"|"closed"|"timeout", 说明)。
      · done      —— 检测到投递表单（连续两次命中，避开跳转瞬间的假象）→ 该自动填了；
      · submitted —— 我们还没来得及填，人已经自己投出去了（如实按成功/重复处理）；
      · cancelled/closed/timeout —— 取消 / 窗口被关 / 预算用尽。

    tag：给**每一条**备注（含心跳）加前缀。理由同 `_park_before_submit`：看板靠
    `"等待人工提交"` 这个标记把状态切到「待人工提交」，否则心跳会把状态刷回
    「投递中」，用户在等待期间**看不到**「进入窗口」按钮。
    """
    budget = int(budget if budget is not None else _handoff_budget())
    limit = budget if budget > 0 else 0    # 0 = 不设上限

    def n_(m):
        if note:
            try:
                note((tag + m) if tag else m)
            except Exception:
                pass

    def _form_here():
        try:
            return _looks_like_delivery_form(_form_signature(page))
        except Exception:
            return False

    waited = 0
    while True:
        if limit and waited >= limit:
            break
        if _is_cancelled(is_cancelled):
            return "cancelled", "已取消：停止等待人工%s" % what
        _maybe_focus(page)
        try:
            if page.is_closed():
                return "closed", "窗口已被关闭"
            page.wait_for_timeout(2000)
        except Exception:
            return "closed", "窗口已被关闭"
        waited += 2
        # 人在我们填表之前就自己投了 → 如实上报，别再多填一遍
        try:
            st, ev = _submit_evidence(page)
        except Exception:
            st, ev = "", ""
        if st in ("success", "dup"):
            return "submitted", ev
        if _form_here():
            try:
                page.wait_for_timeout(1200)
            except Exception:
                return "closed", "窗口已被关闭"
            if _form_here():
                return "done", _sig_desc(_form_signature(page))
        if waited % _PARK_HEARTBEAT == 0:
            n_("仍在等待你%s（已等待 %ds%s）：**浏览器窗口保持打开**，"
               "你一进入投递表单我就自动填写并继续（点「进入窗口」可置前）…"
               % (what, waited, "" if not limit else " / 最长 %ds" % limit))
    return "timeout", "等待 %ds 后仍未检测到你%s" % (budget, what)


def _login_phase(page, phone, emit, get_code=None, code_wait=300, human_wait=None,
                 sms_info=None, on_code_request=None, company="", is_cancelled=None):
    """登录段（**受全局登录闸门 _LOGIN_GATE 保护**）：

    填手机号 → 勾协议 → 点「获取验证码」→ 人机验证（先自动 CV/AI，失败转人工）→
    从短信转发取码 → 填入 → 点「登录」。

    为什么整段都要串行：这一段的每一步都可能**发短信或收短信**，而多站点并发时
    验证码会互相串（不同厂商延迟不同，按时间取最新必错）。串行化是从根上消除竞态。

    调用方（_apply_core）保证只在本岗位**尚未登录**时才调用。
    返回 None 表示登录段走完、可继续投递；返回 dict 表示需要人工介入，调用方直接返回它。
    （`return` 会被下面的 finally 捕获，闸门一定释放，不会把后续登录全堵死。）

    on_code_request: 回调()，在**真的点了「获取验证码」之后**调用，用于让后端把
    取码时间窗**硬性**收窄到「本次请求发出之后」：早于此刻到达的短信一律不再考虑
    （挡住别家那轮的迟到短信、上一轮遗留码、以及自检测试短信）。
    """
    global LOGIN_HOLDER
    log = []

    def note(msg):
        """本段的 note：既累积本地日志（needs_human 的 detail 要用），也上报给上层。

        为什么本段要自带一份：needs_human 的若干 return 会 `"；".join(log)`，
        而 log 原本定义在 apply_by_url 头部 —— 登录段被抽成独立函数后必须自备。
        """
        log.append(msg)
        if emit:
            try:
                emit(msg)
            except Exception:
                pass

    def _mark_request():
        if on_code_request:
            try:
                on_code_request()
            except Exception:
                pass

    got = _acquire(_LOGIN_GATE, note, "登录闸门（多站点登录串行，避免验证码串号）",
                   max_wait=3600)
    if not got:
        note("⚠️ 等待登录闸门超时：本次不再串行（存在取码串号风险），继续尝试登录")
        LOGIN_HOLDER = ""
    else:
        LOGIN_HOLDER = (company or "").strip() or "(未命名公司)"
    try:
        if got:
            note("已进入登录段：全局登录串行中，当前登录「%s」" % LOGIN_HOLDER)
        # 2) 手机号验证页 → 填手机号
        filled_phone = bool(phone) and _fill_first(
            page, ["手机号", "手机号码", "手机", "mobile", "phone", "tel"], phone)
        if filled_phone:
            note("已填手机号")
            page.wait_for_timeout(700)

        # 3) 勾选「我已阅读并同意《隐私协议 / 用户协议》」
        #    该站的关键是「点含『隐私协议』那一行里的 checkbox」；按 class 猜容器会漏，
        #    所以这里最多重试 3 次，并用 _terms_state 明确判断到底勾上了没有。
        ag = ""
        for attempt in range(3):
            ag = _agree_terms(page) or ag
            tstate = _terms_state(page)
            if tstate == "checked":
                break
            # JS 通道没勾上（或压根没定位到控件）→ 直接枚举未勾选的 checkbox 补勾
            f = _force_check_terms(page)
            if f:
                ag = "已直接勾选：%s" % f
                if _terms_state(page) == "checked":
                    break
            if tstate == "none" and not f:
                break          # 页面上确实没有协议勾选框 → 不必重试
            page.wait_for_timeout(700)
        tstate = _terms_state(page)
        if ag:
            note("已处理用户协议：%s" % ag)
            page.wait_for_timeout(700)
        if tstate == "checked":
            note("协议已勾选 ✓")
        elif tstate == "unchecked":
            note("⚠️ 协议勾选未生效（已尝试 3 次）：页面上有《隐私协议》勾选框但未勾上，"
                 "会自动再试一次；若仍失败请手动勾选")

        # 4) 点击「获取验证码」
        #    该按钮常被「协议勾选」门禁：没勾上就 disabled，点了也没反应。
        #    所以先看禁用态 → 补勾协议；仍然禁用就不白点（也避免无效重发）。
        sent = ""
        for _t in range(3):
            if _code_btn_disabled(page):
                ag2 = _agree_terms(page)
                if ag2:
                    note("「获取验证码」按钮禁用中，先补勾协议：%s" % ag2)
                page.wait_for_timeout(800)
                if _code_btn_disabled(page):
                    note("「获取验证码」按钮仍处于禁用态（协议未勾选成功）：跳过点击，"
                         "请在浏览器窗口手动勾选《隐私协议》")
                    break
            hit = _click_by_text(page, ["获取验证码", "发送验证码", "获取短信验证码",
                                        "获取动态码", "发送校验码"])
            sent = sent or hit
            if hit:
                note("已点击：%s" % hit)
                _mark_request()   # 通知后端：此刻起才算「本岗位在等码」
            page.wait_for_timeout(2500)
            if _code_sent(page):
                note("验证码已发出 ✓")
                break
            # 没发出去：只有**确实存在未勾选的协议控件**时才补勾重试，
            # 避免误点已勾选的行（会把勾取消）以及无脑重发短信。
            if _terms_state(page) == "unchecked":
                ag2 = _agree_terms(page)
                if ag2:
                    note("协议仍未勾选，再次勾选：%s" % ag2)
                page.wait_for_timeout(900)
            else:
                break

        # 4.1) 如果压根没点到「获取验证码」，必须**说出来**。
        #      旧版会静默走到等待阶段，最后把「根本没发出短信」误报成「验证码未转发」。
        if not sent:
            note("⚠️ 没点到「获取验证码」按钮（页面上没有该按钮，或它一直处于禁用态）："
                 "大概率**根本没有发出短信**；仍会继续等一小会儿以防万一")
        if not sent and not filled_phone:
            note("页面上既没有手机号输入框、也没有点到「获取验证码」按钮 → 判断为**无需登录**"
                 "（会话已是登录态）：跳过短信验证，直接继续投递")
            return None

        # 5) 人机验证：**先自动（CV→AI→打码平台）尝试，失败才交人工**
        page.wait_for_timeout(3500)
        hv = detect_human_verification(page)
        if hv:
            note("检测到人机验证（%s）：先尝试自动通过" % hv)
            if _attempt_auto_verify(page, attempts=3, note=note):
                hv = ""
            else:
                # **窗口必须真的留着**：旧代码这里受 `human_wait=0` 影响 → 等待循环
                # 一次都不进 → 立刻返回 → 紧接着关窗。note 里那句「请在已打开的窗口
                # 中手动完成验证」是假的，用户实测就是「转人工后没机会操作」。
                budget = _handoff_budget(human_wait)
                note("自动过验证失败 → 转人工：**浏览器窗口保持打开**，请在窗口内手动"
                     "完成验证，完成后我会自动继续（%s；点「进入窗口」可置前）"
                     % _wait_desc(budget))
                how, why = _wait_human_verify(page, note=note, budget=budget,
                                              is_cancelled=is_cancelled)
                if how == "done":
                    note("人机验证已完成（%s），自动继续投递" % why)
                    hv = ""
                elif how == "cancelled":
                    note(why)
                    return {"needs_human": False, "stage": "已取消",
                            "detail": "；".join(log)}
                else:
                    note("⚠️ 人机验证仍未通过（%s）：窗口随后关闭，状态记为「需人工验证」，"
                         "可重新发起投递或点「标记为未投递」后重投" % why)
                    return {"needs_human": True, "stage": "人机验证",
                            "detail": "；".join(log)}
            # 通过后若验证码未发出 → 再点一次「获取验证码」（该站常需重发）
            if not _code_sent(page):
                for _t in range(2):
                    if _click_by_text(page, ["获取验证码", "发送验证码", "重新获取",
                                             "获取短信验证码", "重发验证码"]):
                        note("人机验证通过后重新点击「获取验证码」")
                        _mark_request()
                        break
                    page.wait_for_timeout(1500)
            page.wait_for_timeout(2500)

        # 6.0) **不空等**：验证码没发出去、且页面仍挂着人机验证控件 → 说明被人机验证
        #      拦住了，立刻转人工，而不是白等整段 code_wait（默认 300s）。
        #      这是「旋转验证码卡住」的第二道保险（第一道是 _attempt_auto_verify 的形态识别）。
        if get_code and not _code_sent(page) and detect_human_verification(page):
            # 「不空等 300s」是对的，但**不能顺手把窗口也关掉**：这里同样要把窗口交给
            # 人过验证，过完继续取码；实在过不去才如实记「需人工验证」。
            budget = _handoff_budget(human_wait)
            note("「获取验证码」未发出且页面仍有人机验证控件 → 不空等 %ds，改为请你在"
                 "窗口内完成验证（**浏览器窗口保持打开**，%s，完成后自动继续）"
                 % (code_wait, _wait_desc(budget)))
            how, why = _wait_human_verify(page, note=note, budget=budget,
                                          is_cancelled=is_cancelled)
            if how == "done":
                note("人机验证已完成（%s），自动继续取码" % why)
            elif how == "cancelled":
                note(why)
                return {"needs_human": False, "stage": "已取消", "detail": "；".join(log)}
            else:
                note("⚠️ 人机验证仍未通过（%s）→ 记「需人工验证」" % why)
                return {"needs_human": True, "stage": "人机验证",
                        "detail": "；".join(log)}

        # 6) 取后端转发来的验证码 → 自动填入 → 自动登录
        #    短信转发可能有延迟，这里耐心轮询（不再用「按钮倒计时」这种启发式当硬门槛）
        code = ""
        base = {}
        if sms_info:
            try:
                base = sms_info() or {}
            except Exception:
                base = {}
        if get_code:
            waited_c = 0
            while waited_c < code_wait:
                if _is_cancelled(is_cancelled):
                    note("已取消：停止等待短信验证码（已等待 %ds）" % waited_c)
                    return {"needs_human": False, "stage": "已取消",
                            "detail": "；".join(log)}
                code = (get_code() or "").strip()
                if code:
                    break
                if waited_c and waited_c % 20 == 0:
                    note("等待短信转发到达（已等待 %ds / 最长 %ds）…" % (waited_c, code_wait))
                page.wait_for_timeout(2000)
                waited_c += 2
        if not code:
            # 关键：把「一条新短信都没来」和「来了但没解析出验证码」分开报，
            # 否则会把「没点出去 / 被限流」误报成「转发延迟」。
            now = {}
            if sms_info:
                try:
                    now = sms_info() or {}
                except Exception:
                    now = {}
            got_new = int(now.get("total") or 0) - int(base.get("total") or 0)
            if not sent:
                why = "本轮**没点到「获取验证码」**，短信很可能根本没发出去"
            elif got_new > 0:
                why = "等待期间收到 %d 条新短信，但都没解析出验证码" % got_new
            else:
                why = ("等待 %ds 内**一条新短信都没到**（更可能是验证码没发出/被限流，"
                       "或手机端转发 APP 离线）" % code_wait)
            # **不能把窗口关掉就走**：下面这句 note 请用户手动输码并点「登录」，
            # 但旧代码写完它下一秒就 `finally: browser.close()` —— 用户实测看到的就是
            # 「提示要人工过验证、窗口却已经没了」，状态还永久卡在「需人工验证」。
            # 前端「需人工验证」面板也明确写着「可直接在浏览器窗口手动输入验证码后点击
            # 登录」，所以窗口必须**真的**留着。这里把窗口交给人：你在窗口里输码/点登录，
            # 页面一推进（跳走，或取码框消失）我就接着投递。
            budget = _handoff_budget(human_wait)
            try:
                url0 = page.url
            except Exception:
                url0 = ""
            n0 = _login_widget_count(page)

            def _login_progressed():
                try:
                    if url0 and page.url != url0:
                        return True
                except Exception:
                    return False
                n = _login_widget_count(page)
                return n0 > 0 and n == 0

            note("未取得验证码：%s；**浏览器窗口保持打开**，请在窗口内手动输入验证码并"
                 "点「登录」，完成后我会自动继续投递（%s；点「进入窗口」可置前）"
                 % (why, _wait_desc(budget)))
            how, whyh = _wait_human_verify(page, note=note, budget=budget,
                                           is_cancelled=is_cancelled,
                                           detector=_login_progressed,
                                           what="手动输入验证码并登录")
            if how == "done":
                note("检测到你已完成登录（%s），自动继续投递" % whyh)
                return None
            if how == "cancelled":
                note(whyh)
                return {"needs_human": False, "stage": "已取消", "detail": "；".join(log)}
            note("⚠️ 验证码仍未取得、也没检测到你完成登录（%s）→ 状态记为「需人工验证」，"
                 "可重新发起投递" % whyh)
            return {"needs_human": True, "stage": "验证码未到", "detail": "；".join(log)}

        note("已从短信转发取到验证码：%s（等待 %ds）" % (code, waited_c))
        if _fill_first(page, ["验证码", "短信验证码", "校验码", "code"], code):
            note("已自动填入验证码")
        page.wait_for_timeout(700)
        # 点「登录」：**优先文本精确匹配**。用「包含」匹配时会先点到
        # 标题「手机号登录」这类非按钮元素（点了没反应），登录就静默失败。
        lg = "登录" if _click_exact(page, "登录") else \
            _click_by_text(page, ["登录", "登录/注册", "立即登录", "确定"])
        if lg:
            note("已点击「%s」（自动登录）" % lg)
            page.wait_for_timeout(3500)
        # **核实登录结果**：旧版这里无条件写「自动登录完成（流程已走通）」——
        # 页面明明报「验证码错误」也照写，于是调用方继续走、识别不到投递表单后被
        # 兜底成「待人工提交」，用户看到的就是 bug ①「未登录成功却显示等待人工提交」。
        ev, whye = _login_evidence(page)
        if ev == "failed":
            u0 = ""
            try:
                u0 = page.url
            except Exception:
                u0 = ""
            n0 = _login_widget_count(page)

            def _login_progressed2():
                try:
                    if u0 and page.url != u0:
                        return True
                except Exception:
                    return False
                nn = _login_widget_count(page)
                return n0 > 0 and nn == 0

            budget = _handoff_budget(human_wait)
            note("⚠️ 登录未成功：页面提示「%s」→ 状态记为「需人工验证」，"
                 "请在窗口内核对验证码后手动点「登录」（**窗口保持打开**，"
                 "%s；点「进入窗口」可置前，完成后自动继续投递）"
                 % (whye, _wait_desc(budget)))
            how, whyh = _wait_human_verify(page, note=note, budget=budget,
                                           is_cancelled=is_cancelled,
                                           detector=_login_progressed2, what="登录")
            if how == "done":
                note("检测到你已完成登录（%s），自动继续投递" % whyh)
                return None
            if how == "cancelled":
                note(whyh)
                return {"needs_human": False, "stage": "已取消", "detail": "；".join(log)}
            note("⚠️ 登录仍未完成（%s）→ 记「需人工验证」，可重新发起投递" % whyh)
            return {"needs_human": True, "stage": "登录失败", "detail": "；".join(log)}
        if ev == "ok":
            note("已确认登录成功（%s）" % whye)
        else:
            note("已点击登录、未见页面报错，但未取得「登录成功」的正向证据；"
                 "继续核对是否已进入投递页")
    finally:
        if got:
            LOGIN_HOLDER = ""
            _LOGIN_GATE.release()
    return None


def apply_by_url(url, title="", profile=None, progress=None, human_wait=None,
                 headless=False, get_code=None, code_wait=300, pos_title="",
                 sms_info=None, auto_submit=True, pause_before_submit=False,
                 confirm_wait=None, company="", on_code_request=None,
                 is_cancelled=None, task_key=""):
    """投递流程入口（**两个并发闸门**，流程细节见 _apply_core）：

    闸门 1 · 该公司浏览器会话（profile_path_for(company) 的互斥锁）
        按公司使用**持久化**浏览器会话，登录态（cookie/localStorage）落盘保存，
        同一公司投下一个岗位时若已是登录态就**跳过整个登录段** —— 不再重复登录、重复取码。
        因为 Chromium 不允许同一会话目录被两个实例同时打开，同公司岗位也顺带串行。

    闸门 2 · 全局登录串行（_LOGIN_GATE，位于 _login_phase 内）
        同一时刻只允许**一个**岗位处于登录/取码阶段。前一个登录结束后，下一个才开始。
        原因：多公司并发时验证码几乎同时到达，而**不同厂商发送延迟不一致**，
        「按时间取最新一条」必然串码。填表与提交不占此闸门，仍可并发。

    company: 公司名。决定持久化会话目录；也用于短信匹配（优先取本公司在等的那条）。
    on_code_request: 回调()，在真的点了「获取验证码」之后调用，见 _login_phase。
    task_key: 本岗位的稳定 id（pid），仅供「进入窗口」按钮精确匹配，
        避免并发投递时把别家岗位的置前请求用在本窗口上。空则该岗位无法置前。
    其余参数含义与原版一致。
    """
    profile = profile or load_profile()
    pdir = profile_path_for(company)
    plk = _lock_for(pdir)
    log = []

    def note(msg):
        log.append(msg)
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    if not _acquire(plk, note, "该公司的浏览器会话（同公司岗位串行）", max_wait=7200):
        return {"needs_human": True, "stage": "会话排队超时",
                "detail": "；".join(log + ["等待该公司浏览器会话超过上限，已放弃"])}
    try:
        return _apply_core(url, title=title, profile=profile, emit=note,
                           human_wait=human_wait, headless=headless,
                           get_code=get_code, code_wait=code_wait,
                           pos_title=pos_title, sms_info=sms_info,
                           auto_submit=auto_submit,
                           pause_before_submit=pause_before_submit,
                           confirm_wait=confirm_wait, company=company,
                           on_code_request=on_code_request, pdir=pdir,
                           is_cancelled=is_cancelled, task_key=task_key)
    finally:
        plk.release()


def _ensure_spare_tab(ctx, page):
    """关自己的标签页之前，确保浏览器里**还剩至少一个页面**。

    为什么：CDP 接管的是用户浏览器，私有持久会话也一样 —— 如果里面只剩我们这一页，
    `page.close()` 会把**整个窗口**关掉。用户反复反馈的「填写自动关闭」有一部分
    就是这里：站点把表单开到新标签页、我们收养后关掉旧页，若那恰好是唯一一页，
    Chromium 就退出窗口。
    """
    try:
        alive = [pg for pg in ctx.pages if pg is not page and not pg.is_closed()]
    except Exception:
        return
    if alive:
        return
    try:
        ctx.new_page()
    except Exception:
        pass


def _same_site(a, b):
    """两个 URL 是否同站（区分「站点自己开的新标签页」与第三方广告/协议弹窗）。"""
    try:
        from urllib.parse import urlparse
        ha = (urlparse(a or "").hostname or "").lower()
        hb = (urlparse(b or "").hostname or "").lower()
        if ha.startswith("www."):
            ha = ha[4:]
        if hb.startswith("www."):
            hb = hb[4:]
        if not ha or not hb:
            return False
        if ha == hb:
            return True

        def root(h):
            p = [x for x in h.split(".") if x]
            if len(p) >= 3 and p[-2] in ("com", "net", "org", "gov", "edu", "co"):
                return ".".join(p[-3:])
            return ".".join(p[-2:]) if len(p) >= 2 else h
        return root(ha) == root(hb)
    except Exception:
        return False


def _adopt_new_tab(ctx, cur, before, note, where=""):
    """站点把表单开在**新标签页**时：切过去，并关掉本次投递自己的旧标签页。

    用户报的「一次打开两个页面投递」：站点用 target=_blank 把投递表单/登录页开在新标签，
    而旧实现只在「登录后重新点投递」那一处切换（且切换后**不关旧页**），首次点击
    完全没跟踪新标签 → 两个页面同时开着、我们在旧页上继续操作。

    安全边界（避免「修一个 bug 引入两个」）：
      · 只认**同站**的新标签页；第三方广告/协议弹窗一律忽略（不动、也不关）；
      · 只关 `cur` —— 它是本次投递自己开的标签页（CDP 模式下同样是 `ctx.new_page()` 新建的），
        **绝不关用户自己的页面**；
      · 没有新标签页就原样返回，行为与旧版一致；
      · **只收养「无主」或「本岗位自己开」的新标签页**（`_PAGE_OWNER`）——
        CDP 下并发岗位共享同一个 `ctx.pages`，旧逻辑会把别家岗位的页面抢过来，
        导致「验证码填进别的窗口」。这条是 2026-09-23 用户反馈后补的。
    """
    _sweep_pages()
    try:
        now = list(ctx.pages)
    except Exception:
        return cur
    key = _cur_key()
    # **只认「无主」或「本岗位自己开」的新标签页**（见 _PAGE_OWNER 注释）：
    # 并发共享同一个 ctx 时，直接取 fresh[-1] 会把别家岗位刚开的页面抢过来。
    others = [pg for pg in now if pg not in before and not _claimable(pg, key)]
    fresh = [pg for pg in now if pg not in before and _claimable(pg, key)]
    if not fresh:
        if others:
            note("（已忽略其它岗位新开的 %d 个标签页：不抢别人的窗口）" % len(others))
        return cur
    newpg = fresh[-1]
    if not _try_claim(newpg, key):
        note("（该新标签页刚被其它岗位占用，放弃收养、不抢窗口）")
        return cur
    try:
        newpg.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    try:
        burl = newpg.url
    except Exception:
        burl = ""
    try:
        cur_url = cur.url
    except Exception:
        cur_url = ""
    if not _same_site(burl, cur_url):
        note("（已忽略一个新开的非本站在页：%s）" % ((burl or "about:blank")[:70]))
        release_page(newpg)      # 不是我们的页面 → 归还登记，别占着
        return cur
    # **绝不收养「列表页」**：MokaHR 的页脚「立即投递」链接会开一个 #/jobs 列表页。
    # 旧逻辑照收不误，接着 cur.close() 把**真正的详情页**关掉 → 后续在列表页上采集
    # 字段（命中 0 项），用户看到「没填 + 窗口自己关了」。
    try:
        if _url_looks_list(burl) and not _url_looks_list(cur_url):
            note("（新标签页是岗位列表（%s），不是投递表单 → 不收养、也不关当前页）"
                 % ((burl or "")[-42:]))
            release_page(newpg)
            return cur
    except Exception:
        pass
    try:
        newpg.on("dialog", lambda d: _accept_dialog(d))
    except Exception:
        pass
    newpg.wait_for_timeout(1500)
    if cur is not newpg:
        try:
            cur.close()      # 关掉本次投递自己开的那一页 → 不再「两个页面同时在投递」
        except Exception:
            pass
        release_page(cur)
    note("%s在新标签页中打开，已切换过去（并关掉本次投递的旧标签页）" % (where or "页面"))
    return newpg


def _apply_core(url, title="", profile=None, emit=None, human_wait=None,
                headless=False, get_code=None, code_wait=300, pos_title="",
                sms_info=None, auto_submit=True, pause_before_submit=False,
                confirm_wait=None, company="", on_code_request=None, pdir=None,
                is_cancelled=None, task_key=""):
    """真正的投递流程（调用方已持有该公司会话锁）。返回 {needs_human, stage, detail}。"""
    set_task_key(task_key)   # 本线程在处理哪个岗位 → 供「进入窗口」精确匹配
    profile = profile or load_profile()
    phone = (profile.get("phone") or "").strip()
    log = []

    def note(msg):
        log.append(msg)
        if emit:
            try:
                emit(msg)
            except Exception:
                pass
    if _is_cancelled(is_cancelled):
        note("已取消：尚未打开岗位页，直接结束本岗位")
        return {"needs_human": False, "stage": "已取消", "detail": "；".join(log)}
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        # **持久化**会话（按公司）：登录态落盘，同公司后续岗位不必重新登录
        ctx, _via, _closer = _open_session(p, pdir, headless, note, company=company)
        # 异常 / 需人工时置 True：窗口留给用户看现场，绝不自动关
        # （旧代码没有这层 —— 任何 Playwright/JS 异常都直落 finally 关窗，用户看到的
        #   就是「填表过程中窗口自动关闭」，看板上只剩一句笼统的失败。）
        _keep_window = False
        try:
            if _via == "cdp":
                # 接管模式下 ctx 是用户**自己的**浏览器：必须新开标签页，
                # 绝不能复用 pages[0]（那是用户正在看的页面，会被劫持/改写）
                page = ctx.new_page()
            else:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
            # **登记归属**：这一页是本岗位的，别的并发岗位不得收养/抢占它
            claim_page(page)
            # 点「提交」后站点常用原生 confirm() 二次确认；不接管会直接把流程挂死。
            page.on("dialog", lambda d: _accept_dialog(d))
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)
            note("已打开岗位页（会话已持久化：%s）" % os.path.basename(pdir))


            # 1) 列表页 → 在「目标职位所在行」点击投递（优先用扫描出的精确岗位名）
            kws = ([pos_title] if pos_title else []) + _title_keywords(title)
            before1 = list(ctx.pages)     # 点击前快照：用于识别站点新开的标签页
            hit = _click_apply_in_row(page, kws) if kws else ""
            if hit:
                note("已定位职位并点击投递：%s" % hit)
                page.wait_for_timeout(3500)
            else:
                h = _click_apply_entry(page, ["申请职位", "投递简历", "立即申请",
                                              "我要投递", "在线投递",
                                              "立即投递", "投递"], note=note)
                if h:
                    note("已点击：%s" % h)
                    page.wait_for_timeout(3500)
                else:
                    note("未找到投递入口，请在窗口中手动选择职位")
            # 首次点击也可能把表单开在新标签页 → 统一收养（切过去 + 关掉旧的本次投递标签页）
            page = _adopt_new_tab(ctx, page, before1, note, "首次点击投递后的页面")

            # 1.5) **已是登录态就整段跳过登录**：同一公司的第 2 个岗位会命中这里
            #      （会话已持久化到该公司 profile，cookie 还在）。
            #      跳过 = 不填手机号 / 不发短信 / 不取码 —— 既不重复登录，也不制造串码。
            sig0 = _wait_delivery_form(page, note, tries=2, gap=1500, where="打开岗位页后")
            if _looks_like_delivery_form(sig0):
                note("已是登录态（当前页就是投递表单，%s）：跳过登录与短信验证"
                     % _sig_desc(sig0))
            else:
                if _is_cancelled(is_cancelled):
                    note("已取消：进入登录段前中断")
                    return {"needs_human": False, "stage": "已取消",
                            "detail": "；".join(log)}
                err = _login_phase(page, phone, emit=note, get_code=get_code,
                                   code_wait=code_wait, human_wait=human_wait,
                                   sms_info=sms_info, on_code_request=on_code_request,
                                   company=company, is_cancelled=is_cancelled)
                if err:
                    return err


            # 7) 登录成功后**继续走投递**（旧版到此就结束了，这正是用户报的
            #    「思特奇登录成功以后未见开启自动填写投递信息流程」）。
            #    myyule 这类站的登录门禁是 `回到列表页`，所以必须重新点一次「投递」。
            page.wait_for_timeout(3000)
            sig = _wait_delivery_form(page, note, tries=5, gap=2000, where="登录后")
            if not _looks_like_delivery_form(sig):
                note("登录后当前页不是投递表单（%s），重新点开投递入口" % _sig_desc(sig))
                before = list(ctx.pages)
                hit2 = _click_apply_in_row(page, kws) if kws else ""
                if hit2:
                    note("已在职位行重新点击投递：%s" % hit2)
                else:
                    hit2 = _click_by_text(page, ["立即投递", "投递简历", "立即申请", "申请职位",
                                                 "我要投递", "在线投递", "投递"]) or ""
                    if hit2:
                        note("已重新点击投递入口：%s" % hit2)
                    else:
                        note("⚠️ 登录后没找到可点的投递入口")
                if hit2:
                    page.wait_for_timeout(3500)
                # 部分站点的投递表单在**新标签页**打开 → 统一走「收养新标签页」
                # （切过去 + 关掉本次投递自己的旧标签页，避免两个页面同时在投递）
                page = _adopt_new_tab(ctx, page, before, note, "投递表单")
                sig = _wait_delivery_form(page, note, tries=4, gap=2000, where="重新点开投递入口后")

            if not _looks_like_delivery_form(sig):
                budget = _handoff_budget(human_wait)
                # ⚠️ 先分辨「**没登录成功**」还是「需要人手动点进投递页」——
                #    旧代码不分辨，一律进「等人工提交」，于是 bug ①「未登录成功却显示
                #    等待人工提交」：明明还停在登录/验证表单，看板却记成了「待人工提交」。
                if _looks_like_login_page(page):
                    u0 = ""
                    try:
                        u0 = page.url
                    except Exception:
                        u0 = ""
                    n0 = _login_widget_count(page)

                    def _login_done_here():
                        try:
                            if u0 and page.url != u0:
                                return True
                        except Exception:
                            return False
                        nn = _login_widget_count(page)
                        return n0 > 0 and nn == 0

                    note("⚠️ 登录未完成：当前页仍是登录/验证表单（%s）→ 状态记为"
                         "「需人工验证」，请在窗口内完成登录（点「进入窗口」可置前），"
                         "完成后我会自动继续投递（**窗口保持打开**，%s）"
                         % (_sig_desc(sig), _wait_desc(budget)))
                    how, why = _wait_human_verify(page, note=note, budget=budget,
                                                  is_cancelled=is_cancelled,
                                                  detector=_login_done_here, what="登录")
                    if how == "cancelled":
                        note(why)
                        return {"needs_human": False, "stage": "已取消",
                                "detail": "；".join(log)}
                    if how == "done":
                        note("检测到你已完成登录（%s），自动继续投递" % why)
                        sig = _wait_delivery_form(page, note, tries=3, gap=2000,
                                                  where="人工登录后")
                    if not _looks_like_delivery_form(sig):
                        note("⚠️ 登录仍未完成：当前页仍是登录/验证表单（%s）"
                             % _sig_desc(sig))
                        return {"needs_human": True, "stage": "登录未完成",
                                "detail": "；".join(log)}
                else:
                    # 不是登录页 → 站点要求人自己点进投递页。**等他进去后自动填表**，
                    # 而不是只干等提交（bug ③：人工进入投递页后自动输入未生效）。
                    note("⚠️ 登录完成后仍未识别到投递表单（%s）：自动填表暂未执行；"
                         "等待人工提交（窗口保持打开）：请在窗口内自行进入投递页，"
                         "**你一进入投递表单我就自动填写并继续提交**"
                         "（%s；点「进入窗口」可置前）" % (_sig_desc(sig), _wait_desc(budget)))
                    how, why = _wait_human_reach_form(page, note=note, budget=budget,
                                                      is_cancelled=is_cancelled,
                                                      tag="等待人工提交（窗口保持打开）：")
                    if how == "submitted":
                        note("检测到你已提交：投递成功 ✓（%s）"
                             % (why or "人工进入投递页后提交"))
                        return {"needs_human": False, "stage": "投递成功（人工确认）",
                                "detail": "；".join(log)}
                    if how == "cancelled":
                        note(why)
                        return {"needs_human": False, "stage": "已取消",
                                "detail": "；".join(log)}
                    if how != "done":
                        note("⚠️ 等待 %ds 仍未检测到你进入投递表单（%s）" % (budget, why))
                        return {"needs_human": True, "stage": "未找到投递表单",
                                "detail": "；".join(log)}
                    note("检测到你已进入投递表单（%s），开始自动填写投递信息" % why)
                    sig = _form_signature(page)

            if _is_cancelled(is_cancelled):
                note("已取消：填表前中断（未修改表单）")
                return {"needs_human": False, "stage": "已取消", "detail": "；".join(log)}
            note("已进入投递表单，开始自动填写投递信息")
            try:
                summ = fill_page(page, profile, note=note)
            except Exception as e:
                note("自动填写投递信息失败：%s: %s" % (type(e).__name__, e))
                return {"needs_human": True, "stage": "填表失败", "detail": "；".join(log)}
            note("填写结果：%s" % summ)
            page.wait_for_timeout(1200)

            # 6.5) **提交前再处理一次协议勾选**：投递表单下方常另有一句
            #      「同意并阅读《隐私声明》」+ 它自己独立的勾选框（与登录页那个不是同一个），
            #      是**提交门禁**：不勾 → 提交被拦或静默失败。登录段只覆盖登录页那一处，
            #      所以这里必须补一次（用户报的「下方同意并阅读隐私声明勾选框不会被勾选」）。
            ag_form = ""
            for _k in range(2):
                ag_form = _agree_terms(page) or ag_form
                if _terms_state(page) == "checked":
                    break
                f = _force_check_terms(page)
                if f:
                    ag_form = "已直接勾选：%s" % f
                    break
                if _terms_state(page) == "none":
                    break          # 页面没有协议勾选框 → 不打扰
                page.wait_for_timeout(600)
            if ag_form:
                note("已处理投递表单的同意声明：%s" % ag_form)
            if _terms_state(page) == "unchecked":
                note("⚠️ 投递表单下方的「同意并阅读《隐私声明》」仍未勾选成功："
                     "提交可能被拦下，请看板日志确认原因")

            # 7.1) 提交（默认自动；可用 auto_submit=False 关掉，只填不交）
            if not auto_submit and not pause_before_submit:
                note("已按配置跳过自动提交（仅填表，未提交）；浏览器窗口随后会关闭")
                return {"needs_human": False, "stage": "已填表未提交", "detail": "；".join(log)}

            did_fill = "已填 0 项" not in summ
            if not did_fill:
                budget = _handoff_budget(human_wait)
                note("⚠️ 表单一个字段都没填上，为避免提交空表，**跳过自动提交**；"
                     "等待人工提交（窗口保持打开）：请在窗口内手动填写并提交"
                     "（%s；我盯着页面，你提交成功我会记下来）" % _wait_desc(budget))
                pstf, pevf = _park_before_submit(page, note=note, confirm_wait=budget,
                                                 tag="等待人工提交（窗口保持打开）：",
                                                 is_cancelled=is_cancelled)
                if pstf == "success":
                    note("你已提交：投递成功 ✓（%s）" % (pevf or "人工填写后提交"))
                    return {"needs_human": False, "stage": "投递成功（人工确认）",
                            "detail": "；".join(log)}
                if pstf == "dup":
                    note("页面提示该职位已投递过（%s）" % pevf)
                    return {"needs_human": False, "stage": "已投递过", "detail": "；".join(log)}
                note("⚠️ 等待 %ds 仍未确认提交：表单没填上、也未见提交成功" % budget)
                return {"needs_human": True, "stage": "填表未命中", "detail": "；".join(log)}

            # 7.2) **停在提交前**：把窗口交给你确认（默认给单岗位投递用）
            if pause_before_submit:
                # confirm_wait 未显式指定（None）→ 用 PAUSE_CONFIRM_WAIT（**默认 0 = 不超时**）。
                # 用户要求：停表后窗口不许因为等待超时而关闭，人可以几小时后回来核对。
                # 想限时收尾可设 RESUME_PAUSE_CONFIRM_WAIT=<秒>（或该岗位显式传正数）。
                _cw = _pause_budget(confirm_wait)
                note("已按配置**停在提交前**：浏览器窗口保持打开，请核对投递信息后手动点「提交」；"
                     "我会盯着页面，检测到提交成功就记录结果"
                     + ("（最长等 %ds）" % _cw if _cw else "（不设超时）"))
                pst, pev = _park_before_submit(page, note=note, confirm_wait=_cw,
                                               tag="等待人工提交（窗口保持打开）：",
                                               is_cancelled=is_cancelled)
                if pst == "success":
                    note("检测到你已提交：投递成功 ✓（页面提示：%s）" % pev)
                    return {"needs_human": False, "stage": "投递成功（人工确认）",
                            "detail": "；".join(log)}
                if pst == "dup":
                    note("页面提示该职位已投递过（%s）" % pev)
                    return {"needs_human": False, "stage": "已投递过", "detail": "；".join(log)}
                if pst == "cancelled":
                    note("已取消：停在提交前中断（投递信息已填好但**未提交**）")
                    return {"needs_human": False, "stage": "已取消", "detail": "；".join(log)}
                if pst == "timeout":
                    note("⚠️ 等待 %ds 仍未确认提交：投递信息已填好但**未提交**（窗口随后会关闭）"
                         % _cw)
                    return {"needs_human": True, "stage": "已填表待确认", "detail": "；".join(log)}
                note("⚠️ 浏览器窗口已关闭：投递信息已填好，但**无法确认是否提交成功**")
                return {"needs_human": True, "stage": "已填表待确认", "detail": "；".join(log)}

            if _is_cancelled(is_cancelled):
                note("已取消：提交前中断（表单已填好但**未提交**）")
                return {"needs_human": False, "stage": "已取消", "detail": "；".join(log)}
            state, ev = _submit_application(page, note=note)
            if state == "success":
                note("投递成功 ✓（页面提示：%s）" % ev)
                return {"needs_human": False, "stage": "投递成功", "detail": "；".join(log)}
            if state == "dup":
                note("该职位此前已投递过（页面提示：%s）" % ev)
                return {"needs_human": False, "stage": "已投递过", "detail": "；".join(log)}
            if state == "blocked":
                note("站点限制：页面提示「%s」→ 该岗位当前**不可投递**，"
                     "已记录原因，直接进入下一个岗位" % ev)
                return {"needs_human": False, "stage": "投递受限", "detail": "；".join(log)}
            if state == "invalid":
                # **这里必须把窗口交给人**：页面明说「必填项」没填，人补一下就投出去了。
                # 旧代码「不驻留，直接进入下一个岗位」→ 窗口立刻关闭，用户看到的就是
                # 「明明已经进入最后确认投递阶段却关闭窗口」。
                budget = _handoff_budget(human_wait)
                note("提交被拦下，页面提示「%s」：表单仍有必填项没填上；"
                     "等待人工提交（窗口保持打开）：请补填后手动点提交，"
                     "我盯着页面记录结果（%s）" % (ev, _wait_desc(budget)))
                psti, pevi = _park_before_submit(page, note=note, confirm_wait=budget,
                                                 tag="等待人工提交（窗口保持打开）：")
                if psti == "success":
                    note("你已提交：投递成功 ✓（页面提示：%s）" % (pevi or "补填后提交"))
                    return {"needs_human": False, "stage": "投递成功（人工确认）",
                            "detail": "；".join(log)}
                if psti == "dup":
                    note("页面提示该职位已投递过（%s）" % pevi)
                    return {"needs_human": False, "stage": "已投递过", "detail": "；".join(log)}
                note("⚠️ 等待 %ds 仍未确认提交：投递信息已填好但**未提交**" % budget)
                return {"needs_human": True, "stage": "已填表待确认",
                        "detail": "；".join(log)}
            if state == "clicked":
                # 结果未知：窗口留着再盯一小段，等页面把成功文案/跳转交出来（过去这里
                # 立刻关窗，用户反映「到了最后确认投递阶段窗口却关了」）。
                budget2 = _SUBMIT_CONFIRM_WAIT
                note("已点击「%s」但页面未出现明确成功提示（可能正在跳转/被风控）；"
                     "等待人工提交（窗口保持打开）：我再盯 %ds 等结果，"
                     "你也可在窗口内确认" % (ev, budget2))
                pstc, pevc = _park_before_submit(page, note=note, confirm_wait=budget2,
                                                 tag="等待人工提交（窗口保持打开）：")
                if pstc == "success":
                    note("投递成功 ✓（页面提示：%s）" % (pevc or "提交后确认成功"))
                    return {"needs_human": False, "stage": "投递成功", "detail": "；".join(log)}
                if pstc == "dup":
                    note("该职位此前已投递过（页面提示：%s）" % pevc)
                    return {"needs_human": False, "stage": "已投递过", "detail": "；".join(log)}
                note("⚠️ 已点击提交但 %ds 内未取得明确成功证据：结果**未确认**，"
                     "请在窗口/看板确认" % budget2)
                return {"needs_human": True, "stage": "提交结果未知", "detail": "；".join(log)}
            budget = _handoff_budget(human_wait)
            note("⚠️ 页面上没找到可点的提交按钮，投递信息已填好但**未提交**；"
                 "等待人工提交（窗口保持打开）：请你在窗口内手动点提交"
                 "（%s）" % _wait_desc(budget))
            pstb, pevb = _park_before_submit(page, note=note, confirm_wait=budget,
                                             tag="等待人工提交（窗口保持打开）：")
            if pstb == "success":
                note("你已提交：投递成功 ✓（%s）" % (pevb or "人工点提交"))
                return {"needs_human": False, "stage": "投递成功（人工确认）",
                        "detail": "；".join(log)}
            if pstb == "dup":
                note("页面提示该职位已投递过（%s）" % pevb)
                return {"needs_human": False, "stage": "已投递过", "detail": "；".join(log)}
            note("⚠️ 等待 %ds 仍未确认提交：投递信息已填好但**未提交**" % budget)
            return {"needs_human": True, "stage": "已填表待确认", "detail": "；".join(log)}
        except Exception as _exc:      # noqa: BLE001
            # **填到一半绝不能自动关窗**：先如实记录异常，再把窗口交给人看现场。
            _keep_window = True
            note("⚠️ 填写过程出现异常：%s: %s（窗口保持打开，请确认现场后再处理）"
                 % (type(_exc).__name__, _exc))
            if _page_alive(page):
                try:
                    _park_before_submit(page, note=note, confirm_wait=0,
                                        tag="等待人工提交（窗口保持打开）：",
                                        is_cancelled=is_cancelled)
                except Exception:
                    pass
            else:
                # **页面本身已经被站点/浏览器关掉了**（典型：点投递跳到列表页后目标页被关）。
                # 这时「保持窗口」是空话，驻留也只会让队列空转 → 如实说明，正常收尾。
                note("⚠️ 该岗位的页面已被站点关闭，无法保留窗口；本岗位一个字都没填上，"
                     "请重新投递（若反复出现，说明投递入口点到了导航链接）")
                _keep_window = False
            return {"needs_human": True, "stage": "填写异常",
                    "detail": "；".join(log)}
        finally:
            try:
                release_page(page)    # 解除标签页归属登记
            except Exception:
                pass
            _sweep_pages()
            if not _keep_window:
                if _via == "cdp":
                    try:
                        _ensure_spare_tab(ctx, page)   # 先留一页，别把窗口关没了
                        page.close()   # 只关本次投递标签页，浏览器留给用户/下个岗位
                    except Exception:
                        pass
                _closer()
