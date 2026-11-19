"""Bounded, read-only committed-ledger snapshots for the local dashboard."""

import os
import json
from pathlib import Path
import selectors
import subprocess
import time

from . import ledger, ledger_table

MAX_BYTES = 1024 * 1024
TIMEOUT = 1.0


def valid_binding(value):
    if not isinstance(value, dict) or set(value) != {"key", "anchor", "forge"}:
        return False
    try:
        ledger.safe_key(value["key"])
        anchor = value["anchor"]
        if not isinstance(anchor, str) or not os.path.isabs(anchor) or len(anchor) > 4096:
            return False
        forge = value["forge"]
        return forge is None or (isinstance(forge, dict) and set(forge) == {"kind", "base"}
                                 and forge["kind"] in ("github", "gitlab")
                                 and ledger_table.safe_url(forge["base"]) is not None)
    except (ledger.LedgerError, TypeError):
        return False


def binding(cwd=None):
    book = ledger.Ledger(cwd)
    book.snapshot()
    return {"key": book.key, "anchor": str(book.anchor),
            "forge": ledger_table.current_forge(cwd or os.getcwd())}


class Reader:
    def __init__(self, budget):
        self.deadline = time.monotonic() + TIMEOUT
        self.left = min(MAX_BYTES, budget.remaining())
        self.budget = budget

    def __call__(self, directory, *args, allowed=(0,), raw=False, input_text=None):
        if self.left <= 0 or time.monotonic() >= self.deadline:
            raise ledger.LedgerError("ledger read budget exhausted")
        process = subprocess.Popen(ledger.git_command(*args), cwd=str(directory), env=ledger.git_env(),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output = bytearray()
        pending = memoryview((input_text or "").encode())
        try:
            with selectors.DefaultSelector() as poll:
                for stream in (process.stdin, process.stdout, process.stderr):
                    os.set_blocking(stream.fileno(), False)
                if pending:
                    poll.register(process.stdin, selectors.EVENT_WRITE, "in")
                else:
                    process.stdin.close()
                poll.register(process.stdout, selectors.EVENT_READ, "out")
                poll.register(process.stderr, selectors.EVENT_READ, "err")
                while poll.get_map():
                    remaining = self.deadline - time.monotonic()
                    if remaining <= 0:
                        raise ledger.LedgerError("ledger read timed out")
                    events = poll.select(remaining)
                    for key, _mask in events:
                        if key.data == "in":
                            try:
                                pending = pending[os.write(key.fd, pending[:4096]):]
                            except BrokenPipeError:
                                pending = pending[:0]
                            if not pending:
                                poll.unregister(key.fileobj)
                                key.fileobj.close()
                            continue
                        chunk = os.read(key.fd, min(65536, self.left + 1))
                        if not chunk:
                            poll.unregister(key.fileobj)
                            continue
                        self.budget.charge(min(len(chunk), self.left))
                        self.left -= len(chunk)
                        if self.left < 0:
                            raise ledger.LedgerError("ledger read byte limit exceeded")
                        if key.data == "out":
                            output.extend(chunk)
                code = process.wait(timeout=max(0.001, self.deadline - time.monotonic()))
                if code not in allowed:
                    raise ledger.LedgerError("ledger Git read failed")
        finally:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        return bytes(output) if raw else output.decode("utf-8")


def view(bound, budget):
    if bound is None:
        return {"state": "unbound"}
    if not valid_binding(bound):
        return {"state": "unavailable", "reason": "Invalid ledger binding"}
    try:
        # Identity comes only from an owner-written binding, never a request path.
        # No Git repo lookup through a checkout that may have been deleted.
        book = object.__new__(ledger.Ledger)
        book.key, book.anchor = bound["key"], Path(bound["anchor"])
        book.root = ledger.data_root()
        book.path = book.root / book.key
        book._git = Reader(budget)
        head, _meta = book.snapshot()
        items = book.items(head, max_items=256)
        if sum(len(item["links"]) for item in items) > 256:
            raise ledger.LedgerError("ledger panel row/link limit reached")
        result = dict(ledger_table.project(book, head, bound["forge"], items=items), state="current", observed_at=ledger.stamp())
        if len(json.dumps(result).encode()) > MAX_BYTES:
            raise ledger.LedgerError("ledger panel output limit reached")
        return result
    except (ledger.LedgerError, OSError, ValueError, TypeError, RecursionError, OverflowError, subprocess.SubprocessError):
        return {"state": "unavailable", "reason": "Ledger unavailable, changed identity, or read limit reached"}
