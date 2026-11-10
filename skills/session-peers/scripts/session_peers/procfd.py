"""In-process open-file holder probe: the `lsof -F pcn -- PATHS` answer, no fork.

Every shim asks "does a Codex process still have this rollout or writer lock
open?" every few seconds. Forking `lsof` costs a process spawn per check and,
unscoped, a walk of every process's descriptor table. On macOS this module
answers the same question from libproc (`proc_listallpids`, `proc_name`,
`proc_pidinfo(PROC_PIDLISTFDS)`, `proc_pidfdinfo(PROC_PIDFDVNODEPATHINFO)`).
Other platforms keep `lsof` (`holders()` returns None).

Same question as the lsof probe, nothing tighter: an open descriptor counts,
whether or not it holds a lock. Matching is by (device, inode), as `lsof`
matches file arguments, so a symlinked CODEX_HOME cannot make the two disagree
and an fd on a replaced file does not match the new file. Only processes whose
command name contains "codex" count.

Fail-closed: anything that leaves the enumeration incomplete or an identity
unknown raises ProbeError (the caller reports it as unverified), never a
verified "not held". The unscoped walk covers only processes of this effective
UID (`proc_listpids(PROC_UID_ONLY)`): another user's descriptors are unreadable
without root, to lsof as well, so any refusal inside the walk is unverified.
Only a revoked vnode (ENOENT, which lsof prints as "(revoked)" with no
identity) or a descriptor closed since the listing (EBADF) is skipped.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import struct
import sys

# <sys/proc_info.h>
_PROC_PIDLISTFDS = 1
_PROC_PIDFDVNODEPATHINFO = 2
_PROX_FDTYPE_VNODE = 1
_PROC_UID_ONLY = 4  # <libproc.h> proc_listpids type
_PROC_FDINFO = struct.Struct("=iI")  # struct proc_fdinfo { int32 proc_fd; uint32 proc_fdtype; }
_PROC_FILEINFO_SIZE = 24  # struct proc_fileinfo
_VNODE_INFO_SIZE = 152  # struct vnode_info, vinfo_stat first
_MAXPATHLEN = 1024
# struct vnode_fdinfowithpath { proc_fileinfo; vnode_info_path { vnode_info; char[MAXPATHLEN] } }
_VNODE_FDINFOWITHPATH_SIZE = _PROC_FILEINFO_SIZE + _VNODE_INFO_SIZE + _MAXPATHLEN
# struct vinfo_stat begins: uint32 vst_dev; uint16 vst_mode; uint16 vst_nlink; uint64 vst_ino;
_VINFO_STAT_HEAD = struct.Struct("=IHHQ")
_FD_SLACK = 64  # descriptors that may open between the size query and the read

_libproc = None
_libproc_error = None


class ProbeError(Exception):
    """The native probe could not complete; the answer is unverified."""


def _load_libproc():
    global _libproc, _libproc_error
    if _libproc is not None or _libproc_error is not None:
        return _libproc
    try:
        lib = ctypes.CDLL(
            ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib", use_errno=True
        )
        lib.proc_listpids.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int
        ]
        lib.proc_listpids.restype = ctypes.c_int
        lib.proc_name.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        lib.proc_name.restype = ctypes.c_int
        lib.proc_pidinfo.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int
        ]
        lib.proc_pidinfo.restype = ctypes.c_int
        lib.proc_pidfdinfo.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int
        ]
        lib.proc_pidfdinfo.restype = ctypes.c_int
    except (OSError, AttributeError) as exc:
        _libproc_error = str(exc)
        return None
    _libproc = lib
    return lib


def available():
    """True when this host has the native probe (macOS with libproc)."""
    return sys.platform == "darwin" and _load_libproc() is not None


def unavailable_reason():
    if sys.platform != "darwin":
        return "the native holder probe is macOS-only"
    _load_libproc()
    return _libproc_error


def _dev(value):
    return value & 0xFFFFFFFF


def _errno_text(err):
    return os.strerror(err) if err else "no errno"


def _pids(lib):
    """Every pid of this effective UID; a full buffer is retried, then refused."""
    uid = os.geteuid()
    size = ctypes.sizeof(ctypes.c_int)
    for _attempt in range(3):
        ctypes.set_errno(0)
        need = lib.proc_listpids(_PROC_UID_ONLY, uid, None, 0)
        if need <= 0:
            raise ProbeError("proc_listpids failed: %s" % _errno_text(ctypes.get_errno()))
        capacity = need // size + 256
        buf = (ctypes.c_int * capacity)()
        ctypes.set_errno(0)
        got = lib.proc_listpids(_PROC_UID_ONLY, uid, buf, ctypes.sizeof(buf))
        if got <= 0:
            raise ProbeError("proc_listpids failed: %s" % _errno_text(ctypes.get_errno()))
        if got % size:
            raise ProbeError("pid list is ragged (%d bytes)" % got)
        if got < ctypes.sizeof(buf):
            return [buf[i] for i in range(got // size) if buf[i] > 0]
    raise ProbeError("proc_listpids kept filling its buffer")


_GONE = object()


def _name(lib, pid):
    """The command name, or _GONE for a reaped pid; a refusal raises."""
    buf = ctypes.create_string_buffer(256)
    ctypes.set_errno(0)
    if lib.proc_name(pid, buf, ctypes.sizeof(buf)) > 0:
        return buf.value.decode("utf-8", "replace")
    err = ctypes.get_errno()
    if err in (0, errno.ESRCH):
        return _GONE
    raise ProbeError("cannot read the name of pid %d: %s" % (pid, _errno_text(err)))


def _vnodes(lib, pid):
    """{(dev, ino)} of every vnode descriptor, or _GONE for a reaped pid."""
    ctypes.set_errno(0)
    size = lib.proc_pidinfo(pid, _PROC_PIDLISTFDS, 0, None, 0)
    if size <= 0:
        err = ctypes.get_errno()
        if err == errno.ESRCH:
            return _GONE
        raise ProbeError(
            "cannot list the descriptors of pid %d: %s" % (pid, _errno_text(err))
        )
    capacity = size + _FD_SLACK * _PROC_FDINFO.size
    buf = ctypes.create_string_buffer(capacity)
    ctypes.set_errno(0)
    got = lib.proc_pidinfo(pid, _PROC_PIDLISTFDS, 0, buf, capacity)
    if got <= 0:
        err = ctypes.get_errno()
        if err == errno.ESRCH:
            return _GONE
        raise ProbeError(
            "cannot list the descriptors of pid %d: %s" % (pid, _errno_text(err))
        )
    if got >= capacity or got % _PROC_FDINFO.size:
        # A full buffer may have dropped descriptors; a ragged one is an ABI mismatch.
        raise ProbeError("descriptor list of pid %d is incomplete (%d bytes)" % (pid, got))
    raw = buf.raw[:got]
    info = ctypes.create_string_buffer(_VNODE_FDINFOWITHPATH_SIZE)
    out = set()
    for offset in range(0, got, _PROC_FDINFO.size):
        fd, fdtype = _PROC_FDINFO.unpack_from(raw, offset)
        if fdtype != _PROX_FDTYPE_VNODE:
            continue
        ctypes.set_errno(0)
        n = lib.proc_pidfdinfo(
            pid, fd, _PROC_PIDFDVNODEPATHINFO, info, _VNODE_FDINFOWITHPATH_SIZE
        )
        if n == _VNODE_FDINFOWITHPATH_SIZE:
            dev, _mode, _nlink, ino = _VINFO_STAT_HEAD.unpack_from(
                info.raw, _PROC_FILEINFO_SIZE
            )
            out.add((_dev(dev), ino))
            continue
        err = ctypes.get_errno()
        if n == 0 and err == errno.EBADF:
            continue  # closed since the listing
        if n == 0 and err == errno.ENOENT:
            continue  # revoked vnode: no identity, lsof shows "(revoked)"
        if n == 0 and err == errno.ESRCH:
            return _GONE
        raise ProbeError(
            "cannot read descriptor %d of pid %d: %s (%d bytes)"
            % (fd, pid, _errno_text(err), n)
        )
    return out


def holders(paths, pid=None):
    """{path: [(pid, command)]} for codex-named processes with `paths` open.

    `paths` must already exist (the caller drops absent ones); each result is
    keyed by the path string it was asked about. With `pid`, only that process
    is inspected: a reaped or non-codex pid holds nothing, a refused one raises.
    Without it, every process of this effective UID is inspected.
    Raises ProbeError whenever the answer would be incomplete. Returns None when
    the platform has no native probe.
    """
    if not available():
        return None
    lib = _load_libproc()
    want = {}
    for path in paths:
        try:
            st = os.stat(path)
        except OSError as exc:
            raise ProbeError("cannot inspect %s: %s" % (path, exc)) from exc
        want.setdefault((_dev(st.st_dev), st.st_ino), []).append(path)
    out = {}
    if not want:
        return out
    scoped = pid is not None
    for candidate in [int(pid)] if scoped else _pids(lib):
        name = _name(lib, candidate)
        if name is _GONE:
            continue
        if "codex" not in os.path.basename(name).lower():
            continue
        vnodes = _vnodes(lib, candidate)
        if vnodes is _GONE:
            continue
        for key in vnodes & want.keys():
            for path in want[key]:
                out.setdefault(path, []).append((candidate, name))
    return out
