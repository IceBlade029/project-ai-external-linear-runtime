import glob
import json
from pathlib import Path

from .jsonio import write_json_atomic
from .state import now_iso


class HandoffStore:
    def __init__(self, paths):
        self.paths = paths

    def next_id(self, phase, role):
        existing = sorted(glob.glob(str(self.paths.handoffs / "*.json")))
        number = len(existing) + 1
        safe_phase = _safe(phase)
        safe_role = _safe(role)
        return f"{number:04d}_{safe_phase}_{safe_role}"

    def path_for(self, handoff_id):
        return self.paths.handoffs / f"{handoff_id}.json"

    def write(self, handoff):
        data = dict(handoff)
        data.setdefault("created_at", now_iso())
        write_json_atomic(self.path_for(data["id"]), data)
        return data

    def latest(self):
        files = sorted(Path(self.paths.handoffs).glob("*.json"))
        if not files:
            return None
        with open(files[-1], "r", encoding="utf-8") as f:
            return json.load(f)


def _safe(value):
    out = []
    for ch in str(value):
        out.append(ch if ch.isalnum() or ch in ("-", "_") else "_")
    return "".join(out).strip("_") or "none"
