"""SPEC-AGENT-001: LangGraph 1.x StateGraph orchestration for the Telegram fashion bot.

This package replaces the procedural state machine in `app/channels/scenario.py`
with a graph of small, single-purpose nodes. See `app/graphs/fashion_bot.py` for
the compiled graph entrypoint and `app/graphs/state.py` for the state model.
"""
