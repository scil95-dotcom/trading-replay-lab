import unittest
from decimal import Decimal as D

from trading_lab import Config, replay


class ReplayTests(unittest.TestCase):
    def test_signal_cannot_use_current_or_future_quote(self):
        before = replay([100, 101, 102, 103, 104, 105])
        changed = replay([100, 101, 102, 500, 1, 1])
        self.assertEqual(before["events"][:3], changed["events"][:3])
        self.assertTrue(changed["events"][3]["signal_long"])
        self.assertEqual(changed["events"][3]["action"], "buy")
        self.assertEqual(changed["ledger"][0]["quote"], "500")
        self.assertEqual(changed["events"][3]["signal_history_end"], 2)

    def test_exact_round_trip_fees_and_adverse_slippage(self):
        config = Config(max_units=D("1"), fee_bps=D("100"), slippage_bps=D("100"))
        result = replay([100, 101, 102, 100], config)
        self.assertEqual(result["summary"]["fills"], 2)
        self.assertEqual(D(result["summary"]["final_cash"]), D("996"))
        self.assertEqual(D(result["summary"]["total_fees"]), D("2"))
        self.assertEqual(D(result["ledger"][0]["execution_price"]), D("101"))
        self.assertEqual(D(result["ledger"][1]["execution_price"]), D("99"))

    def test_cash_constraint_and_position_cap(self):
        capped = replay([100, 101, 102, 103], Config(max_units=D("0.5")))
        self.assertEqual(D(capped["ledger"][0]["quantity"]), D("0.5"))
        constrained = replay([100, 101, 102, 103], Config(initial_cash=D("1")))
        self.assertLess(D(constrained["ledger"][0]["quantity"]), D("0.01"))
        for event in constrained["events"]:
            self.assertGreaterEqual(D(event["cash"]), 0)
            self.assertLessEqual(D(event["units"]), D("5"))
        tiny = replay([100, 101, 102, 103], Config(initial_cash=D("0.000001")))
        self.assertEqual(tiny["events"][3]["action"], "insufficient_cash")
        self.assertEqual(tiny["summary"]["fills"], 0)

    def test_gap_stop_executes_quote_and_prevents_reentry(self):
        result = replay([100, 101, 102, 103, 70, 80, 90, 100, 110])
        stop = result["ledger"][1]
        self.assertEqual(stop["reason"], "hard_stop")
        self.assertEqual(stop["quote"], "70")
        self.assertEqual(result["summary"]["fills"], 2)
        self.assertTrue(result["summary"]["halted"])
        self.assertTrue(all(row["action"] == "halted" for row in result["events"][5:]))

    def test_invalid_data_rejected_before_any_replay(self):
        for bad in ("NaN", "Infinity", "-Infinity", 0, -1, "oops", True, "1e100", "1e-100"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                replay([100, 101, 102, bad])
        with self.assertRaises(ValueError):
            replay([])

    def test_invalid_config_is_rejected(self):
        for kwargs in ({"fee_bps": "NaN"}, {"slippage_bps": 10000},
                       {"initial_cash": 0}, {"max_units": 0}, {"stop_loss": 1},
                       {"window": True}, {"window": 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Config(**kwargs)

    def test_ledger_reconciles_and_result_is_deterministic(self):
        prices = [100, 101, 102, 103, 104, 102, 99, 98]
        result = replay(prices)
        self.assertEqual(result, replay(prices))
        cash = D(result["config"]["initial_cash"])
        for fill in result["ledger"]:
            cash += D(fill["cash_delta"])
            self.assertEqual(cash, D(fill["cash_after"]))
        self.assertEqual(cash, D(result["summary"]["final_cash"]))
        self.assertEqual(D(result["summary"]["final_units"]), 0)


if __name__ == "__main__":
    unittest.main()
