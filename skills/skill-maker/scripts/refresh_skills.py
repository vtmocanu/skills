#!/usr/bin/env python3
"""Stage Vercel skill installs and publish readable files for running agents."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile

from publish_skills import atomic_write, inventory, preflight, project_claude, publish


DEFAULT_SOURCE = "https://github.com/vtmocanu/skills"
CLI = ("npx", "-y", "skills@latest")
ADD_ARGS = ("-a", "claude-code", "codex", "-y")
Runner = Callable[..., int]
SUPPORTED_SOURCES = {"github", "git", "gitlab", "local"}
INSTALL_TIMEOUT_SECONDS = 120


def run(*args: str, cwd: Path) -> int:
    # Project-scoped add must not change the caller's global selection state.
    # HOME, credentials, and the package manager's cache remain unchanged.
    environment = dict(os.environ, XDG_STATE_HOME=str(cwd / ".state"), DO_NOT_TRACK="1")
    with subprocess.Popen(
        (*CLI, *args), cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
    ) as process:
        try:
            output, _ = process.communicate(timeout=INSTALL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            # npx can spawn npm/node/git children. Kill the owned process group
            # so a child cannot keep the output pipe open or outlive staging.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            print(f"skills refresh: installer timed out after {INSTALL_TIMEOUT_SECONDS}s", file=sys.stderr)
            return 1
        if process.returncode:
            print(output[-3000:], file=sys.stderr)
        return process.returncode


def read_lock(path: Path, version: int) -> dict:
    if not path.exists():
        return {"version": version, "skills": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != version or not isinstance(data.get("skills"), dict):
        raise ValueError(f"unsupported skills lockfile schema: {path}")
    if any(not isinstance(entry, dict) for entry in data["skills"].values()):
        raise ValueError(f"invalid skill records in lockfile: {path}")
    pending = data.get("refreshPending", {})
    if not isinstance(pending, dict) or any(not isinstance(value, str) for value in pending.values()):
        raise ValueError(f"invalid pending refresh records in lockfile: {path}")
    return data


def write_lock(path: Path, data: dict) -> None:
    atomic_write(path, (json.dumps(data, indent=2) + "\n").encode(),
                 (path.stat().st_mode & 0o777) if path.exists() else 0o600)


def safe_name(name: str) -> str:
    # Fail visibly rather than guessing a display-name-to-directory mapping.
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name) or ".." in name:
        raise ValueError(f"unsupported installed skill name: {name!r}")
    return name


def source_for(entry: dict, project: Path) -> str:
    if entry.get("sourceType") not in SUPPORTED_SOURCES:
        raise ValueError(f"unsupported skill source type: {entry.get('sourceType')!r}")
    source = entry.get("sourceUrl") or entry.get("source")
    if not isinstance(source, str) or not source or source.startswith("-"):
        raise ValueError("lockfile has no usable skill source")
    if entry["sourceType"] == "local":
        source = str((project / source).resolve())
    if entry.get("ref"):
        source += "#" + entry["ref"]
    return source


def source_key(source: str) -> str:
    """Compare configured Git spellings, without implementing source discovery."""
    base, separator, ref = source.partition("#")
    base = re.sub(r"^git@([^:]+):", r"\1/", base)
    base = re.sub(r"^(?:https?|ssh)://(?:git@)?", "", base).rstrip("/")
    if base.endswith(".git"):
        base = base[:-4]
    if re.fullmatch(r"[^./:]+/[^/]+", base):
        base = "github.com/" + base
    return base + (separator + ref if separator else "")


def staged_entries(stage: Path, destination: Path) -> dict:
    lock = read_lock(stage / "skills-lock.json", 1)
    if not lock["skills"]:
        raise ValueError("installer returned no tracked skills; live files were retained")
    for name, entry in lock["skills"].items():
        safe_name(name)
        source_for(entry, stage)
        if not isinstance(entry.get("computedHash"), str) or not entry["computedHash"]:
            raise ValueError(f"installer omitted the content hash for {name}")
        if entry["sourceType"] == "local":
            entry["source"] = os.path.relpath((stage / entry["source"]).resolve(), destination)
    return lock["skills"]


def global_entry(entry: dict, previous: dict, changed: bool, project: Path) -> dict:
    """Adapt Vercel's project metadata to its v3 global lock schema.

    GitHub global installs normally store a Git tree SHA, while project installs
    store Vercel's SHA-256 content hash. Keep a known SHA for unchanged files;
    otherwise retain the content hash. A direct global CLI check may then offer
    one redundant reinstall to restore its Git tree SHA, never a false clean.
    """
    result = {key: value for key, value in entry.items() if key != "computedHash"}
    result["sourceUrl"] = entry.get("sourceUrl") or (
        "https://github.com/" + entry["source"] + ".git"
        if entry["sourceType"] == "github" else source_for(entry, project).partition("#")[0]
    )
    if entry["sourceType"] == "local":
        result["source"] = str((project / entry["source"]).resolve())
    result["skillFolderHash"] = (
        previous["skillFolderHash"]
        if not changed and previous.get("skillFolderHash") else entry["computedHash"]
    )
    if previous.get("pluginName"):
        result["pluginName"] = previous["pluginName"]
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    result["installedAt"] = previous.get("installedAt", now)
    result["updatedAt"] = now
    return result


def install(
    source: str, names: Sequence[str], store: Path, claude: Path,
    lock_path: Path, global_scope: bool, project: Path, runner: Runner,
) -> set[str]:
    if not source or source.startswith("-"):
        raise ValueError("invalid skill source")
    with tempfile.TemporaryDirectory(prefix="skills-refresh-") as temporary:
        # Relative lock sources must use the same physical cwd as the installer
        # (macOS exposes /var and /private/var aliases for temporary directories).
        stage = Path(temporary).resolve()
        args = ("add", source, *ADD_ARGS, "--skill", *names)
        if names != ("*",):
            args += ("--full-depth",)
        if runner(*args, cwd=stage):
            raise ValueError(f"staging failed for {source}; live files were retained")
        entries = staged_entries(stage, project)
        lock = read_lock(lock_path, 3 if global_scope else 1)
        pending = lock.get("refreshPending", {})
        plans = []
        # Validate the whole returned batch before publishing anything. The CLI
        # can exit zero after a partial install; every recorded skill must have
        # canonical files and a valid entrypoint.
        for name in entries:
            canonical, projection = store / name, claude / name
            files = inventory(stage / ".agents" / "skills" / name)
            resumed = pending.get(name) == source_key(source_for(entries[name], project))
            if name not in lock["skills"] and not resumed and (
                os.path.lexists(canonical) or os.path.lexists(projection)
            ):
                raise ValueError(f"refusing to overwrite an untracked skill: {name}")
            preflight(canonical, files)
            copy_projection = projection.exists() and not projection.is_symlink() and (
                projection.resolve() != canonical.resolve()
            )
            if copy_projection:
                preflight(projection, files)
            plans.append((name, canonical, projection, copy_projection, files))
        # Reserve ownership before publishing a fresh skill. If publication or
        # lock commit is interrupted, retry can distinguish our partial install
        # from an authored, untracked directory. Do not claim it is installed:
        # Vercel still sees it absent from `skills` until publication succeeds.
        fresh = {name: source_key(source_for(entries[name], project))
                 for name in entries if name not in lock["skills"]}
        if fresh:
            lock["refreshPending"] = pending | fresh
            write_lock(lock_path, lock)
        changed = 0
        for name, canonical, projection, copy_projection, files in plans:
            count = publish(canonical, files)
            if copy_projection:
                count += publish(projection, files)
            else:
                project_claude(canonical, projection)
            changed += count
            entry = entries[name]
            lock["skills"][name] = (
                global_entry(entry, lock["skills"].get(name, {}), count > 0, project)
                if global_scope else entry
            )
            lock.get("refreshPending", {}).pop(name, None)
        if lock.get("refreshPending") == {}:
            lock.pop("refreshPending")
        # No pruning: an old session may still refer to removed skills or files.
        write_lock(lock_path, lock)
        print(f"skills refresh: {len(plans)} skills checked, {changed} files published")
        return set(entries)


def refresh(
    sources: Sequence[str],
    best_effort_sources: Sequence[str],
    lock_path: Path,
    runner: Runner = run, *, home: Path | None = None, project: Path | None = None,
    global_lock: Path | None = None, claude: Path | None = None,
) -> int:
    home = home or Path.home()
    project = (project or Path.cwd()).resolve()
    global_lock = global_lock or home / ".agents" / ".skill-lock.json"
    claude = claude or home / ".claude" / "skills"
    store = home / ".agents" / "skills"
    def resolve_catalog(source: str) -> str:
        if source.startswith((".", "/", "~")):
            return str((project / Path(source).expanduser()).resolve())
        return source

    sources = tuple(resolve_catalog(source) for source in sources)
    best_effort_sources = tuple(resolve_catalog(source) for source in best_effort_sources)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        result, covered = 0, set()
        initial = read_lock(global_lock, 3)
        configured = {source_key(source) for source in (*sources, *best_effort_sources)}
        catalogs = [(s, False) for s in sources] + [(s, True) for s in best_effort_sources]
        for source, optional in catalogs:
            try:
                covered |= install(source, ("*",), store, claude, global_lock, True, project, runner)
            except (OSError, ValueError) as error:
                print(f"skills refresh: {'optional source: ' if optional else ''}{error}", file=sys.stderr)
                if not optional:
                    result = 1
        # Refresh other tracked global skills and the caller's project. Never
        # run a global installer or update command against either live store.
        for data, scope, target_store, target_claude, target_lock in (
            (initial, True, store, claude, global_lock),
            (None, False, project / ".agents" / "skills",
             project / ".claude" / "skills", project / "skills-lock.json"),
        ):
            if data is None:
                try:
                    data = read_lock(target_lock, 1)
                except (OSError, ValueError) as error:
                    print(f"skills refresh: {error}", file=sys.stderr)
                    result = 1
                    continue
            groups: dict[str, list[str]] = {}
            for name, entry in data["skills"].items():
                try:
                    safe_name(name)
                    source = source_for(entry, project)
                    if scope and (name in covered or source_key(source) in configured):
                        continue
                    groups.setdefault(source, []).append(name)
                except ValueError as error:
                    print(f"skills refresh: {name}: {error}", file=sys.stderr)
                    result = 1
            for source, names in groups.items():
                try:
                    install(source, tuple(names), target_store, target_claude, target_lock, scope, project, runner)
                except (OSError, ValueError) as error:
                    print(f"skills refresh: {error}", file=sys.stderr)
                    result = 1
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
    home = Path.home()
    state = os.environ.get("XDG_STATE_HOME")
    global_lock = Path(state) / "skills" / ".skill-lock.json" if state else home / ".agents" / ".skill-lock.json"
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude") / "skills"
    try:
        return refresh(args.source or [DEFAULT_SOURCE], args.best_effort_source,
                       home / ".agents" / ".skills-refresh.lock", global_lock=global_lock, claude=claude)
    except (OSError, ValueError) as error:
        print(f"skills refresh: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
