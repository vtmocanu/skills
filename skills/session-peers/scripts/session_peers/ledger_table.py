"""Shared ledger table projection and conservative, local-only link expansion."""

import html
import json
import re
import unicodedata
from urllib.parse import quote, urlsplit

from . import ledger


def safe_url(value):
    if not isinstance(value, str) or any(ord(c) < 33 or c in '\\<>"`' for c in value):
        return None
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in ("http", "https") and parsed.hostname else None
    except ValueError:
        return None


def forge_origin(remote):
    """No SSH, DNS or network: unknown forge hosts keep shorthand as plain text."""
    if "://" not in remote:
        match = re.fullmatch(r"(?:[^/@:]+@)?([^/:]+):(.+)", remote)
        if not match:
            return None
        remote = "ssh://" + match[1] + "/" + match[2]
    try:
        parsed = urlsplit(remote)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https", "ssh", "git") or not host:
            return None
        path = parsed.path.strip("/").removesuffix(".git")
        if not path or any(part in ("", ".", "..") for part in path.split("/")):
            return None
        kind = "github" if host == "github.com" else "gitlab" if host == "gitlab.com" or host.startswith("gitlab.") else None
        if kind is None:
            return None
        authority = host
        if parsed.scheme in ("http", "https") and parsed.port:
            authority += ":" + str(parsed.port)
        scheme = parsed.scheme if parsed.scheme in ("http", "https") else "https"
        base = safe_url(scheme + "://" + authority + "/" + path)
        return {"kind": kind, "base": base} if base else None
    except ValueError:
        return None


def current_forge(cwd):
    try:
        return forge_origin(ledger.git(cwd, "remote", "get-url", "origin").strip())
    except ledger.LedgerError:
        return None


def patterns(book, head):
    names = book._git(book.path, "ls-tree", "--name-only", head, ".ledger/links.json").splitlines()
    if not names:
        return {}
    config = json.loads(book._git(book.path, "show", head + ":.ledger/links.json"))
    if (not isinstance(config, dict) or len(config) > 32
            or any(not isinstance(k, str) or not isinstance(v, str) or len(k) > 128 or len(v) > 2048
                   or k.count("<id>") != 1 or v.count("{id}") != 1 for k, v in config.items())):
        raise ledger.LedgerError("invalid .ledger/links.json: use at most 32 'label <id>': 'https://host/{id}' patterns")
    return config


def expand_link(label, forge=None, custom=None):
    result = {"label": str(label), "href": safe_url(label)}
    if result["href"]:
        return result
    for pattern, template in (custom or {}).items():
        regex = re.escape(pattern).replace(re.escape("<id>"), r"([A-Za-z0-9][A-Za-z0-9._:-]{0,127})")
        match = re.fullmatch(regex, label)
        if match:
            result["href"] = safe_url(template.replace("{id}", quote(match[1], safe="")))
            return result
    match = re.fullmatch(r"(?:(PR)\s+)?([#!])([1-9][0-9]*)", label, re.IGNORECASE)
    if match and isinstance(forge, dict) and safe_url(forge.get("base")):
        mr = bool(match[1]) or match[2] == "!"
        if forge.get("kind") == "github":
            result["href"] = forge["base"] + ("/pull/" if mr else "/issues/") + match[3]
        elif forge.get("kind") == "gitlab":
            result["href"] = forge["base"] + ("/-/merge_requests/" if mr else "/-/issues/") + match[3]
    return result


def owner_label(owner):
    if not owner:
        return "Unowned"
    name = ledger.human(owner.get("name", "")).strip()
    typed = owner["id"]
    return name if name and name != typed else typed.split(":", 1)[0] + ":" + typed.split(":", 1)[1][:8]


def project(book, head, forge=None, all_items=False, items=None):
    custom = patterns(book, head)
    items = book.items(head) if items is None else items
    groups = {}
    for item in items:
        if not all_items and (not item["owner"] or item["status"] == "done"):
            continue
        owner = item["owner"]
        ident = owner["id"] if owner else ""
        group = groups.setdefault(ident, {"owner_id": ident or None, "owner": owner_label(owner), "items": []})
        group["items"].append({k: item[k] for k in ("slug", "title", "status", "updated")})
        group["items"][-1]["links"] = [expand_link(link, forge, custom) for link in item["links"]]
    return {"key": book.key, "commit": head, "groups": sorted(groups.values(), key=lambda g: (g["owner"].casefold(), g["owner_id"] or "")),
            "unowned_open": sum(not i["owner"] and i["status"] != "done" for i in items)}


def markdown_text(value):
    value = html.escape(ledger.human(value), quote=False)
    return re.sub(r"([\\|\[\]`*_])", r"\\\1", value)


def cell_width(value):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ("F", "W") else 1
               for c in value)


def render(view, markdown=False):
    lines = []
    for group in view["groups"]:
        lines.append("### " + markdown_text(group["owner"]) if markdown else group["owner"])
        headers = ["item", "title", "status", "links", "updated"]
        rows = []
        for item in group["items"]:
            links = []
            for link in item["links"]:
                label = markdown_text(link["label"]) if markdown else ledger.human(link["label"])
                links.append("[" + label + "](<" + link["href"].replace("|", "%7C") + ">)" if markdown and link["href"] else label)
            text = markdown_text if markdown else ledger.human
            rows.append([text(item["slug"]), text(item["title"]), item["status"], " ".join(links), item["updated"]])
        if markdown:
            lines += ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
            lines += ["| " + " | ".join(row) + " |" for row in rows]
        else:
            widths = [max(cell_width(row[i]) for row in [headers] + rows) for i in range(len(headers))]
            lines += ["  ".join(cell + " " * (widths[i] - cell_width(cell)) for i, cell in enumerate(row)).rstrip() for row in [headers] + rows]
        lines.append("")
    lines.append("unowned open items: %d" % view["unowned_open"])
    return "\n".join(lines)
