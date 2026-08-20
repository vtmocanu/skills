#!/usr/bin/env python3
"""memory-scan.py — enumerate every launch-loaded memory file in scope and size it.

Scope, matching Claude Code's own memory loading (docs: code.claude.com/docs/en/memory):
  * managed/enterprise CLAUDE.md, if present (macOS + Linux paths)
  * user memory: ~/.claude/CLAUDE.md
  * project chain: CLAUDE.md and CLAUDE.local.md in the cwd and every ancestor
    up to filesystem root, plus .claude/CLAUDE.md at the cwd (project root only;
    Claude Code does NOT probe .claude/CLAUDE.md at every ancestor)
  * rules: unconditional *.md under .claude/rules/ (project) and ~/.claude/rules/
    (user). Rules with a `paths:` frontmatter key load on demand, not at launch,
    so they are listed but excluded from the launch total.
Then it follows @import lines (bare @README, @package.json, @dir/file.md, ~/…,
and absolute paths) recursively, cycle-safe, capped at Claude Code's real limit
of four hops. Code spans and fenced code blocks are stripped before import
scanning, exactly as Claude Code does, so an @path shown as an example is not
followed.

Sizes are EXACT in bytes and lines. Tokens have no offline tokenizer here, so
this reports an APPROXIMATION (bytes / 4) clearly labelled as such; treat
/context as the authoritative token source and reconcile against it. Thresholds
flag on the approximation: any single file over 5k approx-tokens, and a launch
total over 10k approx-tokens.

Usage: memory-scan.py            # audit the current working directory's scope
Reads only; writes nothing.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# @import token: '@' (at line start or after whitespace) followed by a path run.
# Claude Code accepts bare filenames (@README, @package.json), relative dir
# paths (@docs/x.md), ~/… and absolute paths. The leading-boundary anchor keeps
# emails (foo@bar) from matching; code is stripped before this runs.
IMPORT_RE = re.compile(r"(?:^|(?<=\s))@([~./A-Za-z0-9][^\s`]*)")
# Fenced code blocks (``` or ~~~, any length >= 3) and inline code spans.
FENCE_RE = re.compile(r"(?ms)^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?^[ \t]*\1[ \t]*$")
SPAN_RE = re.compile(r"`[^`]*`")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.S)
PATHS_KEY_RE = re.compile(r"(?m)^\s*paths\s*:")

APPROX_DIVISOR = 4          # bytes per approx token (English/markdown ballpark)
SINGLE_FLAG = 5_000         # approx-token flag for one file
TOTAL_FLAG = 10_000         # approx-token flag for the launch total
MAX_HOPS = 4                # Claude Code's real @import recursion depth


def approx_tokens(nbytes: int) -> int:
    return round(nbytes / APPROX_DIVISOR)


def strip_code(text: str) -> str:
    """Remove fenced blocks and inline code spans, as Claude Code does before
    parsing @imports, so an @path inside a code example is not followed."""
    return SPAN_RE.sub("", FENCE_RE.sub("", text))


def resolve_import(raw: str, base: Path) -> Path | None:
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("~"):
        return Path(os.path.expanduser(raw))
    p = Path(raw)
    if p.is_absolute():
        return p
    return (base.parent / p).resolve()


def has_paths_frontmatter(path: Path) -> bool:
    """True when a rule file carries a `paths:` frontmatter key (path-scoped,
    so it loads on demand rather than at launch)."""
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:8192]
    except OSError:
        return False
    m = FRONTMATTER_RE.match(head)
    return bool(m and PATHS_KEY_RE.search(m.group(1)))


def scope_files(cwd: Path, home: Path) -> list[tuple[str, Path]]:
    """Return (origin, path) for every in-scope CLAUDE.md-family file that exists,
    in Claude Code load order (broad to specific), de-duplicated by real path."""
    out: list[tuple[str, Path]] = []
    seen: set[Path] = set()

    def add(origin: str, p: Path) -> None:
        rp = p.resolve()
        if p.is_file() and rp not in seen:
            seen.add(rp)
            out.append((origin, p))

    # Managed / enterprise memory (macOS, then Linux/WSL).
    add("enterprise", Path("/Library/Application Support/ClaudeCode/CLAUDE.md"))
    add("enterprise", Path("/etc/claude-code/CLAUDE.md"))

    # User memory (added before the ancestor walk so it keeps its `user` origin).
    add("user", home / ".claude" / "CLAUDE.md")

    # .claude/CLAUDE.md is a project-root alternative only, at the cwd.
    add("project", cwd / ".claude" / "CLAUDE.md")

    # Ancestor walk loads only CLAUDE.md and CLAUDE.local.md at each level.
    d = cwd.resolve()
    while True:
        add("project", d / "CLAUDE.md")
        add("project", d / "CLAUDE.local.md")
        if d == d.parent:
            break
        d = d.parent
    return out


def rules_files(cwd: Path, home: Path) -> list[dict]:
    """Size unconditional and path-scoped rules under the two rules dirs."""
    recs: list[dict] = []
    for base, origin in ((cwd / ".claude" / "rules", "project-rule"),
                         (home / ".claude" / "rules", "user-rule")):
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.md")):
            if not p.is_file():
                continue
            conditional = has_paths_frontmatter(p)
            try:
                nbytes = len(p.read_bytes())
            except OSError as e:
                recs.append({"origin": origin, "path": str(p), "error": str(e)})
                continue
            recs.append({
                "origin": origin, "path": str(p), "conditional": conditional,
                "bytes": nbytes, "approx_tokens": approx_tokens(nbytes),
            })
    return recs


def walk_imports(root_files: list[tuple[str, Path]]) -> list[dict]:
    """Expand @imports depth-first; return a flat record list with depth."""
    records: list[dict] = []
    visited: set[Path] = set()

    def visit(origin: str, path: Path, depth: int) -> None:
        rp = path.resolve()
        if rp in visited:
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
        if depth >= MAX_HOPS:
            return
        root = origin.split(">", 1)[0]
        for m in IMPORT_RE.finditer(strip_code(text)):
            child = resolve_import(m.group(1), path)
            if child and child.is_file():
                visit(f"{root}>import", child, depth + 1)

    for origin, p in root_files:
        visit(origin, p, 0)
    return records


def main() -> int:
    cwd = Path.cwd()
    home = Path.home()
    records = walk_imports(scope_files(cwd, home))
    rules = rules_files(cwd, home)

    md_files = [r for r in records if "bytes" in r]
    launch_rules = [r for r in rules if "bytes" in r and not r["conditional"]]
    ondemand_rules = [r for r in rules if "bytes" in r and r["conditional"]]
    total = sum(r["approx_tokens"] for r in md_files) \
        + sum(r["approx_tokens"] for r in launch_rules)

    print(f"cwd: {cwd}")
    print(f"launch-loaded files: {len(md_files) + len(launch_rules)}  "
          f"(CLAUDE.md-family {len(md_files)}, unconditional rules {len(launch_rules)})")
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

    if rules:
        print("--- rules ---")
        for r in rules:
            if "error" in r:
                print(f"{r['origin']:<16} {'ERR':>10} {'':>8} {'':>6}  "
                      f"{r['path']}  ({r['error']})")
                continue
            tag = "  (path-scoped, on-demand)" if r["conditional"] else ""
            flag = "  <-- >5k" if r["approx_tokens"] > SINGLE_FLAG else ""
            print(f"{r['origin']:<16} {r['approx_tokens']:>10} {r['bytes']:>8} "
                  f"{'':>6}  {r['path']}{tag}{flag}")

    print("-" * 60)
    verdict = "OVER 10k" if total > TOTAL_FLAG else "under 10k"
    print(f"LAUNCH TOTAL approx-tokens: {total}  ({verdict})")
    if ondemand_rules:
        od = sum(r["approx_tokens"] for r in ondemand_rules)
        print(f"(+{od} approx-tokens in {len(ondemand_rules)} path-scoped rule(s), "
              f"loaded on demand, not counted above)")
    print(f"(approx = bytes/{APPROX_DIVISOR}; use /context for exact token counts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
