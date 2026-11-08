#!/usr/bin/env python3
"""Regression tests for peers.py.

Run: python3 skills/session-peers/scripts/test_peers.py   (stdlib unittest)

Nothing here touches a real agent. Every root is a temp directory
(CLAUDE_CONFIG_DIR, CODEX_HOME, the socket directory), PATH holds only fake
`ps`, `lsof`, `codex` and `claude` binaries, and the shim runs against a fixture
rollout the test appends to mid-run. The names follow
`test_<thing>_<what it must do>` so a failure names the behaviour, not the
assertion.

Socket paths are kept short on purpose: AF_UNIX caps a path at 104 bytes on
macOS, and a mkdtemp under /var/folders would spend most of that budget before
the filename.
"""

import importlib
import unittest

MODULES = (
    "peer_tests.test_protocol",
    "peer_tests.test_discovery",
    "peer_tests.test_rollout",
    "peer_tests.test_messaging",
    "peer_tests.test_lifecycle",
    "peer_tests.test_shim",
    "peer_tests.test_budgets",
    "peer_tests.test_buddy",
    "peer_tests.test_cross_home",
    "peer_tests.test_commands",
    "peer_tests.test_topics",
    "peer_tests.test_upgrade",
    "peer_tests.test_opencode",
    "peer_tests.test_inbox",
    "peer_tests.test_watches",
    "peer_tests.test_journal",
    "peer_tests.test_journal_cli",
    "peer_tests.test_journal_reader",
    "peer_tests.test_routes",
    "peer_tests.test_channels",
    "peer_tests.test_channel_send",
    "peer_tests.test_channel_general",
    "peer_tests.test_channel_lifecycle",
    "peer_tests.test_channel_buddy",
    "peer_tests.test_buddy_route",
    "peer_tests.test_shim_route",
    "peer_tests.test_webdata",
    "peer_tests.test_web_server",
    "peer_tests.test_web_assets",
    "peer_tests.test_web_routing",
    "peer_tests.test_web_peer",
    "peer_tests.test_web_chat",
    "peer_tests.test_web_confirm",
)

# Expose test classes so unittest's existing TestClass[.test_method] selectors
# keep working. Import each domain eagerly and reject ambiguous class names.
for module_name in MODULES:
    module = importlib.import_module(module_name)
    for name, value in vars(module).items():
        if (isinstance(value, type) and issubclass(value, unittest.TestCase)
                and value.__module__ == module.__name__):
            if name in globals():
                raise RuntimeError("duplicate test class: %s" % name)
            globals()[name] = value

del module_name, module, name, value

if __name__ == "__main__":
    unittest.main(verbosity=2)
