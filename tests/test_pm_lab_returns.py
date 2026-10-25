"""Regression tests for the frozen-signal scoring rules.

Two defects from the 2026-10-25 overnight run are pinned here:

* a stored ``None`` was treated as a final result, so the 240m horizon never
  got a second chance and stayed at 0 settled rows;
* an entry price of 0.001-0.002 (decided or illiquid book) produced returns of
  +544% and -104%, inflating the 60m mean to +862%.
"""

import unittest
from datetime import UTC, datetime
from typing import ClassVar

import pm_lab


class HorizonReturnTest(unittest.TestCase):
    def test_buy_full_win(self):
        self.assertAlmostEqual(pm_lab.horizon_return("BUY", 0.5, 1.0), 1.0)

    def test_sell_full_win(self):
        self.assertAlmostEqual(pm_lab.horizon_return("SELL", 0.5, 0.0), 1.0)

    def test_buy_loss(self):
        self.assertAlmostEqual(pm_lab.horizon_return("BUY", 0.4, 0.2), -0.5)

    def test_flat_is_zero_not_none(self):
        self.assertEqual(pm_lab.horizon_return("BUY", 0.5, 0.5), 0.0)

    def test_missing_price_is_not_scored(self):
        self.assertIsNone(pm_lab.horizon_return("BUY", 0.5, None))
        self.assertIsNone(pm_lab.horizon_return("BUY", None, 0.9))

    def test_thin_entry_is_not_scored(self):
        # exactly the overnight outliers: the +544% and -104% rows
        self.assertIsNone(pm_lab.horizon_return("BUY", 0.001, 0.545))
        self.assertIsNone(pm_lab.horizon_return("SELL", 0.001, 0.105))
        self.assertIsNone(pm_lab.horizon_return("BUY", 0.002, 1.0))

    def test_entry_at_threshold_is_scored(self):
        self.assertAlmostEqual(pm_lab.horizon_return("BUY", 0.02, 0.03), 0.5)

    def test_missing_side_defaults_to_buy(self):
        self.assertAlmostEqual(pm_lab.horizon_return(None, 0.5, 1.0), 1.0)


class StoredFinalReturnTest(unittest.TestCase):
    def test_stored_none_is_retried(self):
        self.assertIsNone(pm_lab.stored_final_return({"60": None}, 60))

    def test_stored_number_is_final(self):
        self.assertEqual(pm_lab.stored_final_return({"60": 0.25}, 60), 0.25)

    def test_stored_zero_is_final(self):
        self.assertEqual(pm_lab.stored_final_return({"60": 0.0}, 60), 0.0)

    def test_integer_key_is_read(self):
        self.assertEqual(pm_lab.stored_final_return({60: 0.1}, 60), 0.1)

    def test_missing_horizon_is_retried(self):
        self.assertIsNone(pm_lab.stored_final_return({}, 240))


class HorizonStatusTest(unittest.TestCase):
    NOW = 1_800_000_000.0

    def test_future_horizon_waits(self):
        self.assertEqual(pm_lab.horizon_status(240, self.NOW, self.NOW), "future")

    def test_recent_horizon_is_retried(self):
        base = self.NOW - 300 * 60
        self.assertEqual(pm_lab.horizon_status(240, base, self.NOW), "retry")

    def test_old_horizon_expires(self):
        base = self.NOW - (240 + 49 * 60) * 60
        self.assertEqual(pm_lab.horizon_status(240, base, self.NOW), "expired")

    def test_expiry_boundary(self):
        base = self.NOW - 240 * 60 - pm_lab.RETRY_WINDOW
        self.assertEqual(pm_lab.horizon_status(240, base, self.NOW), "retry")
        self.assertEqual(pm_lab.horizon_status(240, base - 1, self.NOW), "expired")


class EntryPriceTest(unittest.TestCase):
    def test_live_signal_without_book_is_not_tradeable(self):
        # 37 such rows in the overnight file produced a fake +$89 when the
        # whale's fill was used as the entry
        self.assertIsNone(pm_lab.entry_price({"now_price": None, "whale_price": 0.61}))
        self.assertIsNone(pm_lab.entry_price({"whale_price": 0.61}))

    def test_live_signal_uses_book_price(self):
        self.assertEqual(
            pm_lab.entry_price({"now_price": 0.52, "whale_price": 0.61}), 0.52
        )

    def test_backfilled_row_falls_back_to_whale_price(self):
        self.assertEqual(
            pm_lab.entry_price(
                {"now_price": None, "whale_price": 0.4, "backfilled": True}
            ),
            0.4,
        )

    def test_backfilled_row_prefers_book_price(self):
        self.assertEqual(
            pm_lab.entry_price(
                {"now_price": 0.45, "whale_price": 0.4, "backfilled": True}
            ),
            0.45,
        )


class HorizonWindowTest(unittest.TestCase):
    DETECTED = "2026-10-25T01:00:00+00:00"
    TRADE_TS = 1792886400.0  # 2026-10-25T00:00:00Z, one hour before detection

    def test_window_that_ends_before_detection_is_invalid(self):
        sig = {"detected_at": self.DETECTED, "trade_ts": self.TRADE_TS}
        self.assertFalse(pm_lab.horizon_window_valid(sig, 60))

    def test_longer_horizon_is_still_valid(self):
        sig = {"detected_at": self.DETECTED, "trade_ts": self.TRADE_TS}
        self.assertTrue(pm_lab.horizon_window_valid(sig, 240))

    def test_backfilled_rows_skip_the_check(self):
        sig = {
            "detected_at": "2026-10-25T12:00:00+00:00",
            "trade_ts": self.TRADE_TS - 86400,
            "backfilled": True,
        }
        self.assertTrue(pm_lab.horizon_window_valid(sig, 60))

    def test_missing_timestamps_are_not_rejected(self):
        self.assertTrue(pm_lab.horizon_window_valid({}, 60))


class StrategyRowsTest(unittest.TestCase):
    ROWS: ClassVar[list[dict]] = [
        {"condition_id": "a", "moved": -0.01, "trade_ts": 10, "returns": {"60": 0.1}},
        {"condition_id": "a", "moved": 0.0, "trade_ts": 20, "returns": {"60": 0.2}},
        {"condition_id": "b", "moved": -0.5, "trade_ts": 5, "returns": {"60": -1.0}},
        {"condition_id": "c", "moved": None, "trade_ts": 30, "returns": {"60": 0.0}},
    ]

    def test_no_flags_keeps_everything(self):
        self.assertEqual(len(pm_lab.strategy_rows(self.ROWS)), 4)

    def test_adverse_move_filter(self):
        out = pm_lab.strategy_rows(self.ROWS, max_adverse_move=0.02)
        self.assertEqual({x["condition_id"] for x in out}, {"a", "c"})

    def test_unknown_move_is_kept(self):
        out = pm_lab.strategy_rows(self.ROWS, max_adverse_move=0.02)
        self.assertTrue(any(x["condition_id"] == "c" for x in out))

    def test_one_per_market_keeps_the_earliest(self):
        out = pm_lab.strategy_rows(self.ROWS, one_per_market=True)
        self.assertEqual({x["condition_id"] for x in out}, {"a", "b", "c"})
        self.assertEqual(
            next(x for x in out if x["condition_id"] == "a")["trade_ts"], 10
        )

    def test_both_filters(self):
        out = pm_lab.strategy_rows(self.ROWS, 0.02, True)
        self.assertEqual({x["condition_id"] for x in out}, {"a", "c"})


def _lag_row(lag_seconds: int, backfilled: bool = False) -> dict:
    detected = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
    return {
        "condition_id": f"c{lag_seconds}",
        "detected_at": detected.isoformat(),
        "trade_ts": detected.timestamp() - lag_seconds,
        "moved": 0.0,
        "backfilled": backfilled,
        "returns": {"60": 0.0},
    }


class StrategyLagTest(unittest.TestCase):
    def test_keeps_fast_and_boundary(self):
        rows = [_lag_row(60), _lag_row(300), _lag_row(600)]
        out = pm_lab.strategy_rows(rows, max_lag=300)
        self.assertEqual({x["condition_id"] for x in out}, {"c60", "c300"})

    def test_backfilled_rows_ignore_wall_clock_lag(self):
        rows = [_lag_row(86400, backfilled=True)]
        self.assertEqual(len(pm_lab.strategy_rows(rows, max_lag=300)), 1)


if __name__ == "__main__":
    unittest.main()
