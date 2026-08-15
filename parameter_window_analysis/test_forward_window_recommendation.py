from __future__ import annotations

from datetime import date
from pathlib import Path
import sys
import unittest


PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


class ForwardRecommendationTests(unittest.TestCase):
    def test_settled_update_days_excludes_date_without_next_day_return(self) -> None:
        from parameter_window_analysis import forward_window_recommendation as recommendation

        next_trade_dates = {
            date(2026, 8, 4): date(2026, 8, 5),
            date(2026, 8, 5): date(2026, 8, 6),
        }

        settled = recommendation.settled_update_days(
            next_trade_dates,
            start_date=date(2026, 8, 4),
            end_date=date(2026, 8, 6),
        )

        self.assertEqual(settled, (date(2026, 8, 4), date(2026, 8, 5)))

    def test_rank_windows_uses_compounded_daily_return_and_zero_for_no_signal(self) -> None:
        from parameter_window_analysis import forward_window_recommendation as recommendation

        daily_rows = [
            {"lookback_days": 5, "settlement_status": "settled", "daily_return_pct": 10.0},
            {"lookback_days": 5, "settlement_status": "settled", "daily_return_pct": -10.0},
            {"lookback_days": 6, "settlement_status": "settled", "daily_return_pct": 2.0},
            {"lookback_days": 6, "settlement_status": "settled", "daily_return_pct": 2.0},
            {"lookback_days": 7, "settlement_status": "awaiting_settlement"},
        ]

        ranked = recommendation.rank_windows(daily_rows)

        self.assertEqual([row["lookback_days"] for row in ranked], [6, 5])
        self.assertAlmostEqual(ranked[0]["average_daily_return_pct"], 2.0, places=8)
        self.assertAlmostEqual(ranked[1]["average_daily_return_pct"], -0.5012562893, places=8)
        self.assertEqual(ranked[1]["settled_days"], 2)


if __name__ == "__main__":
    unittest.main()
