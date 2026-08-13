#!/usr/bin/env python3
"""Validate agent skill files for dot-ai / Claude Code.

Skills live under the ``skills/`` container as ``skills/<name>/SKILL.md`` (the
``agent-team`` skill grouped under ``skills/agent-kit/`` and the ``prd-*``
skills one level deeper under ``skills/agent-kit/prd/``); ``npx skills``
discovers that container to depth 3. ``retired/`` at the repo root is
intentionally excluded.

Checks each skill for:
  - a YAML frontmatter block delimited by ---
  - non-empty `name`: lowercase letters/digits/hyphens, <= 64 chars,
    matching the skill's folder name
  - non-empty single-line `description`, <= 1024 chars (rejects multi-line
    YAML block scalars, which render empty downstream)

Repository meta files (README, CHANGELOG, governance docs) and example docs
(``*.example.md``, e.g. ``CLAUDE.example.md``) are not skills and are skipped.

Exits non-zero with a report if any skill is invalid.

Usage: python3 scripts/validate_skills.py [root_dir]   (default: .)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9-]{1,64}$")
BLOCK_SCALARS = {">", ">-", ">+", "|", "|-", "|+"}
META = {
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
}


def find_skill_files(root: Path) -> list[Path]:
    flat = [
        p
        for p in sorted(root.glob("*.md"))
        if p.name not in META and not p.name.endswith(".example.md")
    ]
    skills_dir = root / "skills"
    folder = sorted(skills_dir.rglob("SKILL.md")) if skills_dir.is_dir() else []
    return flat + folder


def parse_frontmatter(text: str) -> dict[str, str] | None:
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    fm: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip()
    return fm


def skill_id(path: Path) -> str:
    return path.parent.name if path.name == "SKILL.md" else path.stem


def validate(path: Path) -> list[str]:
    fm = parse_frontmatter(path.read_text(encoding="utf-8"))
    if fm is None:
        return ["missing or malformed YAML frontmatter"]

    errs: list[str] = []

    name = fm.get("name", "")
    if not name:
        errs.append("missing `name`")
    else:
        if not NAME_RE.match(name):
            errs.append(f"`name` must match [a-z0-9-]{{1,64}}: {name!r}")
        expected = skill_id(path)
        if name != expected:
            errs.append(f"`name` {name!r} != expected {expected!r}")

    desc = fm.get("description", "")
    if not desc:
        errs.append("missing `description`")
    elif desc in BLOCK_SCALARS:
        errs.append("`description` must be a single-line scalar, not a multi-line YAML block")
    elif len(desc) > 1024:
        errs.append(f"`description` exceeds 1024 chars ({len(desc)})")

    return errs


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(".")
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    files = find_skill_files(root)
    if not files:
        print(f"error: no skill files found under {root}", file=sys.stderr)
        return 2

    failed = 0
    for f in files:
        errs = validate(f)
        if errs:
            failed += 1
            print(f"FAIL {f}")
            for e in errs:
                print(f"  - {e}")
        else:
            print(f"ok   {f}")

    print(f"\n{len(files) - failed}/{len(files)} skills valid")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
