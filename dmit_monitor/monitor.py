"""Inventory state machine and durable outbox. Unknown never means sold out or available."""
import json
import time
from datetime import datetime, timedelta, timezone

from .official import PRODUCTS, OfficialProbe


def timestamp(now):
    return datetime.fromtimestamp(now, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S 北京时间")


def log(event, **fields):
    print(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "event": event, **fields}, ensure_ascii=False), flush=True)


def add_event(state, key, kind, product, now, text):
    state["outbox"][key] = {"kind": kind, "pid": product.pid, "created_at": now,
                            "status": "pending", "next_attempt": now, "text": text}


def stock_text(product, price, now):
    return (f"[DMIT 官方可下单确认]\n{product.name} (PID {product.pid})\n"
            f"本次官方月付价格：${price} USD（参考 ${product.reference_price}/月）\n"
            f"验证：配置接受 → 正确购物车条目 → 可继续结算\n"
            f"检查时间：{timestamp(now)}\n{product.url}\n"
            "库存可能随时变化；请手动打开官方页面下单。")


def observe(state, product, result, now):
    item = state["products"][str(product.pid)]
    item.update(status=result.status, reason=result.reason, checked_at=now, digest=result.digest)
    if result.status == "unknown":
        item["failures"] += 1
        item["sold_out_streak"] = 0
        delay = min(1800, 300 * 2 ** min(5, item["failures"] - 1))
        item["next_check"] = now + max(delay, result.retry_after)
        if item["failures"] >= 3 and (not item["last_health_alert"] or now - item["last_health_alert"] >= 86400):
            item["last_health_alert"] = now
            add_event(state, f"health:{product.pid}:{now}", "health", product, now,
                      f"[监控异常，非补货]\n{product.name} (PID {product.pid})\n"
                      f"官方连续 {item['failures']} 次无法验证：{result.reason}\n"
                      f"没有确认可下单，未发送补货成功通知。\n检查时间：{timestamp(now)}\n"
                      f"当前退避至：{timestamp(item['next_check'])}")
        return
    item["failures"] = 0
    item["next_check"] = now + 300
    if result.status == "sold_out":
        item["sold_out_streak"] += 1
        if item["sold_out_streak"] >= 2:
            item["armed"] = True
        return
    item["sold_out_streak"] = 0
    item["price"] = result.price
    if item["armed"]:
        item["armed"] = False
        item["episode"] += 1
        add_event(state, f"stock:{product.pid}:{item['episode']}", "stock", product, now,
                  stock_text(product, result.price, now))


def run(store, notifier, probe_factory=OfficialProbe, now=None, force=False):
    now = int(time.time()) if now is None else now
    state = store.load()
    # A previous worker may have died immediately before or after sending.
    for event in state["outbox"].values():
        if event["status"] == "sending":
            event["status"] = "uncertain"
            log("delivery_uncertain_manual_review", pid=event["pid"], kind=event["kind"])
    observations = {}
    for product in PRODUCTS:
        item = state["products"][str(product.pid)]
        if not force and now < item["next_check"]:
            log("backoff", pid=product.pid, status=item["status"], next_check=item["next_check"])
            continue
        result = probe_factory().check(product)
        observations[product.pid] = result
        observe(state, product, result, now)
        log("official_check", pid=product.pid, status=result.status, reason=result.reason,
            price=result.price, evidence_sha256=result.digest)
    state["updated_at"] = now
    # Save observations and all outbox intents before any external notification.
    store.save(state)
    for key, event in list(state["outbox"].items()):
        if event["status"] != "pending" or now < event["next_attempt"]:
            continue
        if event["kind"] == "stock":
            result = observations.get(event["pid"])
            if result is None:
                continue  # Never deliver a stale restock event on a skipped check.
            if result.status != "available":
                if result.status == "sold_out":
                    event["status"] = "cancelled"
                    store.save(state)
                continue
            product = next(p for p in PRODUCTS if p.pid == event["pid"])
            event["text"] = stock_text(product, result.price, now)
        elif state["products"][str(event["pid"])]["status"] in {"available", "sold_out"}:
            event["status"] = "cancelled"
            store.save(state)
            continue
        event["status"] = "sending"
        store.save(state)  # If this fails, Telegram is never called.
        delivery = notifier.send(event["text"])
        event.update(status=delivery.status, receipt=delivery.receipt,
                     next_attempt=now + delivery.retry_after)
        store.save(state)
        log("telegram_delivery", pid=event["pid"], kind=event["kind"], status=delivery.status)
    # Keep pending/uncertain receipts; prune only old completed messages.
    state["outbox"] = {key: event for key, event in state["outbox"].items()
                       if event["status"] not in {"sent", "cancelled"} or now - event["created_at"] < 7 * 86400}
    store.save(state)
    degraded = (any(p["status"] in {"unknown", "uninitialized"} for p in state["products"].values()) or
                any(e["status"] in {"uncertain", "pending", "sending"} for e in state["outbox"].values()))
    return state, 2 if degraded else 0
