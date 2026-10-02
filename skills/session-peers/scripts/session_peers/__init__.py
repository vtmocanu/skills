"""Eagerly load runtime modules before a shim starts serving."""

from . import buddy, budgets, claude, cli, codex, config, constants, diagnostics, hooks, identity, lifecycle, maintenance, messaging, process, protocol, requests, rollout, runtime, shim, storage, topics

runtime.LOADED_CODE_DIGEST = runtime.code_digest(runtime.runtime_code_files())
