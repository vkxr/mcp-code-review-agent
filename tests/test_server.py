import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from review_agent.server import create_app, verify_signature
from review_agent.workspace import LocalWorkspace
from tests.fakes import FakeClient, tool_use
from tests.test_graph import AFTER, DIFF, FINDING

SECRET = "whsec_test"


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("ADMIN_TOKEN", "admin")
    workspaces = {}

    def factory(pr):
        key = pr["number"]
        workspaces.setdefault(key, LocalWorkspace(diff=DIFF, files={"app/db.py": AFTER}))
        return workspaces[key]

    client = FakeClient({
        "planning agent": [[tool_use("submit_result", {"files": [], "focus_areas": []})]] * 2,
        "senior engineer": [[tool_use("submit_result", {"findings": [FINDING]})]] * 2,
        "lead reviewer": [[tool_use("submit_result", {"summary": "s", "comments": [FINDING]})]] * 2,
    })
    app = create_app(workspace_factory=factory, client=client, db_path=str(tmp_path / "r.sqlite"))
    return TestClient(app), workspaces


def pr_event(number=7, action="opened") -> bytes:
    return json.dumps({"action": action, "repository": {"full_name": "vkxr/demo"},
                       "pull_request": {"number": number, "head": {"sha": "abc123"}}}).encode()


def post_event(client, body, sig=None, event="pull_request"):
    return client.post("/webhook", content=body,
                       headers={"X-Hub-Signature-256": sig or sign(body), "X-GitHub-Event": event})


def test_verify_signature():
    assert verify_signature(SECRET, b"x", sign(b"x"))
    assert not verify_signature(SECRET, b"x", sign(b"y"))
    assert not verify_signature(SECRET, b"x", None)
    assert not verify_signature("", b"x", sign(b"x"))


def test_rejects_bad_signature(env):
    client, _ = env
    assert post_event(client, pr_event(), sig="sha256=deadbeef").status_code == 401


def test_ignores_other_events(env):
    client, _ = env
    r = post_event(client, b"{}", event="push")
    assert r.status_code == 202 and "ignored" in r.json()


def test_admin_endpoints_require_token(env):
    client, _ = env
    assert client.get("/reviews").status_code == 401
    assert client.get("/reviews", headers={"X-Admin-Token": "wrong"}).status_code == 401


def test_full_flow_approve_then_post(env):
    client, workspaces = env
    rid = post_event(client, pr_event()).json()["review_id"]
    admin = {"X-Admin-Token": "admin"}

    detail = client.get(f"/reviews/{rid}", headers=admin).json()
    assert detail["status"] == "awaiting_approval"
    assert detail["review"]["comments"][0]["title"] == "SQL injection"
    assert workspaces[7].posted == []

    done = client.post(f"/reviews/{rid}/decision", json={"approved": True}, headers=admin).json()
    assert done["status"] == "posted"
    assert workspaces[7].posted[0]["inline"][0]["line"] == 10

    again = client.post(f"/reviews/{rid}/decision", json={"approved": True}, headers=admin)
    assert again.status_code == 409


def test_reject_posts_nothing(env):
    client, workspaces = env
    rid = post_event(client, pr_event(number=8)).json()["review_id"]
    admin = {"X-Admin-Token": "admin"}
    done = client.post(f"/reviews/{rid}/decision", json={"approved": False}, headers=admin).json()
    assert done["status"] == "rejected"
    assert workspaces[8].posted == []


def test_approve_with_edited_comments(env):
    client, workspaces = env
    rid = post_event(client, pr_event(number=9)).json()["review_id"]
    admin = {"X-Admin-Token": "admin"}
    edited = [{**FINDING, "title": "Parameterize this query"}]
    done = client.post(f"/reviews/{rid}/decision", json={"approved": True, "comments": edited},
                       headers=admin).json()
    assert done["status"] == "posted"
    assert workspaces[9].posted[0]["inline"][0]["title"] == "Parameterize this query"
