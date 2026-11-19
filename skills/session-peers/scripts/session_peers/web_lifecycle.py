"""Opt-in, user-wide stable dashboard lifecycle. No service manager or remote listener."""

import contextlib
import errno
import fcntl
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import stat
import subprocess
import sys
import time

from . import process, runtime

TOKEN = re.compile(r"[0-9a-f]{64}")
WAIT = 10.0


class LifecycleError(Exception):
    pass


def root():
    # Shared across runtimes and CODEX_HOMEs, like the channel store.
    return Path.home() / ".codex" / "session-peers" / "web"


def private_open(path, flags):
    fd = os.open(path, flags | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise LifecycleError("dashboard state must be an owned regular file: %s" % path)
    os.fchmod(fd, 0o600)
    return fd


@contextlib.contextmanager
def locked():
    directory = root()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise LifecycleError("dashboard state directory is not owned or is a symlink")
    directory.chmod(0o700)
    fd = private_open(directory / "lock", os.O_RDWR | os.O_CREAT)
    deadline = time.monotonic() + 2 * WAIT
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LifecycleError("dashboard lifecycle lock is busy; retry")
                time.sleep(0.05)
        yield fd
    finally:
        os.close(fd)


def read(path):
    try:
        fd = private_open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        if os.fstat(stream.fileno()).st_size > 4096:
            raise LifecycleError("dashboard state is oversized: %s" % path)
        data = stream.read(4097)
        if len(data) > 4096:
            raise LifecycleError("dashboard state grew beyond its limit: %s" % path)
        value = json.loads(data)
    if not isinstance(value, dict):
        raise LifecycleError("invalid dashboard state: %s" % path)
    return value


def write(path, value):
    # The directory is private; the existing atomic writer creates at 0600.
    runtime.write_json_atomic(str(path), value, mode=0o600)


def first_port(start):
    for port in range(start, 65536):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise LifecycleError("no free dashboard port")


def config(chat=False):
    path = root() / ("chat.json" if chat else "config.json")
    value = read(path)
    if value is None:
        value = {"version": 1, "port": first_port(8766 if chat else 8765), "token": secrets.token_hex(32)}
        # Separate storage and independent randomness keep chat off the read-only token.
        other = read(root() / ("config.json" if chat else "chat.json"))
        while other and value["token"] == other.get("token"):
            value["token"] = secrets.token_hex(32)
        if other and value["port"] == other.get("port"):
            value["port"] = first_port(value["port"] + 1)
        write(path, value)
    if (value.get("version") != 1 or type(value.get("port")) is not int
            or not 1024 <= value["port"] <= 65535 or not isinstance(value.get("token"), str)
            or not TOKEN.fullmatch(value["token"])):
        raise LifecycleError("invalid dashboard configuration: %s" % path)
    other = read(root() / ("config.json" if chat else "chat.json"))
    if other and value["token"] == other.get("token"):
        raise LifecycleError("chat and read-only dashboard tokens must differ")
    return path, value


def url(cfg):
    return "http://127.0.0.1:%d/#token=%s" % (cfg["port"], cfg["token"])


def distinct_port(port, chat=False):
    other = read(root() / ("config.json" if chat else "chat.json"))
    if other and other.get("port") == port:
        raise LifecycleError("persistent chat and read-only dashboard ports must differ")


def recovery(chat=False):
    path = root() / ("chat-server.json" if chat else "server.json")
    return ("Manually inspect %s and its recorded process/port. Only after independently confirming "
            "the recorded server is stopped or stale, remove %s and retry. Keep the config to preserve "
            "the URL; never signal an unverified PID." % (path, path))


def digest():
    script = Path(__file__).resolve().parent.parent / "peers.py"
    assets = sorted(str(p) for p in (script.parent / "session_peers" / "web").rglob("*") if p.is_file())
    asset_digest = runtime.code_digest([str(script)] + assets)
    if not asset_digest or not runtime.LOADED_CODE_DIGEST:
        raise LifecycleError("cannot identify loaded dashboard code")
    return hashlib.sha256((runtime.LOADED_CODE_DIGEST + asset_digest).encode()).hexdigest()


def probe_proof(token, nonce, payload=None):
    # Separate domains prevent a captured health proof from authenticating any other request.
    message = "session-peers-health-v1-request:" + nonce
    if payload is not None:
        message = "session-peers-health-v1-response:" + nonce + ":" + json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(token.encode("ascii"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def valid_probe(token, nonce, proof):
    return (isinstance(nonce, str) and re.fullmatch(r"[0-9a-f]{32}", nonce) is not None
            and isinstance(proof, str) and TOKEN.fullmatch(proof) is not None
            and hmac.compare_digest(proof, probe_proof(token, nonce)))


def health(cfg):
    conn = http.client.HTTPConnection("127.0.0.1", cfg["port"], timeout=1)
    nonce = secrets.token_hex(16)
    try:
        # Authenticate without disclosing the saved token to a foreign listener on this port.
        conn.request("GET", "/api/instance", headers={"X-Peers-Probe": nonce,
                                                      "X-Peers-Proof": probe_proof(cfg["token"], nonce)})
        response = conn.getresponse()
        data = response.read(4097)
        if response.status != 200 or len(data) > 4096:
            return None
        value = json.loads(data)
        if not isinstance(value, dict):
            return None
        proof = value.pop("proof", None)
        return value if isinstance(proof, str) and TOKEN.fullmatch(proof) and hmac.compare_digest(proof, probe_proof(cfg["token"], nonce, value)) else None
    except (OSError, ValueError, RecursionError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def process_state(record):
    if not record:
        return "gone"
    pid = record.get("pid")
    if type(pid) is not int or pid <= 0 or record.get("pid_domain") != process.pid_domain():
        raise LifecycleError("invalid dashboard pidfile. " + recovery())
    if not process.pid_alive(pid):
        return "gone"
    actual, error = process.proc_start_checked(pid)
    if error or not record.get("proc_start"):
        return "unverified"
    return "live" if actual == record["proc_start"] else "gone"


def verified(record, cfg):
    found = health(cfg)
    return bool(found and found.get("service") == "session-peers-web" and found.get("read_only") is True
                and found.get("pid") == record.get("pid") and found.get("instance") == record.get("instance")
                and found.get("code_digest") == record.get("code_digest") and record.get("port") == cfg["port"])


def stop(record, cfg):
    state = process_state(record)
    if state == "gone":
        return
    if state != "live" or not verified(record, cfg):
        raise LifecycleError("cannot verify the recorded dashboard; refusing to signal its PID. " + recovery())
    os.kill(record["pid"], signal.SIGTERM)
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        if process_state(record) == "gone" or (read(root() / "server.json") is None and health(cfg) is None):
            return
        time.sleep(0.05)
    raise LifecycleError("dashboard stop is not yet confirmed; retry --stop")


def cleanup(instance, chat=False):
    # A replacement cannot start while this PID still verifies live. Never remove another instance's record.
    path = root() / ("chat-server.json" if chat else "server.json")
    try:
        record = read(path)
        if record and record.get("instance") == instance:
            path.unlink()
    except (OSError, ValueError, LifecycleError):
        pass


def ensure(cfg, fd):
    log_fd = private_open(root() / "server.log", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    script = str(Path(__file__).resolve().parent.parent / "peers.py")
    try:
        child = subprocess.Popen([sys.executable, script, "web", "--managed-child", "--lock-fd", str(fd)],
                                 stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd,
                                 start_new_session=True, pass_fds=(fd,))
    finally:
        os.close(log_fd)
    ready = False
    try:
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            record = read(root() / "server.json")
            if record and record.get("pid") == child.pid and process_state(record) == "live" and verified(record, cfg):
                ready = True
                return
            if child.poll() is not None:
                raise LifecycleError("dashboard could not start on 127.0.0.1:%d (port may be held by another process); inspect %s"
                                     % (cfg["port"], root() / "server.log"))
            time.sleep(0.05)
        raise LifecycleError("dashboard startup unverified on 127.0.0.1:%d; inspect %s, then retry --ensure"
                             % (cfg["port"], root() / "server.log"))
    finally:
        if not ready:
            # This unreaped Popen child is ours, not a PID recovered from disk. Kill also
            # releases its inherited flock, so a slow/failed startup cannot strand every verb.
            if child.poll() is None:
                child.kill()
            try:
                child.wait(timeout=max(WAIT, 1.0))
            except subprocess.TimeoutExpired as exc:
                raise LifecycleError("startup child %d could not be reaped; inspect its process before retrying" % child.pid) from exc
            with contextlib.suppress(OSError, ValueError, LifecycleError):
                record = read(root() / "server.json")
                if record and record.get("pid") == child.pid:
                    cleanup(record.get("instance"))


def command(args):
    with locked() as fd:
        path, cfg = config()
        if args.port is not None:
            distinct_port(args.port)
        if args.url and args.port is None:
            print(url(cfg))
            return 0
        record = read(root() / "server.json")
        state = process_state(record)
        if state == "unverified":
            raise LifecycleError("recorded dashboard process identity is unverified. " + recovery())
        if args.stop:
            stop(record, cfg)
            cleanup(record.get("instance") if record else None)
            print("Dashboard stopped" if state == "live" else "No recorded dashboard is running")
            return 0
        if args.rotate_token or (args.port is not None and args.port != cfg["port"]):
            if state != "gone":
                raise LifecycleError("stop the recorded dashboard with --stop before changing its port or token")
            if args.port is not None:
                cfg["port"] = args.port
            if args.rotate_token:
                cfg["token"] = secrets.token_hex(32)
                other = read(root() / "chat.json")
                while other and cfg["token"] == other.get("token"):
                    cfg["token"] = secrets.token_hex(32)
            write(path, cfg)
        if args.url or args.rotate_token:
            print(url(cfg))
            return 0
        if args.restart:
            stop(record, cfg)
            cleanup(record.get("instance") if record else None)
            state = "gone"
        if state != "gone":
            if state != "live" or not verified(record, cfg):
                raise LifecycleError("recorded dashboard identity or health is unverified; no second server started. " + recovery())
            if record.get("code_digest") != digest():
                raise LifecycleError("dashboard runs older code; run web --ensure --restart (same port and token)")
        else:
            ensure(cfg, fd)
        print(url(cfg))
        return 0


def server_record(server):
    started, error = process.proc_start_checked(os.getpid())
    if error or not started:
        raise LifecycleError("cannot verify dashboard process start")
    return {"pid": os.getpid(), "pid_domain": process.pid_domain(), "proc_start": started,
            "port": server.port, "instance": server.instance, "code_digest": server.code_digest}


def port_probe(port):
    # No application bytes or credentials. Unknown errors are not evidence of a free port.
    with socket.socket() as probe:
        probe.settimeout(0.1)
        result = probe.connect_ex(("127.0.0.1", port))
        if result == 0 and probe.getpeername() != ("127.0.0.1", port):
            return errno.EADDRNOTAVAIL
        return result


def stable_server(web, port, chat=False):
    class StableServer(web.Server):
        allow_reuse_address = True

    deadline = time.monotonic() + WAIT
    while True:
        if port_probe(port) != errno.ECONNREFUSED:
            raise LifecycleError("dashboard port 127.0.0.1:%d has a live or unverified holder; refusing to bind" % port)
        # Reuse only after a refused connection: TIME_WAIT cannot accept a probe. This
        # is not atomic with bind; a foreign listener appearing between them is an
        # accepted narrow race. On Linux an existing reusable but non-listening
        # reservation can also lose the port (its later listen fails loudly).
        # Never treat a timeout/unknown probe as ECONNREFUSED.
        try:
            server = StableServer(port, chat=chat)
            break
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)  # a verified old instance may still be releasing this exact port
    try:
        if (server.server_address != ("127.0.0.1", port)
                or server.socket.getsockname() != ("127.0.0.1", port) or port_probe(port) != 0):
            raise LifecycleError("dashboard bind/probe did not verify the exact loopback address")
        server.instance = secrets.token_hex(16)
        server.code_digest = digest()
        return server
    except Exception:
        server.server_close()
        raise


def persistent_chat(args, web):
    with locked():
        path, cfg = config(chat=True)
        if process_state(read(root() / "chat-server.json")) != "gone":
            raise LifecycleError("a persistent chat dashboard is already running or unverified. " + recovery(chat=True))
        if args.port is not None:
            distinct_port(args.port, chat=True)
            cfg["port"] = args.port
            write(path, cfg)
        server = stable_server(web, cfg["port"], chat=True)
        server.token = cfg["token"]
        try:
            write(root() / "chat-server.json", server_record(server))
        except Exception:
            server.server_close()
            raise
    try:
        return web.serve(server)
    finally:
        cleanup(server.instance, chat=True)


def child(args, web):
    fd = args.lock_fd
    info, expected = os.fstat(fd), (root() / "lock").stat()
    if (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino):
        raise LifecycleError("managed startup needs its inherited lifecycle lock")
    _path, cfg = config()

    server = stable_server(web, cfg["port"])
    server.token = cfg["token"]
    try:
        record = server_record(server)
    except Exception:
        server.server_close()
        raise

    def ready():
        write(root() / "server.json", record)
        os.close(fd)  # parent still holds its copy until authenticated health succeeds

    try:
        return web.serve(server, ready=ready)
    finally:
        cleanup(server.instance)
