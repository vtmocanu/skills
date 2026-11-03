// SDK contract: @opencode/plugin 2.2.5, promise/session.d.ts SessionContext.
// Plugin.define is an identity function (promise/plugin.js); a plain definition
// avoids a bare SDK import that a path-loaded skill might not resolve.
import { fileURLToPath } from "node:url"
import { createInboxHook, readSummary } from "./core.mjs"

export default {
  id: "session-peers.inbox",
  async setup(ctx) {
    const script = fileURLToPath(new URL("../../scripts/peers.py", import.meta.url))
    const hook = createInboxHook((sessionID) => readSummary(script, sessionID))
    await ctx.session.hook("context", hook)
  },
}
