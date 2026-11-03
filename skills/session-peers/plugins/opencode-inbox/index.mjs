// SDK contract: @opencode/plugin 2.2.5, promise/session.d.ts SessionContext.
import { Plugin } from "@opencode/plugin"
import { fileURLToPath } from "node:url"
import { createInboxHook, readSummary } from "./core.mjs"

export default Plugin.define({
  id: "session-peers.inbox",
  async setup(ctx) {
    const script = fileURLToPath(new URL("../../scripts/peers.py", import.meta.url))
    const hook = createInboxHook((sessionID) => readSummary(script, sessionID))
    await ctx.session.hook("context", hook)
  },
})
