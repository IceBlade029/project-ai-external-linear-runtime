import json
from pathlib import Path

from .errors import StateError
from .jsonio import read_json, write_json_atomic
from .state import now_iso


REQUIRED_TASK_FIELDS = {
    "id",
    "name",
    "description",
    "dependencies",
    "expected_outputs",
    "allowed_write_paths",
    "risk_level",
    "tdd",
}
VALID_TASK_STATUSES = {"pending", "in_progress", "ready_for_completion", "done", "blocked"}
VALID_RISK_LEVELS = {"low", "medium", "high"}


class TaskStore:
    def __init__(self, paths, state_store):
        self.paths = paths
        self.state_store = state_store

    def tasks_path(self, iteration=None):
        state = self.state_store.load()
        iteration = iteration or state["current_iteration"]
        return self.paths.tasks / f"iteration_{iteration}_tasks.json"

    def load_tasks(self, iteration=None):
        path = self.tasks_path(iteration)
        if not path.is_file():
            return []
        try:
            data = read_json(path)
        except Exception as exc:
            raise StateError("任务文件无法解析。", path=str(path), error=str(exc)) from exc
        validate_tasks(data)
        return data

    def sync(self):
        state = self.state_store.load()
        tasks = self.load_tasks(state["current_iteration"])
        statuses = {}
        for task in tasks:
            existing = state.get("task_status", {}).get(task["id"])
            statuses[task["id"]] = existing if existing in VALID_TASK_STATUSES else "pending"
        state["task_status"] = statuses
        state["current_task_id"] = state.get("current_task_id")
        self.state_store.save(state)
        return {
            "ok": True,
            "iteration": state["current_iteration"],
            "synced_count": len(tasks),
            "task_ids": [task["id"] for task in tasks],
        }

    def next(self):
        state = self.state_store.load()
        tasks = self.load_tasks(state["current_iteration"])
        statuses = state.get("task_status", {})
        blocked = [task for task in tasks if statuses.get(task["id"]) == "blocked"]
        if blocked:
            return {"ok": True, "has_next": True, "task": blocked[0], "blocked": True}

        done = {task_id for task_id, status in statuses.items() if status == "done"}
        for task in tasks:
            task_id = task["id"]
            status = statuses.get(task_id, "pending")
            if status == "ready_for_completion":
                return {"ok": True, "has_next": True, "task": task, "ready_for_completion": True, "blocked": False}
            if status not in ("pending", "in_progress"):
                continue
            deps = set(task.get("dependencies", []))
            if deps.issubset(done):
                if status == "pending":
                    statuses[task_id] = "in_progress"
                    state["task_status"] = statuses
                    state["current_task_id"] = task_id
                    self.state_store.save(state)
                return {"ok": True, "has_next": True, "task": task, "blocked": False}

        all_done = bool(tasks) and all(statuses.get(task["id"]) == "done" for task in tasks)
        return {"ok": True, "has_next": False, "all_done": all_done, "task": None}

    def context(self, task_id):
        state = self.state_store.load()
        task = self.find(task_id, state["current_iteration"])
        return {
            "ok": True,
            "iteration": state["current_iteration"],
            "task_id": task_id,
            "task": task,
            "expected_outputs": task["expected_outputs"],
            "allowed_write_paths": task["allowed_write_paths"],
            "risk_level": task["risk_level"],
            "tdd": task["tdd"],
            "forbidden_write_paths": [
                ".elr/runtime/",
                ".elr/handoffs/",
                ".elr/gates/manifests/",
            ],
        }

    def complete(self, task_id, gate_store=None):
        state = self.state_store.load()
        task = self.find(task_id, state["current_iteration"])
        report_path = self.paths.task_reports / f"iteration_{state['current_iteration']}" / f"{task_id}.json"
        if not report_path.is_file():
            raise StateError("任务报告缺失。", path=str(report_path))
        report = read_json(report_path)
        if report.get("status") != "done":
            raise StateError("任务报告状态不是 done。", status=report.get("status"))
        missing = [rel for rel in task["expected_outputs"] if not (self.paths.root / rel).exists()]
        if missing:
            raise StateError("任务必需产物缺失。", missing=missing)

        if gate_store is not None:
            gate_result = gate_store.run_required_gates(task)
            if not gate_result["ok"]:
                raise StateError("任务门禁未通过。", gate_result=gate_result)
        else:
            gate_result = {"ok": True, "skipped": True}

        state["task_status"][task_id] = "done"
        if state.get("current_task_id") == task_id:
            state["current_task_id"] = None
        self.state_store.save(state)
        return {"ok": True, "task_id": task_id, "gate_result": gate_result}

    def mark_ready_for_completion(self, task_id):
        state = self.state_store.load()
        self.find(task_id, state["current_iteration"])
        state["task_status"][task_id] = "ready_for_completion"
        state["current_task_id"] = task_id
        self.state_store.save(state)
        return {"ok": True, "task_id": task_id, "status": "ready_for_completion"}

    def block(self, task_id, reason):
        state = self.state_store.load()
        self.find(task_id, state["current_iteration"])
        state["task_status"][task_id] = "blocked"
        state["current_task_id"] = task_id
        state["last_error"] = {"code": "TASK_BLOCKED", "message": reason, "task_id": task_id}
        self.state_store.save(state)
        return {"ok": True, "task_id": task_id, "status": "blocked"}

    def find(self, task_id, iteration=None):
        tasks = self.load_tasks(iteration)
        for task in tasks:
            if task["id"] == task_id:
                return task
        raise StateError("任务不存在。", task_id=task_id)

    def write_feedback_task(self, decision):
        state = self.state_store.load()
        feedback_id = decision["id"]
        feedback_path = self.paths.feedback / f"{feedback_id}.json"
        write_json_atomic(feedback_path, decision)
        tasks = self.load_tasks(state["current_iteration"])
        task_id = f"feedback-{feedback_id}"
        if not any(task["id"] == task_id for task in tasks):
            tasks.append({
                "id": task_id,
                "name": f"处理反馈 {feedback_id}",
                "description": decision.get("decision", decision.get("summary", "")),
                "dependencies": [],
                "expected_outputs": [f".elr/task_reports/iteration_{state['current_iteration']}/{task_id}.json"],
                "allowed_write_paths": [".elr/agent_outputs/", ".elr/task_reports/"],
                "risk_level": "low",
                "tdd": {"enabled": False, "required_gates": []},
            })
            write_json_atomic(self.tasks_path(state["current_iteration"]), tasks)
        state["task_status"][task_id] = "pending"
        self.state_store.save(state)
        return {"ok": True, "feedback_path": str(feedback_path), "task_id": task_id}


def validate_tasks(tasks):
    if not isinstance(tasks, list):
        raise StateError("任务文件必须是数组。")
    seen = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise StateError("每个任务必须是对象。")
        missing = REQUIRED_TASK_FIELDS - set(task)
        if missing:
            raise StateError("任务缺少必填字段。", task=task.get("id"), missing=sorted(missing))
        if task["id"] in seen:
            raise StateError("任务 id 重复。", task_id=task["id"])
        seen.add(task["id"])
        if task["risk_level"] not in VALID_RISK_LEVELS:
            raise StateError("risk_level 不合法。", task_id=task["id"], risk_level=task["risk_level"])
        for list_field in ("dependencies", "expected_outputs", "allowed_write_paths"):
            if not isinstance(task[list_field], list):
                raise StateError(f"{list_field} 必须是数组。", task_id=task["id"])
        if not isinstance(task["tdd"], dict):
            raise StateError("tdd 必须是对象。", task_id=task["id"])
