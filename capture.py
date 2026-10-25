"""Hidden entry point for the 5-minute strategy capture.

Task Scheduler runs this with ``pythonw.exe`` so no console window flashes every
five minutes. A failure must not look like "no fresh signals", so errors go to
``capture_error.log`` next to this file.

This capture watches only ``watch_selected.json``: the top quartile of wallets
by *closed* hold-to-resolution record (``pm_lab.py select``). Copying their next
entries and holding to resolution returned +25.9% per out-of-sample event
(t=3.7) on a month of history. The broad 133-wallet research scan stays in the
hourly task: doing it every five minutes spent minutes per run on wallets that
are not part of the strategy.

``--require-book`` keeps only signals with a real executable entry. Stale rows
from a long catch-up are dropped instead of being stored as untradeable noise.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import pm_lab

ARGS = [
    "signals",
    "--watch",
    "watch_selected.json",
    "--state",
    "signals_state_selected.json",
    # 24h catch-up after sleep; state prevents re-reporting old trades.
    "--max-age",
    "86400",
    # A book price is only meaningful for a fresh trade: older rows would be
    # recorded with a current book that no longer matches the whale's entry.
    "--live-age",
    "900",
    "--min-usdc",
    "2000",
    "--min-price",
    "0.02",
    "--max-price",
    "0.98",
    # One CLOB book request per candidate, newest first.
    "--max-new",
    "50",
    "--require-book",
    "--append",
    "--out",
    "signals_selected.json",
]


def main() -> int:
    try:
        pm_lab.main(ARGS)
        return 0
    except Exception:  # noqa: BLE001 - log any failure, never die silently
        (BASE / "capture_error.log").write_text(
            traceback.format_exc(), encoding="utf-8"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
