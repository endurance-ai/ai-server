"""SPEC-AGENT-001 — 10 graph nodes.

Each node is a module-level `async def` returning a state delta dict.
Per REQ-AGENT-007, nodes never raise; they log a breadcrumb to log_events
and return an empty (or minimal) delta on error so the graph can continue.
"""
