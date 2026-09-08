"""Publish staged skill files without deleting paths used by active readers."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import tempfile


def atomic_write(path: Path, content: bytes, mode: int = 0o644) -> bool:
    """Readers see one complete file; unchanged content and modes are untouched."""
    if path.is_symlink():
        raise ValueError(f"refusing to overwrite a file symlink: {path}")
    if path.exists():
        if not path.is_file():
            raise ValueError(f"file/directory conflict: {path}")
        if path.read_bytes() == content and stat.S_IMODE(path.stat().st_mode) == mode:
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".skills-refresh-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fchmod(output.fileno(), mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def inventory(directory: Path) -> dict[Path, tuple[bytes, int]]:
    """Vercel materializes supporting symlinks; reject unexpected stage shapes."""
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"expected a real skill directory: {directory}")
    files = {}
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"unexpected staged symlink: {path}")
        if path.is_file():
            files[path.relative_to(directory)] = (
                path.read_bytes(), stat.S_IMODE(path.stat().st_mode)
            )
        elif not path.is_dir():
            raise ValueError(f"unexpected staged file type: {path}")
    body = files.get(Path("SKILL.md"), (b"", 0))[0]
    if not body.startswith(b"---\n") and not body.startswith(b"---\r\n"):
        raise ValueError(f"missing or incomplete staged SKILL.md: {directory}")
    if len(body.decode("utf-8").split("---", 2)) != 3:
        raise ValueError(f"incomplete staged frontmatter: {directory}")
    return files


def preflight(directory: Path, files: dict[Path, tuple[bytes, int]]) -> None:
    """Reject shape conflicts before changing any file in the staged batch."""
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ValueError(f"expected a real installed skill directory: {directory}")
    for relative in files:
        target = directory / relative
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError(f"unsafe installed file or shape conflict: {target}")
        parent = target.parent
        while parent != directory:
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                raise ValueError(f"unsafe installed directory or shape conflict: {parent}")
            parent = parent.parent


def publish(directory: Path, files: dict[Path, tuple[bytes, int]]) -> int:
    """Keep directories and retired support files; publish the entrypoint last.

    Atomic per file, not a transaction across a whole skill. Retired support
    files remain readable by already-loaded bodies. Fresh skills become
    discoverable only after their whole directory is ready.
    """
    preflight(directory, files)
    if not directory.exists():
        directory.parent.mkdir(parents=True, exist_ok=True)
        # Outside the discovery root: recursive scanners cannot load a staged
        # entrypoint. The directory is on the same filesystem as its destination.
        temporary = Path(tempfile.mkdtemp(prefix=".skill-new-", dir=directory.parent.parent))
        try:
            for relative, (body, mode) in files.items():
                atomic_write(temporary / relative, body, mode)
            os.replace(temporary, directory)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return len(files)
    changed = 0
    for relative in sorted(files, key=lambda item: (item == Path("SKILL.md"), str(item))):
        body, mode = files[relative]
        changed += atomic_write(directory / relative, body, mode)
    return changed


def project_claude(canonical: Path, projection: Path) -> None:
    """Preserve directory-wide aliases and existing copies; add/retarget links."""
    if canonical.resolve() == projection.resolve():
        return
    if projection.exists() and not projection.is_symlink():
        return  # Existing real Claude copies are published separately.
    projection.parent.mkdir(parents=True, exist_ok=True)
    target = os.path.relpath(canonical.resolve(), projection.parent.resolve())
    fd, name = tempfile.mkstemp(prefix=".skills-link-", dir=projection.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        temporary.unlink()
        temporary.symlink_to(target, target_is_directory=True)
        os.replace(temporary, projection)
    finally:
        temporary.unlink(missing_ok=True)
