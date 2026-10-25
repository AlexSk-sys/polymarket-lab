"""Hidden entry point for the 5-minute signal capture.

Task Scheduler runs this with ``pythonw.exe`` so no console window flashes every
five minutes. A failure must not look like "no fresh signals", so errors go to
``capture_error.log`` next to this file.

Why five minutes: on a 72h replay the same copy rule returned +7.8% per event
with a 60s lag, +8.9% with a 5m lag, and only +2.3% with the hourly task's
~30m lag. The edge is latency, not the signal list.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import pm_lab


def main() -> int:
    return pm_lab.main(
        [
            "signals",
            "--watch",
            "watch_big.json",
            "--min-usdc",
            "2000",
            "--min-price",
            "0.10",
            "--max-price",
            "0.90",
            # 24h of catch-up after sleep; the state file still prevents
            # re-reporting trades that were already captured.
            "--max-age",
            "86400",
            "--append",
            "--out",
            "signals_hourly.json",
        ]
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        (BASE / "capture_error.log").write_text(
            traceback.format_exc(), encoding="utf-8"
        )
        raise
