"""Characterization test net for SPEC-ARCH-AI-001 (PRESERVE phase).

These tests lock the CURRENT observable behavior of the REST /recommend
pipeline (run_pipeline -> embed -> enhance_query -> search RPC -> diversify ->
RecommendResponse) so the later IMPROVE phase (service/repository extraction)
can be proven byte-identical.

Golden rule: expected literals are whatever the CURRENT code produces. Never
edit app/ to make a test "nicer" -- observe-then-lock.
"""
