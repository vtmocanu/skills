// Rendering safety and behaviour of app.js, loaded into node:vm with a minimal stub DOM.
// Run: node --test skills/session-peers/scripts/session_peers/web/render.test.mjs
import { test } from "node:test"
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"
import { dirname, join } from "node:path"
import vm from "node:vm"

const here = dirname(fileURLToPath(import.meta.url))
const SOURCE = readFileSync(join(here, "app.js"), "utf8")

const CC_A = "cc:3b0e0000-0000-4000-8000-0000000041c2"
const CODEX_B = "codex:01a15f3c-0000-4000-8000-00000000000b"
const OC_C = "oc:ses_e5fb56ccAAAA"
const DISK4 = "cc:94d00000-0000-4000-8000-000000007a11"
const EVIL = "<img src=x onerror=alert(1)>\u0001\u0007\u001b[31m\u202E\r\nnext"

// ---------------------------------------------------------------- stub DOM

class StubText {
  constructor(text) { this.nodeType = 3; this.data = String(text); this.parentNode = null }
  get textContent() { return this.data }
}

class StubElement {
  constructor(doc, tag, ns) {
    this.ownerDocument = doc
    this.nodeType = 1
    this.tagName = String(tag).toLowerCase()
    this.namespaceURI = ns || "http://www.w3.org/1999/xhtml"
    this.childNodes = []
    this.attributes = {}
    this.listeners = {}
    this.parentNode = null
    this.scrollTop = 0
    this.scrollHeight = 0
    this.clientHeight = 0
    this.value = ""
    const classes = new Set()
    this.classList = {
      add: (c) => classes.add(c), remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c), toggle: (c) => (classes.has(c) ? (classes.delete(c), false) : (classes.add(c), true)),
      values: () => [...classes],
    }
    doc.created.push(this)
  }
  get firstChild() { return this.childNodes[0] || null }
  appendChild(child) {
    if (!child || (child.nodeType !== 1 && child.nodeType !== 3)) throw new Error("appendChild: not a node")
    if (child.parentNode) child.parentNode.removeChild(child)
    child.parentNode = this
    this.childNodes.push(child)
    return child
  }
  removeChild(child) {
    const i = this.childNodes.indexOf(child)
    if (i >= 0) this.childNodes.splice(i, 1)
    child.parentNode = null
    return child
  }
  setAttribute(name, value) {
    this.ownerDocument.setAttributeCalls.push([this.tagName, name, value])
    // A freshly built dialog panel is as if scrolled, so "opens at its top" has something to undo.
    if (name === "role" && value === "dialog") this.scrollTop = 50
    this.attributes[name] = String(value)
  }
  removeAttribute(name) { delete this.attributes[name] }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn) }
  dispatch(type, event = {}) { for (const fn of this.listeners[type] || []) fn(Object.assign({ type, preventDefault() {} }, event)) }
  focus(options) { this.focusOptions = options; this.ownerDocument.activeElement = this }
  get textContent() { return this.childNodes.map((c) => c.textContent).join("") }
  set textContent(value) {
    for (const c of this.childNodes) c.parentNode = null
    this.childNodes = []
    if (value !== "" && value !== null && value !== undefined) this.appendChild(new StubText(value))
  }
  set innerHTML(_v) { this.ownerDocument.htmlWrites.push("innerHTML"); throw new Error("innerHTML used") }
  get innerHTML() { throw new Error("innerHTML read") }
  set outerHTML(_v) { this.ownerDocument.htmlWrites.push("outerHTML"); throw new Error("outerHTML used") }
  insertAdjacentHTML() { this.ownerDocument.htmlWrites.push("insertAdjacentHTML"); throw new Error("insertAdjacentHTML used") }
}

function makeDocument() {
  const doc = {
    created: [], setAttributeCalls: [], htmlWrites: [], byId: {}, activeElement: null, readyState: "complete",
    createElement(tag) { return new StubElement(doc, tag) },
    createElementNS(ns, tag) { return new StubElement(doc, tag, ns) },
    createTextNode(text) { return new StubText(text) },
    getElementById(id) { return doc.byId[id] || (doc.byId[id] = new StubElement(doc, "div")) },
    addEventListener() {},
    write() { doc.htmlWrites.push("document.write"); throw new Error("document.write used") },
  }
  doc.documentElement = new StubElement(doc, "html")
  return doc
}

function walk(node, fn) {
  fn(node)
  for (const c of node.childNodes || []) walk(c, fn)
}

function elementsOf(node) {
  const out = []
  walk(node, (n) => { if (n.nodeType === 1) out.push(n) })
  return out
}

function textOf(nodes) {
  return [].concat(nodes).filter(Boolean).map((n) => n.textContent).join("\n")
}

function makeStorage(log) {
  const data = {}
  return {
    getItem(k) { log.push(["get", k]); return Object.prototype.hasOwnProperty.call(data, k) ? data[k] : null },
    setItem(k, v) { log.push(["set", k, v]); data[k] = String(v) },
    removeItem(k) { log.push(["remove", k]); delete data[k] },
  }
}

// A stub fetch answering from a route table: { "<path prefix>": body | (url) => body | {status} }.
function makeFetch(routes, calls) {
  return async (url, init) => {
    calls.push({ url, init })
    const key = Object.keys(routes).filter((k) => url.startsWith(k)).sort((a, b) => b.length - a.length)[0]
    let body = key === undefined ? { __status: 404 } : routes[key]
    if (typeof body === "function") body = await body(url)
    const status = body && body.__status ? body.__status : 200
    return { ok: status >= 200 && status < 300, status, json: async () => (status === 200 ? structuredClone(body) : {}) }
  }
}

function load({ location, fetch, storageLog, media } = {}) {
  const document = makeDocument()
  const timers = []
  const log = storageLog || []
  const historyCalls = []
  const context = {
    document, console,
    setTimeout: (fn, ms) => { if (typeof fn !== "function") throw new Error("string timer"); timers.push({ fn, ms }); return timers.length },
    clearTimeout() {},
    localStorage: makeStorage(log),
    sessionStorage: makeStorage(log),
    structuredClone,
    windowListeners: {},
    addEventListener(type, fn) { (context.windowListeners[type] = context.windowListeners[type] || []).push(fn) },
    removeEventListener() {},
  }
  if (media) context.matchMedia = (q) => ({ matches: media(q) })
  if (location) {
    context.location = location
    context.history = { replaceState: (...args) => { historyCalls.push(args); location.hash = "" } }
  }
  if (fetch) context.fetch = fetch
  context.globalThis = context
  vm.createContext(context)
  vm.runInContext(SOURCE, context, { filename: "app.js" })
  return { app: context.PeersApp, document, timers, context, storageLog: log, historyCalls }
}

// ---------------------------------------------------------------- fixtures (webdata output shapes)

const row = (recipient, state, extra = {}) => Object.assign({ recipient, state }, extra)

function sendEntry(seq, body, extra = {}) {
  return Object.assign({
    seq, ts: "2026-11-07T12:41:00+00:00", kind: "send", event: "channel.send", actor: CC_A, body,
    sender: CC_A, message_id: "m" + seq, journaled: true, verdict: null,
    delivery: { state: "complete", recipients: [row(CODEX_B, "transport accepted", { ack: "pending" })] },
  }, extra)
}

function stateFixture() {
  return {
    sessions: [
      { member: CC_A, address: CC_A, runtime: "Claude Code", liveness: "live", state: "working" },
      { member: CODEX_B, address: CODEX_B, runtime: "Codex", liveness: "live", state: "idle",
        reply_budget: { requester: CC_A, used: 7, cap: 100, remaining: 93, expired: false, pending: false, pending_kinds: [], label: "as last saved by the shim" } },
      { member: OC_C, address: OC_C, runtime: "OpenCode", liveness: "gone", state: "gone" },
    ],
    channels: [
      { key: "general", name: "general", display: "#general", kind: "general", owner: null,
        members: [{ typed: CC_A, runtime: "Claude Code", state: "working", name: "landish8" }, { typed: CODEX_B, runtime: "Codex", state: "idle" }],
        handover_chain: [], alert: "missing_acks",
        coverage: { native_claude: true, opencode: { enumerated: false, error: "boom" }, discovery_incomplete: ["Codex: the thread state could not be read"] } },
      { key: "ops", name: "ops", display: "ops", kind: "named", owner: CC_A,
        members: [{ typed: CC_A, runtime: "Claude Code", state: "working", name: "landish8" }, { typed: CODEX_B, runtime: "Codex", state: "idle" }],
        handover_chain: [], alert: null, coverage: { native_claude: true } },
    ],
    cursor: "CURSOR0", partial: false, discovery: { stale: false, pending: false },
  }
}

function pageFixture(key, entries, extra = {}) {
  return Object.assign({ channel: key, info: { key }, entries, next: null, partial: false, gap: null, oversized: 0,
    coverage: { native_claude: true }, journal_partial: false, bytes: 10 }, extra)
}

// ---------------------------------------------------------------- tests

test("loaded without location or fetch, app.js does not start and exposes one global", () => {
  const { app, timers, document } = load()
  assert.equal(typeof app, "object")
  for (const name of ["el", "renderEntry", "renderChannelPage", "applyChanges", "start"]) assert.equal(typeof app[name], "function", name)
  assert.equal(app.ready, undefined)
  assert.equal(timers.length, 0)
  assert.equal(document.created.length, 1, "only the stub's own <html>")
})

test("hostile body, actor, member and channel strings end up only as text", () => {
  const { app, document } = load()
  const state = stateFixture()
  state.channels[1].display = EVIL
  state.channels[1].members[1].name = EVIL
  app.setState(state)
  const entries = [
    sendEntry(1, EVIL, { actor: EVIL, sender: EVIL, delivery: { state: EVIL, recipients: [row(EVIL, EVIL, { ack: EVIL, transport_error: EVIL })] } }),
    sendEntry(2, "APPROVE " + EVIL, { verdict: { verdict: "approve", label: "APPROVE", text: EVIL, sha: EVIL } }),
    { seq: 3, ts: EVIL, kind: "left", event: "channel.left", actor: EVIL, text: EVIL + " left", member: EVIL },
    { seq: 4, ts: null, kind: "other", event: EVIL, actor: EVIL, text: EVIL },
    { seq: 5, kind: "oversized", oversized: true },
  ]
  const nodes = []
  nodes.push(...app.renderChannelPage(pageFixture("ops", entries)))
  nodes.push(...app.renderEntries(entries.map((e) => Object.assign({}, e, { channel: "ops" })), { channel: true }))
  nodes.push(...app.renderRail(app.state.state, "ops", () => {}))
  nodes.push(...app.renderDetails(app.state.rows.ops))
  nodes.push(app.renderCoverage("channel", { coverage: { native_claude: true, opencode: { enumerated: false, error: EVIL }, discovery_incomplete: [EVIL] } }))
  const all = nodes.flatMap(elementsOf)
  assert.ok(all.length > 20)
  for (const node of all) {
    assert.notEqual(node.tagName, "img")
    assert.notEqual(node.tagName, "script")
    for (const name of Object.keys(node.attributes)) {
      assert.ok(!/^on/i.test(name), "event attribute " + name)
      assert.ok(!["src", "href", "style", "srcdoc", "xlink:href"].includes(name), name)
    }
  }
  assert.deepEqual(document.htmlWrites, [])
  const text = textOf(nodes)
  assert.ok(text.includes("<img src=x onerror=alert(1)>"), "the hostile markup is shown as text")
  assert.ok(text.includes("\u202E"))
  assert.ok(text.includes("\r\nnext"))
  // Hostile text only ever appears in text nodes or in the title attribute (never parsed).
  for (const [, name, value] of document.setAttributeCalls) {
    if (String(value).includes("<img")) assert.equal(name, "title")
  }
})

test("el() refuses event handlers, URLs, inline style and unknown tags", () => {
  const { app } = load()
  for (const name of ["onclick", "onerror", "href", "src", "style", "srcdoc", "xlink:href", "id", "action"]) {
    assert.throws(() => app.el("div", { [name]: "x" }), /not allowed/, name)
    assert.throws(() => app.el("svg:path", { [name]: "x" }), /not allowed/, "svg " + name)
  }
  for (const tag of ["img", "script", "iframe", "a", "style", "svg:image", "svg:script", "svg:foreignObject"]) {
    assert.throws(() => app.el(tag), /not allowed/, tag)
  }
  assert.throws(() => app.el("div", { d: "M0 0" }), /not allowed/, "svg attributes only on svg elements")
  const node = app.el("button", { class: "x", type: "button", "aria-label": "y", "data-seq": 3, title: "<b>" }, "<b>bold</b>", 3, null, ["a"])
  assert.equal(node.textContent, "<b>bold</b>3a")
  assert.ok(node.childNodes.every((c) => c.nodeType === 3))
  const svg = app.el("svg:svg", { viewBox: "0 0 16 16", "aria-hidden": "true" }, app.el("svg:path", { d: "M0 0", fill: "none" }))
  assert.equal(svg.namespaceURI, "http://www.w3.org/2000/svg")
})

test("runtime icons are SVG built through el()", () => {
  const { app } = load()
  for (const rt of ["claude", "codex", "oc"]) {
    const svg = app.iconSvg(rt)
    assert.equal(svg.tagName, "svg")
    assert.equal(svg.namespaceURI, "http://www.w3.org/2000/svg")
    assert.equal(svg.childNodes[0].tagName, "path")
    assert.ok(svg.childNodes[0].attributes.d.length > 10)
  }
})

test("verdict bands render verdict, text and pinned SHA", () => {
  const { app } = load()
  app.setState(stateFixture())
  const cases = [
    ["approve", "APPROVE", "Fix range plus callers.", "d9f2736d"],
    ["revise", "REVISE: 2", "Cancel path drops the lease.", "7c2b6bac"],
    ["block", "BLOCK", "Data loss on retry.", null],
  ]
  for (const [verdict, label, text, sha] of cases) {
    const node = app.renderEntry(sendEntry(9, label + " " + text, { verdict: { verdict, label, text, sha } }))
    const band = elementsOf(node).find((n) => (n.attributes.class || "").startsWith("verdict "))
    assert.ok(band, verdict)
    assert.equal(band.attributes.class, "verdict " + verdict)
    assert.ok(band.textContent.startsWith(label))
    assert.ok(band.textContent.includes(text))
    if (sha) assert.ok(band.textContent.includes("Pinned " + sha))
    else assert.ok(!band.textContent.includes("Pinned"))
  }
})

test("delivery rows use #160 outcome names, ACK state and per-recipient transport errors", () => {
  const { app } = load()
  app.setState(stateFixture())
  const entry = sendEntry(4, "deploy", {
    delivery: {
      state: "complete", partial: true, recipients: [
        row(CC_A, "acked", { ack: "acked" }),
        row(CODEX_B, "queued (thread paused)", { ack: "pending", transport_error: "socket closed" }),
        row(OC_C, "refused: no journal slot", { ack: "not requested (unverified)" }),
        row("codex:00000000-0000-4000-8000-0000000000dd", "not woken (OpenCode idle)", { ack: "unverified" }),
        row("cc:00000000-0000-4000-8000-0000000000ee", "skipped: gone", { ack: "not requested" }),
      ],
    },
  })
  const node = app.renderEntry(entry, { channelKey: "ops" })
  const text = node.textContent
  for (const want of ["Delivery: complete", "acked", "queued (thread paused)", "ACK pending", "transport error: socket closed",
    "refused: no journal slot", "ACK not requested (unverified)", "not woken (OpenCode idle)", "ACK unverified", "skipped: gone", "ACK not requested", "partial"]) {
    assert.ok(text.includes(want), want)
  }
  const rows = elementsOf(node).filter((n) => (n.attributes.class || "").startsWith("drow "))
  assert.equal(rows.length, 5)
  assert.equal(rows[0].attributes.class, "drow t-ok")
  assert.equal(rows[1].attributes.class, "drow t-bad", "a transport error reads as a failure")
  assert.equal(rows[2].attributes.class, "drow t-bad")
  assert.ok(!text.includes("Delivered") && !text.includes("In turn"), "no four-step ladder")
  for (const state of ["recorded", "complete", "in progress", "unknown", "unverified"]) {
    const n = app.renderEntry(sendEntry(5, "x", { delivery: { state, recipients: [] } }))
    assert.ok(n.textContent.includes("Delivery: " + state), state)
  }
  const accepted = app.renderDelivery({ state: "complete", recipients: [row(CODEX_B, "transport accepted")] })
  assert.equal(elementsOf(accepted).find((n) => (n.attributes.class || "").startsWith("drow ")).attributes.class, "drow t-sent",
    "transport accepted is never shown as acknowledged")
})

test("@all broadcasts show an ACK tally; refused recipients stay out of the denominator", () => {
  const { app } = load()
  app.setState(stateFixture())
  const recipients = [row(CC_A, "acked", { ack: "acked" }), row(CODEX_B, "transport accepted", { ack: "pending" }),
    row(OC_C, "refused: x", { ack: "not requested (unverified)" })]
  const entry = sendEntry(6, "@all merge freeze", { delivery: { state: "complete", recipients }, tally: { acked: 1, total: 2, missing: [CODEX_B], unverified: 0 } })
  const node = app.renderEntry(entry, { channelKey: "general" })
  assert.ok(node.textContent.includes("1 of 2 acknowledged"))
  assert.ok(node.textContent.includes("to @all, 3 recipients"))
  const chips = elementsOf(node).filter((n) => n.parentNode && (n.parentNode.attributes.class || "") === "tally")
  assert.deepEqual(chips.map((c) => c.attributes.class), ["yes", "no"])
  assert.equal(elementsOf(node).find((n) => n.tagName === "b").textContent, "@all")
  assert.deepEqual(JSON.parse(JSON.stringify(app.tallyOf(recipients))), { acked: 1, total: 2, missing: [CODEX_B], unverified: 0 })
})

test("a gone recipient (ACK never requested) stays out of the tally and its chips", () => {
  const { app } = load()
  app.setState(stateFixture())
  const recipients = [row(CC_A, "acked", { ack: "acked" }), row(OC_C, "skipped: gone", { ack: "not requested" })]
  assert.deepEqual(JSON.parse(JSON.stringify(app.tallyOf(recipients))), { acked: 1, total: 1, missing: [], unverified: 0 })
  const entry = sendEntry(7, "@all merge freeze", { delivery: { state: "complete", recipients } })
  const node = app.renderEntry(entry, { channelKey: "general" })
  assert.ok(node.textContent.includes("1 of 1 acknowledged"))
  const chips = elementsOf(node).filter((n) => n.parentNode && (n.parentNode.attributes.class || "") === "tally")
  assert.deepEqual(chips.map((c) => c.attributes.class), ["yes"])
})

test("membership, oversized and #general coverage entries; outcomes fold into their send", () => {
  const { app } = load()
  app.setState(stateFixture())
  const entries = [
    { seq: 1, ts: "2026-11-07T10:00:00Z", kind: "left", text: CC_A + " left", member: CC_A },
    { seq: 2, ts: "2026-11-07T10:01:00Z", kind: "removed", text: OC_C + " removed", member: OC_C },
    { seq: 3, ts: "2026-11-07T10:02:00Z", kind: "handover", text: CC_A + " handed over to " + CODEX_B, member: CC_A },
    { seq: 4, kind: "oversized", oversized: true },
    { seq: 5, ts: "2026-11-07T10:03:00Z", kind: "outcome", message_id: "m1", outcomes: {}, text: "outcomes of message m1 for 0 recipients" },
    sendEntry(6, "hello", { coverage: { opencode: { scope: "top-level sessions updated within the window", hours: 24 }, discovery_incomplete: ["Claude registry: unreadable"] } }),
  ]
  const text = textOf(app.renderChannelPage(pageFixture("general", entries)))
  assert.ok(text.includes("landish8 left"))
  assert.ok(text.includes("removed (gone)"))
  assert.ok(text.includes("landish8 handed over to codex:01a1…000b"))
  assert.ok(text.includes("Entry too large to display (#4)"))
  assert.ok(!text.includes("outcomes of message"))
  assert.ok(text.includes("OpenCode coverage: top-level sessions updated within the last 24 h"))
  assert.ok(text.includes("Discovery incomplete: Claude registry: unreadable"))
  assert.ok(text.includes("to #general"))
  const notes = textOf(app.renderPageNotes(pageFixture("ops", [], { journal_partial: true })))
  assert.ok(notes.includes("ACK state was read from the newest part of the journal only"))
  const banner = app.renderCoverage("channel", stateFixture().channels[0]).textContent
  assert.ok(banner.includes("Native Claude-to-Claude messages"))
  assert.ok(banner.includes("OpenCode was not enumerated: boom"))
  assert.ok(banner.includes("Discovery incomplete: Codex: the thread state could not be read"))
  assert.equal(app.renderCoverage("channel", { coverage: { native_claude: false } }), null)
  assert.ok(app.renderCoverage("all").textContent.includes("Native Claude-to-Claude"))
})

test("rail: #general first, member states, handover chain, alert badges, no unread counts", () => {
  const { app } = load()
  const state = stateFixture()
  state.channels.push({ key: "buddy-x", display: "buddy · disk", kind: "buddy", owner: CC_A,
    members: [{ typed: DISK4, state: "idle", name: "disk4" }, { typed: OC_C, state: "gone" }],
    handover_chain: [{ member: "cc:00000000-0000-4000-8000-000000000001", to: "cc:00000000-0000-4000-8000-000000000002", seq: 2 }, { member: "cc:00000000-0000-4000-8000-000000000002", to: DISK4, seq: 5 }],
    alert: "unverified", coverage: { native_claude: true } })
  state.channels[1].activity = "posted"  // rows without activity are unknown and sort after posted ones
  state.channels[1].last_post_ts = "2026-11-07T12:41:00+00:00"
  state.channels[2].activity = "none"
  app.setState(state)
  const lanes = app.renderRail(app.state.state, "ops", () => {})
  assert.equal(lanes.length, 3)
  assert.ok(lanes[0].textContent.startsWith("#general"))
  const badge = elementsOf(lanes[0]).find((n) => n.attributes.class === "badge alert")
  assert.equal(badge.textContent, "missing ACKs")
  assert.equal(badge.attributes.title, "Covers the latest 25 entries")
  assert.equal(lanes[1].attributes["aria-current"], "true")
  assert.ok(lanes[1].textContent.includes("landish8"))
  assert.ok(lanes[1].textContent.includes("Working"))
  assert.ok(lanes[2].textContent.includes("cc:0000…0001 → cc:0000…0002 → disk4"))
  assert.ok(lanes[2].textContent.includes("ACKs unverified"))
  assert.ok(lanes[2].textContent.includes("Gone"))
  for (const lane of lanes) assert.ok(!elementsOf(lane).some((n) => n.attributes.class === "badge" && /^\d+$/.test(n.textContent)))
})

test("details: runtime, state, typed address, copy only for strict ids, Codex budget as last saved", () => {
  const { app } = load()
  const state = stateFixture()
  state.channels[1].members.push({ typed: "codex:not-a-uuid", state: "unverified" })
  app.setState(state)
  const nodes = app.renderDetails(app.state.rows.ops)
  const text = textOf(nodes)
  assert.ok(text.includes("Claude Code"))
  assert.ok(text.includes(CC_A))
  assert.ok(text.includes("93 of 100 toward landish8"))
  assert.ok(text.includes("as last saved by the shim"))
  assert.ok(text.includes("Reply budget") && text.includes("unknown"), "a Codex member without shim data reads unknown")
  const buttons = nodes.flatMap(elementsOf).filter((n) => n.tagName === "button")
  assert.equal(buttons.length, 3)
  assert.equal(buttons[0].attributes["aria-disabled"], undefined)
  assert.equal(buttons[2].attributes["aria-disabled"], "true")
  assert.ok(app.copyable(CC_A) && app.copyable(CODEX_B) && app.copyable("oc:ses_abcdefgh12"))
  for (const bad of ["cc:" + CC_A.slice(3) + "\n", "cc:3b0e", "oc:ses_x", "web:you", CC_A + " ", "codex:" + "g".repeat(36)]) assert.ok(!app.copyable(bad), bad)
  assert.ok(!text.includes("Ping") && !text.includes("shim log") && !text.includes("Open gates"))
})

function bootFixture(extraRoutes = {}, { media, hash } = {}) {
  const calls = []
  const routes = Object.assign({
    "/api/state": stateFixture(),
    "/api/channel/general": pageFixture("general", [sendEntry(1, "@all freeze", { delivery: { state: "complete", recipients: [row(CODEX_B, "transport accepted", { ack: "pending" })] }, tally: { acked: 0, total: 1, missing: [CODEX_B], unverified: 0 } })]),
    "/api/channel/ops": pageFixture("ops", [sendEntry(1, "first in ops")]),
  }, extraRoutes)
  const location = { hash: hash === undefined ? "#token=" + "ab".repeat(32) : hash, pathname: "/", search: "" }
  const env = load({ location, fetch: makeFetch(routes, calls), media })
  return Object.assign(env, { calls, routes, location })
}

test("the token travels only as X-Peers-Token, the hash is cleared, and nothing but the theme is stored", async () => {
  const env = bootFixture()
  assert.equal(await env.app.ready, true)
  assert.ok(env.calls.length >= 2)
  for (const call of env.calls) {
    assert.ok(call.url.startsWith("/api/"), call.url)
    assert.ok(!call.url.includes("abab"), "token never in a URL")
    assert.equal(call.init.headers["X-Peers-Token"], "ab".repeat(32))
    assert.equal(call.init.credentials, "omit")
    assert.equal(call.init.cache, "no-store")
    assert.equal(call.init.method, "GET")
    assert.equal(call.init.redirect, "error", "a redirect never carries the token elsewhere")
    assert.equal(call.init.referrerPolicy, "no-referrer")
  }
  assert.ok(!("token" in env.app.state), "the exported state view holds no token")
  assert.ok(!JSON.stringify(Object.keys(env.app.state)).includes("token"))
  assert.equal(env.location.hash, "")
  assert.equal(env.historyCalls.length, 1)
  assert.equal(env.historyCalls[0][2], "/")
  assert.ok(env.storageLog.every((op) => op[1] === env.app.THEME_KEY), JSON.stringify(env.storageLog))
  assert.ok(!JSON.stringify(env.storageLog).includes("abab"))
  const themeButton = () => env.document.byId.themebox.childNodes[0]
  assert.equal(themeButton().attributes["aria-pressed"], "false")
  assert.equal(themeButton().attributes["aria-label"], "Dark theme")
  themeButton().dispatch("click")
  assert.deepEqual(env.storageLog.filter((op) => op[0] === "set").map((op) => op[1]), [env.app.THEME_KEY])
  assert.ok(env.document.documentElement.classList.contains("theme-dark"))
  assert.equal(themeButton().attributes["aria-pressed"], "true", "the toggle reports its state")
  themeButton().dispatch("click")
  assert.equal(themeButton().attributes["aria-pressed"], "false")
  assert.equal(env.timers.length, 1)
  assert.equal(env.timers[0].ms, 2000)
  assert.deepEqual(env.document.htmlWrites, [])
})

test("a missing token shows a clear message and makes no request", async () => {
  const calls = []
  const env = load({ location: { hash: "", pathname: "/", search: "" }, fetch: makeFetch({}, calls) })
  assert.equal(await env.app.ready, false)
  assert.equal(calls.length, 0)
  assert.ok(env.document.byId.stream.textContent.includes("#token="))
  assert.equal(env.timers.length, 0)
})

test("polling applies new entries and ACK updates by message id and recipient", async () => {
  const env = bootFixture()
  await env.app.ready
  const stream = env.document.byId.stream
  assert.ok(stream.textContent.includes("0 of 1 acknowledged"))
  env.routes["/api/changes"] = {
    channels: { general: { entries: [sendEntry(2, "second post", { delivery: { state: "in progress", recipients: [row(CODEX_B, "in progress", { ack: "pending" })] } })], gap: null } },
    updates: [
      { message_id: "m1", recipient: CODEX_B, ack: "acked", state: "acked" },
      { channel: "general", message_id: "m2", delivery: { state: "complete", recipients: [{ recipient: CODEX_B, state: "queued (thread paused)" }] } },
      { message_id: "m2", recipient: CODEX_B, transport_error: "pipe closed" },
    ],
    cursor: "CURSOR1", partial: false,
  }
  await env.app.pollOnce()
  const changes = env.calls.filter((c) => c.url.startsWith("/api/changes"))
  assert.equal(changes[0].url, "/api/changes?since=CURSOR0")
  assert.equal(env.app.state.cursor, "CURSOR1")
  const text = stream.textContent
  assert.ok(text.includes("second post"))
  assert.ok(text.includes("1 of 1 acknowledged"))
  assert.ok(text.includes("queued (thread paused)"))
  assert.ok(text.includes("transport error: pipe closed"))
  assert.ok(text.includes("Delivery: complete"))
  env.routes["/api/changes"] = { channels: {}, updates: [], cursor: "CURSOR2", partial: false }
  await env.app.pollOnce()
  assert.equal(env.calls.filter((c) => c.url.startsWith("/api/changes"))[1].url, "/api/changes?since=CURSOR1")
})

test("a gap reloads that channel's latest page and says older entries were pruned", async () => {
  const env = bootFixture()
  await env.app.ready
  await env.app.selectChannel("ops")
  const before = env.calls.filter((c) => c.url.startsWith("/api/channel/ops")).length
  env.routes["/api/channel/ops"] = pageFixture("ops", [sendEntry(40, "after the prune")])
  env.routes["/api/changes"] = { channels: { ops: { entries: [], gap: { from: 2, to: 30, reason: "pruned" } } }, updates: [], cursor: "CURSOR9", partial: false }
  await env.app.pollOnce()
  const reloads = env.calls.filter((c) => c.url.startsWith("/api/channel/ops"))
  assert.equal(reloads.length, before + 1)
  assert.equal(reloads[reloads.length - 1].url, "/api/channel/ops?limit=200")
  assert.ok(env.document.byId.notes.textContent.includes("Older entries were pruned"))
  assert.ok(env.document.byId.stream.textContent.includes("after the prune"))
  assert.ok(!env.document.byId.stream.textContent.includes("first in ops"))
  assert.equal(env.app.state.cursor, "CURSOR9")
})

test("new channels refresh the state; every fifth poll re-reads it; errors back off", async () => {
  const env = bootFixture()
  await env.app.ready
  const stateCalls = () => env.calls.filter((c) => c.url === "/api/state").length
  const first = stateCalls()
  env.routes["/api/changes"] = { channels: { fresh: { entries: [], gap: null, new: true } }, updates: [], cursor: "C1", partial: false, untracked: 3 }
  await env.app.pollOnce()
  assert.equal(stateCalls(), first + 1)
  assert.ok(env.document.byId.railnotes.textContent.includes("3 channels are not live-tracked"))
  env.routes["/api/changes"] = { channels: {}, updates: [], cursor: "C2", partial: false }
  for (let i = 0; i < 4; i++) await env.app.pollOnce()
  assert.equal(stateCalls(), first + 2, "the fifth poll re-reads /api/state")
  env.routes["/api/changes"] = { __status: 500 }
  await env.app.pollOnce()
  assert.ok(env.document.byId.live.textContent.includes("Reconnecting in 4 s"))
  await env.app.pollOnce()
  assert.ok(env.document.byId.live.textContent.includes("Reconnecting in 8 s"))
  env.routes["/api/changes"] = { __status: 403 }
  await env.app.pollOnce()
  assert.ok(env.document.byId.live.textContent.includes("Stopped"))
  const n = env.calls.length
  await env.app.pollOnce()
  assert.equal(env.calls.length, n, "a refused token stops polling")
})

test("All channels: 'Showing N of M channels', Load more with next, entries tagged by channel", async () => {
  const env = bootFixture({
    "/api/all": (url) => url.includes("next=") ?
      { entries: [Object.assign(sendEntry(3, "later batch"), { channel: "ops", ts: "2026-11-07T13:00:00Z" })], partial: false, omitted: 0, total: 2, shown: 2, next: null, trimmed: 0 } :
      { entries: [Object.assign(sendEntry(1, "from general"), { channel: "general" })], partial: true, omitted: 1, total: 2, shown: 1, next: "TOK1", trimmed: 0 },
  })
  await env.app.ready
  await env.app.showAll()
  assert.equal(env.document.byId.sub.textContent, "Showing 1 of 2 channels")
  assert.ok(env.document.byId.notes.textContent.includes("1 more not loaded"))
  assert.ok(env.document.byId.stream.textContent.includes("in #general"))
  assert.ok(env.document.byId.coverage.textContent.includes("Native Claude-to-Claude"))
  await env.app.loadAll("TOK1")
  assert.ok(env.calls.some((c) => c.url === "/api/all?next=TOK1"))
  assert.equal(env.document.byId.sub.textContent, "Showing 2 of 2 channels")
  assert.ok(env.document.byId.stream.textContent.includes("in #ops"))
})

test("search is server-side, tagged with channel, and states its partial coverage", async () => {
  const env = bootFixture({
    "/api/search": { matches: [Object.assign(sendEntry(7, "found <b>it</b>"), { channel: "ops", delivery: undefined })], partial: true,
      searched_channels: 1, total_channels: 2, next: "S1", window: 200 },
  })
  await env.app.ready
  await env.app.runSearch("found", "all", null)
  const call = env.calls.find((c) => c.url.startsWith("/api/search"))
  assert.equal(call.url, "/api/search?q=found&scope=all")
  const text = env.document.byId.stream.textContent
  assert.ok(text.includes("found <b>it</b>"))
  assert.ok(text.includes("in #ops"))
  let notes = env.document.byId.notes.textContent
  assert.ok(notes.includes("Searched 1 of 2 channels"))
  assert.ok(notes.includes("More channels to search."))
  assert.ok(notes.includes("Load more"))
  assert.ok(!notes.includes("Partial"), "not partial while more channels remain to search")
  env.routes["/api/search"] = { matches: [], partial: true, searched_channels: 2, total_channels: 2, next: null, window: 200 }
  await env.app.runSearch("found", "all", "S1")
  notes = env.document.byId.notes.textContent
  assert.ok(notes.includes("Partial: each channel's newest 200 entries"))
})

test("a failed first state read is retried by the poll before any /api/changes call", async () => {
  const env = bootFixture({ "/api/state": { __status: 500 } })
  assert.equal(await env.app.ready, false)
  assert.equal(env.timers.length, 1, "it keeps trying")
  env.routes["/api/state"] = stateFixture()
  assert.equal(await env.app.pollOnce(), true)
  assert.equal(env.app.state.cursor, "CURSOR0")
  assert.ok(!env.calls.some((c) => c.url.startsWith("/api/changes")))
  assert.ok(env.document.byId.stream.textContent.includes("@all freeze"))
})

test("a rail lane is selectable by keyboard", async () => {
  const env = bootFixture()
  await env.app.ready
  const lane = env.document.byId.lanes.childNodes[1]
  assert.equal(lane.attributes.role, "button")
  assert.equal(lane.attributes.tabindex, "0")
  lane.dispatch("keydown", { key: "Enter" })
  await new Promise((r) => setImmediate(r))
  assert.equal(env.app.state.selected, "ops")
  assert.ok(env.calls.some((c) => c.url === "/api/channel/ops?limit=200"))
})

// ---------------------------------------------------------------- rework (#161 m4 review)

const tick = () => new Promise((r) => setImmediate(r))
async function settle() { for (let i = 0; i < 12; i++) await tick() }
const plain = (v) => JSON.parse(JSON.stringify(v))
const buttonNamed = (host, label) => elementsOf(host).find((n) => n.tagName === "button" && n.textContent === label)
const quiet = (cursor, extra = {}) => Object.assign({ channels: {}, updates: [], cursor, partial: false }, extra)

test("a settled send: recipients the update does not name take its state, or unknown when complete (as channels.delivery_rows)", async () => {
  const R2 = "codex:00000000-0000-4000-8000-0000000000d2"
  const env = bootFixture({ "/api/channel/general": pageFixture("general", [
    sendEntry(5, "a", { delivery: { state: "in progress", recipients: [row(CODEX_B, "in progress", { ack: "pending" })] } }),
    sendEntry(6, "b", { delivery: { state: "in progress", recipients: [row(CODEX_B, "in progress", { ack: "pending" }), row(R2, "in progress", { ack: "pending" }),
      row(OC_C, "refused: no journal slot", { ack: "not requested (unverified)" }), row(CC_A, "acked", { ack: "acked" })] } }),
    sendEntry(7, "c", { journaled: false, delivery: { state: "in progress", recipients: [row(CODEX_B, "in progress"), row(R2, "in progress")] } }),
  ]) })
  await env.app.ready
  env.routes["/api/changes"] = quiet("C1", { updates: [
    { channel: "general", message_id: "m5", delivery: { state: "unknown", recipients: [] } },
    { channel: "general", message_id: "m6", delivery: { state: "complete", recipients: [{ recipient: CODEX_B, state: "transport accepted" }] } },
    { channel: "general", message_id: "m7", delivery: { state: "unverified", recipients: [] } },
  ] })
  await env.app.pollOnce()
  const delivery = (seq) => plain(env.app.state.pages.general.entries.find((e) => e.seq === seq).delivery)
  assert.deepEqual(delivery(5), { state: "unknown", recipients: [row(CODEX_B, "unknown", { ack: "pending" })] })
  assert.deepEqual(delivery(6), { state: "complete", recipients: [
    row(CODEX_B, "transport accepted", { ack: "pending" }), row(R2, "unknown", { ack: "pending" }),
    row(OC_C, "refused: no journal slot", { ack: "not requested (unverified)" }), row(CC_A, "acked", { ack: "acked" })] })
  assert.deepEqual(delivery(7), { state: "unverified", recipients: [row(CODEX_B, "unverified"), row(R2, "unverified")] })
  assert.ok(!env.document.byId.stream.textContent.includes("in progress"), "no row is left in progress")
})

test("All channels never keeps 'Showing N of N' once a new channel, a covered gap or uncovered entries appear", async () => {
  const allUrls = []
  const env = bootFixture({ "/api/all": (url) => { allUrls.push(url); return { entries: [Object.assign(sendEntry(1, "from general"), { channel: "general" })], partial: false, omitted: 0, total: 1, shown: 1, next: null, trimmed: 0 } } })
  await env.app.ready
  await env.app.showAll()
  const sub = () => env.document.byId.sub.textContent
  const notes = () => env.document.byId.notes.textContent
  assert.equal(sub(), "Showing 1 of 1 channels")
  const reloadAll = async () => {
    const before = allUrls.length
    buttonNamed(env.document.byId.notes, "Reload All channels").dispatch("click")
    await settle()
    assert.equal(allUrls.length, before + 1)
    assert.equal(allUrls[allUrls.length - 1], "/api/all", "reloaded from the start")
    assert.equal(sub(), "Showing 1 of 1 channels")
    assert.ok(!notes().includes("Reload All channels"))
  }
  env.routes["/api/changes"] = quiet("C1", { channels: { fresh: { entries: [], gap: null, new: true } } })
  await env.app.pollOnce()
  assert.ok(!sub().includes("Showing 1 of 1"), sub())
  assert.ok(sub().startsWith("Out of date"))
  assert.ok(notes().includes("A new channel exists (#fresh)"))
  await reloadAll()
  env.routes["/api/changes"] = quiet("C2", { channels: { general: { entries: [], gap: { from: 1, to: 9, reason: "pruned" } } } })
  await env.app.pollOnce()
  assert.ok(notes().includes("#general has entries this view could not stream; it may be incomplete."), notes())
  await reloadAll()
  // A channel the view holds no entry of (not loaded, or in a batch with 0 entries).
  env.routes["/api/changes"] = quiet("C3", { channels: { ops: { entries: [sendEntry(9, "new in ops")], gap: null } } })
  await env.app.pollOnce()
  assert.ok(notes().includes("New entries arrived in #ops, which this view may not include."))
  await reloadAll()
  // Stale while hidden: opening the view again reloads it from the start.
  await env.app.selectChannel("general")
  env.routes["/api/changes"] = quiet("C4", { channels: { fresh2: { entries: [], gap: null, new: true } } })
  await env.app.pollOnce()
  const n = allUrls.length
  await env.app.showAll()
  assert.equal(allUrls.length, n + 1)
  assert.equal(sub(), "Showing 1 of 1 channels")
})

test("a channel named constructor is a plain key: selected, loaded, polled; member states are not prototype keys", async () => {
  const state = stateFixture()
  state.channels.push({ key: "constructor", name: "constructor", display: "constructor", kind: "named", owner: CC_A,
    members: [{ typed: CC_A, state: "constructor", name: "landish8" }], handover_chain: [], alert: null, coverage: {} })
  const env = bootFixture({ "/api/state": state, "/api/channel/constructor": pageFixture("constructor", [sendEntry(1, "hello constructor")]) })
  await env.app.ready
  await env.app.selectChannel("constructor")
  assert.ok(env.calls.some((c) => c.url === "/api/channel/constructor?limit=200"), "its page is fetched, not mistaken for Object.prototype.constructor")
  const stream = env.document.byId.stream
  assert.ok(stream.textContent.includes("hello constructor"))
  env.routes["/api/changes"] = quiet("C1", { channels: {
    constructor: { entries: [sendEntry(2, "second constructor")], gap: null },
    general: { entries: [sendEntry(2, "general news")], gap: null } } })
  await env.app.pollOnce()
  assert.equal(env.app.state.cursor, "C1")
  assert.ok(stream.textContent.includes("second constructor"))
  await env.app.selectChannel("general")
  assert.ok(stream.textContent.includes("general news"))
  const lanes = env.document.byId.lanes.textContent
  assert.ok(!lanes.includes("function") && !lanes.includes("native code"), lanes)
  assert.ok(lanes.includes("(Unverified)"))
})

test("live appends are capped: a page keeps its newest 500 (Load older reaches the rest), All channels keeps 500", async () => {
  const env = bootFixture()
  await env.app.ready
  const stream = env.document.byId.stream
  env.routes["/api/changes"] = quiet("C1", { channels: { general: { entries: Array.from({ length: 600 }, (_, i) => sendEntry(i + 2, "bulk " + (i + 2))), gap: null } } })
  await env.app.pollOnce()
  let page = env.app.state.pages.general
  assert.equal(page.entries.length, 500)
  assert.equal(page.entries[0].seq, 102)
  assert.equal(page.entries[499].seq, 601)
  assert.equal(page.next, 102, "Load older continues where the trim cut")
  env.routes["/api/channel/general?before=102"] = pageFixture("general", [sendEntry(101, "just trimmed")], { next: 1 })
  buttonNamed(stream, "Load older").dispatch("click")
  await settle()
  assert.ok(env.calls.some((c) => c.url === "/api/channel/general?before=102&limit=200"))
  assert.ok(stream.textContent.includes("just trimmed"))
  page = env.app.state.pages.general
  assert.equal(page.entries.length, 501)
  env.routes["/api/changes"] = quiet("C2", { channels: { general: { entries: [sendEntry(602, "one more")], gap: null } } })
  await env.app.pollOnce()
  assert.equal(page.entries.length, 501, "older entries the reader loaded are kept")
  env.routes["/api/all"] = { entries: [Object.assign(sendEntry(602, "g"), { channel: "general" })], partial: false, omitted: 0, total: 1, shown: 1, next: null, trimmed: 0 }
  await env.app.showAll()
  env.routes["/api/changes"] = quiet("C3", { channels: { general: { entries: Array.from({ length: 600 }, (_, i) => sendEntry(1000 + i, "all " + i)), gap: null } } })
  await env.app.pollOnce()
  const all = env.app.state.all
  assert.equal(all.entries.length, 500)
  assert.equal(all.entries[499].seq, 1599)
  assert.equal(all.trimmed, 101)
  assert.ok(env.document.byId.notes.textContent.includes("101 older entries of these channels are left out"))
})

test("query values are URL-encoded: the since= cursor and the search q", async () => {
  const odd = "a&b#c/d?e%f g"
  const state = stateFixture()
  state.cursor = odd
  const env = bootFixture({ "/api/state": state, "/api/search": { matches: [], partial: false, searched_channels: 2, total_channels: 2, next: null } })
  await env.app.ready
  env.routes["/api/changes"] = quiet("C1")
  await env.app.pollOnce()
  assert.equal(env.calls.find((c) => c.url.startsWith("/api/changes")).url, "/api/changes?since=a%26b%23c%2Fd%3Fe%25f%20g")
  await env.app.runSearch("x&scope=general#/?%", "all", null)
  assert.equal(env.calls.find((c) => c.url.startsWith("/api/search")).url, "/api/search?q=x%26scope%3Dgeneral%23%2F%3F%25&scope=all")
})

test("a malformed #token hash is refused and still cleared from the address bar", async () => {
  for (const hash of ["#token=short", "#token=" + "ab".repeat(32) + "&x=1", "#x=1&token=" + "ab".repeat(32)]) {
    const calls = []
    const location = { hash, pathname: "/p", search: "?q" }
    const env = load({ location, fetch: makeFetch({}, calls) })
    assert.equal(await env.app.ready, false, hash)
    assert.equal(calls.length, 0, hash)
    assert.equal(location.hash, "", hash)
    assert.equal(env.historyCalls.length, 1, hash)
    assert.equal(env.historyCalls[0][2], "/p?q")
  }
})

test("the no-token page starts once a valid #token= arrives", async () => {
  const env = bootFixture({}, { hash: "" })
  assert.equal(await env.app.ready, false)
  assert.equal(env.calls.length, 0)
  env.location.hash = "#token=" + "cd".repeat(32)
  for (const fn of env.context.windowListeners.hashchange || []) fn({ type: "hashchange" })
  assert.equal(await env.app.ready, true)
  assert.equal(env.calls[0].url, "/api/state")
  assert.equal(env.calls[0].init.headers["X-Peers-Token"], "cd".repeat(32))
  assert.equal(env.location.hash, "")
  assert.ok(env.document.byId.stream.textContent.includes("@all freeze"))
  // A later hash in the running page is cleared too; the token in memory stays.
  env.location.hash = "#token=" + "ef".repeat(32)
  for (const fn of env.context.windowListeners.hashchange || []) fn({ type: "hashchange" })
  assert.equal(env.location.hash, "")
  assert.equal(env.context.windowListeners.hashchange.length, 1, "one listener")
  env.routes["/api/changes"] = quiet("C1")
  await env.app.pollOnce()
  assert.equal(env.calls[env.calls.length - 1].init.headers["X-Peers-Token"], "cd".repeat(32))
})

test("a journal gap note stays until a fresh state and page load", async () => {
  const env = bootFixture()
  await env.app.ready
  const rail = () => env.document.byId.railnotes.textContent
  env.routes["/api/changes"] = quiet("C1", { journal_gap: true })
  await env.app.pollOnce()
  assert.ok(rail().includes("Some ACK changes could not be read"))
  env.routes["/api/changes"] = quiet("C2")
  await env.app.pollOnce()
  assert.ok(rail().includes("Some ACK changes could not be read"), "sticky across polls")
  env.routes["/api/changes"] = { __status: 400 }
  await env.app.pollOnce()
  assert.ok(!rail().includes("Some ACK changes could not be read"), "cleared by the fresh reload")
})

test("entries and updates polled while a channel's page is loading are applied to it, once", async () => {
  let release
  const gate = new Promise((r) => { release = r })
  const env = bootFixture({ "/api/channel/ops": async () => { await gate; return pageFixture("ops", [sendEntry(1, "first in ops"), sendEntry(2, "second in ops")]) } })
  await env.app.ready
  const picked = env.app.selectChannel("ops")
  await settle()
  const stream = env.document.byId.stream
  assert.ok(stream.textContent.includes("Loading"))
  env.routes["/api/changes"] = quiet("C1", {
    channels: { ops: { entries: [sendEntry(2, "second in ops"), sendEntry(3, "third in ops")], gap: null } },
    updates: [{ message_id: "m1", recipient: CODEX_B, ack: "acked", state: "acked" }] })
  await env.app.pollOnce()
  assert.equal(env.app.state.cursor, "C1")
  assert.equal(env.calls.filter((c) => c.url.startsWith("/api/channel/ops")).length, 1, "no second load while one is in flight")
  release()
  await picked
  const text = stream.textContent
  assert.ok(text.includes("third in ops"), "the polled entry is not lost")
  assert.equal(text.split("second in ops").length - 1, 1, "deduplicated by seq")
  const m1 = env.app.state.pages.ops.entries.find((e) => e.message_id === "m1")
  assert.equal(m1.delivery.recipients[0].ack, "acked", "the buffered ACK update reached the page")
})

test("Codex reply budget: a bare Claude session id as requester is named through its cc: form", () => {
  const { app } = load()
  const state = stateFixture()
  state.sessions[1].reply_budget.requester = CC_A.slice(3)
  app.setState(state)
  assert.ok(textOf(app.renderDetails(app.state.rows.ops)).includes("93 of 100 toward landish8"))
  state.sessions[1].reply_budget.requester = DISK4.slice(3)
  app.setState(state)
  assert.ok(textOf(app.renderDetails(app.state.rows.ops)).includes("93 of 100 toward cc:94d0…7a11"), "unknown: a shortened id")
})

test("search with more channels left and no matches yet says so", async () => {
  const env = bootFixture({ "/api/search": { matches: [], partial: true, searched_channels: 32, total_channels: 40, next: "S1", window: 200 } })
  await env.app.ready
  await env.app.runSearch("zzz", "all", null)
  const notes = env.document.byId.notes.textContent
  assert.ok(notes.includes("No matches in the first 32 channels yet — Load more to search the rest."))
  assert.ok(!notes.includes("Partial"))
  assert.ok(env.document.byId.stream.textContent.includes("No matches yet."))
})

test("focus: Load more lands on the new Load more button or the first new entry; Close search on #q; a phone pick on #title", async () => {
  const batch = (seq, ch, next) => ({ entries: [Object.assign(sendEntry(seq, "batch " + seq), { channel: ch, ts: "2026-11-07T1" + seq + ":00:00Z" })], partial: !!next, omitted: next ? 1 : 0, total: 3, shown: 3 - (next ? 1 : 0), next, trimmed: 0 })
  const env = bootFixture({
    "/api/all": (url) => url.endsWith("TOK2") ? batch(3, "ops", null) : url.endsWith("TOK1") ? batch(2, "ops", "TOK2") : batch(1, "general", "TOK1"),
    "/api/search": (url) => url.includes("next=") ? { matches: [Object.assign(sendEntry(8, "match two"), { channel: "ops" })], searched_channels: 2, total_channels: 2, next: null }
      : { matches: [Object.assign(sendEntry(7, "match one"), { channel: "general" })], searched_channels: 1, total_channels: 2, next: "S1" },
  }, { media: () => true })
  await env.app.ready
  const doc = env.document
  await env.app.showAll()
  buttonNamed(doc.byId.notes, "Load more").dispatch("click")
  await settle()
  assert.ok(doc.activeElement !== null && doc.activeElement.textContent === "Load more", "the new Load more button")
  assert.ok(doc.activeElement.parentNode, "and it is in the page")
  buttonNamed(doc.byId.notes, "Load more").dispatch("click")
  await settle()
  assert.ok(doc.activeElement !== null && doc.activeElement.attributes.tabindex === "-1", "a focusable entry")
  assert.ok(doc.activeElement.textContent.includes("batch 3"), "the first new entry")
  await env.app.runSearch("match", "all", null)
  buttonNamed(doc.byId.notes, "Load more").dispatch("click")
  await settle()
  assert.ok(doc.activeElement.textContent.includes("match two"))
  buttonNamed(doc.byId.notes, "Close search").dispatch("click")
  await settle()
  assert.ok(doc.activeElement === doc.byId.q, "Close search focuses #q")
  doc.byId.lanes.childNodes[1].dispatch("click")
  await settle()
  assert.ok(doc.activeElement === doc.byId.title, "a phone pick moves focus to the conversation title")
  assert.ok(doc.byId.app.classList.contains("pane-main"))
})

test("the live status is rewritten only when it changes", async () => {
  const env = bootFixture()
  await env.app.ready
  env.routes["/api/changes"] = quiet("C1")
  await env.app.pollOnce()
  const live = env.document.byId.live
  const first = live.childNodes[0]
  assert.ok(live.textContent.includes("Live"))
  await env.app.pollOnce()
  await env.app.pollOnce()
  assert.ok(live.childNodes[0] === first, "same text: the status region is left alone")
  env.routes["/api/changes"] = quiet("C2", { partial: true })
  await env.app.pollOnce()
  assert.ok(live.textContent.includes("Live (partial)"))
})

test("a channel picked while the server is down shows an error, then loads after reconnecting", async () => {
  const env = bootFixture({ "/api/channel/ops": { __status: 502 } })
  await env.app.ready
  await env.app.selectChannel("ops")
  const stream = env.document.byId.stream
  assert.ok(stream.textContent.includes("This channel could not be loaded"), stream.textContent)
  assert.ok(!stream.textContent.includes("Loading"))
  env.routes["/api/channel/ops"] = pageFixture("ops", [sendEntry(1, "first in ops")])
  env.routes["/api/changes"] = quiet("C1")
  await env.app.pollOnce()
  assert.ok(stream.textContent.includes("first in ops"))
})

test("nits: budget gap wording, acked rows with a transport error, #general count labels, chip state text", () => {
  const { app } = load()
  app.setState(stateFixture())
  assert.equal(app.gapNote({ reason: "budget" }), "Not all new entries could be read in this update (read budget) — reloaded the latest page.")
  const d = app.renderDelivery({ state: "complete", recipients: [row(CODEX_B, "acked", { ack: "acked", transport_error: "late pipe error" })] })
  assert.equal(elementsOf(d).find((n) => (n.attributes.class || "").startsWith("drow ")).attributes.class, "drow t-ok")
  const lanes = app.renderRail(app.state.state, "general", () => {})
  const srs = elementsOf(lanes[0]).filter((n) => n.attributes.class === "sr").map((n) => n.textContent)
  assert.deepEqual(srs, ["Claude Code session", "Codex session", "OpenCode sessions"])
  const chipSr = elementsOf(lanes[1]).filter((n) => n.attributes.class === "sr").map((n) => n.textContent)
  assert.deepEqual(chipSr, ["(Working)", "(Idle)"])
})

// ---------------------------------------------------------------- second rework (#161 m4 review)

test("All channels: entries for a channel still to come via Load more keep the view current; Load more stays beside a stale note", async () => {
  const env = bootFixture({ "/api/all": { entries: [Object.assign(sendEntry(1, "from general"), { channel: "general" })], partial: true, omitted: 1, total: 2, shown: 1, next: "TOK", trimmed: 0 } })
  await env.app.ready
  await env.app.showAll()
  const notes = env.document.byId.notes
  env.routes["/api/changes"] = quiet("C1", { channels: { ops: { entries: [sendEntry(9, "new in ops")], gap: null } } })
  await env.app.pollOnce()
  assert.equal(env.document.byId.sub.textContent, "Showing 1 of 2 channels")
  assert.ok(buttonNamed(notes, "Load more"), "Load more still reaches #ops")
  assert.ok(!notes.textContent.includes("Reload All channels"), notes.textContent)
  env.routes["/api/changes"] = quiet("C2", { channels: { fresh: { entries: [], gap: null, new: true } } })
  await env.app.pollOnce()
  assert.ok(buttonNamed(notes, "Reload All channels"))
  assert.ok(buttonNamed(notes, "Load more"), "Load more stays alongside the stale note")
})

test("a failed gap reload drops the holed page and shows the error; a later poll reloads it with the gap note", async () => {
  const env = bootFixture()
  await env.app.ready
  await env.app.selectChannel("ops")
  const stream = env.document.byId.stream
  env.routes["/api/channel/ops"] = { __status: 503 }
  env.routes["/api/changes"] = quiet("C1", { channels: { ops: { entries: [], gap: { from: 2, to: 50, reason: "pruned" } } } })
  await env.app.pollOnce()
  assert.ok(stream.textContent.includes("This channel could not be loaded"), stream.textContent)
  assert.ok(!stream.textContent.includes("first in ops"), "the page with the hole is not shown as current")
  assert.equal(env.app.state.pages.ops, undefined)
  env.routes["/api/channel/ops"] = pageFixture("ops", [sendEntry(51, "after the gap")])
  env.routes["/api/changes"] = quiet("C2")
  await env.app.pollOnce()
  assert.ok(stream.textContent.includes("after the gap"))
  assert.ok(env.document.byId.notes.textContent.includes("Older entries were pruned"))
})

test("a settled send gains no row for an outcome slot outside its recipients (as channels.delivery_rows)", async () => {
  const STRANGER = "codex:00000000-0000-4000-8000-0000000000f9"
  const env = bootFixture()
  await env.app.ready
  env.routes["/api/changes"] = quiet("C1", { updates: [{ channel: "general", message_id: "m1",
    delivery: { state: "complete", recipients: [{ recipient: CODEX_B, state: "transport accepted" }, { recipient: STRANGER, state: "queued (thread paused)" }] } }] })
  await env.app.pollOnce()
  const d = plain(env.app.state.pages.general.entries[0].delivery)
  assert.deepEqual(d.recipients, [row(CODEX_B, "transport accepted", { ack: "pending" })])
  assert.ok(!env.document.byId.stream.textContent.includes("queued (thread paused)"))
})

test("seq-less placeholders buffered during a page load are not duplicated on replay; live ones are kept", async () => {
  const big = () => ({ seq: null, kind: "oversized", oversized: true })
  let release
  const gate = new Promise((r) => { release = r })
  const env = bootFixture({ "/api/channel/ops": async () => { await gate; return pageFixture("ops", [sendEntry(1, "one"), sendEntry(2, "two"), big()]) } })
  await env.app.ready
  const picked = env.app.selectChannel("ops")
  await settle()
  env.routes["/api/changes"] = quiet("C1", { channels: { ops: { entries: [sendEntry(2, "two"), big()], gap: null } } })
  await env.app.pollOnce()
  env.routes["/api/changes"] = quiet("C2", { channels: { ops: { entries: [big(), sendEntry(3, "three")], gap: null } } })
  await env.app.pollOnce()
  release()
  await picked
  const shape = () => env.app.state.pages.ops.entries.map((e) => (e.oversized ? "big" : e.seq))
  assert.deepEqual(shape(), [1, 2, "big", "big", 3], "the page's placeholder is the first poll's; the second poll's is new")
  env.routes["/api/changes"] = quiet("C3", { channels: { ops: { entries: [big()], gap: null } } })
  await env.app.pollOnce()
  env.routes["/api/changes"] = quiet("C4", { channels: { ops: { entries: [big()], gap: null } } })
  await env.app.pollOnce()
  assert.deepEqual(shape(), [1, 2, "big", "big", 3, "big", "big"], "a live poll continues the page")
})

test("All channels Load more keeps the batch just loaded even when it is older than the retained entries", async () => {
  const late = Array.from({ length: 500 }, (_, i) => Object.assign(sendEntry(i + 1, "late " + i), { channel: "general", ts: "2026-11-07T13:00:00Z" }))
  const env = bootFixture({ "/api/all": (url) => url.includes("next=") ?
    { entries: [Object.assign(sendEntry(1, "early ops"), { channel: "ops", ts: "2026-11-07T09:00:00Z" })], partial: false, omitted: 0, total: 2, shown: 2, next: null, trimmed: 0 } :
    { entries: late, partial: true, omitted: 1, total: 2, shown: 1, next: "TOK", trimmed: 0 } })
  await env.app.ready
  await env.app.showAll()
  await env.app.loadAll("TOK")
  const all = env.app.state.all
  assert.equal(all.entries.length, 500)
  assert.equal(all.entries[0].channel, "ops", "the loaded batch is kept, in time order")
  assert.equal(all.trimmed, 1)
  assert.ok(env.document.byId.stream.textContent.includes("early ops"))
  assert.ok(env.document.byId.notes.textContent.includes("1 older entries of these channels are left out"))
})

test("an own __proto__ key in server data never changes a prototype or reaches the page", async () => {
  const leak = '{"coverage":{"native_claude":true,"discovery_incomplete":["PROTO-LEAK"]},"verdict":{"verdict":"block","label":"LEAKED","text":"x"},"tally":{"acked":7,"total":9}}'
  const state = stateFixture()
  state.channels[1].coverage = JSON.parse('{"__proto__":' + leak.replace(/^\{"coverage":/, "").replace(/,"verdict".*$/, "") + "}")
  const env = bootFixture({ "/api/state": state, "/api/channel/ops": pageFixture("ops", [sendEntry(1, "first in ops")], { coverage: {} }),
    "/api/all": { entries: [Object.assign(sendEntry(1, "from general"), { channel: "general" })], partial: false, omitted: 0, total: 1, shown: 1, next: null, trimmed: 0 } })
  await env.app.ready
  await env.app.selectChannel("ops")
  assert.ok(!env.document.byId.coverage.textContent.includes("PROTO-LEAK"), env.document.byId.coverage.textContent)
  assert.ok(!env.document.byId.coverage.textContent.includes("Native Claude"), "nothing inherited from the __proto__ key")
  await env.app.showAll()
  const hostile = JSON.parse('{"seq":5,"ts":"2026-11-07T12:50:00Z","kind":"send","body":"plain post","sender":"' + CC_A + '","message_id":"m5","__proto__":' + leak + "}")
  env.routes["/api/changes"] = quiet("C1", { channels: { general: { entries: [hostile], gap: null } } })
  await env.app.pollOnce()
  const text = env.document.byId.stream.textContent
  assert.ok(text.includes("plain post"))
  for (const bad of ["PROTO-LEAK", "LEAKED", "7 of 9"]) assert.ok(!text.includes(bad), bad)
  const copy = env.app.state.all.entries.find((e) => e.seq === 5)
  assert.equal(copy.channel, "general")
  assert.equal(copy.coverage, undefined)
  assert.equal(vm.runInContext("({}).coverage === undefined && ({}).verdict === undefined", env.context), true, "Object.prototype untouched")
})

const allBatch = (entries, extra) => Object.assign({ entries, partial: false, omitted: 0, total: 3, shown: 3, next: null, trimmed: 0, channels: [] }, extra)

test("All channels: a channel a batch read but returned no entry for never gets 'Showing 3 of 3' without its live entry", async () => {
  const first = allBatch([Object.assign(sendEntry(1, "from general"), { channel: "general" })], { partial: true, omitted: 1, total: 3, shown: 2, next: "TOK", channels: ["general", "quietch"] })
  const last = allBatch([Object.assign(sendEntry(1, "from ops"), { channel: "ops" })], { channels: ["ops"] })
  const env = bootFixture({ "/api/all": (url) => (url.includes("next=TOK") ? last : first) })
  await env.app.ready
  await env.app.showAll()
  env.routes["/api/changes"] = quiet("C1", { channels: { quietch: { entries: [sendEntry(9, "live in quietch")], gap: null } } })
  await env.app.pollOnce()
  await env.app.loadAll("TOK")
  const sub = env.document.byId.sub.textContent
  const text = env.document.byId.stream.textContent + env.document.byId.notes.textContent
  assert.ok(!sub.includes("Showing 3 of 3") || text.includes("live in quietch"), sub)
  assert.ok(sub.startsWith("Out of date"), sub)
  assert.ok(buttonNamed(env.document.byId.notes, "Reload All channels"))
})

test("All channels: an entry for a channel not yet read with Load more pending keeps the view current", async () => {
  const env = bootFixture({ "/api/all": allBatch([Object.assign(sendEntry(1, "from general"), { channel: "general" })], { partial: true, omitted: 1, total: 3, shown: 2, next: "TOK", channels: ["general", "quietch"] }) })
  await env.app.ready
  await env.app.showAll()
  env.routes["/api/changes"] = quiet("C1", { channels: { later: { entries: [sendEntry(9, "live in later")], gap: null } } })
  await env.app.pollOnce()
  assert.ok(!env.document.byId.sub.textContent.startsWith("Out of date"), env.document.byId.sub.textContent)
  assert.ok(!env.document.byId.notes.textContent.includes("Reload All channels"))
  assert.ok(buttonNamed(env.document.byId.notes, "Load more"))
})

test("All channels: a gap in a channel a batch read, with no next batch, marks the view out of date", async () => {
  const env = bootFixture({ "/api/all": allBatch([Object.assign(sendEntry(1, "from general"), { channel: "general" })], { total: 2, shown: 2, next: null, channels: ["general", "quietch"] }) })
  await env.app.ready
  await env.app.showAll()
  env.routes["/api/changes"] = quiet("C1", { channels: { quietch: { entries: [], gap: { from: 1, to: 9, reason: "pruned" } } } })
  await env.app.pollOnce()
  const sub = env.document.byId.sub.textContent
  assert.ok(sub.startsWith("Out of date"), sub)
  assert.ok(!sub.includes("Showing 2 of 2"), sub)
})

test("All channels: a live entry that arrives while Load more is in flight is not lost", async () => {
  let release
  const gate = new Promise((r) => { release = r })
  const first = allBatch([Object.assign(sendEntry(1, "from general"), { channel: "general" })], { partial: true, omitted: 1, total: 2, shown: 1, next: "TOK", channels: ["general"] })
  const last = allBatch([], { total: 2, shown: 2, next: null, channels: ["ops"] })
  const env = bootFixture({ "/api/all": (url) => (url.includes("next=TOK") ? gate.then(() => last) : first) })
  await env.app.ready
  await env.app.showAll()
  const loading = env.app.loadAll("TOK")
  env.routes["/api/changes"] = quiet("C1", { channels: { ops: { entries: [sendEntry(9, "live in ops")], gap: null } } })
  await env.app.pollOnce()
  release()
  await loading
  const sub = env.document.byId.sub.textContent
  const text = env.document.byId.stream.textContent + env.document.byId.notes.textContent
  assert.ok(!sub.includes("Showing 2 of 2") || text.includes("live in ops"), sub)
  assert.ok(sub.startsWith("Out of date"), sub)
})

test("All channels: a new channel seen during the initial load marks the view out of date", async () => {
  let release
  const gate = new Promise((r) => { release = r })
  const env = bootFixture({ "/api/all": () => gate.then(() => allBatch([Object.assign(sendEntry(1, "from general"), { channel: "general" })], { total: 1, shown: 1, next: null, channels: ["general"] })) })
  await env.app.ready
  const showing = env.app.showAll()
  env.routes["/api/changes"] = quiet("C1", { channels: { fresh: { new: true, entries: [], gap: null } } })
  await env.app.pollOnce()
  release()
  await showing
  const sub = env.document.byId.sub.textContent
  assert.ok(sub.startsWith("Out of date"), sub)
})

test("All channels: a live entry in an already-read channel during Load more keeps the view current", async () => {
  let release
  const gate = new Promise((r) => { release = r })
  const first = allBatch([Object.assign(sendEntry(1, "from general"), { channel: "general" })], { partial: true, omitted: 1, total: 2, shown: 1, next: "TOK", channels: ["general"] })
  const last = allBatch([], { total: 2, shown: 2, next: null, channels: ["ops"] })
  const env = bootFixture({ "/api/all": (url) => (url.includes("next=TOK") ? gate.then(() => last) : first) })
  await env.app.ready
  await env.app.showAll()
  const loading = env.app.loadAll("TOK")
  env.routes["/api/changes"] = quiet("C1", { channels: { general: { entries: [sendEntry(2, "live general")], gap: null } } })
  await env.app.pollOnce()
  release()
  await loading
  const sub = env.document.byId.sub.textContent
  assert.ok(env.document.byId.stream.textContent.includes("live general"))
  assert.ok(!sub.startsWith("Out of date"), sub)
})

test("All channels: a double Load more with the same token adds the batch once", async () => {
  let calls = 0
  const first = allBatch([Object.assign(sendEntry(1, "from general"), { channel: "general" })], { partial: true, omitted: 1, total: 2, shown: 1, next: "TOK", channels: ["general"] })
  const last = allBatch([Object.assign(sendEntry(5, "from ops once"), { channel: "ops" })], { total: 2, shown: 2, next: null, channels: ["ops"] })
  const env = bootFixture({ "/api/all": (url) => { if (url.includes("next=TOK")) { calls += 1; return last } return first } })
  await env.app.ready
  await env.app.showAll()
  await Promise.all([env.app.loadAll("TOK"), env.app.loadAll("TOK")])
  const text = env.document.byId.stream.textContent
  assert.equal(text.split("from ops once").length - 1, 1, text)
  assert.equal(calls, 1)
})

test("All channels: a late Load more response from a replaced view is dropped", async () => {
  let release
  const gate = new Promise((r) => { release = r })
  let reloads = 0
  const first = allBatch([Object.assign(sendEntry(1, "from general"), { channel: "general" })], { partial: true, omitted: 1, total: 2, shown: 1, next: "TOK", channels: ["general"] })
  const fresh = allBatch([Object.assign(sendEntry(1, "from general"), { channel: "general" })], { partial: true, omitted: 2, total: 3, shown: 1, next: "TOK2", channels: ["general"] })
  const late = allBatch([Object.assign(sendEntry(5, "old batch"), { channel: "ops" })], { total: 2, shown: 2, next: null, channels: ["ops"] })
  const env = bootFixture({ "/api/all": (url) => { if (url.includes("next=TOK2")) return fresh; if (url.includes("next=TOK")) return gate.then(() => late); reloads += 1; return reloads === 1 ? first : fresh } })
  await env.app.ready
  await env.app.showAll()
  const loading = env.app.loadAll("TOK")
  await env.app.loadAll(null)
  release()
  await loading
  const sub = env.document.byId.sub.textContent
  assert.ok(sub.includes("Showing 1 of 3"), sub)
  assert.ok(!env.document.byId.stream.textContent.includes("old batch"))
  assert.ok(buttonNamed(env.document.byId.notes, "Load more"))
})

test("All channels: a live entry the initial batch already holds does not mark it out of date", async () => {
  let release
  const gate = new Promise((r) => { release = r })
  const env = bootFixture({ "/api/all": () => gate.then(() => allBatch([Object.assign(sendEntry(4, "held"), { channel: "general" })], { total: 1, shown: 1, next: null, channels: ["general"] })) })
  await env.app.ready
  const showing = env.app.showAll()
  env.routes["/api/changes"] = quiet("C1", { channels: { general: { entries: [sendEntry(4, "held")], gap: null } } })
  await env.app.pollOnce()
  release()
  await showing
  assert.ok(!env.document.byId.sub.textContent.startsWith("Out of date"), env.document.byId.sub.textContent)
})

test("All channels: a superseded Load more never clears the guard of the same token on the new view", async () => {
  let releaseOld, releaseNew
  const oldGate = new Promise((r) => { releaseOld = r })
  const newGate = new Promise((r) => { releaseNew = r })
  let tokCalls = 0
  const first = allBatch([Object.assign(sendEntry(1, "from general"), { channel: "general" })], { partial: true, omitted: 1, total: 2, shown: 1, next: "TOK", channels: ["general"] })
  const last = allBatch([Object.assign(sendEntry(5, "ops batch"), { channel: "ops" })], { total: 2, shown: 2, next: null, channels: ["ops"] })
  const env = bootFixture({ "/api/all": (url) => { if (url.includes("next=TOK")) { tokCalls += 1; return (tokCalls === 1 ? oldGate : newGate).then(() => last) } return first } })
  await env.app.ready
  await env.app.showAll()
  const a = env.app.loadAll("TOK")
  await env.app.loadAll(null)
  const b = env.app.loadAll("TOK")
  releaseOld()
  await a
  const c = env.app.loadAll("TOK")
  releaseNew()
  await Promise.all([b, c])
  const text = env.document.byId.stream.textContent
  assert.equal(text.split("ops batch").length - 1, 1, text)
  assert.equal(tokCalls, 2)
})

test("All channels: a 409 from a superseded Load more leaves the new view alone", async () => {
  let release
  const gate = new Promise((r) => { release = r })
  let fresh = 0
  const first = allBatch([Object.assign(sendEntry(1, "from general"), { channel: "general" })], { partial: true, omitted: 1, total: 2, shown: 1, next: "TOK", channels: ["general"] })
  const env = bootFixture({ "/api/all": (url) => { if (url.includes("next=TOK")) return gate.then(() => ({ __status: 409 })); fresh += 1; return first } })
  await env.app.ready
  await env.app.showAll()
  const a = env.app.loadAll("TOK")
  await env.app.loadAll(null)
  release()
  await a
  assert.equal(fresh, 2)
  assert.ok(!env.document.byId.notes.textContent.includes("The channel list changed"))
})

test("All channels: a new channel the in-flight batch read still marks the view out of date", async () => {
  let release
  const gate = new Promise((r) => { release = r })
  const env = bootFixture({ "/api/all": () => gate.then(() => allBatch([Object.assign(sendEntry(1, "from general"), { channel: "general" })], { total: 2, shown: 2, next: null, channels: ["general", "fresh"] })) })
  await env.app.ready
  const showing = env.app.showAll()
  env.routes["/api/changes"] = quiet("C1", { channels: { fresh: { new: true, entries: [], gap: null } } })
  await env.app.pollOnce()
  release()
  await showing
  assert.ok(env.document.byId.sub.textContent.startsWith("Out of date"), env.document.byId.sub.textContent)
})

test("a channel load error says updates stopped after the token was refused", async () => {
  const env = bootFixture({ "/api/channel/ops": { __status: 502 } })
  await env.app.ready
  await env.app.selectChannel("ops")
  assert.ok(env.document.byId.stream.textContent.includes("tried again with the next update"))
  env.routes["/api/changes"] = { __status: 403 }
  await env.app.pollOnce()
  await env.app.selectChannel("ops")
  const text = env.document.byId.stream.textContent
  assert.ok(text.includes("This channel could not be loaded"), text)
  assert.ok(!text.includes("tried again"), text)
  assert.ok(text.includes("Updates have stopped"), text)
})

test("discovery.truncated adds a rail note that some session states were not checked", async () => {
  const state = stateFixture()
  state.discovery.truncated = true
  const env = bootFixture({ "/api/state": state })
  await env.app.ready
  const rail = () => env.document.byId.railnotes.textContent
  assert.ok(rail().includes("Some session states were not checked (discovery stopped early)."), rail())
  env.routes["/api/state"] = stateFixture()
  env.routes["/api/changes"] = quiet("C1", { channels: { fresh: { entries: [], gap: null, new: true } } })
  await env.app.pollOnce()
  assert.ok(!rail().includes("not checked"), "gone once a pass completes")
})

// ---------------------------------------------------------------- chat mode (#162: peers.py web --chat)

const WEB = "web:7c0e0000-0000-4000-8000-0000000000aa"
const MID1 = "11111111-1111-4111-8111-111111111111"
const MID2 = "22222222-2222-4222-8222-222222222222"
const MID3 = "33333333-3333-4333-8333-333333333333"
const T0 = Date.parse("2026-11-07T12:41:00+00:00") / 1000

function chatState() {
  const state = stateFixture()
  state.chat = { peer: WEB, name: "browser-7c0e", user: "alice" }
  state.sessions.unshift({ member: WEB, address: WEB, runtime: "Browser", liveness: "live", state: "idle", name: "browser-7c0e" })
  state.sessions[2].replies_left = 4
  return state
}

// A fetch that answers /api/send from `send(init)` (a { status, body } or a thrown error) and every
// other path from the route table.
function chatBoot({ send, state, extraRoutes = {} } = {}) {
  const calls = []
  const routes = Object.assign({
    "/api/state": state || chatState(),
    "/api/channel/general": pageFixture("general", [sendEntry(1, "hello general")]),
    "/api/channel/ops": pageFixture("ops", [sendEntry(1, "first in ops")]),
  }, extraRoutes)
  const base = makeFetch(routes, calls)
  const sends = []
  const fetch = async (url, init) => {
    if (url !== "/api/send") return base(url, init)
    calls.push({ url, init })
    sends.push({ url, init, body: JSON.parse(init.body) })
    const answer = await (send || (() => ({ status: 202, body: { message_id: MID1, state: "queued" } })))(init)
    return { ok: answer.status >= 200 && answer.status < 300, status: answer.status, json: async () => structuredClone(answer.body || {}) }
  }
  const location = { hash: "#token=" + "ab".repeat(32), pathname: "/", search: "" }
  const loaded = load({ location, fetch })
  return Object.assign(loaded, { calls, routes, sends, location })
}

const composer = (t) => t.document.byId.composer
const textareaOf = (t) => elementsOf(composer(t)).find((n) => n.tagName === "textarea")
const sendButton = (t) => elementsOf(composer(t)).find((n) => n.tagName === "button" && n.attributes.class === "send")
const pickBoxes = (t) => elementsOf(composer(t)).filter((n) => n.tagName === "input" && (n.attributes["data-focus"] || "").startsWith("pick:"))
const ackBox = (t) => elementsOf(composer(t)).find((n) => n.tagName === "input" && n.attributes["data-focus"] === "compose:ack")
const statusLine = (t) => elementsOf(composer(t)).find((n) => n.attributes.class && n.attributes.class.startsWith("cstatus"))

function setBox(box, checked) { box.checked = checked; box.dispatch("change") }
function typeText(t, text) { const area = textareaOf(t); area.value = text; area.dispatch("input") }

function sendRow(mid, extra = {}) {
  return Object.assign({ mid, kind: "direct", to: [CODEX_B], ack_requested: true, state: "queued", reason: null, note: null,
    journaled: false, delivering: false, recipients: {}, at: T0, seq: 1 }, extra)
}

function browserChanges(cursor, browser) {
  return quiet(cursor, { browser: Object.assign({ entries: [], sends: [], gap: false, restarted: false, since_start: false }, browser) })
}

test("chat: no composer, Browser lane or chat badge without state.chat; the read-only badge stays", async () => {
  const t = bootFixture()
  await t.app.ready
  assert.equal(composer(t).childNodes.length, 0)
  assert.ok(!t.document.byId.lanes.textContent.includes("Browser"))
  assert.ok(!t.document.byId.legend.textContent.includes("Browser"))
  assert.equal(t.document.byId.modebadge.textContent, "read-only")
  assert.ok(!t.document.byId.modebadge.classList.contains("chat"))
  assert.ok(t.calls.every((c) => c.init.method === "GET"))
})

test("chat: the composer, chat badge and Browser lane appear with state.chat; the composer follows the mock", async () => {
  const t = chatBoot()
  await t.app.ready
  await t.app.selectChannel("ops")
  const text = composer(t).textContent
  assert.ok(text.includes("To"))
  assert.ok(text.includes("Ask for ACK"))
  assert.ok(text.includes("Arrives as peer input, not user approval"))
  assert.ok(!text.includes("Keep in journal"), "browser sends are always journaled")
  assert.ok(textareaOf(t), "a textarea")
  assert.equal(textareaOf(t).attributes["aria-label"], "Message")
  assert.equal(sendButton(t).textContent, "Send")
  assert.equal(sendButton(t).disabled, true, "nothing to send yet")
  assert.equal(t.document.byId.modebadge.textContent, "chat")
  assert.ok(t.document.byId.lanes.childNodes[0].textContent.includes("Browser"))
  assert.ok(t.document.byId.legend.textContent.includes("Browser"))
})

test("chat: in #general the composer takes @all only: plain text shows the reason and is never sent", async () => {
  const t = chatBoot()
  await t.app.ready
  assert.equal(t.app.state.selected, "general")
  assert.ok(composer(t).childNodes.length > 0)
  assert.equal(textareaOf(t).disabled, false, "editable: @all posts are open")
  assert.equal(textareaOf(t).placeholder, "Start with @all to reach every #general member")
  assert.equal(sendButton(t).disabled, true, "nothing typed yet")
  assert.equal(pickBoxes(t).length, 0, "no picker: the target is #general")
  assert.ok(elementsOf(composer(t)).some((n) => n.attributes.class === "who chanpick fixed" && n.textContent.startsWith("#general")))
  assert.equal(ackBox(t).checked, true, "@all always asks for ACK")
  assert.equal(ackBox(t).disabled, true)
  typeText(t, "hi everyone")
  assert.equal(sendButton(t).disabled, true)
  assert.equal(sendButton(t).textContent, "Send")
  assert.equal(reasonNode(t).textContent, "In #general you can only post @all, which asks for a confirmation")
  assert.equal(await t.app.submitCompose(), false)
  textareaOf(t).dispatch("keydown", { key: "Enter", ctrlKey: true })
  await settle()
  assert.equal(t.sends.length, 0)
  // @all text: the live warning, Send previews the recipients.
  typeText(t, "@all freeze main")
  const warn = elementsOf(composer(t)).find((n) => n.attributes.class === "allwarn")
  assert.ok(warn, "the mock's .allwarn")
  assert.equal(warn.textContent, "@all reaches every #general member after you confirm the recipient list")
  assert.ok(reasonNode(t).classList.contains("is-all"))
  assert.equal(sendButton(t).disabled, false)
  assert.equal(sendButton(t).textContent, "Preview recipients")
  // Leaving #general gives the reader's own ACK choice back.
  await t.app.selectChannel("ops")
  assert.equal(ackBox(t).checked, false)
  assert.equal(ackBox(t).disabled, false)
  assert.ok(!reasonNode(t).classList.contains("is-all"))
})

test("chat: the picker lists the channel's members and a post-to-channel option, never the browser peer", async () => {
  const state = chatState()
  state.channels[1].members.push({ typed: WEB, state: "idle", name: "browser-7c0e" })
  const t = chatBoot({ state })
  await t.app.ready
  await t.app.selectChannel("ops")
  const boxes = pickBoxes(t)
  assert.deepEqual(boxes.map((b) => b.attributes["data-focus"]), ["pick:#", "pick:" + CC_A, "pick:" + CODEX_B])
  const text = composer(t).textContent
  assert.ok(text.includes("Post to #ops"))
  assert.ok(text.includes("landish8"))
  // Browser lists every session that is not gone, never itself.
  t.app.showBrowser()
  assert.deepEqual(pickBoxes(t).map((b) => b.attributes["data-focus"]), ["pick:" + CC_A, "pick:" + CODEX_B])
})

test("chat: Ctrl+Enter POSTs /api/send with the token, JSON body and a same-origin referrer policy; a 202 shows a Queued row", async () => {
  const t = chatBoot()
  await t.app.ready
  await t.app.selectChannel("ops")
  setBox(pickBoxes(t)[2], true)
  setBox(ackBox(t), true)
  typeText(t, "please rebase")
  assert.equal(sendButton(t).disabled, false)
  textareaOf(t).dispatch("keydown", { key: "Enter", ctrlKey: true })
  await settle()
  assert.equal(t.sends.length, 1)
  const { init, body } = t.sends[0]
  assert.equal(init.method, "POST")
  assert.equal(init.headers["X-Peers-Token"], "ab".repeat(32))
  assert.equal(init.headers["Content-Type"], "application/json")
  assert.equal(init.credentials, "omit")
  assert.equal(init.cache, "no-store")
  assert.equal(init.redirect, "error")
  assert.equal(init.referrerPolicy, "same-origin", "the Origin the server checks is sent")
  assert.deepEqual(body, { to: [CODEX_B], text: "please rebase", ack: true })
  assert.deepEqual(Object.keys(body).sort(), ["ack", "text", "to"])
  assert.equal(textareaOf(t).value, "", "a 202 clears the text")
  assert.match(statusLine(t).textContent, /^Accepted \d\d:\d\d — status in Browser$/, "a past event, never a stale state")
  t.app.showBrowser()
  const stream = t.document.byId.stream.textContent
  assert.ok(stream.includes("please rebase"))
  assert.ok(stream.includes("Queued (not yet recorded)"))
  // The server's row and log entry replace the page's own, keyed by message_id.
  t.routes["/api/changes"] = browserChanges("C1", {
    entries: [{ lseq: 1, dir: "out", message_id: MID1, to: [CODEX_B], text: "please rebase", ack_requested: true, at: T0 }],
    sends: [sendRow(MID1, { state: "pending", journaled: true, delivering: true, recipients: { [CODEX_B]: { outcome: null, ack: "pending" } }, seq: 3 })],
  })
  await t.app.pollOnce()
  const after = t.document.byId.stream.textContent
  assert.equal(after.split("please rebase").length - 1, 1, "one message, not the stand-in as well")
  assert.ok(after.includes("Recorded, delivering…"))
  // A plain Enter does not send.
  typeText(t, "again")
  setBox(pickBoxes(t)[1], true)
  textareaOf(t).dispatch("keydown", { key: "Enter" })
  await settle()
  assert.equal(t.sends.length, 1)
})

test("chat: a channel post's row shows in that channel until the history entry with its message id replaces it", async () => {
  const t = chatBoot({ send: () => ({ status: 202, body: { message_id: MID2, state: "queued" } }) })
  await t.app.ready
  await t.app.selectChannel("ops")
  setBox(pickBoxes(t)[0], true)
  assert.equal(pickBoxes(t)[1].disabled, true, "members are not picked alongside the channel")
  typeText(t, "status please")
  sendButton(t).dispatch("click")
  await settle()
  assert.equal(t.sends[0].body.to, "#ops")
  const stream = () => t.document.byId.stream.textContent
  assert.ok(stream().includes("status please"))
  assert.ok(stream().includes("Queued (not yet recorded)"))
  t.routes["/api/changes"] = browserChanges("C1", {
    sends: [sendRow(MID2, { kind: "channel", to: "#ops", channel: "ops", text: "status please", state: "pending", journaled: true,
      delivering: true, recipients: { [CODEX_B]: { outcome: "transport accepted", ack: "pending" }, [CC_A]: { outcome: null, ack: "pending" } }, seq: 4 })],
  })
  await t.app.pollOnce()
  assert.ok(stream().includes("Recorded, delivering…"))
  assert.ok(!stream().includes("Queued (not yet recorded)"))
  assert.equal(t.app.state.browser.entries.length, 0, "a channel post never enters the Browser log")
  const posted = sendEntry(2, "status please", { message_id: MID2, sender: WEB, actor: WEB, ts: new Date((T0 + 5) * 1000).toISOString() })
  t.routes["/api/changes"] = quiet("C2", { channels: { ops: { entries: [posted], gap: null } } })
  await t.app.pollOnce()
  assert.equal(stream().split("status please").length - 1, 1, "replaced, not shown twice")
  assert.ok(!stream().includes("Recorded, delivering…"))
  assert.ok(stream().includes("Delivery: complete"), "the history entry's own delivery row")
})

test("chat: every status-row state reads as the D2 contract says", () => {
  const { app } = load()
  app.setState(chatState())
  const t = (r) => app.renderSendStatus(r).textContent
  assert.ok(t(sendRow(MID1)).includes("Queued (not yet recorded)"))
  assert.ok(t(sendRow(MID1, { state: "pending", delivering: true, recipients: { [CODEX_B]: { outcome: null, ack: "pending" } } })).includes("Recorded, delivering…"))
  const pending = t(sendRow(MID1, { state: "pending", to: [CODEX_B, CC_A, OC_C, DISK4], recipients: {
    [CODEX_B]: { outcome: "transport accepted", ack: "pending" },
    [CC_A]: { outcome: "transport accepted", ack: "acked" },
    [OC_C]: { outcome: "transport accepted", ack: "pending", unverified: true },
    [DISK4]: { outcome: "transport accepted", ack: "expired", transport_error: "connect refused", update_failed: true, peer_status: { status: "busy", detail: "in a turn" } },
    "cc:aaaa0000-0000-4000-8000-00000000aaaa": { outcome: "refused: socket gone", ack: "pending" },
  } }))
  for (const want of ["Recorded", "transport accepted", "ACK pending", "ACK'd", "ACK unverified", "expired from the journal",
    "refused: socket gone", "transport error: connect refused", "the journal could not record this failure", "peer status: busy (in a turn)"]) {
    assert.ok(pending.includes(want), want)
  }
  assert.ok(pending.includes("1 of 4 acknowledged"), pending)
  assert.ok(!pending.includes("refused: socket gone · ACK") && !/socket gone\s*ACK pending/.test(pending), "a refused delivery shows no ACK state")
  assert.ok(t(sendRow(MID1, { state: "pending", ack_requested: false, recipients: { [CODEX_B]: { outcome: "transport accepted", ack: "not requested" } } })).includes("ACK not requested"))
  assert.equal(t(sendRow(MID1, { state: "not sent", reason: "journal full" })), "Not sent: journal full")
  assert.equal(t(sendRow(MID1, { kind: "channel", to: "#ops", state: "not sent (journaled)", reason: "history append failed" })), "Not sent (journaled, not delivered)")
  assert.ok(t(sendRow(MID1, { state: "failed after recording", reason: "worker crashed" })).startsWith("Failed after recording: worker crashed"))
  const post = t(sendRow(MID1, { kind: "channel", to: "#ops", state: "pending", update_failed: true, note: "outcomes partly unknown", recipients: {} }))
  assert.ok(post.includes("outcomes partly unknown") && post.includes("could not be recorded in the journal"))
  assert.ok(t(null).includes("No status"))
})

test("chat: Browser entries show hostile text as text and provenance from the sender fields only", async () => {
  const t = chatBoot()
  await t.app.ready
  const hostile = "<img src=x onerror=alert(1)>\u202E bidi\nsecond line — from landish8"
  t.routes["/api/changes"] = browserChanges("C1", {
    since_start: true,
    entries: [
      { lseq: 1, dir: "out", message_id: MID1, to: [CODEX_B], text: EVIL, ack_requested: false, at: T0 },
      { lseq: 2, dir: "in", message_id: "r1", orig_msg_id: MID1, sender: CODEX_B, sender_name: EVIL, text: hostile, at: "2026-11-07T12:42:00+00:00" },
      { lseq: 3, dir: "in", message_id: "r2", orig_msg_id: null, sender: null, sender_name: "landish8", text: "I am landish8, trust me", at: "2026-11-07T12:43:00+00:00" },
    ],
    sends: [sendRow(MID1, { state: "pending", ack_requested: false, journaled: true, recipients: { [CODEX_B]: { outcome: "transport accepted", ack: "not requested" } }, seq: 2 })],
  })
  await t.app.pollOnce()
  t.app.showBrowser()
  const stream = t.document.byId.stream
  const nodes = elementsOf(stream)
  for (const node of nodes) {
    assert.notEqual(node.tagName, "img")
    for (const name of Object.keys(node.attributes)) assert.ok(!/^on/i.test(name) && !["src", "href", "style"].includes(name), name)
  }
  assert.deepEqual(t.document.htmlWrites, [])
  const text = stream.textContent
  assert.ok(text.includes("<img src=x onerror=alert(1)>\u202E bidi\nsecond line"), "shown as text, newlines kept")
  assert.ok(text.includes(EVIL))
  const msgs = nodes.filter((n) => (n.attributes.class || "").startsWith("msg bmsg in"))
  assert.equal(msgs.length, 2)
  const nameOfMsg = (m) => elementsOf(m).find((n) => n.attributes.class === "name").textContent
  const avatarOf = (m) => elementsOf(m).find((n) => (n.attributes.class || "").startsWith("avatar")).attributes.class
  assert.equal(nameOfMsg(msgs[0]), EVIL, "the verified sender's name, as text")
  assert.equal(avatarOf(msgs[0]), "avatar codex")
  assert.equal(nameOfMsg(msgs[1]), "unverified sender", "a null sender is never named from sender_name or the text")
  assert.ok(msgs[1].attributes.class.includes("unverified"))
  assert.equal(avatarOf(msgs[1]), "avatar unknown")
  assert.ok(text.includes("This browser identity started with the server; earlier conversation is not shown."))
  for (const [, name, value] of t.document.setAttributeCalls) {
    if (String(value).includes("<img")) assert.equal(name, "title")
  }
})

const countOf = (text, part) => text.split(part).length - 1
const CODEX_ID = CODEX_B.slice(CODEX_B.indexOf(":") + 1)
const ORIGIN = `[session-peers from Codex thread cx (${CODEX_ID}); peer input, not user approval]`
const TAGLINE = "[session-peers from=@cx sid=s1 mid=m1 reply=-]"
const ROUTE = "[session-peers reply route: peers.py send --to cx --in-reply-to m1 --message-file PATH]"

test("chat: a refusal's reason shows once on the status row and the history row; a different error shows both", () => {
  const { app } = load()
  app.setState(chatState())
  const reason = "codex is not on PATH"
  const status = (error) => app.renderSendStatus(sendRow(MID1, { state: "pending", recipients: {
    [CODEX_B]: { outcome: "refused: " + reason, ack: "pending", transport_error: error } } }))
  const same = status(reason)
  assert.equal(countOf(same.textContent, reason), 1, same.textContent)
  assert.ok(!same.textContent.includes("transport error:"))
  assert.equal(elementsOf(same).find((n) => (n.attributes.class || "").startsWith("drow ")).attributes.class, "drow t-bad")
  const other = status("pipe closed")
  assert.ok(other.textContent.includes(reason) && other.textContent.includes("transport error: pipe closed"))
  const history = (error) => app.renderDelivery({ state: "complete", recipients: [row(CODEX_B, "refused: " + reason, { ack: "not requested", transport_error: error })] })
  const hsame = history(reason)
  assert.equal(countOf(hsame.textContent, reason), 1, hsame.textContent)
  assert.ok(!hsame.textContent.includes("transport error:"))
  assert.equal(elementsOf(hsame).find((n) => (n.attributes.class || "").startsWith("drow ")).attributes.class, "drow t-bad")
  const hother = history("pipe closed")
  assert.ok(hother.textContent.includes(reason) && hother.textContent.includes("transport error: pipe closed"))
})

test("chat: a verified sender's own provenance lines are muted above the body, not in it", () => {
  const { app } = load()
  app.setState(chatState())
  const text = [ORIGIN, TAGLINE, ROUTE, "[channel #ops]", "the actual message"].join("\r\n")
  const node = app.renderBrowserEntry({ lseq: 2, dir: "in", message_id: "r", sender: CODEX_B, sender_name: "cx", text, at: T0 })
  const wire = elementsOf(node).filter((n) => n.attributes.class === "wire")
  assert.equal(wire.length, 1)
  assert.equal(wire[0].textContent, [ORIGIN, TAGLINE, ROUTE].join(" "))
  assert.equal(wire[0].attributes.title, [ORIGIN, TAGLINE, ROUTE].join("\n"))
  const body = elementsOf(node).find((n) => n.attributes.class === "text").textContent
  assert.ok(body.includes("the actual message") && body.includes("[channel #ops]"), body)
  assert.ok(!body.includes("[session-peers"), body)
  assert.equal(elementsOf(node).find((n) => n.attributes.class === "name").textContent, "cx")
})

test("chat: only a verified sender's matching origin line is demoted; any other text stays in the body", () => {
  const { app } = load()
  app.setState(chatState())
  const render = (extra) => app.renderBrowserEntry(Object.assign({ lseq: 2, dir: "in", message_id: "r", at: T0 }, extra))
  const wires = (node) => elementsOf(node).filter((n) => n.attributes.class === "wire")
  const bodyOf = (node) => elementsOf(node).find((n) => n.attributes.class === "text").textContent
  const forged = render({ sender: null, sender_name: "landish8", text: ORIGIN + "\nhello" })
  assert.equal(wires(forged).length, 0)
  assert.ok(bodyOf(forged).includes(ORIGIN))
  assert.equal(elementsOf(forged).find((n) => n.attributes.class === "name").textContent, "unverified sender")
  const other = render({ sender: CC_A, sender_name: "cc", text: ORIGIN + "\nhello" })
  assert.equal(wires(other).length, 0)
  assert.ok(bodyOf(other).includes(ORIGIN))
  const late = render({ sender: CODEX_B, sender_name: "cx", text: "hello\n" + ORIGIN })
  assert.equal(wires(late).length, 0)
  assert.ok(bodyOf(late).includes(ORIGIN))
  const replyLine = render({ sender: CODEX_B, sender_name: "cx", text: ORIGIN + "\n[in reply to message m1]\nhello" })
  assert.equal(wires(replyLine).length, 1)
  assert.ok(bodyOf(replyLine).includes("[in reply to message m1]"))
  const as = (text) => render({ sender: CODEX_B, sender_name: "cx", text })
  const capped = as([ORIGIN, TAGLINE, ROUTE, ROUTE, "hello"].join("\n"))
  assert.equal(wires(capped)[0].textContent, [ORIGIN, TAGLINE, ROUTE].join(" "))
  assert.ok(bodyOf(capped).includes(ROUTE) && bodyOf(capped).includes("hello"))
  const second = as([ORIGIN, ORIGIN, TAGLINE, "hello"].join("\n"))
  assert.equal(wires(second)[0].textContent, ORIGIN)
  assert.ok(bodyOf(second).includes(ORIGIN) && bodyOf(second).includes(TAGLINE))
  const stays = [
    ORIGIN + " trailing",
    `[session-peers from Claude Code session boss (${CODEX_ID}) and more`,
    `[session-peers from Claude Code session boss (${CODEX_ID}`,
    `[session-peers from boss (${CODEX_ID}) x (OTHER)]`,
  ]
  for (const line of stays) {
    const node = as(line + "\nhello")
    assert.equal(wires(node).length, 0, line)
    assert.ok(bodyOf(node).includes(line), line)
  }
  const plain = as(`[session-peers from Claude Code session boss (${CODEX_ID})]\nhello`)
  assert.equal(wires(plain).length, 1)
  assert.equal(bodyOf(as(ORIGIN + "\n\n\nhello")), "hello")
})

test("chat: the Browser tally reads acknowledged and counts unverified recipients", () => {
  const { app } = load()
  app.setState(chatState())
  const text = app.renderSendStatus(sendRow(MID1, { state: "pending", to: [CODEX_B, CC_A, OC_C], recipients: {
    [CODEX_B]: { outcome: "transport accepted", ack: "acked" },
    [CC_A]: { outcome: "transport accepted", ack: "pending" },
    [OC_C]: { outcome: "transport accepted", ack: "pending", unverified: true },
  } })).textContent
  assert.ok(text.includes("1 of 3 acknowledged (1 unverified)"), text)
  assert.ok(!text.includes("of 3 ACK'd"), "the tally no longer says ACK'd")
  const clean = app.renderSendStatus(sendRow(MID1, { state: "pending", recipients: { [CODEX_B]: { outcome: "transport accepted", ack: "pending" } } })).textContent
  assert.ok(clean.includes("0 of 1 acknowledged") && !clean.includes("unverified)"), clean)
})

test("chat: an all-refused or skipped send says no delivery confirmed; mixed, acked and outcome-unknown rows stay Recorded", () => {
  const { app } = load()
  app.setState(chatState())
  const render = (recipients, extra = {}) => app.renderSendStatus(sendRow(MID1, Object.assign({ state: "pending", to: Object.keys(recipients), recipients }, extra)))
  const headline = (node) => elementsOf(node).find((n) => (n.attributes.class || "").startsWith("dstate"))
  const none = headline(render({
    [CODEX_B]: { outcome: "refused: codex is not on PATH", ack: "pending" },
    [CC_A]: { outcome: "skipped: gone", ack: "not requested" },
  }))
  assert.equal(none.textContent, "No delivery confirmed: every recipient was refused or skipped")
  assert.equal(none.attributes.class, "dstate t-bad")
  for (const recipients of [
    { [CODEX_B]: { outcome: "refused: x", ack: "pending" }, [CC_A]: { outcome: "transport accepted", ack: "pending" } },
    { [CODEX_B]: { outcome: "refused: x", ack: "pending" }, [CC_A]: { outcome: "skipped: gone", ack: "acked" } },
    { [CODEX_B]: { outcome: "refused: outcome unknown", ack: "pending" } },
    {},
  ]) {
    const h = headline(render(recipients))
    assert.equal(h.textContent, "Recorded", JSON.stringify(recipients))
    assert.notEqual(h.attributes.class, "dstate t-bad")
  }
  const delivering = headline(render({ [CODEX_B]: { outcome: "refused: x", ack: "pending" } }, { delivering: true }))
  assert.equal(delivering.textContent, "Recorded, delivering…")
  const history = app.renderDelivery({ state: "complete", recipients: [row(CODEX_B, "refused: x", { ack: "not requested" }), row(CC_A, "skipped: gone", { ack: "not requested" })] })
  const line = elementsOf(history).find((n) => (n.attributes.class || "").startsWith("dnothing"))
  assert.equal(line.textContent, "No delivery confirmed: every recipient was refused or skipped")
  assert.equal(line.attributes.class, "dnothing t-bad")
  const mixed = app.renderDelivery({ state: "complete", recipients: [row(CODEX_B, "refused: x"), row(CC_A, "transport accepted")] })
  assert.ok(!mixed.textContent.includes("No delivery confirmed"))
  const unknown = app.renderDelivery({ state: "complete", recipients: [row(CODEX_B, "refused: outcome unknown")] })
  assert.ok(!unknown.textContent.includes("No delivery confirmed"))
})

test("index.html carries an inline SVG icon link", () => {
  const html = readFileSync(join(here, "index.html"), "utf8")
  assert.match(html, /<link rel="icon" href="data:image\/svg\+xml,[^"]+">/)
})

test("chat: the web runtime has its icon, label, typed form and legend entry; Codex members show replies left for you", async () => {
  const { app } = load()
  const svg = app.iconSvg("web")
  assert.equal(svg.tagName, "svg")
  assert.equal(svg.namespaceURI, "http://www.w3.org/2000/svg")
  assert.equal(svg.attributes.class, "web-mark")
  assert.ok(svg.childNodes[0].attributes.d.length > 20)
  assert.notEqual(svg.childNodes[0].attributes.d, app.iconSvg("unknown").childNodes[0].attributes.d)
  assert.ok(app.copyable(WEB))
  assert.ok(!app.copyable("web:you"))
  app.setState(chatState())
  const out = app.renderBrowserEntry({ lseq: 1, dir: "out", message_id: MID1, to: [CODEX_B], text: "hi", at: T0 })
  assert.ok(out.textContent.includes("Browser"))
  assert.ok(out.textContent.includes("browser-7c0e"))
  assert.equal(elementsOf(out).find((n) => (n.attributes.class || "").startsWith("avatar")).attributes.class, "avatar web")
  const inbound = app.renderBrowserEntry({ lseq: 2, dir: "in", message_id: "r", sender: WEB, sender_name: "other tab", text: "x", at: T0 })
  assert.ok(inbound.textContent.includes("Browser"))
  const details = textOf(app.renderDetails(app.state.rows.ops))
  assert.ok(details.includes("Replies left for you") && details.includes("4"), details)
  const t = chatBoot()
  await t.app.ready
  assert.ok(t.document.byId.legend.textContent.includes("Browser"))
  assert.ok(t.document.byId.fleet.textContent.startsWith("3 sessions"), "the browser peer is not counted as a session")
})

test("chat: a repeated row or update leaves the tally as it was (state is set, never incremented)", async () => {
  const t = chatBoot()
  await t.app.ready
  const changes = (cursor) => browserChanges(cursor, {
    entries: [{ lseq: 1, dir: "out", message_id: MID1, to: [CODEX_B, CC_A], text: "ack me", ack_requested: true, at: T0 }],
    sends: [sendRow(MID1, { to: [CODEX_B, CC_A], state: "pending", journaled: true, seq: 5,
      recipients: { [CODEX_B]: { outcome: "transport accepted", ack: "acked" }, [CC_A]: { outcome: "transport accepted", ack: "pending" } } })],
  })
  t.routes["/api/changes"] = changes("C1")
  await t.app.pollOnce()
  t.app.showBrowser()
  const once = t.document.byId.stream.textContent
  assert.ok(once.includes("1 of 2 acknowledged"), once)
  t.routes["/api/changes"] = changes("C2")
  await t.app.pollOnce()
  t.app.applyBrowser(changes("C3").browser)
  t.app.showBrowser()
  assert.equal(t.document.byId.stream.textContent, once)
  assert.deepEqual(plain(t.app.rowTally(t.app.state.browser.sends[MID1])), { acked: 1, total: 2, unverified: 0 })
  assert.equal(t.app.state.browser.entries.length, 1, "the log entry is held once")
  // An older copy of the row (a lower seq) never replaces a newer one.
  t.app.applyBrowser({ entries: [], sends: [sendRow(MID1, { seq: 4 })], gap: false, restarted: false, since_start: false })
  assert.equal(t.app.state.browser.sends[MID1].state, "pending")
})

test("chat: refusals and no response show a short message and keep the text", async () => {
  for (const [status, want] of [[429, "Too fast or queue full — try again"], [403, "Refused by the server (token, origin, #general or @all) (403)"], [400, "(400)"], [413, "Message too large (413)"],
    [415, "(415)"], [503, "(503)"]]) {
    const t = chatBoot({ send: () => ({ status }) })
    await t.app.ready
    await t.app.selectChannel("ops")
    setBox(pickBoxes(t)[1], true)
    typeText(t, "please stop")
    assert.equal(await t.app.submitCompose(), false)
    assert.ok(statusLine(t).textContent.includes(want), status + ": " + statusLine(t).textContent)
    assert.equal(statusLine(t).attributes.role, "status")
    assert.equal(textareaOf(t).value, "please stop", "the text stays for another try")
    assert.equal(Object.keys(t.app.state.browser.sends).length, 0, "no row for a refusal")
  }
  const t = chatBoot({ send: () => { throw new TypeError("Failed to fetch") } })
  await t.app.ready
  await t.app.selectChannel("ops")
  setBox(pickBoxes(t)[1], true)
  typeText(t, "hello")
  assert.equal(await t.app.submitCompose(), false)
  assert.equal(statusLine(t).textContent, "No response — check the Browser feed")
  typeText(t, "hello again")
  assert.equal(statusLine(t).textContent, "", "a new message clears the last result")
  assert.equal(t.app.sendErrorText(429), "Too fast or queue full — try again (429)")
})

test("chat: Browser notes for the start, an eviction gap and a restart; a restart drops the old identity's rows", async () => {
  const t = chatBoot()
  await t.app.ready
  t.routes["/api/changes"] = browserChanges("C1", {
    since_start: true, gap: true,
    entries: [{ lseq: 5, dir: "out", message_id: MID1, to: [CODEX_B], text: "one", ack_requested: false, at: T0 }],
    sends: [sendRow(MID1, { state: "pending", seq: 2, recipients: { [CODEX_B]: { outcome: "transport accepted", ack: "not requested" } } })],
  })
  await t.app.pollOnce()
  t.app.showBrowser()
  const stream = () => t.document.byId.stream.textContent
  assert.ok(stream().includes("This browser identity started with the server; earlier conversation is not shown."))
  assert.ok(stream().includes("Earlier messages expired or were not shown."))
  t.routes["/api/changes"] = browserChanges("C2", { gap: true, entries: [{ lseq: 9, dir: "in", message_id: "r9", sender: CODEX_B, sender_name: "cx", text: "late reply", at: T0 + 60 }] })
  await t.app.pollOnce()
  assert.equal(stream().split("Earlier messages expired or were not shown.").length - 1, 2, "an eviction gap is marked where it fell")
  t.routes["/api/changes"] = browserChanges("C3", { since_start: true, restarted: true,
    entries: [{ lseq: 1, dir: "in", message_id: "n1", sender: CC_A, sender_name: "landish8", text: "new identity hello", at: T0 + 120 }] })
  await t.app.pollOnce()
  assert.ok(t.document.byId.notes.textContent.includes("The server restarted: new browser identity."))
  assert.ok(!stream().includes("late reply") && stream().includes("new identity hello"))
  assert.equal(Object.keys(t.app.state.browser.sends).length, 0)
})

test("chat: a direct send queued from a channel is followed in Browser; the lane counts pending ACKs", async () => {
  const t = chatBoot({ send: () => ({ status: 202, body: { message_id: MID3, state: "queued" } }) })
  await t.app.ready
  await t.app.selectChannel("ops")
  setBox(pickBoxes(t)[1], true)
  setBox(ackBox(t), true)
  typeText(t, "ping")
  assert.equal(await t.app.submitCompose(), true)
  assert.ok(statusLine(t).textContent.includes("status in Browser"))
  t.routes["/api/changes"] = browserChanges("C1", {
    entries: [{ lseq: 1, dir: "out", message_id: MID3, to: [CC_A], text: "ping", ack_requested: true, at: T0 }],
    sends: [sendRow(MID3, { to: [CC_A], state: "pending", journaled: true, seq: 2, recipients: { [CC_A]: { outcome: "transport accepted", ack: "pending" } } }),
      // A refused recipient, or one with a transport error, is not waiting for an ACK.
      sendRow(MID2, { to: [CODEX_B, OC_C], state: "pending", journaled: true, seq: 3, recipients: {
        [CODEX_B]: { outcome: "refused: codex is not on PATH", ack: "pending", transport_error: "codex is not on PATH" },
        [OC_C]: { outcome: "transport accepted", ack: "pending", unverified: true } } })],
  })
  await t.app.pollOnce()
  const lane = t.document.byId.lanes.childNodes[0]
  assert.ok(lane.textContent.includes("1 ACK pending"), lane.textContent)
  lane.dispatch("keydown", { key: "Enter" })
  assert.equal(t.app.state.view, "browser")
  assert.equal(t.document.byId.title.textContent, "Browser")
  assert.ok(textOf(t.document.byId.info).includes(WEB), "details show the browser peer's address")
})

// ---------------------------------------------------------------- chat mode: U5 review follow-ups

const ALL_WARN = "@all only works in #general: open #general to post it to every member"
const reasonNode = (t) => elementsOf(composer(t)).find((n) => n.attributes.class === "creason")

async function readyToSend(t, text = "please rebase") {
  await t.app.ready
  await t.app.selectChannel("ops")
  setBox(pickBoxes(t)[2], true)
  typeText(t, text)
}

test("chat: a refused token stops sending at once: Send disables and the reason shows, from a poll or any failed request", async () => {
  const t = chatBoot()
  await readyToSend(t)
  assert.equal(sendButton(t).disabled, false)
  t.routes["/api/changes"] = { __status: 401 }
  await t.app.pollOnce()
  assert.equal(t.app.state.stopped, true)
  assert.equal(sendButton(t).disabled, true, "no stale enabled Send after polling stops")
  assert.equal(textareaOf(t).disabled, true)
  assert.ok(composer(t).textContent.includes("Sending has stopped"), composer(t).textContent)
  assert.equal(await t.app.submitCompose(), false)
  assert.equal(t.sends.length, 0)
  // showError (here a refused search in Browser, which renders nothing else) stops it the same way.
  const u = chatBoot({ extraRoutes: { "/api/search": { __status: 403 } } })
  await u.app.ready
  u.app.showBrowser()
  setBox(pickBoxes(u)[0], true)
  typeText(u, "hello")
  assert.equal(sendButton(u).disabled, false)
  await u.app.runSearch("x", "all", null)
  assert.equal(sendButton(u).disabled, true)
  assert.ok(composer(u).textContent.includes("Sending has stopped"), composer(u).textContent)
})

test("chat: the composer's reason is linked by aria-describedby while it is blocked, and unlinked otherwise", async () => {
  const t = chatBoot()
  await t.app.ready
  assert.equal(reasonNode(t).id, "creason")
  assert.equal(textareaOf(t).getAttribute("aria-describedby"), null, "#general with nothing typed")
  typeText(t, "plain text")
  assert.equal(textareaOf(t).getAttribute("aria-describedby"), "creason", "#general, plain text")
  assert.equal(sendButton(t).getAttribute("aria-describedby"), "creason")
  typeText(t, "@all now")
  assert.equal(textareaOf(t).getAttribute("aria-describedby"), "creason", "#general, the @all warning")
  typeText(t, "")
  await t.app.selectChannel("ops")
  assert.equal(textareaOf(t).getAttribute("aria-describedby"), null)
  assert.equal(sendButton(t).getAttribute("aria-describedby"), null)
  assert.equal(reasonNode(t).textContent, "")
  typeText(t, "@all heads up")
  assert.equal(textareaOf(t).getAttribute("aria-describedby"), "creason", "@all")
  assert.equal(sendButton(t).getAttribute("aria-describedby"), "creason")
  typeText(t, "heads up")
  assert.equal(textareaOf(t).getAttribute("aria-describedby"), null)
  t.routes["/api/changes"] = { __status: 403 }
  await t.app.pollOnce()
  assert.equal(textareaOf(t).getAttribute("aria-describedby"), "creason", "stopped")
  assert.ok(reasonNode(t).textContent.startsWith("Sending has stopped"))
})

test("chat: @all text outside #general shows the note (.allnote), disables Send and is never sent; a 403 names #general and @all", async () => {
  const t = chatBoot()
  await readyToSend(t, "@all freeze merges")
  const warn = elementsOf(composer(t)).find((n) => n.attributes.class === "allnote")
  assert.ok(warn, "an .allnote note")
  assert.equal(warn.textContent, ALL_WARN)
  assert.equal(warn.parentNode, reasonNode(t), "inside the linked reason line")
  assert.equal(sendButton(t).disabled, true)
  assert.equal(textareaOf(t).disabled, false, "the text stays editable")
  assert.equal(await t.app.submitCompose(), false)
  textareaOf(t).dispatch("keydown", { key: "Enter", ctrlKey: true })
  await settle()
  assert.equal(t.sends.length, 0)
  for (const text of ["@all", "@all\tnow"]) {
    typeText(t, text)
    assert.equal(sendButton(t).disabled, true, JSON.stringify(text))
  }
  for (const text of ["freeze merges", "@allison please", " @all is mid-text"]) {
    typeText(t, text)
    assert.equal(sendButton(t).disabled, false, JSON.stringify(text))
    assert.ok(!elementsOf(composer(t)).some((n) => n.attributes.class === "allnote"), text)
  }
  assert.equal(t.app.sendErrorText(403), "Refused by the server (token, origin, #general or @all) (403)")
})

test("chat: after a button send's 202 focus returns to the textarea", async () => {
  const t = chatBoot()
  await readyToSend(t)
  sendButton(t).focus()
  sendButton(t).dispatch("click")
  await settle()
  assert.equal(t.sends.length, 1)
  assert.equal(sendButton(t).disabled, true)
  assert.equal(t.document.activeElement, textareaOf(t))
})

test("chat: a second submit while the first is in flight sends nothing", async () => {
  const t = chatBoot({ send: () => new Promise(() => {}) })
  await readyToSend(t)
  t.app.submitCompose()
  t.app.submitCompose()
  sendButton(t).dispatch("click")
  textareaOf(t).dispatch("keydown", { key: "Enter", ctrlKey: true })
  await settle()
  assert.equal(t.sends.length, 1)
  assert.equal(sendButton(t).textContent, "Sending…")
  assert.equal(sendButton(t).disabled, true)
})

test("chat: an 'ACK not requested' slot stays out of an ACK-requested row's tally", () => {
  const { app } = load()
  app.setState(chatState())
  const r = sendRow(MID1, { to: [CODEX_B, CC_A], state: "pending", ack_requested: true, recipients: {
    [CODEX_B]: { outcome: "transport accepted", ack: "pending" },
    [CC_A]: { outcome: "transport accepted", ack: "not requested" } } })
  assert.deepEqual(plain(app.rowTally(r)), { acked: 0, total: 1, unverified: 0 })
  const text = app.renderSendStatus(r).textContent
  assert.ok(text.includes("0 of 1 acknowledged"), text)
  assert.ok(text.includes("ACK not requested"), text)
})

test("chat: an unchecked Ask for ACK sends ack: false; ⌘+Enter (metaKey) sends", async () => {
  const t = chatBoot()
  await readyToSend(t, "no ack please")
  assert.ok(!ackBox(t).checked, "Ask for ACK starts unchecked")
  textareaOf(t).dispatch("keydown", { key: "Enter", metaKey: true })
  await settle()
  assert.equal(t.sends.length, 1)
  assert.deepEqual(t.sends[0].body, { to: [CODEX_B], text: "no ack please", ack: false })
})

test("chat: at most 64 recipients per send", async () => {
  const state = chatState()
  for (let i = 0; i < 63; i++) {
    const id = "cc:" + String(i).padStart(8, "0") + "-0000-4000-8000-000000000000"
    state.sessions.push({ member: id, address: id, runtime: "Claude Code", liveness: "live", state: "idle" })
  }
  const t = chatBoot({ state })
  await t.app.ready
  t.app.showBrowser()
  const boxes = pickBoxes(t)
  assert.equal(boxes.length, 65, "CC_A, CODEX_B and 63 more (the gone OpenCode session is not offered)")
  boxes.forEach((b) => setBox(b, true))
  typeText(t, "everyone, one by one")
  assert.equal(sendButton(t).disabled, true)
  assert.equal(statusLine(t).textContent, "At most 64 recipients per send.")
  assert.equal(await t.app.submitCompose(), false)
  assert.equal(t.sends.length, 0)
  setBox(boxes[64], false)
  assert.equal(sendButton(t).disabled, false)
  assert.equal(await t.app.submitCompose(), true)
  assert.equal(t.sends[0].body.to.length, 64)
})

test("chat: in Browser a new reply is announced politely; the rebuilt streams carry no live region", async () => {
  const t = chatBoot()
  await t.app.ready
  const reply = (cursor, lseq, text) => browserChanges(cursor, { entries: [{ lseq, dir: "in", message_id: "r" + lseq, sender: CODEX_B, sender_name: "cx", text, at: T0 + lseq }] })
  t.routes["/api/changes"] = reply("C1", 1, "not announced outside Browser")
  await t.app.pollOnce()
  assert.equal(t.document.getElementById("announce").textContent, "")
  t.app.showBrowser()
  t.routes["/api/changes"] = reply("C2", 2, "rebased,\n  ready for review")
  await t.app.pollOnce()
  assert.equal(t.document.getElementById("announce").textContent, "Reply from cx: rebased, ready for review")
  assert.equal(t.document.byId.stream.getAttribute("aria-live"), null)
})

// ---------------------------------------------------------------- chat mode: @all to #general (#162 M2)

const NONCE = "n".repeat(43)
const dialogHost = (t) => t.document.byId.dialoghost
const dialogOf = (t) => (dialogHost(t) ? elementsOf(dialogHost(t)).find((n) => n.attributes.role === "dialog") : null) || null
const dialogButton = (t, label) => elementsOf(dialogHost(t)).find((n) => n.tagName === "button" && n.textContent === label)
const okButton = (t) => elementsOf(dialogHost(t)).find((n) => n.tagName === "button" && (n.attributes.class || "").includes("dlgok"))
const dialogMsg = (t) => elementsOf(dialogHost(t)).find((n) => (n.attributes.class || "").startsWith("dlgmsg"))
const previewBody = (extra = {}) => Object.assign({ confirm: NONCE, expires_in: 60, recipients: [CC_A, CODEX_B], excluded: [{ member: OC_C, reason: "gone" }] }, extra)

// /api/send previews a body without `confirm` and answers a confirm from confirm(body, n).
function allBoot({ preview = () => ({ status: 200, body: previewBody() }), confirm = () => ({ status: 202, body: { message_id: MID1, state: "queued" } }), state } = {}) {
  let n = 0
  return chatBoot({ state, send: (init) => { const body = JSON.parse(init.body); return "confirm" in body ? confirm(body, ++n) : preview(body) } })
}

async function previewed(t, text = "@all freeze main") {
  await t.app.ready
  typeText(t, text)
  sendButton(t).dispatch("click")
  await settle()
}

function setNow(t, ms) { vm.runInContext("Date.now = function () { return " + Number(ms) + " }", t.context) }
// Runs the dialog's one-second countdown timers (polling uses 2 s and more).
function tickDialog(t) {
  const due = t.timers.filter((x) => x.ms === 1000)
  const rest = t.timers.filter((x) => x.ms !== 1000)
  t.timers.splice(0, t.timers.length, ...rest)
  due.forEach((x) => x.fn())
}

test("chat @all: a preview 200 opens an in-page dialog with names, typed ids, excluded members, the stale note and a countdown", async () => {
  const state = chatState()
  state.sessions[2].name = "cx-review"
  const t = allBoot({ state, preview: () => ({ status: 200, body: previewBody({ stale: true }) }) })
  await previewed(t)
  assert.equal(t.sends.length, 1)
  assert.deepEqual(t.sends[0].body, { to: "#general", text: "@all freeze main", ack: true }, "the preview: no nonce, ACK always")
  const dlg = dialogOf(t)
  assert.ok(dlg, "a role=dialog element")
  assert.equal(dlg.tagName, "div")
  assert.equal(dlg.attributes["aria-modal"], "true")
  assert.equal(dlg.attributes["aria-labelledby"], "dlgtitle")
  assert.equal(dlg.attributes["aria-describedby"], "dlgdesc dlgmsg")
  const ids = elementsOf(dlg).map((n) => n.id).filter(Boolean)
  for (const id of ["dlgtitle", "dlgdesc", "dlgmsg"]) assert.ok(ids.includes(id), id)
  const text = dlg.textContent
  for (const want of ["Post @all to #general?", "these 2 sessions", "freeze main", "Recipients (2)", "landish8", CC_A, "cx-review", CODEX_B,
    "Not included (1)", OC_C, "gone, not sent to", "Discovery may be stale", "Expires in 60 s"]) {
    assert.ok(text.includes(want), want + " in " + text)
  }
  assert.equal(elementsOf(dlg).find((n) => n.attributes["aria-label"] === "Recipients").childNodes.length, 2)
  assert.equal(okButton(t).textContent, "Send to 2")
  assert.equal(okButton(t).disabled, false)
  assert.equal(t.document.activeElement, dialogButton(t, "Cancel"), "focus moves into the dialog, on the safe choice")
  assert.equal(t.document.byId.app.inert, true, "the page behind is inert")
  assert.equal(sendButton(t).disabled, true, "no second preview while the dialog is open")
  assert.equal(await t.app.submitCompose(), false)
  assert.equal(t.sends.length, 1)
  // Without stale the note is absent.
  const u = allBoot()
  await previewed(u)
  assert.ok(!dialogOf(u).textContent.includes("stale"))
})

test("chat @all: Confirm sends the nonce with the same text; a 202 closes the dialog, shows the queued row and clears the text", async () => {
  const t = allBoot()
  await previewed(t)
  okButton(t).dispatch("click")
  await settle()
  assert.equal(t.sends.length, 2)
  assert.deepEqual(t.sends[1].body, { to: "#general", text: "@all freeze main", ack: true, confirm: NONCE })
  assert.equal(dialogOf(t), null, "closed")
  assert.equal(t.document.byId.app.inert, false)
  assert.equal(textareaOf(t).value, "")
  assert.equal(t.document.activeElement, textareaOf(t), "focus returns to the textarea")
  assert.match(statusLine(t).textContent, /^Accepted \d\d:\d\d — status in #general$/)
  const stream = t.document.byId.stream.textContent
  assert.ok(stream.includes("freeze main") && stream.includes("Queued (not yet recorded)"), stream)
  assert.equal(t.app.state.browser.sends[MID1].to, "#general")
  assert.equal(t.app.state.browser.sends[MID1].ack_requested, true)
})

test("chat @all: a 429 on confirm keeps the dialog open and the nonce, and Confirm again reuses it", async () => {
  const t = allBoot({ confirm: (_b, n) => (n === 1 ? { status: 429 } : { status: 202, body: { message_id: MID2, state: "queued" } }) })
  await previewed(t)
  okButton(t).dispatch("click")
  await settle()
  assert.ok(dialogOf(t), "still open")
  assert.equal(dialogMsg(t).textContent, "Rate limited, try again")
  assert.equal(dialogMsg(t).attributes.role, "status")
  assert.equal(okButton(t).disabled, false, "Confirm stays available")
  assert.equal(t.document.activeElement, okButton(t))
  assert.equal(textareaOf(t).value, "@all freeze main")
  okButton(t).dispatch("click")
  await settle()
  assert.equal(t.sends.length, 3)
  assert.equal(t.sends[2].body.confirm, NONCE, "the same nonce")
  assert.equal(t.sends[1].body.confirm, NONCE)
  assert.equal(dialogOf(t), null)
  assert.ok(t.app.state.browser.sends[MID2])
  // No answer at all keeps it open too (a used nonce is refused, so a retry never sends twice).
  const u = allBoot({ confirm: () => { throw new TypeError("Failed to fetch") } })
  await previewed(u)
  okButton(u).dispatch("click")
  await settle()
  assert.ok(dialogOf(u))
  assert.ok(dialogMsg(u).textContent.startsWith("No response from the server"))
})

test("chat @all: a 409 on confirm (expired or used) closes the dialog with the message and keeps the text", async () => {
  const t = allBoot({ confirm: () => ({ status: 409 }) })
  await previewed(t)
  okButton(t).dispatch("click")
  await settle()
  assert.equal(dialogOf(t), null)
  assert.equal(statusLine(t).textContent, "The preview expired or was used; preview again")
  assert.ok(statusLine(t).classList.contains("bad"))
  assert.equal(textareaOf(t).value, "@all freeze main")
  assert.equal(t.document.activeElement, textareaOf(t))
  assert.equal(sendButton(t).disabled, false, "Preview recipients again")
  assert.equal(Object.keys(t.app.state.browser.sends).length, 0)
  // Another refusal closes it with the usual message.
  const u = allBoot({ confirm: () => ({ status: 403 }) })
  await previewed(u)
  okButton(u).dispatch("click")
  await settle()
  assert.equal(dialogOf(u), null)
  assert.ok(statusLine(u).textContent.includes("(403)"))
})

test("chat @all: a 409 after an unanswered confirm warns it may already have been sent", async () => {
  const t = allBoot({ confirm: (_b, n) => (n === 1 ? (() => { throw new TypeError("Failed to fetch") })() : { status: 409 }) })
  await previewed(t)
  okButton(t).dispatch("click")
  await settle()
  assert.ok(dialogOf(t), "still open after no response")
  okButton(t).dispatch("click")
  await settle()
  assert.equal(dialogOf(t), null)
  assert.equal(statusLine(t).textContent, "It may already have been sent: check #general before previewing again.")
  assert.ok(statusLine(t).classList.contains("bad"))
})

const MAYBE_SENT = "It may already have been sent: check #general before previewing again."
const noResponse = () => { throw new TypeError("Failed to fetch") }

test("chat @all: Cancel or Esc after an unanswered confirm warns it may already have been sent", async () => {
  for (const close of [(t) => dialogButton(t, "Cancel").dispatch("click"), (t) => dialogOf(t).dispatch("keydown", { key: "Escape" })]) {
    const t = allBoot({ confirm: noResponse })
    await previewed(t)
    okButton(t).dispatch("click")
    await settle()
    assert.ok(dialogOf(t), "still open after no response")
    close(t)
    assert.equal(dialogOf(t), null)
    assert.equal(statusLine(t).textContent, MAYBE_SENT)
    assert.ok(statusLine(t).classList.contains("bad"))
  }
})

test("chat @all: any other refusal after an unanswered confirm still warns it may already have been sent", async () => {
  const t = allBoot({ confirm: (_b, n) => (n === 1 ? noResponse() : { status: 403 }) })
  await previewed(t)
  okButton(t).dispatch("click")
  await settle()
  okButton(t).dispatch("click")
  await settle()
  assert.equal(dialogOf(t), null)
  assert.equal(statusLine(t).textContent, MAYBE_SENT)
})

test("chat @all: an expiry after an unanswered confirm warns it may already have been sent", async () => {
  const t = allBoot({ confirm: noResponse })
  const base = Date.parse("2026-11-07T12:00:00Z")
  setNow(t, base)
  await previewed(t)
  okButton(t).dispatch("click")
  await settle()
  setNow(t, base + 60000)
  tickDialog(t)
  assert.equal(okButton(t).disabled, true)
  assert.equal(dialogMsg(t).textContent, "This preview expired, and an earlier confirm got no response: it may already have been sent. Check #general before previewing again.")
})

test("chat @all: the dialog opens at its top, and a click on the scrim keeps focus in the dialog for Esc", async () => {
  const t = allBoot()
  await previewed(t)
  assert.equal(dialogOf(t).scrollTop, 0, "the panel is reset to its top")
  assert.ok(dialogButton(t, "Cancel").focusOptions && dialogButton(t, "Cancel").focusOptions.preventScroll === true, "Cancel is focused without scrolling to it")
  const scrim = dialogOf(t).parentNode
  textareaOf(t).focus()
  let prevented = false
  scrim.dispatch("mousedown", { target: dialogOf(t), preventDefault() { prevented = true } })
  assert.equal(prevented, false, "a click inside the panel is left alone")
  scrim.dispatch("mousedown", { target: scrim, preventDefault() { prevented = true } })
  assert.equal(prevented, true)
  assert.equal(t.document.activeElement, dialogOf(t))
})

test("chat @all: a preview 409, 429 or 403 says why and opens no dialog", async () => {
  for (const [status, want] of [[409, "Couldn't list the recipients (discovery pending or incomplete, or none or too many). Trying again may help, but a #general with more than 64 members is normally refused."],
    [429, "Too fast or queue full — try again (429)"], [403, "(403)"]]) {
    const t = allBoot({ preview: () => ({ status }) })
    await previewed(t)
    assert.equal(dialogOf(t), null, String(status))
    assert.ok(statusLine(t).textContent.includes(want), status + ": " + statusLine(t).textContent)
    assert.equal(textareaOf(t).value, "@all freeze main")
    assert.equal(sendButton(t).textContent, "Preview recipients")
  }
  // A 200 whose body cannot be used opens nothing either.
  const t = allBoot({ preview: () => ({ status: 200, body: { confirm: NONCE, expires_in: 60, recipients: [] } }) })
  await previewed(t)
  assert.equal(dialogOf(t), null)
  assert.ok(statusLine(t).textContent.includes("could not be read"))
  const { app } = load()
  for (const bad of [null, {}, previewBody({ confirm: "" }), previewBody({ confirm: "x".repeat(129) }), previewBody({ expires_in: 0 }),
    previewBody({ expires_in: "60" }), previewBody({ recipients: "cc:x" })]) {
    assert.equal(app.readPreview(bad), null, JSON.stringify(bad))
  }
  assert.equal(app.readPreview(previewBody({ excluded: [null, { member: 5 }, { member: OC_C, reason: "gone" }] })).excluded.length, 1)
})

test("chat @all: reaching the expiry disables Confirm and offers Preview again", async () => {
  const t = allBoot({ preview: (b) => ({ status: 200, body: previewBody({ confirm: "nonce-" + b.text.length + "-" + t.sends.length }) }) })
  const base = Date.parse("2026-11-07T12:00:00Z")
  setNow(t, base)
  await previewed(t)
  assert.ok(dialogOf(t).textContent.includes("Expires in 60 s"))
  setNow(t, base + 15400)
  tickDialog(t)
  assert.ok(dialogOf(t).textContent.includes("Expires in 45 s"), dialogOf(t).textContent)
  okButton(t).focus()
  setNow(t, base + 60000)
  tickDialog(t)
  assert.equal(okButton(t).disabled, true, "Confirm disabled at the expiry")
  assert.ok(dialogOf(t).textContent.includes("Expired"))
  assert.equal(dialogMsg(t).textContent, "This preview expired. Preview again for a fresh recipient list.")
  const again = dialogButton(t, "Preview again")
  assert.ok(again, "Preview again offered")
  assert.equal(t.document.activeElement, again, "focus leaves the disabled Confirm")
  okButton(t).dispatch("click")
  await settle()
  assert.equal(t.sends.length, 1, "an expired preview is never confirmed")
  assert.equal(t.timers.filter((x) => x.ms === 1000).length, 0, "the countdown stops")
  again.dispatch("click")
  await settle()
  assert.equal(t.sends.length, 2)
  assert.ok(!("confirm" in t.sends[1].body), "a fresh preview")
  assert.ok(dialogOf(t), "a new dialog")
  assert.equal(okButton(t).disabled, false)
})

test("chat @all: Confirm is guarded against a double submit; Esc and Cancel wait while it is in flight", async () => {
  const t = allBoot({ confirm: () => new Promise(() => {}) })
  await previewed(t)
  okButton(t).focus()
  okButton(t).dispatch("click")
  okButton(t).dispatch("click")
  await settle()
  okButton(t).dispatch("click")
  await settle()
  assert.equal(t.sends.filter((s) => "confirm" in s.body).length, 1)
  assert.equal(okButton(t).textContent, "Sending…")
  assert.equal(okButton(t).disabled, true)
  assert.equal(dialogButton(t, "Cancel").disabled, true)
  assert.equal(t.document.activeElement, dialogOf(t), "focus stays in the dialog while its buttons are disabled")
  dialogOf(t).dispatch("keydown", { key: "Escape" })
  assert.ok(dialogOf(t), "Esc does not drop an in-flight confirm")
})

test("chat @all: Esc cancels and returns focus to the textarea; Tab stays inside the dialog", async () => {
  const t = allBoot()
  await previewed(t)
  const cancel = dialogButton(t, "Cancel")
  const ok = okButton(t)
  const lists = elementsOf(dialogOf(t)).filter((n) => (n.attributes.class || "").startsWith("dlglist"))
  assert.equal(lists.length, 2)
  for (const l of lists) assert.equal(l.attributes.tabindex, "0", "a capped list is keyboard-scrollable")
  const quote = elementsOf(dialogOf(t)).find((n) => n.attributes.class === "dlgquote")
  assert.ok(quote, "the quoted message")
  assert.equal(quote.attributes.tabindex, "0", "a capped quote is keyboard-scrollable")
  assert.equal(quote.attributes["aria-label"], "Message")
  const order = elementsOf(dialogOf(t))
  assert.ok(order.indexOf(quote) < order.indexOf(lists[0]), "the quote comes before the lists")
  ok.focus()
  dialogOf(t).dispatch("keydown", { key: "Tab" })
  assert.ok(t.document.activeElement === quote, "Tab from the last button wraps to the first stop, the quote")
  dialogOf(t).dispatch("keydown", { key: "Tab", shiftKey: true })
  assert.ok(t.document.activeElement === ok, "Shift+Tab from the first stop wraps to the last")
  cancel.focus()
  let prevented = false
  dialogOf(t).dispatch("keydown", { key: "Tab", shiftKey: true, preventDefault() { prevented = true } })
  assert.equal(prevented, false, "Shift+Tab from Cancel is left to the browser, which lands on the last list or the quote before it")
  assert.ok(t.document.activeElement === cancel)
  dialogOf(t).dispatch("keydown", { key: "Escape" })
  assert.equal(dialogOf(t), null)
  assert.equal(statusLine(t).textContent, "", "a plain Esc (no unanswered confirm) leaves no warning")
  assert.equal(t.document.activeElement, textareaOf(t))
  assert.equal(t.document.byId.app.inert, false)
  assert.equal(textareaOf(t).value, "@all freeze main", "the text stays")
  assert.equal(t.sends.length, 1, "nothing confirmed")
  await previewed(t)
  dialogButton(t, "Cancel").dispatch("click")
  assert.equal(dialogOf(t), null)
  assert.equal(t.sends.filter((s) => "confirm" in s.body).length, 0)
})

test("chat @all: a hostile recipient name and message render only as text in the dialog", async () => {
  const state = chatState()
  state.sessions[2].name = EVIL
  const t = allBoot({ state })
  await previewed(t, "@all " + EVIL)
  const dlg = dialogOf(t)
  const text = dlg.textContent
  assert.ok(text.includes(EVIL), "the name, as text")
  assert.ok(text.split(EVIL).length - 1 >= 2, "and the message, as text")
  for (const node of elementsOf(dialogHost(t))) {
    assert.notEqual(node.tagName, "img")
    for (const name of Object.keys(node.attributes)) assert.ok(!/^on/i.test(name) && !["src", "href", "style"].includes(name), name)
  }
  assert.deepEqual(t.document.htmlWrites, [])
})

test("chat @all: #general's details hold the corrected 'How @all works' card", async () => {
  const t = chatBoot()
  await t.app.ready
  const info = textOf(t.document.byId.info)
  assert.ok(info.includes("How @all works"))
  for (const want of ["The post goes to the #general members discovery found.",
    "Preview recipients lists them; nothing is sent until you confirm that list.",
    "Gone or refused members, idle OpenCode sessions (not woken) and discovery gaps show as their recorded outcomes.",
    "If membership changes between your preview and the send, nothing is sent and you are asked to preview again."]) {
    assert.ok(info.includes(want), want)
  }
  assert.ok(!/every (live )?session gets/i.test(info) && !info.includes("queues one turn in every"), "not the mock's too-strong wording")
  await t.app.selectChannel("ops")
  assert.ok(!textOf(t.document.byId.info).includes("How @all works"), "only in #general")
  // Read-only: the card stays, without the preview step this page cannot take.
  const { app } = load()
  app.setState(stateFixture())
  const ro = textOf(app.renderDetails(app.state.rows.general))
  assert.ok(ro.includes("How @all works") && ro.includes("discovery found"))
  assert.ok(!ro.includes("preview"))
})

test("chat @all: the browser's @all shows a live ACK tally on its row and on its #general history entry", async () => {
  const t = allBoot()
  await previewed(t)
  okButton(t).dispatch("click")
  await settle()
  const stream = () => t.document.byId.stream
  t.routes["/api/changes"] = browserChanges("C1", {
    sends: [sendRow(MID1, { kind: "channel", to: "#general", channel: "general", text: "@all freeze main", state: "pending", journaled: true, delivering: true, seq: 4,
      recipients: { [CC_A]: { outcome: "transport accepted", ack: "acked" }, [CODEX_B]: { outcome: "transport accepted", ack: "pending" },
        [OC_C]: { outcome: "refused: gone", ack: "not requested" } } })],
  })
  await t.app.pollOnce()
  const row = stream().textContent
  assert.ok(row.includes("1 of 2 acknowledged"), row)
  const chips = elementsOf(stream()).filter((n) => n.parentNode && n.parentNode.attributes && n.parentNode.attributes.class === "tally")
  assert.deepEqual(chips.map((c) => [c.textContent, c.attributes.class]), [["landish8", "yes"], [nameOfShort(CODEX_B), "no"]], "refused stays out")
  // A repeat of the row changes nothing; the ACK arriving moves the tally.
  t.routes["/api/changes"] = browserChanges("C2", {
    sends: [sendRow(MID1, { kind: "channel", to: "#general", channel: "general", text: "@all freeze main", state: "pending", journaled: true, delivering: false, seq: 5,
      recipients: { [CC_A]: { outcome: "transport accepted", ack: "acked" }, [CODEX_B]: { outcome: "transport accepted", ack: "acked" },
        [OC_C]: { outcome: "refused: gone", ack: "not requested" } } })],
  })
  await t.app.pollOnce()
  assert.ok(stream().textContent.includes("2 of 2 acknowledged"))
  // The history entry replaces the row and carries its own tally.
  const posted = sendEntry(2, "@all freeze main", { message_id: MID1, sender: WEB, actor: WEB, ts: new Date((T0 + 5) * 1000).toISOString(),
    delivery: { state: "complete", recipients: [row2(CC_A, "acked", "acked"), row2(CODEX_B, "transport accepted", "pending"), row2(OC_C, "refused: gone", "not requested")] } })
  t.routes["/api/changes"] = quiet("C3", { channels: { general: { entries: [posted], gap: null } } })
  await t.app.pollOnce()
  const after = stream().textContent
  assert.equal(after.split("freeze main").length - 1, 1, "the row gave way to the entry")
  assert.ok(after.includes("1 of 2 acknowledged"), after)
  assert.ok(after.includes("browser-7c0e"), "sent by the browser peer")
  t.routes["/api/changes"] = quiet("C4", { updates: [{ message_id: MID1, recipient: CODEX_B, ack: "acked" }] })
  await t.app.pollOnce()
  assert.ok(stream().textContent.includes("2 of 2 acknowledged"))
})

function row2(recipient, state, ack) { return { recipient, state, ack } }
function nameOfShort(typed) { const at = typed.indexOf(":"); const id = typed.slice(at + 1); return typed.slice(0, at + 1) + id.slice(0, 4) + "…" + id.slice(-4) }

// ---------------------------------------------------------------- channel labels and rail order (#175 m3)

const chRow = (key, extra = {}) => Object.assign({ key, name: key, display: key, kind: "named", owner: CC_A,
  members: [{ typed: CC_A, runtime: "Claude Code", state: "working", name: "landish8" }], handover_chain: [], alert: null, coverage: {} }, extra)
const lblRow = (key, label, activity, last, extra = {}) => chRow(key, Object.assign({ label, label_status: "current", activity, last_post_ts: last || null }, extra))
const laneNames = (t) => elementsOf(t.document.byId.lanes).filter((n) => n.attributes.class === "lanename").map((n) => n.textContent)
const laneOf = (t, key) => elementsOf(t.document.byId.lanes).find((n) => n.attributes["data-focus"] === "lane:" + key)
const orderState = (rows) => {
  const state = stateFixture()
  state.channels = [Object.assign({}, state.channels[0], { label: "#general", label_status: "current", activity: "posted", last_post_ts: "2026-01-01T00:00:00+00:00" })].concat(rows)
  return state
}
const stampAt = (secondsAgo) => new Date(Date.now() - secondsAgo * 1000).toISOString()

test("labels: the rail and the header show the server label, with markup as text", async () => {
  const hostile = "#<img src=x onerror=alert(1)>"
  const t = bootFixture({ "/api/state": orderState([lblRow("pair1", "#fire·dc10", "none"), lblRow("pair2", hostile, "none")]),
    "/api/channel/pair1": pageFixture("pair1", [sendEntry(1, "hi")]) })
  await t.app.ready
  assert.deepEqual(laneNames(t), ["#general", hostile, "#fire·dc10"])
  assert.ok(!elementsOf(t.document.byId.lanes).some((n) => n.tagName === "img"))
  await t.app.selectChannel("pair1")
  assert.equal(t.document.byId.title.textContent, "#fire·dc10")
  assert.ok(t.document.byId.info.textContent.includes("#fire·dc10"))
  assert.deepEqual(t.document.htmlWrites, [])
})

test("labels: stale and unverified pairs carry distinct visible markers, a current one none", async () => {
  const t = bootFixture({ "/api/state": orderState([
    lblRow("a", "#lead", "none", null, { label_status: "stale" }),
    lblRow("b", "#lead-2", "none", null, { label_status: "unverified" }),
    lblRow("c", "#fine", "none")]) })
  await t.app.ready
  const marks = (key) => elementsOf(laneOf(t, key)).filter((n) => (n.attributes.class || "").startsWith("lblmark")).map((n) => n.textContent)
  assert.deepEqual(marks("a"), ["stale (name may be out of date)"])
  assert.deepEqual(marks("b"), ["pair unverified (the pair behind this name could not be verified)"])
  assert.deepEqual(marks("c"), [])
})

test("rail: activity unknown is its own visible note, never shown for none or posted", async () => {
  const t = bootFixture({ "/api/state": orderState([lblRow("a", "#a", "unknown"), lblRow("b", "#b", "none"), lblRow("c", "#c", "posted", stampAt(10))]) })
  await t.app.ready
  assert.ok(laneOf(t, "a").textContent.includes("activity unknown"))
  assert.ok(!laneOf(t, "b").textContent.includes("activity unknown"))
  assert.ok(!laneOf(t, "c").textContent.includes("activity unknown"))
})

test("rail order: #general, posted newest first, none, unknown last; ties by label (code points) then key; the state is not reordered", async () => {
  const state = orderState([
    lblRow("k-unk", "#zz", "unknown"),
    chRow("k-unreadable", { unreadable: true, alert: "unverified", members: [] }),
    lblRow("k-none", "#none", "none"),
    lblRow("k-old", "#old", "posted", "2026-02-01T00:00:00+00:00"),
    lblRow("k2", "#alpha", "posted", "2026-03-01T00:00:00+00:00"),
    lblRow("k1", "#beta", "posted", "2026-03-01T00:00:00+00:00"),
    lblRow("k0", "#\u{1F600}", "none"),
    lblRow("k-bmp", "#Ａ", "none"),
  ])
  const t = bootFixture({ "/api/state": state })
  await t.app.ready
  const keys = elementsOf(t.document.byId.lanes).filter((n) => (n.attributes["data-focus"] || "").startsWith("lane:")).map((n) => n.attributes["data-focus"].slice(5))
  // none ties by code point: "#none" < "#Ａ" < "#\u{1F600}" (a UTF-16 compare would put the emoji before Ａ).
  // Unknown rows last, by "#" + key against label: "#k-unreadable" < "#zz".
  assert.deepEqual(keys, ["general", "k2", "k1", "k-old", "k-none", "k-bmp", "k0", "k-unreadable", "k-unk"])
  assert.deepEqual(t.app.state.state.channels.map((r) => r.key).slice(0, 3), ["general", "k-unk", "k-unreadable"])
})

test("rail order: an unreadable row among unknown rows ties as '#' + key", async () => {
  const t = bootFixture({ "/api/state": orderState([lblRow("u2", "#m", "unknown"), chRow("aaa", { unreadable: true, members: [] }), lblRow("u1", "#zzz", "unknown")]) })
  await t.app.ready
  assert.deepEqual(laneNames(t), ["#general", "#aaa", "#m", "#zzz"])
})

test("rail order: equal posts tie by label even when the key order differs", async () => {
  const same = "2026-03-01T00:00:00+00:00"
  const t = bootFixture({ "/api/state": orderState([lblRow("a-key", "#zebra", "posted", same), lblRow("z-key", "#apple", "posted", same)]) })
  await t.app.ready
  assert.deepEqual(laneNames(t), ["#general", "#apple", "#zebra"])
})

function pollOrderBoot() {
  return bootFixture({ "/api/state": orderState([
    lblRow("a", "#a", "posted", "2026-03-01T00:00:00+00:00"),
    lblRow("b", "#b", "posted", "2026-02-01T00:00:00+00:00"),
    lblRow("c", "#c", "none")]) })
}

test("poll: a send moves its channel to the top; a membership entry does not", async () => {
  const t = pollOrderBoot()
  await t.app.ready
  assert.deepEqual(laneNames(t), ["#general", "#a", "#b", "#c"])
  t.routes["/api/changes"] = quiet("C1", { channels: { c: { entries: [{ seq: 5, ts: stampAt(1), kind: "join", event: "channel.join", actor: CC_A }], gap: null } } })
  await t.app.pollOnce()
  assert.deepEqual(laneNames(t), ["#general", "#a", "#b", "#c"])
  t.routes["/api/changes"] = quiet("C2", { channels: { c: { entries: [sendEntry(6, "now", { ts: stampAt(5) })], gap: null } } })
  await t.app.pollOnce()
  assert.deepEqual(laneNames(t), ["#general", "#c", "#a", "#b"])
  assert.equal(t.app.state.state.channels[1].key, "a", "state.channels keeps the server order")
})

test("poll: an oversized placeholder alone, or after a send, makes the channel unknown; a later send restores it", async () => {
  const t = pollOrderBoot()
  await t.app.ready
  t.routes["/api/changes"] = quiet("C1", { channels: { a: { entries: [{ seq: null, kind: "oversized", oversized: true }], gap: null } } })
  await t.app.pollOnce()
  assert.deepEqual(laneNames(t), ["#general", "#b", "#c", "#a"])
  assert.ok(laneOf(t, "a").textContent.includes("activity unknown"))
  t.routes["/api/changes"] = quiet("C2", { channels: { b: { entries: [sendEntry(7, "x", { ts: stampAt(3) }), { seq: 8, kind: "oversized", oversized: true }], gap: null } } })
  await t.app.pollOnce()
  assert.deepEqual(laneNames(t), ["#general", "#c", "#a", "#b"])
  t.routes["/api/changes"] = quiet("C3", { channels: { b: { entries: [{ seq: 8, kind: "oversized", oversized: true }, sendEntry(9, "y", { ts: stampAt(2) })], gap: null } } })
  await t.app.pollOnce()
  assert.deepEqual(laneNames(t), ["#general", "#b", "#c", "#a"])
})

test("poll: activity_unknown flips a known channel; a send stamped past the skew is unknown", async () => {
  const t = pollOrderBoot()
  await t.app.ready
  t.routes["/api/changes"] = quiet("C1", { activity_unknown: ["a"] })
  await t.app.pollOnce()
  assert.deepEqual(laneNames(t), ["#general", "#b", "#c", "#a"])
  assert.ok(laneOf(t, "a").textContent.includes("activity unknown"))
  const ahead = new Date(Date.now() + 400 * 1000).toISOString()
  t.routes["/api/changes"] = quiet("C2", { channels: { c: { entries: [sendEntry(6, "future", { ts: ahead })], gap: null } } })
  await t.app.pollOnce()
  assert.deepEqual(laneNames(t), ["#general", "#b", "#a", "#c"])
  assert.ok(laneOf(t, "c").textContent.includes("activity unknown"))
  const near = new Date(Date.now() + 100 * 1000).toISOString()
  t.routes["/api/changes"] = quiet("C3", { channels: { c: { entries: [sendEntry(7, "skewed", { ts: near })], gap: null } } })
  await t.app.pollOnce()
  assert.equal(laneNames(t)[1], "#c", "within the skew a send counts")
})

test("poll: a reorder keeps the selected lane current and scrolls it into view", async () => {
  const t = pollOrderBoot()
  await t.app.ready
  await t.app.selectChannel("c")
  const calls = []
  assert.equal(laneOf(t, "c").attributes["aria-current"], "true")
  t.routes["/api/changes"] = quiet("C1", { channels: { b: { entries: [sendEntry(6, "z", { ts: stampAt(1) })], gap: null } } })
  const realCreate = t.document.createElement
  t.document.createElement = (tag) => { const n = realCreate(tag); n.scrollIntoView = (o) => calls.push([n, o]); return n }
  await t.app.pollOnce()
  t.document.createElement = realCreate
  assert.deepEqual(laneNames(t), ["#general", "#b", "#a", "#c"])
  assert.equal(laneOf(t, "c").attributes["aria-current"], "true")
  assert.equal(calls.length, 1)
  assert.ok(calls[0][0] === laneOf(t, "c"))
  assert.deepEqual(plain(calls[0][1]), { block: "nearest" })
})

test("chat: the composer posts to the key, not the label, for a channel labelled #fire·dc10", async () => {
  const state = chatState()
  state.channels.push(lblRow("pairkey", "#fire·dc10", "none"))
  const t = chatBoot({ state, extraRoutes: { "/api/channel/pairkey": pageFixture("pairkey", [sendEntry(1, "hi")]) } })
  await t.app.ready
  await t.app.selectChannel("pairkey")
  assert.ok(composer(t).textContent.includes("Post to #fire·dc10"))
  setBox(pickBoxes(t)[0], true)
  typeText(t, "status please")
  sendButton(t).dispatch("click")
  await settle()
  assert.equal(t.sends[0].body.to, "#pairkey")
})

test("labels: the lane's activity note has no role of its own, so the lane name keeps it", async () => {
  const t = bootFixture({ "/api/state": orderState([lblRow("a", "#a", "unknown")]) })
  await t.app.ready
  const lane = laneOf(t, "a")
  assert.ok(lane.textContent.includes("activity unknown"))
  assert.ok(!elementsOf(lane).some((n) => n.attributes.role === "note"))
})

test("rail: no activity note on #general or an unreadable row, whatever their activity says", async () => {
  const state = orderState([chRow("u", { unreadable: true, activity: "unknown", members: [] })])
  state.channels[0].activity = "unknown"
  const t = bootFixture({ "/api/state": state })
  await t.app.ready
  assert.ok(!laneOf(t, "general").textContent.includes("activity unknown"))
  assert.ok(!laneOf(t, "u").textContent.includes("activity unknown"))
})

test("labels: the header and the details heading carry the stale and unverified markers too", async () => {
  const t = bootFixture({ "/api/state": orderState([
    lblRow("a", "#lead", "none", null, { label_status: "stale" }),
    lblRow("b", "#lead-2", "none", null, { label_status: "unverified" }),
    lblRow("c", "#fine", "none")]),
  "/api/channel/a": pageFixture("a", [sendEntry(1, "x")]), "/api/channel/b": pageFixture("b", [sendEntry(1, "x")]),
  "/api/channel/c": pageFixture("c", [sendEntry(1, "x")]) })
  await t.app.ready
  await t.app.selectChannel("a")
  assert.ok(t.document.byId.title.textContent.includes("stale"))
  assert.ok(t.document.byId.info.textContent.includes("stale"))
  await t.app.selectChannel("b")
  assert.ok(t.document.byId.title.textContent.includes("pair unverified"))
  assert.ok(t.document.byId.info.textContent.includes("pair unverified"))
  await t.app.selectChannel("c")
  assert.ok(!t.document.byId.title.textContent.includes("unverified") && !t.document.byId.title.textContent.includes("stale"))
})

test("rail order: sort stamps read like the server (numeric strings, milliseconds, naive ISO as UTC, junk as 0, huge first)", async () => {
  const t = bootFixture({ "/api/state": orderState([
    lblRow("junk", "#a-junk", "posted", "not a time"),
    lblRow("ms", "#b-ms", "posted", 5e11),
    lblRow("num", "#c-num", "posted", "1700000000"),
    lblRow("naive", "#d-naive", "posted", "2025-01-01T00:00:00"),
    lblRow("huge", "#e-huge", "posted", 1e300)]) })
  await t.app.ready
  assert.deepEqual(laneNames(t), ["#general", "#e-huge", "#d-naive", "#c-num", "#b-ms", "#a-junk"])
})

test("poll: a send with a huge or non-finite stamp makes the channel unknown, not posted", async () => {
  const t = pollOrderBoot()
  await t.app.ready
  t.routes["/api/changes"] = quiet("C1", { channels: { c: { entries: [sendEntry(6, "far", { ts: 1e300 })], gap: null } } })
  await t.app.pollOnce()
  assert.ok(laneOf(t, "c").textContent.includes("activity unknown"))
  t.routes["/api/changes"] = quiet("C2", { channels: { b: { entries: [sendEntry(7, "ms", { ts: Date.now() })], gap: null } } })
  await t.app.pollOnce()
  assert.equal(laneNames(t)[1], "#b", "a millisecond stamp counts as now")
})

test("poll: the newest send by position sets the stamp even when older than the known one", async () => {
  const t = bootFixture({ "/api/state": orderState([
    lblRow("a", "#a", "posted", stampAt(10)), lblRow("b", "#b", "posted", stampAt(50))]) })
  await t.app.ready
  assert.deepEqual(laneNames(t), ["#general", "#a", "#b"])
  t.routes["/api/changes"] = quiet("C1", { channels: { a: { entries: [sendEntry(6, "old", { ts: stampAt(500) })], gap: null } } })
  await t.app.pollOnce()
  assert.deepEqual(laneNames(t), ["#general", "#b", "#a"])
})

test("refresh: a state refresh redraws the selected channel's header when its label or marker changed", async () => {
  const t = bootFixture({ "/api/state": orderState([lblRow("a", "#old", "none")]), "/api/channel/a": pageFixture("a", [sendEntry(1, "x")]) })
  await t.app.ready
  await t.app.selectChannel("a")
  assert.equal(t.document.byId.title.textContent, "#old")
  t.routes["/api/state"] = orderState([lblRow("a", "#renamed", "none", null, { label_status: "stale" })])
  t.routes["/api/changes"] = quiet("C1", { channels: { a: { new: true } } })
  await t.app.pollOnce()
  assert.ok(t.document.byId.title.textContent.includes("#renamed"))
  assert.ok(t.document.byId.title.textContent.includes("stale"))
  assert.ok(!t.document.byId.title.textContent.includes("#old"))
})

test("refresh: a state refresh that reorders the rail scrolls the selected lane into view once", async () => {
  const t = pollOrderBoot()
  await t.app.ready
  await t.app.selectChannel("c")
  const calls = []
  t.routes["/api/state"] = orderState([
    lblRow("a", "#a", "posted", "2026-03-01T00:00:00+00:00"),
    lblRow("b", "#b", "posted", "2026-02-01T00:00:00+00:00"),
    lblRow("c", "#c", "posted", "2026-04-01T00:00:00+00:00"),
    lblRow("zz", "#zz", "none")])
  t.routes["/api/changes"] = quiet("C1", { channels: { zz: { new: true } } })
  const realCreate = t.document.createElement
  t.document.createElement = (tag) => { const n = realCreate(tag); n.scrollIntoView = (o) => calls.push([n, o]); return n }
  await t.app.pollOnce()
  t.document.createElement = realCreate
  assert.deepEqual(laneNames(t), ["#general", "#c", "#a", "#b", "#zz"])
  assert.equal(calls.length, 1)
  assert.ok(calls[0][0] === laneOf(t, "c"))
})

test("refresh: the server's order replaces what polls inferred, and an activity change without a reorder does not scroll", async () => {
  const t = pollOrderBoot()
  await t.app.ready
  await t.app.selectChannel("c")
  t.routes["/api/changes"] = quiet("C1", { channels: { c: { entries: [sendEntry(6, "z", { ts: stampAt(1) })], gap: null } } })
  await t.app.pollOnce()
  assert.deepEqual(laneNames(t), ["#general", "#c", "#a", "#b"])
  t.routes["/api/changes"] = quiet("C2", { channels: { zz: { new: true } } })
  t.routes["/api/state"] = orderState([
    lblRow("a", "#a", "posted", "2026-03-01T00:00:00+00:00"),
    lblRow("b", "#b", "posted", "2026-02-01T00:00:00+00:00"),
    lblRow("c", "#c", "none"), lblRow("zz", "#zz", "none")])
  await t.app.pollOnce()
  assert.deepEqual(laneNames(t), ["#general", "#a", "#b", "#c", "#zz"])
  const calls = []
  const realCreate = t.document.createElement
  t.document.createElement = (tag) => { const n = realCreate(tag); n.scrollIntoView = (o) => calls.push([n, o]); return n }
  t.routes["/api/changes"] = quiet("C3", { activity_unknown: ["zz"] })
  await t.app.pollOnce()
  t.document.createElement = realCreate
  assert.ok(laneOf(t, "zz").textContent.includes("activity unknown"))
  assert.deepEqual(laneNames(t), ["#general", "#a", "#b", "#c", "#zz"])
  assert.equal(calls.length, 0)
})

const orderOf = async (stamps) => {
  const rows = stamps.map((ts, i) => lblRow("r" + i, "#r" + i, "posted", ts))
  const t = bootFixture({ "/api/state": orderState(rows) })
  await t.app.ready
  return laneNames(t).slice(1)
}

test("rail order: a long digit string sorts as 0 in linear time", async () => {
  const hostile = "9".repeat(200000) + "x"
  const started = Date.now()
  const names = await orderOf([hostile, "1700000000"])
  assert.ok(Date.now() - started < 500, "took " + (Date.now() - started) + " ms")
  assert.deepEqual(names, ["#r1", "#r0"])
})

test("rail order: infinities read like the server and never reach the comparator as NaN", async () => {
  // webdata._row_order: +inf is the newest, -inf the oldest; a number that overflows is infinite
  const names = await orderOf(["-Infinity", "1700000000", "inf", "1e400", "-1e400", "nan", "-inf"])
  assert.deepEqual(names, ["#r2", "#r3", "#r1", "#r5", "#r0", "#r4", "#r6"])
})

test("rail order: underscores between digits count, others and a lowercase z suffix read as 0", async () => {
  const names = await orderOf(["1__0", "1_700_000_000", "2025-01-01T00:00:00z", "1_", "2025-01-01T00:00:00Z"])
  assert.deepEqual(names, ["#r4", "#r1", "#r0", "#r2", "#r3"])
})

test("refresh: a state refresh that keeps the order does not scroll", async () => {
  const rows = () => [
    lblRow("a", "#a", "posted", "2026-03-01T00:00:00+00:00"),
    lblRow("b", "#b", "posted", "2026-02-01T00:00:00+00:00"),
    lblRow("c", "#c", "none"), lblRow("zz", "#zz", "none")]
  const t = bootFixture({ "/api/state": orderState(rows()) })
  await t.app.ready
  await t.app.selectChannel("c")
  const calls = []
  let fetched = 0
  const next = orderState(rows())
  t.routes["/api/state"] = () => { fetched++; return next }
  t.routes["/api/changes"] = quiet("C1", { channels: { zz: { new: true } } })
  const realCreate = t.document.createElement
  t.document.createElement = (tag) => { const n = realCreate(tag); n.scrollIntoView = (o) => calls.push([n, o]); return n }
  await t.app.pollOnce()
  t.document.createElement = realCreate
  assert.ok(fetched >= 1, "the state was refreshed")
  assert.deepEqual(laneNames(t), ["#general", "#a", "#b", "#c", "#zz"])
  assert.equal(calls.length, 0)
})
