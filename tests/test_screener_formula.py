import unittest
import numpy as np
import pandas as pd

from screener import _sma_tdx, calc_original_formula


class FormulaTests(unittest.TestCase):
    def test_tdx_sma_recurrence(self):
        values = pd.Series([3.0, 6.0, 9.0])
        actual = _sma_tdx(values, 3, 1).tolist()
        self.assertAlmostEqual(actual[0], 3.0)
        self.assertAlmostEqual(actual[1], 4.0)
        self.assertAlmostEqual(actual[2], 17.0 / 3.0)

    def test_original_formula_returns_expected_columns(self):
        n = 530
        close = np.linspace(20, 18, n) + np.sin(np.arange(n) / 8) * 0.3
        frame = pd.DataFrame({
            "trade_date": pd.date_range("2024-01-01", periods=n, freq="B").strftime("%Y%m%d"),
            "open": close, "high": close * 1.01, "low": close * 0.99,
            "close": close, "vol": np.full(n, 10000.0),
            "amount": np.full(n, 200000.0), "pct_chg": np.zeros(n)
        })
        result = calc_original_formula(frame)
        self.assertIn("formula_capital", result.columns)
        self.assertIn("formula_signal", result.columns)
        self.assertFalse(result["formula_capital"].iloc[-1] is None)
        self.assertTrue(np.isfinite(float(result["formula_capital"].iloc[-1])))


if __name__ == "__main__":
    unittest.main()
