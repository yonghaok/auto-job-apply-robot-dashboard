"""任务状态持久化。

把每条候选任务的人工判定（confirmed / rejected）落盘到 state.json，
重启服务后可叠加回 xlsx 加载结果，不丢人工操作。
"""
import json
import os

STATE_PATH = os.environ.get(
    "RESUME_STATE",
    os.path.join(os.path.dirname(__file__), "state.json"),
)


def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)  # 原子落盘


def apply_status(candidates, state):
    """把已保存的状态叠加回候选列表。"""
    for c in candidates:
        rec = state.get(c["id"])
        if rec:
            c["status"] = rec.get("status", c["status"])
            c["note"] = rec.get("note", "")
    return candidates
