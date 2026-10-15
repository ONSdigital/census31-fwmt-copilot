#!/usr/bin/env python3
"""Process queued prompts sequentially with a CLI-specific runner executable."""

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path
from threading import Event

from task_queue import TaskQueue


class TaskWorker:
    def __init__(self, queue, runner, workspace, poll_interval=2.0):
        self.queue = queue
        self.runner = Path(runner)
        self.workspace = Path(workspace)
        self.poll_interval = poll_interval
        self.stopping = Event()
        if not self.runner.is_absolute() or not self.runner.is_file() or not os.access(self.runner, os.X_OK):
            raise ValueError("runner must be an absolute path to an executable file")
        if not self.workspace.is_dir():
            raise ValueError("workspace directory does not exist: {}".format(self.workspace))
        if poll_interval <= 0:
            raise ValueError("poll interval must be positive")

    def _stop(self, signal_number, frame):
        self.stopping.set()

    def run(self, until_empty=False):
        previous_handlers = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
        for number in previous_handlers:
            signal.signal(number, self._stop)
        try:
            while not self.stopping.is_set():
                task = self.queue.claim()
                if task is None:
                    if until_empty:
                        return
                    self.stopping.wait(self.poll_interval)
                    continue
                try:
                    result = subprocess.run(
                        [str(self.runner)], input=task["prompt"], text=True, cwd=self.workspace, check=False,
                    )
                except OSError as error:
                    self.queue.fail(task["id"], "Runner launch failed: {}".format(type(error).__name__))
                else:
                    if result.returncode == 0:
                        self.queue.complete(task["id"])
                    else:
                        self.queue.fail(task["id"], "Runner exited with status {}".format(result.returncode))
        finally:
            for number, handler in previous_handlers.items():
                signal.signal(number, handler)


class EventWorker:
    """Dispatch only structured events; legacy prompt tasks remain untouched."""

    def __init__(self, queue, workflow, poll_interval=2.0):
        if poll_interval <= 0:
            raise ValueError("poll interval must be positive")
        self.queue = queue
        self.workflow = workflow
        self.poll_interval = poll_interval
        self.stopping = Event()

    def _stop(self, signal_number, frame):
        self.stopping.set()

    def run(self, until_empty=False):
        previous_handlers = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
        for number in previous_handlers:
            signal.signal(number, self._stop)
        try:
            while not self.stopping.is_set():
                task = self.queue.claim_event()
                if task is None:
                    if until_empty:
                        return
                    self.stopping.wait(self.poll_interval)
                    continue
                try:
                    report = self.workflow.run(
                        task["id"], task["command"], task["event_type"], task["payload"]
                    )
                except (OSError, ValueError, RuntimeError) as error:
                    print("Workflow failed: {}".format(error), file=sys.stderr)
                    self.queue.finish_event(task["id"], "failed", {"reason": str(error)})
                else:
                    self.queue.finish_event(task["id"], report["status"], report)
        finally:
            for number, handler in previous_handlers.items():
                signal.signal(number, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue-db", type=Path, required=True)
    parser.add_argument("--runner", default=os.environ.get("AGENT_RUNNER"), required="AGENT_RUNNER" not in os.environ)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--until-empty", action="store_true", help="process pending tasks and exit")
    parser.add_argument("--events", action="store_true", help="process structured events instead of legacy prompts")
    parser.add_argument("--checkpoints", type=Path, help="persistent LangGraph SQLite checkpoint file")
    parser.add_argument("--worktree-root", type=Path, help="dedicated writable worktree directory")
    args = parser.parse_args()
    try:
        if args.events:
            if not args.checkpoints or not args.worktree_root:
                raise ValueError("--events requires --checkpoints and --worktree-root")
            from workflow.evidence import EvidenceClient
            from workflow.graph import BuildFailureWorkflow
            from workflow.repair import RepairAdapter
            evidence = EvidenceClient()
            repair = RepairAdapter(args.workspace, args.worktree_root, args.runner)
            with BuildFailureWorkflow(args.checkpoints, evidence, repair) as workflow:
                EventWorker(TaskQueue(args.queue_db), workflow, args.poll_interval).run(args.until_empty)
        else:
            TaskWorker(TaskQueue(args.queue_db), args.runner, args.workspace, args.poll_interval).run(args.until_empty)
    except (OSError, ValueError) as error:
        print("Task worker error: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())