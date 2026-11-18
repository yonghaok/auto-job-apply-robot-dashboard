# -*- coding: utf-8 -*-
"""级联 / 日期面板等**自定义控件填表**的离线自测（T62）。

背景（用户 2026-09-23 报「级联填表失败」）：税友/高途 `native_place→请输入籍贯(TimeoutError)`、
货拉拉 `birth→出生日期(年龄)(TimeoutError)`。本地按真实形态复现，根因三条：
  ① readonly input + 点击才展开的面板（籍贯级联 / 出生日期）→ `page.fill` 的**可操作性检查
     一直重试到超时**（Playwright 默认 30s/字段）才抛 TimeoutError；
  ② 真值塞在 `display:none` 的原生 input 里 → 同样 30s 超时；
  ③ SPA 在采集之后重渲染会抹掉 `data-filler-idx` → **每个字段**都 30s 超时（复现 3 字段 90s 全败）。

覆盖：
  A) 纯函数：`_split_levels` / `_norm_token` / `_pick_token` / `_value_ok`
     —— **重点是反向断言**：错一位的手机号、错省份、只填了第 1 级，都**不许**判成功
  B) 真浏览器 5 种控件形态（C1~C5），并**带耗时上限**（回归守卫：30s 干等不许回来）
  C) 源码结构断言（4s 显式超时 / fill_page 已走分层 / 「待确认」不混进「已填」）

用法（cwd=backend）：<venv-python> smoke_cascade_fill.py
"""
import inspect
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ["RESUME_CDP_ENDPOINT"] = "http://127.0.0.1:59999"

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


PROFILE = {
    "name": "张三", "phone": "13800000000", "gender": "男",
    "birth": "2004-12-05", "native_place": "福建省-龙岩市-新罗区",
}

HEAD = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
.panel{display:none} .panel.open{display:block}
.col{display:inline-block;vertical-align:top;width:110px;border:1px solid #ccc}
.col div{padding:4px;cursor:pointer}
</style></head><body>"""

C1 = HEAD + """
<div><label for="np">籍贯</label>
  <input id="np" class="van-field__control" placeholder="请输入籍贯" readonly>
  <div class="panel" id="npPanel">
    <div class="col" data-level="0"><div>北京市</div><div>福建省</div><div>浙江省</div></div>
    <div class="col" data-level="1"></div><div class="col" data-level="2"></div>
  </div></div>
<script>
const CITY={"福建省":["福州市","厦门市","龙岩市"],"浙江省":["杭州市","宁波市"],"北京市":["北京市"]};
const DIST={"龙岩市":["新罗区","永定区"],"杭州市":["西湖区","余杭区"],"北京市":["朝阳区"],"福州市":["鼓楼区"]};
const panel=document.getElementById('npPanel'), inp=document.getElementById('np'), picked=[];
inp.onclick=()=>panel.classList.add('open');
panel.querySelectorAll('.col[data-level="0"] div').forEach(d=>{ d.onclick=()=>{
  picked[0]=d.textContent;
  const c1=panel.querySelector('.col[data-level="1"]');
  c1.innerHTML=(CITY[d.textContent]||[]).map(c=>'<div>'+c+'</div>').join('');
  c1.querySelectorAll('div').forEach(e=>{ e.onclick=()=>{
    picked[1]=e.textContent;
    const c2=panel.querySelector('.col[data-level="2"]');
    c2.innerHTML=(DIST[e.textContent]||[]).map(c=>'<div>'+c+'</div>').join('');
    c2.querySelectorAll('div').forEach(f=>{ f.onclick=()=>{ picked[2]=f.textContent;
      inp.value=picked.join('-'); panel.classList.remove('open'); }; });
  }; });
}; });
</script></body></html>"""

C2 = HEAD + """
<div><label for="bd">出生日期(年龄)</label>
  <input id="bd" placeholder="请选择出生日期" readonly>
  <div class="panel" id="bdPanel">
    <div class="col" data-k="y"><div>2003年</div><div>2004年</div><div>2005年</div></div>
    <div class="col" data-k="m"><div>1月</div><div>12月</div></div>
    <div class="col" data-k="d"><div>5日</div><div>28日</div></div>
  </div></div>
<script>
const p=document.getElementById('bdPanel'), i=document.getElementById('bd'), pick={};
i.onclick=()=>p.classList.add('open');
p.querySelectorAll('.col').forEach(c=>c.querySelectorAll('div').forEach(d=>{ d.onclick=()=>{
  pick[c.dataset.k]=d.textContent;
  if(pick.y&&pick.m&&pick.d){ i.value=pick.y+pick.m+pick.d; p.classList.remove('open'); } }; }));
</script></body></html>"""

C3 = HEAD + """
<div><label>籍贯</label>
  <div id="fake" style="border:1px solid #999;padding:4px;width:200px">请选择</div>
  <input id="hidden_np" type="text" placeholder="请输入籍贯" style="display:none" readonly>
</div>
<script>
document.getElementById('fake').onclick=function(){
  document.getElementById('hidden_np').value='福建省-龙岩市-新罗区';
  this.textContent='福建省-龙岩市-新罗区'; };
</script></body></html>"""

C4 = HEAD + """
<div><label for="np2">籍贯</label><input id="np2" placeholder="请输入籍贯"></div>
<div><label for="bd2">出生日期(年龄)</label><input id="bd2" placeholder="请选择出生日期"></div>
</body></html>"""

C5 = HEAD + """
<div id="host">
  <div><label for="a">姓名</label><input id="a" placeholder="请输入姓名"></div>
  <div><label for="np3">籍贯</label><input id="np3" placeholder="请输入籍贯"></div>
  <div><label for="ph3">手机号</label><input id="ph3" placeholder="请输入手机号"></div>
</div>
<script>
const HOST=document.getElementById('host'), HTML=HOST.innerHTML;
let n=0;
new MutationObserver(()=>{           // 忠实模拟 SPA：采集刚跑完就把表单重挂 → 标记全失效
  if(n++>0) return;
  if(!HOST.querySelector('[data-filler-idx]')) return;
  HOST.innerHTML=HTML;
}).observe(HOST,{attributes:true,subtree:true,attributeFilter:['data-filler-idx']});
</script></body></html>"""

# 每个形态的耗时上限：修复前是 30s/字段，这里卡死「干等」回归
MAX_SEC = 12.0


def run_case(pw, d, name, html, expects):
    """跑一个形态。expects: {选择器: 期望值 或 断言函数}；返回 (summary, elapsed, page)。"""
    import filler
    p = os.path.join(d, name.split()[0] + ".html")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(html)
    b = pw.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
    try:
        pg = b.new_page()
        pg.goto("file://" + p, wait_until="domcontentloaded")
        t0 = time.time()
        try:
            s = filler.fill_page(pg, PROFILE, note=lambda m: None)
        except Exception as e:                    # noqa: BLE001
            s = "EXC %s: %s" % (type(e).__name__, e)
        el = time.time() - t0
        return s, el, pg, b
    except Exception:
        b.close()
        raise


def main():
    import filler

    # ---------------------------------------------------------------- A 纯函数
    print("=== A. 纯函数：拆级 / 归一化 / 选项匹配 / 回读判定 ===")
    check("_split_levels 三级地址", filler._split_levels("福建省-龙岩市-新罗区")
          == ["福建省", "龙岩市", "新罗区"])
    check("_split_levels 日期", filler._split_levels("2004-12-05") == ["2004", "12", "05"])
    check("_split_levels 斜杠/顿号分隔", filler._split_levels("浙江省/杭州市")
          == ["浙江省", "杭州市"])
    check("_split_levels 空值 → []", filler._split_levels("") == [])

    check("_norm_token 去行政区后缀", filler._norm_token("福建省") == "福建")
    check("_norm_token 去年月日", filler._norm_token("2004年") == "2004")
    check("_norm_token 数字去前导零", filler._norm_token("05日") == "5")
    check("_norm_token 去分隔符（两种写法归一后相等）",
          filler._norm_token("福建省/龙岩市") == filler._norm_token("福建省-龙岩市"))

    check("_pick_token 精确命中", filler._pick_token(["北京市", "福建省", "浙江省"], "福建省") == 1)
    check("_pick_token 年月命中", filler._pick_token(["2004年"], "2004") == 0)
    check("_pick_token 精确优先于包含（12月 而非 1月）",
          filler._pick_token(["1月", "12月"], "12") == 1)
    check("_pick_token 无命中 → None", filler._pick_token(["北京市"], "福建省") is None)

    print("\n=== A2. _value_ok 的反向断言（放宽必须有硬边界） ===")
    check("完全相同 → True", filler._value_ok("13800000000", "13800000000") is True)
    check("⚠️手机号错一位 → **不许**判成功",
          filler._value_ok("13800000001", "13800000000") is False)
    check("⚠️空值 → 不许判成功", filler._value_ok("", "2004-12-05") is False)
    check("⚠️None → 不许判成功", filler._value_ok(None, "2004-12-05") is False)
    check("⚠️错省份 → 不许判成功",
          filler._value_ok("北京市", "福建省-龙岩市-新罗区") is False)
    check("⚠️只填了第 1 级 → 不许判成功",
          filler._value_ok("福建省", "福建省-龙岩市-新罗区") is False)
    check("面板把日期回写成「2004年12月5日」→ 算成功",
          filler._value_ok("2004年12月5日", "2004-12-05") is True)
    check("面板把地址回写成斜杠分隔 → 算成功",
          filler._value_ok("福建省/龙岩市/新罗区", "福建省-龙岩市-新罗区") is True)

    # ---------------------------------------------------------------- B 真浏览器
    print("\n=== B. 真浏览器：5 种控件形态（含耗时上限 %.0fs） ===" % MAX_SEC)
    from playwright.sync_api import sync_playwright
    d = tempfile.mkdtemp()
    results = {}
    with sync_playwright() as pw:
        cases = [("C1", C1), ("C2", C2), ("C3", C3), ("C4", C4), ("C5", C5)]
        for tag, html in cases:
            s, el, pg, b = run_case(pw, d, tag, html, None)
            results[tag] = (s, el, pg)
            print("  [%s] %.1fs  %s" % (tag, el, s[:150]))
            b.close()

    s1, e1, _ = results["C1"]
    check("C1 籍贯级联(readonly+面板)：填进去了（修复前 TimeoutError 30s）",
          "native_place" in s1 and "已填 0 项" not in s1, s1[:160])
    check("C1 耗时 < %.0fs（不再干等到超时）" % MAX_SEC, e1 < MAX_SEC, "%.1fs" % e1)

    s2, e2, _ = results["C2"]
    check("C2 出生日期(readonly+面板)：填进去了（修复前 TimeoutError 30s）",
          "birth" in s2 and "已填 0 项" not in s2, s2[:160])
    check("C2 耗时 < %.0fs" % MAX_SEC, e2 < MAX_SEC, "%.1fs" % e2)

    s3, e3, _ = results["C3"]
    check("C3 隐藏原生 input：如实报「待确认」，**不**混进「已填」",
          "待确认" in s3 and "native_place" not in s3.split("；")[0], s3[:200])
    check("C3 耗时 < %.0fs" % MAX_SEC, e3 < MAX_SEC, "%.1fs" % e3)

    s4, e4, _ = results["C4"]
    check("C4 对照组 普通 input 仍然正常（回归）",
          "native_place" in s4 and "birth" in s4, s4[:160])

    s5, e5, _ = results["C5"]
    check("C5 采集后重渲染：3 个字段全部填上（修复前 3 个全败、90s）",
          all(k in s5 for k in ("phone", "native_place", "name")), s5[:200])
    check("C5 耗时 < %.0fs" % MAX_SEC, e5 < MAX_SEC, "%.1fs" % e5)

    # 真实 DOM 值复核（不只看摘要）
    with sync_playwright() as pw:
        for tag, html, sel, want in [
            ("C1", C1, "#np", "福建省-龙岩市-新罗区"),
            ("C2", C2, "#bd", "2004"),
            ("C4", C4, "#np2", "福建省-龙岩市-新罗区"),
            ("C4", C4, "#bd2", "2004-12-05"),
            ("C5", C5, "#ph3", "13800000000"),
            ("C5", C5, "#np3", "福建省-龙岩市-新罗区"),
        ]:
            p = os.path.join(d, tag + "_v.html")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(html)
            b = pw.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
            try:
                pg = b.new_page()
                pg.goto("file://" + p, wait_until="domcontentloaded")
                filler.fill_page(pg, PROFILE, note=lambda m: None)
                got = pg.input_value(sel)
            finally:
                b.close()
            check("%s 真实 DOM %s 的值 == %r" % (tag, sel, want), want in str(got), repr(got))

    # ---------------------------------------------------------------- C 结构断言
    print("\n=== C. 源码结构断言 ===")
    rsrc = inspect.getsource(filler._robust_fill_field)
    check("显式 4s 超时（不再吃 Playwright 默认 30s）", "timeout=4000" in rsrc)
    check("分层：正常 fill → 自定义控件 → JS 兜底",
          "_fill_custom_widget" in rsrc and "_js_set_value" in rsrc)
    check("回读不通过时**不**报成功", '"uncertain"' in rsrc)
    fsrc = inspect.getsource(filler.fill_page)
    # T70：一对一填值统一走 _fill_field_smart（内部再按需回落 _robust_fill_field），
    # 这样「年月区间」控件才会按区间填，而不是把整串塞进第 1 个框。
    check("fill_page 改走分层填值 _fill_field_smart（内部回落 _robust_fill_field）",
          "_fill_field_smart(page, f, val" in fsrc
          and "_robust_fill_field(page, f, val" not in fsrc)
    check("fill_page 不再有裸 `page.fill(sel, val)`（无超时那个）",
          re.search(r"page\.fill\(sel, val\)\s*$", fsrc, re.M) is None)
    check("「待确认」单独成类，不混进「已填」", "待确认" in fsrc and "uncertain" in fsrc)
    check("选择器会重定位（SPA 重渲染后仍能填）",
          "_resolve_selector(page, f)" in rsrc and "控件已不在页面上" in rsrc)
    check("面板只在**面板类**容器里找（不退回整个 document，避免点跑偏）",
          "if (!roots.length) return [];" in inspect.getsource(filler._panel_marks))

    print("\n===== 汇总：PASS=%d FAIL=%d =====" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
