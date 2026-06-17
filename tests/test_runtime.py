import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from external_linear_runtime.cli import main
from external_linear_runtime.config import AUTONOMOUS_CLAUDE_COMMAND, ConfigStore
from external_linear_runtime.errors import LockError, WorkflowError
from external_linear_runtime.gates import GateStore
from external_linear_runtime.jsonio import write_json_atomic
from external_linear_runtime.lock import LockManager
from external_linear_runtime.paths import RuntimePaths
from external_linear_runtime.runner import Runtime, init_project
from external_linear_runtime.state import StateStore
from external_linear_runtime.tasks import TaskStore
from external_linear_runtime.workflow import WorkflowLoader


class RuntimeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_cwd = os.getcwd()
        os.chdir(self.root)
        self.old_env = os.environ.copy()

    def tearDown(self):
        os.chdir(self.old_cwd)
        os.environ.clear()
        os.environ.update(self.old_env)
        self.tmp.cleanup()

    def init(self, profile="minimal"):
        return init_project(self.root, profile=profile)

    def fake_agent(self, exit_code=0, create_outputs=True):
        script = self.root / f"fake_agent_{exit_code}_{int(create_outputs)}.py"
        task_json = json.dumps([
            {
                "id": "I1_T01",
                "name": "示例 TDD 任务",
                "description": "fake task",
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
        ], ensure_ascii=False, indent=2)
        report_json = json.dumps({"status": "done", "files_created": [], "files_modified": []}, ensure_ascii=False)
        script.write_text(
            textwrap.dedent(
                f"""
                import os
                import re
                import sys
                from pathlib import Path

                prompt = Path(sys.argv[1]).read_text(encoding="utf-8")
                if {bool(create_outputs)!r}:
                    capture = False
                    outputs = []
                    for line in prompt.splitlines():
                        if line.strip() in ("Required outputs:", "必需产物:"):
                            capture = True
                            continue
                        if capture:
                            if not line.strip():
                                break
                            outputs.append(line.strip())
                    for rel in outputs:
                        path = Path.cwd() / rel
                        path.parent.mkdir(parents=True, exist_ok=True)
                        if "probe-results" in rel:
                            content = "Killed: 3/3\\n"
                        elif "spec-compliance" in rel:
                            content = "COMPLIANT\\n"
                        elif rel.endswith("iteration_1_tasks.json"):
                            content = {task_json!r}
                        elif rel.endswith(".json"):
                            content = {report_json!r} + "\\n"
                        else:
                            content = "fake output for " + rel
                        path.write_text(content, encoding="utf-8")
                sys.exit({exit_code})
                """
            ).strip(),
            encoding="utf-8",
        )
        return script

    def set_fake_agents(self, codex_script=None, claude_script=None):
        if codex_script:
            os.environ["ELR_CODEX_CMD_JSON"] = json.dumps([sys.executable, str(codex_script), "{prompt_file}"])
        if claude_script:
            os.environ["ELR_CLAUDE_CMD_JSON"] = json.dumps([sys.executable, str(claude_script), "{prompt_file}"])

    def decision_file(self, decision_id, decision_type, phase):
        path = self.root / f"{decision_id}.json"
        write_json_atomic(path, {
            "id": decision_id,
            "type": decision_type,
            "phase": phase,
            "task_id": None,
            "summary": f"{decision_type} for {phase}",
            "decision": "approved",
            "created_by": "codex_coordinator",
            "source": "human_conversation",
        })
        return path

    def test_init_status_and_plan(self):
        self.init()
        status = Runtime(self.root).status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["phase"], "codex_planning")
        self.assertFalse(status["human_waiting"])

        plan = Runtime(self.root).plan()
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["handoff"]["to_agent"], "codex")
        self.assertEqual(plan["handoff"]["role"], "planner")

    def test_workflow_json_parse_failure_is_fail_closed(self):
        self.init()
        (self.root / ".elr" / "workflow.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(WorkflowError):
            WorkflowLoader(RuntimePaths(self.root)).load()

    def test_lock_existing_refuses_second_worker_turn(self):
        self.init()
        paths = RuntimePaths(self.root)
        LockManager(paths).acquire("codex", "manual")
        with self.assertRaises(LockError):
            Runtime(self.root).step()

    def test_worker_success_creates_handoff_and_waits_for_human(self):
        self.init()
        fake = self.fake_agent()
        self.set_fake_agents(codex_script=fake)

        result = Runtime(self.root).step()
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"]["status"], "waiting_human")
        self.assertTrue((self.root / ".elr" / "agent_outputs" / "planning.md").is_file())
        self.assertEqual(len(list((self.root / ".elr" / "handoffs").glob("*.json"))), 1)

    def test_decision_wrong_type_is_rejected_for_current_phase(self):
        self.init()
        fake = self.fake_agent()
        self.set_fake_agents(codex_script=fake)
        Runtime(self.root).step()

        bad = self.decision_file("bad_decision", "polishing_feedback", "codex_planning")
        with self.assertRaises(Exception):
            Runtime(self.root).decide(bad)

    def test_approval_decision_advances_phase(self):
        self.init()
        fake = self.fake_agent()
        self.set_fake_agents(codex_script=fake)
        Runtime(self.root).step()

        approval = self.decision_file("approval_1", "approval", "codex_planning")
        result = Runtime(self.root).decide(approval)
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"]["phase"], "claude_implementation")
        self.assertEqual(result["state"]["status"], "idle")
        self.assertTrue((self.root / ".elr" / "human_decisions" / "approval_1.json").is_file())

    def test_full_fake_codex_claude_codex_workflow(self):
        self.init()
        fake = self.fake_agent()
        self.set_fake_agents(codex_script=fake, claude_script=fake)
        runtime = Runtime(self.root)

        self.assertTrue(runtime.step()["ok"])
        self.assertTrue(runtime.decide(self.decision_file("approve_plan", "approval", "codex_planning"))["ok"])
        self.assertTrue(runtime.step()["ok"])
        self.assertTrue(runtime.decide(self.decision_file("approve_impl", "approval", "claude_implementation"))["ok"])
        final = runtime.step()

        self.assertTrue(final["ok"])
        self.assertEqual(final["state"]["status"], "done")
        self.assertTrue((self.root / ".elr" / "agent_outputs" / "final_review.md").is_file())

    def test_agent_nonzero_blocks_without_advancing(self):
        self.init()
        failing = self.fake_agent(exit_code=7)
        self.set_fake_agents(codex_script=failing)

        result = Runtime(self.root).step()
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"]["status"], "blocked")
        self.assertEqual(result["error_code"], "AGENT_EXIT_NONZERO")
        self.assertEqual(Runtime(self.root).status()["phase"], "codex_planning")

    def test_missing_required_output_blocks(self):
        self.init()
        fake = self.fake_agent(create_outputs=False)
        self.set_fake_agents(codex_script=fake)

        result = Runtime(self.root).step()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "VALIDATION_FAILED")
        self.assertEqual(result["state"]["status"], "blocked")

    def test_validation_command_failure_blocks(self):
        self.init()
        workflow_path = self.root / ".elr" / "workflow.json"
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        workflow["phases"][0]["turns"][0]["validation_commands"] = [
            f'"{sys.executable}" -c "import sys; sys.exit(3)"'
        ]
        workflow_path.write_text(json.dumps(workflow, indent=2), encoding="utf-8")
        fake = self.fake_agent()
        self.set_fake_agents(codex_script=fake)

        result = Runtime(self.root).step()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "VALIDATION_FAILED")
        self.assertEqual(result["state"]["status"], "blocked")

    def test_generated_templates_include_role_contracts(self):
        self.init()
        planner = (self.root / ".elr" / "templates" / "codex" / "planner.md").read_text(encoding="utf-8")
        implementer = (self.root / ".elr" / "templates" / "claude" / "implementer.md").read_text(encoding="utf-8")

        self.assertIn("禁止事项", planner)
        self.assertIn("最终产物", planner)
        self.assertIn("必需产物:", planner)
        self.assertIn("不得充当流程主控", implementer)
        self.assertIn("blocker 报告要求", implementer)
        self.assertIn("必需产物:", implementer)

    def test_init_creates_default_agent_config(self):
        self.init()
        config_path = self.root / ".elr" / "config.json"
        self.assertTrue(config_path.is_file())
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["agents"]["claude"]["mode"], "default")
        self.assertIsNone(config["agents"]["claude"]["command"])

    def test_configure_claude_autonomous_records_skip_permissions(self):
        self.init()
        result = ConfigStore(RuntimePaths(self.root)).configure_agent("claude", "autonomous")
        self.assertTrue(result["ok"])
        self.assertEqual(result["config"]["mode"], "autonomous")
        self.assertEqual(result["config"]["command"], AUTONOMOUS_CLAUDE_COMMAND)
        self.assertIn("--dangerously-skip-permissions", result["config"]["command"])

    def test_agent_config_command_is_used_without_env_override(self):
        self.init()
        fake = self.fake_agent()
        os.environ["ELR_CODEX_CMD_JSON"] = json.dumps([sys.executable, str(fake), "{prompt_file}"])
        ConfigStore(RuntimePaths(self.root)).configure_agent(
            "claude",
            "custom",
            command=[sys.executable, str(fake), "{prompt_file}"],
        )
        runtime = Runtime(self.root)
        runtime.step()
        runtime.decide(self.decision_file("approve_plan", "approval", "codex_planning"))

        result = runtime.step()
        self.assertTrue(result["ok"])
        self.assertEqual(result["handoff"]["to_agent"], "claude")
        self.assertEqual(result["handoff"]["agent_result"]["command"][0], sys.executable)

    def test_task_next_respects_dependencies(self):
        self.init(profile="product_tdd")
        tasks_path = self.root / ".elr" / "tasks" / "iteration_1_tasks.json"
        write_json_atomic(tasks_path, [
            {
                "id": "A",
                "name": "A",
                "description": "first",
                "dependencies": [],
                "expected_outputs": [".elr/task_reports/iteration_1/A.json"],
                "allowed_write_paths": [".elr/task_reports/"],
                "risk_level": "low",
                "tdd": {"enabled": False, "required_gates": []},
            },
            {
                "id": "B",
                "name": "B",
                "description": "second",
                "dependencies": ["A"],
                "expected_outputs": [".elr/task_reports/iteration_1/B.json"],
                "allowed_write_paths": [".elr/task_reports/"],
                "risk_level": "low",
                "tdd": {"enabled": False, "required_gates": []},
            },
        ])
        store = TaskStore(RuntimePaths(self.root), StateStore(RuntimePaths(self.root)))
        store.sync()
        self.assertEqual(store.next()["task"]["id"], "A")

        report_dir = self.root / ".elr" / "task_reports" / "iteration_1"
        report_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(report_dir / "A.json", {"status": "done"})
        store.complete("A", GateStore(RuntimePaths(self.root), StateStore(RuntimePaths(self.root))))
        self.assertEqual(store.next()["task"]["id"], "B")

    def test_gate_seal_and_integrity_detects_tamper(self):
        self.init(profile="product_tdd")
        output = self.root / ".elr" / "tdd" / "coverage" / "I1_T01.md"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("coverage", encoding="utf-8")
        gates = GateStore(RuntimePaths(self.root), StateStore(RuntimePaths(self.root)))
        self.assertTrue(gates.seal("I1_T01", "test_writer")["ok"])
        self.assertTrue(gates.check_integrity("I1_T01")["intact"])
        output.write_text("tampered", encoding="utf-8")
        self.assertFalse(gates.check_integrity("I1_T01")["intact"])

    def test_product_tdd_profile_runs_to_execution_and_completes_task(self):
        self.init(profile="product_tdd")
        fake = self.fake_agent()
        self.set_fake_agents(codex_script=fake, claude_script=fake)
        runtime = Runtime(self.root)

        self.assertTrue(runtime.step()["ok"])
        self.assertTrue(runtime.decide(self.decision_file("approve_discovery", "approval", "product_discovery"))["ok"])
        self.assertTrue(runtime.step()["ok"])
        self.assertTrue(runtime.decide(self.decision_file("approve_planning", "approval", "planning"))["ok"])

        for _ in range(7):
            self.assertTrue(runtime.step()["ok"])

        status = runtime.status()
        self.assertEqual(status["phase"], "execution")
        self.assertIn("task complete I1_T01", status["next_action"])

        report_dir = self.root / ".elr" / "task_reports" / "iteration_1"
        report_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(report_dir / "I1_T01.json", {"status": "done", "files_created": [], "files_modified": []})
        result = TaskStore(RuntimePaths(self.root), StateStore(RuntimePaths(self.root))).complete(
            "I1_T01",
            GateStore(RuntimePaths(self.root), StateStore(RuntimePaths(self.root))),
        )
        self.assertTrue(result["ok"])

        advanced = runtime.step()
        self.assertTrue(advanced["ok"])
        self.assertEqual(advanced["state"]["phase"], "iteration_review")

    def test_scope_change_decision_returns_to_planning(self):
        self.init(profile="product_tdd")
        fake = self.fake_agent()
        self.set_fake_agents(codex_script=fake, claude_script=fake)
        runtime = Runtime(self.root)
        runtime.step()
        runtime.decide(self.decision_file("approve_discovery", "approval", "product_discovery"))
        runtime.step()
        runtime.decide(self.decision_file("approve_planning", "approval", "planning"))

        state = runtime.state_store.load()
        state["status"] = "waiting_human"
        runtime.state_store.save(state)
        result = runtime.decide(self.decision_file("scope_change_1", "scope_change", "execution"))
        self.assertEqual(result["state"]["phase"], "planning")
        self.assertEqual(result["state"]["status"], "idle")

    def test_polishing_feedback_writes_feedback_task(self):
        self.init(profile="product_tdd")
        runtime = Runtime(self.root)
        state = runtime.state_store.load()
        state["phase"] = "iteration_polishing"
        state["status"] = "waiting_human"
        runtime.state_store.save(state)

        result = runtime.decide(self.decision_file("polish_1", "polishing_feedback", "iteration_polishing"))
        self.assertEqual(result["state"]["phase"], "execution")
        self.assertTrue((self.root / ".elr" / "feedback" / "polish_1.json").is_file())
        tasks = json.loads((self.root / ".elr" / "tasks" / "iteration_1_tasks.json").read_text(encoding="utf-8"))
        self.assertTrue(any(task["id"] == "feedback-polish_1" for task in tasks))

    def test_tdd_complete_requires_probe_and_spec_outputs(self):
        self.init(profile="product_tdd")
        report_dir = self.root / ".elr" / "task_reports" / "iteration_1"
        report_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(report_dir / "I1_T01.json", {"status": "done"})
        with self.assertRaises(Exception):
            TaskStore(RuntimePaths(self.root), StateStore(RuntimePaths(self.root))).complete(
                "I1_T01",
                GateStore(RuntimePaths(self.root), StateStore(RuntimePaths(self.root))),
            )

    def test_cli_init_status_plan(self):
        self.assertEqual(main(["init"]), 0)
        self.assertEqual(main(["status"]), 0)
        self.assertEqual(main(["plan"]), 0)


if __name__ == "__main__":
    unittest.main()
