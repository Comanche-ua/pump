#!/usr/bin/env python3
"""
Unit tests for Swing Engine and Multi-TF Long Strategy (+20%+).
"""
import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import swing_engine as se


class TestSwingEngine(unittest.TestCase):

    def test_calculate_ema(self):
        prices = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0]
        ema = se.calculate_ema(prices, period=3)
        self.assertGreater(len(ema), 0)
        self.assertAlmostEqual(ema[0], 11.0)
        self.assertGreater(ema[-1], ema[0])

    def test_calculate_sma(self):
        prices = [10.0, 20.0, 30.0, 40.0, 50.0]
        sma = se.calculate_sma(prices, period=3)
        self.assertEqual(len(sma), 3)
        self.assertAlmostEqual(sma[0], 20.0)
        self.assertAlmostEqual(sma[-1], 40.0)

    def test_calculate_atr(self):
        highs = [12.0, 13.0, 14.0, 15.0, 16.0]
        lows = [10.0, 11.0, 12.0, 13.0, 14.0]
        closes = [11.0, 12.0, 13.0, 14.0, 15.0]
        atr = se.calculate_atr(highs, lows, closes, period=3)
        self.assertGreater(len(atr), 0)
        self.assertGreater(atr[-1], 0)

    def test_calculate_rsi(self):
        # Monotonically increasing closes should have high RSI (> 80)
        closes = [float(i) for i in range(1, 30)]
        rsi = se.calculate_rsi(closes, period=14)
        self.assertIsNotNone(rsi)
        self.assertGreater(rsi, 80.0)

        # Monotonically decreasing closes should have low RSI (< 20)
        closes_down = [float(30 - i) for i in range(1, 30)]
        rsi_down = se.calculate_rsi(closes_down, period=14)
        self.assertIsNotNone(rsi_down)
        self.assertLess(rsi_down, 20.0)

    def test_detect_squeeze(self):
        # Low volatility candles (squeeze condition)
        highs = [100.5] * 30
        lows = [99.5] * 30
        closes = [100.0] * 30
        is_sqz, strength = se.detect_squeeze(highs, lows, closes, length=20)
        self.assertTrue(is_sqz)
        self.assertGreaterEqual(strength, 0.0)

    def test_aggregate_klines(self):
        # 12 candles of 5m -> 1 candle of 1h
        base_5m = []
        for i in range(24):
            base_5m.append([
                1700000000000 + i * 300000,  # open_time
                100.0 + i,                   # open
                105.0 + i,                   # high
                95.0 + i,                    # low
                102.0 + i,                   # close
                10.0,                        # volume
                1000.0                       # quote_volume
            ])
        agg_1h = se.aggregate_klines(base_5m, "1h")
        self.assertEqual(len(agg_1h), 2)
        # Check high of first 1h candle
        self.assertEqual(agg_1h[0][2], max(c[2] for c in base_5m[:12]))
        # Check low of first 1h candle
        self.assertEqual(agg_1h[0][3], min(c[3] for c in base_5m[:12]))

    def test_analyze_swing_setup_generates_signal(self):
        # Generate 400 bullish candles with squeeze and volume burst
        klines_5m = []
        for i in range(400):
            p = 10.0 + i * 0.05
            v = 100.0 if i < 380 else 1500.0  # Volume spike at the end
            klines_5m.append([
                1700000000000 + i * 300000,
                p,
                p + 0.1,
                p - 0.1,
                p + 0.05,
                v,
                v * p
            ])
        sig = se.analyze_swing_setup("TESTUSDT", klines_5m, min_score=50.0)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.symbol, "TESTUSDT")
        self.assertGreater(sig.target_price, sig.price)
        self.assertLess(sig.sl_price, sig.price)
        self.assertGreaterEqual(sig.estimated_target_pct, 18.0)


if __name__ == "__main__":
    unittest.main()
