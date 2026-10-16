# Workflow event worker sequence

These UML sequence diagrams follow the [GeeksforGeeks sequence diagram guide](https://www.geeksforgeeks.org/system-design/unified-modeling-language-uml-sequence-diagrams/): participants have lifelines, calls appear in time order as solid arrows, and replies use dashed arrows. They describe how [worker.py](worker.py) claims a structured event, runs its checkpointed workflow, and persists the result.

## Happy path

A supported Cloud Build failure event is verified, repaired in an isolated worktree, rechecked before push, and recorded as `pushed-awaiting-ci`.

<!-- mermaid-checked: every participant uses `participant Id as "Label"`, no \n in aliases/messages/notes, every alt/opt/loop closed by end, no `:` inside any alias -->
```mermaid
sequenceDiagram
    participant Operator as "Worker runtime"
    participant Worker as "EventWorker"
    participant Queue as "TaskQueue"
    participant DB as "Queue SQLite"
    participant Workflow as "BuildFailureWorkflow"
    participant Checkpoint as "Checkpoint SQLite"
    participant Evidence as "EvidenceClient"
    participant GitHub as "GitHub"
    participant CloudBuild as "Cloud Build"
    participant Repair as "RepairAdapter"
    participant Worktree as "Isolated worktree"

    Operator->>Worker: Start with queue, workspace, checkpoint, and worktree paths
    Worker->>Workflow: Initialize evidence and repair workflow
    Worker->>Queue: claim_event()
    Queue->>DB: Claim oldest pending event
    DB-->>Queue: Running event and payload
    Queue-->>Worker: Event
    Worker->>Workflow: run(event ID, command, type, payload)
    Workflow->>Checkpoint: Load or initialize event state
    Checkpoint-->>Workflow: Checkpoint state
    Workflow->>Evidence: inspect(payload)
    Evidence->>GitHub: Verify PR head, check run, other checks, and build config
    GitHub-->>Evidence: PR and check evidence
    Evidence->>CloudBuild: Read build metadata and logs
    CloudBuild-->>Evidence: Failed build details and logs
    Evidence-->>Workflow: Eligible sole Spotless failure
    Workflow->>Repair: reproduce_and_fix(event ID, payload, evidence)
    Repair->>Worktree: Create detached worktree at verified PR head
    Repair->>Worktree: Reproduce failure, apply Spotless, run verification
    Worktree-->>Repair: Validated formatter-only changes
    Repair-->>Workflow: Validated repair
    Workflow->>Checkpoint: Persist validated workflow state
    Workflow->>Repair: publish with fresh evidence recheck
    Repair->>Evidence: recheck(payload, verified evidence)
    Evidence->>GitHub: Recheck PR head and PR-head checks
    GitHub-->>Evidence: Unchanged PR head and checks
    Evidence-->>Repair: Complete eligible snapshot
    Repair->>GitHub: Commit repair and push without force
    GitHub-->>Repair: Push accepted
    Repair-->>Workflow: pushed-awaiting-ci report
    Workflow-->>Worker: Final workflow report
    Worker->>Queue: finish_event(event ID, status, report)
    Queue->>DB: Persist terminal status and report
    DB-->>Queue: Updated
    Queue-->>Worker: Complete
```

## Full event processing flow

This view includes idle polling, unsupported or ineligible events, repair and publication safeguards, and workflow failures.

<!-- mermaid-checked: every participant uses `participant Id as "Label"`, no \n in aliases/messages/notes, every alt/opt/loop closed by end, no `:` inside any alias -->
```mermaid
sequenceDiagram
    participant Runtime as "Worker runtime"
    participant Worker as "EventWorker"
    participant Queue as "TaskQueue"
    participant DB as "Queue SQLite"
    participant Workflow as "BuildFailureWorkflow"
    participant Checkpoint as "Checkpoint SQLite"
    participant Evidence as "EvidenceClient"
    participant GitHub as "GitHub"
    participant CloudBuild as "Cloud Build"
    participant Repair as "RepairAdapter"
    participant Worktree as "Isolated worktree"

    Runtime->>Worker: Start with required paths
    Worker->>Workflow: Initialize workflow, evidence client, and repair adapter
    loop Until stopped
        Worker->>Queue: claim_event()
        Queue->>DB: Find and claim oldest pending event
        alt No pending event
            DB-->>Queue: No event
            Queue-->>Worker: None
            alt Run until empty
                Worker-->>Runtime: Exit
            else Keep polling
                Worker->>Worker: Wait poll interval
            end
        else Event claimed
            DB-->>Queue: Event marked running
            Queue-->>Worker: Event and payload
            Worker->>Workflow: run(event ID, command, type, payload)
            alt Caught OSError, ValueError, or RuntimeError
                Worker->>Queue: finish_event(event ID, failed, error report)
                Queue->>DB: Persist failed status and report
                DB-->>Queue: Updated
            else Workflow returns report
                Workflow->>Checkpoint: Load or initialize event state
                Checkpoint-->>Workflow: State for this event
                alt Checkpoint already has final report
                    Workflow-->>Worker: Previously persisted report
                else Workflow resumes or starts
                    Workflow->>Workflow: Route command, event type, and provider
                    alt Unsupported command, event type, or provider
                        Workflow->>Checkpoint: Persist unsupported report
                        Workflow-->>Worker: unsupported report
                    else Supported event
                        Workflow->>Evidence: inspect(payload)
                        Evidence->>GitHub: Verify PR head, target check, all PR checks, and config revision
                        GitHub-->>Evidence: PR and check evidence
                        Evidence->>CloudBuild: Read build status, steps, and logs
                        CloudBuild-->>Evidence: Build evidence
                        alt Evidence incomplete or not eligible
                            Evidence-->>Workflow: Ineligible evidence and reason
                            Workflow->>Checkpoint: Persist reported result
                            Workflow-->>Worker: reported result
                        else Sole Spotless failure is proven
                            Evidence-->>Workflow: Eligible evidence and verified PR head
                            Workflow->>Repair: reproduce_and_fix(event ID, payload, evidence)
                            Repair->>Worktree: Create or resume isolated detached worktree
                            Repair->>Worktree: Reproduce failure, apply formatter, run build and tests
                            Worktree-->>Repair: Validated changes or verification failure
                            alt Repair cannot be validated
                                Repair-->>Workflow: Not validated with reason
                                Workflow->>Checkpoint: Persist reported result
                                Workflow-->>Worker: reported result
                            else Repair validated
                                Repair-->>Workflow: Validated repair
                                Workflow->>Checkpoint: Persist validated state
                                Workflow->>Repair: publish with fresh evidence recheck
                                Repair->>Evidence: Recheck PR head and check runs
                                Evidence->>GitHub: Read current PR head and checks
                                GitHub-->>Evidence: Current PR and check snapshot
                                alt Snapshot changed or is incomplete
                                    Evidence-->>Repair: Recheck not complete and eligible
                                    Repair-->>Workflow: Commit retained, not pushed
                                    Workflow->>Checkpoint: Persist reported result
                                    Workflow-->>Worker: reported result
                                else Snapshot remains eligible
                                    Evidence-->>Repair: Complete unchanged snapshot
                                    Repair->>GitHub: Verify remote branch and push without force
                                    GitHub-->>Repair: Push accepted
                                    Repair-->>Workflow: pushed-awaiting-ci report
                                    Workflow->>Checkpoint: Persist publication report
                                    Workflow-->>Worker: pushed-awaiting-ci report
                                end
                            end
                        end
                    end
                end
                Worker->>Queue: finish_event(event ID, status, report)
                Queue->>DB: Persist terminal status and report
                DB-->>Queue: Updated
            end
        end
    end
    Runtime->>Worker: SIGINT or SIGTERM
    Worker-->>Runtime: Stop after current event processing
```

The worker polls every two seconds by default; `--until-empty` exits when no event is pending. SIGINT or SIGTERM sets the stop flag, and the current event processing finishes before the loop stops. Workflow exceptions caught by the worker (`OSError`, `ValueError`, and `RuntimeError`) are reported as failed events; other failures in queue handling are not silently converted to success.

Run the event worker with persistent queue and checkpoint databases, an existing workspace, and a separate writable worktree root:

```sh
PYTHONPATH=agent-workspace:agent-workspace/task-queue \
  python3 agent-workspace/worker/worker.py \
    --queue-db /path/to/state/task-queue.sqlite3 \
    --checkpoints /path/to/state/workflow-checkpoints.sqlite3 \
    --workspace /path/to/created-fwmt-workspace \
    --worktree-root /path/to/state/triage-worktrees
```

The worker does not invoke a prompt runner or automatically retry interrupted events. After confirming the worker has stopped, use `recover-events` and inspect the checkpoint, worktree, and remote state before any `retry-event ID`; in particular, a prior push attempt can have an uncertain outcome. A `pushed-awaiting-ci` result means the repair commit was pushed, not that CI passed.
