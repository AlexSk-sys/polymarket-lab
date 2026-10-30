import unittest

from egc.gamma import parse_market
from egc.model import _parse_strike, fair_exclusive, isotonic
from egc.signals import fee, signals_for_model


class FairExclusiveTest(unittest.TestCase):
    def test_simplex_is_preserved(self):
        q = fair_exclusive([0.5, 0.5])
        self.assertAlmostEqual(sum(q), 1.0, places=6)
        self.assertAlmostEqual(q[0], 0.5, places=4)

    def test_overround_is_projected_down(self):
        q = fair_exclusive([0.8, 0.8])
        self.assertAlmostEqual(sum(q), 1.0, places=6)
        self.assertTrue(all(x < 0.8 for x in q))

    def test_latent_other_absorbs_mass(self):
        q = fair_exclusive([0.6, 0.6], other_price=0.001)
        self.assertTrue(all(x < 0.6 for x in q))
        self.assertLess(sum(q), 1.0)


class StrikeParserTest(unittest.TestCase):
    def test_comma_number_before_by_is_not_a_multiplier(self):
        # Regression: the "b" in "by" used to be captured as the "billion"
        # suffix, turning $90,000 into 9e13 and corrupting strike ladders.
        self.assertEqual(
            _parse_strike("Will Bitcoin reach $90,000 by December 31, 2026?"),
            90000.0,
        )

    def test_suffixes_still_work(self):
        self.assertEqual(_parse_strike("Will BTC reach $150k by 2027?"), 150000.0)
        self.assertEqual(_parse_strike("FDV above $5B one day after launch?"), 5e9)


class IsotonicTest(unittest.TestCase):
    def test_violation_is_pooled(self):
        q = isotonic([0.2, 0.5, 0.4])
        self.assertAlmostEqual(q[1], 0.45, places=6)
        self.assertAlmostEqual(q[2], 0.45, places=6)

    def test_monotone_input_unchanged(self):
        self.assertEqual(isotonic([0.1, 0.2, 0.3]), [0.1, 0.2, 0.3])


class SignalTest(unittest.TestCase):
    def test_fee_at_half_is_half_percent(self):
        self.assertAlmostEqual(fee(0.5), 0.005)

    def test_yes_edge_after_fee(self):
        model = {
            "kind": "exclusive",
            "markets": [
                {
                    "question": "A",
                    "condition_id": "c",
                    "tokens": ["y", "n"],
                    "bid": 0.49,
                    "ask": 0.51,
                    "mid": 0.5,
                    "volume": 30000,
                    "end_date": "",
                }
            ],
            "q": [0.6],
        }
        sigs = signals_for_model(model)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["side"], "YES")
        spread = 0.51 - 0.49
        self.assertAlmostEqual(
            sigs[0]["edge"],
            0.6 - 0.5 - spread - fee(0.51) - fee(0.49),
            places=4,
        )


class ParseMarketTest(unittest.TestCase):
    def test_json_string_fields(self):
        m = parse_market(
            {
                "conditionId": "c",
                "question": "Q",
                "clobTokenIds": '["y","n"]',
                "outcomes": '["Yes","No"]',
                "bestBid": "0.4",
                "bestAsk": "0.5",
            }
        )
        self.assertEqual(m["tokens"], ["y", "n"])
        self.assertAlmostEqual(m["mid"], 0.45)

    def test_bad_book_is_rejected(self):
        self.assertIsNone(
            parse_market({"clobTokenIds": '["y","n"]', "bestBid": "0", "bestAsk": "0"})
        )


if __name__ == "__main__":
    unittest.main()
