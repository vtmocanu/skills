# Regression tests

Run the aggregate from the repository root:

```sh
python3 skills/session-peers/scripts/test_peers.py
python3 skills/session-peers/scripts/test_peers.py TestCorrelatedAskReply
```

Run one domain from the skill's scripts directory:

```sh
python3 -m unittest peer_tests.test_messaging
```

| Module | Coverage |
| --- | --- |
| test_protocol | Tags, wrappers, frames, credentials, socket trust, and input bounds |
| test_discovery | Registry, Codex threads, aliases, session index, and stable cwd |
| test_rollout | Incremental rollout reading and chunk boundaries |
| test_messaging | Send, ask/reply, and dispatch/await |
| test_lifecycle | Registration, GC, list, ownership, shutdown, and atomic writes |
| test_shim | Inbound messages, replies, status, recovery, records, and delivery ordering |
| test_budgets | Reset persistence, allowances, and restart/buddy reply accounting |
| test_buddy | Binding records, routing, and buddy GC |
| test_commands | Version pins, doctor, hooks, config readers, and CLI surface |
| test_topics | Shared topic logs and cursors |
| test_upgrade | Source digests and replacing an installed copy under a running shim |

Keep shared fixture roots, fake binaries, listeners, builders, and base classes
in `support.py`. `HERE` points to the scripts directory, so fixtures invoke the
same stable launcher from source and installed copies. All files stay inside
the skill folder and require only the standard library.

Add each new domain to the aggregate's explicit `MODULES` list. It collects
locally defined `unittest.TestCase` subclasses and rejects duplicate class
names. Keep test class names unique so existing aggregate selectors work.
Run serially: fixtures replace process environment and patch shared runtime
modules. The installed-upgrade fixture excludes tests from its runtime-only
A/B copies.
