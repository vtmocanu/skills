#!/usr/bin/env python3
"""Tests for the cross-process skills refresh lock."""

from __future__ import annotations

import multiprocessing
import os
import tempfile
import time
from pathlib import Path

from refresh_skills import refresh


def concurrent_worker(lock_path: str, log_path: str, barrier: multiprocessing.synchronize.Barrier) -> None:
    def slow_runner(*args: str) -> int:
        pid = os.getpid()
        with Path(log_path).open("a", encoding="utf-8") as log:
            log.write(f"start {pid} {args[0]}\n")
            log.flush()
        time.sleep(0.1)
        with Path(log_path).open("a", encoding="utf-8") as log:
            log.write(f"end {pid} {args[0]}\n")
        return 0

    barrier.wait()
    raise SystemExit(refresh(["required"], [], Path(lock_path), runner=slow_runner))


def test_serializes_whole_refreshes() -> None:
    with tempfile.TemporaryDirectory(prefix="skills-refresh-test.") as temp_dir:
        temp = Path(temp_dir)
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        processes = [
            context.Process(
                target=concurrent_worker,
                args=(str(temp / "refresh.lock"), str(temp / "calls.log"), barrier),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            assert process.exitcode == 0

        lines = (temp / "calls.log").read_text(encoding="utf-8").splitlines()
        pids = [line.split()[1] for line in lines]
        groups = [pid for index, pid in enumerate(pids) if index == 0 or pid != pids[index - 1]]
        assert len(lines) == 8
        assert len(set(pids)) == 2
        assert len(groups) == 2, f"refreshes interleaved: {lines}"


def test_add_targets_claude_and_the_universal_skill_store() -> None:
    calls: list[tuple[str, ...]] = []

    def fake_runner(*args: str) -> int:
        calls.append(args)
        return 0

    with tempfile.TemporaryDirectory(prefix="skills-refresh-test.") as temp_dir:
        result = refresh(
            ["required"],
            [],
            Path(temp_dir) / "refresh.lock",
            runner=fake_runner,
        )

    assert result == 0
    assert calls == [
        (
            "add",
            "required",
            "-a",
            "claude-code",
            "codex",
            "--skill",
            "*",
            "-g",
            "-y",
        ),
        ("update", "-g", "-p"),
    ]


def test_optional_failure_is_best_effort() -> None:
    calls: list[tuple[str, ...]] = []

    def fake_runner(*args: str) -> int:
        calls.append(args)
        return 7 if len(args) > 1 and args[1] == "optional" else 0

    with tempfile.TemporaryDirectory(prefix="skills-refresh-test.") as temp_dir:
        result = refresh(
            ["required"],
            ["optional"],
            Path(temp_dir) / "refresh.lock",
            runner=fake_runner,
        )

    assert result == 0
    assert calls == [
        (
            "add",
            "required",
            "-a",
            "claude-code",
            "codex",
            "--skill",
            "*",
            "-g",
            "-y",
        ),
        ("update", "-g", "-p"),
        (
            "add",
            "optional",
            "-a",
            "claude-code",
            "codex",
            "--skill",
            "*",
            "-g",
            "-y",
        ),
    ]


if __name__ == "__main__":
    test_serializes_whole_refreshes()
    test_add_targets_claude_and_the_universal_skill_store()
    test_optional_failure_is_best_effort()
    print("refresh-skills tests: ok")
