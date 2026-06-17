import argparse
import json
import os
import sys

from .errors import ELRError
from .gates import GateStore
from .runner import Runtime, doctor, init_project
from .state import StateStore
from .tasks import TaskStore


def main(argv=None):
    parser = argparse.ArgumentParser(prog="elr", description="External Linear Runtime 外部线性运行时")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="在当前项目初始化 .elr")
    p_init.add_argument("--force", action="store_true", help="重新初始化已存在的 .elr")
    p_init.add_argument("--profile", choices=["minimal", "product_tdd"], default="product_tdd", help="初始化 workflow profile")
    p_init.add_argument("--json", action="store_true", default=True)

    sub.add_parser("status", help="显示 runtime 状态").add_argument("--json", action="store_true", default=True)
    sub.add_parser("plan", help="预览下一次 handoff，不执行").add_argument("--json", action="store_true", default=True)
    sub.add_parser("step", help="执行一个 worker turn").add_argument("--json", action="store_true", default=True)

    p_run = sub.add_parser("run", help="连续执行直到停止条件")
    p_run.add_argument("--until", choices=["human_review", "blocked", "done"], default="human_review")
    p_run.add_argument("--json", action="store_true", default=True)

    sub.add_parser("resume", help="从 running/中断状态恢复").add_argument("--json", action="store_true", default=True)

    p_decide = sub.add_parser("decide", help="提交人类 decision")
    p_decide.add_argument("--file", required=True)
    p_decide.add_argument("--json", action="store_true", default=True)

    sub.add_parser("doctor", help="检查 runtime 依赖和配置").add_argument("--json", action="store_true", default=True)

    p_task = sub.add_parser("task", help="任务队列操作")
    task_sub = p_task.add_subparsers(dest="task_command")
    task_sub.add_parser("sync", help="同步当前迭代任务").add_argument("--json", action="store_true", default=True)
    task_sub.add_parser("next", help="获取下一个任务").add_argument("--json", action="store_true", default=True)
    p_context = task_sub.add_parser("context", help="获取任务上下文")
    p_context.add_argument("task_id")
    p_context.add_argument("--json", action="store_true", default=True)
    p_complete = task_sub.add_parser("complete", help="完成任务并执行门禁")
    p_complete.add_argument("task_id")
    p_complete.add_argument("--json", action="store_true", default=True)

    p_gate = sub.add_parser("gate", help="硬门禁操作")
    gate_sub = p_gate.add_subparsers(dest="gate_command")
    p_seal = gate_sub.add_parser("seal", help="封存一个 TDD phase 产物")
    p_seal.add_argument("task_id")
    p_seal.add_argument("phase")
    p_seal.add_argument("--json", action="store_true", default=True)
    p_integrity = gate_sub.add_parser("check-integrity", help="检查 manifest 完整性")
    p_integrity.add_argument("task_id")
    p_integrity.add_argument("--json", action="store_true", default=True)
    p_boundary = gate_sub.add_parser("check-boundary", help="检查文件边界")
    p_boundary.add_argument("task_id")
    p_boundary.add_argument("--json", action="store_true", default=True)
    p_validation = gate_sub.add_parser("run-validation", help="运行任务 validation commands")
    p_validation.add_argument("task_id")
    p_validation.add_argument("--json", action="store_true", default=True)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1

    try:
        result = dispatch(args)
    except ELRError as exc:
        result = {"ok": False, "error_code": exc.code, "message": exc.message, "details": exc.details}
    except Exception as exc:
        result = {"ok": False, "error_code": "UNEXPECTED_ERROR", "message": str(exc)}

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def dispatch(args):
    root = os.getcwd()
    if args.command == "init":
        return init_project(root, force=args.force, profile=args.profile)
    if args.command == "doctor":
        return doctor(root)

    runtime = Runtime(root)
    if args.command == "status":
        return runtime.status()
    if args.command == "plan":
        return runtime.plan()
    if args.command == "step":
        return runtime.step()
    if args.command == "run":
        return runtime.run_until(args.until)
    if args.command == "resume":
        return runtime.resume()
    if args.command == "decide":
        return runtime.decide(args.file)
    if args.command == "task":
        paths = runtime.paths
        state_store = StateStore(paths)
        tasks = TaskStore(paths, state_store)
        gates = GateStore(paths, state_store)
        if args.task_command == "sync":
            return tasks.sync()
        if args.task_command == "next":
            return tasks.next()
        if args.task_command == "context":
            return tasks.context(args.task_id)
        if args.task_command == "complete":
            return tasks.complete(args.task_id, gates)
        return {"ok": False, "error_code": "UNKNOWN_TASK_COMMAND", "message": f"未知 task 命令: {args.task_command}"}
    if args.command == "gate":
        paths = runtime.paths
        state_store = StateStore(paths)
        gates = GateStore(paths, state_store)
        tasks = TaskStore(paths, state_store)
        if args.gate_command == "seal":
            return gates.seal(args.task_id, args.phase)
        if args.gate_command == "check-integrity":
            return gates.check_integrity(args.task_id)
        if args.gate_command == "check-boundary":
            return gates.check_boundary(tasks.find(args.task_id))
        if args.gate_command == "run-validation":
            return gates.run_validation(args.task_id)
        return {"ok": False, "error_code": "UNKNOWN_GATE_COMMAND", "message": f"未知 gate 命令: {args.gate_command}"}
    return {"ok": False, "error_code": "UNKNOWN_COMMAND", "message": f"未知命令: {args.command}"}


if __name__ == "__main__":
    sys.exit(main())
