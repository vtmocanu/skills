"""Eagerly load runtime modules before a shim starts serving."""

from . import claude, codex, config, constants, diagnostics, lifecycle, process, protocol, requests, rollout, runtime, storage

runtime.LOADED_CODE_DIGEST = runtime.code_digest(runtime.runtime_code_files())
