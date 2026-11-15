"""
Pipeline: ordered stage-based message processing.

Public API:
  - `Stage` — abstract base for pipeline stages
  - `StageContext` — data passed between stages
  - `PipelineScheduler` — executes stages in order
"""

from core.pipeline.stage import Stage, StageContext, registered_stages
from core.pipeline.scheduler import PipelineScheduler

__all__ = [
    "Stage",
    "StageContext",
    "PipelineScheduler",
    "registered_stages",
]
