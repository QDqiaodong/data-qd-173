import json
import unittest
from pathlib import Path
from charging.core import settle


class SettlementTests(unittest.TestCase):
    def test_splits_at_tariff_boundary(self):
        result = settle(json.loads(Path("fixtures/input.json").read_text()))
        self.assertEqual(result["sessions"][0]["session_id"], "C1")
        self.assertEqual([x["energy_wh"] for x in result["sessions"][0]["segments"]], [1000, 1000])
        self.assertEqual(result["total_charge_cents"], 58)

    def test_rejects_tariff_gap(self):
        data = {"tariffs": [{"start": "2026-01-01T00:00:00+00:00", "end": "2026-01-01T01:00:00+00:00", "cents_per_kwh": 10}], "sessions": [{"id": "x", "start": "2026-01-01T00:30:00+00:00", "end": "2026-01-01T02:00:00+00:00", "start_wh": 0, "end_wh": 1000}]}
        with self.assertRaisesRegex(ValueError, "coverage gap"):
            settle(data)


if __name__ == "__main__":
    unittest.main()
