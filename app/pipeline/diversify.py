"""Thin re-export shim (SPEC-ARCH-AI-001 PR1).

The diversify cap/tolerance/order arithmetic moved verbatim into
app/services/diversify_service.py. This module preserves the original public
names (`diversify_step`, `_tolerance_to_target_count`) and the `@observe`
decoration so all existing import sites and characterization tests keep
working byte-identically.
"""

from app.observability.langfuse import observe
from app.pipeline.state import PipelineState
from app.services.diversify_service import diversify_service
from app.services.diversify_service import tolerance_to_target_count as _tolerance_to_target_count

__all__ = ["diversify_step", "_tolerance_to_target_count"]


@observe(name="pipeline.diversify")
async def diversify_step(state: PipelineState) -> PipelineState:
    return await diversify_service(state)
