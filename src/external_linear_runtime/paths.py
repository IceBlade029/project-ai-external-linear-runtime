from pathlib import Path


ELR_DIR = ".elr"


class RuntimePaths:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.elr = self.root / ELR_DIR
        self.runtime = self.elr / "runtime"
        self.workflow = self.elr / "workflow.json"
        self.config = self.elr / "config.json"
        self.state = self.runtime / "state.json"
        self.lock = self.runtime / "lock.json"
        self.handoffs = self.elr / "handoffs"
        self.human_decisions = self.elr / "human_decisions"
        self.agent_outputs = self.elr / "agent_outputs"
        self.logs = self.elr / "logs"
        self.templates = self.elr / "templates"
        self.plans = self.elr / "plans"
        self.specs_bdd = self.elr / "specs" / "bdd"
        self.specs_rules = self.elr / "specs" / "rules"
        self.tasks = self.elr / "tasks"
        self.task_reports = self.elr / "task_reports"
        self.feedback = self.elr / "feedback"
        self.iteration_reports = self.elr / "iteration_reports"
        self.tdd = self.elr / "tdd"
        self.gates = self.elr / "gates"
        self.gate_manifests = self.gates / "manifests"

    def ensure(self):
        for path in (
            self.runtime,
            self.handoffs,
            self.human_decisions,
            self.agent_outputs,
            self.logs,
            self.templates / "codex",
            self.templates / "claude",
            self.plans,
            self.specs_bdd,
            self.specs_rules,
            self.tasks,
            self.task_reports,
            self.feedback,
            self.iteration_reports,
            self.tdd / "approvals",
            self.tdd / "coverage",
            self.tdd / "reviews",
            self.tdd / "probe-plans",
            self.tdd / "probe-results",
            self.tdd / "spec-compliance",
            self.tdd / "e2e-results",
            self.gate_manifests,
        ):
            path.mkdir(parents=True, exist_ok=True)
