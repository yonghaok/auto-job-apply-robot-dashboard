"""精准投递入口发现（找入口）——启发式，纯本地。

按用户要求：**不接入外部 AI 模型**。本模块只做确定性启发式：
用 Playwright 打开岗位列表页，扫描锚点文案命中 申请/投递/报名/apply 等，
返回最可能是「投递入口」的链接；找不到或浏览器不可用 → EntryNotFound（交人工复核）。

注：旧版在启发式未命中时会回退到 LLM（_pick_ai）。该回退已按用户要求移除，
因此不再 import llm_client，也不再需要 llm_config.json。
"""
from urllib.parse import urljoin  # noqa: F401  (保留给未来相对链接兜底)

ENTRY_KEYWORDS = ["申请", "投递", "报名", "应聘", "立即申请", "马上申请",
                  "在线申请", "apply", "submit", "join", "register"]

ENTRY_NEG = ["申请记录", "申请进度", "已申请", "我的申请"]


class EntryNotFound(Exception):
    """未找到精准投递入口。"""
    pass


def find_entry(listing_url, company="", title="", timeout=20000):
    """返回投递入口链接字符串；找不到抛 EntryNotFound（无 AI 回退，交人工复核）。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        raise EntryNotFound("playwright 不可用，无法自动找入口")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", args=["--no-sandbox"])
            try:
                page = browser.new_page()
                page.goto(listing_url, wait_until="domcontentloaded", timeout=timeout)
                page.wait_for_timeout(4000)
                links = page.eval_on_selector_all(
                    "a[href]",
                    "els => els.map(e => ({href: e.href, text: (e.innerText || '').trim()}))",
                )
            finally:
                browser.close()
    except Exception as e:
        raise EntryNotFound("页面打开失败: %s" % e)

    return _pick(links)


def _pick(links):
    scored = []
    for l in links:
        href = (l.get("href") or "").strip()
        text = (l.get("text") or "").strip()
        if not href or href.startswith("javascript") or href.startswith("#"):
            continue
        low = text.lower()
        hit = False
        for kw in ENTRY_KEYWORDS:
            if kw.lower() in low:
                hit = True
                break
        if not hit:
            continue
        # 排除"我的申请/已申请"等历史类
        neg = any(n.lower() in low for n in ENTRY_NEG)
        if neg:
            continue
        # 文案越短越像按钮
        scored.append((len(text), href))
    if not scored:
        raise EntryNotFound("未找到投递入口链接")
    scored.sort(key=lambda x: x[0])
    return scored[0][1]
