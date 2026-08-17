import importlib.util
import sys
import types
import unittest
from datetime import date
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "traffic_analyzer.py"
for dependency in ("asyncpg", "httpx"):
    if dependency not in sys.modules:
        try:
            __import__(dependency)
        except ModuleNotFoundError:
            sys.modules[dependency] = types.ModuleType(dependency)
SPEC = importlib.util.spec_from_file_location("traffic_analyzer", SCRIPT)
traffic_analyzer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = traffic_analyzer
SPEC.loader.exec_module(traffic_analyzer)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self):
        self.current = {
            "factor_07_10": 1.59,
            "factor_10_13": 1.43,
            "factor_13_16": 1.68,
            "factor_16_19": 1.10,
            "factor_other": 1.52,
        }
        self.update_args = None
        self.history_rows = None

    def transaction(self):
        return _Transaction()

    async def fetchrow(self, query, *args):
        return self.current

    async def execute(self, query, *args):
        if "UPDATE traffic_planning_settings" in query:
            self.update_args = args

    async def executemany(self, query, rows):
        self.history_rows = list(rows)


class PlanningFactorUpdateTest(unittest.IsolatedAsyncioTestCase):
    async def test_minimum_sample_and_clamps(self):
        conn = _FakeConn()
        rows = [
            {"group_key": "time:07-10", "time_bucket": "07-10",
             "sample_count": 25, "weighted_factor": 1.61},
            {"group_key": "time:10-13", "time_bucket": "10-13",
             "sample_count": 19, "weighted_factor": 1.20},
            {"group_key": "time:13-16", "time_bucket": "13-16",
             "sample_count": 20, "weighted_factor": 3.20},
            {"group_key": "time:other", "time_bucket": "other",
             "sample_count": 30, "weighted_factor": 0.40},
        ]
        audit = await traffic_analyzer.update_planning_factors(
            conn, date(2026, 7, 19), date(2026, 8, 17), rows)

        self.assertEqual(audit["07-10"]["applied_factor"], 1.61)
        self.assertEqual(audit["10-13"]["applied_factor"], 1.43)
        self.assertEqual(audit["10-13"]["status"], "insufficient_sample")
        self.assertEqual(audit["13-16"]["applied_factor"], 3.0)
        self.assertEqual(audit["13-16"]["status"], "clamped")
        self.assertEqual(audit["16-19"]["applied_factor"], 1.10)
        self.assertEqual(audit["other"]["applied_factor"], 0.5)
        self.assertEqual(len(conn.history_rows), 5)
        self.assertIsNotNone(conn.update_args)


if __name__ == "__main__":
    unittest.main()
