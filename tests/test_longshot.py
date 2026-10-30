import csv
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from longshot.backtest import event_portfolio, load_history, no_return
from longshot.ledger import add_positions, load_ledger, resolve, summarize
from longshot.scanner import parse_candidate, scan


def mk_market(cid, question, mid, spread=0.01, volume=100_000, event="e1", days=5):
    end = dt.datetime.now(dt.UTC) + dt.timedelta(days=days)
    return {
        "conditionId": cid,
        "question": question,
        "bestBid": mid - spread / 2,
        "bestAsk": mid + spread / 2,
        "volumeNum": volume,
        "endDate": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "events": [{"id": event, "title": f"Event {event}"}],
    }


def mk_candidate(cid="c1", mid=0.05, event="e1", volume=100_000):
    return {
        "event_id": event,
        "event_title": f"Event {event}",
        "condition_id": cid,
        "question": f"Will {cid} happen?",
        "yes_bid": mid - 0.005,
        "yes_ask": mid + 0.005,
        "yes_mid": mid,
        "spread": 0.01,
        "volume": volume,
        "end_date": "2026-11-05T00:00:00Z",
        "days_to_end": 5.0,
    }


class ScannerTest(unittest.TestCase):
    def test_parse_candidate_filters(self):
        self.assertIsNotNone(parse_candidate(mk_market("c1", "Q", 0.05)))
        self.assertIsNone(parse_candidate(mk_market("c2", "Q", 0.20)))
        self.assertIsNone(parse_candidate(mk_market("c3", "Q", 0.05, spread=0.05)))
        self.assertIsNone(parse_candidate(mk_market("c4", "Q", 0.05, volume=10_000)))
        self.assertIsNone(parse_candidate(mk_market("c5", "Q", 0.05, days=-1)))

    def test_scan_picks_one_market_per_event(self):
        markets = [
            mk_market("c1", "Q1", 0.07, event="e1"),
            mk_market("c2", "Q2", 0.05, event="e1"),
            mk_market("c3", "Q3", 0.09, event="e2"),
            mk_market("c4", "Q4", 0.20, event="e3"),
        ]
        with patch("longshot.scanner.fetch_live_markets", return_value=markets):
            result = scan(days_min=1, days_max=8)
        self.assertEqual(result["events"], 2)
        self.assertEqual(len(result["candidates"]), 2)
        e1 = next(c for c in result["candidates"] if c["event_id"] == "e1")
        self.assertAlmostEqual(e1["yes_mid"], 0.05)


class LedgerTest(unittest.TestCase):
    def test_add_skips_duplicate_event_and_market(self):
        ledger = {"created_at": "x", "stake": 20.0, "positions": []}
        candidates = [
            mk_candidate("c1", 0.05, "e1"),
            mk_candidate("c2", 0.06, "e1"),
            mk_candidate("c3", 0.07, "e2"),
        ]
        added = add_positions(ledger, candidates, stake=20.0, max_open=10, max_new=10)
        self.assertEqual(len(added), 2)
        self.assertEqual({p["event_id"] for p in added}, {"e1", "e2"})
        again = add_positions(ledger, candidates, stake=20.0, max_open=10, max_new=10)
        self.assertEqual(again, [])

    def test_resolve_no_and_yes(self):
        ledger = {"created_at": "x", "stake": 20.0, "positions": []}
        added = add_positions(
            ledger,
            [mk_candidate("c1", 0.05, "e1"), mk_candidate("c2", 0.05, "e2")],
            stake=20.0,
            max_open=10,
            max_new=10,
        )
        self.assertEqual(len(added), 2)
        closed = {
            "c1": {"outcomePrices": '["0","1"]'},
            "c2": {"outcomePrices": ["1", "0"]},
        }
        resolved = resolve(ledger, closed_markets=closed)
        self.assertEqual(len(resolved), 2)
        no_pos = next(p for p in resolved if p["id"] == "c1")
        yes_pos = next(p for p in resolved if p["id"] == "c2")
        self.assertGreater(no_pos["pnl"], 0)
        self.assertLess(yes_pos["pnl"], 0)
        summary = summarize(ledger)
        self.assertEqual(summary["closed"], 2)
        self.assertEqual(summary["open"], 0)

    def test_load_missing_ledger_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = load_ledger(Path(tmp) / "missing.json")
            self.assertEqual(ledger["positions"], [])


class BacktestTest(unittest.TestCase):
    def test_no_return_positive_when_longshot_loses(self):
        self.assertGreater(no_return(0.05, 0), 0)
        self.assertLess(no_return(0.05, 1), 0)

    def test_event_portfolio(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.csv"
            with path.open("w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(
                    f,
                    fieldnames=[
                        "event_id",
                        "year",
                        "price",
                        "outcome",
                        "volume",
                        "question",
                    ],
                )
                w.writeheader()
                w.writerow(
                    {
                        "event_id": "e1",
                        "year": "2024",
                        "price": "0.05",
                        "outcome": "0",
                        "volume": "100000",
                        "question": "q1",
                    }
                )
                w.writerow(
                    {
                        "event_id": "e2",
                        "year": "2024",
                        "price": "0.04",
                        "outcome": "0",
                        "volume": "100000",
                        "question": "q2",
                    }
                )
                w.writerow(
                    {
                        "event_id": "e3",
                        "year": "2025",
                        "price": "0.06",
                        "outcome": "1",
                        "volume": "100000",
                        "question": "q3",
                    }
                )
                w.writerow(
                    {
                        "event_id": "e3",
                        "year": "2025",
                        "price": "0.09",
                        "outcome": "1",
                        "volume": "100000",
                        "question": "q4",
                    }
                )
            rows = load_history(path)
            self.assertEqual(len(rows), 4)
            result = event_portfolio(rows)
            self.assertIsNotNone(result)
            self.assertEqual(result["n"], 3)  # one per event
            self.assertEqual(result["events"], 3)
            self.assertEqual(result["losses"], 1)
            self.assertIn("2024", result["yearly"])
            self.assertIn("2025", result["yearly"])


if __name__ == "__main__":
    unittest.main()
