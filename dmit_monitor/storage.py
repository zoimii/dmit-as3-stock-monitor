"""Durable, compare-and-swap state. Caches are deliberately not used for deduplication."""
import base64
import json
import os
import re
from pathlib import Path

from .http import SafeError, Transport
from .official import PRODUCTS


def fresh_state():
    return {"schema": 1, "products": {str(p.pid): {
        "status": "uninitialized", "reason": "not_checked", "failures": 0,
        "next_check": 0, "sold_out_streak": 0, "armed": True, "episode": 0,
        "last_health_alert": 0,
    } for p in PRODUCTS}, "outbox": {}, "updated_at": 0}


def validate(state):
    if (not isinstance(state, dict) or state.get("schema") != 1 or
            set(state.get("products", {})) != {str(p.pid) for p in PRODUCTS} or
            not isinstance(state.get("outbox"), dict)):
        raise SafeError("state_schema_invalid_no_notifications_sent")
    for item in state["products"].values():
        if (not isinstance(item, dict) or not isinstance(item.get("armed"), bool) or
                any(type(item.get(k)) is not int or item[k] < 0
                    for k in ("failures", "next_check", "sold_out_streak", "episode", "last_health_alert")) or
                item.get("status") not in {"uninitialized", "available", "sold_out", "unknown"}):
            raise SafeError("state_product_invalid_no_notifications_sent")
    for event in state["outbox"].values():
        if (not isinstance(event, dict) or event.get("kind") not in {"stock", "health"} or
                event.get("pid") not in {p.pid for p in PRODUCTS} or
                event.get("status") not in {"pending", "sending", "uncertain", "sent", "cancelled"}):
            raise SafeError("state_outbox_invalid_no_notifications_sent")
    return state


class FileStore:
    """For local inspection only; workflow uses GitHubStore."""
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            return validate(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            raise SafeError("state_missing_or_corrupt_initialize_explicitly") from None

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("x") as out:
                json.dump(fresh_state(), out, indent=2)
        except FileExistsError:
            raise SafeError("state_already_exists_will_not_reset_dedup") from None

    def save(self, state):
        validate(state)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        os.replace(temp, self.path)


class GitHubStore:
    BRANCH = "monitor-state"

    def __init__(self, repository, token, seed_sha, transport=None):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise SafeError("invalid_github_repository")
        self.base = f"https://api.github.com/repos/{repository}"
        self.token = token
        self.seed_sha = seed_sha
        self.transport = transport or Transport({"api.github.com"})
        self.sha = None

    def api(self, method, path, data=None):
        response = self.transport.request(method, self.base + path,
            json.dumps(data).encode() if data is not None else None,
            {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
        if response.status not in {200, 201, 404}:
            raise SafeError(f"github_state_http_{response.status}_stopped_before_next_send")
        try:
            return response.status, json.loads(response.body)
        except ValueError:
            raise SafeError("github_state_response_invalid") from None

    def initialize(self):
        status, _ = self.api("GET", f"/git/ref/heads/{self.BRANCH}")
        if status == 404:
            if not re.fullmatch(r"[a-f0-9]{40}", self.seed_sha):
                raise SafeError("missing_seed_commit")
            self.api("POST", "/git/refs", {"ref": f"refs/heads/{self.BRANCH}", "sha": self.seed_sha})
        status, _ = self.api("GET", f"/contents/state.json?ref={self.BRANCH}")
        if status != 404:
            raise SafeError("state_already_exists_will_not_reset_dedup")
        self.save(fresh_state())

    def load(self):
        status, payload = self.api("GET", f"/contents/state.json?ref={self.BRANCH}")
        if status == 404:
            raise SafeError("state_missing_run_initialize_no_notifications_sent")
        try:
            self.sha = payload["sha"]
            return validate(json.loads(base64.b64decode(payload["content"], validate=False)))
        except (ValueError, KeyError, TypeError):
            raise SafeError("state_corrupt_no_notifications_sent") from None

    def save(self, state):
        validate(state)
        content = base64.b64encode((json.dumps(state, sort_keys=True, indent=2) + "\n").encode()).decode()
        payload = {"message": "Update monitor state [skip ci]", "content": content, "branch": self.BRANCH}
        if self.sha:
            payload["sha"] = self.sha
        status, result = self.api("PUT", "/contents/state.json", payload)
        if status not in {200, 201}:
            raise SafeError("state_save_failed_no_further_notifications")
        try:
            self.sha = result["content"]["sha"]
        except (KeyError, TypeError):
            raise SafeError("state_save_unconfirmed_no_further_notifications") from None
