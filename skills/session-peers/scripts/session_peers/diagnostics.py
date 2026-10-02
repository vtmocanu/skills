"""Diagnostics for the session-peers CLI."""

from __future__ import annotations
import time
from . import constants as sp_constants, runtime as sp_runtime
from . import storage as sp_storage

def warn_versions(kinds=("claude", "codex")) -> None:
    """D11: warn once per installed/pinned pair per day, never fail a run."""
    previous = sp_runtime.read_json(sp_storage.version_warning_path(), {}) or {}
    if not isinstance(previous, dict):
        previous = {}
    now = time.time()
    changed = False

    def warn_once(key, message):
        nonlocal changed
        try:
            last = float(previous.get(key, 0))
        except (TypeError, ValueError):
            last = 0
        if now - last < sp_constants.VERSION_WARNING_WINDOW:
            return
        sp_runtime.log(message)
        previous[key] = now
        changed = True

    if "claude" in kinds:
        v = sp_runtime.tool_version("claude")
        if v and sp_runtime.version_is_newer(v, sp_constants.CLAUDE_CODE_TESTED):
            warn_once(
                "claude:%s>%s" % (v.strip(), sp_constants.CLAUDE_CODE_TESTED),
                "Claude Code %s is newer than the tested %s; if peers stop "
                "appearing, re-run references/spike-checklist.md"
                % (v.strip(), sp_constants.CLAUDE_CODE_TESTED)
            )
    if "codex" in kinds:
        v = sp_runtime.tool_version("codex")
        if v and sp_runtime.version_is_newer(v, sp_constants.CODEX_TESTED):
            warn_once(
                "codex:%s>%s" % (v.strip(), sp_constants.CODEX_TESTED),
                "Codex CLI %s is newer than the tested %s; if discovery breaks, "
                "re-run references/spike-checklist.md" % (v.strip(), sp_constants.CODEX_TESTED)
            )
    if changed:
        try:
            sp_runtime.write_json_atomic(sp_storage.version_warning_path(), previous)
        except OSError:
            pass
