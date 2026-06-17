import json
import subprocess

from .adapters import adapter_for
from .decision import DecisionStore
from .decision_policy import DecisionPolicy
from .errors import ELRError, StateError, WorkflowError
from .gates import GateStore
from .handoff import HandoffStore
from .jsonio import write_json_atomic
from .lock import LockManager
from .paths import RuntimePaths
from .state import StateStore, default_state
from .tasks import TaskStore
from .workflow import WorkflowLoader, phase_by_id


STOP_STATUSES = {"waiting_human", "blocked", "done"}


class Runtime:
    def __init__(self, root):
        self.paths = RuntimePaths(root)
        self.workflow_loader = WorkflowLoader(self.paths)
        self.state_store = StateStore(self.paths)
        self.lock_manager = LockManager(self.paths)
        self.handoffs = HandoffStore(self.paths)
        self.decisions = DecisionStore(self.paths)
        self.tasks = TaskStore(self.paths, self.state_store)
        self.gates = GateStore(self.paths, self.state_store)

    def status(self):
        workflow = self.workflow_loader.load()
        state = self.state_store.load()
        phase = phase_by_id(workflow, state["phase"])
        lock = self.lock_manager.read()
        return {
            "ok": True,
            "status": state["status"],
            "phase": state["phase"],
            "phase_turn_index": state["phase_turn_index"],
            "task_id": state.get("task_id"),
            "last_handoff_id": state.get("last_handoff_id"),
            "last_decision_id": state.get("last_decision_id"),
            "last_error": state.get("last_error"),
            "lock": lock,
            "human_waiting": state["status"] == "waiting_human",
            "accepted_human_decisions": phase.get("accepts_human_decisions", []),
            "next_action": self._next_action(workflow, state, phase, lock),
        }

    def plan(self):
        workflow = self.workflow_loader.load()
        state = self.state_store.load()
        state = self._preview_task_loop(workflow, state)
        handoff = self._build_next_handoff(workflow, state)
        return {"ok": True, "handoff": handoff, "will_execute": False}

    def step(self):
        workflow = self.workflow_loader.load()
        state = self.state_store.load()
        prepared = self._prepare_task_loop(workflow, state)
        if isinstance(prepared, dict) and prepared.get("_state_prepared"):
            state = prepared["state"]
        elif prepared is not None:
            return prepared
        else:
            state = self.state_store.load()
        handoff = self._build_next_handoff(workflow, state)

        lock = self.lock_manager.acquire(handoff["to_agent"], handoff["id"])
        state["status"] = "running"
        self.state_store.save(state)

        try:
            prompt = self._render_prompt(handoff)
            result = adapter_for(handoff["to_agent"], self.paths).run(handoff, prompt)
            handoff["agent_result"] = result.to_dict()

            if not result.ok:
                return self._block(state, handoff, "AGENT_EXIT_NONZERO", "worker 命令执行失败。")

            validation = self._validate_handoff(handoff)
            handoff["validation"] = validation
            if not validation["ok"]:
                return self._block(state, handoff, "VALIDATION_FAILED", "turn 验证失败。")
            if handoff.get("gate_phase") and handoff.get("task_id"):
                handoff["gate_seal"] = self.gates.seal(handoff["task_id"], handoff["gate_phase"])

            handoff["status"] = "success"
            self.handoffs.write(handoff)
            next_state = self._advance_after_success(workflow, state, handoff)
            self.state_store.save(next_state)
            return {"ok": True, "handoff": handoff, "state": next_state, "lock": lock}
        except ELRError as exc:
            handoff["status"] = "failed"
            handoff["error"] = {"code": exc.code, "message": exc.message, **exc.details}
            self.handoffs.write(handoff)
            state["status"] = "blocked"
            state["last_error"] = handoff["error"]
            self.state_store.save(state)
            return {"ok": False, "error_code": exc.code, "message": exc.message, "details": exc.details}
        finally:
            self.lock_manager.release()

    def run_until(self, until):
        if until not in ("human_review", "blocked", "done"):
            raise StateError("run --until 的值不合法。", until=until)
        steps = []
        while True:
            state = self.state_store.load()
            if state["status"] in STOP_STATUSES:
                if _status_matches_until(state["status"], until):
                    return {"ok": True, "stopped_at": state["status"], "steps": steps}
                return {"ok": state["status"] != "blocked", "stopped_at": state["status"], "steps": steps}
            result = self.step()
            steps.append(result)
            if not result.get("ok"):
                return {"ok": False, "stopped_at": "blocked", "steps": steps}

    def resume(self):
        state = self.state_store.load()
        if state["status"] == "running":
            state["status"] = "idle"
            state["last_error"] = None
            self.state_store.save(state)
        return self.step()

    def decide(self, file_path):
        workflow = self.workflow_loader.load()
        state = self.state_store.load()
        decision, target = self.decisions.submit(file_path, workflow, state)
        state["last_decision_id"] = decision["id"]
        phase = phase_by_id(workflow, state["phase"])
        state = DecisionPolicy(workflow).apply(state, phase, decision, task_store=self.tasks)
        self.state_store.save(state)
        return {"ok": True, "decision": decision, "stored_at": target, "state": state}

    def _build_next_handoff(self, workflow, state):
        if state["status"] == "waiting_human":
            raise StateError("runtime 正在等待人类 decision。")
        if state["status"] == "blocked":
            raise StateError("runtime 已阻塞。", last_error=state.get("last_error"))
        if state["status"] == "done":
            raise StateError("runtime 已完成。")

        phase = phase_by_id(workflow, state["phase"])
        turns = phase.get("turns", [])
        idx = state["phase_turn_index"]
        if idx >= len(turns):
            raise StateError("当前 phase 没有可执行的 worker turn。")
        turn = turns[idx]
        handoff_id = self.handoffs.next_id(state["phase"], turn["role"])
        handoff = {
            "id": handoff_id,
            "phase": state["phase"],
            "task_id": state.get("task_id"),
            "turn_index": idx,
            "to_agent": turn["to_agent"],
            "role": turn["role"],
            "prompt_template": turn["prompt_template"],
            "read_paths": turn["read_paths"],
            "allowed_write_paths": turn["allowed_write_paths"],
            "required_outputs": turn["required_outputs"],
            "validation_commands": turn["validation_commands"],
            "stop_after_done": bool(turn.get("stop_after_done", False)),
            "timeout_seconds": turn.get("timeout_seconds", 1800),
            **self._handoff_task_fields(turn, state),
        }
        return _format_handoff_paths(handoff)

    def _render_prompt(self, handoff):
        template_path = self.paths.root / handoff["prompt_template"]
        if not template_path.is_file():
            template_path = self.paths.elr / handoff["prompt_template"]
        if not template_path.is_file():
            raise WorkflowError("未找到 prompt 模板。", prompt_template=handoff["prompt_template"])
        template = template_path.read_text(encoding="utf-8")
        return template.format(
            handoff_id=handoff["id"],
            phase=handoff["phase"],
            task_id=handoff.get("task_id") or "",
            role=handoff["role"],
            read_paths="\n".join(handoff["read_paths"]),
            allowed_write_paths="\n".join(handoff["allowed_write_paths"]),
            required_outputs="\n".join(handoff["required_outputs"]),
            task_context=_json_text(handoff.get("task_context", {})),
            risk_level=handoff.get("risk_level") or "",
        )

    def _validate_handoff(self, handoff):
        missing = []
        for rel in handoff["required_outputs"]:
            if not (self.paths.root / rel).exists():
                missing.append(rel)
        command_results = []
        for command in handoff["validation_commands"]:
            result = subprocess.run(
                command,
                shell=True,
                cwd=str(self.paths.root),
                text=True,
                capture_output=True,
                timeout=300,
            )
            command_results.append({
                "command": command,
                "exit_code": result.returncode,
                "stdout": (result.stdout or "")[-2000:],
                "stderr": (result.stderr or "")[-2000:],
            })
        failed_commands = [r for r in command_results if r["exit_code"] != 0]
        return {
            "ok": not missing and not failed_commands,
            "missing_required_outputs": missing,
            "command_results": command_results,
        }

    def _advance_after_success(self, workflow, state, handoff):
        state["run_count"] += 1
        state["last_handoff_id"] = handoff["id"]
        state["last_error"] = None
        state["phase_turn_index"] += 1
        phase = phase_by_id(workflow, state["phase"])
        if handoff["stop_after_done"]:
            state["status"] = "waiting_human"
            return state
        if state["phase_turn_index"] >= len(phase.get("turns", [])):
            if phase.get("task_loop"):
                task_id = state.get("current_task_id")
                if task_id:
                    self.tasks.mark_ready_for_completion(task_id)
                    state = self.state_store.load()
                state["status"] = "idle"
                state["phase_turn_index"] = len(phase.get("turns", []))
                return state
            if phase.get("stop") == "human_review":
                state["status"] = "waiting_human"
                return state
            if phase.get("stop") == "done":
                state["status"] = "done"
                return state
            return self._advance_phase(workflow, state)
        state["status"] = "idle"
        return state

    def _advance_phase(self, workflow, state):
        phase = phase_by_id(workflow, state["phase"])
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

    def _block(self, state, handoff, code, message):
        handoff["status"] = "failed"
        handoff["error"] = {"code": code, "message": message}
        self.handoffs.write(handoff)
        state["status"] = "blocked"
        state["last_handoff_id"] = handoff["id"]
        state["last_error"] = handoff["error"]
        self.state_store.save(state)
        return {"ok": False, "error_code": code, "message": message, "handoff": handoff, "state": state}

    def _next_action(self, workflow, state, phase, lock):
        if lock:
            return "已有 worker turn 正在运行。"
        if state["status"] == "waiting_human":
            return "Codex Coordinator 应收集人类 decision，并通过 elr decide 提交。"
        if state["status"] == "blocked":
            return "Codex Coordinator 应解释阻塞原因，并收集修正 decision。"
        if state["status"] == "done":
            return "workflow 已完成。"
        if phase.get("task_loop") and state.get("current_task_id") and state["phase_turn_index"] >= len(phase.get("turns", [])):
            return f"当前任务 worker turns 已完成，请运行 elr task complete {state['current_task_id']} --json。"
        turns = phase.get("turns", [])
        if state["phase_turn_index"] < len(turns):
            turn = turns[state["phase_turn_index"]]
            return f"运行下一个 worker turn: {turn['to_agent']}:{turn['role']}。"
        return "推进 phase，或等待人类 review。"

    def _prepare_task_loop(self, workflow, state):
        phase = phase_by_id(workflow, state["phase"])
        if not phase.get("task_loop"):
            return None
        turns = phase.get("turns", [])
        if state.get("current_task_id") and state["phase_turn_index"] >= len(turns):
            return None
        if state.get("current_task_id"):
            return None
        next_result = self.tasks.next()
        if not next_result.get("has_next"):
            if next_result.get("all_done"):
                state = self.state_store.load()
                state = self._advance_phase(workflow, state)
                self.state_store.save(state)
                return {"ok": True, "message": "所有任务已完成，已推进到下一 phase。", "state": state}
            raise StateError("当前 execution phase 没有可执行任务。")
        if next_result.get("ready_for_completion"):
            return None
        prepared_state = self.state_store.load()
        task = next_result.get("task")
        if task and not prepared_state.get("current_task_id"):
            prepared_state["current_task_id"] = task["id"]
            prepared_state.setdefault("task_status", {})[task["id"]] = "in_progress"
            self.state_store.save(prepared_state)
            prepared_state = self.state_store.load()
        return {"_state_prepared": True, "state": prepared_state}

    def _preview_task_loop(self, workflow, state):
        phase = phase_by_id(workflow, state["phase"])
        if not phase.get("task_loop") or state.get("current_task_id"):
            return state
        tasks = self.tasks.load_tasks(state["current_iteration"])
        statuses = state.get("task_status", {})
        done = {task_id for task_id, status in statuses.items() if status == "done"}
        preview = dict(state)
        for task in tasks:
            status = statuses.get(task["id"], "pending")
            if status in ("pending", "in_progress") and set(task.get("dependencies", [])).issubset(done):
                preview["current_task_id"] = task["id"]
                return preview
        return state

    def _handoff_task_fields(self, turn, state):
        if turn.get("task_binding") != "current":
            return {}
        task_id = state.get("current_task_id")
        if not task_id:
            raise StateError("当前 turn 需要 current task，但 state.current_task_id 为空。")
        context = self.tasks.context(task_id)
        task = context["task"]
        return {
            "task_context": context,
            "gate_phase": turn.get("gate_phase"),
            "risk_level": task.get("risk_level"),
            "decision_policy": turn.get("decision_policy"),
            "task_id": task_id,
        }


def init_project(root, force=False, profile="product_tdd"):
    import shutil

    paths = RuntimePaths(root)
    if paths.elr.exists() and not force:
        raise StateError(".elr 已存在。使用 --force 重新初始化。")
    if paths.elr.exists() and force:
        shutil.rmtree(paths.elr)
    paths.ensure()
    workflow = _workflow_for_profile(profile)
    write_json_atomic(paths.workflow, workflow)
    StateStore(paths).save(default_state(workflow))
    _write_default_templates(paths)
    _write_sample_tasks(paths, workflow)
    return {"ok": True, "elr_path": str(paths.elr), "workflow": str(paths.workflow), "profile": profile}


def doctor(root):
    import shutil

    paths = RuntimePaths(root)
    checks = {
        "elr_initialized": paths.elr.is_dir(),
        "workflow_exists": paths.workflow.is_file(),
        "state_exists": paths.state.is_file(),
        "codex": shutil.which("codex"),
        "claude": shutil.which("claude"),
        "git": shutil.which("git"),
    }
    workflow_ok = False
    workflow_error = None
    if paths.workflow.is_file():
        try:
            WorkflowLoader(paths).load()
            workflow_ok = True
        except ELRError as exc:
            workflow_error = {"code": exc.code, "message": exc.message, **exc.details}
    checks["workflow_valid"] = workflow_ok
    checks["workflow_error"] = workflow_error
    return {"ok": checks["elr_initialized"] and checks["workflow_valid"], "checks": checks}


def _status_matches_until(status, until):
    return (
        (until == "human_review" and status == "waiting_human")
        or (until == "blocked" and status == "blocked")
        or (until == "done" and status == "done")
    )


def _workflow_for_profile(profile):
    if profile == "minimal":
        return _minimal_workflow()
    if profile == "product_tdd":
        return _product_tdd_workflow()
    raise StateError("未知 init profile。", profile=profile)


def _minimal_workflow():
    return {
        "schema_version": "1.0.0",
        "name": "minimal-external-linear-runtime-workflow",
        "initial_phase": "codex_planning",
        "phases": [
            {
                "id": "codex_planning",
                "stop": "human_review",
                "accepts_human_decisions": ["approval", "rejection", "scope_change", "clarification"],
                "next_phase": "claude_implementation",
                "turns": [
                    {
                        "role": "planner",
                        "to_agent": "codex",
                        "prompt_template": "templates/codex/planner.md",
                        "read_paths": ["."],
                        "allowed_write_paths": [".elr/agent_outputs/planning.md"],
                        "required_outputs": [".elr/agent_outputs/planning.md"],
                        "validation_commands": [],
                        "stop_after_done": False,
                    }
                ],
            },
            {
                "id": "claude_implementation",
                "stop": "human_review",
                "accepts_human_decisions": ["approval", "rejection", "polishing_feedback", "clarification"],
                "next_phase": "codex_final_review",
                "turns": [
                    {
                        "role": "implementer",
                        "to_agent": "claude",
                        "prompt_template": "templates/claude/implementer.md",
                        "read_paths": [".elr/agent_outputs/planning.md"],
                        "allowed_write_paths": [".elr/agent_outputs/implementation.md"],
                        "required_outputs": [".elr/agent_outputs/implementation.md"],
                        "validation_commands": [],
                        "stop_after_done": False,
                    }
                ],
            },
            {
                "id": "codex_final_review",
                "stop": "done",
                "accepts_human_decisions": [],
                "next_phase": None,
                "turns": [
                    {
                        "role": "final_auditor",
                        "to_agent": "codex",
                        "prompt_template": "templates/codex/final_auditor.md",
                        "read_paths": [".elr/agent_outputs/implementation.md"],
                        "allowed_write_paths": [".elr/agent_outputs/final_review.md"],
                        "required_outputs": [".elr/agent_outputs/final_review.md"],
                        "validation_commands": [],
                        "stop_after_done": False,
                    }
                ],
            },
        ],
    }


def _product_tdd_workflow():
    return {
        "schema_version": "1.0.0",
        "name": "product-tdd-workflow",
        "initial_phase": "product_discovery",
        "phases": [
            {
                "id": "product_discovery",
                "stop": "human_review",
                "accepts_human_decisions": ["approval", "rejection", "clarification", "scope_change"],
                "on_decision": {
                    "approval": {"action": "next_phase"},
                    "clarification": {"action": "resume"},
                    "scope_change": {"action": "resume"},
                    "rejection": {"action": "block"},
                },
                "next_phase": "planning",
                "turns": [
                    {
                        "role": "product_discovery",
                        "to_agent": "codex",
                        "prompt_template": "templates/codex/product_discovery.md",
                        "read_paths": ["."],
                        "allowed_write_paths": [".elr/plans/product_vision.md"],
                        "required_outputs": [".elr/plans/product_vision.md"],
                        "validation_commands": [],
                        "stop_after_done": False,
                    }
                ],
            },
            {
                "id": "planning",
                "stop": "human_review",
                "accepts_human_decisions": ["approval", "rejection", "scope_change", "clarification"],
                "on_decision": {
                    "approval": {"action": "next_phase"},
                    "scope_change": {"action": "resume"},
                    "clarification": {"action": "resume"},
                    "rejection": {"action": "block"},
                },
                "next_phase": "execution",
                "turns": [
                    {
                        "role": "iteration_planner",
                        "to_agent": "codex",
                        "prompt_template": "templates/codex/iteration_planner.md",
                        "read_paths": [".elr/plans/product_vision.md"],
                        "allowed_write_paths": [".elr/plans/", ".elr/tasks/", ".elr/specs/"],
                        "required_outputs": [
                            ".elr/plans/iteration_plan.md",
                            ".elr/tasks/iteration_1_tasks.json",
                        ],
                        "validation_commands": [],
                        "stop_after_done": False,
                    }
                ],
            },
            {
                "id": "execution",
                "stop": "none",
                "task_loop": True,
                "accepts_human_decisions": ["clarification", "scope_change"],
                "on_decision": {
                    "clarification": {"action": "resume"},
                    "scope_change": {"action": "goto", "phase": "planning"},
                },
                "next_phase": "iteration_review",
                "turns": [
                    {
                        "role": "test_writer",
                        "to_agent": "claude",
                        "prompt_template": "templates/claude/test_writer.md",
                        "task_binding": "current",
                        "gate_phase": "test_writer",
                        "read_paths": [".elr/specs/", ".elr/plans/"],
                        "allowed_write_paths": [".elr/tdd/coverage/"],
                        "required_outputs": [".elr/tdd/coverage/{task_id}.md"],
                        "validation_commands": [],
                        "stop_after_done": False,
                    },
                    {
                        "role": "test_reviewer",
                        "to_agent": "codex",
                        "prompt_template": "templates/codex/reviewer.md",
                        "task_binding": "current",
                        "gate_phase": "test_reviewer",
                        "read_paths": [".elr/tdd/coverage/", ".elr/specs/"],
                        "allowed_write_paths": [".elr/tdd/reviews/"],
                        "required_outputs": [".elr/tdd/reviews/{task_id}.md"],
                        "validation_commands": [],
                        "stop_after_done": False,
                    },
                    {
                        "role": "probe_designer",
                        "to_agent": "codex",
                        "prompt_template": "templates/codex/probe_designer.md",
                        "task_binding": "current",
                        "gate_phase": "probe_designer",
                        "read_paths": [".elr/tdd/reviews/", ".elr/specs/"],
                        "allowed_write_paths": [".elr/tdd/probe-plans/"],
                        "required_outputs": [".elr/tdd/probe-plans/{task_id}.md"],
                        "validation_commands": [],
                        "stop_after_done": False,
                    },
                    {
                        "role": "probe_executor",
                        "to_agent": "claude",
                        "prompt_template": "templates/claude/probe_executor.md",
                        "task_binding": "current",
                        "gate_phase": "probe_executor",
                        "read_paths": [".elr/tdd/probe-plans/"],
                        "allowed_write_paths": [".elr/tdd/probe-results/"],
                        "required_outputs": [".elr/tdd/probe-results/{task_id}.md"],
                        "validation_commands": [],
                        "stop_after_done": False,
                    },
                    {
                        "role": "approval_decider",
                        "to_agent": "codex",
                        "prompt_template": "templates/codex/approval_decider.md",
                        "task_binding": "current",
                        "gate_phase": "approval_decider",
                        "read_paths": [".elr/tdd/reviews/", ".elr/tdd/probe-results/"],
                        "allowed_write_paths": [".elr/tdd/approvals/"],
                        "required_outputs": [".elr/tdd/approvals/{task_id}.md"],
                        "validation_commands": [],
                        "stop_after_done": False,
                    },
                    {
                        "role": "implementer",
                        "to_agent": "claude",
                        "prompt_template": "templates/claude/implementer.md",
                        "task_binding": "current",
                        "gate_phase": "implementer",
                        "read_paths": [".elr/tdd/approvals/", ".elr/specs/"],
                        "allowed_write_paths": ["src/", ".elr/task_reports/"],
                        "required_outputs": [".elr/task_reports/{task_id}.implementation.md"],
                        "validation_commands": [],
                        "stop_after_done": False,
                    },
                    {
                        "role": "spec_compliance",
                        "to_agent": "codex",
                        "prompt_template": "templates/codex/spec_compliance.md",
                        "task_binding": "current",
                        "gate_phase": "spec_compliance",
                        "read_paths": [".elr/specs/", ".elr/task_reports/"],
                        "allowed_write_paths": [".elr/tdd/spec-compliance/"],
                        "required_outputs": [".elr/tdd/spec-compliance/{task_id}.md"],
                        "validation_commands": [],
                        "stop_after_done": False,
                    },
                ],
            },
            {
                "id": "iteration_review",
                "stop": "human_review",
                "accepts_human_decisions": ["approval", "rejection", "clarification"],
                "on_decision": {"approval": {"action": "next_phase"}, "clarification": {"action": "resume"}, "rejection": {"action": "block"}},
                "next_phase": "iteration_polishing",
                "turns": [
                    {
                        "role": "final_auditor",
                        "to_agent": "codex",
                        "prompt_template": "templates/codex/final_auditor.md",
                        "read_paths": [".elr/task_reports/", ".elr/tdd/"],
                        "allowed_write_paths": [".elr/iteration_reports/"],
                        "required_outputs": [".elr/iteration_reports/iteration_review.md"],
                        "validation_commands": [],
                        "stop_after_done": False,
                    }
                ],
            },
            {
                "id": "iteration_polishing",
                "stop": "human_review",
                "accepts_human_decisions": ["approval", "polishing_feedback", "scope_change", "clarification"],
                "on_decision": {
                    "approval": {"action": "next_phase"},
                    "polishing_feedback": {"action": "feedback_task", "phase": "execution"},
                    "scope_change": {"action": "goto", "phase": "planning"},
                    "clarification": {"action": "resume"},
                },
                "next_phase": "backlog_update",
                "turns": [
                    {
                        "role": "reviewer",
                        "to_agent": "codex",
                        "prompt_template": "templates/codex/reviewer.md",
                        "read_paths": [".elr/iteration_reports/", ".elr/feedback/"],
                        "allowed_write_paths": [".elr/iteration_reports/polishing_review.md"],
                        "required_outputs": [".elr/iteration_reports/polishing_review.md"],
                        "validation_commands": [],
                        "stop_after_done": False,
                    }
                ],
            },
            {
                "id": "backlog_update",
                "stop": "human_review",
                "accepts_human_decisions": ["approval", "scope_change", "polishing_feedback", "clarification"],
                "on_decision": {
                    "approval": {"action": "complete"},
                    "scope_change": {"action": "goto", "phase": "planning"},
                    "polishing_feedback": {"action": "feedback_task", "phase": "execution"},
                    "clarification": {"action": "resume"},
                },
                "next_phase": "done",
                "turns": [],
            },
            {
                "id": "done",
                "stop": "done",
                "accepts_human_decisions": [],
                "next_phase": None,
                "turns": [],
            },
        ],
    }


def _json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def _format_handoff_paths(handoff):
    task_id = handoff.get("task_id") or ""
    for field in ("read_paths", "allowed_write_paths", "required_outputs", "validation_commands"):
        handoff[field] = [str(item).format(task_id=task_id) for item in handoff[field]]
    return handoff


def _write_sample_tasks(paths, workflow):
    if workflow.get("name") != "product-tdd-workflow":
        return
    sample = [
        {
            "id": "I1_T01",
            "name": "示例 TDD 任务",
            "description": "用 fake worker 或真实 worker 完成一个可门禁的示例任务。",
            "dependencies": [],
            "expected_outputs": [".elr/task_reports/iteration_1/I1_T01.json"],
            "allowed_write_paths": ["src/", ".elr/agent_outputs/", ".elr/task_reports/"],
            "risk_level": "medium",
            "tdd": {
                "enabled": True,
                "required_gates": ["integrity", "validation"],
                "validation_commands": [],
            },
        }
    ]
    path = paths.tasks / "iteration_1_tasks.json"
    if not path.exists():
        write_json_atomic(path, sample)


def _write_default_templates(paths):
    templates = {
        paths.templates / "codex" / "planner.md": (
            "# Codex 规划员\n\n"
            "Handoff: {handoff_id}\nPhase: {phase}\nRole: {role}\n\n"
            "## 输入\n\n"
            "读取路径:\n{read_paths}\n\n"
            "你必须阅读这些路径中的真实文件，不要只根据 handoff 摘要推测。\n\n"
            "## 写入边界\n\n"
            "只允许写入:\n{allowed_write_paths}\n\n"
            "不得写入 runtime 权威文件，不得创建未列入允许范围的业务代码。\n\n"
            "## 最终产物\n\n"
            "必需产物:\n{required_outputs}\n\n"
            "产物必须包含：目标摘要、范围、非目标、建议 phase/turn、风险、需要人类确认的问题。\n\n"
            "## 工作步骤\n\n"
            "1. 读取输入路径和已有 runtime 产物。\n"
            "2. 将人类目标整理成可执行计划。\n"
            "3. 明确哪些判断需要人类确认，哪些可以交给 worker 执行。\n"
            "4. 写入必需产物。\n\n"
            "## 禁止事项\n\n"
            "- 不要推进 runtime 状态。\n"
            "- 不要调用 Claude 或 Codex worker。\n"
            "- 不要修改 `.elr/runtime/*` 或 `.elr/handoffs/*`。\n"
            "- 不要把未确认的人类偏好写成事实。\n"
        ),
        paths.templates / "codex" / "product_discovery.md": (
            "# Codex 产品发现员\n\n"
            "Handoff: {handoff_id}\nPhase: {phase}\nRole: {role}\n\n"
            "## 输入\n\n"
            "读取路径:\n{read_paths}\n\n"
            "## 写入边界\n\n"
            "只允许写入:\n{allowed_write_paths}\n\n"
            "## 最终产物\n\n"
            "必需产物:\n{required_outputs}\n\n"
            "产物必须包含：产品目标、目标用户、核心场景、MVP 边界、风险、需要人类确认的问题。\n\n"
            "## 工作步骤\n\n"
            "1. 从人类需求、README、现有文件中提炼产品意图。\n"
            "2. 区分已确认事实和待确认假设。\n"
            "3. 写入产品愿景产物。\n\n"
            "## 禁止事项\n\n"
            "- 不要生成实现代码。\n"
            "- 不要推进 runtime 状态。\n"
            "- 不要替人类确认产品范围。\n"
        ),
        paths.templates / "codex" / "iteration_planner.md": (
            "# Codex 迭代规划员\n\n"
            "Handoff: {handoff_id}\nPhase: {phase}\nRole: {role}\n\n"
            "## 输入\n\n"
            "读取路径:\n{read_paths}\n\n"
            "## 写入边界\n\n"
            "只允许写入:\n{allowed_write_paths}\n\n"
            "## 最终产物\n\n"
            "必需产物:\n{required_outputs}\n\n"
            "产物必须包含：迭代目标、任务列表、依赖、allowed_write_paths、risk_level、TDD 配置。\n\n"
            "## 工作步骤\n\n"
            "1. 基于产品愿景和已有报告拆分当前迭代。\n"
            "2. 为每个任务写清 expected_outputs、allowed_write_paths 和 tdd 字段。\n"
            "3. 同步生成必要的 specs/rules 草案。\n\n"
            "## 禁止事项\n\n"
            "- 不要为未来多轮生成详细任务。\n"
            "- 不要把不清楚的范围写成已确认。\n"
            "- 不要推进 runtime 状态。\n"
        ),
        paths.templates / "codex" / "bdd_spec_writer.md": (
            "# Codex BDD/Spec 编写员\n\n"
            "Handoff: {handoff_id}\nPhase: {phase}\nRole: {role}\n\n"
            "读取路径:\n{read_paths}\n\n"
            "只允许写入:\n{allowed_write_paths}\n\n"
            "必需产物:\n{required_outputs}\n\n"
            "产物必须包含：可测试场景、规则表、边界条件、未澄清问题。\n\n"
            "## 禁止事项\n\n"
            "- 不要写实现代码。\n"
            "- 不要生成不可验证的主观需求。\n"
            "- 不要推进 runtime 状态。\n"
        ),
        paths.templates / "codex" / "reviewer.md": (
            "# Codex 审查员\n\n"
            "Handoff: {handoff_id}\nPhase: {phase}\nRole: {role}\n\n"
            "## 输入\n\n"
            "读取路径:\n{read_paths}\n\n"
            "你必须检查真实文件、worker 产物和日志；不要只相信实现报告。\n\n"
            "## 写入边界\n\n"
            "只允许写入:\n{allowed_write_paths}\n\n"
            "不得修改被审查的实现文件。\n\n"
            "## 最终产物\n\n"
            "必需产物:\n{required_outputs}\n\n"
            "产物必须包含：结论、发现列表、证据路径、风险等级、是否建议进入人类确认。\n\n"
            "## 工作步骤\n\n"
            "1. 阅读输入与实际变更。\n"
            "2. 按正确性、范围、可维护性、测试充分性审查。\n"
            "3. 用文件路径或日志片段支撑每个发现。\n"
            "4. 写入必需产物。\n\n"
            "## 禁止事项\n\n"
            "- 不要修改实现、测试或 runtime 状态。\n"
            "- 不要用“看起来可以”替代证据。\n"
            "- 不要替 runtime 决定下一步调度。\n"
        ),
        paths.templates / "codex" / "spec_compliance.md": (
            "# Codex Spec 合规裁判\n\n"
            "Handoff: {handoff_id}\nPhase: {phase}\nRole: {role}\n\n"
            "## 输入\n\n"
            "读取路径:\n{read_paths}\n\n"
            "spec、规则、计划和实现产物都必须以文件为准。\n\n"
            "## 写入边界\n\n"
            "只允许写入:\n{allowed_write_paths}\n\n"
            "## 最终产物\n\n"
            "必需产物:\n{required_outputs}\n\n"
            "产物必须包含：`COMPLIANT` 或 `ISSUES_FOUND` 结论、缺失项、误解项、额外实现项、spec 到代码/产物的追踪说明。\n\n"
            "## 工作步骤\n\n"
            "1. 提取 spec 中的可验证要求。\n"
            "2. 对照真实实现或 worker 产物逐项核查。\n"
            "3. 区分缺失、误解、额外工作和证据不足。\n"
            "4. 写入必需产物。\n\n"
            "## 禁止事项\n\n"
            "- 不要修改 spec 或实现。\n"
            "- 不要因测试通过就直接判定合规。\n"
            "- 不要推进 runtime 状态。\n"
        ),
        paths.templates / "codex" / "probe_designer.md": (
            "# Codex Probe 设计员\n\n"
            "Handoff: {handoff_id}\nPhase: {phase}\nRole: {role}\n\n"
            "## 输入\n\n"
            "读取路径:\n{read_paths}\n\n"
            "重点阅读 spec、测试说明、已有 review 和风险说明。\n\n"
            "## 写入边界\n\n"
            "只允许写入:\n{allowed_write_paths}\n\n"
            "不得向业务代码注入 probe；执行由 Claude worker 完成。\n\n"
            "## 最终产物\n\n"
            "必需产物:\n{required_outputs}\n\n"
            "产物必须包含：至少 3 个 probe、每个 probe 的假实现策略、预期测试结果、恢复要求、判定标准。\n\n"
            "## 工作步骤\n\n"
            "1. 找出最容易被弱测试放过的错误实现。\n"
            "2. 设计可由 Claude 临时执行并回滚的 probe。\n"
            "3. 明确每个 probe 的 KILLED/SURVIVED 判定。\n"
            "4. 写入必需产物。\n\n"
            "## 禁止事项\n\n"
            "- 不要自己修改 src/tests。\n"
            "- 不要把 probe 结果伪装成已执行。\n"
            "- 不要推进 runtime 状态。\n"
        ),
        paths.templates / "codex" / "approval_decider.md": (
            "# Codex 测试批准裁判\n\n"
            "Handoff: {handoff_id}\nPhase: {phase}\nRole: {role}\nRisk: {risk_level}\n\n"
            "## 输入\n\n"
            "读取路径:\n{read_paths}\n\n"
            "## 写入边界\n\n"
            "只允许写入:\n{allowed_write_paths}\n\n"
            "## 最终产物\n\n"
            "必需产物:\n{required_outputs}\n\n"
            "产物必须包含：批准/拒绝结论、测试覆盖证据、probe 统计、实现者第一条应运行的命令。\n\n"
            "## 工作步骤\n\n"
            "1. 阅读测试审查和 probe 执行结果。\n"
            "2. 确认 risk>=medium 时 probe 至少 3 个且全部 KILLED。\n"
            "3. 只有测试可信时才写 approval 产物。\n\n"
            "## 禁止事项\n\n"
            "- 不要执行代码注入。\n"
            "- 不要在有 SURVIVED probe 时批准。\n"
            "- 不要推进 runtime 状态。\n"
        ),
        paths.templates / "codex" / "final_auditor.md": (
            "# Codex 最终审计员\n\n"
            "Handoff: {handoff_id}\nPhase: {phase}\nRole: {role}\n\n"
            "## 输入\n\n"
            "读取路径:\n{read_paths}\n\n"
            "必须阅读真实产物、handoff、worker 输出和验证结果。\n\n"
            "## 写入边界\n\n"
            "只允许写入:\n{allowed_write_paths}\n\n"
            "## 最终产物\n\n"
            "必需产物:\n{required_outputs}\n\n"
            "产物必须包含：最终结论、已验证内容、遗留风险、建议给人类的下一步说明。\n\n"
            "## 工作步骤\n\n"
            "1. 核对所有必需 worker 产物是否存在且内容一致。\n"
            "2. 检查失败日志、blocked 记录或未处理问题。\n"
            "3. 总结面向人类的最终状态。\n"
            "4. 写入必需产物。\n\n"
            "## 禁止事项\n\n"
            "- 不要补写其他 worker 应产生的报告。\n"
            "- 不要忽略未解决的 blocker。\n"
            "- 不要推进 runtime 状态。\n"
        ),
        paths.templates / "claude" / "implementer.md": (
            "# Claude 实现员\n\n"
            "Handoff: {handoff_id}\nPhase: {phase}\nRole: {role}\n\n"
            "## 输入\n\n"
            "读取路径:\n{read_paths}\n\n"
            "只根据 handoff 和读取路径中的文件执行，不从人类聊天中直接接收新范围。\n\n"
            "## 写入边界\n\n"
            "只允许写入:\n{allowed_write_paths}\n\n"
            "如果需要写入范围外文件，停止并写 blocker 报告，不要擅自扩大范围。\n\n"
            "## 最终产物\n\n"
            "必需产物:\n{required_outputs}\n\n"
            "产物必须包含：完成内容、修改文件、运行过的检查、未运行检查及原因、遗留 blocker。\n\n"
            "## 执行要求\n\n"
            "1. 先读输入，再动手。\n"
            "2. 只实现当前 handoff 要求的内容。\n"
            "3. 尽量运行 handoff 或项目要求的验证命令。\n"
            "4. 写入必需产物。\n\n"
            "## blocker 报告要求\n\n"
            "无法完成时，必需产物中必须写明：阻塞原因、需要的人类决定、已尝试动作、不能继续的风险。\n\n"
            "## 禁止事项\n\n"
            "- 不得充当流程主控。\n"
            "- 不得调用 Codex 或其他 Claude 流程。\n"
            "- 不得修改 `.elr/runtime/*` 或 `.elr/handoffs/*`。\n"
            "- 不得跳过边界限制。\n"
        ),
        paths.templates / "claude" / "test_writer.md": (
            "# Claude 测试编写员\n\n"
            "Handoff: {handoff_id}\nPhase: {phase}\nRole: {role}\n\n"
            "## 输入\n\n"
            "读取路径:\n{read_paths}\n\n"
            "测试依据来自 spec、计划和 handoff，不来自当前实现的偶然行为。\n\n"
            "## 写入边界\n\n"
            "只允许写入:\n{allowed_write_paths}\n\n"
            "## 最终产物\n\n"
            "必需产物:\n{required_outputs}\n\n"
            "产物必须包含：测试范围、覆盖规则、红灯/失败预期、运行命令、无法覆盖的点。\n\n"
            "## 执行要求\n\n"
            "1. 为核心规则写可观察行为测试或测试计划。\n"
            "2. 覆盖 happy path、失败路径和边界条件。\n"
            "3. 记录测试命令和结果。\n"
            "4. 写入必需产物。\n\n"
            "## blocker 报告要求\n\n"
            "如果 spec 不清或 API 不足，写明缺口和需要人类/Coordinator 澄清的问题。\n\n"
            "## 禁止事项\n\n"
            "- 不要实现生产代码。\n"
            "- 不要推进 runtime 状态。\n"
            "- 不要把弱断言包装成完整覆盖。\n"
        ),
        paths.templates / "claude" / "probe_executor.md": (
            "# Claude Probe 执行员\n\n"
            "Handoff: {handoff_id}\nPhase: {phase}\nRole: {role}\n\n"
            "## 输入\n\n"
            "读取路径:\n{read_paths}\n\n"
            "必须按 Codex probe plan 执行，不自行改变 probe 目标。\n\n"
            "## 写入边界\n\n"
            "只允许写入:\n{allowed_write_paths}\n\n"
            "临时注入代码必须在每个 probe 后恢复；最终只保留允许路径内的报告。\n\n"
            "## 最终产物\n\n"
            "必需产物:\n{required_outputs}\n\n"
            "产物必须包含：每个 probe 的注入方式、测试命令、实际结果、KILLED/SURVIVED 判定、恢复确认。\n\n"
            "## 执行要求\n\n"
            "1. 逐个执行 probe。\n"
            "2. 每次执行后恢复临时修改。\n"
            "3. 记录命令输出和判定。\n"
            "4. 写入必需产物。\n\n"
            "## blocker 报告要求\n\n"
            "如果无法安全恢复或测试命令不可用，立即停止并报告。\n\n"
            "## 禁止事项\n\n"
            "- 不要留下临时代码。\n"
            "- 不要生成 approval。\n"
            "- 不要推进 runtime 状态。\n"
        ),
        paths.templates / "claude" / "fixer.md": (
            "# Claude 修复员\n\n"
            "Handoff: {handoff_id}\nPhase: {phase}\nRole: {role}\n\n"
            "## 输入\n\n"
            "读取路径:\n{read_paths}\n\n"
            "只修复 handoff 或 review 指明的问题。\n\n"
            "## 写入边界\n\n"
            "只允许写入:\n{allowed_write_paths}\n\n"
            "## 最终产物\n\n"
            "必需产物:\n{required_outputs}\n\n"
            "产物必须包含：修复项、修改文件、验证结果、未修复项和原因。\n\n"
            "## 执行要求\n\n"
            "1. 逐条对应 review/blocker 修复。\n"
            "2. 避免额外重构和范围膨胀。\n"
            "3. 运行可行验证。\n"
            "4. 写入必需产物。\n\n"
            "## blocker 报告要求\n\n"
            "如果修复需要越界文件或需求变更，写明原因并停止。\n\n"
            "## 禁止事项\n\n"
            "- 不要修改未授权文件。\n"
            "- 不要绕过测试或验证。\n"
            "- 不要推进 runtime 状态。\n"
        ),
        paths.templates / "claude" / "runner.md": (
            "# Claude 运行员\n\n"
            "Handoff: {handoff_id}\nPhase: {phase}\nRole: {role}\n\n"
            "## 输入\n\n"
            "读取路径:\n{read_paths}\n\n"
            "根据 handoff 中的检查目标运行命令或人工验证步骤。\n\n"
            "## 写入边界\n\n"
            "只允许写入:\n{allowed_write_paths}\n\n"
            "## 最终产物\n\n"
            "必需产物:\n{required_outputs}\n\n"
            "产物必须包含：执行命令、退出码、关键输出、失败详情、是否需要 Coordinator 介入。\n\n"
            "## 执行要求\n\n"
            "1. 按要求运行检查。\n"
            "2. 不美化失败结果。\n"
            "3. 记录可复现命令。\n"
            "4. 写入必需产物。\n\n"
            "## blocker 报告要求\n\n"
            "如果环境缺失、命令不可用或结果不稳定，写明复现条件和下一步需要。\n\n"
            "## 禁止事项\n\n"
            "- 不要修代码，除非 handoff 明确授权。\n"
            "- 不要推进 runtime 状态。\n"
            "- 不要把未运行的检查写成通过。\n"
        ),
        paths.templates / "claude" / "e2e_verifier.md": (
            "# Claude E2E 验证员\n\n"
            "Handoff: {handoff_id}\nPhase: {phase}\nRole: {role}\nRisk: {risk_level}\n\n"
            "## 输入\n\n"
            "读取路径:\n{read_paths}\n\n"
            "## 写入边界\n\n"
            "只允许写入:\n{allowed_write_paths}\n\n"
            "## 最终产物\n\n"
            "必需产物:\n{required_outputs}\n\n"
            "产物必须包含：场景、执行步骤、结果、失败截图或日志路径、是否全部 PASS。\n\n"
            "## 执行要求\n\n"
            "1. 从真实用户入口验证。\n"
            "2. 使用真实交互，不直接改内部状态。\n"
            "3. 覆盖成功、失败、空状态和刷新后的状态。\n\n"
            "## blocker 报告要求\n\n"
            "环境无法启动或 E2E 工具缺失时，报告阻塞原因和复现命令。\n\n"
            "## 禁止事项\n\n"
            "- 不要伪造 PASS。\n"
            "- 不要推进 runtime 状态。\n"
        ),
    }
    for path, content in templates.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(content, encoding="utf-8")
