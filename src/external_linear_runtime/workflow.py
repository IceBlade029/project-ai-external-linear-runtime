from .errors import WorkflowError
from .jsonio import read_json


REQUIRED_TURN_FIELDS = {
    "role",
    "to_agent",
    "prompt_template",
    "read_paths",
    "allowed_write_paths",
    "required_outputs",
    "validation_commands",
}


class WorkflowLoader:
    def __init__(self, paths):
        self.paths = paths

    def load(self):
        if not self.paths.workflow.is_file():
            raise WorkflowError("未找到 workflow 文件。", path=str(self.paths.workflow))
        try:
            workflow = read_json(self.paths.workflow)
        except Exception as exc:
            raise WorkflowError("workflow JSON 无法解析。", error=str(exc)) from exc
        validate_workflow(workflow)
        return workflow


def validate_workflow(workflow):
    if not isinstance(workflow, dict):
        raise WorkflowError("workflow 根节点必须是对象。")
    for field in ("schema_version", "initial_phase", "phases"):
        if field not in workflow:
            raise WorkflowError(f"workflow 缺少必填字段: {field}")
    phases = workflow["phases"]
    if not isinstance(phases, list) or not phases:
        raise WorkflowError("workflow phases 必须是非空数组。")

    seen = set()
    for phase in phases:
        if not isinstance(phase, dict):
            raise WorkflowError("每个 phase 必须是对象。")
        phase_id = phase.get("id")
        if not phase_id:
            raise WorkflowError("每个 phase 必须有 id。")
        if phase_id in seen:
            raise WorkflowError("phase id 重复。", phase=phase_id)
        seen.add(phase_id)

        stop = phase.get("stop", "none")
        if stop not in ("none", "human_review", "done"):
            raise WorkflowError("phase stop 策略不合法。", phase=phase_id, stop=stop)
        decisions = phase.get("accepts_human_decisions", [])
        if not isinstance(decisions, list):
            raise WorkflowError("accepts_human_decisions 必须是数组。", phase=phase_id)
        on_decision = phase.get("on_decision", {})
        if not isinstance(on_decision, dict):
            raise WorkflowError("on_decision 必须是对象。", phase=phase_id)
        for decision_type, policy in on_decision.items():
            if not isinstance(policy, dict) or "action" not in policy:
                raise WorkflowError("on_decision policy 必须包含 action。", phase=phase_id, decision_type=decision_type)
            if policy["action"] == "goto" and policy.get("phase") not in seen | {p.get("id") for p in phases if isinstance(p, dict)}:
                raise WorkflowError("on_decision goto phase 不存在。", phase=phase_id, decision_type=decision_type)
        task_loop = phase.get("task_loop", False)
        if not isinstance(task_loop, bool):
            raise WorkflowError("task_loop 必须是 boolean。", phase=phase_id)
        required_gates = phase.get("required_gates", [])
        if not isinstance(required_gates, list):
            raise WorkflowError("required_gates 必须是数组。", phase=phase_id)
        turns = phase.get("turns", [])
        if not isinstance(turns, list):
            raise WorkflowError("turns 必须是数组。", phase=phase_id)
        for turn in turns:
            missing = REQUIRED_TURN_FIELDS - set(turn)
            if missing:
                raise WorkflowError(
                    "turn 缺少必填字段。",
                    phase=phase_id,
                    missing=sorted(missing),
                )
            if turn["to_agent"] not in ("codex", "claude"):
                raise WorkflowError(
                    "turn.to_agent 必须是 codex 或 claude。",
                    phase=phase_id,
                    to_agent=turn["to_agent"],
                )
            for list_field in (
                "read_paths",
                "allowed_write_paths",
                "required_outputs",
                "validation_commands",
            ):
                if not isinstance(turn[list_field], list):
                    raise WorkflowError(
                        f"turn 字段 {list_field} 必须是数组。",
                        phase=phase_id,
                    )
            if "task_binding" in turn and turn["task_binding"] not in ("current", None):
                raise WorkflowError("turn.task_binding 只支持 current。", phase=phase_id)

    if workflow["initial_phase"] not in seen:
        raise WorkflowError("initial_phase 没有匹配的 phase id。")
    for phase in phases:
        next_phase = phase.get("next_phase")
        if next_phase is not None and next_phase not in seen:
            raise WorkflowError("next_phase 没有匹配的 phase id。", phase=phase["id"])


def phase_by_id(workflow, phase_id):
    for phase in workflow["phases"]:
        if phase["id"] == phase_id:
            return phase
    raise WorkflowError("未知 phase。", phase=phase_id)
