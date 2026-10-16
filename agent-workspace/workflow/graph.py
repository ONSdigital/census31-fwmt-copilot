"""Checkpointed orchestration for one PR build-failure event."""

from contextlib import ExitStack
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph


class BuildState(TypedDict, total=False):
    task_id: int
    command: str
    event_type: str
    payload: dict
    evidence: dict
    repair: dict
    report: dict


class BuildFailureWorkflow:
    def __init__(self, checkpoints, evidence, repair):
        self.checkpoints = Path(checkpoints)
        self.evidence = evidence
        self.repair = repair
        self.resources = ExitStack()

    def __enter__(self):
        if not self.checkpoints.parent.is_dir():
            raise ValueError("Checkpoint directory does not exist")
        saver = self.resources.enter_context(SqliteSaver.from_conn_string(str(self.checkpoints)))
        graph = StateGraph(BuildState)
        graph.add_node("route", self._route)
        graph.add_node("inspect", self._inspect)
        graph.add_node("repair", self._repair)
        graph.add_node("publish", self._publish)
        graph.add_edge(START, "route")
        graph.add_conditional_edges("route", lambda state: "inspect" if "report" not in state else END)
        graph.add_conditional_edges("inspect", lambda state: "repair" if "report" not in state else END)
        graph.add_conditional_edges("repair", lambda state: "publish" if "report" not in state else END)
        graph.add_edge("publish", END)
        self.graph = graph.compile(checkpointer=saver)
        return self

    def __exit__(self, *args):
        return self.resources.__exit__(*args)

    @staticmethod
    def _route(state):
        if state["command"] != "triage-pr-build-failure" or state["event_type"] != "github":
            return {"report": {"status": "unsupported", "reason": "Unsupported command or event type"}}
        if state["payload"].get("source") != "google-cloud-build":
            return {"report": {"status": "unsupported", "reason": "CI provider is not supported"}}
        return {}

    def _inspect(self, state):
        evidence = self.evidence.inspect(state["payload"])
        if not evidence["eligible"]:
            return {"evidence": evidence, "report": {"status": "reported", "reason": evidence["reason"],
                                                        "evidence": evidence}}
        return {"evidence": evidence}

    def _repair(self, state):
        result = self.repair.reproduce_and_fix(state["task_id"], state["payload"], state["evidence"])
        if not result.get("validated"):
            return {"repair": result, "report": {"status": "reported", "reason": result.get("reason", "Repair not validated")}}
        return {"repair": result}

    def _publish(self, state):
        report = self.repair.publish(
            state["task_id"], state["payload"], state["evidence"], state["repair"],
            lambda: self.evidence.recheck(state["payload"], state["evidence"])
        )
        return {"report": report}

    def run(self, task_id, command, event_type, payload):
        config = {"configurable": {"thread_id": "build-failure:{}".format(task_id)}}
        snapshot = self.graph.get_state(config)
        if snapshot.values:
            if snapshot.values.get("payload") != payload or snapshot.values.get("command") != command:
                raise ValueError("Checkpoint event does not match queued event")
            if not snapshot.next:
                return snapshot.values["report"]
            result = self.graph.invoke(None, config, durability="sync")
        else:
            result = self.graph.invoke(
                {"task_id": task_id, "command": command, "event_type": event_type, "payload": payload},
                config, durability="sync",
            )
        return result["report"]
