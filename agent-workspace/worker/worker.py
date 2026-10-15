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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue-db", type=Path, required=True)
    parser.add_argument("--runner", default=os.environ.get("AGENT_RUNNER"), required="AGENT_RUNNER" not in os.environ)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--until-empty", action="store_true", help="process pending tasks and exit")
    args = parser.parse_args()
    try:
        TaskWorker(TaskQueue(args.queue_db), args.runner, args.workspace, args.poll_interval).run(args.until_empty)
    except (OSError, ValueError) as error:
        print("Task worker error: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())