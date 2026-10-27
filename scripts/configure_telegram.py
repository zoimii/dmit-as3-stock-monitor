#!/usr/bin/env python3
"""Run personally in a terminal. Hidden input -> gh secret set STDIN, never chat/files/argv."""
import argparse
import getpass
import json
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dmit_monitor.http import SafeError, Transport
from dmit_monitor.telegram import Telegram


def gh(arguments, value=None):
    result = subprocess.run(["gh", *arguments], input=value, text=True, capture_output=True)
    if result.returncode:
        raise SafeError("GitHub 操作失败；请检查登录和该仓库的管理权限。未输出密钥或服务器响应。")
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="OWNER/REPO (no secrets)")
    parser.add_argument("--discover", action="store_true", help="Discover your private chat through a unique /start message")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        raise SafeError("请由你本人在交互终端运行；拒绝非隐藏输入。")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        raise SafeError("仓库格式应为 OWNER/REPO。")
    gh(["api", f"repos/{args.repository}", "--jq", ".full_name"])
    token = getpass.getpass("粘贴 BotFather 提供的 bot token（隐藏输入）：").strip()
    transport = Transport({"api.telegram.org"})

    def telegram_api(method):
        result = transport.request("GET", f"https://api.telegram.org/bot{token}/{method}")
        try:
            payload = json.loads(result.body)
            if result.status != 200 or payload.get("ok") is not True:
                raise SafeError("Telegram 凭据或 API 请求未通过验证。")
            return payload["result"]
        except (ValueError, KeyError):
            raise SafeError("Telegram 返回无法验证的响应。") from None

    # Validate token format before putting it into any request URL.
    Telegram(token, "1")
    me = telegram_api("getMe")
    if args.discover:
        if telegram_api("getWebhookInfo").get("url"):
            raise SafeError("该 bot 已使用 webhook；不会修改它。请不用 --discover 并输入已知 chat ID。")
        nonce = "dmit_" + secrets.token_hex(8)
        print(f"请在 Telegram 打开 @{me['username']}，发送：/start {nonce}")
        print("只识别本次随机口令的私聊，等待最多 3 分钟；不会输出聊天内容。")
        chat_id = None
        for _ in range(36):
            updates = telegram_api("getUpdates?timeout=0&limit=100")
            matches = {str(u["message"]["chat"]["id"]) for u in updates
                       if u.get("message", {}).get("text") == f"/start {nonce}" and
                       u["message"].get("chat", {}).get("type") == "private"}
            if len(matches) == 1:
                chat_id = matches.pop()
                break
            time.sleep(5)
        if chat_id is None:
            raise SafeError("未找到本次 /start 私聊。请重新运行，或输入已知 chat ID。")
    else:
        chat_id = getpass.getpass("输入目标 chat ID（隐藏输入；请先给 bot 发 /start）：").strip()
    Telegram(token, chat_id)  # Validate both values locally.
    # Pause during credential rotation, avoiding partially updated secret pairs.
    gh(["variable", "set", "MONITOR_ENABLED", "--repo", args.repository, "--body", "false"])
    gh(["secret", "set", "TELEGRAM_BOT_TOKEN", "--repo", args.repository], token)
    gh(["secret", "set", "TELEGRAM_CHAT_ID", "--repo", args.repository], chat_id)
    print("两个 Secrets 已写入 GitHub，未写入本地文件；调度仍关闭。")
    print(f"下一步：python3 scripts/deploy.py --repository {args.repository} --activate")


if __name__ == "__main__":
    try:
        main()
    except (SafeError, KeyboardInterrupt, EOFError) as exc:
        print(str(exc) if isinstance(exc, SafeError) else "已取消，未显示任何凭据。", file=sys.stderr)
        sys.exit(1)
    except Exception:
        print("配置未完成；请检查网络和权限。为避免泄露凭据，未输出原始错误。", file=sys.stderr)
        sys.exit(1)
