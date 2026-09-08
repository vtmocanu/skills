#!/usr/bin/env python3
"""Serialize a rolling skills refresh shared by Claude Code and Codex."""

from __future__ import annotations

import argparse
import fcntl
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path


DEFAULT_SOURCE = "https://github.com/vtmocanu/skills"
CLI = ("npx", "-y", "skills@latest")
ADD_ARGS = ("-a", "claude-code", "--skill", "*", "-g", "-y")
Runner = Callable[..., int]


def run(*args: str) -> int:
    return subprocess.run((*CLI, *args), check=False).returncode


def refresh(
    sources: Sequence[str],
    best_effort_sources: Sequence[str],
    lock_path: Path,
    runner: Runner = run,
) -> int:
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    # The caller's cwd is inherited deliberately: `update -p` refreshes the
    # repository whose session triggered this user-level hook.
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)

        result = 0
        for source in sources:
            source_result = runner("add", source, *ADD_ARGS)
            if result == 0 and source_result != 0:
                result = source_result

        if result == 0:
            result = runner("update", "-g", "-p")

        for source in best_effort_sources:
            source_result = runner("add", source, *ADD_ARGS)
            if source_result != 0:
                print(f"skills refresh: optional source failed: {source}", file=sys.stderr)

        return result


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help=f"required source to refresh; repeatable (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--best-effort-source",
        action="append",
        default=[],
        help="optional source whose failure does not fail the refresh; repeatable",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    sources = args.source or [DEFAULT_SOURCE]
    lock_path = Path.home() / ".agents" / ".skills-refresh.lock"
    return refresh(sources, args.best_effort_source, lock_path)


if __name__ == "__main__":
    raise SystemExit(main())
