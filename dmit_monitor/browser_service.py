"""Persistent macOS Chrome worker. GitHub only stores state and relays notifications."""
import argparse
import atexit
import copy
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import select
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .http import SafeError
from .monitor import log, run
from .official import PRODUCTS, DOM, Observation, configuration, cart_evidence, one
from .storage import GitHubStore, fresh_state
from .telegram import Delivery

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "zoimii/dmit-as3-stock-monitor"


class RollingLog:
    def __init__(self, path):
        self.buffer = ""
        self.logger = logging.getLogger("dmit-browser-monitor")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(self.handler)

    def write(self, value):
        self.buffer += value
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line: self.logger.info(line)
        return len(value)

    def flush(self):
        self.handler.flush()


class BrowserStore(GitHubStore):
    BRANCH = "browser-monitor-state"


def gh(args, config=None):
    binary = (config or {}).get("gh", "/opt/homebrew/bin/gh")
    result = subprocess.run([binary, *args], capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise SafeError("github_cli_request_failed")
    return result.stdout.strip()


def store_for(config):
    # Keychain credential is kept only in process memory; never print or save it.
    token = gh(["auth", "token", "--hostname", "github.com"], config)
    seed = gh(["api", f"repos/{REPOSITORY}/git/ref/heads/main", "--jq", ".object.sha"], config)
    return BrowserStore(REPOSITORY, token, seed)


def initialize(store):
    status, _ = store.api("GET", f"/contents/state.json?ref={store.BRANCH}")
    if status == 200:
        store.load()
        return
    old = GitHubStore(REPOSITORY, store.token, store.seed_sha, store.transport)
    previous = old.load()  # Missing/corrupt old state must not silently reset deduplication.
    state = fresh_state()
    state["outbox"] = copy.deepcopy(previous["outbox"])
    for event in state["outbox"].values():
        if event["status"] in {"pending", "sending"}:
            event["status"] = "uncertain" if event["kind"] == "stock" else "cancelled"
    for pid, item in state["products"].items():
        before = previous["products"][pid]
        item.update(armed=before["armed"], episode=before["episode"])
    state["executor"] = "macos_persistent_chrome"
    state["source"] = "official_browser"
    state["migrated_at"] = int(time.time())
    branch_status, _ = store.api("GET", f"/git/ref/heads/{store.BRANCH}")
    if branch_status == 404:
        store.api("POST", "/git/refs", {"ref": f"refs/heads/{store.BRANCH}", "sha": store.seed_sha})
    store.save(state)


def validate_browser_result(result, product):
    if not isinstance(result, dict) or result.get("status") not in {"sold_out", "available", "unknown"}:
        raise SafeError("invalid_browser_result")
    reason = result.get("reason", "")
    if not re.fullmatch(r"[a-z0-9_]{1,100}", reason):
        raise SafeError("invalid_browser_reason")
    digest = result.get("digest", "")
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise SafeError("invalid_browser_evidence_digest")
    if result["status"] == "sold_out" and reason != "official_browser_purchase_page_sold_out":
        raise SafeError("invalid_negative_browser_evidence")
    if result["status"] == "available":
        if reason != "official_browser_configuration_cart_checkout_verified":
            raise SafeError("invalid_positive_browser_evidence")
        evidence = result.get("evidence", {})
        if evidence.get("accepted") is not True:
            raise SafeError("browser_configuration_acceptance_missing")
        _, configured_price = configuration(DOM(evidence.get("configuration", "")).root, product)
        observed_price = cart_evidence(DOM(evidence.get("cart", "")).root, product)
        if configured_price != observed_price or observed_price != result.get("price"):
            raise SafeError("browser_verified_price_mismatch")
        root = DOM(evidence.get("checkout", "")).root
        form = one(root.find(lambda n: n.tag == "form" and n.attrs.get("id") == "frmCheckout" and n.enabled()), "browser_checkout_form_missing")
        from urllib.parse import urljoin, urlparse, parse_qs
        action = urlparse(urljoin(product.url, form.attrs.get("action", "")))
        if (form.attrs.get("method", "").lower() != "post" or action.scheme != "https" or
                action.hostname not in {"www.dmit.io", "dmit.io"} or action.path != "/cart.php" or
                parse_qs(action.query).get("a") != ["checkout"]):
            raise SafeError("browser_checkout_action_invalid")
        one(form.find(lambda n: n.tag == "button" and n.attrs.get("id") == "btnCompleteOrder" and
                      n.attrs.get("type") == "submit" and n.enabled()), "browser_checkout_not_ready")
    return Observation(result["status"], reason, result.get("price", ""), digest)


class BrowserClient:
    def __init__(self, config):
        self.config, self.process = config, None

    def start(self):
        env = {**os.environ, "DMIT_BROWSER_PROFILE": self.config["profile"],
               "DMIT_BROWSER_HEADLESS": str(self.config.get("headless", False)).lower()}
        if self.config.get("browser_executable"):
            env["DMIT_BROWSER_EXECUTABLE"] = self.config["browser_executable"]
        self.process = subprocess.Popen([self.config["node"], str(ROOT / "browser" / "collector.cjs")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            env=env, cwd=str(ROOT), bufsize=1)

    def check(self, product):
        try:
            if not self.process or self.process.poll() is not None:
                self.start()
            self.process.stdin.write(json.dumps({"op": "check", "pid": product.pid}) + "\n")
            self.process.stdin.flush()
            ready, _, _ = select.select([self.process.stdout], [], [], 180)
            if not ready:
                self.close()
                raise SafeError("browser_collector_timeout")
            line = self.process.stdout.readline(7_000_000)
            return validate_browser_result(json.loads(line), product)
        except (OSError, ValueError, SafeError) as exc:
            reason = str(exc) if isinstance(exc, SafeError) else "browser_collector_unavailable"
            return Observation("unknown", reason)

    def close(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.write('{"op":"shutdown"}\n')
                self.process.stdin.flush()
                self.process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                self.process.terminate()
                try: self.process.wait(timeout=5)
                except subprocess.TimeoutExpired: self.process.kill()
        self.process = None


class GitHubRelay:
    def __init__(self, store, config):
        self.store, self.config = store, config

    def send_event(self, key, event, state):
        nonce = uuid.uuid4().hex[:16]
        try:
            gh(["workflow", "run", "browser-notify.yml", "--repo", REPOSITORY, "--ref", "main",
                "-f", "mode=event", "-f", f"event_id={key}", "-f", f"nonce={nonce}"], self.config)
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                runs = json.loads(gh(["run", "list", "--repo", REPOSITORY, "--workflow", "browser-notify.yml",
                    "--event", "workflow_dispatch", "--limit", "10", "--json", "displayTitle,status,conclusion"], self.config))
                match = next((r for r in runs if r["displayTitle"] == f"DMIT browser relay {nonce}"), None)
                if match and match["status"] == "completed":
                    remote = self.store.load()  # Refresh CAS SHA after the relay updates its receipt.
                    updated = remote["outbox"].get(key, {})
                    status = updated.get("status")
                    if status in {"sent", "pending", "uncertain", "cancelled"}:
                        return Delivery(status, updated.get("receipt", 0), max(300, updated.get("next_attempt", 0) - int(time.time())))
                    return Delivery("uncertain")
                time.sleep(4)
        except (SafeError, ValueError, subprocess.TimeoutExpired):
            try: self.store.load()
            except SafeError: pass
        return Delivery("uncertain")


def status_file(config, state, code, browser, started):
    value = {"executor": "macos_persistent_chrome", "service_pid": os.getpid(),
             "browser_worker_pid": browser.process.pid if browser.process else None,
             "started_at": started, "completed_at": int(time.time()), "interval_seconds": config["interval"],
             "result_code": code, "products": {pid: {k: p.get(k) for k in
                ("status", "reason", "checked_at", "next_check", "failures")} for pid, p in state["products"].items()}}
    target = Path(config["status_file"])
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, target)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--inspect", action="store_true", help="Read official pages only; no remote writes or notifications")
    parser.add_argument("--hold-open", action="store_true", help="Keep the isolated browser open for user verification")
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    config = json.loads(Path(args.config).read_text())
    if config.get("repository") != REPOSITORY or config.get("interval") != 300:
        raise SafeError("invalid_service_configuration")
    browser = BrowserClient(config)
    atexit.register(browser.close)
    stop = False
    def stop_handler(*unused):
        nonlocal stop
        stop = True
        browser.close()
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    if args.inspect:
        log("browser_inspect_started", service_pid=os.getpid())
        for product in PRODUCTS:
            result = browser.check(product)
            log("browser_inspect_no_notification", pid=product.pid, status=result.status, reason=result.reason, digest=result.digest)
        while args.hold_open and not stop:
            time.sleep(1)
        return
    lock = Path(config["lock_file"]).open("a")
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: raise SafeError("browser_service_already_running") from None
    if args.initialize:
        initialize(store_for(config))
        log("browser_state_initialized_preserving_dedup")
        return
    if config.get("log_file") and not args.once:
        sys.stdout = RollingLog(config["log_file"])
    started = int(time.time())
    last_code = 1
    while not stop:
        begin = time.monotonic()
        try:
            store = store_for(config)
            state, code = run(store, GitHubRelay(store, config), probe_factory=lambda: browser,
                              force=args.force and args.once)
            status_file(config, state, code, browser, started)
            last_code = code
            log("browser_service_cycle", result_code=code, executor="macos_persistent_chrome", interval_seconds=300)
        except Exception as exc:
            last_code = 1
            log("browser_service_error", reason=str(exc) if isinstance(exc, SafeError) else "service_error_requires_review",
                error_type=type(exc).__name__)
        if args.once: break
        remaining = max(1, config["interval"] - (time.monotonic() - begin))
        deadline = time.monotonic() + remaining
        while not stop and time.monotonic() < deadline:
            time.sleep(min(1, max(0.05, deadline - time.monotonic())))
    browser.close()
    if args.once: raise SystemExit(last_code)


if __name__ == "__main__":
    try: main()
    except SafeError as exc:
        log("browser_service_fatal", reason=str(exc))
        sys.exit(1)
