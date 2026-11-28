#!/usr/bin/env python3
"""补刷 mimotion 工作流：按指定日期触发 GitHub Actions。"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Iterable

WORKFLOW_FILE = "run.yml"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_DATES = 50
BEIJING_TZ = dt.timezone(dt.timedelta(hours=8))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="触发『刷步数』工作流补刷指定日期（可多个，支持 A..B 范围）"
    )
    parser.add_argument(
        "dates",
        nargs="+",
        help="日期 YYYY-MM-DD；多个日期用空格分隔；范围写 2026-11-20..2026-11-22",
    )
    parser.add_argument("--repo", help="仓库 owner/repo，默认自动检测")
    parser.add_argument("--ref", help="工作流所在分支，默认仓库默认分支")
    parser.add_argument(
        "--delay", type=float, default=45.0, help="多个日期之间等待秒数（默认 45，避免并发 push 冲突）"
    )
    parser.add_argument(
        "--yes", action="store_true", help="跳过交互确认，直接触发"
    )
    parser.add_argument("--dry-run", action="store_true", help="只校验并打印，不实际触发")
    parser.add_argument(
        "--continue-on-error", action="store_true", help="某个日期触发失败时继续后续日期"
    )
    return parser.parse_args()


def beijing_today() -> dt.date:
    return dt.datetime.now(BEIJING_TZ).date()


def validate_date(value: str) -> dt.date:
    if not DATE_RE.match(value):
        raise ValueError(f"日期格式必须为 YYYY-MM-DD：{value}")
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"无效日期：{value}") from exc
    if parsed > beijing_today():
        raise ValueError(f"不能补刷未来日期：{value}")
    return parsed


def expand_dates(values: Iterable[str]) -> list[str]:
    expanded: list[str] = []
    for value in values:
        if ".." in value:
            start_text, end_text = value.split("..", 1)
            start = validate_date(start_text)
            end = validate_date(end_text)
            if start > end:
                raise ValueError(f"日期范围起点晚于终点：{value}")
            current = start
            while current <= end:
                expanded.append(current.isoformat())
                current += dt.timedelta(days=1)
        else:
            expanded.append(validate_date(value).isoformat())
    if not expanded:
        raise ValueError("至少指定一个日期")
    if len(expanded) > MAX_DATES:
        raise ValueError(f"一次最多补刷 {MAX_DATES} 个日期")
    return list(dict.fromkeys(expanded))


def detect_ref() -> str:
    """Return the branch to dispatch; GitHub dispatch API requires an explicit ref."""
    return "master"


def detect_repo() -> str:
    env_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if env_repo:
        return env_repo
    if shutil.which("gh"):
        try:
            result = subprocess.run(
                ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except (subprocess.CalledProcessError, OSError):
            pass
    raise RuntimeError("无法自动检测仓库，请通过 --repo owner/repo 指定")


def dispatch_via_gh(repo: str, ref: str | None, date: str) -> str:
    cmd = [
        "gh",
        "api",
        "--silent",
        "-X",
        "POST",
        f"repos/{repo}/actions/workflows/{WORKFLOW_FILE}/dispatches",
        "-f",
        "inputs[target_date]=" + date,
    ]
    cmd += ["-f", "ref=" + (ref or detect_ref())]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "未知错误"
        raise RuntimeError(f"gh api 失败：{detail}")
    return "gh api"


def dispatch_via_rest(repo: str, ref: str | None, date: str) -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise RuntimeError("未找到 gh 或 GITHUB_TOKEN/GH_TOKEN，无法触发工作流")
    payload: dict[str, object] = {"ref": ref or detect_ref(), "inputs": {"target_date": date}}
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/dispatches",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 204:
                raise RuntimeError(f"GitHub API 返回 HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"GitHub API HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GitHub API 网络错误：{exc.reason}") from exc
    return "GitHub REST API"


def dispatch(repo: str, ref: str | None, date: str, dry_run: bool) -> str:
    if dry_run:
        return "dry-run"
    if shutil.which("gh"):
        try:
            return dispatch_via_gh(repo, ref, date)
        except RuntimeError as exc:
            if not (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")):
                raise
            print(f"  gh 触发失败，改用 REST API：{exc}", file=sys.stderr)
    return dispatch_via_rest(repo, ref, date)


def main() -> int:
    args = parse_args()
    try:
        dates = expand_dates(args.dates)
        repo = args.repo or detect_repo()
    except (ValueError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    print(f"仓库：{repo}")
    print(f"补刷日期（{len(dates)} 个）：{', '.join(dates)}")
    if args.dry_run:
        print("Dry-run：仅校验，不触发工作流")
    elif not args.yes:
        answer = input("确认触发以上日期？输入 y 继续：").strip().lower()
        if answer not in {"y", "yes"}:
            print("已取消")
            return 0

    failures = 0
    processed = 0
    succeeded = 0
    for index, date in enumerate(dates):
        if index and args.delay > 0 and not args.dry_run:
            time.sleep(args.delay)
        try:
            method = dispatch(repo, args.ref, date, args.dry_run)
            succeeded += 1
            print(f"OK   {date} 已触发（{method}）")
        except RuntimeError as exc:
            failures += 1
            print(f"FAIL {date}：{exc}", file=sys.stderr)
            if not args.continue_on_error:
                print("已停止，剩余日期未触发。", file=sys.stderr)
                break
        finally:
            processed += 1

    print(
        f"完成：成功 {succeeded} 个，失败 {failures} 个"
        f"（已处理 {processed} 个，共 {len(dates)} 个）"
    )
    print(f"查看执行记录：https://github.com/{repo}/actions/workflows/{WORKFLOW_FILE}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
