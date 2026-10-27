#!/usr/bin/env python3
"""Create a dedicated public repository; optionally verify and enable the monitor."""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def command(args, check=True, capture=True):
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=capture)
    if check and result.returncode:
        raise RuntimeError("操作未成功：" + " ".join(args[:3]) + "。请检查终端登录、网络和仓库权限。")
    return result


def gh(args, **kwargs):
    return command(["gh", *args], **kwargs)


def get_runs(repo):
    result = gh(["run", "list", "--repo", repo, "--workflow", "stock-monitor.yml", "--event", "workflow_dispatch",
                 "--limit", "30", "--json", "databaseId,displayTitle,headBranch,headSha"])
    return json.loads(result.stdout)


def dispatch(repo, mode):
    previous = {r["databaseId"] for r in get_runs(repo)}
    gh(["workflow", "run", "stock-monitor.yml", "--repo", repo, "--ref", "main", "-f", f"mode={mode}"])
    for _ in range(60):
        matches = [r for r in get_runs(repo) if r["databaseId"] not in previous and
                   r["displayTitle"] == f"DMIT {mode}" and r["headBranch"] == "main"]
        if matches:
            run_id = max(r["databaseId"] for r in matches)
            print(f"{mode}: https://github.com/{repo}/actions/runs/{run_id}", flush=True)
            completed = gh(["run", "watch", str(run_id), "--repo", repo, "--exit-status"], check=False, capture=False)
            return completed.returncode == 0
        time.sleep(2)
    raise RuntimeError("GitHub 未及时返回本次运行。请到 Actions 确认；不会自动重复触发。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", help="OWNER/REPO; default: your account/dmit-as3-stock-monitor")
    parser.add_argument("--activate", action="store_true", help="After secrets exist: test Telegram, initialize once, enable schedule")
    args = parser.parse_args()
    command([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], capture=False)
    login = gh(["api", "user", "--jq", ".login"]).stdout.strip()
    repo = args.repository or f"{login}/dmit-as3-stock-monitor"
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise RuntimeError("仓库格式应为 OWNER/REPO。")
    existing = gh(["repo", "view", repo, "--json", "nameWithOwner,visibility,defaultBranchRef"], check=False)
    if args.activate:
        if existing.returncode:
            raise RuntimeError("仓库尚未创建；先不带 --activate 运行。")
        metadata = json.loads(existing.stdout)
        if metadata["visibility"] != "PUBLIC" or metadata["defaultBranchRef"]["name"] != "main":
            raise RuntimeError("本部署要求专用公开仓库、默认分支 main。")
        names = {s["name"] for s in json.loads(gh(["secret", "list", "--repo", repo, "--json", "name"]).stdout)}
        if not {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"} <= names:
            raise RuntimeError("请先运行 configure_telegram.py 安全设置两个 Secrets。")
        if not dispatch(repo, "telegram-test"):
            raise RuntimeError("云端 Telegram 通知测试未成功；未启用调度。")
        branch = gh(["api", f"repos/{repo}/git/ref/heads/monitor-state"], check=False)
        if branch.returncode:
            if not dispatch(repo, "initialize"):
                raise RuntimeError("状态初始化失败；未启用调度。")
        else:
            state = gh(["api", f"repos/{repo}/contents/state.json?ref=monitor-state"], check=False)
            if state.returncode:
                raise RuntimeError("状态分支存在但文件缺失；请检查后手动 initialize，避免重置去重记录。")
        gh(["variable", "set", "MONITOR_ENABLED", "--repo", repo, "--body", "true"])
        print("每 5 分钟调度已启用；继续执行首次官方验证。", flush=True)
        if not dispatch(repo, "check"):
            print("首次检查未通过（可能是官方 403/无法验证）；调度仍开启。请查看该运行记录；这不表示有货。")
        print(f"Actions: https://github.com/{repo}/actions")
        return
    if existing.returncode == 0:
        raise RuntimeError(f"https://github.com/{repo} 已存在；未覆盖。已部署项目请用 --activate。")
    if (ROOT / ".git").exists():
        remotes = command(["git", "remote"]).stdout.strip()
        if remotes:
            raise RuntimeError("本地已有远端；请检查，避免向错误仓库推送。")
    else:
        command(["git", "init", "--initial-branch=main"])
    command(["git", "add", "dmit_monitor", "tests", "scripts", ".github", "README.md", ".gitignore"])
    command(["git", "-c", "user.name=DMIT Monitor Setup", "-c", "user.email=monitor@users.noreply.github.com",
             "commit", "-m", "Add strict official-only DMIT inventory monitor"], check=False)
    gh(["repo", "create", repo, "--public", "--source", str(ROOT), "--remote", "origin", "--push",
        "--description", "Official-only DMIT AS3 TINY/Pocket inventory monitor with Telegram notifications"], capture=False)
    gh(["variable", "set", "MONITOR_ENABLED", "--repo", repo, "--body", "false"])
    print(f"仓库已创建：https://github.com/{repo}；调度关闭，等待 Secrets。")
    print(f"下一步：python3 scripts/configure_telegram.py --repository {repo} --discover")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, KeyboardInterrupt) as exc:
        print(str(exc) or "已取消。", file=sys.stderr)
        sys.exit(1)
