from __future__ import annotations

from pathlib import Path
import unittest

import plot_optimizer_return_matrix as plots


MATRIX_PATH = Path(
    "optimizer_lookback_sweep/20260810_175119/收益率矩阵.csv"
)


class OptimizerReturnMatrixPlotTests(unittest.TestCase):
    def test_load_matrix_divides_each_row_by_its_window_label(self) -> None:
        raw_matrix, normalized_matrix = plots.load_matrix(MATRIX_PATH)

        self.assertEqual(raw_matrix.shape, (23, 20))
        self.assertEqual(raw_matrix.index[0], 8)
        self.assertEqual(raw_matrix.columns[0], "2026-08-07")
        self.assertAlmostEqual(
            normalized_matrix.loc[8, "2026-08-07"],
            raw_matrix.loc[8, "2026-08-07"] / 8,
        )


if __name__ == "__main__":
    unittest.main()
