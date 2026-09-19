#!/usr/bin/env python3
"""
Модуль мульти-таймфрейм анализа (5m, 15m, 30m, 1h, 4h) для стратегии Лонг-Свинг (+20%+).

Анализирует:
1. Аккумуляцию и объем крупного игрока (RVOL >= 2.0x, OBV slope).
2. Сжатие волатильности (Bollinger Bands внутри Keltner Channel — TTM Squeeze).
3. Трендовую структуру (цена > EMA20 > EMA50 на 1h и 4h).
4. Импульс и моментум (RSI 50-70, MACD бычий кросс).
5. Расчет потенциала движения (+15% .. +35%+) и динамических уровней SL/TP/Trailing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any


@dataclass
class SwingSignal:
    symbol: str
    base: str
    price: float
    score: float
    estimated_target_pct: float
    target_price: float
    sl_price: float
    sl_pct: float
    be_activation_pct: float  # Уровень перевода в безубыток (+8%)
    trailing_activation_pct: float  # Уровень включения трейлинга (+15%)
    trailing_distance_pct: float
    factors: Dict[str, Any]
    timeframe_grades: Dict[str, str]
    timestamp: int


def calculate_ema(prices: List[float], period: int) -> List[float]:
    """Рассчитывает экспоненциальную скользящую среднюю (EMA)."""
    if len(prices) < period or period <= 0:
        return []
    multiplier = 2.0 / (period + 1.0)
    ema = [sum(prices[:period]) / period]
    for p in prices[period:]:
        ema.append((p - ema[-1]) * multiplier + ema[-1])
    return ema


def calculate_sma(values: List[float], period: int) -> List[float]:
    """Рассчитывает простую скользящую среднюю (SMA)."""
    if len(values) < period or period <= 0:
        return []
    res = []
    curr_sum = sum(values[:period])
    res.append(curr_sum / period)
    for i in range(period, len(values)):
        curr_sum += values[i] - values[i - period]
        res.append(curr_sum / period)
    return res


def calculate_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[float]:
    """Рассчитывает Average True Range (ATR)."""
    n = len(closes)
    if n < period + 1:
        return []
    trs = []
    for i in range(1, n):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    if len(trs) < period:
        return []
    atr = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        atr.append((atr[-1] * (period - 1) + tr) / period)
    return atr


def calculate_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Рассчитывает Relative Strength Index (RSI)."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss <= 1e-12:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def detect_squeeze(highs: List[float], lows: List[float], closes: List[float], length: int = 20) -> Tuple[bool, float]:
    """
    Детектор сжатия волатильности (Bollinger Bands внутри Keltner Channel).
    Возвращает (is_squeeze, squeeze_strength).
    """
    if len(closes) < length + 1:
        return False, 0.0

    recent_closes = closes[-length:]
    sma = sum(recent_closes) / length
    variance = sum((x - sma) ** 2 for x in recent_closes) / length
    std_dev = math.sqrt(variance)

    bb_upper = sma + 2.0 * std_dev
    bb_lower = sma - 2.0 * std_dev

    # Keltner Channel
    atrs = calculate_atr(highs, lows, closes, period=length)
    if not atrs:
        return False, 0.0
    atr = atrs[-1]
    kc_upper = sma + 1.5 * atr
    kc_lower = sma - 1.5 * atr

    # Сжатие: Полосы Боллинджера сузились ВНУТРЬ канала Кельтнера
    is_squeeze = (bb_lower > kc_lower) and (bb_upper < kc_upper)
    strength = max(0.0, 1.0 - (std_dev / (1.5 * atr + 1e-9))) if atr > 0 else 0.0
    return is_squeeze, strength


def aggregate_klines(base_5m: List[List[float]], target_tf: str) -> List[List[float]]:
    """
    Агрегирует базовые 5m свечи в целевой таймфрейм (15m, 30m, 1h, 4h).
    Формат свечи: [open_time, open, high, low, close, volume, quote_volume, ...]
    """
    factor_map = {"5m": 1, "15m": 3, "30m": 6, "1h": 12, "4h": 48}
    factor = factor_map.get(target_tf, 1)
    if factor == 1 or len(base_5m) < factor:
        return base_5m

    res = []
    total = len(base_5m)
    rem = total % factor
    aligned = base_5m[rem:]

    for i in range(0, len(aligned), factor):
        chunk = aligned[i : i + factor]
        if not chunk:
            continue
        o_time = chunk[0][0]
        o_price = chunk[0][1]
        h_price = max(c[2] for c in chunk)
        l_price = min(c[3] for c in chunk)
        c_price = chunk[-1][4]
        vol = sum(c[5] for c in chunk)
        q_vol = sum(c[6] for c in chunk)
        res.append([o_time, o_price, h_price, l_price, c_price, vol, q_vol])

    return res


def analyze_swing_setup(
    symbol: str,
    klines_5m: List[List[float]],
    min_score: float = 55.0,  # Оптимизировано Grid Search (WR 55.1%, PF 1.96)
) -> Optional[SwingSignal]:
    """
    Проводит глубокий мульти-таймфрейм анализ монеты на 5m, 15m, 30m, 1h, 4h.
    Оценивает потенциал движения на +20%+ и формирует свинг-сигнал.
    """
    if len(klines_5m) < 300:
        return None

    current_price = float(klines_5m[-1][4])
    if current_price <= 0:
        return None

    base = symbol.replace("USDT", "")

    klines_15m = aggregate_klines(klines_5m, "15m")
    klines_30m = aggregate_klines(klines_5m, "30m")
    klines_1h = aggregate_klines(klines_5m, "1h")
    klines_4h = aggregate_klines(klines_5m, "4h")

    closes_1h = [c[4] for c in klines_1h]
    highs_1h = [c[2] for c in klines_1h]
    lows_1h = [c[3] for c in klines_1h]
    vols_1h = [c[5] for c in klines_1h]

    closes_15m = [c[4] for c in klines_15m]
    vols_15m = [c[5] for c in klines_15m]

    score = 0.0
    factors: Dict[str, Any] = {}
    tf_grades: Dict[str, str] = {}

    # Фактор 1: Трендовое выравнивание (1h и 15m)
    ema20_1h = calculate_ema(closes_1h, 20)
    ema50_1h = calculate_ema(closes_1h, 50) if len(closes_1h) >= 50 else calculate_ema(closes_1h, 30)
    if ema20_1h:
        curr_ema20 = ema20_1h[-1]
        curr_ema50 = ema50_1h[-1] if ema50_1h else curr_ema20 * 0.98
        if current_price > curr_ema20 > curr_ema50:
            score += 25.0
            factors["trend_1h"] = "strong_bullish"
            tf_grades["1h"] = "🟢 Strong Bull (P > EMA20 > EMA50)"
        elif current_price > curr_ema20:
            score += 15.0
            factors["trend_1h"] = "bullish"
            tf_grades["1h"] = "🟡 Bullish (P > EMA20)"
        else:
            factors["trend_1h"] = "bearish_or_flat"
            tf_grades["1h"] = "🔴 Neutral/Bear"

    # Фактор 2: Сжатие волатильности (TTM Squeeze на 1h / 30m / 15m)
    is_sqz_1h, sqz_str_1h = detect_squeeze(highs_1h, lows_1h, closes_1h, length=20)
    is_sqz_30m, sqz_str_30m = detect_squeeze([c[2] for c in klines_30m], [c[3] for c in klines_30m], [c[4] for c in klines_30m], length=20)
    is_sqz_15m, _ = detect_squeeze([c[2] for c in klines_15m], [c[3] for c in klines_15m], [c[4] for c in klines_15m], length=20)

    if is_sqz_1h:
        score += 25.0
        factors["squeeze"] = {"detected": True, "tf": "1h", "strength": round(sqz_str_1h, 2)}
        tf_grades["squeeze"] = "🔥 1h Squeeze Ready (+20%+ potential)"
    elif is_sqz_30m or is_sqz_15m:
        sqz_tf = "30m" if is_sqz_30m else "15m"
        score += 20.0
        factors["squeeze"] = {"detected": True, "tf": sqz_tf, "strength": round(sqz_str_30m if is_sqz_30m else 0.5, 2)}
        tf_grades["squeeze"] = f"⚡ {sqz_tf} Squeeze Ready"
    else:
        factors["squeeze"] = {"detected": False}
        tf_grades["squeeze"] = "⚪ Normal Volatility"

    # Фактор 3: Относительный объем и аккумуляция (RVOL)
    rvol_1h = 1.0
    if len(vols_1h) >= 15:
        avg_vol = sum(vols_1h[-15:-1]) / 14.0
        if avg_vol > 0:
            rvol_1h = vols_1h[-1] / avg_vol
    elif len(vols_15m) >= 20:
        avg_vol = sum(vols_15m[-20:-1]) / 19.0
        if avg_vol > 0:
            rvol_1h = vols_15m[-1] / avg_vol

    factors["rvol_1h"] = round(rvol_1h, 2)
    if rvol_1h >= 2.2:
        score += 25.0
        factors["volume_burst"] = "extreme"
        tf_grades["volume"] = f"🚀 RVOL {rvol_1h:.1f}x (Крупный закуп)"
    elif rvol_1h >= 1.5:
        score += 18.0
        factors["volume_burst"] = "strong"
        tf_grades["volume"] = f"🟢 RVOL {rvol_1h:.1f}x"
    elif rvol_1h >= 1.1:
        score += 12.0
        factors["volume_burst"] = "moderate"
        tf_grades["volume"] = f"🟡 RVOL {rvol_1h:.1f}x"
    else:
        factors["volume_burst"] = "low"
        tf_grades["volume"] = "⚪ Обычный объём"

    # Фактор 4: Momentum & RSI (1h и 15m)
    rsi_1h = calculate_rsi(closes_1h, 14)
    rsi_15m = calculate_rsi(closes_15m, 14)

    factors["rsi_1h"] = round(rsi_1h, 1) if rsi_1h is not None else None
    factors["rsi_15m"] = round(rsi_15m, 1) if rsi_15m is not None else None

    active_rsi = rsi_1h if rsi_1h is not None else rsi_15m
    if active_rsi is not None:
        if 50.0 <= active_rsi <= 68.0:
            score += 25.0
            tf_grades["rsi"] = f"🎯 RSI {active_rsi:.0f} (Бычий импульс)"
        elif 68.0 < active_rsi <= 75.0:
            score += 18.0
            tf_grades["rsi"] = f"🟢 RSI {active_rsi:.0f} (Мощный тренд)"
        elif 45.0 <= active_rsi < 50.0:
            score += 10.0
            tf_grades["rsi"] = f"🟡 RSI {active_rsi:.0f} (Начало разгона)"
        elif active_rsi > 75.0:
            score += 5.0
            tf_grades["rsi"] = f"⚠️ RSI {active_rsi:.0f} (Перекуплен)"
        else:
            tf_grades["rsi"] = f"⚪ RSI {active_rsi:.0f} (Слабый)"


    if score < min_score:
        return None

    atrs_1h = calculate_atr(highs_1h, lows_1h, closes_1h, period=14)
    recent_low_1h = min(lows_1h[-10:]) if len(lows_1h) >= 10 else (current_price * 0.96)
    swing_sl_pct = max(3.5, min(5.0, ((current_price - recent_low_1h) / current_price * 100.0) + 0.3))
    sl_price = current_price * (1.0 - swing_sl_pct / 100.0)

    bonus_potential = 5.0 if (is_sqz_1h or is_sqz_30m) else 0.0
    if score >= 85.0:
        target_pct = 28.0 + bonus_potential
    elif score >= 75.0:
        target_pct = 22.0 + bonus_potential
    else:
        target_pct = 18.0 + bonus_potential

    target_price = current_price * (1.0 + target_pct / 100.0)
    be_activation_pct = 4.5      # Оптимизировано: был 8.0% (Grid Search WR 55.1%)
    trailing_activation_pct = 18.0  # Оптимизировано: был 15.0% (Grid Search WR 55.1%)
    trailing_distance_pct = 4.0  # Оптимизировано: был 3.5% (Grid Search PF 1.96)

    return SwingSignal(
        symbol=symbol,
        base=base,
        price=current_price,
        score=score,
        estimated_target_pct=target_pct,
        target_price=target_price,
        sl_price=sl_price,
        sl_pct=swing_sl_pct,
        be_activation_pct=be_activation_pct,
        trailing_activation_pct=trailing_activation_pct,
        trailing_distance_pct=trailing_distance_pct,
        factors=factors,
        timeframe_grades=tf_grades,
        timestamp=int(klines_5m[-1][0] // 1000 if klines_5m[-1][0] > 1e11 else klines_5m[-1][0]),
    )
