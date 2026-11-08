"""Static checks of the web dashboard's assets: rendering safety, CSP fit, no external requests, notices.

The behaviour of ``app.js`` is covered by ``session_peers/web/render.test.mjs`` (``node --test``);
these checks guard the source text itself, so a later edit cannot slip an HTML sink, an inline
script or style, a stored token or an external fetch past review.
"""

from __future__ import annotations

import html.parser
import pathlib
import re
import unittest
import urllib.parse

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
WEB = SCRIPTS / "session_peers" / "web"
SKILL = SCRIPTS.parent
REPO = SKILL.parent.parent
MOCK_JS = REPO / "prds" / "mockups" / "161-session-peers-web" / "app.js"
NOTICES = SKILL / "THIRD_PARTY_NOTICES.md"

# The SVG namespace is a name, never fetched; it is the only URL an asset may contain.
SVG_NS = "http://www.w3.org/2000/svg"


def read(name):
    return (WEB / name).read_text(encoding="utf-8")


def light_tokens(css):
    """The custom properties of the light ``:root`` block."""
    block = re.search(r":root\{([^}]*)\}", css).group(1)
    return dict(re.findall(r"--([a-z0-9-]+):\s*([^;]+);", block))


def luminance(hex_colour):
    channels = [int(hex_colour.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(fg, bg):
    hi, lo = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def rule(css, selector):
    """The declarations of the first rule whose selector list is exactly ``selector``."""
    match = re.search(r"(?:^|[}\s])%s\{([^}]*)\}" % re.escape(selector), css)
    if match is None:
        raise AssertionError("no rule for %s" % selector)
    return match.group(1)


def function_body(source, name):
    """The text of ``function <name>(...) { ... }`` by brace matching (the helper holds no braces in strings)."""
    match = re.search(r"\bfunction\s+%s\s*\(" % re.escape(name), source)
    if match is None:
        raise AssertionError("function %s not found" % name)
    start = source.index("{", match.end())
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return match.start(), index + 1
    raise AssertionError("function %s is not closed" % name)


class _Shell(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.script_open = False
        self.script_text = []
        self.style_tags = 0

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, attrs))
        if tag == "script":
            self.script_open = True
        if tag == "style":
            self.style_tags += 1

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag == "script":
            self.script_open = False

    def handle_data(self, data):
        if self.script_open:
            self.script_text.append(data)


class WebAssetTests(unittest.TestCase):
    def setUp(self):
        self.js = read("app.js")
        self.css = read("app.css")
        self.html = read("index.html")

    # -- app.js -------------------------------------------------------------

    def test_app_js_has_no_html_sinks_or_code_from_strings(self):
        forbidden = {
            "innerHTML": r"innerHTML",
            "outerHTML": r"outerHTML",
            "insertAdjacentHTML": r"insertAdjacentHTML",
            "document.write": r"document\s*\.\s*write",
            "eval(": r"\beval\s*\(",
            "Function(": r"\bFunction\s*\(",
            "createContextualFragment": r"createContextualFragment",
            "DOMParser": r"\bDOMParser\b",
            "srcdoc": r"srcdoc\s*=",
            "document.cookie": r"document\s*\.\s*cookie",
            "sessionStorage": r"sessionStorage",
            "indexedDB": r"indexedDB",
            "import(": r"\bimport\s*\(",
        }
        for label, pattern in forbidden.items():
            self.assertIsNone(re.search(pattern, self.js), label)
        # Handlers are attached with addEventListener (the CSP allows no inline handler); no handler
        # property is assigned, and no text is parsed as markup by any other name.
        self.assertIsNone(re.search(r"\.\s*on[a-z]+\s*=[^=]", self.js), "handler property")
        self.assertIsNone(re.search(r"\b(?:innerText|outerText)\s*=", self.js), "innerText/outerText")
        self.assertIsNone(re.search(r"\bparseFromString\b|\bsetHTML(?:Unsafe)?\b", self.js), "HTML parsing")

    def test_el_allowlists_hold_no_active_tag_or_attribute(self):
        tags = re.search(r"var HTML_TAGS = \[([^\]]*)\]", self.js).group(1)
        attrs = re.search(r"var ATTRS = \[([^\]]*)\]", self.js).group(1)
        tag_names = re.findall(r'"([^"]*)"', tags)
        attr_names = re.findall(r'"([^"]*)"', attrs)
        # Exact sets: any addition is a deliberate edit here. The composer's form controls are allowed;
        # nothing that loads, navigates, submits or runs is (no script, iframe, a, img, form, style...;
        # no on*, href, src, action, formaction, style, srcdoc, value, name, form, autofocus).
        self.assertEqual(sorted(tag_names), sorted({
            "div", "span", "p", "b", "i", "code", "button", "ul", "li", "dl", "dt", "dd", "h1", "h2", "small",
            "strong", "label", "input", "textarea"}))
        self.assertEqual(len(tag_names), len(set(tag_names)))
        self.assertEqual(sorted(attr_names), sorted({
            "class", "role", "title", "type", "tabindex", "checked", "disabled", "placeholder", "rows", "maxlength"}))
        self.assertEqual(len(attr_names), len(set(attr_names)))

    def test_the_all_confirmation_is_an_in_page_dialog(self):
        # #162 M2: the @all recipient list is confirmed in a dialog built by el() (a div with
        # role="dialog" and aria-modal, both inside the existing allowlist), never a native prompt
        # that can show only a count and cannot list the recipients.
        self.assertIsNone(re.search(r"\b(?:confirm|alert|prompt)\s*\(", self.js), "native dialog")
        self.assertNotRegex(self.js, r"\bshowModal\b|\"dialog\"\s*,\s*\{|createElement\(\s*\"dialog\"")
        self.assertIn('role: "dialog", "aria-modal": "true"', self.js)
        # Its host sits outside #app, so the page behind can be made inert while it is open.
        shell = _Shell()
        shell.feed(self.html)
        ids = [dict(attrs).get("id") for _tag, attrs in shell.tags]
        self.assertIn("dialoghost", ids)
        self.assertGreater(ids.index("dialoghost"), ids.index("mobtabs"))
        self.assertRegex(self.html, r'</div>\n<div id="dialoghost"></div>\n<script')

    def test_timers_take_functions_never_strings(self):
        calls = list(re.finditer(r"\bset(?:Timeout|Interval)\s*\(\s*", self.js))
        self.assertTrue(calls, "the page polls with setTimeout")
        for call in calls:
            rest = self.js[call.end():call.end() + 40]
            self.assertRegex(rest, r"^(?:function\b|\(|[A-Za-z_$][\w$]*\s*[,)])", rest)
            self.assertNotRegex(rest, r"^[\"'`]")

    def test_set_attribute_is_called_only_inside_el(self):
        start, end = function_body(self.js, "el")
        # describedBy() is the one other setter: it links the composer's reason, and names nothing else.
        d_start, d_end = function_body(self.js, "describedBy")
        self.assertEqual(re.findall(r'setAttribute\(\s*("[^"]*"|[^,]*)', self.js[d_start:d_end]), ['"aria-describedby"'])
        uses = [m.start() for m in re.finditer(r"setAttribute(?:NS)?\b", self.js)]
        self.assertTrue(uses, "el() sets attributes")
        for pos in uses:
            self.assertTrue(start <= pos < end or d_start <= pos < d_end, "setAttribute outside el() at offset %d" % pos)
        self.assertIsNone(re.search(r"\.\s*attributes\s*\[|\bsetAttributeNode\b|\.\s*(?:href|src|style)\s*=", self.js))

    def test_el_enforces_an_attribute_allowlist(self):
        start, end = function_body(self.js, "el")
        body = self.js[start:end]
        self.assertIn("attrAllowed", body)
        self.assertIn("throw new Error", body)
        for name in ("onclick", "href", "src", "style", "srcdoc", "xlink:href"):
            self.assertNotIn('"%s"' % name, self.js.split("function attrAllowed")[0].split("var ATTRS")[1])

    def test_storage_holds_only_the_theme(self):
        key = re.search(r'var THEME_KEY = "([^"]+)";', self.js)
        self.assertIsNotNone(key)
        self.assertNotIn("token", key.group(1).lower())
        for match in re.finditer(r"localStorage(\s*\.\s*(\w+)|\s*\[)?", self.js):
            member = match.group(2)
            if match.group(1) is None:
                continue  # a bare presence check
            self.assertIn(member, ("getItem", "setItem", "removeItem"), match.group(0))
            self.assertRegex(self.js[match.end():match.end() + 12], r"^\(THEME_KEY\b")

    def test_requests_go_only_to_the_api_with_the_header(self):
        self.assertEqual(len(re.findall(r"\bfetch\s*\(", self.js)), 1)
        self.assertIn('"X-Peers-Token": S.token', self.js)
        self.assertIn('credentials: "omit"', self.js)
        self.assertIn('cache: "no-store"', self.js)
        self.assertIn('redirect: "error"', self.js)
        # A GET sends no referrer; the chat-mode POST keeps a same-origin policy, a defence against
        # browsers that have sent "Origin: null" under no-referrer, so the server sees the real Origin.
        self.assertIn('referrerPolicy: post ? "same-origin" : "no-referrer"', self.js)
        self.assertIn('method: post ? "POST" : "GET"', self.js)
        self.assertIn('headers["Content-Type"] = "application/json"', self.js)
        self.assertIn('var SEND_PATH = "/api/send";', self.js)
        for literal in re.findall(r'"(/[^"]*)"', self.js):
            self.assertTrue(literal == "/" or literal.startswith("/api/"), literal)
        self.assertNotRegex(self.js, r"\b(?:XMLHttpRequest|WebSocket|EventSource|sendBeacon)\b")

    def test_icon_path_data_matches_the_mock(self):
        if not MOCK_JS.exists():
            self.skipTest("design mock not present")
        mock = MOCK_JS.read_text(encoding="utf-8")
        paths = re.findall(r' d="([^"]+)"', mock)[:3]  # Claude, OpenAI (Codex), OpenCode placeholder
        self.assertEqual(len(paths), 3)
        for d in paths:
            self.assertIn('"%s"' % d, self.js)

    def test_server_keyed_tables_have_no_prototype(self):
        # A channel named "constructor" must not hit Object.prototype: no {} table keyed by data.
        self.assertNotRegex(self.js, r"\b(?:names|members|rows|sessions|pages|pendingNotes|covered|focusables|loading|pageErrors)\s*[:=]\s*\{\s*\}")
        self.assertIn("Object.create(null)", self.js)
        # Copies go into a prototype-free table: an own "__proto__" key from JSON.parse would
        # otherwise hit the __proto__ setter of a {} target and swap the copy's prototype.
        self.assertNotRegex(self.js, r"Object\.assign\(\s*\{")

    # -- app.css ------------------------------------------------------------

    def test_light_theme_text_contrast_is_aa(self):
        tokens = light_tokens(self.css)
        pairs = [("ink-3", "line-2"), ("ink-3", "paper"), ("ink-3", "raised"), ("ink-3", "panel"),
                 ("mention", "paper"), ("mention", "raised"), ("ink-2", "line-2"),
                 # chat mode: the browser peer's name, the authority line and the chat badge
                 ("human", "paper"), ("human", "panel"), ("human", "raised"), ("human", "human-bg")]
        for fg, bg in pairs:
            ratio = contrast(tokens[fg], tokens[bg])
            self.assertGreaterEqual(ratio, 4.5, "--%s on --%s is %.2f:1" % (fg, bg, ratio))
        self.assertIn("color:var(--mention)", rule(self.css, ".mention"))

    def test_screen_reader_text_cannot_stretch_the_page(self):
        sr = rule(self.css, ".sr")
        for want in ("position:absolute", "width:1px", "height:1px", "overflow:hidden", "margin:-1px"):
            self.assertIn(want, sr)
        positioned = re.search(r"\n([^{}\n]*)\{position:relative\}", self.css)
        self.assertIsNotNone(positioned, "a positioned ancestor rule for .sr hosts")
        hosts = {s.strip() for s in positioned.group(1).split(",")}
        self.assertLessEqual({".lane", ".card", ".who"}, hosts)

    def test_details_pane_reachable_up_to_1080px(self):
        narrow = re.search(r"@media \(max-width:1080px\)\{(.*?)\n\}", self.css, re.S)
        self.assertIsNotNone(narrow)
        self.assertRegex(narrow.group(1), r"\.mobtabs\{display:flex")
        mid = re.search(r"@media \(min-width:721px\) and \(max-width:1080px\)\{(.*?)\n\}", self.css, re.S)
        self.assertIsNotNone(mid)
        self.assertIn(".app.pane-info aside{display:block}", mid.group(1))

    # -- index.html ---------------------------------------------------------

    def test_index_html_has_no_inline_code_and_references_only_the_two_assets(self):
        shell = _Shell()
        shell.feed(self.html)
        self.assertEqual(shell.style_tags, 0)
        self.assertEqual("".join(shell.script_text).strip(), "")
        scripts = [dict(attrs) for tag, attrs in shell.tags if tag == "script"]
        self.assertEqual([s.get("src") for s in scripts], ["/app.js"])
        links = [dict(attrs) for tag, attrs in shell.tags if tag == "link"]
        # Exactly one extra link: the favicon, an inline SVG data URI (the CSP allows img-src data:).
        icons = [l for l in links if l.get("rel") == "icon"]
        self.assertEqual(len(icons), 1)
        icon = icons[0]["href"]
        self.assertTrue(icon.startswith("data:image/svg+xml,"), icon)
        decoded = urllib.parse.unquote(icon[len("data:image/svg+xml,"):])
        self.assertNotIn("<script", decoded.lower())
        self.assertNotRegex(decoded, r"(?i)\son[a-z]+\s*=")
        self.assertNotRegex(decoded.replace(SVG_NS, ""), r"(?i)https?:")
        self.assertEqual([(l.get("rel"), l.get("href")) for l in links if l.get("rel") != "icon"],
                         [("stylesheet", "/app.css")])
        for tag, attrs in shell.tags:
            for name, value in attrs:
                self.assertFalse(name.lower().startswith("on"), "%s %s" % (tag, name))
                self.assertNotEqual(name.lower(), "style", tag)
                if name.lower() in ("src", "href", "action", "formaction", "srcset", "poster", "data", "xlink:href"):
                    self.assertIn((tag, value), [("script", "/app.js"), ("link", "/app.css"), ("link", icon)])
        self.assertNotIn("<form", self.html.lower())
        self.assertNotRegex(self.html, r"(?i)javascript:")

    # -- every asset --------------------------------------------------------

    def test_no_external_urls_in_any_asset(self):
        for name, text in (("app.js", self.js), ("app.css", self.css), ("index.html", self.html)):
            stripped = text.replace(SVG_NS, "")
            self.assertNotRegex(stripped, r"(?i)\bhttps?:", name)
            self.assertNotRegex(stripped, r"(?i)fonts\.(?:googleapis|gstatic)", name)
            self.assertNotRegex(stripped, r"(?i)[\"'(=]\s*//[a-z0-9]", name)  # protocol-relative
        self.assertNotRegex(self.css, r"(?i)@import|url\s*\(")
        self.assertNotRegex(self.css, r"(?i)expression\s*\(")

    # -- notices ------------------------------------------------------------

    def test_third_party_notices(self):
        self.assertTrue(NOTICES.is_file())
        text = NOTICES.read_text(encoding="utf-8")
        for want in ("Bootstrap Icons 1.13.1", "openai.svg", "The MIT License (MIT)",
                     "Copyright (c) 2019-2024 The Bootstrap Authors", "Permission is hereby granted",
                     'THE SOFTWARE IS PROVIDED "AS IS"', "simple-icons", "claude.svg", "CC0"):
            self.assertIn(want, text)


if __name__ == "__main__":
    unittest.main()
