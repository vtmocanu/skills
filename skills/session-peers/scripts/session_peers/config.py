"""Config for the session-peers CLI."""

from __future__ import annotations
from . import constants as sp_constants, runtime as sp_runtime

# --------------------------------------------------------------------------
# A very small TOML reader (D3: no tomllib on 3.9)
# --------------------------------------------------------------------------


def _toml_scalar(raw):
    raw = raw.strip()
    if not raw:
        return ""
    if raw[0] in "\"'":
        quote = raw[0]
        end = raw.find(quote, 1)
        if end == -1:
            return raw[1:]
        return raw[1:end]
    # Strip an inline comment from an unquoted value.
    raw = raw.split("#", 1)[0].strip()
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _toml_key_text(part):
    """One dotted-path segment as TOML would write it in a table header."""
    return part if sp_constants.BARE_KEY_RE.match(part) else '"%s"' % part


def _flatten_toml(data):
    """A parsed TOML document in the shape read_toml_lite returns.

    {"": root scalars, "features": {...}, 'hooks.state."<k>"': {...}}, so the
    two readers are interchangeable for every caller.
    """
    out = {}

    def walk(prefix, table):
        scalars = {}
        for key, value in table.items():
            if isinstance(value, dict):
                walk(prefix + [key], value)
            else:
                scalars[key] = value
        out[".".join(_toml_key_text(p) for p in prefix)] = scalars

    walk([], data)
    out.setdefault("", {})
    return out


def read_toml_lite(path):
    """Return {section_header: {key: value}} with "" for the root table.

    R3: a real parser reads the file where one exists, because a line reader
    cannot tell a key from the same text inside a multiline string. The line
    reader stays as the fallback for 3.9 and 3.10. With tomllib available, an
    invalid file yields environment/default paths, matching Codex refusal.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except FileNotFoundError:
        return {"": {}}
    except OSError as exc:
        sp_runtime.log("ignoring unreadable %s: %s" % (path, exc))
        return {"": {}}
    tomllib = _load_tomllib()
    if tomllib is not None:
        try:
            return _flatten_toml(tomllib.loads(text))
        except Exception as exc:
            # A partial line-by-line read of an invalid file is worse than no
            # read: it could route the database off a key Codex never honours,
            # because Codex refuses the same file outright.
            sp_runtime.log(
                "%s does not parse as TOML (%s); Codex would refuse it too, so "
                "the bridge is using environment and default paths" % (path, exc)
            )
            return {"": {}}
    # No tomllib (3.9, 3.10): the line reader is the only reader there is.
    return read_toml_lite_text(text)


def read_toml_lite_text(text):
    """The line-reader fallback.

    R3: lines inside a multiline string are skipped, and the active table name
    is normalised so `["features"]` stores its keys under `features`.
    """
    out = {"": {}}
    lines = text.splitlines()
    inside = _line_states(lines)
    section = ""
    for i, line in enumerate(lines):
        if inside[i]:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        header = sp_constants.TOML_HEADER_RE.match(line)
        if header:
            name = header.group(1).strip()
            if name.startswith("[") and name.endswith("]"):
                name = name[1:-1].strip()  # array of tables
            section = _unquote_table_name(name)
            out.setdefault(section, {})
            continue
        if "=" not in stripped:
            continue
        key, _, raw = stripped.partition("=")
        out.setdefault(section, {})[key.strip().strip("\"'")] = _toml_scalar(raw)
    return out


def _load_tomllib():
    """The stdlib TOML parser, or None on 3.9 and 3.10.

    A seam, so a test can prove the no-parser path without a second runtime.
    """
    try:
        import tomllib
    except ImportError:
        return None
    return tomllib


def _unquote_table_name(name):
    """`["features"]` names the same table as `[features]` (R1c)."""
    name = name.strip()
    if len(name) >= 2 and name[0] == name[-1] and name[0] in "\"'":
        inner = name[1:-1]
        if inner and '"' not in inner and "'" not in inner:
            return inner.strip()
    return name


def _scan_multiline(line, delim):
    """The open multiline-string delimiter after this line, or None.

    Good enough to tell whether a `[features]` line is real config or an
    example inside a triple-quoted block (R1); the parser diff backstops it.
    """
    i = 0
    while i < len(line):
        if delim is not None:
            j = line.find(delim, i)
            if j == -1:
                return delim
            i = j + 3
            delim = None
            continue
        if line.startswith(sp_constants.TRIPLE_DQ, i) or line.startswith(sp_constants.TRIPLE_SQ, i):
            delim = line[i:i + 3]
            i += 3
            continue
        ch = line[i]
        if ch == "#":
            return None
        if ch in "\"'":
            j = line.find(ch, i + 1)
            if j == -1:
                return None
            i = j + 1
            continue
        i += 1
    return delim


def _line_states(lines):
    """[bool] telling, per line, whether it STARTS inside a multiline string."""
    states = []
    delim = None
    for line in lines:
        states.append(delim is not None)
        delim = _scan_multiline(line, delim)
    return states
