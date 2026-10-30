"""Event Graph Coherence (EGC) package."""

from .cross import build_graph, cross_signals, cross_snapshot, solve_graph
from .cross_backtest import run_cross_backtest
from .gamma import fetch_events, parse_event
from .model import event_model, fair_exclusive, isotonic
from .signals import fee, signals_for_model

__all__ = [
    "build_graph",
    "cross_signals",
    "cross_snapshot",
    "event_model",
    "fair_exclusive",
    "fee",
    "fetch_events",
    "isotonic",
    "parse_event",
    "run_cross_backtest",
    "signals_for_model",
    "solve_graph",
]
