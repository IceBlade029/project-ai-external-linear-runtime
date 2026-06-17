from datetime import datetime, timezone

from .errors import StateError
from .jsonio import read_json, write_json_atomic


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def default_state(workflow):
    return {
        "schema_version": "1.0.0",
        "runtime_version": "0.1.0",
        "status": "idle",
        "phase": workflow["initial_phase"],
        "phase_turn_index": 0,
        "task_id": None,
        "current_iteration": 1,
        "current_task_id": None,
        "task_status": {},
        "completed_iterations": [],
        "run_count": 0,
        "last_handoff_id": None,
        "last_decision_id": None,
        "last_error": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }


class StateStore:
    def __init__(self, paths):
        self.paths = paths

    def exists(self):
        return self.paths.state.is_file()

    def load(self):
        if not self.exists():
            raise StateError("未找到 runtime state。", path=str(self.paths.state))
        try:
            state = read_json(self.paths.state)
        except Exception as exc:
            raise StateError("runtime state 无法解析。", error=str(exc)) from exc
        validate_state(state)
        return state

    def save(self, state):
        state["updated_at"] = now_iso()
        validate_state(state)
        write_json_atomic(self.paths.state, state)


def validate_state(state):
    required = {
        "schema_version",
        "runtime_version",
        "status",
        "phase",
        "phase_turn_index",
        "run_count",
        "current_iteration",
        "task_status",
        "completed_iterations",
        "created_at",
        "updated_at",
    }
    missing = required - set(state)
    if missing:
        raise StateError("state 缺少必填字段。", missing=sorted(missing))
    if state["status"] not in ("idle", "waiting_human", "blocked", "done", "running"):
        raise StateError("state.status 不合法。", status=state["status"])
    if not isinstance(state["phase_turn_index"], int) or state["phase_turn_index"] < 0:
        raise StateError("phase_turn_index 必须是非负整数。")
    if not isinstance(state["run_count"], int) or state["run_count"] < 0:
        raise StateError("run_count 必须是非负整数。")
    if not isinstance(state["current_iteration"], int) or state["current_iteration"] < 1:
        raise StateError("current_iteration 必须是正整数。")
    if not isinstance(state["task_status"], dict):
        raise StateError("task_status 必须是对象。")
    if not isinstance(state["completed_iterations"], list):
        raise StateError("completed_iterations 必须是数组。")
