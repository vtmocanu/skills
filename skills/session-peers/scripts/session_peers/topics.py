"""Topics for the session-peers CLI."""

from __future__ import annotations
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from . import constants as sp_constants, runtime as sp_runtime
from . import claude as sp_claude, codex as sp_codex, process as sp_process, storage as sp_storage
from . import identity as sp_identity

class TopicError(Exception):
    """A refused topic operation; carries the exit code."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def topics_dir():
    path = os.path.join(sp_storage.state_dir(), "topics")
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def validate_topic(topic):
    """The topic string unchanged, or TopicError. It is opaque otherwise."""
    if not isinstance(topic, str) or not topic.strip():
        raise TopicError("a topic must be a non-empty string", 2)
    if len(topic) > sp_constants.TOPIC_MAX_CHARS:
        raise TopicError("a topic is at most %d characters" % sp_constants.TOPIC_MAX_CHARS, 2)
    if sp_constants.TOPIC_BAD_CHARS_RE.search(topic):
        raise TopicError("a topic must not contain control characters", 2)
    try:
        topic.encode("utf-8")
    except UnicodeError:
        raise TopicError("a topic must be valid UTF-8", 2)
    return topic


def _topic_paths(topic):
    """(log, meta, lock) paths. The file name is a digest, never the topic,
    so no topic string can name a path outside the topics directory."""
    digest = hashlib.sha256(topic.encode("utf-8")).hexdigest()[:40]
    base = os.path.join(topics_dir(), digest)
    return base + ".jsonl", base + ".meta.json", base + ".lock"


@contextlib.contextmanager
def _topic_lock(lock_path):
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing drops the flock


def _topic_limits():
    ttl = sp_runtime._float_env("SESSION_PEERS_TOPIC_TTL_DAYS", sp_constants.TOPIC_TTL_DAYS_DEFAULT)
    entries = int(sp_runtime._float_env("SESSION_PEERS_TOPIC_MAX_ENTRIES", sp_constants.TOPIC_MAX_ENTRIES_DEFAULT))
    size = int(sp_runtime._float_env("SESSION_PEERS_TOPIC_MAX_BYTES", sp_constants.TOPIC_MAX_BYTES_DEFAULT))
    return (
        ttl if ttl > 0 else sp_constants.TOPIC_TTL_DAYS_DEFAULT,
        entries if entries > 0 else sp_constants.TOPIC_MAX_ENTRIES_DEFAULT,
        size if size > 0 else sp_constants.TOPIC_MAX_BYTES_DEFAULT,
    )


def _read_topic_lines(log_path):
    """Parsed entries in file order; a torn or foreign line is skipped."""
    out = []
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and isinstance(entry.get("seq"), int):
                    out.append((entry, len(line.encode("utf-8"))))
    except FileNotFoundError:
        pass
    return out


def _topic_state(topic, log_path, meta_path, now):
    """Retained entries after pruning, and the next seq. Call under the lock.

    The next seq lives in the meta file, so it survives pruning every entry;
    the log's own last seq covers a meta write lost to a crash.
    """
    ttl_days, max_entries, max_bytes = _topic_limits()
    lines = _read_topic_lines(log_path)
    meta = sp_runtime.read_json(meta_path, {}) or {}
    next_seq = meta.get("next_seq") if isinstance(meta.get("next_seq"), int) else 1
    if lines:
        next_seq = max(next_seq, lines[-1][0]["seq"] + 1)
    cutoff = now - ttl_days * 86400.0
    kept = [
        (entry, size) for entry, size in lines
        if (sp_runtime.parse_time(entry.get("ts")) or 0.0) >= cutoff
    ]
    kept = kept[-max_entries:]
    total = sum(size for _entry, size in kept)
    while kept and total > max_bytes:
        total -= kept.pop(0)[1]
    if len(kept) != len(lines):
        _write_topic_log(log_path, [entry for entry, _size in kept])
    return [entry for entry, _size in kept], next_seq


def _write_topic_log(log_path, entries):
    tmp = "%s.tmp.%d" % (log_path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, log_path)


def _write_topic_meta(meta_path, topic, next_seq, last_ts):
    sp_runtime.write_json_atomic(
        meta_path,
        {"topic": topic, "next_seq": next_seq, "last_ts": last_ts},
        mode=0o600,
    )


def topic_post(topic, from_identity, kind=None, text=None, data=sp_constants._NO_DATA):
    """Append one entry and return it. Seq is unique and monotonic per topic."""
    validate_topic(topic)
    if kind is not None and not sp_constants.TOPIC_KIND_RE.match(kind):
        raise TopicError(
            "--kind must be 1-64 characters of letters, digits, '.', '_', ':' "
            "or '-', starting with a letter or digit", 2
        )
    log_path, meta_path, lock_path = _topic_paths(topic)
    with _topic_lock(lock_path):
        now = time.time()
        _entries, next_seq = _topic_state(topic, log_path, meta_path, now)
        entry = {
            "seq": next_seq,
            "ts": sp_runtime.now_iso(),
            "topic": topic,
            "from_kind": from_identity.get("kind"),
            "from_name": from_identity.get("name"),
            "from_sid": from_identity.get("uuid"),
            "kind": kind,
        }
        if data is not sp_constants._NO_DATA:
            entry["data"] = data
        else:
            entry["text"] = text
        line = json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n"
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(log_path, 0o600)
        _write_topic_meta(meta_path, topic, next_seq + 1, entry["ts"])
    return entry


def topic_tail(topic, since=None, limit=sp_constants.TOPIC_TAIL_DEFAULT):
    """{"entries", "next", "gap"}: entries with seq > since in order, or the
    last ``limit`` when since is None. ``gap`` names pruned seqs the cursor
    skipped, so a slow reader learns it lost entries instead of guessing."""
    validate_topic(topic)
    log_path, meta_path, lock_path = _topic_paths(topic)
    if not os.path.exists(meta_path) and not os.path.exists(log_path):
        return {"topic": topic, "entries": [], "next": since or 0, "gap": None}
    with _topic_lock(lock_path):
        entries, next_seq = _topic_state(topic, log_path, meta_path, time.time())
    last_seq = next_seq - 1
    gap = None
    if since is None:
        chosen = entries[-limit:]
    else:
        oldest = entries[0]["seq"] if entries else next_seq
        if since + 1 < oldest and since < last_seq:
            gap = {"from": since + 1, "to": oldest - 1}
        chosen = [e for e in entries if e["seq"] > since][:limit]
    if chosen:
        cursor = chosen[-1]["seq"]
    elif since is None:
        cursor = last_seq
    else:
        # Nothing newer: stay put, or move past a gap that ends the log.
        cursor = max(since, gap["to"]) if gap else since
    return {"topic": topic, "entries": chosen, "next": cursor, "gap": gap}


def topic_list():
    out = []
    try:
        names = sorted(os.listdir(topics_dir()))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".meta.json"):
            continue
        meta = sp_runtime.read_json(os.path.join(topics_dir(), name), None)
        if not isinstance(meta, dict) or not isinstance(meta.get("topic"), str):
            continue
        next_seq = meta.get("next_seq")
        out.append({
            "topic": meta["topic"],
            "last_seq": next_seq - 1 if isinstance(next_seq, int) else None,
            "last_ts": meta.get("last_ts"),
        })
    out.sort(key=lambda item: item["topic"])
    return out


def topic_prune_all():
    """Apply retention to every topic, including ones nobody posts to or reads."""
    for item in topic_list():
        log_path, meta_path, lock_path = _topic_paths(item["topic"])
        with _topic_lock(lock_path):
            _topic_state(item["topic"], log_path, meta_path, time.time())


def topic_sender(args):
    """This session's identity for a post, or None (anonymous).

    The same sources `send` uses: the Claude messaging socket or session id,
    and the Codex thread id. Either agent can inherit the other's variables
    when started from its shell, so with both present the nearer ancestor
    process owns the post; unverifiable means refused. ``--as`` overrides.
    """
    explicit = getattr(args, "as_identity", None)
    if explicit:
        ident = sp_identity.parse_typed(explicit)
        return _topic_named(ident)
    claude = None
    sock = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
    rec = sp_claude.claude_record_by_socket(sock) if sock else None
    if rec and sp_runtime.is_uuid(rec.get("sessionId")):
        claude = {"kind": "cc", "uuid": rec["sessionId"], "name": rec.get("name"),
                  "pid": rec.get("pid")}
    else:
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
        if sp_runtime.is_uuid(sid):
            matches = sp_claude.claude_record_by_target(sid)
            only = matches[0] if len(matches) == 1 else {}
            claude = {"kind": "cc", "uuid": sid, "name": only.get("name"),
                      "pid": only.get("pid")}
    tid = sp_identity._thread_from_args(args)
    codex = _topic_named({"kind": "codex", "uuid": tid}) if tid else None
    if claude and codex:
        owner = sp_process._nearest_owner(claude.get("pid"), sp_codex._codex_holder_pid(tid))
        if owner is None:
            raise TopicError(
                "this environment names both Claude session %s and Codex thread "
                "%s, and which one runs this command cannot be verified; pass "
                "--as cc:%s or --as codex:%s" % (claude["uuid"], tid, claude["uuid"], tid),
                2,
            )
        chosen = claude if owner == "cc" else codex
    else:
        chosen = claude or codex
    if chosen is None:
        return None
    chosen.pop("pid", None)
    return chosen


def _topic_named(ident):
    if ident["kind"] == "cc":
        matches = sp_claude.claude_record_by_target(ident["uuid"])
        ident["name"] = matches[0].get("name") if len(matches) == 1 else None
    else:
        entry = sp_storage.read_registered().get(ident["uuid"])
        ident["name"] = entry.get("name") if isinstance(entry, dict) else None
    return ident


def _topic_printable(text):
    """Escape control characters except newline and tab for a terminal."""
    return re.sub(
        r"[\x00-\x08\x0b-\x1f\x7f-\x9f]",
        lambda m: "\\x%02x" % ord(m.group(0)),
        text,
    )


def cmd_topic(args):
    try:
        if args.topic_cmd == "post":
            return _cmd_topic_post(args)
        if args.topic_cmd == "tail":
            return _cmd_topic_tail(args)
        if args.topic_cmd == "list":
            return _cmd_topic_list(args)
    except TopicError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return exc.code
    sys.stderr.write("error: topic subcommands are `post`, `tail` and `list`\n")
    return 2


def _cmd_topic_post(args):
    validate_topic(args.topic)
    data, text = sp_constants._NO_DATA, None
    try:
        if args.json_file:
            raw = sp_runtime.message_from_args(
                argparse.Namespace(message=None, message_file=args.json_file)
            )
            try:
                data = json.loads(raw)
            except ValueError as exc:
                raise TopicError("--json-file is not valid JSON: %s" % exc)
        else:
            text = sp_runtime.message_from_args(args)
    except UnicodeError:
        raise TopicError("the message is not valid UTF-8")
    except ValueError as exc:
        raise TopicError(str(exc))
    try:
        sender = topic_sender(args)
    except ValueError as exc:
        raise TopicError(str(exc), 2)
    if sender is None:
        sp_runtime.log(
            "sender identity absent: posting anonymously; from Claude Code use "
            "its Bash tool, from Codex its shell, or pass --as"
        )
        sender = {}
    entry = topic_post(args.topic, sender, kind=args.kind, text=text, data=data)
    if args.json:
        print(json.dumps({"topic": entry["topic"], "seq": entry["seq"], "ts": entry["ts"]}))
    else:
        print("posted %s #%d" % (args.topic, entry["seq"]))
    return 0


def _cmd_topic_tail(args):
    if args.since is not None and args.since < 0:
        raise TopicError("--since must be zero or greater", 2)
    if not 1 <= args.limit <= sp_constants.TOPIC_TAIL_MAX:
        raise TopicError("--limit must be between 1 and %d" % sp_constants.TOPIC_TAIL_MAX, 2)
    result = topic_tail(args.topic, since=args.since, limit=args.limit)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return 0
    gap = result["gap"]
    if gap:
        print("gap: entries %d..%d pruned" % (gap["from"], gap["to"]))
    for entry in result["entries"]:
        who = entry.get("from_name") or entry.get("from_sid") or "anonymous"
        kind = " [%s]" % entry["kind"] if entry.get("kind") else ""
        if "data" in entry:
            body = json.dumps(entry["data"], ensure_ascii=False)
        else:
            body = entry.get("text") or ""
        print("#%d %s %s%s: %s" % (
            entry["seq"], entry.get("ts"), _topic_printable(str(who)), kind,
            _topic_printable(body),
        ))
    print("next: --since %d" % result["next"])
    return 0


def _cmd_topic_list(args):
    topics = topic_list()
    if args.json:
        print(json.dumps(topics, ensure_ascii=False))
        return 0
    if not topics:
        print("no topics")
    for item in topics:
        print("%s  last #%s  %s" % (
            _topic_printable(item["topic"]), item["last_seq"], item["last_ts"] or "-",
        ))
    return 0
