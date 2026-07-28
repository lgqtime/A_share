import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
import json

import szse_quant_app as app


class ScreeningPresetTests(unittest.TestCase):
    def test_default_settings_match_configured_strategy(self) -> None:
        settings = app.default_screening_settings()
        selected = {
            key: settings[f"szse_quant_filter_{key}"]
            for key in app.SCORING_INDICATOR_KEYS
        }

        self.assertEqual(app.maximum_score(selected), 9.5)
        self.assertEqual(app.MIN_KDJ_HEALTHY_GOLDEN_CROSS_AGE, 0)
        self.assertEqual(app.MAX_KDJ_HEALTHY_GOLDEN_CROSS_AGE, 10)
        self.assertEqual(app.DEFAULT_KDJ_HEALTHY_GOLDEN_CROSS_AGE_RANGE, (1, 3))
        self.assertTrue(settings["szse_quant_filter_above_ma5"])
        self.assertTrue(settings["szse_quant_filter_above_ma20"])
        self.assertFalse(settings["szse_quant_filter_ma5_above_ma20"])
        self.assertTrue(settings["szse_quant_filter_rsi_in_range"])
        self.assertEqual(settings["szse_quant_filter_rsi_range"], (49.1, 62.6))
        self.assertTrue(settings["szse_quant_filter_macd_bullish"])
        self.assertFalse(settings["szse_quant_filter_macd_golden_cross"])
        self.assertEqual(
            settings["szse_quant_filter_macd_golden_cross_lookback_days"], 3
        )
        self.assertTrue(settings["szse_quant_filter_volume_breakout"])
        self.assertEqual(settings["szse_quant_filter_volume_ratio_range"], (1.8, 3.8))
        self.assertNotIn("szse_quant_filter_volume_ratio_threshold", settings)
        self.assertTrue(settings["szse_quant_filter_amount_at_least_100m"])
        self.assertTrue(settings["szse_quant_filter_turnover_in_range"])
        self.assertFalse(settings["szse_quant_filter_positive_change"])
        self.assertFalse(settings["szse_quant_filter_float_market_cap_in_range"])
        self.assertTrue(settings["szse_quant_filter_pct_change_in_range"])
        self.assertFalse(settings["szse_quant_filter_platform_breakout_20d"])
        self.assertFalse(settings["szse_quant_filter_ma5_rising"])
        self.assertFalse(settings["szse_quant_filter_close_near_daily_high"])
        self.assertTrue(settings["szse_quant_filter_kdj_healthy_golden_cross_3d"])
        self.assertEqual(
            settings["szse_quant_filter_kdj_healthy_golden_cross_age_range"],
            (1, 3),
        )
        self.assertNotIn(
            "szse_quant_filter_kdj_healthy_golden_cross_lookback_days", settings
        )
        self.assertFalse(settings["szse_quant_filter_macd_dea_minus_dif_high"])
        self.assertEqual(
            settings["szse_quant_filter_macd_dea_minus_dif_range"], (0.1, 0.2)
        )
        self.assertEqual(settings["szse_quant_filter_turnover_range"], (5.4, 10.7))
        self.assertEqual(settings["szse_quant_filter_float_market_cap_range_yi"], (50.0, 200.0))
        self.assertEqual(settings["szse_quant_filter_pct_change_range"], (-2.7, 10.1))
        self.assertTrue(settings["szse_quant_filter_require_all"])
        self.assertTrue(settings["szse_quant_risk_bias_high"])
        self.assertFalse(settings["szse_quant_risk_upper_shadow"])
        self.assertTrue(settings["szse_quant_risk_resistance_60_day"])
        self.assertNotIn("szse_quant_risk_macd_dea_minus_dif_high", settings)
        self.assertNotIn("szse_quant_risk_macd_dea_minus_dif_range", settings)
        self.assertEqual(
            settings["szse_quant_risk_candlestick_patterns"],
            ["doji_3d", "inverted_t_doji_3d", "hanging_man_3d"],
        )

    def test_platform_preset_is_a_single_pool_configuration(self) -> None:
        settings = app.platform_breakout_screening_settings()

        self.assertTrue(settings["szse_quant_filter_above_ma5"])
        self.assertTrue(settings["szse_quant_filter_above_ma20"])
        self.assertFalse(settings["szse_quant_filter_ma5_above_ma20"])
        self.assertTrue(settings["szse_quant_filter_platform_breakout_20d"])
        self.assertTrue(settings["szse_quant_filter_ma5_rising"])
        self.assertTrue(settings["szse_quant_filter_close_near_daily_high"])
        self.assertTrue(settings["szse_quant_filter_volume_breakout"])
        self.assertTrue(settings["szse_quant_filter_amount_at_least_100m"])
        self.assertTrue(settings["szse_quant_filter_turnover_in_range"])
        self.assertFalse(settings["szse_quant_filter_float_market_cap_in_range"])
        self.assertFalse(settings["szse_quant_filter_pct_change_in_range"])
        self.assertFalse(settings["szse_quant_filter_rsi_in_range"])
        self.assertEqual(settings["szse_quant_filter_rsi_range"], (49.1, 62.6))
        self.assertFalse(settings["szse_quant_filter_macd_bullish"])
        self.assertFalse(settings["szse_quant_filter_macd_golden_cross"])
        self.assertEqual(
            settings["szse_quant_filter_macd_golden_cross_lookback_days"], 3
        )
        self.assertFalse(settings["szse_quant_filter_kdj_healthy_golden_cross_3d"])
        self.assertEqual(
            settings["szse_quant_filter_kdj_healthy_golden_cross_age_range"],
            (1, 3),
        )
        self.assertNotIn(
            "szse_quant_filter_kdj_healthy_golden_cross_lookback_days", settings
        )
        self.assertEqual(settings["szse_quant_filter_volume_ratio_range"], (1.8, 3.8))
        self.assertFalse(settings["szse_quant_filter_macd_dea_minus_dif_high"])
        self.assertEqual(
            settings["szse_quant_filter_macd_dea_minus_dif_range"], (0.1, 0.2)
        )
        self.assertTrue(settings["szse_quant_filter_require_all"])
        self.assertTrue(settings["szse_quant_risk_bias_high"])
        self.assertFalse(settings["szse_quant_risk_upper_shadow"])
        self.assertFalse(settings["szse_quant_risk_resistance_60_day"])
        self.assertNotIn("szse_quant_risk_macd_dea_minus_dif_high", settings)
        self.assertNotIn("szse_quant_risk_macd_dea_minus_dif_range", settings)
        self.assertEqual(
            settings["szse_quant_risk_candlestick_patterns"],
            list(app.CANDLESTICK_RISK_PATTERN_KEYS),
        )

    def test_apply_default_preset_replaces_all_changed_filter_values(self) -> None:
        state = {key: "已修改" for key in app.SCREENING_WIDGET_DEFAULTS}
        state["other_setting"] = "保留"
        app.apply_screening_settings(state, app.platform_breakout_screening_settings())
        app.apply_screening_settings(state, app.default_screening_settings())

        for key, value in app.SCREENING_WIDGET_DEFAULTS.items():
            self.assertEqual(state[key], value)
        self.assertEqual(state["other_setting"], "保留")

    def test_returned_settings_are_independent_copies(self) -> None:
        first = app.default_screening_settings()
        first["szse_quant_filter_rsi_in_range"] = False
        second = app.default_screening_settings()

        self.assertTrue(second["szse_quant_filter_rsi_in_range"])

    def test_optimized_parameter_overrides_preserve_other_settings(self) -> None:
        settings = app.default_screening_settings()
        settings["szse_quant_filter_rsi_in_range"] = False
        settings["szse_quant_risk_bias_high"] = False

        app.apply_optimized_parameter_overrides(settings)

        self.assertEqual(
            settings["szse_quant_filter_rsi_range"], (48.4, 54.6)
        )
        self.assertEqual(
            settings["szse_quant_filter_turnover_range"], (5.2, 11.8)
        )
        self.assertEqual(
            settings["szse_quant_filter_pct_change_range"], (-1.0, 8.3)
        )
        self.assertEqual(
            settings["szse_quant_filter_kdj_healthy_golden_cross_age_range"], (1, 3)
        )
        self.assertFalse(settings["szse_quant_filter_rsi_in_range"])
        self.assertFalse(settings["szse_quant_risk_bias_high"])

    def test_optimized_parameter_file_rejects_invalid_ranges(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "invalid.json"
            report_path.write_text(
                json.dumps(
                    {
                        "best_settings": {
                            "szse_quant_filter_rsi_range": [54.0, 48.0]
                        }
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "超出允许范围"):
                app.load_optimized_parameter_overrides(report_path)


if __name__ == "__main__":
    unittest.main()
