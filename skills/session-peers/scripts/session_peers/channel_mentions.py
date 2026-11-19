"""Mention intent only: no resolution, delivery, writes, or authority."""

import re

_URL = re.compile(r"(?:[A-Za-z][A-Za-z0-9+.-]*://|mailto:)\S+")
_EMAIL = re.compile(r"[\w.!#$%&'*+/=?^{}|~-]+@[\w.-]+", re.UNICODE)
_TOKEN = re.compile(r"(?<![\w@\\])@([\w][\w.:-]{0,255})", re.UNICODE)


def visible_text(body):
    """Mask backtick runs and escaped @; an unclosed run masks the rest."""
    out, delimiter, index = [], 0, 0
    while index < len(body):
        char = body[index]
        if char == "`":
            end = index + 1
            while end < len(body) and body[end] == "`":
                end += 1
            count = end - index
            if not delimiter:
                delimiter = count
            elif count == delimiter:
                delimiter = 0
            out.append(" " * count)
            index = end
        elif delimiter:
            out.append("\n" if char == "\n" else " ")
            index += 1
        elif char == "\\" and index + 1 < len(body) and body[index + 1] in "@`":
            out.append("  ")
            index += 2
        else:
            out.append(char)
            index += 1
    return _EMAIL.sub(" ", _URL.sub(" ", "".join(out)))


def parse(body):
    """Ordered unique tokens outside code, URLs and email addresses.

    Tokens begin with a Unicode word character, permit dots, hyphens and
    colons, and shed trailing sentence dots/colons. here/all are lowercase
    reserved tokens. Names outside this token grammar use full typed ids.
    """
    tokens = []
    for match in _TOKEN.finditer(visible_text(body)):
        token = match[1].rstrip(".:")
        if token and token not in tokens:
            tokens.append(token)
    return {"tokens": tokens, "all": "all" in tokens, "here": "here" in tokens,
            "names": [token for token in tokens if token not in ("all", "here")]}
