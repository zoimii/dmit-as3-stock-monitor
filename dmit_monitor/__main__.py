import argparse
import os
import sys
from pathlib import Path

from .http import SafeError
from .monitor import log, run
from .official import PRODUCTS, OfficialProbe
from .storage import FileStore, GitHubStore
from .telegram import Telegram


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["check", "initialize", "telegram-test", "inspect"], default="check")
    parser.add_argument("--local-state", help="Local use only; run a single process at a time")
    parser.add_argument("--force", action="store_true", help="Ignore persistent backoff for manual diagnosis")
    args = parser.parse_args()
    if args.mode == "inspect":
        for product in PRODUCTS:
            result = OfficialProbe().check(product)
            log("inspect_no_notification", pid=product.pid, status=result.status, reason=result.reason)
        return 0
    notifier = Telegram(os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", ""))
    if args.mode == "telegram-test":
        delivery = notifier.send("[DMIT 监控通知测试，非补货]\nTelegram 通道已连接。此消息不表示任何套餐有货。")
        log("telegram_test", status=delivery.status)
        return 0 if delivery.status == "sent" else 2
    store = FileStore(args.local_state) if args.local_state else GitHubStore(
        os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_SHA", ""))
    if args.mode == "initialize":
        store.initialize()
        log("state_initialized_no_stock_claim")
        return 0
    state, code = run(store, notifier, force=args.force)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as out:
            out.write("## DMIT 官方检查\n\n| PID | 套餐 | 结果 | 原因 |\n|---|---|---|---|\n")
            for product in PRODUCTS:
                item = state["products"][str(product.pid)]
                out.write(f"| {product.pid} | {product.name} | {item['status']} | {item['reason']} |\n")
            out.write("\nunknown 表示无法验证，不表示有货。403、验证码、模板变化均不触发补货。\n")
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SafeError as exc:
        log("fatal", reason=str(exc))
        sys.exit(1)
    except Exception as exc:
        # Do not leak HTTP URLs (Telegram token), payloads, cookies or credentials.
        log("fatal", reason="unexpected_error_requires_review", error_type=type(exc).__name__)
        sys.exit(1)
