import unittest

from chat.budget import LiveBudgetLedger


class LiveBudgetLedgerTestCase(unittest.TestCase):
    def test_parallel_estimates_share_one_participant_credit_pool(self):
        ledger = LiveBudgetLedger()
        ledger.register("P-test", "call-a", 1.0, 0.2, 0.2)
        ledger.register("P-test", "call-b", 1.0, 0.2, 0.2)

        self.assertFalse(ledger.set_estimated_cost("P-test", "call-a", 0.3))
        self.assertTrue(ledger.set_estimated_cost("P-test", "call-b", 0.5))

        ledger.settle("P-test", "call-a", 0.5)
        self.assertTrue(ledger.set_estimated_cost("P-test", "call-b", 0.5))
        ledger.discard("P-test", "call-b")
        self.assertNotIn("P-test", ledger._participants)


if __name__ == "__main__":
    unittest.main()
