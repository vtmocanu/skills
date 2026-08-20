#!/usr/bin/env python3
"""memory-scan.py — enumerate every CLAUDE.md in scope and size it.

Scope (Claude Code's own memory precedence):
  * the project chain: CLAUDE.md, .claude/CLAUDE.md and CLAUDE.local.md in the
    cwd and every parent directory up to filesystem root
  * the user memory: ~/.claude/CLAUDE.md
  * the managed/enterprise memory, if present (macOS + Linux paths)
Then it follows @import lines recursively (depth-guarded, cycle-safe) and sizes
every imported file too.

Sizes are EXACT in bytes and lines. Tokens have no offline tokenizer here, so
this reports an APPROXIMATION (bytes / 4) clearly labelled as such; treat
/context as the authoritative token source and reconcile against it. Thresholds
flag on the approximation: any single file over 5k approx-tokens, and a total
over 10k approx-tokens.

Usage: memory-scan.py            # audit the current working directory's scope
Reads only; writes nothing.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Claude Code inline-import syntax: an @path token pulls that file in. Match a
# leading-or-space @ followed by a path; skip @ inside code spans and emails is
# best-effort — the caller verifies against /context.
IMPORT_RE = re.compile(r"(?:^|\s)@([~./][^\s`]+|[A-Za-z0-9_][^\s`]*/[^\s`]+)")
APPROX_DIVISOR = 4          # bytes per approx token (English/markdown ballpark)
SINGLE_FLAG = 5_000         # approx-token flag for one file
TOTAL_FLAG = 10_000         # approx-token flag for the total


def approx_tokens(nbytes: int) -> int:
    return round(nbytes / APPROX_DIVISOR)


def resolve_import(raw: str, base: Path) -> Path | None:
    raw = raw.strip()
    if raw.startswith("~"):
        return Path(os.path.expanduser(raw))
    p = Path(raw)
    if p.is_absolute():
        return p
    return (base.parent / p).resolve()


def scope_files(cwd: Path, home: Path) -> list[tuple[str, Path]]:
    """Return (origin, path) for every in-scope memory file that exists."""
    out: list[tuple[str, Path]] = []
    seen: set[Path] = set()

    def add(origin: str, p: Path) -> None:
        rp = p.resolve()
        if p.is_file() and rp not in seen:
            seen.add(rp)
            out.append((origin, p))

    # Project chain: cwd upward to root.
    d = cwd.resolve()
    while True:
        for name in ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md"):
            add("project", d / name)
        if d == d.parent:
            break
        d = d.parent

    # User memory.
    add("user", home / ".claude" / "CLAUDE.md")

    # Managed / enterprise memory (macOS, then Linux).
    add("enterprise", Path("/Library/Application Support/ClaudeCode/CLAUDE.md"))
    add("enterprise", Path("/etc/claude-code/CLAUDE.md"))
    return out


def walk_imports(root_files: list[tuple[str, Path]]) -> list[dict]:
    """Expand @imports depth-first; return a flat record list with depth."""
    records: list[dict] = []
    visited: set[Path] = set()

    def visit(origin: str, path: Path, depth: int) -> None:
        rp = path.resolve()
        if rp in visited or depth > 10:
            return
        visited.add(rp)
        try:
            data = path.read_bytes()
        except OSError as e:
            records.append({"origin": origin, "path": str(path), "depth": depth,
                            "error": str(e)})
            return
        text = data.decode("utf-8", "replace")
        nbytes = len(data)
        records.append({
            "origin": origin, "path": str(path), "depth": depth,
            "bytes": nbytes, "lines": text.count("\n") + 1,
            "approx_tokens": approx_tokens(nbytes),
        })
        for m in IMPORT_RE.finditer(text):
            child = resolve_import(m.group(1), path)
            if child and child.is_file():
                visit(f"{origin}>import", child, depth + 1)

    for origin, p in root_files:
        visit(origin, p, 0)
    return records


def main() -> int:
    cwd = Path.cwd()
    home = Path.home()
    roots = scope_files(cwd, home)
    records = walk_imports(roots)

    files = [r for r in records if "bytes" in r]
    total = sum(r["approx_tokens"] for r in files)

    print(f"cwd: {cwd}")
    print(f"files in scope: {len(files)}")
    print(f"{'ORIGIN':<16} {'APPROX_TOK':>10} {'BYTES':>8} {'LINES':>6}  PATH  [FLAG]")
    for r in records:
        if "error" in r:
            print(f"{r['origin']:<16} {'ERR':>10} {'':>8} {'':>6}  "
                  f"{'  ' * r['depth']}{r['path']}  ({r['error']})")
            continue
        flag = "  <-- >5k" if r["approx_tokens"] > SINGLE_FLAG else ""
        indent = "  " * r["depth"]
        print(f"{r['origin']:<16} {r['approx_tokens']:>10} {r['bytes']:>8} "
              f"{r['lines']:>6}  {indent}{r['path']}{flag}")

    print("-" * 60)
    verdict = "OVER 10k" if total > TOTAL_FLAG else "under 10k"
    print(f"TOTAL approx-tokens: {total}  ({verdict})")
    print(f"(approx = bytes/{APPROX_DIVISOR}; use /context for exact token counts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
