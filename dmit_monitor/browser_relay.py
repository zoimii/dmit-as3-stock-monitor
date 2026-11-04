"""Cloud notification relay: only authenticated persisted browser events, never arbitrary text inputs."""
import os
import re
import time

from .browser_service import BrowserStore
from .http import SafeError
from .monitor import stock_text, log
from .official import PRODUCTS
from .telegram import Telegram


def guard_event(state, key, now):
    if not re.fullmatch(r"(?:stock|health):(?:253|254):\d+", key):
        raise SafeError("invalid_browser_event_id")
    event = state["outbox"].get(key)
    if not event:
        raise SafeError("browser_event_not_found")
    if event["status"] != "sending":
        return None
    if state.get("executor") != "macos_persistent_chrome" or state.get("source") != "official_browser":
        raise SafeError("browser_event_source_unverified")
    product = next((p for p in PRODUCTS if p.pid == event["pid"]), None)
    if product is None: raise SafeError("browser_event_pid_invalid")
    item = state["products"][str(product.pid)]
    expected_key = (f"stock:{product.pid}:{item['episode']}" if event["kind"] == "stock"
                    else f"health:{product.pid}:{event.get('created_at')}")
    if key != expected_key:
        raise SafeError("browser_event_key_mismatch")
    checked = item.get("checked_at", 0)
    if type(checked) is not int or not -60 <= now - checked <= 600:
        raise SafeError("browser_event_observation_expired")
    if event["kind"] == "stock":
        if (item["status"] != "available" or item["reason"] != "official_browser_configuration_cart_checkout_verified" or
                not re.fullmatch(r"[a-f0-9]{64}", item.get("digest", "")) or
                not re.fullmatch(r"\d+\.\d{2}", item.get("price", "")) or float(item["price"]) <= 0 or
                event["text"] != stock_text(product, item["price"], checked)):
            raise SafeError("browser_stock_confirmation_invalid")
    elif event["kind"] == "health":
        if item["status"] != "unknown" or item["failures"] < 3 or not event["text"].startswith("[监控异常，非补货]\n" + product.name):
            raise SafeError("browser_health_event_invalid")
    else: raise SafeError("browser_event_kind_invalid")
    return event


def deliver(store, notifier, key, now=None):
    now = int(time.time()) if now is None else now
    state = store.load()
    event = guard_event(state, key, now)
    if event is None:
        log("browser_relay_already_processed")
        return 0
    # Commit an uncertainty claim before contacting Telegram. A second run cannot resend it.
    event["status"] = "uncertain"
    store.save(state)
    result = notifier.send(event["text"])
    event.update(status=result.status, receipt=result.receipt, next_attempt=now + result.retry_after)
    store.save(state)
    log("browser_relay_delivery", pid=event["pid"], kind=event["kind"], status=result.status)
    return 0 if result.status == "sent" else 2


def main():
    notifier = Telegram(os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", ""))
    if os.environ.get("BROWSER_RELAY_MODE") == "test":
        result = notifier.send("[本机常驻浏览器监控通知测试，非补货]\n通知通道已接通。此消息不表示任何套餐有货。")
        log("browser_relay_test", status=result.status)
        return 0 if result.status == "sent" else 2
    store = BrowserStore(os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_SHA", ""))
    return deliver(store, notifier, os.environ.get("BROWSER_EVENT_ID", ""))


if __name__ == "__main__":
    try: raise SystemExit(main())
    except SafeError as exc:
        log("browser_relay_error", reason=str(exc))
        raise SystemExit(1)
