"""Live-model evaluation track (Experiment C).

Configurable real models via environment. Reports UNAVAILABLE without
credentials rather than fabricating results.
"""

from evaluation.live.config import LiveEvalConfig, describe_live_availability

__all__ = ["LiveEvalConfig", "describe_live_availability"]
