"""Event Graph Coherence (EGC) package."""

from .gamma import fetch_events, parse_event
from .model import event_model, fair_exclusive, isotonic
from .signals import fee, signals_for_model

__all__ = [
    "event_model",
    "fair_exclusive",
    "fee",
    "fetch_events",
    "isotonic",
    "parse_event",
    "signals_for_model",
]
