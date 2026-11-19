"""Private, local-only Git handover notes shared by a repository's worktrees."""

from __future__ import annotations

import contextlib
import argparse
import datetime as dt
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

from . import identity

META = ".ledger/metadata.json"
FORMAT = 1
LOCK_TIMEOUT = 5.0
GIT_TIMEOUT = 5.0
_now = time.time


class LedgerError(Exception):
    """Refuse without discarding a note or changing another owner's claim."""


def safe_key(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", value)
            or ".." in value or value in (".", "") or len(value.encode()) > 240):
        raise LedgerError("key/slug must be 1..240 ASCII letters, digits, '.', '_' or '-'; no '..'")
    return value


def git_env():
    # No inherited repository selector, config injection, template or transport.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_CONFIG_NOSYSTEM="1", GIT_ATTR_NOSYSTEM="1",
               GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat")
    return env


def git(directory, *args, timeout=GIT_TIMEOUT, allowed=(0,), author=None, input_text=None, raw=False):
    env = git_env()
    if author:
        name = human(author)
        env.update(GIT_AUTHOR_NAME="session-peers " + name,
                   GIT_COMMITTER_NAME="session-peers " + name,
                   GIT_AUTHOR_EMAIL="session-peers@localhost",
                   GIT_COMMITTER_EMAIL="session-peers@localhost")
    command = ["git", "--no-pager", "-c", "core.hooksPath=" + os.devnull,
               "-c", "commit.gpgsign=false", "-c", "core.fsmonitor=false",
               "-c", "core.untrackedCache=false", "-c", "protocol.allow=never",
               "-c", "gc.auto=0", "-c", "maintenance.auto=false",
               "-c", "core.excludesFile=" + os.devnull,
               "-c", "core.attributesFile=" + os.devnull, *args]
    result = subprocess.run(command, cwd=str(directory), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, check=False, umask=0o077,
                            input=input_text.encode() if input_text is not None else None)
    if result.returncode not in allowed:
        raise LedgerError("local git %s failed (exit %d); files preserved" % (args[0], result.returncode))
    return result.stdout if raw else result.stdout.decode("utf-8")


def human(value):
    return "".join(c for c in str(value) if c.isprintable())


def anchor_for(cwd):
    common = Path(git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()).resolve()
    return common.parent if common.name in (".git", ".bare") else common


def default_key(anchor):
    return safe_key(re.sub(r"[^A-Za-z0-9._-]", "-", str(anchor)))


def data_root():
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    if not Path(base).is_absolute():
        raise LedgerError("XDG_DATA_HOME must be absolute")
    return Path(base) / "session-peers/ledger"


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise LedgerError("ledger directory must be a real directory")
    path.chmod(0o700)


def private_tree(path):
    for directory, dirs, files in os.walk(path):
        Path(directory).chmod(0o700)
        for name in dirs + files:
            child = Path(directory) / name
            if child.is_symlink():
                raise LedgerError("symlinks are not allowed in a ledger")
            child.chmod(0o700 if child.is_dir() else 0o600)


@contextlib.contextmanager
def locked(root, keys):
    private_dir(root.parent)
    private_dir(root)
    private_dir(root / ".locks")
    handles = []
    deadline = time.monotonic() + LOCK_TIMEOUT
    try:
        for key in sorted(set(keys)):
            name = hashlib.sha256(key.encode()).hexdigest() + ".lock"
            fd = os.open(str(root / ".locks" / name), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            handles.append(fd)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LedgerError("ledger busy; retry after its writer finishes")
                    time.sleep(0.025)
        yield
    finally:
        for fd in reversed(handles):
            os.close(fd)


def atomic_write(path, text):
    private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".ledger-write-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def stamp():
    return dt.datetime.fromtimestamp(_now(), dt.timezone.utc).isoformat().replace("+00:00", "Z")


def timestamp(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def encode(item):
    fields = {k: v for k, v in item.items() if k not in ("body", "slug")}
    return "---\n" + "".join(k + ": " + json.dumps(v, ensure_ascii=False) + "\n"
                              for k, v in fields.items()) + "---\n" + item["body"]


def decode(text, slug):
    try:
        header, body = text.removeprefix("---\n").split("\n---\n", 1)
        if not text.startswith("---\n"):
            raise ValueError("missing frontmatter")
        item = dict((k, json.loads(v)) for k, v in
                    (line.split(": ", 1) for line in header.splitlines()))
        if (item["format"] != FORMAT or item["status"] not in ("open", "blocked", "done")
                or not isinstance(item["title"], str) or not isinstance(item["links"], list)
                or any(not isinstance(link, str) for link in item["links"])):
            raise ValueError("invalid item fields")
        if item["owner"] is not None:
            identity.parse_typed(item["owner"]["id"])
            if not isinstance(item["owner"]["name"], str):
                raise ValueError("invalid owner")
        timestamp(item["updated"])
        if item["lease_until"] is not None:
            timestamp(item["lease_until"])
        item.update(body=body, slug=slug)
        return item
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise LedgerError("invalid ledger item %s; preserve and reconcile it" % slug) from exc


def caller(args):
    ident = identity.caller_identity(args)
    typed = ident["kind"] + ":" + ident["uuid"]
    return {"id": typed, "name": human(getattr(args, "name", None) or ident.get("name") or typed)}


def can_take(old, new):
    # The channel implementation already distinguishes failed discovery from death
    # and verifies journal handovers. Reuse those rules; expiry alone grants nothing.
    from . import channels
    return (channels._grant_recorded(old, new)
            or channels.member_liveness(old) == "gone")


class Ledger:
    def __init__(self, cwd=None, key=None):
        cwd = Path(cwd or os.getcwd()).resolve()
        self.anchor = anchor_for(cwd)
        self.key = safe_key(key) if key is not None else default_key(self.anchor)
        self.root = data_root()
        self.path = self.root / self.key
        checkouts = [self.anchor]
        # A linked worktree can live outside the anchor. Discover its filesystem
        # boundary without another Git invocation or an origin lookup.
        for directory in (cwd, *cwd.parents):
            if (directory / ".git").exists():
                checkouts.append(directory)
                break
        if any(self.path.resolve() == root or root in self.path.resolve().parents for root in checkouts):
            raise LedgerError("ledger storage must be outside the project checkout")

    def exists(self):
        return self.path.exists() or self.path.is_symlink()

    def snapshot(self, check_anchor=True):
        if not self.exists():
            raise LedgerError("no ledger; run peers.py ledger init")
        if self.path.is_symlink() or not (self.path / ".git").is_dir() or (self.path / ".git").is_symlink():
            raise LedgerError("ledger must be its own local Git repository")
        # `remote` includes empty remote sections; config catches remote.* custom keys.
        if (git(self.path, "remote").strip()
                or git(self.path, "config", "--get-regexp", r"^remote\.", allowed=(0, 1)).strip()):
            raise LedgerError("ledger has a configured remote; remove it locally before continuing")
        head = git(self.path, "rev-parse", "--verify", "HEAD").strip()
        try:
            meta = json.loads(git(self.path, "show", head + ":" + META))
            if meta["format"] != FORMAT or not isinstance(meta["anchor"], str):
                raise ValueError("invalid metadata")
        except (ValueError, KeyError, TypeError) as exc:
            raise LedgerError("invalid ledger metadata") from exc
        if check_anchor and meta["anchor"] != str(self.anchor):
            raise LedgerError("ledger anchor mismatch: stored %s; current %s. Choose a distinct --repo-key; "
                              "for a moved repository use ledger relocate --from=OLD_KEY"
                              % (meta["anchor"], self.anchor))
        return head, meta

    def clean(self):
        if git(self.path, "status", "--porcelain", "--untracked-files=all", "--ignored").strip():
            raise LedgerError("ledger has uncommitted files (possibly an interrupted mutation). "
                              "Files preserved: back up and inspect git status/diff locally, then "
                              "reconcile and commit the intended changes before retrying; never reset them blindly")

    def read(self, head, slug):
        safe_key(slug)
        return decode(git(self.path, "show", head + ":items/" + slug + ".md"), slug)

    def items(self, head):
        names = git(self.path, "ls-tree", "--name-only", head, "items/").splitlines()
        for name in names:
            if not name.startswith("items/") or not name.endswith(".md"):
                raise LedgerError("unexpected ledger item path")
            safe_key(name[6:-3])
        if not names:
            return []
        # One process for all bodies keeps the bounded reminder useful with a
        # real handover backlog, rather than spawning Git once per note.
        objects = io.BytesIO(git(self.path, "cat-file", "--batch", raw=True,
                                 input_text="".join(head + ":" + name + "\n" for name in names)))
        result = []
        for name in names:
            header = objects.readline().split()
            if len(header) != 3 or header[1] != b"blob":
                raise LedgerError("invalid committed item object")
            body = objects.read(int(header[2]))
            if objects.read(1) != b"\n":
                raise LedgerError("incomplete committed item object")
            result.append(decode(body.decode("utf-8"), name[6:-3]))
        return result

    def commit(self, files, message, owner):
        for name, text in files.items():
            target = self.path / name
            if target.is_symlink():
                raise LedgerError("symlinks are not allowed in a ledger")
            atomic_write(target, text)
        git(self.path, "add", "--", *files)
        if git(self.path, "diff", "--cached", "--name-only").strip():
            git(self.path, "commit", "--quiet", "-m", message, author=owner["name"])
        private_tree(self.path)

    def init(self, owner):
        with locked(self.root, [self.key]):
            template = (Path(__file__).parents[2] / "assets/ledger-readme.md").read_text(encoding="utf-8")
            digest = hashlib.sha256(template.encode()).hexdigest()
            if self.exists():
                head, meta = self.snapshot()
                self.clean()
                current = git(self.path, "show", head + ":README.md")
                if hashlib.sha256(current.encode()).hexdigest() != meta["readme_sha256"]:
                    return {"key": self.key, "directory": str(self.path), "readme": "locally modified; preserved"}
                meta["readme_sha256"] = digest
            else:
                private_dir(self.path)
                git(self.path, "init", "--quiet", "--template=", "--initial-branch=main", ".")
                meta = {"format": FORMAT, "anchor": str(self.anchor), "readme_sha256": digest}
            self.commit({META: json.dumps(meta, indent=2) + "\n", "README.md": template},
                        "ledger: initialize or refresh generic rules", owner)
        return {"key": self.key, "directory": str(self.path)}

    def mutate(self, action, args, owner):
        with locked(self.root, [self.key]):
            head, _meta = self.snapshot()
            self.clean()
            if action == "import-dir":
                return self.import_items(head, args.directory, owner)
            slug = safe_key(args.slug)
            existing = {item["slug"]: item for item in self.items(head)}
            if action == "add":
                if slug in existing:
                    raise LedgerError("item already exists: " + slug)
                item = {"format": FORMAT, "title": args.title, "status": "open", "owner": owner,
                        "lease_until": lease_end("4h"), "updated": stamp(),
                        "links": args.link or [], "body": source_text(args.body_file)}
            else:
                if slug not in existing:
                    raise LedgerError("no item: " + slug)
                item = existing[slug]
                held = item["owner"]
                if action == "claim":
                    if held and held["id"] != owner["id"] and not can_take(held["id"], owner["id"]):
                        raise LedgerError("item owned by %s; expiry is not a handover. Ask for release "
                                          "or a verified handover; unverified is not dead" % held["id"])
                    item.update(owner=owner, lease_until=lease_end(args.lease))
                elif not held or held["id"] != owner["id"]:
                    raise LedgerError("claim this item before changing it; current owner: %s"
                                      % (held["id"] if held else "none"))
                elif action == "release":
                    item.update(owner=None, lease_until=None)
                elif action == "done":
                    item.update(status="done", owner=None, lease_until=None)
                elif action == "update":
                    if args.status:
                        item["status"] = args.status
                    if args.body_file:
                        item["body"] = source_text(args.body_file)
                    if args.append_file:
                        item["body"] += source_text(args.append_file)
                    if args.link is not None:
                        item["links"] = args.link
                    if item["status"] == "done":
                        item.update(owner=None, lease_until=None)
                item["updated"] = stamp()
            self.commit({"items/" + slug + ".md": encode(item)}, "ledger: %s %s" % (action, slug), owner)
            return dict(item, slug=slug)

    def import_items(self, head, directory, owner):
        source = Path(directory)
        if not source.is_dir():
            raise LedgerError("import source must be a directory")
        existing = {item["slug"] for item in self.items(head)}
        files = {}
        for path in sorted(source.glob("*.md")):
            if path.name.upper() in ("README.MD", "REPO.MD"):
                continue
            if path.is_symlink() or not path.is_file():
                raise LedgerError("import requires regular files: " + str(path))
            slug = safe_key(path.stem)
            if slug in existing:
                raise LedgerError("import collision: " + slug)
            original = path.read_bytes().decode("utf-8")
            item = {"format": FORMAT, "title": slug, "status": "open", "owner": None,
                    "lease_until": None, "updated": stamp(), "links": [],
                    "provenance": {"file": str(path.resolve()),
                                   "sha256": hashlib.sha256(original.encode()).hexdigest()},
                    "body": original}
            files["items/" + slug + ".md"] = encode(item)
        if files:
            self.commit(files, "ledger: import %d notes" % len(files), owner)
        return {"imported": len(files)}

    def relocate(self, old_key, owner):
        safe_key(old_key)
        old = self.root / old_key
        if old == self.path:
            raise LedgerError("relocation needs a different destination key")
        with locked(self.root, [old_key, self.key]):
            if self.exists():
                raise LedgerError("relocation destination exists; nothing overwritten")
            source = Ledger(key=old_key)
            _head, meta = source.snapshot(check_anchor=False)
            source.clean()
            # Move first: if the following commit fails, the destination keeps
            # both the old committed anchor and the new uncommitted metadata.
            os.rename(old, self.path)
            meta["anchor"] = str(self.anchor)
            self.commit({META: json.dumps(meta, indent=2) + "\n"}, "ledger: explicitly relocate anchor", owner)
        return {"key": self.key, "directory": str(self.path), "from": old_key}


def lease_end(value):
    match = re.fullmatch(r"([1-9][0-9]*)(m|h)", value)
    if not match:
        raise LedgerError("lease must be e.g. 30m or 4h (maximum 4h)")
    seconds = int(match[1]) * (60 if match[2] == "m" else 3600)
    if seconds > 4 * 3600:
        raise LedgerError("lease maximum is 4h")
    return dt.datetime.fromtimestamp(_now() + seconds, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def source_text(path):
    return Path(path).read_bytes().decode("utf-8") if path else ""


def summary(cwd=None):
    ledger = Ledger(cwd)
    if not ledger.exists():
        return None
    head, _meta = ledger.snapshot()
    items = [item for item in ledger.items(head) if item["status"] != "done"]
    if not items:
        return None
    return "ledger: %d open (%d unowned, %d stale): peers.py ledger list" % (
        len(items), sum(item["owner"] is None for item in items),
        sum(item["owner"] is not None and item["lease_until"] is not None
            and timestamp(item["lease_until"]) <= _now() for item in items))


def print_summary():
    """Best-effort child process: bound the whole reminder, not each Git call."""
    process = None
    try:
        launcher = Path(__file__).parents[1] / "peers.py"
        process = subprocess.Popen([sys.executable, str(launcher), "ledger", "summary"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        stdout, _stderr = process.communicate(timeout=1.0)
        if process.returncode == 0 and stdout:
            print(stdout.decode("utf-8").strip())
    except subprocess.TimeoutExpired:
        # The child may itself be waiting on Git. Kill only this owned process
        # group so a timeout leaves no orphan discovery or Git subprocess.
        try:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=0.2)
        except (OSError, subprocess.SubprocessError):
            pass
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    finally:
        if process is not None:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def cmd_ledger(args):
    try:
        action = args.ledger_cmd
        if action == "summary":
            line = summary()
            if line:
                print(line)
            return 0
        ledger = Ledger(key=getattr(args, "repo_key", None))
        if action == "path":
            if ledger.exists():
                ledger.snapshot()
            result = {"key": ledger.key, "directory": str(ledger.path)}
        elif action == "init":
            result = ledger.init(caller(args))
        elif action == "relocate":
            result = ledger.relocate(args.old_key, caller(args))
        elif action in ("add", "update", "claim", "release", "done", "import-dir"):
            result = ledger.mutate(action, args, caller(args))
        elif action == "list" and not ledger.exists():
            result = []
        else:
            head, _meta = ledger.snapshot()
            if action == "list":
                result = ledger.items(head)
                if not args.all:
                    result = [item for item in result if item["status"] != "done"]
                if args.mine:
                    me = caller(args)["id"]
                    result = [item for item in result if item["owner"] and item["owner"]["id"] == me]
            elif action == "show":
                result = ledger.read(head, args.slug)
            elif action == "log":
                paths = ["--", "items/" + safe_key(args.slug) + ".md"] if args.slug else []
                rows = git(ledger.path, "log", "--format=%H%x00%aI%x00%s", head, *paths).splitlines()
                result = [dict(zip(("commit", "at", "subject"), row.split("\0", 2))) for row in rows]
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif action == "show":
            print("\n".join(human(line) for line in encode(result).splitlines()))
        elif action == "list":
            for item in result:
                print(human("%s  %s  %s  %s" % (item["slug"], item["status"],
                      item["owner"]["id"] if item["owner"] else "unowned", item["title"])))
        elif action == "log":
            for row in result:
                print(human("{commit} {at} {subject}".format(**row)))
        else:
            print(human(json.dumps(result, ensure_ascii=False)))
        return 0
    except (LedgerError, OSError, ValueError, subprocess.SubprocessError) as exc:
        if getattr(args, "ledger_cmd", None) != "summary":
            sys.stderr.write("ledger: " + human(exc) + "\n")
        return 1


def register(sub):
    parser = sub.add_parser("ledger", help="private, local-only per-repository handover notes")
    parser.add_argument("--repo-key", help="explicit identity key, not a directory")
    commands = parser.add_subparsers(dest="ledger_cmd", required=True)
    for name in ("path", "init", "list", "show", "add", "update", "claim", "release", "done",
                 "log", "import-dir", "relocate", "summary"):
        command = commands.add_parser(name)
        command.set_defaults(func=cmd_ledger)
        command.add_argument("--repo-key", default=argparse.SUPPRESS)
        command.add_argument("--json", action="store_true")
        command.add_argument("--as", dest="as_identity", help="full typed session identity")
        command.add_argument("--name", help="owner display name (identity stays typed)")
        if name in ("show", "add", "update", "claim", "release", "done", "log"):
            command.add_argument("slug", nargs="?" if name == "log" else None)
        if name == "list":
            group = command.add_mutually_exclusive_group()
            for flag in ("mine", "open", "all"):
                group.add_argument("--" + flag, action="store_true")
        if name in ("add", "update"):
            if name == "add":
                command.add_argument("--title", required=True)
            command.add_argument("--link", action="append")
            group = command.add_mutually_exclusive_group()
            group.add_argument("--body-file")
            if name == "update":
                group.add_argument("--append-file")
                command.add_argument("--status", choices=("open", "blocked", "done"))
        if name == "claim":
            command.add_argument("--lease", default="4h")
        if name == "import-dir":
            command.add_argument("directory")
        if name == "relocate":
            command.add_argument("--from", dest="old_key", required=True)
