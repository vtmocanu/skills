#!/usr/bin/env python3
"""Validate agent skill files for dot-ai / Claude Code.

Checks each skills/*.md (flat) and skills/*/SKILL.md (folder) for:
  - a YAML frontmatter block delimited by ---
  - non-empty `name`: lowercase letters/digits/hyphens, <= 64 chars,
    no reserved substrings ('anthropic', 'claude'); matches the filename
    for flat skills
  - non-empty single-line `description`, <= 1024 chars (rejects multi-line
    YAML block scalars, which render empty downstream)

Exits non-zero with a report if any skill is invalid.

Usage: python3 scripts/validate_skills.py [skills_dir]   (default: skills)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9-]{1,64}$")
RESERVED = ("anthropic", "claude")
BLOCK_SCALARS = {">", ">-", ">+", "|", "|-", "|+"}


def find_skill_files(root: Path) -> list[Path]:
    return sorted(root.glob("*.md")) + sorted(root.glob("*/SKILL.md"))


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
        for r in RESERVED:
            if r in name.lower():
                errs.append(f"`name` contains reserved substring {r!r}")
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
    root = Path(argv[1]) if len(argv) > 1 else Path("skills")
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
