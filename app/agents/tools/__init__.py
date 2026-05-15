"""SPEC-AGENT-V2-REACT — Tool wrapper modules.

Each module exports a single `dispatch(args, ctx) -> result` async function.
Wrappers are thin: validation → call helper → shape result. No business logic.
"""
