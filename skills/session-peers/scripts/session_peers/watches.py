"""Finite owner-bound poller observations, with optional one-shot notification."""

import argparse
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
import uuid

from . import codex, identity, inbox, opencode, process, rollout, runtime, storage

MAX_TIMEOUT = 86400
TERM_GRACE = 3


def owner_now():
    owner = identity.caller_identity(argparse.Namespace())
    if owner['kind'] not in ('oc', 'codex'):
        raise ValueError('watch requires OpenCode or Codex; use native run_in_background in Claude Code')
    return '%s:%s' % (owner['kind'], owner['uuid'])


def paths(owner, watch_id=None, create=False):
    typed = identity.parse_typed(owner)
    base = os.path.join(storage.codex_home(), 'session-peers', 'watches',
                        typed['kind'] + '-' + typed['uuid'])
    if create:
        os.makedirs(base, mode=0o700, exist_ok=True)
        os.chmod(base, 0o700)
    if watch_id is None:
        return base
    if not runtime.is_uuid(watch_id):
        raise ValueError('watch id must be a full UUID')
    return os.path.join(base, watch_id)


def load(owner, watch_id):
    with open(paths(owner, watch_id) + '.json', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict) or data.get('id') != watch_id or data.get('owner') != owner:
        raise ValueError('invalid or mismatched watch receipt')
    return data


def token_from_log(path):
    with open(path, 'rb') as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 8192))
        lines = f.read().decode('utf-8', 'replace').splitlines()
    if not lines:
        return 'unknown'
    last = lines[-1]
    # A token is an untrusted observation hint, never approval or task success.
    match = re.fullmatch(r'(RESULT=[a-zA-Z0-9_:.-]{1,100}|STOP=[a-zA-Z0-9_:.-]{1,100}(?: .{0,100})?|ELAPSED .{0,150}|EXIT=-?[0-9]{1,5})', last)
    return inbox.human_text(match.group(0)) if match else 'unknown'


def _notify(data, base):
    if not data['wake_consent']:
        data['notification'] = 'not_requested'
        return
    # Claim before the transport: a crash or uncertain write never permits resend.
    try:
        fd = os.open(base + '.notification-attempt', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except FileExistsError:
        data['notification'] = 'attempt_already_claimed_unknown'
        return
    data['notification'] = 'attempting'
    runtime.write_json_atomic(base + '.json', data)
    text = ('WATCH %s: task=%s; observation=%s; exit=%s; hint=%s. '
            'Not task completion or approval. Read the private receipt %s.json and log %s; '
            'reconcile current task/head before acting. Record handling with '
            'peers.py watch handled %s --next NEXT_ACTION_OR_BLOCKER.') % (
                data['id'], inbox.human_text(data['task'])[:300], data['result'],
                data.get('exit_code'), data['hint'], base, data['log'], data['id'])
    text = inbox.human_text(text)
    owner = identity.parse_typed(data['owner'])
    try:
        if owner['kind'] == 'oc':
            opencode.get_session(owner['uuid'])
            data['inbox_id'] = opencode.deliver(owner['uuid'], text,
                {'message_id': data['id'], 'from': data['owner']}, resume=True)
        else:
            thread = codex.resolve_thread(owner['uuid'])
            held, _ = codex.thread_is_held(thread['rollout_path'],
                lock_path=codex.writer_lock_path(thread['id'], home=thread.get('codex_home')))
            if held is not True:
                data['notification'] = 'owner_unavailable'
                return
            if rollout.thread_is_paused(thread['rollout_path']):
                data['notification'] = 'skipped_paused'
                return
            codex.codex_queue(owner['uuid'], text, cwd=thread.get('cwd'), home=thread.get('codex_home'))
        data['notification'] = 'queued_unacknowledged'
    except (ValueError, OSError, opencode.OpenCodeError, codex.ResolveError, codex.QueueError) as exc:
        data['notification'] = 'failed_or_unconfirmed'
        data['notification_error'] = str(exc)


def run_observation(data, argv, base):
    """Run once; callable synchronously in tests, detached in the CLI."""
    data.update(worker_pid=os.getpid(), worker_start=process.proc_start(os.getpid()),
                status='starting', worker_started_at=runtime.now_iso())
    runtime.write_json_atomic(base + '.json', data)
    started = time.monotonic()
    try:
        fd = os.open(data['log'], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as output:
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output,
                stderr=subprocess.STDOUT, cwd=data['cwd'], start_new_session=True, close_fds=True)
            data.update(status='observing', child_pid=child.pid, child_start=process.proc_start(child.pid))
            runtime.write_json_atomic(base + '.json', data)
            try:
                data['exit_code'] = child.wait(timeout=max(0, min(data['timeout'], data['deadline']-time.time())))
                data['result'] = 'command_exited'
            except subprocess.TimeoutExpired:
                # Do not reap the group leader until both signals are sent: its
                # unreaped PID cannot be recycled into an unrelated process group.
                try:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    time.sleep(TERM_GRACE)
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    data.update(exit_code=child.wait(), result='timeout', cleanup='group_signalled')
                except PermissionError as exc:
                    # Darwin can return EPERM for a zombie-only group. Reap our
                    # leader, then probe without a signal. NEVER signal the group
                    # again after reaping: its numeric id could now be reused.
                    code = child.poll()
                    gone = False
                    try:
                        os.killpg(child.pid, 0)
                    except ProcessLookupError:
                        gone = True
                    except PermissionError:
                        pass
                    if code is not None and gone:
                        data.update(exit_code=code, result='timeout', cleanup='verified_gone')
                    else:
                        data.update(exit_code=code, result='timeout_cleanup_refused',
                                    cleanup='remaining_unknown', cleanup_error=str(exc))
        data['hint'] = token_from_log(data['log'])
    except (OSError, ValueError) as exc:
        data.update(result='launch_error', exit_code=None, hint='unknown', error=str(exc))
    data.update(status='finished', ended_at=runtime.now_iso(), elapsed_seconds=time.monotonic()-started)
    runtime.write_json_atomic(base + '.json', data)
    _notify(data, base)
    runtime.write_json_atomic(base + '.json', data)


def start(owner, task, timeout, argv, wake=False):
    if isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT:
        raise ValueError('timeout must be finite, positive and at most 86400 seconds')
    if not argv or not argv[0] or not task.strip() or len(task) > 500:
        raise ValueError('supply a task label (1..500 chars) and a nonempty poller argv after --')
    watch_id = str(uuid.uuid4())
    paths(owner, create=True)
    base = paths(owner, watch_id)
    data = dict(id=watch_id, owner=owner, task=task, timeout=timeout, wake_consent=wake,
                receipt_version=1, loaded_code_digest=runtime.LOADED_CODE_DIGEST,
                cwd=os.getcwd(), created_at=runtime.now_iso(), deadline=time.time()+timeout,
                argv_sha256=hashlib.sha256(json.dumps(argv).encode()).hexdigest(),
                log=base+'.log', status='launching', notification='not_attempted')
    runtime.write_json_atomic(base + '.json', data)
    pid = os.fork()
    if pid:
        os.waitpid(pid, 0)
        return data
    try:
        os.setsid()
        if os.fork():
            os._exit(0)
        os.umask(0o077)
        os.closerange(0, int(os.sysconf('SC_OPEN_MAX')))
        fd = os.open(os.devnull, os.O_RDWR)
        for n in range(3):
            os.dup2(fd, n)
        run_observation(data, argv, base)
    finally:
        os._exit(0)


def listing(owner):
    base = paths(owner)
    try:
        names = os.listdir(base)
    except FileNotFoundError:
        return []
    rows = []
    for name in sorted(names):
        if not name.endswith('.json') or name.endswith('.handled.json'):
            continue
        data = load(owner, name[:-5])
        if data['status'] != 'finished':
            pid, expected = data.get('worker_pid'), data.get('worker_start')
            current = process.proc_start(pid) if pid else None
            data['worker_liveness'] = 'matched' if expected and current == expected else 'unknown_or_lost'
        ack = paths(owner, data['id']) + '.handled.json'
        if os.path.exists(ack):
            data['handling'] = read_handling(ack, owner, data['id'])
        else:
            data['handling'] = None
        rows.append(data)
    return rows


def read_handling(path, owner, watch_id):
    with open(path, encoding='utf-8') as f:
        ack = json.load(f)
    if (not isinstance(ack, dict) or ack.get('owner') != owner or ack.get('id') != watch_id
            or not isinstance(ack.get('next'), str) or not ack['next'].strip()):
        raise ValueError('invalid or mismatched watch handling receipt')
    return ack


def handled(owner, watch_id, next_action):
    data = load(owner, watch_id)
    if data.get('status') != 'finished':
        raise ValueError('only a finished observation can be marked handled')
    if not next_action.strip() or len(next_action) > 2000:
        raise ValueError('record the actual next action or explicit blocker (1..2000 chars)')
    ack = dict(id=watch_id, owner=owner, handled_at=runtime.now_iso(), next=next_action)
    path = paths(owner, watch_id)+'.handled.json'
    if not runtime.write_json_exclusive(path, ack):
        read_handling(path, owner, watch_id)


def cmd_watch(args):
    try:
        owner = owner_now()
        if args.watch_cmd == 'start':
            argv = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
            data = start(owner, args.task, args.timeout, argv, args.wake)
            print(json.dumps(data, sort_keys=True))
        elif args.watch_cmd == 'handled':
            handled(owner, args.watch_id, args.next)
            print(json.dumps({'id': args.watch_id, 'status': 'handled_recorded'}))
        else:
            rows = listing(owner)
            if args.json:
                print(json.dumps(rows, sort_keys=True))
            else:
                for row in rows:
                    print(inbox.human_text('%s %s %s notification=%s handled=%s task=%s' % (
                        row['id'], row['status'], row.get('result', row.get('worker_liveness', 'unknown')),
                        row['notification'], bool(row['handling']), row['task'])))
    except (ValueError, OSError, opencode.OpenCodeError, codex.ResolveError) as exc:
        sys.stderr.write('error: %s\n' % exc)
        return 1
    return 0
