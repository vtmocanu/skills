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
    0  no drift (check), or applied (apply). NOTE that a successful apply
       REPLACES the generic body, so anything a human edited into the
       library-owned half is gone; every replaced file gets a `.pre-sync`
       backup named on stdout. There is deliberately no separate status
       for that — it is the normal outcome, not an exceptional one.
    1  drift found (check), or nothing applied because a guard fired (apply).
       Every guard, including the version stamp, runs over every named role
       before the first byte is written, so a GUARD refusal never leaves a
       partial batch. It is not a promise about every path that can return 1:
       an uncaught exception is 1 too, because that is Python's exit code for a
       traceback.
    2  the instrument itself failed — unreadable library, unreadable file,
       malformed library entry, bad arguments. From `apply` it also covers a
       write error or a failed post-write verification, where earlier roles in
       the batch ARE on disk; the message says so and names the file.

Read the output, not just the status: `check` exits 1 for anything from a
single stale role to a whole unsynced roster.
"""

from __future__ import annotations

import argparse
import difflib
import os
import pathlib
import re
import shutil
import stat
import sys
import tempfile

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


def write_source(
    path: pathlib.Path, text: str, backup: pathlib.Path | None = None
) -> None:
    """Write atomically, with newline translation OFF.

    `open(path, "w")` truncates before writing, so a failure partway through
    leaves half a role prompt on disk with the repo-owned tail gone. Reproduced
    under an `RLIMIT_FSIZE` cap (the disk-full shape): a 1245B file became 512B
    and the only output was a traceback. Writing a sibling temp file and
    `os.replace`-ing it means the file is either the old one or the new one.
    """
    original_mode = stat.S_IMODE(os.stat(path).st_mode)
    if backup is not None:
        # Written ONLY when lines are being dropped. `git diff` is not a
        # recovery path for this case and the warning used to say it was: with
        # the roster committed and the hand-tuning added since, the diff shows
        # exactly one deletion — the version line — which is the signature the
        # drop-free closing message calls the all-clear. Measured unrecoverable
        # in 2 of 3 ordinary states, one of them "the roster was generated by
        # init and not yet committed", which every consumer passes through.
        shutil.copy2(path, backup)
    # mkstemp rather than a name derived from the target: the derived name is
    # predictable and SHARED, so two agents applying the same role in one repo
    # would write the same temp file and one would truncate the other's.
    handle_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".sync-")
    tmp = pathlib.Path(tmp_name)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        # os.replace does not carry the target's mode across, so without this a
        # deliberately-restricted file comes back 0644 on an ordinary sync.
        os.chmod(tmp, original_mode)
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
        first = self.text.find("\n")
        if first > 0 and self.text[first - 1] == "\r":
            return "\r\n"
        return "\n"

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
        # Presence is not enough: emptying a block scalar while editing
        # roles.yaml is an ordinary slip and lands the value as None, which
        # used to surface as a traceback with exit 1 — the "unreadable library
        # read as drift" case the exit-code split exists to prevent.
        label = role["name"]
        for key in ("name", "description", "prompt_body"):
            if not isinstance(role[key], str):
                die(
                    f"{path}: role {label} has {key} of type "
                    f"{type(role[key]).__name__}, expected a string"
                )
        if not isinstance(role["version"], int):
            die(
                f"{path}: role {label} has version of type "
                f"{type(role['version']).__name__}, expected an integer"
            )
        roles[role["name"]] = role
    return roles


def body_delta(agent: AgentFile, role: dict) -> tuple[int, int]:
    """(lost, gained) lines if the repo's generic body is replaced by the library's.

    LOST = lines in the repo body that are absent from the library body, so
    replacing the body removes them. GAINED = the reverse.

    LOST counts `insert` AND the repo side of `replace`, and that is the whole
    correction: a reworded line is one the repo has and the library does not, so
    it IS lost. An earlier version counted `insert` only — correct as a GUARD,
    since a `replace` cannot be told from a library rewording and gating on it
    refused 11 of 11 roles on a real bump — and then that guard-shaped number
    was left driving the user-facing count, which reported "0 body line(s)
    DROPPED" on a run that dropped a line.

    The distinction to hold: this metric was never wrong as a DESCRIPTION of
    what is lost, only as a PREDICATE for whether to act. Retiring it from the
    predicate had to mean finding every consumer, not deleting the number.
    """
    matcher = difflib.SequenceMatcher(
        None,
        normalized(role["prompt_body"]).strip().splitlines(),
        normalized(agent.generic).strip().splitlines(),
        autojunk=False,
    )
    lost = gained = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            lost += j2 - j1
        if tag in ("delete", "replace"):
            gained += i2 - i1
    return lost, gained


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
    lost = 0
    metadata_differs = False
    tools_incomplete = False

    if body_differs:
        lost, gained = body_delta(agent, role)
        notes.append(f"body -{lost}/+{gained} vs library")

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
        # repo-specifics.
        notes.append("no tail (nothing repo-specific to preserve)")

    # ORDER IS THE CONTRACT: each status must predict what `apply` does to
    # THIS file. BAD-FM and an equal-version difference are both REFUSALS, so
    # they have to be decided before LEGACY, which promises the opposite ("the
    # body is replaced and these lines go"). Ranking LEGACY first made the row
    # a false statement about the two files it refuses — the same defect that
    # ranking BAD-FM below STALE produced two rounds ago, re-entered through
    # statement order rather than through a gate.
    if agent.error:
        return "BAD-FM", notes

    if not stale:
        if body_differs or metadata_differs or tools_incomplete:
            # Equal version, content differs: `apply` refuses without --force.
            # A distinct category from staleness and invisible to a version
            # comparison. It may be an improvement worth sending back to the
            # library — the next sync destroys it either way, so it has to be
            # visible before anyone decides.
            return "MODIFIED", notes
        return "ok", notes

    if lost:
        notes.append(
            f"{lost} line(s) of this body are not in the library — apply "
            f"replaces the body, so they go; a backup is written"
        )
        return "LEGACY", notes

    return "STALE", notes


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
        # Same containment as `apply`. `diff` does not write, but it PRINTS the
        # file, and for an agent that means straight into its context and
        # possibly its report — so an uncontained path here is a disclosure
        # rather than a corruption.
        path = resolve_target(agents_dir, name)
        if path is None:
            print(
                f"{name}: resolves outside {agents_dir} — refusing.",
                file=sys.stderr,
            )
            return 1
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


def stamp_version(raw_frontmatter: str, version: int, newline: str = "\n") -> str | None:
    """Rewrite the `version:` line, or insert one if the file predates it.

    Matches ANY `version:` line, not just `version: <digits>`. A hand-edited
    `version: "1"` used to fall through to the insertion path and produce a
    file with TWO version keys — and since PyYAML resolves duplicates
    last-wins and the insertion goes above the original, the value the script
    reported writing was not the value any reader saw. It never converged.
    """
    new, count = re.subn(
        r"(?m)^version:[^\r\n]*", f"version: {version}", raw_frontmatter, count=1
    )
    if count == 1:
        return new
    new, count = re.subn(
        r"(?m)^(name:[^\r\n]*)",
        lambda m: f"{m.group(1)}{newline}version: {version}",
        raw_frontmatter,
        count=1,
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

        raw_path = agents_dir / f"{name}.md"
        if raw_path.is_symlink():
            # Tested BEFORE resolving: Path.resolve() follows symlinks, so a
            # check on the resolved path is unreachable. The case that check
            # missed is a symlink pointing INSIDE the agents dir, which passes
            # containment and rewrites a different role's file under this
            # role's name.
            print(
                f"{name}: {raw_path} is a symlink to {os.readlink(raw_path)} — "
                f"refusing. Writing through it edits a file the caller did not "
                f"name.",
                file=sys.stderr,
            )
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
        if not os.access(path, os.W_OK):
            # `chmod -w` is how a user says "do not touch this file". The
            # non-atomic write refused it for free, because open(..., "w") needs
            # write permission on the FILE; os.replace only needs it on the
            # DIRECTORY, so the atomic-write fix silently removed a protection.
            print(
                f"{name}: {path} is not writable — refusing rather than "
                f"replacing it.",
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

        # Lines in the repo body that the library does not have. This CANNOT
        # distinguish hand-written content from previous-release library text,
        # and both of the obvious sources look identical to it: a line the
        # library REWORDED (handled — `replace` no longer counts toward adds)
        # and a line the library DELETED (not handled, and unhandleable —
        # the consumer's older copy legitimately still has it).
        #
        # So this is a WARNING, not a refusal. Refusing measured at 17 of 51
        # real library edits in this repo's history, 11 of 11 roles on one real
        # bump, and 5 of 11 on a roster generated from an older library
        # revision — i.e. it refuses the tool's primary use case, and the
        # documented escape was a flag whose help says it destroys content. A
        # guard the operator learns to force past by reflex is worse than no
        # guard, because the reflex survives into the case that mattered.
        #
        # What still refuses: a body differing at EQUAL version, immediately
        # below. There the difference is UNEXPLAINED, which is the actual
        # signal that someone edited the file by hand.
        lost, gained = body_delta(agent, role)
        body_differs = normalized(agent.generic).strip() != normalized(role["prompt_body"]).strip()

        # The backup is keyed on the body being REPLACED AT ALL, not on any
        # measure of how it differs. Three rounds moved that threshold and each
        # time the untested side of it was the one that broke: gated on having
        # no tail (too narrow), then on `adds` counting `replace` (so broad it
        # refused 11 of 11 roles on a real bump), then on `adds` counting
        # `insert` only — which is blind to a human EDITING a library line in
        # place, because that produces the same `replace` opcode as the library
        # rewording it.
        #
        # Measured over every historical roles.yaml revision in this repo: of
        # 119 (role, release) pairs whose body differs, the insert-only warning
        # fired on ZERO, and 99 carried a `replace` that would have been dropped
        # silently. A compensating control with a measured firing rate of zero
        # on real data is not a control.
        #
        # No predicate can separate "the library reworded this" from "a human
        # edited this" — the opcodes are identical. So stop trying to, and make
        # the UNDO unconditional instead. A backup costs one file and asks the
        # operator for no decision, which is the property that makes it immune
        # to the reflex that defeats guards.
        backup = None
        warnings = []
        if body_differs:
            backup = path.with_name(path.name + ".pre-sync")
            suffix = 2
            while backup.exists():
                # Never overwrite: the existing one may hold the only copy of
                # content dropped by an earlier sync the operator has not read.
                backup = path.with_name(f"{path.name}.pre-sync.{suffix}")
                suffix += 1
            warnings.append(
                f"{name}: the generic body is being REPLACED — {lost} line(s) of it are "
                f"not in the library and will NOT survive ({gained} arriving). Most of "
                f"that is the previous release's wording, which is what a sync is for "
                f"— but anything a human edited into the library-owned body goes with "
                f"it, and nothing can tell the two apart. The file as it was is kept "
                f"at {backup.name}; diff against that. Do NOT rely on `git diff`: if "
                f"the change was not committed, it is in no git object at all."
            )

        if body_differs and agent.version == role["version"] and not force:
            print(
                f"{name}: body differs at EQUAL version {agent.version} — this is a "
                f"local modification, not staleness. Read it first (`sync.py diff "
                f"{name}`) and decide whether it belongs back in the library; "
                f"re-run with --force to overwrite it.",
                file=sys.stderr,
            )
            return 1

        new_frontmatter = stamp_version(
            agent.raw_frontmatter, role["version"], agent.newline
        )
        if new_frontmatter is None:
            print(
                f"{name}: cannot place a `version:` line in the frontmatter — "
                f"add one by hand and re-run.",
                file=sys.stderr,
            )
            return 1

        planned.append((agent, role, new_frontmatter, warnings, backup, lost))

    for warning in [w for _, _, _, ws, _, _ in planned for w in ws]:
        print(f"warning: {warning}", file=sys.stderr)

    dropped_lines = False
    for agent, role, new_frontmatter, warnings, backup, lost in planned:
        dropped_lines = dropped_lines or bool(warnings)
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
        try:
            write_source(agent.path, out, backup)
        except OSError as exc:
            # Not 1. Exit 1 is documented as "the tree is untouched", and by
            # this point earlier roles in the batch are already written.
            print(
                f"{agent.name}: write failed ({exc}). Earlier roles in this "
                f"batch are already on disk; check `git status`.",
                file=sys.stderr,
            )
            return 2

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
        # The drop is named on STDOUT beside its own success line. It used to be
        # a stderr paragraph in a block printed before any write, so at roster
        # scale eleven warnings preceded eleven success lines saying "preserved"
        # and nothing correlated them; and the closing line pointed at
        # "the warnings above", which a caller reading only stdout does not have.
        # `lost` is the PRE-write count, carried from the guard phase.
        # Recomputing it here would measure the body we just wrote.
        drop_note = ""
        if backup is not None:
            # The count is suppressed at zero rather than the BACKUP being
            # suppressed. `lost == 0` provably means every repo body line
            # survives, so a backup is unnecessary — but "provably" is what the
            # last three rounds each believed about a predicate, and a spare
            # file costs nothing next to being wrong about that.
            drop_note = (
                f", {lost} body line(s) NOT CARRIED OVER" if lost else ""
            ) + f" — previous body kept at {backup.name}"
        print(
            f"{agent.name}: version -> {role['version']}, "
            f"body {len(role['prompt_body'])}B replaced, {tail_note}{drop_note}"
        )

    if dropped_lines:
        # No exit code here, and that is a reversal of the previous round.
        # A distinct status was right while "content was dropped" was a rare,
        # detectable event. Once the predicate was corrected it fires on every
        # ordinary sync — apply's whole job is replacing bodies — so a nonzero
        # status would mean "it worked", which is worse than no signal. The
        # backup and the per-file line carry it instead.
        print(
            "\nBodies were replaced. Each file above names its `.pre-sync` "
            "backup — diff against that, not against git, and delete the "
            "backups once you have checked them."
        )
    else:
        print("\nno body changed; only the version stamp moved")
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
    # Output carries em dashes and role prose. Under a C/POSIX locale the
    # default stdout encoding is ascii and printing a row raises, which exits 1
    # — an instrument failure wearing "drift found" again.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

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
        "modification). This is the only content guard; lines the library "
        "lacks are dropped with a warning and a backup, not refused.",
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
        return cmd_apply(args.agents, roles, args.roles, args.force)
    return cmd_check(args.agents, roles)


if __name__ == "__main__":
    sys.exit(main())
