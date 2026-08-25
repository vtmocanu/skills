#!/usr/bin/env python3
"""Publish the product roster: emit one tail-free Claude Code subagent Markdown
file per role in roles.yaml into a committed folder that a downstream runtime
(uzi, PRD #602) clones at a tag and reads into its agent store.

Design contract (why this is a separate script from sync.py):

- sync.py writes `.claude/agents/*.md` INTO A CONSUMER REPO and preserves that
  repo's `## For this repo` tail. This script does the opposite: it emits the
  GENERIC body only, with NO tail, because the downstream runtime supplies its
  own per-repo context and must never inherit ours.

- The roster is discovered DYNAMICALLY. Every role in roles.yaml is published;
  a role opts out with `publish: false` (absent field means published). There
  is no hardcoded name list, so adding a role to roles.yaml publishes it with
  zero edits here.

- The emitted frontmatter matches the downstream parser's fixed shape exactly:
  field order name, version, description, tools, model; `version` omitted when
  absent/zero; `tools` an inline `", "`-joined string omitted when empty;
  `model` omitted when empty; then one blank line, then the prompt body with a
  single trailing newline. Only those five keys are emitted — the downstream
  parser rejects any unknown frontmatter key, so `publish` is a generator
  directive that never reaches the file.

- Every scalar is emitted so the frontmatter re-parses as valid YAML: a value
  that would not round-trip as a bare scalar (notably a description containing
  `: `) is double-quoted. Downstream reads the value verbatim, so a value
  carrying a `"` or `\\` or a control character (which YAML and the verbatim
  reader would disagree on) is refused loudly rather than emitted ambiguously.

Usage:
    publish_roles.py [--roles PATH] [--out DIR]   # (re)generate the folder
    publish_roles.py --check [--roles PATH] [--out DIR]   # verify, write nothing
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

import yaml

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
DEFAULT_ROLES = SCRIPT_DIR / ".." / "roles.yaml"
# scripts -> agent-team -> agent-kit -> skills -> repo root
DEFAULT_OUT = SCRIPT_DIR / ".." / ".." / ".." / ".." / "product-agents"

# The single non-role file the generator leaves alone when pruning stale output.
KEEP = {"README.md"}

# The `## For this repo` tail sync.py appends into a consumer repo. A published
# body must not carry one. This matches sync.py's TAIL_RE: a WHOLE LINE opening
# with `## For this repo` (optional non-alpha suffix), so a mid-line, backticked
# PROSE mention of the heading -- which several generic bodies legitimately carry
# ("every slot named in your `## For this repo` tail") -- is not a tail.
TAIL_RE = re.compile(r"(?m)^## For this repo(?![A-Za-z])[^\n]*$")


def die(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(2)


def read_text(path: pathlib.Path) -> str:
    """Read with newline translation OFF, so a CRLF-corrupted committed file is
    seen as different from the LF the generator emits (and the drift check
    reddens) rather than silently normalized to look in-sync."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def write_text(path: pathlib.Path, text: str) -> None:
    """Always write LF, on every platform."""
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def is_published(role: dict) -> bool:
    """Absent field => published. Only an explicit boolean false opts out."""
    return role.get("publish") is not False


def scalar(key: str, value: str) -> str:
    """Render `value` so `key: <value>` re-parses as YAML yielding `value`.

    Bare when it round-trips; otherwise double-quoted. A value that cannot be
    represented as a plain double-quoted scalar without escaping (it holds a
    `"`, a backslash, or a control character) is refused: the downstream parser
    reads the bytes verbatim and would disagree with YAML about the escapes, so
    an ambiguous emission is a defect, not a thing to paper over.
    """
    try:
        loaded = yaml.safe_load(f"k: {value}")
        bare_ok = isinstance(loaded, dict) and loaded.get("k") == value
    except yaml.YAMLError:
        bare_ok = False
    if bare_ok:
        return value
    if '"' in value or "\\" in value or any(ord(c) < 32 for c in value):
        die(
            f"{key} value cannot be emitted unambiguously (contains a quote, "
            f"backslash, or control character): {value!r}"
        )
    quoted = '"' + value + '"'
    # Belt-and-braces: the quoted form must itself round-trip.
    if yaml.safe_load(f"k: {quoted}") != {"k": value}:
        die(f"{key} value does not round-trip when quoted: {value!r}")
    return quoted


def render_role(role: dict) -> str:
    """Return the full `.md` text for one role, tail-free."""
    for required in ("name", "description", "prompt_body"):
        if not role.get(required):
            die(f"role {role.get('name', '<unnamed>')!r} is missing {required}")

    name = str(role["name"])
    body = role["prompt_body"]
    if TAIL_RE.search(body):
        die(f"role {name!r} prompt_body carries a '## For this repo' tail heading; refusing to publish it")

    lines = ["---", f"name: {scalar('name', name)}"]

    version = role.get("version")
    if version is not None:
        # bool is a subtype of int, and YAML `true`/`false` load as bool, so an
        # `isinstance(version, int)` check would emit `version: True` or silently
        # omit `version: False`. `type(...) is int` rejects both.
        if type(version) is not int:
            die(f"role {name!r} has a non-integer version {version!r}; want a positive integer")
        if version < 0:
            die(f"role {name!r} has a negative version {version}")
        if version > 0:
            lines.append(f"version: {version}")
        # version == 0 => omit (unstamped), matching the downstream "zero means
        # unstamped" convention.

    lines.append(f"description: {scalar('description', str(role['description']))}")

    tools = role.get("tools") or []
    if tools:
        lines.append(f"tools: {', '.join(str(t) for t in tools)}")

    model = role.get("model")
    if model:
        lines.append(f"model: {scalar('model', str(model))}")

    lines.append("---")
    frontmatter = "\n".join(lines)
    # Exactly one trailing newline on the body: tail-free, and byte-stable so
    # the drift check does not flap on trailing-whitespace differences.
    body = body.rstrip("\n") + "\n"
    return frontmatter + "\n\n" + body


def published(data: dict) -> list[dict]:
    roles = data.get("roles")
    if not isinstance(roles, list):
        die("roles.yaml: top-level `roles` is not a list")
    return [r for r in roles if is_published(r)]


def load_roles(path: pathlib.Path) -> dict:
    try:
        data = yaml.safe_load(read_text(path))
    except (OSError, yaml.YAMLError) as exc:
        die(f"cannot read {path}: {exc}")
    if not isinstance(data, dict):
        die(f"{path}: not a YAML mapping")
    return data


def desired(data: dict) -> dict[str, str]:
    """Map of `<name>.md` -> file content for every published role."""
    out: dict[str, str] = {}
    for role in published(data):
        name = str(role["name"])
        fname = f"{name}.md"
        if pathlib.Path(fname).name != fname or name in ("", ".", ".."):
            die(f"role name {name!r} does not yield a safe filename")
        if fname in KEEP:
            die(f"role name {name!r} collides with the reserved file {fname}")
        if fname in out:
            die(f"duplicate role name {name!r}")
        out[fname] = render_role(role)
    return out


def current(out_dir: pathlib.Path) -> dict[str, str]:
    if not out_dir.is_dir():
        return {}
    return {
        p.name: read_text(p)
        for p in out_dir.glob("*.md")
        if p.name not in KEEP
    }


def generate(roles_path: pathlib.Path, out_dir: pathlib.Path) -> tuple[list[str], list[str]]:
    data = load_roles(roles_path)
    want = desired(data)
    out_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for fname, content in sorted(want.items()):
        write_text(out_dir / fname, content)
        written.append(fname)

    removed = []
    for fname in sorted(current(out_dir)):
        if fname not in want:
            (out_dir / fname).unlink()
            removed.append(fname)
    return written, removed


def check(roles_path: pathlib.Path, out_dir: pathlib.Path) -> int:
    data = load_roles(roles_path)
    want = desired(data)
    have = current(out_dir)

    problems = []
    for fname in sorted(set(want) | set(have)):
        if fname not in have:
            problems.append(f"MISSING   {fname} (roles.yaml has the role; regenerate)")
        elif fname not in want:
            problems.append(f"STALE     {fname} (no such published role; regenerate)")
        elif have[fname] != want[fname]:
            problems.append(f"OUT-OF-SYNC {fname} (content differs; regenerate)")

    if problems:
        print("product-agents/ is out of sync with roles.yaml:", file=sys.stderr)
        for line in problems:
            print("  " + line, file=sys.stderr)
        print(
            "\nRun: python3 skills/agent-kit/agent-team/scripts/publish_roles.py",
            file=sys.stderr,
        )
        return 1
    print(f"product-agents/ is in sync ({len(want)} published roles).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish tail-free product-roster agent files from roles.yaml.")
    parser.add_argument("--roles", type=pathlib.Path, default=DEFAULT_ROLES)
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed folder matches roles.yaml; write nothing; exit 1 on drift",
    )
    args = parser.parse_args(argv)

    roles_path = args.roles.resolve()
    out_dir = args.out.resolve()

    if args.check:
        return check(roles_path, out_dir)

    written, removed = generate(roles_path, out_dir)
    print(f"wrote {len(written)} file(s) to {out_dir}")
    for fname in removed:
        print(f"  removed stale {fname}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
