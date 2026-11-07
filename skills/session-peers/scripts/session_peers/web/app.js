/*
 * session-peers web dashboard: read-only rendering of the JSON the local server returns.
 *
 * Rendering safety: every element is built by el(); text only ever enters the page as text
 * nodes; el() refuses any attribute outside a short allowlist and any tag outside a short
 * list. The access token lives in one variable, is sent as X-Peers-Token and is never stored.
 * The only storage use is the colour theme (THEME_KEY).
 *
 * Icons: the Claude mark is simple-icons claude.svg (CC0-1.0); the OpenAI mark used for Codex
 * is Bootstrap Icons 1.13.1 openai.svg (MIT, see THIRD_PARTY_NOTICES.md); the OpenCode glyph
 * is a placeholder.
 */
(function (root) {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";
  var THEME_KEY = "session-peers-web-theme";
  var POLL_MS = 2000;
  var BACKOFF_MAX_MS = 30000;
  var STATE_EVERY = 5;
  var PAGE_LIMIT = 200;
  var ALERT_WINDOW = 25;
  var JOURNAL_MIB = 8;
  var QUERY_MAX = 256;
  // A typed address the page may copy: exactly identity.parse_typed's forms
  // (constants.UUID_RE for cc/codex, constants.OPENCODE_SESSION_ID_RE for oc).
  var TYPED_RE = /^(?:cc|codex):[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$|^oc:ses_[A-Za-z0-9]{8,64}$/;
  var TOKEN_RE = /^#token=([A-Za-z0-9_-]{16,512})$/;
  var ALL_RE = /^@all(\s|$)/;

  // ------------------------------------------------------------------------
  // The one DOM helper
  // ------------------------------------------------------------------------

  var HTML_TAGS = ["div", "span", "p", "b", "i", "code", "button", "ul", "li", "dl", "dt", "dd", "h1", "h2", "small", "strong"];
  var SVG_TAGS = ["svg", "path", "g"];
  var ATTRS = ["class", "role", "title", "type", "tabindex"];
  var SVG_ATTRS = ["viewBox", "d", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "fill-rule", "clip-rule", "xmlns"];

  function attrAllowed(name, svg) {
    if (ATTRS.indexOf(name) >= 0) return true;
    if (/^aria-[a-z]+$/.test(name) || /^data-[a-z][a-z0-9-]*$/.test(name)) return true;
    return svg && SVG_ATTRS.indexOf(name) >= 0;
  }

  function el(tag, attrs) {
    var svg = tag.indexOf("svg:") === 0;
    var name = svg ? tag.slice(4) : tag;
    if ((svg ? SVG_TAGS : HTML_TAGS).indexOf(name) < 0) throw new Error("el: tag not allowed: " + tag);
    var node = svg ? document.createElementNS(SVG_NS, name) : document.createElement(name);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        var value = attrs[key];
        if (value === null || value === undefined || value === false) return;
        if (!attrAllowed(key, svg)) throw new Error("el: attribute not allowed: " + key);
        node.setAttribute(key, value === true ? "true" : String(value));
      });
    }
    for (var i = 2; i < arguments.length; i++) append(node, arguments[i]);
    return node;
  }

  function append(node, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) {
      child.forEach(function (c) { append(node, c); });
      return;
    }
    if (typeof child === "object" && typeof child.nodeType === "number") {
      node.appendChild(child);
      return;
    }
    node.appendChild(document.createTextNode(String(child)));
  }

  function fill(node, children) {
    if (!node) return;
    node.textContent = "";
    append(node, children);
  }

  function on(node, type, handler) {
    if (node && node.addEventListener) node.addEventListener(type, handler);
    return node;
  }

  // Keyboard activation for a non-button element with role="button" (a real <button> already clicks).
  function onActivate(node, handler) {
    on(node, "click", handler);
    on(node, "keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        if (event.preventDefault) event.preventDefault();
        handler(event);
      }
    });
    return node;
  }

  // ------------------------------------------------------------------------
  // Icons (path data as in the design mock)
  // ------------------------------------------------------------------------

  var CLAUDE_D = "m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z";
  var OPENAI_D = "M14.949 6.547a3.94 3.94 0 0 0-.348-3.273 4.11 4.11 0 0 0-4.4-1.934A4.1 4.1 0 0 0 8.423.2 4.15 4.15 0 0 0 6.305.086a4.1 4.1 0 0 0-1.891.948 4.04 4.04 0 0 0-1.158 1.753 4.1 4.1 0 0 0-1.563.679A4 4 0 0 0 .554 4.72a3.99 3.99 0 0 0 .502 4.731 3.94 3.94 0 0 0 .346 3.274 4.11 4.11 0 0 0 4.402 1.933c.382.425.852.764 1.377.995.526.231 1.095.35 1.67.346 1.78.002 3.358-1.132 3.901-2.804a4.1 4.1 0 0 0 1.563-.68 4 4 0 0 0 1.14-1.253 3.99 3.99 0 0 0-.506-4.716m-6.097 8.406a3.05 3.05 0 0 1-1.945-.694l.096-.054 3.23-1.838a.53.53 0 0 0 .265-.455v-4.49l1.366.778q.02.011.025.035v3.722c-.003 1.653-1.361 2.992-3.037 2.996m-6.53-2.75a2.95 2.95 0 0 1-.36-2.01l.095.057L5.29 12.09a.53.53 0 0 0 .527 0l3.949-2.246v1.555a.05.05 0 0 1-.022.041L6.473 13.3c-1.454.826-3.311.335-4.15-1.098m-.85-6.94A3.02 3.02 0 0 1 3.07 3.949v3.785a.51.51 0 0 0 .262.451l3.93 2.237-1.366.779a.05.05 0 0 1-.048 0L2.585 9.342a2.98 2.98 0 0 1-1.113-4.094zm11.216 2.571L8.747 5.576l1.362-.776a.05.05 0 0 1 .048 0l3.265 1.86a3 3 0 0 1 1.173 1.207 2.96 2.96 0 0 1-.27 3.2 3.05 3.05 0 0 1-1.36.997V8.279a.52.52 0 0 0-.276-.445m1.36-2.015-.097-.057-3.226-1.855a.53.53 0 0 0-.53 0L6.249 6.153V4.598a.04.04 0 0 1 .019-.04L9.533 2.7a3.07 3.07 0 0 1 3.257.139c.474.325.843.778 1.066 1.303.223.526.289 1.103.191 1.664zM5.503 8.575 4.139 7.8a.05.05 0 0 1-.026-.037V4.049c0-.57.166-1.127.476-1.607s.752-.864 1.275-1.105a3.08 3.08 0 0 1 3.234.41l-.096.054-3.23 1.838a.53.53 0 0 0-.265.455zm.742-1.577 1.758-1 1.762 1v2l-1.755 1-1.762-1z";
  var OPENCODE_D = "M5.5 3H3v10h2.5M10.5 3H13v10h-2.5";
  var UNKNOWN_D = "M8 2.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 1 0 0-11";
  var WARN_D = ["M8 1.5 15 14H1z", "M8 6v3.5M8 11.6v.1"];

  function iconSvg(rt) {
    if (rt === "claude") {
      return el("svg:svg", { viewBox: "0 0 24 24", class: "claude-mark", fill: "currentColor", "aria-hidden": "true" },
        el("svg:path", { d: CLAUDE_D }));
    }
    if (rt === "codex") {
      return el("svg:svg", { viewBox: "0 0 16 16", fill: "currentColor", "aria-hidden": "true" }, el("svg:path", { d: OPENAI_D }));
    }
    if (rt === "oc") {
      return el("svg:svg", { viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", "stroke-width": "1.8",
        "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" }, el("svg:path", { d: OPENCODE_D }));
    }
    return el("svg:svg", { viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", "stroke-width": "1.6", "aria-hidden": "true" },
      el("svg:path", { d: UNKNOWN_D }));
  }

  function hicon(rt) {
    return el("span", { class: "hicon" }, iconSvg(rt));
  }

  function warnIcon() {
    return el("svg:svg", { viewBox: "0 0 16 16", class: "warnicon", fill: "none", stroke: "currentColor", "aria-hidden": "true" },
      el("svg:path", { d: WARN_D[0], "stroke-width": "1.5", "stroke-linejoin": "round" }),
      el("svg:path", { d: WARN_D[1], "stroke-width": "1.6", "stroke-linecap": "round" }));
  }

  // ------------------------------------------------------------------------
  // Model state (module-local; nothing is persisted)
  // ------------------------------------------------------------------------

  var S = {
    token: null, state: null, names: {}, members: {}, sessions: {}, rows: {},
    selected: "general", view: "conv", pages: {}, pendingNotes: {}, all: null, search: null,
    cursor: null, polls: 0, failures: 0, polling: false, stopped: false, timer: null, needState: false,
    live: { mode: "starting" }, untracked: 0, journalGap: false, pane: "main", focusables: {}
  };

  var RT_LABEL = { claude: "Claude Code", codex: "Codex", oc: "OpenCode", unknown: "Unknown runtime" };
  var STATE_LABEL = { working: "Working", idle: "Idle", unverified: "Unverified", gone: "Gone" };

  function str(value) {
    return typeof value === "string" ? value : (value === null || value === undefined ? "" : String(value));
  }

  function rtOf(typed) {
    var kind = str(typed).split(":")[0];
    return kind === "cc" ? "claude" : (kind === "codex" || kind === "oc") ? kind : "unknown";
  }

  function shortId(typed) {
    var text = str(typed);
    var at = text.indexOf(":");
    if (at < 0 || text.length <= 18) return text;
    var id = text.slice(at + 1);
    return text.slice(0, at + 1) + id.slice(0, 4) + "…" + id.slice(-4);
  }

  function nameOf(typed) {
    var name = S.names[typed];
    return name ? name : shortId(typed);
  }

  function copyable(address) {
    return typeof address === "string" && TYPED_RE.test(address);
  }

  // ------------------------------------------------------------------------
  // Time
  // ------------------------------------------------------------------------

  function parseTs(ts) {
    if (typeof ts === "number" && isFinite(ts)) return new Date(ts > 1e12 ? ts : ts * 1000);
    if (typeof ts !== "string" || !ts) return null;
    var ms = Date.parse(ts);
    if (isNaN(ms)) ms = Date.parse(ts.replace(/(\.\d{3})\d+/, "$1"));
    return isNaN(ms) ? null : new Date(ms);
  }

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function clock(ts) {
    var d = parseTs(ts);
    return d ? pad(d.getHours()) + ":" + pad(d.getMinutes()) : str(ts);
  }

  function dayKey(ts) {
    var d = parseTs(ts);
    return d ? d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) : "";
  }

  function dayLabel(ts) {
    var d = parseTs(ts);
    if (!d) return "";
    var now = new Date();
    var today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    var that = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    if (that === today) return "Today";
    if (that === today - 86400000) return "Yesterday";
    return dayKey(ts);
  }

  function stamp(ts) {
    var d = parseTs(ts);
    return d ? d.getTime() : 0;
  }

  // ------------------------------------------------------------------------
  // Pieces
  // ------------------------------------------------------------------------

  function stateDot(state) {
    var cls = "dot" + (state === "working" ? " busy" : state === "unverified" ? " unv" : state === "gone" ? " gone" : "");
    return el("i", { class: cls, title: STATE_LABEL[state] || "Unverified", "aria-hidden": "true" });
  }

  function chip(member) {
    var typed = member.typed;
    var rt = rtOf(typed);
    var state = member.state || "unverified";
    return el("span", { class: "who " + rt + (state === "gone" ? " is-gone" : ""), title: str(typed) },
      hicon(rt), el("span", { class: "nm" }, nameOf(typed)), stateDot(state),
      el("span", { class: "sr" }, ", " + (STATE_LABEL[state] || "Unverified")));
  }

  function nameRef(typed) {
    return el("span", { class: "ref", title: str(typed) }, nameOf(typed));
  }

  function chanLabel(key) {
    var row = S.rows[key];
    var display = row && typeof row.display === "string" ? row.display : str(key);
    if (row && row.kind === "buddy") return [display];
    return [el("span", { class: "chan" }, "#"), display.replace(/^#/, "")];
  }

  function chanText(key) {
    var row = S.rows[key];
    var display = row && typeof row.display === "string" ? row.display : str(key);
    return row && row.kind === "buddy" ? display : "#" + display.replace(/^#/, "");
  }

  function note(text, tone) {
    return el("div", { class: "note" + (tone ? " " + tone : ""), role: "note" }, text);
  }

  // Text segments of a body: an @all lead and `code` spans become elements, all as text.
  function bodyNodes(body) {
    var text = str(body);
    var out = [];
    if (ALL_RE.test(text)) {
      out.push(el("b", { class: "mention" }, "@all"));
      text = text.slice(4);
    }
    var parts = text.split(/`([^`\n]{1,200})`/);
    for (var i = 0; i < parts.length; i++) {
      if (i % 2 === 1) out.push(el("code", null, parts[i]));
      else if (parts[i]) out.push(parts[i]);
    }
    return out;
  }

  // ------------------------------------------------------------------------
  // Delivery: outcome names, ACK state, tallies
  // ------------------------------------------------------------------------

  // Only an ACK is green: "transport accepted" is not "read by the model".
  function tone(state) {
    var s = str(state);
    if (s === "acked") return "ok";
    if (s.indexOf("refused: ") === 0 || s === "skipped: gone") return "bad";
    if (s === "transport accepted" || s === "recorded" || s === "complete") return "sent";
    if (s === "in progress") return "now";
    return "warn";
  }

  function ackText(ack) {
    var a = str(ack);
    if (a === "pending") return "ACK pending";
    if (a === "not requested") return "ACK not requested";
    if (a === "not requested (unverified)") return "ACK not requested (unverified)";
    if (a === "unverified") return "ACK unverified";
    return "ACK " + a;
  }

  function tallyOf(rows) {
    var t = { acked: 0, total: 0, missing: [], unverified: 0 };
    (rows || []).forEach(function (row) {
      if (!row || !("ack" in row) || str(row.state).indexOf("refused: ") === 0) return;
      t.total += 1;
      if (row.ack === "acked") t.acked += 1;
      else {
        t.missing.push(row.recipient);
        if (row.ack === "unverified") t.unverified += 1;
      }
    });
    return t;
  }

  function renderDelivery(delivery) {
    if (!delivery || typeof delivery !== "object") return null;
    var rows = Array.isArray(delivery.recipients) ? delivery.recipients : [];
    var items = rows.map(function (row) {
      var state = str(row.state);
      var bits = [el("i", { "aria-hidden": "true" }), nameRef(row.recipient), el("span", { class: "out" }, state)];
      if ("ack" in row && row.ack !== "acked") bits.push(el("span", { class: "ack" }, ackText(row.ack)));
      if (row.transport_error) bits.push(el("span", { class: "terr" }, "transport error: " + str(row.transport_error)));
      return el("li", { class: "drow t-" + tone(row.transport_error ? "skipped: gone" : state) }, bits);
    });
    return el("div", { class: "delivery" },
      el("span", { class: "dstate t-" + tone(delivery.state) }, "Delivery: " + str(delivery.state)),
      items.length ? el("ul", { class: "drows", "aria-label": "Delivery per recipient" }, items) : null,
      delivery.partial ? el("span", { class: "dpartial", title: "The outcome or ACK lies beyond what one bounded read reaches" }, "partial") : null);
  }

  function renderTally(entry) {
    var rows = entry.delivery && Array.isArray(entry.delivery.recipients) ? entry.delivery.recipients : [];
    var t = entry.tally && typeof entry.tally === "object" ? entry.tally : tallyOf(rows);
    var counted = rows.filter(function (r) { return r && "ack" in r && str(r.state).indexOf("refused: ") !== 0; });
    var line = t.acked + " of " + t.total + " acknowledged";
    if (t.unverified) line += " (" + t.unverified + " unverified)";
    return el("div", { class: "tallybox" },
      el("div", { class: "ackline" + (t.acked < t.total ? " short" : "") }, line),
      counted.length ? el("div", { class: "tally" }, counted.map(function (r) {
        return el("span", { class: r.ack === "acked" ? "yes" : "no", title: str(r.recipient) + ": " + ackText(r.ack) }, nameOf(r.recipient));
      })) : null);
  }

  function opencodeStatement(cov) {
    if (!cov || typeof cov !== "object") return null;
    if (cov.enumerated === false) {
      return "OpenCode was not enumerated: " + str(cov.error) + ". OpenCode sessions are absent from the recipients and the ACK tally.";
    }
    if (cov.hours !== undefined && cov.hours !== null) {
      return "OpenCode coverage: top-level sessions updated within the last " + str(cov.hours) + " h (as peers.py list).";
    }
    if (cov.scope) return "OpenCode coverage: " + str(cov.scope) + ".";
    return null;
  }

  function gapStatement(gap) {
    return "Discovery incomplete: " + str(gap) + ". That runtime is absent from the recipients and the ACK tally.";
  }

  // ------------------------------------------------------------------------
  // Entries
  // ------------------------------------------------------------------------

  function sysline(kind, children, entry, opts) {
    return el("div", { class: "sysline k-" + kind, "data-seq": entry && typeof entry.seq === "number" ? entry.seq : null },
      children, opts && opts.channel && entry.channel ? el("span", { class: "where" }, " in " + chanText(entry.channel)) : null,
      entry && entry.ts !== undefined && entry.ts !== null ? el("span", { class: "at" }, clock(entry.ts)) : null);
  }

  function membershipNodes(text) {
    return str(text).split(/(\s+)/).map(function (token) {
      return S.names[token] || S.members[token] || /^(?:cc|codex|oc):\S+$/.test(token) ? nameRef(token) : token;
    });
  }

  function toText(entry, opts) {
    var general = (entry.channel || opts.channelKey) === "general";
    var rows = entry.delivery && Array.isArray(entry.delivery.recipients) ? entry.delivery.recipients : null;
    if (rows && ALL_RE.test(str(entry.body))) return "@all, " + rows.length + " recipient" + (rows.length === 1 ? "" : "s");
    if (general) return "#general";
    if (!rows) return null;
    if (!rows.length) return "nobody (recorded only)";
    var names = rows.slice(0, 4).map(function (r) { return nameOf(r.recipient); });
    return names.join(", ") + (rows.length > 4 ? " +" + (rows.length - 4) : "");
  }

  function renderSend(entry, opts) {
    var who = entry.sender || entry.actor;
    var rt = rtOf(who);
    var to = toText(entry, opts);
    var body = [];
    var v = entry.verdict;
    if (v && typeof v === "object" && (v.verdict === "approve" || v.verdict === "revise" || v.verdict === "block")) {
      body.push(el("div", { class: "verdict " + v.verdict },
        el("div", { class: "v" }, str(v.label)),
        el("div", { class: "vd" },
          el("div", { class: "vtext" }, bodyNodes(v.text)),
          v.sha ? el("div", { class: "pin" }, "Pinned ", el("span", { class: "mono" }, str(v.sha))) : null)));
    } else {
      body.push(el("p", { class: "text" }, bodyNodes(entry.body)));
    }
    if (entry.tally || (entry.delivery && ALL_RE.test(str(entry.body)) && entry.journaled)) body.push(renderTally(entry));
    if (!opts.search) body.push(renderDelivery(entry.delivery));
    if (entry.coverage && typeof entry.coverage === "object") {
      var lines = [];
      var oc = opencodeStatement(entry.coverage.opencode);
      if (oc) lines.push(el("li", null, oc));
      (Array.isArray(entry.coverage.discovery_incomplete) ? entry.coverage.discovery_incomplete : []).forEach(function (g) {
        lines.push(el("li", { class: "gap" }, gapStatement(g)));
      });
      if (lines.length) body.push(el("ul", { class: "postcov" }, lines));
    }
    return el("div", { class: "msg", "data-seq": typeof entry.seq === "number" ? entry.seq : null },
      el("div", { class: "avatar " + rt, "aria-hidden": "true" }, iconSvg(rt)),
      el("div", { class: "msgmain" },
        el("div", { class: "meta" },
          el("span", { class: "name", title: str(who) }, nameOf(who)),
          el("span", { class: "rt" }, RT_LABEL[rt]),
          to ? el("span", null, "to " + to) : null,
          opts.channel && entry.channel ? el("span", { class: "where" }, "in " + chanText(entry.channel)) : null,
          el("span", { class: "at", title: str(entry.ts) }, clock(entry.ts))),
        el("div", { class: "body" }, body)));
  }

  function renderEntry(entry, opts) {
    opts = opts || {};
    if (!entry || typeof entry !== "object") return null;
    var kind = str(entry.kind);
    if (entry.oversized || kind === "oversized") {
      return sysline("oversized", el("span", null, "Entry too large to display" + (typeof entry.seq === "number" ? " (#" + entry.seq + ")" : "")), entry, opts);
    }
    if (kind === "send") return renderSend(entry, opts);
    if (kind === "outcome") {
      // Outcomes are joined into their send's delivery row; only search shows the raw entry.
      return opts.search ? sysline("outcome", el("span", null, str(entry.text)), entry, opts) : null;
    }
    if (kind === "left" || kind === "removed" || kind === "handover" || kind === "joined" || kind === "created") {
      var extra = kind === "removed" ? " (gone)" : "";
      return sysline(kind, [el("span", null, membershipNodes(entry.text)), extra], entry, opts);
    }
    return sysline("other", el("span", { class: "othertext" }, str(entry.text)), entry, opts);
  }

  function renderEntries(entries, opts) {
    var out = [];
    var lastDay = null;
    (entries || []).forEach(function (entry) {
      var node = renderEntry(entry, opts);
      if (!node) return;
      var key = entry && entry.ts !== undefined ? dayKey(entry.ts) : "";
      if (key && key !== lastDay) {
        out.push(el("div", { class: "day" }, dayLabel(entry.ts)));
        lastDay = key;
      }
      out.push(node);
    });
    return out;
  }

  function renderChannelPage(page, opts) {
    opts = Object.assign({ channelKey: page && page.channel }, opts || {});
    var out = [];
    if (!page) return out;
    if (page.next !== null && page.next !== undefined) {
      out.push(on(el("button", { class: "older", type: "button" }, "Load older"), "click", function () {
        if (opts.onOlder) opts.onOlder();
      }));
    } else {
      out.push(el("div", { class: "day start" }, page.partial ? "Older entries could not be read within the request budget" : "Start of the retained history"));
    }
    var shown = renderEntries(page.entries, opts);
    if (!shown.length) out.push(el("div", { class: "empty" }, "No entries in this channel yet."));
    return out.concat(shown);
  }

  function renderPageNotes(page) {
    var out = [];
    if (!page) return out;
    (page.notes || []).forEach(function (text) { out.push(note(text, "warn")); });
    if (page.journal_partial) out.push(note("ACK state read from the latest " + JOURNAL_MIB + " MiB of the journal; older ACKs show as unverified."));
    if (page.partial) out.push(note("Partial: something on this page lay beyond the per-request read budget and shows as unverified."));
    if (page.info && page.info.unreadable) out.push(note("This channel's membership record could not be read.", "warn"));
    if (page.info && page.info.omitted_members) out.push(note(page.info.omitted_members + " members beyond the member limit are not shown."));
    return out;
  }

  function gapNote(gap) {
    var reason = gap && typeof gap === "object" ? gap.reason : null;
    if (reason === "pruned") return "Older entries were pruned from this channel's history; showing its latest page.";
    if (reason === "regression") return "This channel's history went backwards (rewritten); showing its latest page.";
    return "Too many new entries to stream (" + str(reason || "gap") + "); showing this channel's latest page.";
  }

  function renderCoverage(kind, row) {
    var lines = [];
    if (kind === "all") {
      lines.push("Shows what went through session-peers. Native Claude-to-Claude messages (SendMessage) are missing unless their sender recorded them.");
    } else {
      var cov = row && row.coverage && typeof row.coverage === "object" ? row.coverage : {};
      if (cov.native_claude) {
        lines.push("This channel has a Claude Code member. Native Claude-to-Claude messages (SendMessage) are not in this view unless their sender recorded them; delivery for those is unknown.");
      }
      var oc = opencodeStatement(cov.opencode);
      if (oc) lines.push(oc);
      (Array.isArray(cov.discovery_incomplete) ? cov.discovery_incomplete : []).forEach(function (g) { lines.push(gapStatement(g)); });
    }
    if (!lines.length) return null;
    return el("div", { class: "coverage", role: "note" }, warnIcon(),
      el("div", null, lines.map(function (line) { return el("p", null, line); })));
  }

  // ------------------------------------------------------------------------
  // Rail and details
  // ------------------------------------------------------------------------

  function chainText(row) {
    var chain = Array.isArray(row.handover_chain) ? row.handover_chain : [];
    if (!chain.length) return null;
    var names = [nameOf(chain[0].member)];
    chain.forEach(function (link) { names.push(nameOf(link.to)); });
    return names.join(" → ");
  }

  function alertBadge(alert) {
    if (alert === "missing_acks") {
      return el("span", { class: "badge alert", title: "Covers the latest " + ALERT_WINDOW + " entries" }, "missing ACKs");
    }
    if (alert === "unverified") {
      return el("span", { class: "badge unv", title: "ACK state could not be established; covers the latest " + ALERT_WINDOW + " entries" }, "ACKs unverified");
    }
    return null;
  }

  function renderLane(row, current, select) {
    var key = str(row.key);
    var members = Array.isArray(row.members) ? row.members : [];
    var body;
    if (row.unreadable) {
      body = el("div", { class: "lineage" }, "Membership record unreadable");
    } else if (key === "general") {
      var counts = { claude: 0, codex: 0, oc: 0 };
      members.forEach(function (m) { var rt = rtOf(m.typed); if (rt in counts) counts[rt] += 1; });
      body = [el("div", { class: "lineage" }, "Every live session on this machine"),
        el("div", { class: "members" }, ["claude", "codex", "oc"].map(function (rt) {
          return el("span", { class: "who " + rt, title: counts[rt] + " " + RT_LABEL[rt] }, hicon(rt), String(counts[rt]));
        }))];
    } else {
      var chain = chainText(row);
      body = [el("div", { class: "lineage" }, chain || (members.length + " member" + (members.length === 1 ? "" : "s"))),
        el("div", { class: "members" }, members.map(chip),
          row.omitted_members ? el("span", { class: "who more" }, "+" + row.omitted_members) : null)];
    }
    var lane = el("div", { class: "lane", role: "button", tabindex: "0", "aria-current": current ? "true" : null, "data-focus": "lane:" + key },
      el("div", { class: "lanetop" }, el("span", { class: "lanename" }, chanLabel(key)), alertBadge(row.alert)), body);
    S.focusables["lane:" + key] = lane;
    return onActivate(lane, function () { select(key); });
  }

  function renderRail(state, selected, select) {
    var rows = state && Array.isArray(state.channels) ? state.channels : [];
    return rows.map(function (row) { return renderLane(row, row.key === selected, select || function () {}); });
  }

  function budgetNodes(details) {
    if (details.budget_unknown || !details.reply_budget || typeof details.reply_budget !== "object") {
      return [el("dt", null, "Reply budget"), el("dd", null, "unknown")];
    }
    var rb = details.reply_budget;
    if (!rb.requester) {
      return [el("dt", null, "Reply budget"), el("dd", null, "no current requester, " + str(rb.label || "as last saved by the shim"))];
    }
    var cap = typeof rb.cap === "number" ? rb.cap : 0;
    var left = typeof rb.remaining === "number" ? rb.remaining : 0;
    var tenth = cap > 0 ? Math.max(0, Math.min(10, Math.round(left / cap * 10))) : 0;
    var extra = [];
    if (rb.expired) extra.push("window expired, nothing counted");
    if (rb.pending) extra.push("pending: " + (Array.isArray(rb.pending_kinds) ? rb.pending_kinds.join(", ") : "change"));
    return [el("dt", null, "Replies left"),
      el("dd", null, left + " of " + cap + " toward ", nameRef(rb.requester),
        el("div", { class: "meter w" + tenth + (cap > 0 && left / cap < 0.1 ? " low" : ""), role: "img", "aria-label": left + " of " + cap + " replies left" }, el("span")),
        el("small", { class: "muted" }, str(rb.label || "as last saved by the shim") + (extra.length ? "; " + extra.join("; ") : "")))];
  }

  function copyButton(address) {
    if (!copyable(address)) {
      return el("button", { type: "button", "aria-disabled": "true", title: "Not a typed session address" }, "Copy address");
    }
    var button = el("button", { type: "button" }, "Copy address");
    on(button, "click", function () {
      var nav = root.navigator;
      if (!nav || !nav.clipboard || !nav.clipboard.writeText) return;
      nav.clipboard.writeText(address).then(function () {
        button.textContent = "Copied";
        root.setTimeout(function () { button.textContent = "Copy address"; }, 1500);
      }, function () { button.textContent = "Copy failed"; });
    });
    return button;
  }

  function renderMemberCard(member, row) {
    var details = S.sessions[member.typed] || {};
    var rt = rtOf(member.typed);
    var address = typeof details.address === "string" ? details.address : str(member.typed);
    var state = details.state || member.state || "unverified";
    var kv = [el("dt", null, "State"), el("dd", null, STATE_LABEL[state] || "Unverified"),
      el("dt", null, "Address"), el("dd", { class: "mono" }, address)];
    if (rt === "codex") kv = kv.concat(budgetNodes(details));
    return el("div", { class: "card" },
      el("div", { class: "srow" }, chip({ typed: member.typed, state: state }),
        el("span", { class: "muted" }, (member.runtime || RT_LABEL[rt]) + (row && row.owner === member.typed ? ", owner" : ""))),
      el("dl", { class: "kv" }, kv),
      el("div", { class: "acts" }, copyButton(address)));
  }

  function renderDetails(row) {
    if (!row) return [el("h2", null, "Details"), el("p", { class: "muted" }, "No channel selected.")];
    var members = Array.isArray(row.members) ? row.members : [];
    var out = [el("h2", null, chanText(row.key))];
    if (row.key === "general") {
      out.push(el("div", { class: "card" }, el("b", null, "Everyone"),
        el("dl", { class: "kv" }, ["claude", "codex", "oc"].map(function (rt) {
          var mine = members.filter(function (m) { return rtOf(m.typed) === rt; });
          var working = mine.filter(function (m) { return m.state === "working"; }).length;
          return [el("dt", null, hicon(rt), " " + RT_LABEL[rt]), el("dd", null, mine.length + " live, " + working + " working")];
        }))));
    }
    if (row.unreadable) out.push(note("Membership record unreadable.", "warn"));
    members.forEach(function (m) { out.push(renderMemberCard(m, row)); });
    var chain = chainText(row);
    if (chain) {
      out.push(el("div", { class: "card small" }, el("b", null, "Earlier sessions in this channel"), el("div", null, chain),
        el("div", { class: "muted" }, "From the handover entries in the latest " + ALERT_WINDOW + " entries.")));
    }
    return out;
  }

  // ------------------------------------------------------------------------
  // Server access
  // ------------------------------------------------------------------------

  function api(path) {
    return root.fetch(path, {
      method: "GET", headers: { "X-Peers-Token": S.token }, credentials: "omit", cache: "no-store",
      redirect: "error", referrerPolicy: "no-referrer"
    }).then(function (res) {
      if (!res.ok) {
        var err = new Error("HTTP " + res.status);
        err.status = res.status;
        throw err;
      }
      return res.json();
    });
  }

  function readToken() {
    var loc = root.location;
    var hash = loc && typeof loc.hash === "string" ? loc.hash : "";
    var match = TOKEN_RE.exec(hash);
    if (hash && root.history && root.history.replaceState) {
      try { root.history.replaceState(null, "", loc.pathname + (loc.search || "")); } catch (e) { /* keep going */ }
    }
    return match ? match[1] : null;
  }

  // ------------------------------------------------------------------------
  // Controller
  // ------------------------------------------------------------------------

  function $(id) { return document.getElementById(id); }

  function setState(data) {
    S.state = data && typeof data === "object" ? data : { channels: [], sessions: [] };
    var names = {}, members = {}, rows = {}, sessions = {};
    (Array.isArray(S.state.channels) ? S.state.channels : []).forEach(function (row) {
      rows[row.key] = row;
      (Array.isArray(row.members) ? row.members : []).forEach(function (m) {
        members[m.typed] = m;
        if (typeof m.name === "string" && m.name) names[m.typed] = m.name;
      });
    });
    (Array.isArray(S.state.sessions) ? S.state.sessions : []).forEach(function (s) { sessions[s.member] = s; });
    S.names = names; S.members = members; S.rows = rows; S.sessions = sessions;
    if (typeof S.state.untracked === "number") S.untracked = S.state.untracked;
    if (!rows[S.selected]) S.selected = rows.general ? "general" : (S.state.channels[0] ? S.state.channels[0].key : "general");
  }

  function refreshState() {
    return api("/api/state").then(function (data) {
      setState(data);
      if (!S.cursor && typeof data.cursor === "string") S.cursor = data.cursor;
      renderChrome();
    });
  }

  function pageUrl(key, before) {
    return "/api/channel/" + encodeURIComponent(key) + "?" + (before !== undefined && before !== null ? "before=" + encodeURIComponent(before) + "&" : "") + "limit=" + PAGE_LIMIT;
  }

  function normalizePage(data) {
    var page = data && typeof data === "object" ? data : {};
    return {
      channel: page.channel, info: page.info || {}, entries: Array.isArray(page.entries) ? page.entries.slice() : [],
      next: page.next === undefined ? null : page.next, partial: !!page.partial, journal_partial: !!page.journal_partial,
      coverage: page.coverage || {}, gap: page.gap || null, notes: []
    };
  }

  function loadLatest(key, extraNote) {
    return api(pageUrl(key)).then(function (data) {
      var page = normalizePage(data);
      var pending = S.pendingNotes[key];
      if (pending) { page.notes.push(pending); delete S.pendingNotes[key]; }
      if (extraNote) page.notes.push(extraNote);
      if (page.gap) page.notes.push("Part of this page could not be read within the request budget.");
      S.pages[key] = page;
      return page;
    });
  }

  function loadOlder(key) {
    var page = S.pages[key];
    if (!page || page.next === null) return Promise.resolve();
    var stream = $("stream");
    var before = stream ? stream.scrollHeight - stream.scrollTop : 0;
    return api(pageUrl(key, page.next)).then(function (data) {
      var older = normalizePage(data);
      var first = firstSeq(page.entries);
      var add = older.entries.filter(function (e) { return first === null || typeof e.seq !== "number" || e.seq < first; });
      page.entries = add.concat(page.entries);
      page.next = older.next;
      page.partial = page.partial || older.partial;
      page.journal_partial = page.journal_partial || older.journal_partial;
      renderMain({ keepFromBottom: before });
    }, showError);
  }

  function firstSeq(entries) {
    for (var i = 0; i < entries.length; i++) if (typeof entries[i].seq === "number") return entries[i].seq;
    return null;
  }

  function lastSeq(entries) {
    for (var i = entries.length - 1; i >= 0; i--) if (typeof entries[i].seq === "number") return entries[i].seq;
    return null;
  }

  function selectChannel(key) {
    S.selected = key;
    S.view = "conv";
    S.search = null;
    var ready = S.pages[key] ? Promise.resolve() : loadLatest(key);
    renderChrome();
    renderMain({ toBottom: true });
    if (isPhone()) setPane("main");
    return ready.then(function () { renderMain({ toBottom: true }); }, showError);
  }

  function loadAll(next) {
    var url = "/api/all" + (next ? "?next=" + encodeURIComponent(next) : "");
    return api(url).then(function (data) {
      var entries = Array.isArray(data.entries) ? data.entries : [];
      if (!next || !S.all) {
        S.all = { entries: [], covered: {}, trimmed: 0, notes: [] };
      }
      var all = S.all;
      all.entries = all.entries.concat(entries);
      all.entries.sort(function (a, b) { return stamp(a.ts) - stamp(b.ts); });
      entries.forEach(function (e) { if (e.channel) all.covered[e.channel] = true; });
      all.shown = data.shown; all.total = data.total; all.omitted = data.omitted;
      all.next = data.next || null; all.partial = !!data.partial; all.trimmed += data.trimmed || 0;
      renderMain({ toBottom: !next });
    }, function (err) {
      if (err && err.status === 409) {
        S.all = null;
        return loadAll(null).then(function () { if (S.all) S.all.notes.push("The channel list changed; reloaded from the start."); renderMain({}); });
      }
      showError(err);
    });
  }

  function showAll() {
    S.view = "all";
    S.search = null;
    renderChrome();
    renderMain({ toBottom: true });
    return S.all ? Promise.resolve() : loadAll(null);
  }

  function runSearch(q, scope, next) {
    var url = "/api/search?q=" + encodeURIComponent(q) + "&scope=" + encodeURIComponent(scope) + (next ? "&next=" + encodeURIComponent(next) : "");
    return api(url).then(function (data) {
      var matches = Array.isArray(data.matches) ? data.matches : [];
      if (!next || !S.search) S.search = { q: q, scope: scope, matches: [], notes: [] };
      S.search.matches = S.search.matches.concat(matches);
      S.search.searched = data.searched_channels; S.search.total = data.total_channels;
      S.search.next = data.next || null; S.search.partial = !!data.partial; S.search.window = data.window;
      S.view = "search";
      renderChrome();
      renderMain({ toTop: !next });
    }, function (err) {
      if (err && err.status === 409) {
        S.search = { q: q, scope: scope, matches: [], notes: ["The channel list changed; search again."] };
        S.view = "search";
        renderMain({});
        return;
      }
      showError(err);
    });
  }

  function appendEntries(key, entries) {
    var page = S.pages[key];
    var changed = false;
    if (page) {
      var last = lastSeq(page.entries);
      entries.forEach(function (e) {
        if (last === null || typeof e.seq !== "number" || e.seq > last) { page.entries.push(e); changed = true; }
      });
    }
    if (S.all && S.all.covered[key]) {
      entries.forEach(function (e) {
        var tagged = Object.assign({}, e, { channel: key });
        S.all.entries.push(tagged);
      });
      changed = true;
    }
    return changed;
  }

  function eachSend(mid, channel, fn) {
    var lists = [];
    Object.keys(S.pages).forEach(function (key) {
      if (!channel || key === channel) lists.push(S.pages[key].entries);
    });
    if (S.all) lists.push(S.all.entries.filter(function (e) { return !channel || e.channel === channel; }));
    lists.forEach(function (list) {
      list.forEach(function (e) {
        if (e && e.kind === "send" && e.message_id === mid && e.delivery && typeof e.delivery === "object") fn(e);
      });
    });
  }

  function findRow(delivery, recipient) {
    var rows = Array.isArray(delivery.recipients) ? delivery.recipients : (delivery.recipients = []);
    for (var i = 0; i < rows.length; i++) if (rows[i].recipient === recipient) return rows[i];
    return null;
  }

  function applyUpdate(u) {
    if (!u || typeof u !== "object" || typeof u.message_id !== "string") return false;
    var hit = false;
    eachSend(u.message_id, u.delivery ? u.channel : null, function (entry) {
      var d = entry.delivery;
      if (u.delivery && typeof u.delivery === "object") {
        if (u.delivery.state) d.state = u.delivery.state;
        (Array.isArray(u.delivery.recipients) ? u.delivery.recipients : []).forEach(function (row) {
          var mine = findRow(d, row.recipient);
          if (!mine) d.recipients.push({ recipient: row.recipient, state: row.state });
          else if (mine.state !== "acked" && str(mine.state).indexOf("refused: ") !== 0) mine.state = row.state;
        });
      } else if (typeof u.recipient === "string") {
        var r = findRow(d, u.recipient);
        if (r && str(r.state).indexOf("refused: ") !== 0) {
          if (u.ack) { r.ack = u.ack; if (u.state) r.state = u.state; }
          if (u.transport_error) r.transport_error = u.transport_error;
        }
      } else if (u.ack) {
        d.recipients.forEach(function (row) { if ("ack" in row && row.ack !== "acked" && str(row.state).indexOf("refused: ") !== 0) row.ack = u.ack; });
        d.partial = true;
      }
      if (entry.tally) entry.tally = tallyOf(d.recipients);
      hit = true;
    });
    return hit;
  }

  function applyChanges(data) {
    if (!data || typeof data !== "object") return Promise.resolve(false);
    var changed = false;
    var reloads = [];
    var chans = data.channels && typeof data.channels === "object" ? data.channels : {};
    Object.keys(chans).forEach(function (key) {
      var c = chans[key] || {};
      if (c.new) { S.needState = true; return; }
      if (c.gap) { reloads.push([key, c.gap]); return; }
      if (Array.isArray(c.entries) && c.entries.length && appendEntries(key, c.entries)) changed = true;
    });
    (Array.isArray(data.updates) ? data.updates : []).forEach(function (u) { if (applyUpdate(u)) changed = true; });
    if (typeof data.cursor === "string" && data.cursor) S.cursor = data.cursor;
    S.untracked = typeof data.untracked === "number" ? data.untracked : 0;
    S.journalGap = !!data.journal_gap;
    S.live = { mode: "live", partial: !!data.partial };
    var waits = reloads.map(function (pair) {
      var key = pair[0], text = gapNote(pair[1]);
      if (S.pages[key] || key === S.selected) {
        return loadLatest(key, text).then(function () { changed = true; });
      }
      S.pendingNotes[key] = text;
      return Promise.resolve();
    });
    return Promise.all(waits).then(function () {
      renderLive();
      renderRailNotes();
      if (changed) renderMain({});
      return changed;
    });
  }

  function pollOnce() {
    if (S.polling || S.stopped) return Promise.resolve(false);
    if (!S.cursor) {
      // The first state read failed: retry it (it supplies the cursor) before polling.
      S.polling = true;
      return refreshState().then(function () { return loadLatest(S.selected); }).then(function () {
        S.failures = 0;
        S.live = { mode: "live" };
        renderLive();
        renderMain({ toBottom: true });
        return true;
      }, function (err) {
        S.failures += 1;
        if (err && (err.status === 401 || err.status === 403)) S.stopped = true;
        S.live = S.stopped ? { mode: "stopped" } : { mode: "retry", delay: delay() };
        renderLive();
        return false;
      }).then(function (result) { S.polling = false; return result; });
    }
    S.polling = true;
    return api("/api/changes?since=" + encodeURIComponent(S.cursor)).then(function (data) {
      S.failures = 0;
      S.polls += 1;
      return applyChanges(data).then(function (changed) {
        if (S.needState || S.polls % STATE_EVERY === 0) {
          S.needState = false;
          return refreshState().then(function () { return changed; });
        }
        return changed;
      });
    }).catch(function (err) {
      S.failures += 1;
      if (err && (err.status === 401 || err.status === 403)) {
        S.stopped = true;
        S.live = { mode: "stopped" };
      } else if (err && err.status === 400) {
        // The cursor no longer decodes: start again from a fresh state.
        S.cursor = null;
        S.pages = {};
        S.live = { mode: "retry", delay: delay() };
        return refreshState().then(function () { return loadLatest(S.selected); }).then(function () { renderMain({ toBottom: true }); }, function () {});
      } else {
        S.live = { mode: "retry", delay: delay() };
      }
      renderLive();
      return false;
    }).then(function (result) {
      S.polling = false;
      return result;
    });
  }

  function delay() {
    if (!S.failures) return POLL_MS;
    return Math.min(BACKOFF_MAX_MS, POLL_MS * Math.pow(2, S.failures));
  }

  function schedule() {
    if (S.stopped || typeof root.setTimeout !== "function") return;
    S.timer = root.setTimeout(function () {
      pollOnce().then(schedule, schedule);
    }, delay());
  }

  // ------------------------------------------------------------------------
  // Rendering into the shell
  // ------------------------------------------------------------------------

  function isPhone() {
    return !!(root.matchMedia && root.matchMedia("(max-width: 720px)").matches);
  }

  function setPane(pane) {
    S.pane = pane;
    var app = $("app");
    if (app && app.classList) {
      ["rail", "main", "info"].forEach(function (p) { app.classList.remove("pane-" + p); });
      app.classList.add("pane-" + pane);
    }
    renderMobTabs();
  }

  function withFocus(fn) {
    var active = document.activeElement;
    var key = active && active.getAttribute ? active.getAttribute("data-focus") : null;
    fn();
    if (key && S.focusables[key] && S.focusables[key].focus) S.focusables[key].focus();
  }

  function renderMobTabs() {
    var host = $("mobtabs");
    if (!host) return;
    withFocus(function () {
      fill(host, [["rail", "Channels"], ["main", "Chat"], ["info", "Details"]].map(function (pair) {
        var b = el("button", { type: "button", role: "tab", "aria-selected": S.pane === pair[0] ? "true" : "false", "data-focus": "pane:" + pair[0] }, pair[1]);
        S.focusables["pane:" + pair[0]] = b;
        return on(b, "click", function () { setPane(pair[0]); });
      }));
    });
  }

  function renderViewTabs() {
    var host = $("viewtabs");
    if (!host) return;
    withFocus(function () {
      fill(host, [["conv", "Conversation"], ["all", "All channels"]].map(function (pair) {
        var selected = S.view === pair[0];
        var b = el("button", { type: "button", role: "tab", "aria-selected": selected ? "true" : "false", "data-focus": "view:" + pair[0] }, pair[1]);
        S.focusables["view:" + pair[0]] = b;
        return on(b, "click", function () { if (pair[0] === "all") showAll(); else selectChannel(S.selected); });
      }));
    });
  }

  function renderFleet() {
    var host = $("fleet");
    var st = S.state || {};
    var sessions = Array.isArray(st.sessions) ? st.sessions : [];
    var rows = Array.isArray(st.channels) ? st.channels : [];
    var working = sessions.filter(function (s) { return s.state === "working"; }).length;
    var missing = rows.filter(function (r) { return r.alert === "missing_acks"; }).length;
    fill(host, [
      el("span", null, el("b", null, String(sessions.length)), " sessions"),
      el("span", null, el("b", null, String(working)), " working"),
      el("span", null, el("b", null, String(rows.length)), " channels"),
      el("span", { title: "Channels whose latest " + ALERT_WINDOW + " entries hold an @all post with missing ACKs" }, el("b", null, String(missing)), " missing ACKs")
    ]);
    fill($("lanecount"), String(rows.length));
  }

  function renderLegend() {
    fill($("legend"), [["claude"], ["codex"], ["oc"]].map(function (r) {
      return el("span", null, hicon(r[0]), RT_LABEL[r[0]]);
    }).concat([
      el("span", null, el("i", { class: "dot busy", "aria-hidden": "true" }), "Working"),
      el("span", null, el("i", { class: "dot", "aria-hidden": "true" }), "Idle"),
      el("span", null, el("i", { class: "dot unv", "aria-hidden": "true" }), "Unverified"),
      el("span", null, el("i", { class: "dot gone", "aria-hidden": "true" }), "Gone")
    ]));
  }

  function renderRailNotes() {
    var out = [];
    var st = S.state || {};
    if (S.untracked) out.push(note(S.untracked + " channel" + (S.untracked === 1 ? " is" : "s are") + " not live-tracked; reload the page to see " + (S.untracked === 1 ? "it" : "them") + "."));
    if (st.partial) out.push(note("Some channel data lay beyond the per-request read budget and shows as unverified."));
    if (st.discovery && st.discovery.stale) out.push(note("Session discovery is stale."));
    if (st.discovery && st.discovery.pending) out.push(note("Session discovery is refreshing."));
    if (S.journalGap) out.push(note("Some ACK changes could not be read; ACK state may lag until the next reload."));
    fill($("railnotes"), out);
  }

  function renderLive() {
    var host = $("live");
    var live = S.live || {};
    var text = live.mode === "live" ? (live.partial ? "Live (partial)" : "Live")
      : live.mode === "retry" ? "Reconnecting in " + Math.round((live.delay || POLL_MS) / 1000) + " s"
        : live.mode === "stopped" ? "Stopped: the token was refused. Reopen the URL peers.py web printed."
          : "Connecting";
    fill(host, [el("i", { class: "livedot m-" + str(live.mode), "aria-hidden": "true" }), text]);
  }

  function renderChrome() {
    S.focusables = {};
    withFocus(function () {
      fill($("lanes"), renderRail(S.state, S.view === "conv" ? S.selected : null, selectChannel));
    });
    renderFleet();
    renderLegend();
    renderRailNotes();
    renderViewTabs();
    renderMobTabs();
    renderLive();
    fill($("info"), renderDetails(S.rows[S.selected]));
  }

  function moreButton(label, handler) {
    return on(el("button", { class: "older", type: "button" }, label), "click", handler);
  }

  function renderMain(opts) {
    opts = opts || {};
    var stream = $("stream");
    var stick = stream && typeof stream.scrollHeight === "number" ? stream.scrollHeight - stream.scrollTop - (stream.clientHeight || 0) < 48 : true;
    var prevTop = stream ? stream.scrollTop : 0;
    var title, sub, cover = null, notes = [], body = [];
    if (S.view === "all") {
      var all = S.all;
      title = "All channels";
      sub = all ? "Showing " + str(all.shown) + " of " + str(all.total) + " channels" : "Loading…";
      cover = renderCoverage("all");
      if (all) {
        (all.notes || []).forEach(function (t) { notes.push(note(t, "warn")); });
        if (all.trimmed) notes.push(note(all.trimmed + " older entries of these channels are left out (at most " + PAGE_LIMIT + " per batch, 25 per channel)."));
        if (all.partial && !all.next) notes.push(note("Partial: some reads were cut short by the per-request budget."));
        if (all.next) notes.push(el("div", { class: "note row" }, "Showing " + str(all.shown) + " of " + str(all.total) + " channels; " + str(all.omitted) + " more not loaded.",
          moreButton("Load more", function () { loadAll(S.all.next); })));
        body = renderEntries(all.entries, { channel: true });
        if (!body.length) body = [el("div", { class: "empty" }, "No entries in the channels shown.")];
      }
    } else if (S.view === "search" && S.search) {
      var sr = S.search;
      title = "Search";
      sub = "“" + sr.q + "” in " + (sr.scope === "all" ? "all channels" : chanText(sr.scope));
      if (sr.scope === "all") cover = renderCoverage("all");
      (sr.notes || []).forEach(function (t) { notes.push(note(t, "warn")); });
      if (sr.total !== undefined) notes.push(note("Searched " + str(sr.searched) + " of " + str(sr.total) + " channels; " + sr.matches.length + " match" + (sr.matches.length === 1 ? "" : "es") + ", newest first."));
      if (sr.partial) notes.push(note("Partial: each channel's newest " + str(sr.window || PAGE_LIMIT) + " entries are searched, and some reads or matches were cut short."));
      if (sr.next) notes.push(el("div", { class: "note row" }, "More channels to search.", moreButton("Load more", function () { runSearch(sr.q, sr.scope, sr.next); })));
      notes.push(el("div", { class: "note row" }, moreButton("Close search", function () { closeSearch(); })));
      body = sr.matches.map(function (m) { return renderEntry(m, { channel: true, search: true }); });
      if (!body.length && sr.total !== undefined) body = [el("div", { class: "empty" }, "No matches.")];
    } else {
      var key = S.selected;
      var row = S.rows[key];
      var page = S.pages[key];
      title = chanText(key);
      var count = row && Array.isArray(row.members) ? row.members.length : 0;
      sub = key === "general" ? count + " sessions, everyone" : count + " member" + (count === 1 ? "" : "s");
      var covRow = row ? { coverage: Object.assign({}, row.coverage || {}) } : { coverage: {} };
      if (page && page.coverage && typeof page.coverage.native_claude === "boolean") covRow.coverage.native_claude = page.coverage.native_claude;
      cover = renderCoverage("channel", covRow);
      notes = renderPageNotes(page);
      body = page ? renderChannelPage(page, { onOlder: function () { loadOlder(key); } }) : [el("div", { class: "empty" }, "Loading…")];
    }
    fill($("title"), title);
    fill($("sub"), sub);
    fill($("coverage"), cover);
    fill($("notes"), notes);
    fill(stream, body);
    if (!stream) return;
    if (opts.toTop) stream.scrollTop = 0;
    else if (typeof opts.keepFromBottom === "number") stream.scrollTop = stream.scrollHeight - opts.keepFromBottom;
    else if (opts.toBottom || stick) stream.scrollTop = stream.scrollHeight;
    else stream.scrollTop = prevTop;
  }

  function closeSearch() {
    S.search = null;
    var q = $("q");
    if (q) q.value = "";
    return selectChannel(S.selected);
  }

  function showError(err) {
    var status = err && err.status;
    if (status === 401 || status === 403) {
      S.stopped = true;
      S.live = { mode: "stopped" };
      renderLive();
    }
    fill($("notes"), note(status ? "The server refused the request (HTTP " + status + ")." : "The server could not be reached.", "bad"));
  }

  function showFatal(text) {
    fill($("title"), "Session peers");
    fill($("sub"), "no access token");
    fill($("stream"), el("div", { class: "fatal", role: "alert" },
      el("h2", null, "This page needs the URL peers.py web printed"), el("p", null, text)));
    S.live = { mode: "stopped" };
    fill($("live"), "Not connected");
  }

  // ------------------------------------------------------------------------
  // Theme (the only stored value)
  // ------------------------------------------------------------------------

  function storedTheme() {
    try { return root.localStorage ? root.localStorage.getItem(THEME_KEY) : null; } catch (e) { return null; }
  }

  function applyTheme(theme) {
    var html = document.documentElement;
    if (!html || !html.classList) return;
    html.classList.remove("theme-dark");
    html.classList.remove("theme-light");
    if (theme === "dark" || theme === "light") html.classList.add("theme-" + theme);
  }

  function toggleTheme() {
    var html = document.documentElement;
    var dark = html.classList.contains("theme-dark") ||
      (!html.classList.contains("theme-light") && !!(root.matchMedia && root.matchMedia("(prefers-color-scheme: dark)").matches));
    var next = dark ? "light" : "dark";
    applyTheme(next);
    try { if (root.localStorage) root.localStorage.setItem(THEME_KEY, next); } catch (e) { /* theme stays for this tab */ }
  }

  // ------------------------------------------------------------------------
  // Boot
  // ------------------------------------------------------------------------

  function wire() {
    on($("theme"), "click", toggleTheme);
    var q = $("q");
    on(q, "keydown", function (event) {
      if (event.key !== "Enter") return;
      if (event.preventDefault) event.preventDefault();
      var text = str(q.value).trim().slice(0, QUERY_MAX);
      if (!text) { closeSearch(); return; }
      var scopeSel = $("scope");
      var scope = scopeSel && scopeSel.value === "channel" ? S.selected : "all";
      runSearch(text, scope, null);
    });
    on(q, "search", function () { if (!str(q.value).trim() && S.view === "search") closeSearch(); });
  }

  function start() {
    applyTheme(storedTheme());
    S.token = readToken();
    renderLegend();
    if (!S.token) {
      showFatal("Open the address peers.py web printed: it ends in #token=…. The token lives only in this tab's memory, so a reload or a copied link without it cannot read any data.");
      return Promise.resolve(false);
    }
    wire();
    setPane(S.pane);
    renderLive();
    return refreshState().then(function () {
      return selectChannel(S.selected);
    }).then(function () {
      schedule();
      return true;
    }, function (err) {
      showError(err);
      if (!S.stopped) schedule();
      return false;
    });
  }

  root.PeersApp = {
    el: el, iconSvg: iconSvg, renderEntry: renderEntry, renderEntries: renderEntries, renderChannelPage: renderChannelPage,
    renderPageNotes: renderPageNotes, renderDelivery: renderDelivery, renderTally: renderTally, renderCoverage: renderCoverage,
    renderRail: renderRail, renderDetails: renderDetails, renderMain: renderMain, applyChanges: applyChanges,
    applyUpdate: applyUpdate, pollOnce: pollOnce, setState: setState, selectChannel: selectChannel, showAll: showAll,
    loadAll: loadAll, runSearch: runSearch, tallyOf: tallyOf, copyable: copyable, gapNote: gapNote, start: start,
    toggleTheme: toggleTheme, state: S, THEME_KEY: THEME_KEY
  };

  if (typeof root.location === "object" && root.location && typeof document === "object" && document && document.getElementById) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", function () { root.PeersApp.ready = start(); });
    } else {
      root.PeersApp.ready = start();
    }
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
