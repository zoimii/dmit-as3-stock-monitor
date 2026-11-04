"""Chrome Native Messaging bridge. Cookie values never enter this process."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys
import time
from urllib.parse import urlparse, parse_qs

from .browser_service import REPOSITORY, RollingLog, GitHubRelay, initialize, store_for, validate_browser_result
from .http import SafeError
from .monitor import log, run
from .official import PRODUCTS, DOM, configuration, cart_evidence, normalized

EXTENSION_ID = "cnmlgapddnmggdcjgkofcmaidoigaaep"
MAX_MESSAGE = 7_000_000


def read_message(stream):
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise SafeError("native_header_truncated")
    size = struct.unpack("<I", header)[0]
    if not 0 < size <= MAX_MESSAGE:
        raise SafeError("native_message_too_large")
    data = stream.read(size)
    if len(data) != size:
        raise SafeError("native_message_truncated")
    try:
        message = json.loads(data)
    except (ValueError, UnicodeError):
        raise SafeError("native_message_invalid") from None
    if not isinstance(message, dict):
        raise SafeError("native_message_invalid")
    return message


def write_message(stream, message):
    body = json.dumps(message, ensure_ascii=False).encode()
    if len(body) > 900_000:
        raise SafeError("native_reply_too_large")
    stream.write(struct.pack("<I", len(body)) + body)
    stream.flush()


def snapshot(value, action=None, pid=None):
    if not isinstance(value, dict) or not isinstance(value.get("html"), str) or len(value["html"]) > 2_000_000:
        raise SafeError("browser_snapshot_invalid")
    if value.get("status") != 200:
        raise SafeError("browser_snapshot_http_not_200")
    u = urlparse(value.get("url", ""))
    q = parse_qs(u.query)
    if u.scheme != "https" or u.hostname not in {"www.dmit.io", "dmit.io"} or u.path != "/cart.php":
        raise SafeError("browser_left_official_cart")
    if action is not None and q.get("a") != [action]:
        raise SafeError("browser_unexpected_cart_route")
    if pid is not None and q.get("pid") != [str(pid)]:
        raise SafeError("browser_wrong_purchase_pid")
    html = value["html"]
    root = DOM(html).root
    # A challenge or an authenticated session never establishes inventory.
    if any(marker in html.casefold() for marker in ("cf-chl-", "challenge-platform", "verify you are human", "just a moment", "g-recaptcha", "hcaptcha", "sorry, you have been blocked")):
        raise SafeError("official_browser_challenge_or_blocked")
    if root.find(lambda n: n.tag == "a" and "logout" in n.attrs.get("href", "").casefold()):
        raise SafeError("unexpected_authenticated_browser_session")
    if root.find(lambda n: n.tag == "form" and "login" in n.attrs.get("action", "").casefold()):
        raise SafeError("official_browser_login_required")
    return root, html


def product_for(pid):
    product = next((p for p in PRODUCTS if type(pid) is int and p.pid == pid), None)
    if product is None:
        raise SafeError("product_not_allowlisted")
    return product


def catalogue(value, product):
    root, html = snapshot(value)
    cards = root.find(lambda n: n.has_class("cart-products-box") and n.attrs.get("pid") == str(product.pid) and not n.hidden())
    if len(cards) != 1:
        raise SafeError("browser_catalog_pid_missing")
    titles = cards[0].find(lambda n: n.has_class("cart-products-title") and not n.hidden())
    if len(titles) != 1 or normalized(titles[0].text()) != normalized(product.name):
        raise SafeError("browser_catalog_product_mismatch")
    return html


def prepare_config(value, product):
    root, html = snapshot(value)
    fields, price = configuration(root, product)
    tokens = set(re.findall(r'''\bcsrfToken\s*=\s*["']([A-Za-z0-9_-]{16,200})["']''', html))
    if "token" not in fields and len(tokens) == 1:
        fields["token"] = tokens.pop()
    return {"fields": fields, "price": price}


def validate_collection(value, product, now):
    if not isinstance(value, dict) or type(value.get("checked_at")) is not int or not 0 <= now - value["checked_at"] <= 300:
        raise SafeError("browser_collection_expired")
    digest = hashlib.sha256()
    # Only unknown may lack page evidence; neither unavailable nor available may.
    if value.get("status") == "unknown":
        reason = value.get("reason", "browser_unknown")
        if not re.fullmatch(r"[a-z0-9_]{1,100}", reason):
            raise SafeError("browser_unknown_reason_invalid")
        return validate_browser_result({"status": "unknown", "reason": reason, "digest": digest.hexdigest()}, product)
    evidence = value.get("evidence", {})
    digest.update(catalogue(evidence.get("catalogue"), product).encode())
    if value.get("status") == "sold_out":
        root, html = snapshot(evidence.get("configuration"), "add", product.pid)
        if not root.find(lambda n: n.tag in {"h1", "h2"} and normalized(n.text()) in {"out of stock", "sold out", "缺货", "售罄"}):
            raise SafeError("browser_sold_out_heading_missing")
        digest.update(html.encode())
        return validate_browser_result({"status": "sold_out", "reason": "official_browser_purchase_page_sold_out", "digest": digest.hexdigest()}, product)
    if value.get("status") != "available":
        raise SafeError("browser_collection_status_invalid")
    pages = {}
    for key, action in (("configuration", None), ("cart", "view"), ("checkout", "checkout")):
        _, html = snapshot(evidence.get(key), action)
        pages[key] = html
        digest.update(html.encode())
    return validate_browser_result({"status": "available", "reason": "official_browser_configuration_cart_checkout_verified",
                                   "price": value.get("price"), "digest": digest.hexdigest(),
                                   "evidence": {**pages, "accepted": evidence.get("accepted")}}, product)


def public_status(state, code):
    return {"executor": "macos_chrome_extension", "completed_at": int(time.time()), "interval_seconds": 300,
            "result_code": code, "products": {pid: {k: p.get(k) for k in ("status", "reason", "checked_at", "next_check", "failures")}
                                              for pid, p in state["products"].items()}}


class Bridge:
    def __init__(self, config):
        self.config, self.store, self.due, self.planned_at = config, None, None, None
        self.force = False

    def handle(self, message):
        op = message.get("op")
        if op == "ping":
            return {"bridge": "ready", "pids": [253, 254], "interval_seconds": 300}
        if op == "plan":
            self.store = store_for(self.config)
            initialize(self.store)
            state = self.store.load()
            self.planned_at = int(time.time())
            self.force = message.get("force") is True
            self.due = [p.pid for p in PRODUCTS if self.force or state["products"][str(p.pid)]["next_check"] <= self.planned_at]
            return {"due": self.due, **public_status(state, 0)}
        if op == "prepare_config":
            product = product_for(message.get("pid"))
            if self.due is None or product.pid not in self.due:
                raise SafeError("browser_cycle_not_planned")
            return prepare_config(message.get("page"), product)
        if op == "validate_cart":
            product = product_for(message.get("pid"))
            root, _ = snapshot(message.get("page"), "view")
            price = cart_evidence(root, product)
            if price != message.get("price"):
                raise SafeError("browser_cart_configuration_price_mismatch")
            return {"valid": True}
        if op == "apply":
            if self.due is None or int(time.time()) - self.planned_at > 300:
                raise SafeError("browser_cycle_not_planned_or_expired")
            values = message.get("results")
            if not isinstance(values, dict) or set(values) != {str(pid) for pid in self.due}:
                raise SafeError("browser_cycle_results_incomplete")
            now = int(time.time())
            observations = {pid: validate_collection(values[str(pid)], product_for(pid), now) for pid in self.due}
            store = self.store
            state = store.load()
            state.update(executor="macos_persistent_chrome", source="official_browser", collector="chrome_extension")
            store.save(state)
            class Probe:
                def check(self, product):
                    if product.pid not in observations:
                        raise SafeError("unplanned_browser_product")
                    return observations[product.pid]
            # The same reference time avoids a product becoming due between planning and applying.
            state, code = run(store, GitHubRelay(store, self.config), lambda: Probe(), now=self.planned_at, force=self.force)
            status = public_status(state, code)
            target = Path(self.config["status_file"])
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n")
            os.replace(temporary, target)
            log("chrome_extension_cycle", result_code=code, products_checked=list(observations))
            self.due = None
            return status
        raise SafeError("native_operation_not_allowlisted")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("origin", nargs="?")
    args = parser.parse_args()
    if args.origin != f"chrome-extension://{EXTENSION_ID}/":
        raise SafeError("native_extension_origin_rejected")
    os.umask(0o077)
    config = json.loads(Path(args.config).read_text())
    if config.get("repository") != REPOSITORY or config.get("interval") != 300:
        raise SafeError("native_configuration_invalid")
    incoming, outgoing = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = RollingLog(config["log_file"])
    lock = Path(config["lock_file"]).open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        write_message(outgoing, {"id": 0, "error": "browser_cycle_already_running"})
        return
    bridge = Bridge(config)
    while True:
        message = read_message(incoming)
        if message is None:
            break
        response = {"id": message.get("id")}
        try:
            response["result"] = bridge.handle(message)
        except Exception as exc:
            response["error"] = str(exc) if isinstance(exc, SafeError) else "native_operation_failed"
            log("chrome_native_error", reason=response["error"], error_type=type(exc).__name__)
        write_message(outgoing, response)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # No exception repr or unframed output: it may contain page/credential data.
        sys.exit(1)
