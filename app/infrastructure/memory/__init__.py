"""Memory infrastructure (SPEC-ARCH-AI-001 PR4, REQ-AI-004).

Session + taste-profile persistence relocated VERBATIM from app/channels/
(session.py, session_pg.py, taste_profile.py, taste_profile_pg.py). The old
app/channels/ paths remain as thin re-export shims so all ~57 import sites
keep working unchanged. Zero logic change — module-global store state lives
in these modules' namespaces, so set_store/get_store consistency is
preserved across both the canonical and the shim import paths.
"""
