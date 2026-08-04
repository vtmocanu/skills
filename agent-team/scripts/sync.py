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
preserved. Files are read and written with newline translation DISABLED, so a
CRLF file stays CRLF; the post-write check then compares the tail as BYTES,
because a comparison that shares the reader's newline normalization cannot
observe the one corruption this step is most likely to introduce.

Exit codes:
    0  no drift (check), or applied cleanly (apply)
    1  drift found (check), or NOTHING applied because a guard fired (apply);
       every guard, including the version stamp, runs over every named role
       before the first byte is written, so 1 always means the tree is untouched
    2  the instrument itself failed — unreadable library, unreadable file,
       malformed library entry, bad arguments. From `apply` it can also mean a
       post-write verification failed, in which case earlier roles in the batch
       ARE on disk; the message names the file to restore.

Read the output, not just the status: `check` exits 1 for anything from a
single stale role to a whole unsynced roster.
"""

from __future__ import annotations

import argparse
import difflib
import os
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

# Anchored to a whole line. A substring test would take `## For this repository`
# for the marker, and four library bodies already contain the literal
# `## For this repo` in prose.
# A whole HEADING LINE beginning `## For this repo`, with an optional suffix:
# real rosters write `## For this repo (uzi)`, and requiring an exact match
# reported 10 of 11 files as tail-less — which routes every one of them to the
# inline-tuning refusal, since with no tail the entire repo section counts as
# body lines the library lacks. The negative lookahead keeps
# `## For this repository` from being taken as the marker, and `\r` is
# tolerated because newline translation is off, so on a CRLF file the line ends
# `repo\r` and a `$`-anchored class without it silently matches nothing.
TAIL_RE = re.compile(r"(?m)^## For this repo(?![A-Za-z])[^\n]*$")
FRONTMATTER_OPEN = re.compile(r"---[ \t]*\r?\n")
FRONTMATTER_CLOSE = re.compile(r"\r?\n---[ \t]*\r?\n")
DEFAULT_AGENTS = pathlib.Path(".claude/agents")

# Every non-empty tools allowlist must carry these, or the spawned agent cannot
# transmit its report, claim a task, or answer a shutdown_request.
REQUIRED_TOOLS = {"SendMessage", "TaskUpdate", "TaskList", "TaskGet"}

REQUIRED_ROLE_KEYS = ("name", "version", "description", "prompt_body")


def die(message: str) -> None:
    """Exit 2. The library being unreadable is the instrument failing, not a
    finding about the repo — a caller that treats it as `drift found` would go
    looking for drift that was never measured."""
    print(message, file=sys.stderr)
    sys.exit(2)


def normalized(text: str) -> str:
    """Line endings folded to LF, for COMPARISON only.

    Storage stays byte-exact — that is the whole point of reading with
    translation off — but every comparison must be newline-agnostic, or a CRLF
    file reads as having replaced every line in its own body and the
    inline-tuning guard fires on a file nobody touched.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_source(path: pathlib.Path) -> str:
    """Read with newline translation OFF, so CRLF survives the round trip."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def write_source(path: pathlib.Path, text: str) -> None:
    """Write atomically, with newline translation OFF.

    `open(path, "w")` truncates before writing, so a failure partway through
    leaves half a role prompt on disk with the repo-owned tail gone. Reproduced
    under an `RLIMIT_FSIZE` cap (the disk-full shape): a 1245B file became 512B
    and the only output was a traceback. Writing a sibling temp file and
    `os.replace`-ing it means the file is either the old one or the new one.
    """
    tmp = path.with_name(path.name + ".sync-tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class AgentFile:
    """One `.claude/agents/<role>.md`, split into its three parts.

    The split is the whole point: the generic body is library-owned and
    replaceable, the tail is repo-owned and must survive every sync.
    """

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.name = path.stem
        self.text = read_source(path)
        self.error: str | None = None
        self.frontmatter: dict = {}

        opening = FRONTMATTER_OPEN.match(self.text)
        if opening is None:
            self.error = "no frontmatter"
            self.raw_frontmatter, self.body = "", self.text
        else:
            # Newline translation is off, so the delimiters must be matched
            # CRLF-tolerantly here. A plain "\n---\n" search reports a perfectly
            # good Windows file as having unterminated frontmatter.
            closing = FRONTMATTER_CLOSE.search(self.text, opening.end())
            if closing is None:
                self.error = "unterminated frontmatter"
                self.raw_frontmatter, self.body = "", self.text
            else:
                self.raw_frontmatter = self.text[: closing.end()]
                self.body = self.text[closing.end() :]
                self._parse_frontmatter(self.text[opening.end() : closing.start()])

        match = TAIL_RE.search(self.body)
        if match:
            start = match.start()
            # Take the newline before the heading with the tail, so the tail is
            # exactly the bytes that must survive and the body above it ends
            # where the library body ends.
            if start and self.body[start - 1] == "\n":
                start -= 1
            if start and self.body[start - 1] == "\r":
                start -= 1
            self.generic, self.tail = self.body[:start], self.body[start:]
        else:
            self.generic, self.tail = self.body, None

    def _parse_frontmatter(self, raw: str) -> None:
        try:
            loaded = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            # Worth reporting rather than crashing on. Claude Code's loader
            # tolerates an unquoted `description` containing `: `; a stricter
            # downstream parser does not, so the copy that looks fine in use is
            # exactly the one hiding the defect.
            first = str(exc).splitlines()[0]
            self.error = f"frontmatter is not valid YAML ({first})"
            self._recover_frontmatter(raw)
            return
        if not isinstance(loaded, dict):
            self.error = "frontmatter is not a YAML mapping"
            self._recover_frontmatter(raw)
            return
        self.frontmatter = loaded

    def _recover_frontmatter(self, raw: str) -> None:
        """Salvage just enough to keep the body comparison meaningful."""
        for key in ("name", "version", "model"):
            match = re.search(rf"(?m)^{key}: *(.+?) *$", raw)
            if match:
                value = match.group(1)
                self.frontmatter[key] = (
                    int(value) if key == "version" and value.isdigit() else value
                )

    @property
    def newline(self) -> str:
        """The file's own convention, so a synced body does not arrive as an
        LF island inside a CRLF file."""
        return "\r\n" if "\r\n" in self.text else "\n"

    @property
    def version(self):
        return self.frontmatter.get("version")

    @property
    def tools(self) -> list[str]:
        raw = self.frontmatter.get("tools")
        if raw is None:
            return []
        if isinstance(raw, list):
            # Off-spec (the skill specifies a comma-separated string), but
            # reporting off-spec files is what this script is for. A crash is
            # not one of the statuses it is allowed to return.
            return [str(item).strip() for item in raw if str(item).strip()]
        return [part.strip() for part in str(raw).split(",") if part.strip()]

    @property
    def tools_are_off_spec(self) -> bool:
        return isinstance(self.frontmatter.get("tools"), list)


def load_library(path: pathlib.Path) -> dict:
    try:
        data = yaml.safe_load(read_source(path))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        die(f"cannot read the role library at {path}: {exc}")
    if not isinstance(data, dict) or "roles" not in data:
        die(f"{path} has no top-level `roles:` key — is it the right file?")
    if not isinstance(data["roles"], list):
        die(f"{path}: `roles:` is {type(data['roles']).__name__}, expected a list")

    roles = {}
    for index, role in enumerate(data["roles"]):
        if not isinstance(role, dict):
            die(f"{path}: roles[{index}] is {type(role).__name__}, expected a mapping")
        missing = [key for key in REQUIRED_ROLE_KEYS if key not in role]
        if missing:
            label = role.get("name", f"roles[{index}]")
            die(f"{path}: role {label} is missing {', '.join(missing)}")
        roles[role["name"]] = role
    return roles


def body_delta(agent: AgentFile, role: dict) -> tuple[int, int]:
    """(added, deleted) lines of the repo's generic body against the library's.

    ADDED is the load-bearing half: a line present in the repo body and absent
    from the library's is content the library cannot restore, so replacing the
    body destroys it. DELETED lines are just the library additions this file has
    not received yet, which is what a sync is for.

    Counted from SequenceMatcher opcodes rather than by filtering a unified
    diff's `+++`/`---` header lines. That filter cannot tell a header from a
    CONTENT line beginning `++` or `--`, and these bodies quote CLI flags at
    line start routinely: the library's own auditor and reviewer bodies each
    carry a line starting `--oneline -3`, which made the old counter report 28
    deletions where there were 29. The same undercount could drive `adds` to
    zero and disarm the inline-tuning guard on a file whose only repo-owned
    line began with `++`, which is how it destroyed one.
    """
    matcher = difflib.SequenceMatcher(
        None,
        normalized(role["prompt_body"]).strip().splitlines(),
        normalized(agent.generic).strip().splitlines(),
        autojunk=False,
    )
    adds = dels = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            adds += j2 - j1
        if tag in ("delete", "replace"):
            dels += i2 - i1
    return adds, dels


def compare(agent: AgentFile, role: dict | None) -> tuple[str, list[str]]:
    """Return (status, notes). Status is the one word a reader scans for.

    Every condition that sets a non-`ok` status also appends a note, and the
    status is derived from explicit flags rather than by matching note text —
    a note whose wording drifts out of a prefix list stops being counted, which
    is how a real finding ends up printed beside a green verdict.
    """
    notes: list[str] = []
    if agent.error:
        notes.append(agent.error)

    if role is None:
        notes.append("not in the library — an update must not touch it")
        return "CUSTOM", notes

    stale = agent.version != role["version"]
    body_differs = normalized(agent.generic).strip() != normalized(role["prompt_body"]).strip()
    adds = 0
    metadata_differs = False
    tools_incomplete = False

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
            metadata_differs = True

        if agent.tools_are_off_spec:
            notes.append("tools is a YAML list; the skill specifies a comma-separated string")
            metadata_differs = True

        lib_tools = sorted(role.get("tools") or [])
        if sorted(agent.tools) != lib_tools:
            notes.append(
                f"tools differ (library {len(lib_tools)}, file {len(agent.tools)})"
            )
            metadata_differs = True
        if agent.tools and not REQUIRED_TOOLS.issubset(set(agent.tools)):
            missing = ", ".join(sorted(REQUIRED_TOOLS - set(agent.tools)))
            notes.append(f"MISSING COORDINATION TOOLS: {missing}")
            # Counted as drift even when the file matches the library exactly,
            # because then BOTH are wrong: a teammate spawned without these
            # produces its report and cannot send it.
            tools_incomplete = True

        if agent.frontmatter.get("model") != role.get("model"):
            notes.append(
                f"model {agent.frontmatter.get('model')} vs library {role.get('model')}"
            )
            metadata_differs = True
    else:
        # Phrased off the actual error: "unparseable" is wrong for a file that
        # has no frontmatter at all, and these notes are what a reader uses to
        # decide whether a green covers the tools invariant.
        notes.append(f"description/tools/model not compared — {agent.error}")

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

    if agent.error:
        # Not "ok". Claude Code's loader tolerates this file, so it works in use
        # and looks fine; a stricter downstream parser rejects it outright, and
        # the copy that works is exactly the one that hides the defect.
        # Outranks STALE deliberately: `apply` refuses this file, so a row
        # reading STALE would send the reader to a command that will not run.
        return "BAD-FM", notes
    if stale:
        return "STALE", notes
    if body_differs or metadata_differs or tools_incomplete:
        # Equal version, content differs: a distinct category from staleness and
        # invisible to a version comparison. It may be an improvement worth
        # sending back to the library — the next sync destroys it either way, so
        # it has to be visible before anyone decides.
        return "MODIFIED", notes
    return "ok", notes


def cmd_check(agents_dir: pathlib.Path, roles: dict) -> int:
    files = sorted(agents_dir.glob("*.md"))
    if not files:
        # A setup problem, not drift: an empty directory is not a measurement.
        die(f"no agent files in {agents_dir} — wrong --agents path, or an init?")

    rows, drift, broken = [], False, False
    for path in files:
        try:
            agent = AgentFile(path)
            role = roles.get(agent.name)
            status, notes = compare(agent, role)
            version = (
                f"{agent.version} -> {role['version']}"
                if role and agent.version != role["version"]
                else str(agent.version)
            )
            tail = f"{len(agent.tail)}B" if agent.tail else "-"
        except (OSError, UnicodeDecodeError) as exc:
            # One unreadable file must not blank the other ten rows. The whole
            # value of this pass is the roster-wide picture.
            status, notes, version, tail = "ERROR", [str(exc)], "?", "?"
            broken = True
        # CUSTOM is not drift: an update must not touch those files, so there is
        # nothing for the caller to act on and no reason to hold a repo's
        # mandatory load-time check permanently red for keeping notes here.
        if status not in ("ok", "CUSTOM"):
            drift = True
        rows.append((path.stem, version, tail, status, "; ".join(notes)))

    width = max(len(row[0]) for row in rows)
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
    if broken:
        # Rows are printed first: an unreadable file must not cost the reader
        # the other ten verdicts. But the exit code says the instrument failed,
        # because for that file nothing was measured.
        print("\nat least one file could not be read — see the ERROR rows above")
        return 2
    return 1 if drift else 0


def cmd_diff(agents_dir: pathlib.Path, roles: dict, names: list[str]) -> int:
    status = 0
    for name in names:
        path = agents_dir / f"{name}.md"
        if not path.exists():
            print(f"{name}: no such file at {path}", file=sys.stderr)
            return 2
        if name not in roles:
            # Same condition, same exit code as `apply`: a request the tool
            # cannot fulfil. Two subcommands disagreeing about one input is how
            # a caller learns to trust neither.
            print(f"{name}: not in the library — nothing to diff against", file=sys.stderr)
            status = 1
            continue
        agent = AgentFile(path)
        diff = list(
            difflib.unified_diff(
                normalized(roles[name]["prompt_body"]).strip().splitlines(),
                normalized(agent.generic).strip().splitlines(),
                fromfile=f"library/{name}",
                tofile=f"repo/{name}",
                lineterm="",
                n=1,
            )
        )
        print("\n".join(diff) if diff else f"{name}: generic body matches the library")
    return status


def stamp_version(raw_frontmatter: str, version: int) -> str | None:
    """Rewrite the `version:` line, or insert one if the file predates it.

    Matches ANY `version:` line, not just `version: <digits>`. A hand-edited
    `version: "1"` used to fall through to the insertion path and produce a
    file with TWO version keys — and since PyYAML resolves duplicates
    last-wins and the insertion goes above the original, the value the script
    reported writing was not the value any reader saw. It never converged.
    """
    new, count = re.subn(
        r"(?m)^version:.*$", f"version: {version}", raw_frontmatter, count=1
    )
    if count == 1:
        return new
    new, count = re.subn(
        r"(?m)^(name: .+)$", rf"\1\nversion: {version}", raw_frontmatter, count=1
    )
    return new if count == 1 else None


def resolve_target(agents_dir: pathlib.Path, name: str) -> pathlib.Path | None:
    """The file for `name`, or None if it would land outside `agents_dir`.

    Role names come from the LIBRARY, and `--library` is a caller-chosen flag,
    so a library declaring `name: ../CLAUDE-notes` used to make `apply` write
    outside the agents directory entirely. Membership in the library is not a
    path check and must not be used as one.
    """
    if not name or name != pathlib.Path(name).name or name in (".", ".."):
        return None
    root = agents_dir.resolve()
    target = (agents_dir / f"{name}.md").resolve()
    return target if target.parent == root else None


def cmd_apply(
    agents_dir: pathlib.Path,
    roles: dict,
    names: list[str],
    force: bool,
    force_inline: bool,
) -> int:
    if not names:
        print("apply needs at least one role name", file=sys.stderr)
        return 2

    # Every guard runs before any write, so a refusal on the last role does not
    # leave the first one rewritten. The version stamp is computed here too, for
    # the same reason: it used to be able to fail in the write loop and return
    # "nothing applied" with earlier roles already on disk.
    planned = []
    for name in names:
        if name not in roles:
            print(f"{name}: not in the library — refusing to touch it", file=sys.stderr)
            return 1

        path = resolve_target(agents_dir, name)
        if path is None:
            print(
                f"{name}: resolves outside {agents_dir} — refusing. A role name is "
                f"a file name, not a path.",
                file=sys.stderr,
            )
            return 1
        if not path.exists():
            print(f"{name}: no such file at {path}", file=sys.stderr)
            return 2
        if path.is_symlink():
            print(
                f"{name}: {path} is a symlink to {os.readlink(path)} — refusing. "
                f"Writing through it edits a file outside the agents directory.",
                file=sys.stderr,
            )
            return 1

        agent = AgentFile(path)
        role = roles[name]

        if agent.error:
            print(
                f"{name}: {agent.error}. Fix the frontmatter by hand first — "
                f"syncing the body would leave the file still failing a strict "
                f"parse, and `check` still red.",
                file=sys.stderr,
            )
            return 1

        # This guard is about the BODY and has nothing to do with the tail. It
        # used to be gated on `agent.tail is None`, so two byte-identical files
        # carrying the same hand-written paragraph got opposite treatment: the
        # tail-less one was refused and the one WITH a tail was silently eaten.
        # body_delta's own docstring stated the principle unconditionally; only
        # the code was conditional.
        adds, _ = body_delta(agent, role)
        if adds and not force_inline:
            where = "no `## For this repo` tail, and the" if agent.tail is None else "the generic"
            print(
                f"{name}: {where} body carries {adds} line(s) the library does "
                f"not have — that is inline hand-tuning, and replacing the body "
                f"would eat it. Move them into the `## For this repo` tail, then "
                f"re-run (`sync.py diff {name}` shows them). --force does NOT "
                f"lift this guard; --force-inline does, and destroys those lines.",
                file=sys.stderr,
            )
            return 1

        body_differs = normalized(agent.generic).strip() != normalized(role["prompt_body"]).strip()
        if body_differs and agent.version == role["version"] and not force:
            print(
                f"{name}: body differs at EQUAL version {agent.version} — this is a "
                f"local modification, not staleness. Read it first (`sync.py diff "
                f"{name}`) and decide whether it belongs back in the library; "
                f"re-run with --force to overwrite it.",
                file=sys.stderr,
            )
            return 1

        new_frontmatter = stamp_version(agent.raw_frontmatter, role["version"])
        if new_frontmatter is None:
            print(
                f"{name}: cannot place a `version:` line in the frontmatter — "
                f"add one by hand and re-run.",
                file=sys.stderr,
            )
            return 1

        planned.append((agent, role, new_frontmatter))

    for agent, role, new_frontmatter in planned:
        body = role["prompt_body"].rstrip("\n")
        if agent.newline != "\n":
            body = body.replace("\n", agent.newline)
        out = (
            new_frontmatter
            + agent.newline
            + body
            + agent.newline
            + (agent.tail or "")
        )
        write_source(agent.path, out)

        # Verify against the file on disk, not against the string we just built.
        # The tail comparison is on BYTES: an earlier version compared two
        # values that had both been through newline translation, so it could
        # not see a CRLF file being silently rewritten to LF — the verification
        # shared the exact blind spot it existed to close.
        after = AgentFile(agent.path)
        before_tail = (agent.tail or "").encode("utf-8")
        after_tail = (after.tail or "").encode("utf-8")
        if after_tail != before_tail:
            print(
                f"{agent.name}: TAIL CHANGED ACROSS THE WRITE "
                f"({len(before_tail)}B -> {len(after_tail)}B) — restore this file "
                f"from git before doing anything else.",
                file=sys.stderr,
            )
            return 2
        if normalized(after.generic).strip() != normalized(role["prompt_body"]).strip():
            print(f"{agent.name}: body did not land as written", file=sys.stderr)
            return 2
        if after.version != role["version"]:
            # The version stamp is the one thing this branch exists to write,
            # and it was the one thing the check never looked at.
            print(
                f"{agent.name}: version reads {after.version!r} after writing "
                f"{role['version']} — the frontmatter is not what it appears.",
                file=sys.stderr,
            )
            return 2

        tail_note = f"tail {len(agent.tail)}B preserved" if agent.tail else "no tail"
        print(
            f"{agent.name}: version -> {role['version']}, "
            f"body {len(role['prompt_body'])}B replaced, {tail_note}"
        )

    print("\nverify with `git diff` — the only deletions should be the old version lines")
    return 0


def add_common(parser, default_library: pathlib.Path, *, suppress: bool) -> None:
    """Add --library/--agents to a parser.

    `suppress` is what makes the flag work on BOTH sides of the subcommand.
    argparse parses a subcommand into a fresh namespace and copies every key
    back over the parent's, so a subparser default overwrites a value the user
    supplied BEFORE the subcommand. With argparse.SUPPRESS the key is simply
    absent unless the user passed it there, and the parent's value survives.

    The bug this replaces was worse than the usage error it was fixing:
    `--agents X apply role` silently wrote to ./.claude/agents and reported
    success, having never touched X.
    """
    library_default = argparse.SUPPRESS if suppress else default_library
    agents_default = argparse.SUPPRESS if suppress else DEFAULT_AGENTS
    parser.add_argument(
        "--library",
        type=pathlib.Path,
        default=library_default,
        help=f"role library (default: {default_library})",
    )
    parser.add_argument(
        "--agents",
        type=pathlib.Path,
        default=agents_default,
        help=f"agents directory (default: {DEFAULT_AGENTS})",
    )


def main() -> int:
    default_library = pathlib.Path(__file__).resolve().parent.parent / "roles.yaml"

    parser = argparse.ArgumentParser(
        description="Compare and sync .claude/agents/*.md against the role library."
    )
    add_common(parser, default_library, suppress=False)
    sub = parser.add_subparsers(dest="command")

    p_check = sub.add_parser("check", help="report drift for every agent file (default)")
    p_diff = sub.add_parser("diff", help="show the body diff, library vs repo")
    p_diff.add_argument("roles", nargs="+")
    p_apply = sub.add_parser("apply", help="replace the generic body, keep the tail")
    p_apply.add_argument("roles", nargs="+")
    p_apply.add_argument(
        "--force",
        action="store_true",
        help="overwrite a body that differs at equal version (a local "
        "modification). Does NOT lift the tail-less guard.",
    )
    p_apply.add_argument(
        "--force-inline",
        action="store_true",
        help="overwrite a body carrying lines the library lacks, destroying "
        "that inline tuning. Separate from --force on purpose: the two "
        "hazards are not one decision.",
    )
    for subparser in (p_check, p_diff, p_apply):
        add_common(subparser, default_library, suppress=True)

    args = parser.parse_args()

    if not args.agents.is_dir():
        die(
            f"no agents directory at {args.agents} — run this from the repo root, "
            f"or pass --agents. If the repo has no team yet, that is an `init`."
        )

    roles = load_library(args.library)

    if args.command == "diff":
        return cmd_diff(args.agents, roles, args.roles)
    if args.command == "apply":
        return cmd_apply(
            args.agents, roles, args.roles, args.force, args.force_inline
        )
    return cmd_check(args.agents, roles)


if __name__ == "__main__":
    sys.exit(main())
