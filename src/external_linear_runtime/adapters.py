import json
import os
import shlex
import subprocess
from pathlib import Path

from .config import ConfigStore
from .errors import AdapterError


class AgentResult:
    def __init__(self, ok, exit_code, stdout, stderr, command, log_path):
        self.ok = ok
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.command = command
        self.log_path = log_path

    def to_dict(self):
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "stdout": self.stdout[-4000:],
            "stderr": self.stderr[-4000:],
            "command": self.command,
            "log_path": self.log_path,
        }


class AgentAdapter:
    agent_name = "agent"

    def __init__(self, paths):
        self.paths = paths

    def run(self, handoff, prompt):
        prompt_path = self.paths.runtime / "current_prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        args = self._command_args(prompt_path, handoff)
        log_path = self.paths.logs / f"{handoff['id']}.{self.agent_name}.log"
        try:
            result = subprocess.run(
                args,
                cwd=str(self.paths.root),
                text=True,
                capture_output=True,
                timeout=handoff.get("timeout_seconds", 1800),
            )
        except FileNotFoundError as exc:
            raise AdapterError(
                "找不到 agent 命令。",
                agent=self.agent_name,
                command=args,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            log_path.write_text(_format_log(args, stdout, stderr, "TIMEOUT"), encoding="utf-8")
            return AgentResult(False, -1, stdout, stderr, args, str(log_path))

        log_path.write_text(
            _format_log(args, result.stdout or "", result.stderr or "", result.returncode),
            encoding="utf-8",
        )
        return AgentResult(
            result.returncode == 0,
            result.returncode,
            result.stdout or "",
            result.stderr or "",
            args,
            str(log_path),
        )

    def _command_args(self, prompt_path, handoff):
        env_json = os.environ.get(f"ELR_{self.agent_name.upper()}_CMD_JSON")
        if env_json:
            try:
                raw = json.loads(env_json)
            except json.JSONDecodeError as exc:
                raise AdapterError("agent 命令 JSON 不合法。", agent=self.agent_name) from exc
            if not isinstance(raw, list) or not raw:
                raise AdapterError("agent 命令 JSON 必须是非空数组。", agent=self.agent_name)
            return [self._format_part(part, prompt_path, handoff) for part in raw]

        env_cmd = os.environ.get(f"ELR_{self.agent_name.upper()}_CMD")
        if env_cmd:
            raw = shlex.split(env_cmd, posix=os.name != "nt")
            return [self._format_part(part, prompt_path, handoff) for part in raw]

        configured = ConfigStore(self.paths).command_for(self.agent_name)
        if configured:
            return [self._format_part(part, prompt_path, handoff) for part in configured]

        return self.default_command(str(prompt_path))

    def _format_part(self, part, prompt_path, handoff):
        return str(part).format(
            prompt_file=str(prompt_path),
            prompt=prompt_path.read_text(encoding="utf-8"),
            handoff_id=handoff["id"],
            output_dir=str(self.paths.agent_outputs),
        )

    def default_command(self, prompt_file):
        raise NotImplementedError


class CodexAdapter(AgentAdapter):
    agent_name = "codex"

    def default_command(self, prompt_file):
        return ["codex", "exec", prompt_file]


class ClaudeAdapter(AgentAdapter):
    agent_name = "claude"

    def default_command(self, prompt_file):
        prompt = Path(prompt_file).read_text(encoding="utf-8")
        return ["claude", "-p", prompt]


def adapter_for(agent, paths):
    if agent == "codex":
        return CodexAdapter(paths)
    if agent == "claude":
        return ClaudeAdapter(paths)
    raise AdapterError("不支持的 agent。", agent=agent)


def _format_log(args, stdout, stderr, exit_code):
    return (
        "# External Linear Runtime Agent 日志\n\n"
        f"Command: {args}\n\n"
        f"Exit: {exit_code}\n\n"
        "## STDOUT\n\n"
        f"{stdout}\n\n"
        "## STDERR\n\n"
        f"{stderr}\n"
    )
