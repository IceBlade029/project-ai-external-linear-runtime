import os

from .errors import LockError
from .jsonio import read_json, write_json_atomic
from .state import now_iso


class LockManager:
    def __init__(self, paths):
        self.paths = paths

    def read(self):
        if not self.paths.lock.is_file():
            return None
        try:
            return read_json(self.paths.lock)
        except Exception:
            return {"unreadable": True, "path": str(self.paths.lock)}

    def acquire(self, owner, handoff_id):
        existing = self.read()
        if existing:
            raise LockError("runtime lock 已存在。", lock=existing)
        lock = {
            "owner": owner,
            "handoff_id": handoff_id,
            "pid": os.getpid(),
            "started_at": now_iso(),
        }
        write_json_atomic(self.paths.lock, lock)
        return lock

    def release(self):
        try:
            self.paths.lock.unlink()
        except FileNotFoundError:
            return
