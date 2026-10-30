"""Executable relative-value signals from EGC fair probabilities."""

from __future__ import annotations

FEE_RATE = 0.02


def fee(price: float) -> float:
    """Polymarket V2 taker fee per unit notional."""
    p = min(1.0, max(0.0, price))
    return FEE_RATE * p * (1.0 - p)


def signals_for_model(
    model: dict,
    event_title: str = "",
    min_edge: float = 0.02,
    max_spread: float = 0.03,
    min_volume: float = 20000.0,
) -> list[dict]:
    out = []
    for m, q in zip(model.get("markets") or [], model.get("q") or []):
        if m.get("volume", 0.0) < min_volume:
            continue
        spread = m["ask"] - m["bid"]
        if spread > max_spread:
            continue
        yes_edge = q - m["ask"] - fee(m["ask"])
        no_edge = m["bid"] - q - fee(1.0 - m["bid"])
        if yes_edge >= no_edge and yes_edge > min_edge:
            side, edge, entry = "YES", yes_edge, m["ask"]
            token = m["tokens"][0]
        elif no_edge > min_edge:
            side, edge, entry = "NO", no_edge, 1.0 - m["bid"]
            token = m["tokens"][1]
        else:
            continue
        out.append(
            {
                "event": event_title,
                "kind": model.get("kind"),
                "question": m["question"],
                "condition_id": m["condition_id"],
                "token_id": token,
                "side": side,
                "edge": round(float(edge), 4),
                "fair": round(float(q), 4),
                "mid": round(float(m["mid"]), 4),
                "entry": round(float(entry), 4),
                "bid": m["bid"],
                "ask": m["ask"],
                "spread": round(float(spread), 4),
                "volume": m["volume"],
                "end_date": m["end_date"],
            }
        )
    out.sort(key=lambda x: -x["edge"])
    return out
