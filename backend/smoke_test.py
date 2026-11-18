"""冒烟测试 v2：覆盖多栏看板新流程。
预选 → (待审批 | 入口失败) → 决策(确定/取消) → 字段匹配预览。

注意：这是**旧版预选/审批流程**的测试。新版前端已移除「添加预选 / 引擎状态 /
失败队列」三栏，日常使用走 `/api/columns`（看板）——本脚本保留只为回归旧接口。
跑完后会把候选 /reset 回 pending，不留残留。
"""
import json
import time
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8000"


def req(method, path, data=None):
    body = None
    headers = {}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    for _ in range(60):
        try:
            r = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("server not reachable")


def main():
    s, b = req("GET", "/api/stats")
    print("[stats]", s, b[:140])

    # 取一个 pending 候选
    s, b = req("GET", "/api/candidates?status=pending")
    list0 = json.loads(b)
    assert list0, "no pending candidate"
    cid = list0[0]["id"]
    print("[pick]", cid, list0[0]["company"], list0[0]["title"][:20])

    # 预选 → 触发 entry_finder
    s, b = req("POST", "/api/candidates/%s/preselect" % cid)
    d = json.loads(b)
    print("[preselect]", s, d)
    assert d.get("ok") is True
    assert d["status"] in ("待审批", "入口失败"), d

    # 预选列表可见
    s, b = req("GET", "/api/preselect")
    pres = json.loads(b)
    assert any(c["id"] == cid for c in pres), "preselect list missing"
    print("[preselect list] ok, count =", len(pres))

    # 决策：待审批→确定；入口失败→取消
    if d["status"] == "待审批":
        s, b = req("POST", "/api/preselect/%s/decide" % cid, {"decision": "确定"})
        print("[decide 确定]", s, b)
        assert json.loads(b).get("ok") is True
    else:
        s, b = req("POST", "/api/preselect/%s/decide" % cid, {"decision": "取消"})
        print("[decide 取消]", s, b)
        assert json.loads(b).get("ok") is True

    # 字段匹配预览
    s, b = req("GET", "/api/preselect/%s/mapping" % cid)
    m = json.loads(b)
    print("[mapping] ok=%s rows=%d" % (m.get("ok"), len(m.get("rows", []))))
    assert m.get("ok") is True and m.get("rows")

    # 复位该候选回 pending，避免污染数据。
    # 注意：这里**不要**用 /reject —— 那会把候选永久标成 rejected 并留在
    # state.json 里（旧的「smoke-cleanup」写法就是这么留下残留的）。
    s, b = req("POST", "/api/candidates/%s/reset" % cid, {})
    print("[cleanup reset]", s, b)

    print("\nALL SMOKE CHECKS PASSED (新流程)")


if __name__ == "__main__":
    main()
