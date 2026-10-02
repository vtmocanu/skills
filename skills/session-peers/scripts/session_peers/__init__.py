"""Eagerly load runtime modules before a shim starts serving."""

from . import config, constants, protocol, rollout, runtime

runtime.LOADED_CODE_DIGEST = runtime.code_digest(runtime.runtime_code_files())
