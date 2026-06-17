from .errors import DecisionError


class DecisionPolicy:
    def __init__(self, workflow):
        self.workflow = workflow

    def apply(self, state, phase, decision, task_store=None):
        policies = phase.get("on_decision", {})
        policy = policies.get(decision["type"])
        if policy is None:
            if decision["type"] == "approval":
                policy = {"action": "next_phase"}
            else:
                policy = {"action": "block"}

        action = policy.get("action")
        if action == "next_phase":
            return _next_phase(state, phase)
        if action == "goto":
            state["phase"] = policy["phase"]
            state["phase_turn_index"] = 0
            state["status"] = "idle"
            state["last_error"] = None
            return state
        if action == "resume":
            state["status"] = "idle"
            state["last_error"] = None
            return state
        if action == "complete":
            state["status"] = "done"
            state["phase_turn_index"] = 0
            return state
        if action == "feedback_task":
            if task_store is None:
                raise DecisionError("feedback_task 需要 TaskStore。")
            task_store.write_feedback_task(decision)
            state["phase"] = policy.get("phase", "execution")
            state["phase_turn_index"] = 0
            state["status"] = "idle"
            return state

        state["status"] = "blocked"
        state["last_error"] = {
            "code": "HUMAN_DECISION_REQUIRES_COORDINATOR",
            "message": "该人类 decision 需要 Codex Coordinator 处理后 runtime 才能继续。",
            "decision_id": decision["id"],
            "decision_type": decision["type"],
        }
        return state


def _next_phase(state, phase):
    next_phase = phase.get("next_phase")
    if not next_phase:
        state["status"] = "done"
        state["phase_turn_index"] = 0
        return state
    state["phase"] = next_phase
    state["phase_turn_index"] = 0
    state["status"] = "idle"
    state["last_error"] = None
    return state
