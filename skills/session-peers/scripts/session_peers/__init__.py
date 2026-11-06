"""Eagerly load runtime modules before a shim starts serving."""

from . import buddy, budgets, channels, claude, cli, codex, config, constants, diagnostics, hooks, identity, journal, lifecycle, maintenance, messaging, process, protocol, requests, rollout, routes, runtime, shim, storage, topics

runtime.LOADED_CODE_DIGEST = runtime.code_digest(runtime.runtime_code_files())
