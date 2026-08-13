#!/usr/bin/env python3
"""Guard that every SKILL.md in a subpath bundle is actually installable.

The ``agent-kit`` bundle is installed via the ``skills/agent-kit`` subpath
(``npx skills add vtmocanu/skills/skills/agent-kit``). Under a subpath, npx
walks the subpath dir only one level deep, so a skill nested deeper (e.g.
``skills/agent-kit/prd/prd-create``) is discovered only because
``skills/agent-kit/.claude-plugin/plugin.json`` registers its folder: each
manifest entry ``./prd/<x>`` registers ``./prd`` (npx takes the entry's parent
dir) and then walks that dir one level deep. Registering a folder covers every
skill directly inside it, so new skills dropped into an already-registered
folder auto-join with no manifest edit.

The gap this guards: adding a NEW nested folder (``skills/agent-kit/review/``)
that no manifest entry registers. Those skills exist on disk but the subpath
install silently drops them. This check recomputes exactly what the subpath
install would discover and fails if any on-disk SKILL.md is not reachable.

Usage: python3 scripts/check_bundle_coverage.py [bundle_dir]
       (default bundle_dir: skills/agent-kit)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def registered_dirs(bundle: Path) -> set[Path]:
    """Dirs npx walks one level deep under the subpath: the bundle root itself,
    plus the parent dir of every plugin.json skills[] entry."""
    dirs = {bundle.resolve()}
    manifest = bundle / ".claude-plugin" / "plugin.json"
    if manifest.is_file():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for entry in data.get("skills", []):
            if not isinstance(entry, str):
                continue
            # npx: dirname(join(bundle, entry)) -> registered, walked depth 1.
            registered = (bundle / entry).resolve().parent
            if bundle.resolve() in registered.parents or registered == bundle.resolve():
                dirs.add(registered)
    return dirs


def reachable_skill_dirs(bundle: Path) -> set[Path]:
    """Skill dirs the subpath install would find: direct children (with a
    SKILL.md) of each registered dir."""
    found = set()
    for d in registered_dirs(bundle):
        if not d.is_dir():
            continue
        for child in d.iterdir():
            if child.is_dir() and (child / "SKILL.md").is_file():
                found.add(child.resolve())
    return found


def present_skill_dirs(bundle: Path) -> set[Path]:
    """Every dir under the bundle that actually contains a SKILL.md."""
    return {p.parent.resolve() for p in bundle.rglob("SKILL.md")}


def main(argv: list[str]) -> int:
    bundle = Path(argv[1]) if len(argv) > 1 else Path("skills/agent-kit")
    if not bundle.is_dir():
        print(f"error: bundle dir {bundle} not found", file=sys.stderr)
        return 2

    present = present_skill_dirs(bundle)
    reachable = reachable_skill_dirs(bundle)
    orphaned = sorted(present - reachable)

    if orphaned:
        print(f"FAIL: {len(orphaned)} skill(s) under {bundle} are NOT installable "
              f"via the subpath bundle (npx would silently drop them):")
        for d in orphaned:
            rel = d.relative_to(bundle.resolve())
            print(f"  - {bundle}/{rel}/SKILL.md")
        print(f"\nFix: add an entry to {bundle}/.claude-plugin/plugin.json that "
              f"registers each orphan's folder, e.g. \"./<folder>/<any-skill>\" "
              f"(registering a folder covers every skill directly inside it).")
        return 1

    print(f"ok: all {len(present)} skill(s) under {bundle} are reachable by the "
          f"subpath install ({len(reachable)} discoverable dirs).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
