# Agent request API sequence

These UML sequence diagrams follow the [GeeksforGeeks sequence diagram guide](https://www.geeksforgeeks.org/system-design/unified-modeling-language-uml-sequence-diagrams/): participants have lifelines, calls appear in time order as solid arrows, and replies use dashed arrows. They describe the `POST /agent-requests` flow in [app.py](app.py), from authentication through validation and durable queueing.

## Happy path

A valid new request is persisted as a pending event and receives a `201` response.

<!-- mermaid-checked: every participant uses `participant Id as "Label"`, no \n in aliases/messages/notes, every alt/opt/loop closed by end, no `:` inside any alias -->
```mermaid
sequenceDiagram
    participant Client as "HTTP client"
    participant API as "FastAPI endpoint"
    participant Auth as "Token dependency"
    participant Model as "AgentRequest model"
    participant Validator as "Command validator"
    participant Queue as "TaskQueue"
    participant DB as "SQLite"

    Client->>API: POST /agent-requests with token and JSON
    API->>Auth: require_api_token(credentials)
    Auth-->>API: Authorized
    API->>API: Read request stream within size limit
    API->>Model: model_validate_json(body)
    Model-->>API: AgentRequest
    API->>Validator: validate_command(command, type, payload)
    Validator-->>API: Valid
    API->>Queue: enqueue_event(command, type, payload, idempotencyKey)
    Queue->>DB: Begin transaction and look up key
    DB-->>Queue: Key not found
    Queue->>DB: Insert workflow event
    DB-->>Queue: New ID
    Queue-->>API: Message ID, created, pending
    API-->>Client: 201 messageId and pending status
```

## Full request flow

<!-- mermaid-checked: every participant uses `participant Id as "Label"`, no \n in aliases/messages/notes, every alt/opt/loop closed by end, no `:` inside any alias -->
```mermaid
sequenceDiagram
    participant Client as "HTTP client"
    participant API as "FastAPI endpoint"
    participant Auth as "Bearer token dependency"
    participant Model as "AgentRequest model"
    participant Validator as "Command validator"
    participant Queue as "TaskQueue"
    participant DB as "SQLite"

    Client->>API: POST /agent-requests with bearer token and JSON
    API->>Auth: require_api_token(credentials)
    alt Token missing or does not match
        Auth-->>Client: 401 Unauthorized with WWW-Authenticate Bearer
    else Token matches
        Auth-->>API: Authorized
        API->>API: Read request stream up to 65536 bytes
        alt Body exceeds limit
            API-->>Client: 413 Request body too large
        else Body within limit
            API->>Model: model_validate_json(body)
            alt JSON or request model invalid
                Model-->>API: ValidationError
                API-->>Client: 422 Invalid agent request
            else Request model valid
                Model-->>API: AgentRequest
                API->>Validator: validate_command(command, type, payload)
                alt Command or payload rejected
                    Validator-->>API: ValueError
                    API-->>Client: 422 Validation detail
                else Command accepted
                    Validator-->>API: Valid
                    API->>Queue: enqueue_event(command, type, payload, idempotencyKey)
                    Queue->>DB: Begin transaction and find idempotency key
                    alt Existing key belongs to another event
                        DB-->>Queue: Conflicting event
                        Queue-->>API: IdempotencyConflict
                        API-->>Client: 409 Idempotency key conflict
                    else Database error
                        DB-->>Queue: SQLite error
                        Queue-->>API: sqlite3.Error
                        API-->>Client: 503 Task queue unavailable
                    else Matching event already exists
                        DB-->>Queue: Existing ID and status
                        Queue-->>API: message ID, false, status
                        API-->>Client: 200 messageId and status
                    else New event
                        Queue->>DB: Insert workflow event
                        DB-->>Queue: New ID
                        Queue-->>API: message ID, true, pending
                        API-->>Client: 201 messageId and pending status
                    end
                end
            end
        end
    end
```

At startup, the application lifespan reads `AGENT_API_TOKEN_FILE` and `TASK_QUEUE_DB`, loads the bearer token, and initializes `TaskQueue`; missing or unusable configuration prevents startup. The endpoint queues events but does not run the worker. The diagram shows calls as synchronous interactions even though the HTTP handler is declared `async`; no fire-and-forget dispatch occurs in this endpoint.
