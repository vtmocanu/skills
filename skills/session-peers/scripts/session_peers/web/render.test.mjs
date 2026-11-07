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
const EVIL = "<img src=x onerror=alert(1)>\u0001\u0007\u001b[31m‮\r\nnext"

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
    this.attributes[name] = String(value)
  }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn) }
  dispatch(type, event = {}) { for (const fn of this.listeners[type] || []) fn(Object.assign({ type, preventDefault() {} }, event)) }
  focus() { this.ownerDocument.activeElement = this }
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
  assert.ok(text.includes("‮"))
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
  assert.ok(notes.includes("ACK state read from the latest 8 MiB of the journal"))
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
