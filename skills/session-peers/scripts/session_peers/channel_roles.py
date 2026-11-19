"""Read current buddy bindings, never a runtime-based lead designation."""

import json
import os
import stat
import time
from . import identity, journal, storage

MAX_FILES = 1024
MAX_BYTES = 2 * 1024 * 1024


def roles(members, home=None, known_owner_states=None):
    """lead/buddy/unverified, failing closed when the binding inventory is incomplete.

    Missing stores mean no bindings. Read errors, malformed files, symlinks,
    FIFOs and bounded-scan exhaustion mean unknown. Only a verified-gone owner
    makes its still-stored binding inactive. No files are created or changed.
    """
    from . import channels
    members = tuple(members)
    result = {member: "lead" for member in members}
    remaining, files, deadline = MAX_BYTES, 0, time.monotonic() + 1.0

    def read(path):
        nonlocal remaining, files
        files += 1
        if files > MAX_FILES or time.monotonic() > deadline:
            raise ValueError("binding inventory exceeds bounds")
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > min(65536, remaining):
                raise ValueError("unreadable binding inventory")
            raw = os.pread(fd, info.st_size + 1, 0)
            remaining -= len(raw)
            if len(raw) != info.st_size or remaining < 0:
                raise ValueError("binding inventory changed during read")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("invalid binding inventory")
            return value
        finally:
            os.close(fd)

    def entries(directory):
        nonlocal files
        try:
            if not stat.S_ISDIR(os.lstat(directory).st_mode):
                raise ValueError("unreadable binding directory")
            with os.scandir(directory) as stream:
                for entry in stream:
                    files += 1
                    if files > MAX_FILES or time.monotonic() > deadline:
                        raise ValueError("binding inventory exceeds bounds")
                    if entry.name.endswith(".json"):
                        yield entry.path
        except FileNotFoundError:
            return

    def typed(value):
        if not isinstance(value, dict) or not identity.valid_ident(value.get("kind"), value.get("uuid")):
            raise ValueError("invalid binding identity")
        return journal._norm("%s:%s" % (value["kind"], value["uuid"]))

    try:
        with storage.no_create():
            homes = [storage.codex_home() if home is None else home, os.path.expanduser("~/.codex")]
            for path in entries(storage.claude_sessions_dir()):
                rec = read(path)
                if rec.get("entrypoint") == "codex":
                    home = rec.get("codexHome")
                    if not isinstance(home, str) or not os.path.isabs(home):
                        raise ValueError("unknown shim home")
                    homes.append(home)
            homes = set(os.path.realpath(home) for home in homes)
            if len(homes) > 32:
                raise ValueError("too many configured homes")
            owner_states, cache = {}, {}
            for home in sorted(homes):
                for path in entries(os.path.join(home, "session-peers", "buddies")):
                    rec = read(path)
                    owner, buddy = typed(rec.get("owner")), typed(rec.get("buddy"))
                    filename = os.path.basename(path)
                    if not owner.startswith("oc:"):
                        filename = filename.lower()
                    if filename != owner.replace(":", "-", 1) + ".json":
                        raise ValueError("binding owner contradicts its path")
                    if buddy not in result:
                        continue
                    if owner not in owner_states:
                        owner_states[owner] = (channels.member_liveness(owner, cache) if known_owner_states is None
                                               else known_owner_states.get(owner, "unverified"))
                    if owner_states[owner] != "gone":
                        result[buddy] = "buddy"
    except (OSError, ValueError, TypeError, RecursionError):
        return {member: "unverified" for member in members}
    return result
