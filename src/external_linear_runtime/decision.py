from .errors import DecisionError
from .jsonio import read_json, write_json_atomic
from .state import now_iso
from .workflow import phase_by_id


REQUIRED_DECISION_FIELDS = {
    "id",
    "type",
    "phase",
    "summary",
    "decision",
    "created_by",
    "source",
}
VALID_DECISION_TYPES = {
    "approval",
    "rejection",
    "scope_change",
    "polishing_feedback",
    "clarification",
}


class DecisionStore:
    def __init__(self, paths):
        self.paths = paths

    def submit(self, file_path, workflow, state):
        try:
            decision = read_json(file_path)
        except Exception as exc:
            raise DecisionError("decision JSON 无法解析。", error=str(exc)) from exc
        self._validate(decision, workflow, state)
        decision = dict(decision)
        decision["received_at"] = now_iso()
        target = self.paths.human_decisions / f"{decision['id']}.json"
        write_json_atomic(target, decision)
        return decision, str(target)

    def _validate(self, decision, workflow, state):
        if not isinstance(decision, dict):
            raise DecisionError("decision 根节点必须是对象。")
        missing = REQUIRED_DECISION_FIELDS - set(decision)
        if missing:
            raise DecisionError("decision 缺少必填字段。", missing=sorted(missing))
        if decision["type"] not in VALID_DECISION_TYPES:
            raise DecisionError("decision type 不合法。", type=decision["type"])
        if decision["created_by"] != "codex_coordinator":
            raise DecisionError("decision 必须由 codex_coordinator 创建。")
        if decision["source"] != "human_conversation":
            raise DecisionError("decision source 必须是 human_conversation。")
        if decision["phase"] != state["phase"]:
            raise DecisionError(
                "decision.phase 与 runtime 当前 phase 不一致。",
                decision_phase=decision["phase"],
                state_phase=state["phase"],
            )
        if state["status"] != "waiting_human":
            raise DecisionError("runtime 当前没有在等待人类 decision。", status=state["status"])

        phase = phase_by_id(workflow, state["phase"])
        accepted = phase.get("accepts_human_decisions", [])
        if decision["type"] not in accepted:
            raise DecisionError(
                "当前 phase 不接受这个 decision type。",
                type=decision["type"],
                accepted=accepted,
            )
