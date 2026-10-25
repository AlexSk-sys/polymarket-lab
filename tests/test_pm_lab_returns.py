"""Regression tests for the frozen-signal scoring rules.

Two defects from the 2026-10-25 overnight run are pinned here:

* a stored ``None`` was treated as a final result, so the 240m horizon never
  got a second chance and stayed at 0 settled rows;
* an entry price of 0.001-0.002 (decided or illiquid book) produced returns of
  +544% and -104%, inflating the 60m mean to +862%.
"""

import unittest

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


if __name__ == "__main__":
    unittest.main()
