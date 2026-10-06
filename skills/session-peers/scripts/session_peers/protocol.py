"""Protocol for the session-peers CLI."""

from __future__ import annotations
import uuid as uuidlib
from . import constants as sp_constants

# --------------------------------------------------------------------------
# The tag line (D4)
# --------------------------------------------------------------------------


def build_tag(from_name=None, sid=None, reply_socket=None, msg_id=None) -> str:
    """The one line every bridged message carries into a Codex thread.

    A field the sender could not fill in renders as `-` and parses back to
    None, so an untagged-looking send is still distinguishable from a typed
    prompt while carrying no reply address.
    """

    def field(value):
        # A newline would end the tag line and forge a second one, so CR and
        # LF collapse to underscores exactly like spaces (N3).
        value = (value or "").strip()
        if not value:
            return sp_constants.TAG_ABSENT
        for ch in (" ", "\r", "\n", "\t"):
            value = value.replace(ch, "_")
        return sp_constants.C0_RE.sub("", value)[:sp_constants.MAX_TAG_FIELD_CHARS] or sp_constants.TAG_ABSENT

    reply = (reply_socket or "").strip()
    return "[session-peers from=@%s sid=%s mid=%s reply=%s]" % (
        field(from_name),
        field(sid),
        field(msg_id),
        ("uds:%s" % reply) if reply else sp_constants.TAG_ABSENT,
    )


def sender_runtime(record):
    """Runtime of a verified sender registry record; "unknown" without one.

    A Codex shim registers itself with entrypoint "codex"; any other verified
    record is a Claude Code session. An unverified sender is never labelled.
    """
    if not isinstance(record, dict):
        return "unknown"
    return "codex" if record.get("entrypoint") == "codex" else "claude"


def build_origin(runtime, name, ident):
    """One provenance line naming the true runtime of a relayed message.

    ``runtime`` is "claude", "codex" or "unknown" (sender not verified). The
    line sits after the tag (Codex side) or first in the body (Claude side).
    Fields are sanitised so a name cannot end the line or forge a second one.
    """
    label = {
        "claude": "Claude Code session",
        "codex": "Codex thread",
        "unknown": "peer of unverified runtime",
    }[runtime]

    def clean(value):
        value = sp_constants.ORIGIN_BREAK_RE.sub(" ", str(value or ""))
        return value.replace("[", "(").replace("]", ")").strip()[:sp_constants.MAX_TAG_FIELD_CHARS]

    name, ident = clean(name), clean(ident)
    who = " ".join(x for x in (name, "(%s)" % ident if ident else "") if x)
    return "[session-peers from %s%s]" % (label, " " + who if who else "")


def parse_tag(text):
    """(tag_dict_or_None, body_without_tag). Never raises."""
    if not text:
        return None, text or ""
    first, sep, rest = text.partition("\n")
    if not first.startswith(sp_constants.TAG_PREFIX):
        return None, text
    m = sp_constants.TAG_RE.match(first.strip())
    if not m:
        return None, text
    tag = {}
    for key in ("from", "sid", "mid", "reply"):
        value = m.group(key)
        if value == sp_constants.TAG_ABSENT or value == "":
            tag[key] = None
        elif key == "reply" and value.startswith("uds:"):
            tag[key] = value[4:]
        else:
            tag[key] = value
    return tag, rest if sep else ""


def strip_tag(text):
    return parse_tag(text)[1]


# --------------------------------------------------------------------------
# Frames (D7)
# --------------------------------------------------------------------------


class NameError_(ValueError):
    """A peer name that cannot be put into a wrapper attribute safely."""


def valid_peer_name(name) -> bool:
    return bool(name) and bool(sp_constants.PEER_NAME_RE.match(str(name)))


def require_peer_name(name, what="thread name"):
    """Refuse a name that could break out of a wrapper attribute (B1).

    A Codex thread name reaches Claude as `from-name="..."`, so a name
    containing a quote could assert `from-mode`, the one claim D7 forbids.
    Restricting the character set at registration beats escaping at every use.
    """
    if not valid_peer_name(name):
        raise NameError_(
            "%s %r is not usable as a peer name: allow only letters, digits, "
            "dot, underscore and hyphen (up to 64). /rename the thread."
            % (what, name)
        )
    return str(name)


def escape_attr(value) -> str:
    """XML-escape one wrapper attribute value and drop control characters."""
    text = "" if value is None else str(value)
    text = text.replace("\r", "").replace("\n", "")
    text = sp_constants.C0_RE.sub("", text)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def neutralise_wrapper_markup(body) -> str:
    """Stop a body closing or forging the wrapper that carries it (B1)."""
    return sp_constants.WRAPPER_MARKUP_RE.sub(sp_constants.LT_SUBSTITUTE + r"\1\2", body or "")


def neutralise_request_markup(body) -> str:
    """Stop request content closing or forging its correlation envelope."""
    return sp_constants.REQUEST_MARKUP_RE.sub(sp_constants.LT_SUBSTITUTE + r"\1\2", body or "")


def build_wrapper(body, from_socket, from_session, from_name) -> str:
    """The wrapper Claude's parser accepts, WITHOUT `from-mode` (D7).

    Codex has no Claude permission class, and an unclassified sender is
    exactly what a bypass-permissions session holds for approval. Asserting a
    mode here would be a lie with a security consequence, which is why every
    attribute is escaped and the body cannot close the element (B1).
    """
    socket_text = str(from_socket or "")
    if sp_constants.C0_RE.search(socket_text) or set('"<>&\r\n') & set(socket_text):
        # Never ship a mangled reply address: a Claude reply would go nowhere.
        raise ValueError("socket path %r cannot be put in a wrapper" % socket_text)
    return (
        '<cross-session-message from="uds:%s" from-session="%s" from-name="%s">\n'
        "%s\n</cross-session-message>"
        % (
            escape_attr(socket_text),
            escape_attr(from_session),
            escape_attr(from_name),
            neutralise_wrapper_markup(body),
        )
    )


def unwrap_message(content):
    """(body, attrs) for a wrapped frame; (content, {}) for a bare one."""
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        content = "\n".join(parts)
    if not isinstance(content, str):
        return "", {}
    m = sp_constants.WRAPPER_RE.match(content)
    if not m:
        return content, {}
    attrs = dict(sp_constants.ATTR_RE.findall(m.group("attrs")))
    return m.group("body"), attrs


def build_user_frame(body, from_socket=None):
    """The exact frame Claude itself sends between sessions."""
    frame = {
        "msgV": 1,
        "msg_id": str(uuidlib.uuid4()),
        "type": "user",
        "message": {"role": "user", "content": body},
        "priority": "next",
    }
    if from_socket:
        frame["from"] = "uds:%s" % from_socket
    return frame


def reply_text(text, msg_id, held_reply=False):
    """Prefix a reply with the id of the request it answers.

    `msg_id` is the requester's own SendMessage msg_id (carried in the turn
    tag), so a Claude session can tell which of its messages a reply answers
    when several crossed. No id, no prefix.
    """
    if not msg_id:
        return text
    label = "held reply, in reply to message" if held_reply else "in reply to message"
    return "[%s %s]\n%s" % (label, msg_id, text)


def build_cc_body(text, thread_id, thread_name, shim_socket):
    """Wrapped when the thread has a shim socket to reply to, bare otherwise.

    Both forms start the body with a provenance line naming the sender as a
    Codex thread: the wrapper's from-name alone reads as a Claude session. The
    bare form also renders like a typed prompt with no peer name (measured).
    """
    if shim_socket:
        return build_wrapper(
            "%s\n%s" % (build_origin("codex", thread_name, thread_id), text),
            shim_socket,
            thread_id,
            thread_name,
        )
    return "%s\n%s" % (build_origin("codex", thread_name, thread_id), text)
