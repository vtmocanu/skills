"""One-shot, requester-consented OpenCode reply wakes (same-user attribution)."""

import json
import math
import os
import sys
import time
import uuid

from . import identity, requests, runtime, storage

SUFFIX = ".reply-wake.json"


def path_for(message_id):
    if not runtime.is_uuid(message_id):
        raise ValueError("reply-wake message id must be a full UUID")
    return os.path.join(storage.request_dir(), message_id + SUFFIX)


def issue(message_id, requester, recipient, seconds):
    if identity.parse_typed(requester)["kind"] != "oc":
        raise ValueError("reply-wake requester must be OpenCode")
    identity.parse_typed(recipient)
    seconds = requests._bounded_timeout(seconds)
    requests.warn_short_lifetime(seconds)
    data = {"message_id": message_id, "requester": requester, "recipient": recipient,
            "created_at": time.time(), "expires_at": time.time() + seconds}
    if not runtime.write_json_exclusive(path_for(message_id), data):
        raise ValueError("reply-wake grant already exists")
    return data


def forget(message_id):
    requests._unlink_quiet(path_for(message_id))


def find(message_id):
    if not runtime.is_uuid(message_id):
        raise ValueError("reply-wake message id must be a full UUID")
    paths = []
    for home in storage.candidate_homes():
        path = os.path.join(home, "session-peers", "requests", message_id + SUFFIX)
        try:
            os.stat(path)
        except FileNotFoundError:
            continue
        if path not in paths:
            paths.append(path)
    if len(paths) > 1:
        raise ValueError("reply-wake grant is ambiguous across CODEX_HOMEs")
    if not paths:
        raise ValueError("reply-wake grant absent, used or revoked")
    return paths[0]


def _check(data, message_id, requester, recipient):
    if not isinstance(data, dict) or data.get("message_id") != message_id:
        raise ValueError("invalid reply-wake grant")
    if data.get("requester") != requester or data.get("recipient") != recipient:
        raise ValueError("reply-wake grant does not match this sender and recipient")
    expires = data.get("expires_at")
    if (not isinstance(expires, (float, int)) or isinstance(expires, bool)
            or not math.isfinite(expires) or expires <= time.time()):
        raise ValueError("reply-wake grant expired or invalid")


def consume(message_id, requester, recipient):
    path = find(message_id)
    # Honest mismatches must not burn the intended peer's consent.
    _check(runtime.read_json(path), message_id, requester, recipient)
    claim = "%s.consumed.%s" % (path, uuid.uuid4().hex)
    try:
        os.rename(path, claim)
    except FileNotFoundError:
        raise ValueError("reply-wake grant already consumed or revoked")
    try:
        _check(runtime.read_json(claim), message_id, requester, recipient)
    except ValueError as exc:
        raise ValueError("reply-wake grant consumed during validation: %s" % exc)
    finally:
        requests._unlink_quiet(claim)


def cleanup(now, dry_run=False):
    removed = []
    try:
        names = os.listdir(storage.request_dir())
    except OSError:
        return removed
    for name in names:
        if not name.endswith(SUFFIX):
            continue
        mid = name[:-len(SUFFIX)]
        if not runtime.is_uuid(mid):
            continue
        path = path_for(mid)
        data = runtime.read_json(path, {})
        expires = data.get("expires_at") if isinstance(data, dict) else None
        if (isinstance(expires, (float, int)) and not isinstance(expires, bool)
                and math.isfinite(expires) and expires > now):
            continue
        if not dry_run:
            requests._unlink_quiet(path)
        removed.append(mid)
    return removed


def cmd_revoke(args):
    try:
        caller = identity.caller_identity(args)
        if caller["kind"] != "oc":
            raise ValueError("only the issuing OpenCode session can revoke reply wake")
        path = path_for(args.message_id)
        data = runtime.read_json(path)
        if data is not None and (not isinstance(data, dict)
                or data.get("requester") != "oc:" + caller["uuid"]):
            raise ValueError("only the issuing OpenCode session can revoke this grant")
        requests._unlink_quiet(path)
    except (ValueError, OSError) as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    print(json.dumps({"message_id": args.message_id, "status": "revoked" if data else "absent"}))
    return 0
