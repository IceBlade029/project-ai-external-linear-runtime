class ELRError(Exception):
    """带稳定错误码的基础异常。"""

    code = "ELR_ERROR"

    def __init__(self, message, **details):
        super().__init__(message)
        self.message = message
        self.details = details


class WorkflowError(ELRError):
    code = "WORKFLOW_ERROR"


class StateError(ELRError):
    code = "STATE_ERROR"


class LockError(ELRError):
    code = "LOCK_ERROR"


class DecisionError(ELRError):
    code = "DECISION_ERROR"


class AdapterError(ELRError):
    code = "ADAPTER_ERROR"
