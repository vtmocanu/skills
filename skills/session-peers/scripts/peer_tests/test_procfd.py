"""The native (libproc) holder probe against real descriptors, on macOS.

Holders are real child processes: a signed copy of /bin/sleep named
`codex-probe` keeps a file open on stdin, so the probe sees a codex-named process with that
descriptor exactly as it sees a Codex TUI with its rollout.
"""

from __future__ import annotations

import ctypes
import errno
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

from session_peers import codex as sp_codex
from session_peers import process as sp_process
from session_peers import procfd as sp_procfd

from .support import Base

DARWIN = sys.platform == "darwin" and sp_procfd.available()
REAL_LSOF = shutil.which("lsof", path="/usr/sbin:/usr/bin")


_BIN_DIR = None


def _codex_bin():
    """An ad-hoc signed copy of /bin/sleep named `codex-probe`, built once.

    macOS kills an unsigned copy of a platform binary at exec, and a symlink
    keeps the target's name, so the copy is re-signed.
    """
    global _BIN_DIR
    if _BIN_DIR is None:
        _BIN_DIR = pathlib.Path(tempfile.mkdtemp(prefix="spfdbin", dir="/tmp"))
        target = _BIN_DIR / "codex-probe"
        shutil.copy("/bin/sleep", target)
        subprocess.run(
            ["/usr/bin/codesign", "-f", "-s", "-", str(target)],
            check=True, capture_output=True, timeout=60,
        )
    return _BIN_DIR / "codex-probe"


def tearDownModule():
    if _BIN_DIR is not None:
        shutil.rmtree(_BIN_DIR, True)


class _Holders:
    """Children holding files open on stdin, killed on cleanup."""

    def __init__(self, test, _root=None):
        self.test = test
        self.codex_bin = _codex_bin()
        self.plain_bin = pathlib.Path("/bin/sleep")
        self.procs = []

    def hold(self, path, codex=True):
        handle = open(path, "rb")
        try:
            proc = subprocess.Popen(
                [str(self.codex_bin if codex else self.plain_bin), "60"], stdin=handle
            )
        finally:
            handle.close()
        self.procs.append(proc)
        self.test.addCleanup(self._stop, proc)
        return proc

    @staticmethod
    def _stop(proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait()


@unittest.skipUnless(DARWIN, "native holder probe is macOS-only")
class NativeProbeTests(Base):
    def setUp(self):
        super().setUp()
        os.environ["SESSION_PEERS_HOLDER_PROBE"] = "native"
        # Real resolution of /tmp -> /private/tmp keeps the symlink case honest.
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="spfd", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.holders = _Holders(self, self.work)
        self.rollout = self.work / "rollout.jsonl"
        self.rollout.write_text("{}\n")
        self.argv_log = self.root / "lsof-argv.log"
        os.environ["FAKE_LSOF_ARGV_LOG"] = str(self.argv_log)

    def assertNoLsof(self):
        self.assertFalse(
            self.argv_log.exists() and self.argv_log.read_text().strip(),
            "the native probe forked lsof",
        )

    def test_scoped_probe_finds_the_holder(self):
        child = self.holders.hold(self.rollout)
        held, pid = sp_codex.thread_is_held(str(self.rollout), child.pid)
        self.assertEqual((held, pid), (True, child.pid))
        self.assertNoLsof()

    def test_unscoped_probe_finds_codex_holders_only(self):
        codex_child = self.holders.hold(self.rollout)
        self.holders.hold(self.rollout, codex=False)
        holders, verified, error = sp_process.lsof_holders_checked([str(self.rollout)])
        self.assertTrue(verified, error)
        rows = holders[sp_process.canon_path(str(self.rollout))]
        self.assertEqual([pid for pid, _cmd in rows], [codex_child.pid])
        self.assertEqual(rows[0][1], "codex-probe")
        self.assertNoLsof()

    def test_a_symlinked_probe_path_matches(self):
        child = self.holders.hold(self.rollout)
        link = self.work / "home-link"
        link.symlink_to(self.work, target_is_directory=True)
        probed = str(link / "rollout.jsonl")
        holders, verified, _error = sp_process.lsof_holders_checked([probed], pid=child.pid)
        self.assertTrue(verified)
        self.assertIn(sp_process.canon_path(probed), holders)

    def test_a_replaced_file_is_not_held_by_the_old_descriptor(self):
        child = self.holders.hold(self.rollout)
        fresh = self.work / "fresh.jsonl"
        fresh.write_text("{}\n")
        os.replace(fresh, self.rollout)
        self.assertEqual(sp_codex.thread_is_held(str(self.rollout), child.pid), (False, None))

    def test_a_dead_pid_is_verified_not_held(self):
        child = self.holders.hold(self.rollout)
        other = self.holders.hold(self.work / "rollout.jsonl")
        self.assertEqual(sp_codex.thread_is_held(str(self.rollout), child.pid)[0], True)
        child.kill()
        child.wait()
        self.assertEqual(sp_codex.thread_is_held(str(self.rollout), child.pid), (False, None))
        # The unscoped walk still sees the surviving holder, not the dead one.
        holders, verified, _error = sp_process.lsof_holders_checked([str(self.rollout)])
        self.assertTrue(verified)
        self.assertEqual(
            [pid for pid, _ in holders[sp_process.canon_path(str(self.rollout))]], [other.pid]
        )

    def test_either_handle_keeps_the_thread_live(self):
        lock = self.work / "thread.lock"
        lock.write_text("")
        child = self.holders.hold(lock)
        held, pid = sp_codex.thread_is_held(str(self.rollout), child.pid, str(lock))
        self.assertEqual((held, pid), (True, child.pid))

    def test_matches_real_lsof(self):
        if REAL_LSOF is None:
            self.skipTest("no lsof on this host")
        a = self.holders.hold(self.rollout)
        b = self.holders.hold(self.rollout)
        self.holders.hold(self.rollout, codex=False)
        native, verified, _error = sp_process.lsof_holders_checked([str(self.rollout)])
        self.assertTrue(verified)
        out = subprocess.run(
            [REAL_LSOF, "-F", "pcn", "--", str(self.rollout)],
            capture_output=True, text=True, timeout=60,
        ).stdout
        real, pid, cmd = set(), None, ""
        for line in out.splitlines():
            if line.startswith("p"):
                pid = int(line[1:])
            elif line.startswith("c"):
                cmd = line[1:]
            elif line.startswith("n") and "codex" in cmd.lower():
                real.add((pid, cmd))
        self.assertEqual(real, {(a.pid, "codex-probe"), (b.pid, "codex-probe")})
        self.assertEqual(set(native[sp_process.canon_path(str(self.rollout))]), real)


class _FakeLib:
    """Wraps the real libproc, overriding one call to inject a failure."""

    def __init__(self, real, **overrides):
        self._real = real
        self._overrides = overrides

    def __getattr__(self, name):
        return self._overrides.get(name) or getattr(self._real, name)


def _fail_with(err, result=0):
    def call(*_args):
        ctypes.set_errno(err)
        return result
    return call


@unittest.skipUnless(DARWIN, "native holder probe is macOS-only")
class NativeProbeFailClosedTests(Base):
    """Incomplete or refused enumeration is unverified, never 'not held', and
    never retried through lsof."""

    def setUp(self):
        super().setUp()
        os.environ["SESSION_PEERS_HOLDER_PROBE"] = "native"
        self.work = pathlib.Path(tempfile.mkdtemp(prefix="spfd", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.holders = _Holders(self, self.work)
        self.rollout = self.work / "rollout.jsonl"
        self.rollout.write_text("{}\n")
        self.child = self.holders.hold(self.rollout)
        self.argv_log = self.root / "lsof-argv.log"
        os.environ["FAKE_LSOF_ARGV_LOG"] = str(self.argv_log)
        real = sp_procfd._load_libproc()
        saved = sp_procfd._libproc
        self.addCleanup(setattr, sp_procfd, "_libproc", saved)
        self.real = real

    def inject(self, **overrides):
        sp_procfd._libproc = _FakeLib(self.real, **overrides)

    def assertUnverified(self, pid=None):
        holders, verified, error = sp_process.lsof_holders_checked(
            [str(self.rollout)], pid=pid
        )
        self.assertFalse(verified)
        self.assertTrue(error)
        self.assertEqual(holders, {})
        self.assertFalse(self.argv_log.exists(), "fell back to lsof")
        return error

    def test_a_full_descriptor_buffer_is_unverified(self):
        real = self.real

        def full(pid, flavor, arg, buf, size):
            if buf is None:
                return real.proc_pidinfo(pid, flavor, arg, buf, size)
            return size

        self.inject(proc_pidinfo=full)
        self.assertIn("incomplete", self.assertUnverified(self.child.pid))

    def test_a_ragged_descriptor_list_is_unverified(self):
        real = self.real

        def ragged(pid, flavor, arg, buf, size):
            got = real.proc_pidinfo(pid, flavor, arg, buf, size)
            return got - 3 if buf is not None and got > 8 else got

        self.inject(proc_pidinfo=ragged)
        self.assertUnverified(self.child.pid)

    def test_a_short_descriptor_record_is_unverified(self):
        self.inject(proc_pidfdinfo=lambda *_a: 100)
        self.assertUnverified(self.child.pid)

    def test_a_descriptor_read_error_is_unverified(self):
        self.inject(proc_pidfdinfo=_fail_with(errno.EIO))
        self.assertUnverified(self.child.pid)

    def test_a_refused_descriptor_list_is_unverified(self):
        self.inject(proc_pidinfo=_fail_with(errno.EPERM))
        self.assertUnverified(self.child.pid)

    def test_a_refused_name_is_unverified_when_scoped(self):
        self.inject(proc_name=_fail_with(errno.EPERM))
        self.assertIn("not permitted", self.assertUnverified(self.child.pid))

    def test_a_refused_name_of_our_own_process_is_unverified_unscoped(self):
        # The walk lists only this UID's processes, so a refusal there may hide
        # our own Codex holder: never a verified "no holder".
        real = self.real
        child = self.child.pid

        def refuse_child(pid, buf, size):
            if pid == child:
                ctypes.set_errno(errno.EPERM)
                return 0
            return real.proc_name(pid, buf, size)

        self.inject(proc_name=refuse_child)
        self.assertIn("not permitted", self.assertUnverified())

    def test_the_unscoped_walk_lists_only_this_uids_processes(self):
        # Another user's processes (launchd is root) are never inspected, so
        # their refusals cannot reach the answer; ours are all listed.
        pids = sp_procfd._pids(self.real)
        self.assertIn(os.getpid(), pids)
        self.assertIn(self.child.pid, pids)
        self.assertNotIn(1, pids)

    def test_a_full_pid_list_is_unverified(self):
        real = self.real

        def full(kind, uid, buf, size):
            got = real.proc_listpids(kind, uid, buf, size)
            return size if buf is not None else got

        self.inject(proc_listpids=full)
        self.assertIn("kept filling", self.assertUnverified())

    def test_auto_does_not_retry_a_failed_native_probe_through_lsof(self):
        # Falling back per failure would restore the lsof fork storm under load.
        os.environ["SESSION_PEERS_HOLDER_PROBE"] = "auto"
        self.inject(proc_pidinfo=_fail_with(errno.EPERM))
        self.assertUnverified(self.child.pid)

    def test_a_failed_pid_listing_is_unverified(self):
        self.inject(proc_listpids=_fail_with(errno.EIO))
        self.assertUnverified()

    def test_revoked_and_closed_descriptors_are_skipped(self):
        out = open(self.work / "out.log", "wb")
        err = open(self.work / "err.log", "wb")
        self.addCleanup(out.close)
        self.addCleanup(err.close)
        handle = open(self.rollout, "rb")
        child = subprocess.Popen([str(self.holders.codex_bin), "60"], stdin=handle, stdout=out, stderr=err)
        handle.close()
        self.addCleanup(_Holders._stop, child)
        real = self.real
        seen = set()

        def flaky(pid, fd, flavor, buf, size):
            seen.add(fd)
            if pid == child.pid and fd in (1, 2):
                ctypes.set_errno(errno.ENOENT if fd == 1 else errno.EBADF)
                return 0
            return real.proc_pidfdinfo(pid, fd, flavor, buf, size)

        self.inject(proc_pidfdinfo=flaky)
        holders, verified, error = sp_process.lsof_holders_checked(
            [str(self.rollout)], pid=child.pid
        )
        self.assertTrue(verified, error)
        self.assertEqual(
            holders[sp_process.canon_path(str(self.rollout))], [(child.pid, "codex-probe")]
        )
        self.assertTrue({0, 1, 2} <= seen)


class BackendSelectionTests(Base):
    def setUp(self):
        super().setUp()
        self.argv_log = self.root / "lsof-argv.log"
        os.environ["FAKE_LSOF_ARGV_LOG"] = str(self.argv_log)
        self.path = self.root / "rollout.jsonl"
        self.path.write_text("{}\n")
        saved = (sp_procfd._libproc, sp_procfd._libproc_error)
        self.addCleanup(
            lambda: (setattr(sp_procfd, "_libproc", saved[0]),
                     setattr(sp_procfd, "_libproc_error", saved[1]))
        )

    def make_unavailable(self):
        sp_procfd._libproc = None
        sp_procfd._libproc_error = "injected: no libproc"

    def test_auto_falls_back_to_lsof_when_native_is_unavailable(self):
        os.environ["SESSION_PEERS_HOLDER_PROBE"] = "auto"
        self.make_unavailable()
        _holders, verified, _error = sp_process.lsof_holders_checked([str(self.path)])
        self.assertTrue(verified)
        self.assertTrue(self.argv_log.exists(), "auto did not use lsof")

    def test_forced_native_never_forks_lsof(self):
        os.environ["SESSION_PEERS_HOLDER_PROBE"] = "native"
        self.make_unavailable()
        _holders, verified, error = sp_process.lsof_holders_checked([str(self.path)])
        self.assertFalse(verified)
        self.assertTrue(error)
        self.assertFalse(self.argv_log.exists())

    def test_pinned_lsof_uses_lsof(self):
        os.environ["SESSION_PEERS_HOLDER_PROBE"] = "lsof"
        sp_process.lsof_holders_checked([str(self.path)])
        self.assertTrue(self.argv_log.exists())

    def test_unknown_value_means_auto(self):
        os.environ["SESSION_PEERS_HOLDER_PROBE"] = "bogus"
        self.assertEqual(sp_process.holder_probe(), "auto")


if __name__ == "__main__":
    unittest.main()
