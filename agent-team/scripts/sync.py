#!/usr/bin/env python3
"""Compare and sync a repo's .claude/agents/*.md against the role library.

Three subcommands:

    sync.py check              report drift for every agent file (default)
    sync.py diff <role> ...    show the body diff, library vs repo
    sync.py apply <role> ...   replace the generic body, bump version, keep tail

`check` implements the load-time staleness pass: it compares the frontmatter
`version:` AND the generic body, because the two disagree. roles.yaml allows one
version bump per release rather than one per edit, so a body change can ship
without an increment and is invisible to a version-keyed comparison by
construction.

`apply` implements the Mode 2 Step 5 merge: everything above `## For this repo`
is replaced from the library, the `version:` line is rewritten, and the tail is
preserved byte-exact (verified by re-reading the file after the write).

Exit codes:
    0  no drift (check), or applied cleanly (apply)
    1  drift found (check), or nothing applied because a guard fired (apply)
    2  the instrument itself failed — unreadable library, bad arguments

Read the output, not just the status: `check` exits 1 for anything from a
single stale role to a whole unsynced roster.
"""

from __future__ import annotations

import argparse
import difflib
import pathlib
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - environment problem, not a finding
    print(
        "sync.py needs PyYAML (`pip install pyyaml`, or run it under a python "
        "that has it). Refusing to hand-parse roles.yaml: it is full of block "
        "scalars and a partial parse would report clean bodies it never read.",
        file=sys.stderr,
    )
    sys.exit(2)  # instrument failure, not a finding — see the exit-code contract

TAIL_MARKER = "\n## For this repo"
FRONTMATTER_END = "\n---\n"

# Every non-empty tools allowlist must carry these, or the spawned agent cannot
# transmit its report, claim a task, or answer a shutdown_request.
REQUIRED_TOOLS = {"SendMessage", "TaskUpdate", "TaskList", "TaskGet"}


class AgentFile:
    """One `.claude/agents/<role>.md`, split into its three parts.

    The split is the whole point: the generic body is library-owned and
    replaceable, the tail is repo-owned and must survive every sync.
    """

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.name = path.stem
        self.text = path.read_text()
        self.error: str | None = None
        self.frontmatter: dict = {}

        if not self.text.startswith("---\n"):
            self.error = "no frontmatter"
            self.raw_frontmatter, self.body = "", self.text
        else:
            end = self.text.find(FRONTMATTER_END, 3)
            if end == -1:
                self.error = "unterminated frontmatter"
                self.raw_frontmatter, self.body = "", self.text
            else:
                self.raw_frontmatter = self.text[: end + len(FRONTMATTER_END)]
                self.body = self.text[end + len(FRONTMATTER_END) :]
                self._parse_frontmatter(self.text[4:end])

        # A file with no tail marker is a legacy or hand-written file. `apply`
        # refuses to touch it: the library/local boundary is not mechanical
        # there, so an overwrite would silently eat repo-specific tuning.
        if TAIL_MARKER in self.body:
            idx = self.body.index(TAIL_MARKER)
            self.generic, self.tail = self.body[:idx], self.body[idx:]
        else:
            self.generic, self.tail = self.body, None

    def _parse_frontmatter(self, raw: str) -> None:
        try:
            self.frontmatter = yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            # Worth reporting rather than crashing on. Claude Code's loader
            # tolerates an unquoted `description` containing `: `; a stricter
            # downstream parser does not, so the copy that looks fine in use is
            # exactly the one hiding the defect.
            first = str(exc).splitlines()[0]
            self.error = f"frontmatter is not valid YAML ({first})"
            # Recover what we can so the body comparison still runs.
            for key in ("name", "version", "model"):
                m = re.search(rf"(?m)^{key}: *(.+?) *$", raw)
                if m:
                    value = m.group(1)
                    self.frontmatter[key] = (
                        int(value) if key == "version" and value.isdigit() else value
                    )

    @property
    def version(self):
        return self.frontmatter.get("version")

    @property
    def tools(self) -> list[str]:
        raw = self.frontmatter.get("tools") or ""
        return [t.strip() for t in raw.split(",") if t.strip()]


def die(message: str) -> None:
    """Exit 2. The library being unreadable is the instrument failing, not a
    finding about the repo — a caller that treats it as `drift found` would go
    looking for drift that was never measured."""
    print(message, file=sys.stderr)
    sys.exit(2)


def load_library(path: pathlib.Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        die(f"cannot read the role library at {path}: {exc}")
    if not isinstance(data, dict) or "roles" not in data:
        die(f"{path} has no top-level `roles:` key — is it the right file?")
    return {r["name"]: r for r in data["roles"]}


def body_delta(agent: AgentFile, role: dict) -> tuple[int, int]:
    """(added, deleted) lines of the repo's generic body against the library's.

    ADDED is the load-bearing half: a line present in the repo body and absent
    from the library's is content the library cannot restore, so replacing the
    body destroys it. DELETED lines are just the library additions this file has
    not received yet, which is what a sync is for.
    """
    d = list(
        difflib.unified_diff(
            role["prompt_body"].strip().splitlines(),
            agent.generic.strip().splitlines(),
            lineterm="",
            n=0,
        )
    )
    adds = sum(1 for x in d if x.startswith("+") and not x.startswith("+++"))
    dels = sum(1 for x in d if x.startswith("-") and not x.startswith("---"))
    return adds, dels


def compare(agent: AgentFile, role: dict | None) -> tuple[str, list[str]]:
    """Return (status, notes). Status is the one word a reader scans for."""
    notes: list[str] = []
    if agent.error:
        notes.append(agent.error)

    if role is None:
        return "CUSTOM", notes + ["not in the library — an update must not touch it"]

    stale = agent.version != role["version"]
    body_differs = agent.generic.strip() != role["prompt_body"].strip()
    adds = 0

    if body_differs:
        adds, dels = body_delta(agent, role)
        notes.append(f"body +{adds}/-{dels} vs library")

    # A frontmatter that did not parse gives us no values to compare, so any
    # description/tools verdict here would be a statement about our own failed
    # read. Report the parse error and stop, rather than emitting confident
    # findings ("tools differ, file 0") derived from nothing.
    if not agent.error:
        if (agent.frontmatter.get("description") or "").strip() != role[
            "description"
        ].strip():
            notes.append("description differs")

        lib_tools = sorted(role.get("tools") or [])
        if sorted(agent.tools) != lib_tools:
            notes.append(
                f"tools differ (library {len(lib_tools)}, file {len(agent.tools)})"
            )
        if agent.tools and not REQUIRED_TOOLS.issubset(set(agent.tools)):
            missing = ", ".join(sorted(REQUIRED_TOOLS - set(agent.tools)))
            notes.append(f"MISSING COORDINATION TOOLS: {missing}")

        if agent.frontmatter.get("model") != role.get("model"):
            notes.append(
                f"model {agent.frontmatter.get('model')} vs library {role.get('model')}"
            )
    else:
        notes.append("description/tools/model not compared — unparseable frontmatter")

    if agent.tail is None:
        # No tail is legitimate: the skill omits the heading when a role has no
        # repo-specifics. It is only a hazard when the body ALSO carries lines
        # the library does not have, which is inline hand-tuning that an
        # overwrite would eat. That distinction is mechanical, so make it rather
        # than refusing every tail-less file.
        if adds:
            notes.append(
                f"NO TAIL and {adds} line(s) the library lacks — inline tuning, "
                f"apply refuses this file"
            )
            return "LEGACY", notes
        notes.append("no tail (nothing repo-specific to preserve)")

    if stale:
        return "STALE", notes
    if agent.error:
        # Not "ok". Claude Code's loader tolerates this file, so it works in use
        # and looks fine; a stricter downstream parser rejects it outright, and
        # the copy that works is exactly the one that hides the defect.
        return "BAD-FM", notes
    if body_differs or any(
        n.startswith(("description differs", "tools differ", "model "))
        for n in notes
    ):
        # Equal version, content differs: a distinct category from staleness and
        # invisible to a version comparison. It may be an improvement worth
        # sending back to the library — the next sync destroys it either way, so
        # it has to be visible before anyone decides.
        return "MODIFIED", notes
    return "ok", notes


def cmd_check(agents_dir: pathlib.Path, roles: dict) -> int:
    files = sorted(agents_dir.glob("*.md"))
    if not files:
        print(f"no agent files in {agents_dir}")
        return 1

    rows, drift = [], False
    for path in files:
        agent = AgentFile(path)
        role = roles.get(agent.name)
        status, notes = compare(agent, role)
        if status != "ok":
            drift = True
        version = (
            f"{agent.version} -> {role['version']}"
            if role and agent.version != role["version"]
            else str(agent.version)
        )
        tail = f"{len(agent.tail)}B" if agent.tail else "-"
        rows.append((agent.name, version, tail, status, "; ".join(notes)))

    width = max(len(r[0]) for r in rows)
    for name, version, tail, status, notes in rows:
        print(f"{name:<{width}}  {version:<10} tail {tail:<8} {status:<9} {notes}")

    absent = [n for n in roles if not (agents_dir / f"{n}.md").exists()]
    if absent:
        print(
            f"\nin the library, no file here: {', '.join(sorted(absent))}"
            "\n  (informational — whether a role belongs is a roster decision,"
            " not a sync one)"
        )

    if drift:
        print("\nrun `sync.py diff <role>` to read a drift, `apply <role>` to merge it")
    return 1 if drift else 0


def cmd_diff(agents_dir: pathlib.Path, roles: dict, names: list[str]) -> int:
    for name in names:
        path = agents_dir / f"{name}.md"
        if not path.exists():
            print(f"{name}: no such file at {path}", file=sys.stderr)
            return 2
        if name not in roles:
            print(f"{name}: not in the library — nothing to diff against")
            continue
        agent = AgentFile(path)
        diff = list(
            difflib.unified_diff(
                roles[name]["prompt_body"].strip().splitlines(),
                agent.generic.strip().splitlines(),
                fromfile=f"library/{name}",
                tofile=f"repo/{name}",
                lineterm="",
                n=1,
            )
        )
        print("\n".join(diff) if diff else f"{name}: generic body matches the library")
    return 0


def cmd_apply(
    agents_dir: pathlib.Path, roles: dict, names: list[str], force: bool
) -> int:
    if not names:
        print("apply needs at least one role name", file=sys.stderr)
        return 2

    planned = []
    for name in names:
        path = agents_dir / f"{name}.md"
        if not path.exists():
            print(f"{name}: no such file at {path}", file=sys.stderr)
            return 2
        if name not in roles:
            print(f"{name}: not in the library — refusing to touch it", file=sys.stderr)
            return 1

        agent = AgentFile(path)
        role = roles[name]

        adds, _ = body_delta(agent, role)
        if agent.tail is None and adds and not force:
            print(
                f"{name}: no `## For this repo` tail, and the body carries {adds} "
                f"line(s) the library does not have — that is inline hand-tuning, "
                f"and replacing the body would eat it. Separate the library "
                f"paragraphs from the hand-written ones into a tail, then re-run "
                f"(`sync.py diff {name}` shows them).",
                file=sys.stderr,
            )
            return 1

        body_differs = agent.generic.strip() != role["prompt_body"].strip()
        if body_differs and agent.version == role["version"] and not force:
            print(
                f"{name}: body differs at EQUAL version {agent.version} — this is a "
                f"local modification, not staleness. Read it first (`sync.py diff "
                f"{name}`) and decide whether it belongs back in the library; "
                f"re-run with --force to overwrite it.",
                file=sys.stderr,
            )
            return 1

        planned.append((agent, role))

    for agent, role in planned:
        new_frontmatter, n = re.subn(
            r"(?m)^version: *\d+ *$",
            f"version: {role['version']}",
            agent.raw_frontmatter,
            count=1,
        )
        if n != 1:
            # Predates versioning. Insert the stamp after `name:` rather than
            # guessing a position.
            new_frontmatter, n = re.subn(
                r"(?m)^(name: .+)$",
                rf"\1\nversion: {role['version']}",
                agent.raw_frontmatter,
                count=1,
            )
            if n != 1:
                print(
                    f"{agent.name}: cannot place a `version:` line in the "
                    f"frontmatter — add one by hand and re-run.",
                    file=sys.stderr,
                )
                return 1

        out = (
            new_frontmatter
            + "\n"
            + role["prompt_body"].rstrip("\n")
            + "\n"
            + (agent.tail or "")
        )
        agent.path.write_text(out)

        # Verify against the file on disk, not against the string we just built:
        # the claim worth checking is that the tail survived the write.
        after = AgentFile(agent.path)
        if after.tail != agent.tail:
            print(
                f"{agent.name}: TAIL CHANGED ACROSS THE WRITE — restore this file "
                f"from git before doing anything else.",
                file=sys.stderr,
            )
            return 2
        if after.generic.strip() != role["prompt_body"].strip():
            print(f"{agent.name}: body did not land as written", file=sys.stderr)
            return 2

        tail_note = f"tail {len(agent.tail)}B preserved" if agent.tail else "no tail"
        print(
            f"{agent.name}: version -> {role['version']}, "
            f"body {len(role['prompt_body'])}B replaced, {tail_note}"
        )

    print("\nverify with `git diff` — the only deletions should be the old version lines")
    return 0


def main() -> int:
    default_library = pathlib.Path(__file__).resolve().parent.parent / "roles.yaml"

    # Declared on a parent parser so they are accepted on BOTH sides of the
    # subcommand. argparse otherwise binds them to the top level only, and
    # `sync.py check --agents X` — the order everyone types — exits on a usage
    # error instead of running.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--library",
        type=pathlib.Path,
        default=default_library,
        help=f"role library (default: {default_library})",
    )
    common.add_argument(
        "--agents",
        type=pathlib.Path,
        default=pathlib.Path(".claude/agents"),
        help="agents directory (default: .claude/agents)",
    )

    parser = argparse.ArgumentParser(
        parents=[common],
        description="Compare and sync .claude/agents/*.md against the role library.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser(
        "check", parents=[common], help="report drift for every agent file (default)"
    )
    p_diff = sub.add_parser(
        "diff", parents=[common], help="show the body diff, library vs repo"
    )
    p_diff.add_argument("roles", nargs="+")
    p_apply = sub.add_parser(
        "apply", parents=[common], help="replace the generic body, keep the tail"
    )
    p_apply.add_argument("roles", nargs="+")
    p_apply.add_argument(
        "--force",
        action="store_true",
        help="overwrite a body that differs at equal version (a local modification)",
    )
    args = parser.parse_args()

    if not args.agents.is_dir():
        print(
            f"no agents directory at {args.agents} — run this from the repo root, "
            f"or pass --agents. If the repo has no team yet, that is an `init`.",
            file=sys.stderr,
        )
        return 2

    roles = load_library(args.library)

    if args.command == "diff":
        return cmd_diff(args.agents, roles, args.roles)
    if args.command == "apply":
        return cmd_apply(args.agents, roles, args.roles, args.force)
    return cmd_check(args.agents, roles)


if __name__ == "__main__":
    sys.exit(main())
