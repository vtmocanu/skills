"""Nonconsuming own-OpenCode inbox checks and explicit local handled markers."""

import json
import math
import os
import re
import sys
import time
import unicodedata

from . import identity, opencode, runtime, storage

HANDLED_TTL = 14 * 86400
HUMAN_TEXT_LIMIT = 4000
MISSING = object()


def own_session(args):
    caller = identity.caller_identity(args)
    if caller["kind"] != "oc" or caller["uuid"] != opencode.current_session_id():
        raise ValueError("inbox requires this shell's own OPENCODE_SESSION_ID")
    return caller["uuid"]


def ledger_path(sid):
    if not opencode.is_session_id(sid):
        raise ValueError("invalid OpenCode session id")
    # Read paths must not call state_dir(): it creates/chmods directories.
    return os.path.join(storage.codex_home(), "session-peers", "opencode-inbox", sid + ".json")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return MISSING


def _valid_pair(sender, mid):
    try:
        identity.parse_typed(sender)
    except ValueError:
        return False
    return runtime.is_uuid(mid)


def _timestamp(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _ledger(sid):
    data = _read_json(ledger_path(sid))
    if data is MISSING:
        return []
    if (not isinstance(data, dict) or data.get("session_id") != sid
            or not isinstance(data.get("handled"), list)):
        raise ValueError("invalid inbox handled ledger")
    for row in data["handled"]:
        if (not isinstance(row, dict) or not _valid_pair(row.get("from"), row.get("message_id"))
                or not _timestamp(row.get("handled_at"))):
            raise ValueError("invalid inbox handled entry")
    return data["handled"]


def is_handled(sid, sender, mid):
    return any(r["from"] == sender and r["message_id"] == mid
               and r["handled_at"] > time.time() - HANDLED_TTL for r in _ledger(sid))


def mark_handled(sid, sender, mid):
    if not _valid_pair(sender, mid):
        raise ValueError("handled requires a typed sender address and full message UUID")
    path = ledger_path(sid)
    storage.state_dir()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    os.chmod(os.path.dirname(path), 0o700)
    with storage._flock_file(path + ".lock", None, "OpenCode inbox handled ledger"):
        now = time.time()
        rows = [r for r in _ledger(sid) if r["handled_at"] > now - HANDLED_TTL]
        if not any(r["from"] == sender and r["message_id"] == mid for r in rows):
            rows.append({"from": sender, "message_id": mid, "handled_at": now})
        runtime.write_json_atomic(path, {"session_id": sid, "handled": rows}, mode=0o600)


def request_state(rid, sid):
    """Read existing mailboxes without cleanup, creation, consumption or fallback."""
    if not runtime.is_uuid(rid):
        return "unknown"
    found = []
    for home in storage.candidate_homes():
        base = os.path.join(home, "session-peers", "requests", rid)
        meta = _read_json(base + ".request.json")
        if meta is not MISSING:
            found.append((base, meta))
    if not found:
        return "unknown"  # Missing may mean consumed, expired or undiscoverable.
    if len(found) != 1:
        raise ValueError("ambiguous inbox request mailbox")
    base, meta = found[0]
    if (not isinstance(meta, dict) or meta.get("request_id") != rid
            or meta.get("target_kind") != "oc" or meta.get("target_session_id") != sid
            or not _timestamp(meta.get("expires_at"))):
        raise ValueError("invalid inbox request mailbox")
    if meta["expires_at"] <= time.time():
        return "expired"
    reply = _read_json(base + ".reply.json")
    if reply is not MISSING:
        if (not isinstance(reply, dict) or reply.get("request_id") != rid
                or reply.get("session_id") != sid or not isinstance(reply.get("message"), str)):
            raise ValueError("invalid inbox request reply")
        return "replied"
    return "pending"


def list_items(sid, include_handled=False):
    body = opencode.api("session.inbox.list", {"sessionID": sid})
    data = body.get("data")
    if not isinstance(data, list):
        raise ValueError("session.inbox.list returned an invalid shape")
    rows = _ledger(sid)
    handled = {(r["from"], r["message_id"]) for r in rows
               if r["handled_at"] > time.time() - HANDLED_TTL}
    result = []
    for item in data:
        if not isinstance(item, dict) or item.get("sessionID") != sid:
            raise ValueError("session.inbox.list returned a foreign or invalid item")
        payload = item.get("payload")
        if item.get("type") not in ("user", "synthetic") or not isinstance(payload, dict):
            continue
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict) or "session_peers" not in metadata:
            continue
        peer = metadata["session_peers"]
        if not isinstance(peer, dict) or not isinstance(payload.get("text"), str):
            raise ValueError("invalid peer inbox payload")
        sender, mid = peer.get("from"), peer.get("message_id")
        verified = _valid_pair(sender, mid)
        done = verified and (sender, mid) in handled
        if done and not include_handled:
            continue
        created = item.get("time", {}).get("created") if isinstance(item.get("time"), dict) else None
        rid = peer.get("request_id")
        result.append({"inbox_id": item.get("id"), "from": sender, "message_id": mid,
                       "request_id": rid, "in_reply_to": peer.get("in_reply_to"),
                       "request_state": request_state(rid, sid) if rid else None,
                       "age_seconds": max(0, time.time() - created / 1000) if _timestamp(created) else None,
                       "sender_well_formed": verified, "handled": done, "text": payload["text"]})
    return result


def human_text(text):
    # Remove OSC/CSI sequences before controls; never let peer text drive a terminal.
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]", "", str(text))
    text = "".join(c if c in "\n\t" or not unicodedata.category(c).startswith("C") else " " for c in text)
    return text[:HUMAN_TEXT_LIMIT] + ("\n[truncated; use --json]" if len(text) > HUMAN_TEXT_LIMIT else "")


def cmd_inbox(args):
    try:
        sid = own_session(args)
        if args.inbox_cmd == "handled":
            mark_handled(sid, args.sender, args.message_id)
            result = {"from": args.sender, "message_id": args.message_id, "handled": True}
        elif args.inbox_cmd == "status":
            if not _valid_pair(args.sender, args.message_id):
                raise ValueError("status requires a typed sender and full message UUID")
            result = {"from": args.sender, "message_id": args.message_id,
                      "handled": is_handled(sid, args.sender, args.message_id)}
        else:
            result = list_items(sid, args.all)
    except (ValueError, OSError, storage.BindingLockTimeout, opencode.OpenCodeError) as exc:
        sys.stderr.write("error: %s\n" % human_text(exc))
        return 1
    if args.json or not isinstance(result, list):
        print(json.dumps(result, ensure_ascii=True))
    elif not result:
        print("No unhandled queued peer messages." if not args.all else "No queued peer messages.")
    else:
        for row in result:
            header = {k: v for k, v in row.items() if k != "text"}
            print(human_text(json.dumps(header, ensure_ascii=True)))
            print(human_text(row["text"]))
    return 0
