import fnmatch
import hashlib
import json
import subprocess
from pathlib import Path

from .errors import StateError
from .jsonio import read_json, write_json_atomic
from .state import now_iso


TDD_PHASE_OUTPUTS = {
    "test_writer": [
        ".elr/tdd/coverage/{task_id}.md",
    ],
    "test_reviewer": [
        ".elr/tdd/reviews/{task_id}.md",
    ],
    "probe_designer": [
        ".elr/tdd/probe-plans/{task_id}.md",
    ],
    "probe_executor": [
        ".elr/tdd/probe-results/{task_id}.md",
    ],
    "approval_decider": [
        ".elr/tdd/approvals/{task_id}.md",
    ],
    "implementer": [
        ".elr/task_reports/{task_id}.implementation.md",
    ],
    "spec_compliance": [
        ".elr/tdd/spec-compliance/{task_id}.md",
    ],
    "e2e_verifier": [
        ".elr/tdd/e2e-results/{task_id}.md",
    ],
}


class GateStore:
    def __init__(self, paths, state_store):
        self.paths = paths
        self.state_store = state_store

    def seal(self, task_id, phase):
        if phase not in TDD_PHASE_OUTPUTS:
            raise StateError("未知 gate phase。", phase=phase)
        manifest_path = self._manifest_path(task_id, phase)
        if manifest_path.is_file():
            raise StateError("manifest 已存在，拒绝覆盖。", path=str(manifest_path))
        files = {}
        missing = []
        for template in TDD_PHASE_OUTPUTS[phase]:
            rel = template.format(task_id=task_id)
            full = self.paths.root / rel
            if not full.is_file():
                missing.append(rel)
                continue
            files[rel] = "sha256:" + _sha256(full)
        if missing:
            raise StateError("seal 缺少必需产物。", missing=missing)
        manifest = {
            "task_id": task_id,
            "phase": phase,
            "created_at": now_iso(),
            "files": files,
        }
        write_json_atomic(manifest_path, manifest)
        return {"ok": True, "task_id": task_id, "phase": phase, "manifest": str(manifest_path), "files": files}

    def check_integrity(self, task_id):
        manifests = sorted(self.paths.gate_manifests.glob(f"{task_id}.*.manifest.json"))
        violations = []
        phases = []
        for path in manifests:
            manifest = read_json(path)
            phases.append(manifest.get("phase"))
            for rel, expected in manifest.get("files", {}).items():
                full = self.paths.root / rel
                if not full.is_file():
                    violations.append({"file": rel, "issue": "missing", "manifest": str(path)})
                    continue
                current = "sha256:" + _sha256(full)
                if current != expected:
                    violations.append({"file": rel, "issue": "tampered", "manifest": str(path)})
        return {
            "ok": True,
            "task_id": task_id,
            "intact": not violations,
            "violations": violations,
            "phases_found": phases,
        }

    def check_boundary(self, task):
        changed = _git_changed_files(self.paths.root)
        method = "git_diff" if changed is not None else "report_fallback"
        if changed is None:
            changed = _report_changed_files(self.paths, task["id"])
        forbidden = [
            rel for rel in changed
            if rel.startswith(".elr/runtime/")
            or rel.startswith(".elr/handoffs/")
            or rel.startswith(".elr/gates/manifests/")
        ]
        allowed = task.get("allowed_write_paths", [])
        production = [rel for rel in changed if not rel.startswith(".elr/")]
        extra = [rel for rel in production if not _matches_any(rel, allowed)]
        return {
            "ok": not forbidden and not extra,
            "method": method,
            "changed_files": changed,
            "forbidden_violations": forbidden,
            "extra_files": extra,
            "allowed_write_paths": allowed,
        }

    def run_validation(self, task_id):
        from .tasks import TaskStore

        task = TaskStore(self.paths, self.state_store).find(task_id)
        commands = task.get("tdd", {}).get("validation_commands", [])
        results = []
        for command in commands:
            result = subprocess.run(
                command,
                shell=True,
                cwd=str(self.paths.root),
                text=True,
                capture_output=True,
                timeout=300,
            )
            results.append({
                "command": command,
                "exit_code": result.returncode,
                "stdout": (result.stdout or "")[-2000:],
                "stderr": (result.stderr or "")[-2000:],
            })
        output = {
            "ok": all(item["exit_code"] == 0 for item in results),
            "task_id": task_id,
            "results": results,
        }
        write_json_atomic(self.paths.gates / f"{task_id}.validation.json", output)
        return output

    def run_required_gates(self, task):
        required = task.get("tdd", {}).get("required_gates", [])
        details = {}
        if "integrity" in required:
            details["integrity"] = self.check_integrity(task["id"])
            if not details["integrity"]["intact"]:
                return {"ok": False, "failed": "integrity", "details": details}
        if "boundary" in required:
            details["boundary"] = self.check_boundary(task)
            if not details["boundary"]["ok"]:
                return {"ok": False, "failed": "boundary", "details": details}
        if "validation" in required:
            details["validation"] = self.run_validation(task["id"])
            if not details["validation"]["ok"]:
                return {"ok": False, "failed": "validation", "details": details}
        if task.get("tdd", {}).get("enabled"):
            tdd = self._check_tdd_outputs(task)
            details["tdd"] = tdd
            if not tdd["ok"]:
                return {"ok": False, "failed": "tdd", "details": details}
        return {"ok": True, "details": details}

    def _check_tdd_outputs(self, task):
        task_id = task["id"]
        missing = []
        for rel in (
            f".elr/tdd/approvals/{task_id}.md",
            f".elr/tdd/probe-results/{task_id}.md",
            f".elr/tdd/spec-compliance/{task_id}.md",
        ):
            if not (self.paths.root / rel).is_file():
                missing.append(rel)
        if task["risk_level"] == "high" and task.get("tdd", {}).get("e2e"):
            rel = f".elr/tdd/e2e-results/{task_id}.md"
            if not (self.paths.root / rel).is_file():
                missing.append(rel)
        probe_ok = True
        probe_detail = {}
        probe_path = self.paths.root / f".elr/tdd/probe-results/{task_id}.md"
        if probe_path.is_file():
            content = probe_path.read_text(encoding="utf-8")
            import re
            match = re.search(r"(?i)(?:killed|杀死)\s*[:：]\s*(\d+)\s*/\s*(\d+)", content)
            if match:
                killed, total = int(match.group(1)), int(match.group(2))
                probe_detail = {"killed": killed, "total": total}
                probe_ok = total >= 3 and killed == total
            else:
                probe_ok = False
                probe_detail = {"error": "未找到 killed 统计"}
        spec_ok = True
        sc_path = self.paths.root / f".elr/tdd/spec-compliance/{task_id}.md"
        if sc_path.is_file():
            spec_ok = "COMPLIANT" in sc_path.read_text(encoding="utf-8")
        return {
            "ok": not missing and probe_ok and spec_ok,
            "missing": missing,
            "probe": probe_detail,
            "probe_ok": probe_ok,
            "spec_compliance_ok": spec_ok,
        }

    def _manifest_path(self, task_id, phase):
        return self.paths.gate_manifests / f"{task_id}.{phase}.manifest.json"


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_changed_files(root):
    commands = [
        ["git", "diff", "--name-only"],
        ["git", "diff", "--cached", "--name-only"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ]
    files = set()
    any_success = False
    for command in commands:
        try:
            result = subprocess.run(command, cwd=str(root), text=True, capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            continue
        any_success = True
        for line in result.stdout.splitlines():
            if line.strip():
                files.add(line.strip().replace("\\", "/"))
    return sorted(files) if any_success else None


def _report_changed_files(paths, task_id):
    files = []
    for path in paths.task_reports.glob(f"**/{task_id}.json"):
        try:
            report = read_json(path)
        except Exception:
            continue
        files.extend(report.get("files_created", []))
        files.extend(report.get("files_modified", []))
    return sorted(set(str(item).replace("\\", "/") for item in files))


def _matches_any(path, patterns):
    for pattern in patterns:
        if path == pattern:
            return True
        if pattern.endswith("/") and path.startswith(pattern):
            return True
        if fnmatch.fnmatch(path, pattern):
            return True
    return False
