"""Authenticated HTTP ingress for the agent task queue."""

import hmac
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator
from task_queue import IdempotencyConflict, TaskQueue


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=4096)
    idempotencyKey: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("prompt", "idempotencyKey", mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value


@asynccontextmanager
async def lifespan(app: FastAPI):
    token_file = os.environ.get("AGENT_API_TOKEN_FILE")
    database = os.environ.get("TASK_QUEUE_DB")
    if not token_file or not database:
        raise RuntimeError("AGENT_API_TOKEN_FILE and TASK_QUEUE_DB are required")
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError("API token must not be blank")
        queue = TaskQueue(database)
    except (OSError, sqlite3.Error, ValueError) as error:
        raise RuntimeError("API token or task queue is unavailable") from error
    app.state.api_token = token
    app.state.queue = queue
    yield


bearer = HTTPBearer(auto_error=False)


def require_api_token(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    if credentials is None or not hmac.compare_digest(credentials.credentials, request.app.state.api_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )


def create_app():
    application = FastAPI(lifespan=lifespan)

    @application.post("/agent-requests", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_api_token)])
    def add_agent_request(agent_request: AgentRequest, request: Request, response: Response):
        try:
            message_id, created, task_status = request.app.state.queue.enqueue_once(
                agent_request.prompt, agent_request.idempotencyKey
            )
        except IdempotencyConflict as error:
            raise HTTPException(status_code=409, detail="Idempotency key already used for another prompt") from error
        except sqlite3.Error as error:
            raise HTTPException(status_code=503, detail="Task queue unavailable") from error
        if not created:
            response.status_code = status.HTTP_200_OK
        return {"messageId": message_id, "status": task_status}

    return application


app = create_app()