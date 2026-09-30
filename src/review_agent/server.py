"""GitHub webhook service with human-in-the-loop approval.

Flow:
1. GitHub sends a `pull_request` webhook (opened / synchronize / reopened).
2. The signature is verified with HMAC-SHA256 (X-Hub-Signature-256).
3. The review graph runs in the background and pauses at `human_approval`.
   Its state is checkpointed to SQLite, so pending reviews survive restarts.
4. A maintainer inspects GET /reviews/{id} and calls POST /reviews/{id}/decision.
   Approved reviews are posted to the pull request; rejected ones are dropped.

Env: GITHUB_TOKEN, GITHUB_WEBHOOK_SECRET, ADMIN_TOKEN, ANTHROPIC_API_KEY,
     REVIEW_DB (default reviews.sqlite), REVIEW_MODEL (optional)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import BaseModel

from .graph import Deps, build_graph
from .workspace import GitHubWorkspace, Workspace

HANDLED_ACTIONS = {"opened", "synchronize", "reopened"}


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret or not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


class Decision(BaseModel):
    approved: bool
    comments: list[dict] | None = None  # optional edited comment list


class ReviewStore:
    """Tracks review status; graph state itself lives in the LangGraph checkpointer."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.lock = threading.Lock()
        with self.lock:
            conn.execute("""CREATE TABLE IF NOT EXISTS reviews (
                id TEXT PRIMARY KEY, owner TEXT, repo TEXT, number INTEGER, head_sha TEXT,
                status TEXT, review_url TEXT, error TEXT, created_at TEXT, updated_at TEXT)""")
            conn.commit()

    def create(self, pr: dict, head_sha: str) -> str:
        rid = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        with self.lock:
            self.conn.execute("INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (rid, pr["owner"], pr["repo"], pr["number"], head_sha, "running",
                               None, None, now, now))
            self.conn.commit()
        return rid

    def update(self, rid: str, **fields: Any) -> None:
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.lock:
            self.conn.execute(f"UPDATE reviews SET {cols} WHERE id = ?", (*fields.values(), rid))
            self.conn.commit()

    def get(self, rid: str) -> dict | None:
        with self.lock:
            cur = self.conn.execute("SELECT * FROM reviews WHERE id = ?", (rid,))
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
        return dict(zip(cols, row, strict=True)) if row else None

    def list(self, status: str | None = None) -> list[dict]:
        q, params = "SELECT * FROM reviews", ()
        if status:
            q, params = q + " WHERE status = ?", (status,)
        with self.lock:
            cur = self.conn.execute(q + " ORDER BY created_at DESC LIMIT 100", params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def create_app(
    workspace_factory: Callable[[dict], Workspace] | None = None,
    client: Any = None,
    db_path: str | None = None,
) -> FastAPI:
    app = FastAPI(title="MCP Code Review Agent")
    conn = sqlite3.connect(db_path or os.environ.get("REVIEW_DB", "reviews.sqlite"), check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    store = ReviewStore(sqlite3.connect(db_path or os.environ.get("REVIEW_DB", "reviews.sqlite"),
                                        check_same_thread=False))
    make_ws = workspace_factory or (lambda pr: GitHubWorkspace(pr["owner"], pr["repo"], pr["number"]))
    model = os.environ.get("REVIEW_MODEL")

    def graph_for(pr: dict):
        return build_graph(Deps(workspace=make_ws(pr), client=client, model=model), checkpointer=checkpointer)

    def config(rid: str) -> dict:
        return {"configurable": {"thread_id": rid}}

    def run_review(rid: str, pr: dict) -> None:
        try:
            state = graph_for(pr).invoke({"pr": pr}, config(rid))
            store.update(rid, status="awaiting_approval" if "__interrupt__" in state else "done")
        except Exception as exc:
            store.update(rid, status="failed", error=f"{type(exc).__name__}: {exc}")

    def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
        token = os.environ.get("ADMIN_TOKEN", "")
        if not token or not x_admin_token or not hmac.compare_digest(token, x_admin_token):
            raise HTTPException(status_code=401, detail="invalid admin token")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/webhook", status_code=202)
    async def webhook(request: Request, background: BackgroundTasks,
                      x_hub_signature_256: str | None = Header(default=None),
                      x_github_event: str | None = Header(default=None)) -> dict:
        body = await request.body()
        if not verify_signature(os.environ.get("GITHUB_WEBHOOK_SECRET", ""), body, x_hub_signature_256):
            raise HTTPException(status_code=401, detail="bad signature")
        if x_github_event != "pull_request":
            return {"ignored": f"event {x_github_event}"}
        event = json.loads(body)
        if event.get("action") not in HANDLED_ACTIONS:
            return {"ignored": f"action {event.get('action')}"}
        pull = event["pull_request"]
        owner, repo = event["repository"]["full_name"].split("/", 1)
        pr = {"owner": owner, "repo": repo, "number": pull["number"]}
        rid = store.create(pr, pull["head"]["sha"])
        background.add_task(run_review, rid, pr)
        return {"review_id": rid}

    @app.get("/reviews", dependencies=[Depends(require_admin)])
    def list_reviews(status: str | None = None) -> list[dict]:
        return store.list(status)

    @app.get("/reviews/{rid}", dependencies=[Depends(require_admin)])
    def get_review(rid: str) -> dict:
        row = store.get(rid)
        if row is None:
            raise HTTPException(status_code=404)
        pr = {"owner": row["owner"], "repo": row["repo"], "number": row["number"]}
        values = graph_for(pr).get_state(config(rid)).values
        return {**row, "review": values.get("review"), "usage": values.get("usage"),
                "timings": values.get("timings")}

    @app.post("/reviews/{rid}/decision", dependencies=[Depends(require_admin)])
    def decide(rid: str, decision: Decision) -> dict:
        row = store.get(rid)
        if row is None:
            raise HTTPException(status_code=404)
        if row["status"] != "awaiting_approval":
            raise HTTPException(status_code=409, detail=f"review is {row['status']}")
        pr = {"owner": row["owner"], "repo": row["repo"], "number": row["number"]}
        try:
            state = graph_for(pr).invoke(Command(resume=decision.model_dump()), config(rid))
        except Exception as exc:
            store.update(rid, status="failed", error=f"{type(exc).__name__}: {exc}")
            raise HTTPException(status_code=502, detail="posting the review failed") from exc
        if decision.approved:
            store.update(rid, status="posted", review_url=state.get("review_url"))
        else:
            store.update(rid, status="rejected")
        return store.get(rid)

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
