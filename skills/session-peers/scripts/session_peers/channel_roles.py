"""Read current buddy bindings, never a runtime-based lead designation."""

import json
import os
import stat
import time
from . import identity, journal, storage

MAX_BYTES = 2 * 1024 * 1024
SCAN_SECONDS = 1.0


def _read(path, remaining):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > min(65536, remaining):
            raise ValueError("unreadable binding inventory")
        raw = os.pread(fd, info.st_size + 1, 0)
        return raw, info.st_size
    finally:
        os.close(fd)


def _typed(value):
    if not isinstance(value, dict) or not identity.valid_ident(value.get("kind"), value.get("uuid")):
        raise ValueError("invalid binding identity")
    return journal._norm("%s:%s" % (value["kind"], value["uuid"]))


def _owner_name(name):
    if not name.endswith(".json"):
        return None
    kind, sep, ident = name[:-5].partition("-")
    try:
        return _typed({"kind": kind, "uuid": ident}) if sep else None
    except ValueError:
        return None


def _homes(home):
    """Known homes plus whether bounds or I/O left a valid shim home unseen."""
    homes = {os.path.realpath(home), os.path.realpath(os.path.expanduser("~/.codex"))}
    incomplete = False
    deadline, remaining = time.monotonic() + SCAN_SECONDS, MAX_BYTES
    try:
        with os.scandir(storage.claude_sessions_dir()) as entries:
            for entry in entries:
                if time.monotonic() > deadline or remaining <= 0:
                    incomplete = True
                    break
                if not entry.name.endswith(".json"):
                    continue
                try:
                    raw, expected = _read(entry.path, remaining)
                    remaining -= len(raw)
                    if len(raw) != expected:
                        raise ValueError("registry changed during read")
                except (OSError, ValueError):
                    incomplete = True
                    continue
                try:
                    rec = json.loads(raw)
                except (ValueError, TypeError, RecursionError):
                    continue
                value = rec.get("codexHome") if isinstance(rec, dict) and rec.get("entrypoint") == "codex" else None
                if isinstance(value, str) and os.path.isabs(value):
                    homes.add(os.path.realpath(value))
    except FileNotFoundError:
        pass
    except OSError:
        incomplete = True
    return homes, incomplete


def roles(members, home=None, known_owner_states=None):
    """lead/buddy/unverified, without writes or a historical-file-count cutoff.

    Candidate binding files and possibly unseen shim homes can introduce
    uncertainty. Valid bindings name their target exactly; unrelated records need no owner probe.
    Unreadable candidate bindings whose owner is not verified gone make the
    remaining queried roles unknown. Keep positive buddy evidence if a later
    read fails. Callers decide admission vs fan-out policy for unknown roles.
    """
    from . import channels
    members = tuple(members)
    result = {member: "lead" for member in members}
    owner_states, cache = {}, {}
    unknown = False

    def active(owner):
        if owner not in owner_states:
            owner_states[owner] = (channels.member_liveness(owner, cache) if known_owner_states is None
                                   else known_owner_states.get(owner, "unverified"))
        return owner_states[owner] != "gone"

    with storage.no_create():
        homes, unknown = _homes(storage.codex_home() if home is None else home)
        deadline, remaining = time.monotonic() + SCAN_SECONDS, MAX_BYTES
        for root in sorted(homes):
            if time.monotonic() > deadline or remaining <= 0:
                unknown = True
                break
            directory = os.path.join(root, "session-peers", "buddies")
            try:
                if not stat.S_ISDIR(os.lstat(directory).st_mode):
                    raise OSError("unreadable binding directory")
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if time.monotonic() > deadline or remaining <= 0:
                            unknown = True
                            break
                        owner = _owner_name(entry.name)
                        if owner is None:
                            continue
                        try:
                            raw, expected = _read(entry.path, remaining)
                            remaining -= len(raw)
                            if len(raw) != expected:
                                raise ValueError("binding changed during read")
                            rec = json.loads(raw)
                        except (OSError, ValueError, TypeError, RecursionError):
                            unknown = active(owner) or unknown
                            continue
                        try:
                            buddy = _typed(rec.get("buddy") if isinstance(rec, dict) else None)
                        except ValueError:
                            unknown = active(owner) or unknown
                            continue
                        if buddy in result and active(owner):
                            result[buddy] = "buddy"
            except FileNotFoundError:
                continue
            except OSError:
                unknown = True
    if unknown:
        result = {member: "unverified" if role == "lead" else role for member, role in result.items()}
    return result
