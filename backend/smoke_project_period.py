# -*- coding: utf-8 -*-
"""项目/工作经历 JSON 解析的 period 修复回归（T67）。

用户报：「修复项目json解析后无period项目时间问题」。

缺陷（两处，同一处不对称）：
  1. `/api/parse-resume` 的 system prompt 里
       experiences (array of {company, role, period, highlights:[str]})   ← 有 period
       projects    (array of {name, role, desc})                          ← **漏了 period**
     于是从简历 PDF 抽出来的项目**天生没有时间**。
  2. 解析结果是「模型返回啥就存啥」，不做结构归一 → 缺字段/类型不对都会原样落盘。
  前端 textarea 的占位符也是同一处不对称（工作经历有 "period":""，项目没有）。

覆盖
 A) `_norm_resume_items`（结构归一）：补 period · 保留原值 · 列表键归一 ·
    非 dict 项兜底不丢 · 额外字段保留 · 非列表输入 · None → ""
 B) prompt schema 已对称：projects 含 period + 「照抄原文、不许编造」的硬要求
 C') **证伪**：绕过归一（等价于修复前的 `d.get("projects") or []`）→
    没有 period 的项目**必须重现「无 period key」**（即用户报的现象）
 D) 前端两个 textarea 占位符都含 period（同一处不对称已消除）
 E) 源码结构：两处调用都走归一

用法（cwd=backend）：<venv-python> smoke_project_period.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["RESUME_CDP_ENDPOINT"] = "http://127.0.0.1:59999"

import server as srv               # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


N = srv._norm_resume_items

print("A) _norm_resume_items · 结构归一")
r = N([{"name": "AI 短剧分镜创作工作台", "role": "产品负责人", "desc": "x"}], srv._PROJ_KEYS)
check("A1 项目缺 period → 自动补成 ''（不再是「无 period」）",
      r == [{"name": "AI 短剧分镜创作工作台", "role": "产品负责人",
             "period": "", "desc": "x"}], repr(r))
r = N([{"name": "P", "role": "R", "period": "2026.02 – 2026.06", "desc": "d"}], srv._PROJ_KEYS)
check("A2 已带的 period 原样保留",
      r[0]["period"] == "2026.02 – 2026.06", repr(r))
r = N([{"name": "P", "period": None}], srv._PROJ_KEYS)
check("A3 null 值 → ''（不是 'None'）", r[0]["period"] == "" and r[0]["desc"] == "", repr(r))
r = N([{"company": "C", "period": "2026", "highlights": "只有一条"}], srv._EXP_KEYS,
      list_key="highlights")
check("A4 highlights 给成字符串 → 归一成单元素列表", r[0]["highlights"] == ["只有一条"], repr(r))
r = N([{"company": "C", "highlights": ["a", "", "b"]}], srv._EXP_KEYS, list_key="highlights")
check("A5 highlights 列表里的空串被剔除", r[0]["highlights"] == ["a", "b"], repr(r))
r = N([{"company": "C"}], srv._EXP_KEYS, list_key="highlights")
check("A6 整个 highlights 缺失 → 补成 []（且 period 补成 ''）",
      r[0]["highlights"] == [] and r[0]["period"] == "", repr(r))
r = N(["某公司（模型只回了一个字符串）"], srv._EXP_KEYS, list_key="highlights")
check("A7 非 dict 项按首列兜底、**不静默丢弃**",
      len(r) == 1 and r[0]["company"] == "某公司（模型只回了一个字符串）"
      and r[0]["highlights"] == [], repr(r))
r = N([{"name": "P", "period": "2026", "extra_llm_field": "别丢我"}], srv._PROJ_KEYS)
check("A8 模型多给的字段被保留（不丢信息）",
      r[0].get("extra_llm_field") == "别丢我", repr(r))
check("A9 非列表输入（模型返回 dict/None/str）→ []",
      N({"name": "P"}, srv._PROJ_KEYS) == [] and N(None, srv._PROJ_KEYS) == []
      and N("x", srv._PROJ_KEYS) == [])
check("A10 空列表 / 全空项 → 不产生垃圾行",
      N([], srv._PROJ_KEYS) == [] and N(["", "  "], srv._PROJ_KEYS) == [])

print("B) prompt schema 已对称（projects 也要 period）")
src = open(os.path.join(HERE, "server.py"), encoding="utf-8").read()
check("B1 prompt 里 projects schema 含 period",
      "projects (array of {name, role, period, desc})" in src)
check("B2 prompt 明确要求 period 照抄原文、不许编造",
      "never invent a date" in src and "MUST carry a period field" in src)
check("B3 prompt 给出 period 的书写格式示例",
      "YYYY.MM – YYYY.MM" in src)
check("B4 旧的无 period 写法已彻底消失",
      "projects (array of {name, role, desc})" not in src)

print("C') 证伪：绕过归一 → 必须重现「项目无 period」")
post = [{"name": "AI 短剧分镜创作工作台", "role": "产品负责人", "desc": "x"}]   # LLM 没给 period
old_way = post or []          # 等价于修复前的 `d.get("projects") or []`
check("C'1 不走归一确实拿不到 period（这就是用户报的现象）",
      "period" not in old_way[0], repr(old_way))
check("C'2 走归一后同一份输入一定有 period",
      "period" in N(post, srv._PROJ_KEYS)[0])

print("D) 前端占位符两处对称")
fsrc = open(os.path.join(HERE, "..", "frontend", "index.html"),
            encoding="utf-8").read()
check("D1 工作经历占位符含 \"period\":\"\"", '"period":""' in fsrc)
check("D2 项目占位符也含 period",
      '[{"name":"","role":"","period":"","desc":""}]' in fsrc)

print("E) 源码结构")
check("E1 experiences 走归一（且 highlights 按列表键处理）",
      '_norm_resume_items(d.get("experiences"), _EXP_KEYS,' in src
      and 'list_key="highlights"' in src)
check("E2 projects 走归一", '_norm_resume_items(d.get("projects"), _PROJ_KEYS)' in src)
check("E3 归一助手是模块级函数（import 时即定义）",
      "def _norm_resume_items(items, keys, list_key=None):" in src)

print("\n汇总：PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
