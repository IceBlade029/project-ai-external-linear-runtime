from .errors import StateError
from .jsonio import read_json, write_json_atomic


DEFAULT_CONFIG = {
    "schema_version": "1.0.0",
    "agents": {
        "codex": {
            "mode": "default",
            "command": None,
        },
        "claude": {
            "mode": "default",
            "command": None,
        },
    },
}


AUTONOMOUS_CLAUDE_COMMAND = [
    "claude",
    "-p",
    "--dangerously-skip-permissions",
    "{prompt}",
]


class ConfigStore:
    def __init__(self, paths):
        self.paths = paths

    def load(self):
        if not self.paths.config.is_file():
            return _copy_default()
        try:
            data = read_json(self.paths.config)
        except Exception as exc:
            raise StateError("config.json 无法解析。", path=str(self.paths.config), error=str(exc)) from exc
        return validate_config(data)

    def save(self, config):
        config = validate_config(config)
        write_json_atomic(self.paths.config, config)
        return config

    def ensure(self):
        if not self.paths.config.exists():
            self.save(_copy_default())

    def command_for(self, agent_name):
        config = self.load()
        agent = config["agents"].get(agent_name)
        if not agent:
            return None
        return agent.get("command")

    def configure_agent(self, agent_name, mode, command=None):
        config = self.load()
        if agent_name not in ("codex", "claude"):
            raise StateError("不支持的 agent。", agent=agent_name)
        if mode == "autonomous" and agent_name != "claude":
            raise StateError("autonomous 模式目前只支持 claude。", agent=agent_name)

        if mode == "default":
            config["agents"][agent_name] = {"mode": "default", "command": None}
        elif mode == "autonomous":
            config["agents"][agent_name] = {
                "mode": "autonomous",
                "command": AUTONOMOUS_CLAUDE_COMMAND,
            }
        elif mode == "custom":
            if not command:
                raise StateError("custom 模式必须提供 command。", agent=agent_name)
            config["agents"][agent_name] = {"mode": "custom", "command": command}
        else:
            raise StateError("agent mode 不合法。", mode=mode)
        self.save(config)
        return {"ok": True, "agent": agent_name, "mode": mode, "config": config["agents"][agent_name]}


def validate_config(config):
    if not isinstance(config, dict):
        raise StateError("config.json 必须是对象。")
    if config.get("schema_version") != "1.0.0":
        raise StateError("config schema_version 不支持。", schema_version=config.get("schema_version"))
    agents = config.get("agents")
    if not isinstance(agents, dict):
        raise StateError("config.agents 必须是对象。")
    for name in ("codex", "claude"):
        agents.setdefault(name, {"mode": "default", "command": None})
    for name, agent in agents.items():
        if name not in ("codex", "claude"):
            raise StateError("config.agents 包含不支持的 agent。", agent=name)
        if not isinstance(agent, dict):
            raise StateError("agent config 必须是对象。", agent=name)
        mode = agent.get("mode")
        if mode not in ("default", "autonomous", "custom"):
            raise StateError("agent mode 不合法。", agent=name, mode=mode)
        command = agent.get("command")
        if mode == "default":
            agent["command"] = None
        else:
            if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
                raise StateError("agent command 必须是非空字符串数组。", agent=name)
            if mode == "autonomous" and name != "claude":
                raise StateError("autonomous 模式目前只支持 claude。", agent=name)
    return config


def _copy_default():
    return {
        "schema_version": DEFAULT_CONFIG["schema_version"],
        "agents": {
            name: {"mode": agent["mode"], "command": agent["command"]}
            for name, agent in DEFAULT_CONFIG["agents"].items()
        },
    }
