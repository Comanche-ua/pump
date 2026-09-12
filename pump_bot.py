#!/usr/bin/env python3
"""
Pump Pulse Scanner (Telegram Bot 2.1)

Сканирует USDT-спот на Binance, считает multi-TF score
(объём, breakout, ATR, RSI, EMA, taker buy, vs BTC, ускорение)
и предоставляет полнофункциональный Telegram-интерфейс:
  - Интерактивное меню и Reply-кнопки
  - Фоновый автоскан рынка в реальном времени с защитой от дублирования
  - Управление портфелем (добавление, удаление, расчёт PnL)
  - Настройки порогов и фильтров через Inline-кнопки
  - Прямые ссылки на графики Binance и TradingView
  - Режим FSM для пошагового ввода позиций
"""

from __future__ import annotations

import os
import sys
import time
import math
import json
import hmac
import hashlib
import signal
import threading
import traceback
import urllib.parse
import urllib.request
import urllib.error
import concurrent.futures as cf
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple, Any, Set, Union

# Настройка UTF-8 вывода для Windows консоли
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Поддержка автоматической загрузки .env (если установлен python-dotenv)
try:
    import dotenv
    dotenv.load_dotenv()
except ImportError:
    # Ручной парсинг .env при отсутствии библиотеки
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip().strip("'\""))
        except Exception:
            pass


# ───────────────────────── Config ─────────────────────────

BINANCE_BASE = (os.environ.get("BINANCE_DATA_BASE") or os.environ.get("BINANCE_BASE") or "").strip() or "https://data-api.binance.vision"
BINANCE_TRADE_URL = (os.environ.get("BINANCE_TRADE_URL") or "").strip() or "https://api.binance.com"

# Настройка прокси (если задан BINANCE_PROXY или PROXY_URL) для обхода региональных ограничений (США/GitHub Actions)
_proxy_cfg = (os.environ.get("BINANCE_PROXY") or os.environ.get("PROXY_URL") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "").strip()
if _proxy_cfg:
    os.environ["HTTP_PROXY"] = _proxy_cfg
    os.environ["HTTPS_PROXY"] = _proxy_cfg
    try:
        _proxy_handler = urllib.request.ProxyHandler({"http": _proxy_cfg, "https": _proxy_cfg})
        _opener = urllib.request.build_opener(_proxy_handler)
        urllib.request.install_opener(_opener)
        print(f"🌐 Прокси успешно подключен: {_proxy_cfg}")
    except Exception as _p_err:
        print(f"⚠️ Ошибка настройки прокси: {_p_err}", file=sys.stderr)

TIMEFRAMES = ("5m", "15m", "1h")
TF_MS = {"5m": 5 * 60_000, "15m": 15 * 60_000, "1h": 60 * 60_000}

DEFAULT_MIN_QUOTE_VOLUME = float(os.environ.get("MIN_QUOTE_VOLUME", "1200000"))
DEFAULT_MIN_SCORE = float(os.environ.get("MIN_SCORE", "58"))
ALREADY_PUMPED_MAX = float(os.environ.get("ALREADY_PUMPED_MAX", "8.0"))  # Защита от перегретых монет: ищем в зародыше (до +8% за 24ч)
MAX_CANDIDATES = int(os.environ.get("MAX_CANDIDATES", "50"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "16"))
KLINES_LIMIT = 250  # 250 баров по 5m (~20 часов подробной истории)

# Параметры спотовой автоторговли
DEFAULT_TRADE_AMOUNT = float(os.environ.get("TRADE_AMOUNT_USDT", "11.0"))
DEFAULT_TAKE_PROFIT = float(os.environ.get("TAKE_PROFIT_PCT", "2.0"))
DEFAULT_STOP_LOSS = float(os.environ.get("STOP_LOSS_PCT", "2.0"))
DEFAULT_ENTRY_PULLBACK = float(os.environ.get("ENTRY_PULLBACK_PCT", "0.4"))  # Вход на микро-откате (-0.4% по умолчанию)
DEFAULT_ENTRY_TIMEOUT_SEC = int(os.environ.get("ENTRY_TIMEOUT_SEC", "300"))   # Таймаут жизни лимитного ордера на вход (5 мин)
DEFAULT_TRAILING_ACTIVATION = float(os.environ.get("TRAILING_ACTIVATION_PCT", "1.0"))
DEFAULT_TRAILING_DISTANCE = float(os.environ.get("TRAILING_DISTANCE_PCT", "0.8"))
DEFAULT_DYNAMIC_TP = os.environ.get("DYNAMIC_TP", "true").strip().lower() in ("true", "1")
DEFAULT_AUTO_TRADE = os.environ.get("AUTO_TRADE", "false").strip().lower() in ("true", "1")
DEFAULT_TRADE_MIN_SCORE = float(os.environ.get("TRADE_MIN_SCORE", "70.0"))

IS_CI = (
    os.environ.get("GITHUB_ACTIONS") == "true"
    or os.environ.get("CI") == "true"
    or os.environ.get("CONTINUOUS_INTEGRATION") == "true"
)
DEFAULT_RUN_MODE = "oneshot" if IS_CI else "bot"
RUN_MODE = os.environ.get("RUN_MODE", DEFAULT_RUN_MODE).strip().lower()
STATE_FILE = os.environ.get("STATE_FILE", "bot_state.json")
DEFAULT_SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL_SEC", "300"))

STABLE_OR_FIAT = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "EUR", "EURI", "AEUR",
    "TRY", "BRL", "ARS", "IDRT", "UAH", "NGN", "GBP", "AUD",
    "USDP", "USDS", "USD1", "PYUSD", "RLUSD", "BFUSD",
}
LEV_RE = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")

# ───────────────────────── HTTP ─────────────────────────

def http_get_json(url: str, timeout: int = 12) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "pump-pulse/2.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

# ───────────────────────── Types ─────────────────────────

@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades: int
    taker_buy_base: float
    close_time: int

@dataclass
class FactorScore:
    id: str
    label: str
    weight: float
    value: float
    note: str

@dataclass
class TfBreakdown:
    timeframe: str
    score: float
    grade: str          # strong | watch | late | none
    volume_ratio: float
    atr_expansion: float
    breakout_pct: float
    rsi: float
    taker_buy: float
    vs_btc_pct: float
    change_pct: float
    ema_aligned: bool
    late: bool
    forming: bool
    bar_open_time: int
    factors: List[FactorScore] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)

@dataclass
class PumpSignal:
    symbol: str
    base: str
    price: float
    change_24h: float
    quote_volume_24h: float
    high_24h: float
    low_24h: float
    btc_relative_24h: float
    best_tf: str
    best_score: float
    grade: str
    alert_key: str
    by_tf: List[TfBreakdown]

# ───────────────────────── Formatters ─────────────────────────

def fmt_price(val: Optional[float]) -> str:
    """Адаптивное форматирование цены (для BTC и для микро-токенов типа PEPE)."""
    if val is None:
        return "—"
    if val == 0:
        return "0.00"
    abs_val = abs(val)
    if abs_val >= 1000:
        return f"{val:,.2f}"
    if abs_val >= 1:
        return f"{val:.4f}".rstrip("0").rstrip(".")
    if abs_val >= 0.0001:
        return f"{val:.6f}".rstrip("0").rstrip(".")
    return f"{val:.8f}".rstrip("0").rstrip(".")

def fmt_qty(val: Optional[float]) -> str:
    """Форматирование количества актива без лишних нулей."""
    if val is None:
        return "0"
    if val >= 1000:
        return f"{val:,.4f}".rstrip("0").rstrip(".")
    if val >= 1:
        return f"{val:.4f}".rstrip("0").rstrip(".")
    return f"{val:.8f}".rstrip("0").rstrip(".")

def fmt_pct(val: Optional[float]) -> str:
    if val is None:
        return "—"
    return f"{val:+.2f}%"

# ───────────────────────── Indicators ─────────────────────────

def clamp(n: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, n))

def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period

def ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    for v in values[period:]:
        prev = v * k + prev * (1 - k)
    return prev

def rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)

def true_range(curr: Candle, prev: Candle) -> float:
    return max(
        curr.high - curr.low,
        abs(curr.high - prev.close),
        abs(curr.low - prev.close),
    )

def atr(candles: List[Candle], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs = [true_range(candles[i], candles[i - 1]) for i in range(1, len(candles))]
    val = sum(trs[:period]) / period
    for tr in trs[period:]:
        val = (val * (period - 1) + tr) / period
    return val

def donchian_high(candles: List[Candle], period: int) -> Optional[float]:
    if len(candles) < period:
        return None
    return max(c.high for c in candles[-period:])

def stddev(values: List[float], period: int = 20) -> Optional[float]:
    """Среднеквадратичное отклонение цены."""
    if len(values) < period:
        return None
    sub = values[-period:]
    mean = sum(sub) / period
    variance = sum((x - mean) ** 2 for x in sub) / period
    return math.sqrt(variance)

def bollinger_squeeze(closes: List[float], period: int = 20) -> float:
    """
    Рассчитывает степень сжатия волатильности (BandWidth / SMA).
    Чем меньше значение (< 0.035..0.045), тем сильнее сжата 'пружина' перед выстрелом.
    Возвращает 1.0 (максимальное сжатие) .. 0.0 (сильная растянутость).
    """
    if len(closes) < period:
        return 0.5
    mean = sma(closes, period)
    sd = stddev(closes, period)
    if not mean or not sd or mean <= 0:
        return 0.5
    bandwidth = (2.0 * sd * 2.0) / mean  # 4 * sigma / mean
    # Если bandwidth <= 0.03 (3%), сжатие максимальное (1.0). Если > 0.08, сжатия нет (0.0).
    return clamp(1.0 - (bandwidth - 0.025) / 0.055, 0.0, 1.0)

def pct_change(from_: float, to: float) -> float:
    if from_ == 0:
        return 0.0
    return (to - from_) / from_ * 100

def roc(closes: List[float], bars: int) -> float:
    if len(closes) < bars + 1:
        return 0.0
    return pct_change(closes[-(bars + 1)], closes[-1])

def aggregate_timeframe(candles: List[Candle], ms: int) -> List[Candle]:
    groups: Dict[int, List[Candle]] = {}
    for c in candles:
        key = (c.open_time // ms) * ms
        groups.setdefault(key, []).append(c)
    out = []
    for key in sorted(groups):
        g = groups[key]
        first, last = g[0], g[-1]
        out.append(Candle(
            open_time=key,
            open=first.open,
            high=max(c.high for c in g),
            low=min(c.low for c in g),
            close=last.close,
            volume=sum(c.volume for c in g),
            quote_volume=sum(c.quote_volume for c in g),
            trades=sum(c.trades for c in g),
            taker_buy_base=sum(c.taker_buy_base for c in g),
            close_time=last.close_time,
        ))
    return out
# ───────────────────────── Universe / Tickers ─────────────────────────

def is_usdt_spot(symbol: str) -> bool:
    if not symbol.endswith("USDT"):
        return False
    if "_" in symbol or "USDUSDT" in symbol:
        return False
    if any(x in symbol for x in LEV_RE):
        return False
    base = symbol[:-4]
    if base in STABLE_OR_FIAT or len(base) < 2:
        return False
    return True

def base_asset(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol

def get_24h_tickers() -> List[dict]:
    data = http_get_json(f"{BINANCE_BASE}/api/v3/ticker/24hr")
    return [
        {
            "symbol": d["symbol"],
            "lastPrice": float(d["lastPrice"]),
            "priceChangePercent": float(d["priceChangePercent"]),
            "highPrice": float(d["highPrice"]),
            "lowPrice": float(d["lowPrice"]),
            "quoteVolume": float(d["quoteVolume"]),
            "volume": float(d["volume"]),
        }
        for d in data
        if is_usdt_spot(d["symbol"])
    ]

def pick_candidates(tickers: List[dict], min_quote_volume: float = DEFAULT_MIN_QUOTE_VOLUME) -> List[dict]:
    liquid = [
        t for t in tickers
        if t["quoteVolume"] >= min_quote_volume
        and -4.0 <= t["priceChangePercent"] <= ALREADY_PUMPED_MAX  # Отсекаем дампы (<-4%) и уже взлетевшие (>+8%)
    ]
    # Приоритет 1: Умеренный рост в зародыше (+0.5% .. +4%)
    by_germ = sorted(
        liquid,
        key=lambda t: t["quoteVolume"] if (0.5 <= t["priceChangePercent"] <= 4.5) else 0.0,
        reverse=True,
    )
    # Приоритет 2: Высокая ликвидность в узком флэте (-1.0% .. +2.0%)
    by_flat = sorted(
        liquid,
        key=lambda t: t["quoteVolume"] if (-1.5 <= t["priceChangePercent"] <= 2.5) else 0.0,
        reverse=True,
    )
    # Приоритет 3: Общая активность
    by_vol = sorted(liquid, key=lambda t: t["quoteVolume"], reverse=True)

    picked: Dict[str, dict] = {}
    for t in by_germ[:24] + by_flat[:20] + by_vol[:16]:
        picked[t["symbol"]] = t
    picked.pop("BTCUSDT", None)
    return list(picked.values())[:MAX_CANDIDATES]

# ───────────────────────── Klines ─────────────────────────

def parse_kline(raw: list) -> Candle:
    return Candle(
        open_time=int(raw[0]),
        open=float(raw[1]),
        high=float(raw[2]),
        low=float(raw[3]),
        close=float(raw[4]),
        volume=float(raw[5]),
        close_time=int(raw[6]),
        quote_volume=float(raw[7]),
        trades=int(raw[8]),
        taker_buy_base=float(raw[9]),
    )

def fetch_klines(symbol: str, limit: int = KLINES_LIMIT) -> List[Candle]:
    url = f"{BINANCE_BASE}/api/v3/klines?symbol={symbol}&interval=5m&limit={limit}"
    try:
        raw = http_get_json(url, timeout=10)
        return [parse_kline(r) for r in raw]
    except Exception:
        return []

# ───────────────────────── Scoring Engine ─────────────────────────

MIN_BARS = 32

def grade_from(score: float, late: bool) -> str:
    if score < 48:
        return "none"
    if late and score >= 62:
        return "late"
    if score >= 70:
        return "strong"
    if score >= 58:
        return "watch"
    return "none"

def score_window(
    timeframe: str,
    candles: List[Candle],
    btc_candles: List[Candle],
    change_24h: float,
    forming: bool,
    strict: bool = True,
) -> Optional[TfBreakdown]:
    if len(candles) < MIN_BARS:
        return None

    last = candles[-1]
    hist = candles[:-1]
    closes = [c.close for c in candles]
    hist_closes = [c.close for c in hist]
    vols = [c.volume for c in hist]

    vol_sma = sma(vols, 20)
    if not vol_sma or vol_sma <= 0:
        return None

    vol_ratio = last.volume / vol_sma
    range_ = last.high - last.low
    if range_ <= 0:
        return None

    body = abs(last.close - last.open)
    upper_wick = last.high - max(last.close, last.open)
    close_pos = (last.close - last.low) / range_
    body_ratio = body / range_
    upper_wick_ratio = upper_wick / range_
    bullish = last.close >= last.open

    # 1. Сжатие волатильности (Bollinger Squeeze) перед пампом (пружина)
    squeeze_val = bollinger_squeeze(hist_closes, 20)  # 0..1 (1 = максимальное сжатие флэта)

    # 2. Taker Buy Ratio (агрессия покупателей по рынку)
    taker_buy = last.taker_buy_base / last.volume if last.volume > 0 else 0.5

    # 3. ATR и относительное расширение
    atr14 = atr(hist, 14)
    if not atr14 or atr14 <= 0:
        return None
    atr_expansion = range_ / atr14

    # 4. Пробой локального коридора консолидации
    prior_high = donchian_high(hist, 14)
    if prior_high is None:
        return None
    breakout_pct = pct_change(prior_high, last.close)

    # 5. RSI 14
    rsi14 = rsi(closes, 14)
    if rsi14 is None:
        return None

    # 6. Трендовые EMA
    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema_aligned = (
        ema9 is not None and ema21 is not None
        and last.close >= ema9 and ema9 >= ema21
    )

    # 7. Динамика против BTC
    lookback = 4 if timeframe == "1h" else 6
    asset_roc = roc(closes, lookback)
    btc_closes = [c.close for c in btc_candles]
    btc_roc = roc(btc_closes, lookback) if len(btc_closes) >= lookback + 1 else 0.0
    vs_btc_pct = asset_roc - btc_roc

    prev_roc = roc(closes[:-1], max(1, lookback - 1))
    accel = asset_roc - prev_roc
    change_pct = pct_change(last.open, last.close)

    # Проверка активности сделок (Trade Count / защита от фейкового объёма wash-trading)
    trades_hist = [float(c.trades) for c in hist]
    trades_sma = sma(trades_hist, 20)
    trades_ratio = (float(last.trades) / trades_sma) if (trades_sma and trades_sma > 0) else 1.0

    # ── ФАКТОРЫ СКОРИНГА В ЗАРОДЫШЕ (EARLY PUMP ENGINE) ──
    # 1. Всплеск объема на покупку (25б): ищем от 2.0x до 5.0x
    # Фильтр фейкового объёма: если объём вырос в 2.2x+, а число сделок < 1.25x -> штрафуем
    fake_vol_penalty = 1.0
    if vol_ratio >= 2.0 and trades_ratio < 1.25:
        fake_vol_penalty = clamp(trades_ratio / 1.5, 0.25, 0.8)

    vol_factor = clamp((vol_ratio - 1.2) / 3.0, 0.0, 1.0) * fake_vol_penalty

    # 2. Доминирование рыночных покупок Taker Buy (22б): от 55% до 80%+
    taker_factor = clamp((taker_buy - 0.52) / 0.26, 0.0, 1.0)

    # 3. Сжатие диапазона консолидации перед выстрелом (15б)
    squeeze_factor = squeeze_val

    # 4. Пробой локального флэта без перегрева (12б)
    # Идеальный ранний пробой: от +0.3% до +2.5% выше коридора
    if 0.2 <= breakout_pct <= 2.8:
        breakout_factor = clamp(breakout_pct / 2.0, 0.2, 1.0)
    elif breakout_pct > 2.8:
        breakout_factor = clamp(1.0 - (breakout_pct - 2.8) / 3.0, 0.0, 0.7)
    else:
        breakout_factor = 0.0

    # 5. Качество свечи (10б)
    quality = 0.0
    if bullish:
        quality += 0.4
    quality += clamp(body_ratio * 1.0, 0.0, 0.3)
    quality += clamp(close_pos * 0.3, 0.0, 0.3)
    quality = clamp(quality, 0.0, 1.0)

    # 6. Окно RSI (8б): идеальное окно зарождения 48..65
    rsi_factor = 0.0
    if 48 <= rsi14 <= 66:
        rsi_factor = 1.0 - abs(rsi14 - 58) / 18.0
    elif 66 < rsi14 <= 74:
        rsi_factor = clamp(1.0 - (rsi14 - 66) / 12.0, 0.2, 0.7)
    elif 40 <= rsi14 < 48:
        rsi_factor = 0.4

    # 7. Выравнивание EMA (4б)
    ema_factor = 1.0 if ema_aligned else 0.3

    # 8. Опережение BTC (4б)
    vs_btc_factor = clamp((vs_btc_pct + 0.3) / 2.0, 0.0, 1.0)

    # Жесткий фильтр перегретости (LATE / PUMP OVER)
    # Если монета уже выросла за сутки более 7%, или RSI > 74, или свеча уже улетела > 4.5%
    late = (
        change_24h > 7.0
        or rsi14 >= 74
        or breakout_pct > 4.0
        or (vol_ratio > 7.0 and upper_wick_ratio > 0.35)
        or (vol_ratio >= 3.0 and trades_ratio < 0.9)  # Явный wash-trading / накрутка
        or not bullish  # Исключаем красные свечи (дампы)
    )

    vol_note = f"{vol_ratio:.1f}× SMA20"
    if trades_ratio < 1.2 and vol_ratio >= 2.0:
        vol_note += f" (сделок {trades_ratio:.1f}× ⚠️)"

    factors = [
        FactorScore("vol", "Всплеск объёма", 25, vol_factor, vol_note),
        FactorScore("taker", "Агрессия покупок", 22, taker_factor, f"{taker_buy*100:.0f}% Taker Buy"),
        FactorScore("squeeze", "Сжатие пружины", 15, squeeze_factor, f"Сжатие {squeeze_val*100:.0f}%"),
        FactorScore("breakout", "Пробой флэта", 12, breakout_factor, f"{breakout_pct:+.2f}%"),
        FactorScore("candle", "Структура свечи", 10, quality, f"Бычья, тело {body_ratio*100:.0f}%"),
        FactorScore("rsi", "Окно RSI 14", 8, rsi_factor, f"{rsi14:.1f} (ранняя зона)"),
        FactorScore("ema", "Тренд EMA", 4, ema_factor, "Выровнен" if ema_aligned else "Нейтрален"),
        FactorScore("btc", "Опережение BTC", 4, vs_btc_factor, f"{vs_btc_pct:+.2f}%"),
    ]

    score = sum(f.value * f.weight for f in factors)
    grade = grade_from(score, late)
    if strict and grade == "none":
        return None

    reasons = []
    if vol_ratio >= 2.0:
        reasons.append(f"приток объёма {vol_ratio:.1f}× к среднему")
    if taker_buy >= 0.62:
        reasons.append(f"доминируют рыночные покупки ({taker_buy*100:.0f}%)")
    if squeeze_val >= 0.65:
        reasons.append("выход из длительного сжатия волатильности")
    if 0.3 <= breakout_pct <= 2.5:
        reasons.append(f"аккуратный пробой базы ({breakout_pct:+.1f}%)")
    if 50 <= rsi14 <= 66:
        reasons.append(f"RSI {rsi14:.0f} — запас хода вверх без перегрева")
    if ema_aligned:
        reasons.append("структура тренда EMA 9/21 бычья")

    risks = []
    if late:
        risks.append("монета уже дала ход, риск разгрузки")
    if rsi14 >= 72:
        risks.append("RSI в зоне перекупленности")
    if upper_wick_ratio > 0.28:
        risks.append("длинная верхняя тень (сопротивление)")
    if change_24h > 6.0:
        risks.append(f"суточный рост {change_24h:+.1f}% (поздняя фаза)")

    return TfBreakdown(
        timeframe=timeframe,
        score=score,
        grade=grade,
        volume_ratio=vol_ratio,
        atr_expansion=atr_expansion,
        breakout_pct=breakout_pct,
        rsi=rsi14,
        taker_buy=taker_buy,
        vs_btc_pct=vs_btc_pct,
        change_pct=change_pct,
        ema_aligned=ema_aligned,
        late=late,
        forming=forming,
        bar_open_time=last.open_time,
        factors=factors,
        reasons=reasons,
        risks=risks,
    )

def pick_best(rows: List[TfBreakdown]) -> TfBreakdown:
    order = {"strong": 0, "watch": 1, "late": 2, "none": 3}
    return sorted(rows, key=lambda r: (order.get(r.grade, 9), -r.score))[0]

# ───────────────────────── Scan ─────────────────────────

def analyze_symbol(
    ticker: dict,
    raw15: List[Candle],
    btc_raw15: List[Candle],
    btc_ticker: Optional[dict] = None,
    min_score: float = DEFAULT_MIN_SCORE,
) -> Tuple[Optional[PumpSignal], Optional[dict]]:
    """
    Возвращает (PumpSignal если прошёл порог, candidate_summary с лучшим TF для отчёта).
    """
    if len(raw15) < MIN_BARS:
        return None, None

    btc_change = btc_ticker["priceChangePercent"] if btc_ticker else 0.0
    btc_rel_24h = ticker["priceChangePercent"] - btc_change

    all_rows: List[TfBreakdown] = []
    for tf in TIMEFRAMES:
        candles = raw15 if tf == "5m" else aggregate_timeframe(raw15, TF_MS[tf])
        btc_c = btc_raw15 if tf == "5m" else aggregate_timeframe(btc_raw15, TF_MS[tf])
        bd = score_window(tf, candles, btc_c, ticker["priceChangePercent"], forming=True, strict=False)
        if bd:
            all_rows.append(bd)

    if not all_rows:
        return None, None

    best = pick_best(all_rows)
    summary = {
        "symbol": ticker["symbol"],
        "base": base_asset(ticker["symbol"]),
        "price": ticker["lastPrice"],
        "change_24h": ticker["priceChangePercent"],
        "best_score": best.score,
        "best_tf": best.timeframe,
        "grade": best.grade,
        "vol_ratio": best.volume_ratio,
    }

    if best.score < min_score or best.grade == "none":
        return None, summary

    valid_rows = [r for r in all_rows if r.grade != "none"]
    if not valid_rows:
        valid_rows = [best]

    signal_obj = PumpSignal(
        symbol=ticker["symbol"],
        base=base_asset(ticker["symbol"]),
        price=ticker["lastPrice"],
        change_24h=ticker["priceChangePercent"],
        quote_volume_24h=ticker["quoteVolume"],
        high_24h=ticker["highPrice"],
        low_24h=ticker["lowPrice"],
        btc_relative_24h=btc_rel_24h,
        best_tf=best.timeframe,
        best_score=best.score,
        grade=best.grade,
        alert_key=f"{ticker['symbol']}:{best.timeframe}:{best.bar_open_time}",
        by_tf=valid_rows,
    )
    return signal_obj, summary

def run_scan(
    min_score: Optional[float] = None,
    min_quote_volume: Optional[float] = None,
) -> Tuple[List[PumpSignal], dict, List[dict]]:
    started = time.time()
    score_threshold = min_score if min_score is not None else DEFAULT_MIN_SCORE
    volume_threshold = min_quote_volume if min_quote_volume is not None else DEFAULT_MIN_QUOTE_VOLUME

    tickers = get_24h_tickers()
    universe = len(tickers)
    candidates = pick_candidates(tickers, min_quote_volume=volume_threshold)

    btc_raw = fetch_klines("BTCUSDT")
    btc_ticker = next((t for t in tickers if t["symbol"] == "BTCUSDT"), None)

    signals: List[PumpSignal] = []
    summaries: List[dict] = []

    def worker(t: dict) -> Tuple[Optional[PumpSignal], Optional[dict]]:
        raw = fetch_klines(t["symbol"])
        if not raw:
            return None, None
        return analyze_symbol(t, raw, btc_raw, btc_ticker, min_score=score_threshold)

    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for sig, smm in pool.map(worker, candidates):
            if sig:
                signals.append(sig)
            if smm:
                summaries.append(smm)

    order = {"strong": 0, "watch": 1, "late": 2}
    signals.sort(key=lambda s: (order.get(s.grade, 9), -s.best_score))
    summaries.sort(key=lambda s: -s["best_score"])

    meta = {
        "universe": universe,
        "candidates": len(candidates),
        "duration_ms": int((time.time() - started) * 1000),
        "min_score": score_threshold,
        "btc": {
            "price": btc_ticker["lastPrice"] if btc_ticker else None,
            "change24h": btc_ticker["priceChangePercent"] if btc_ticker else None,
        },
    }
    return signals, meta, summaries[:5]
# ───────────────────────── Состояние: портфель и настройки ─────────────────────────

STATE_LOCK = threading.Lock()

def default_state() -> dict:
    return {
        "portfolio": {},   # symbol -> {"qty": float, "avg_price": float, "added_at": int}
        "settings": {
            "min_score": DEFAULT_MIN_SCORE,
            "min_quote_volume": DEFAULT_MIN_QUOTE_VOLUME,
            "autoscan": True,
            "scan_interval_sec": DEFAULT_SCAN_INTERVAL,
            "filter_level": "strong_and_watch",  # "strong_only" или "strong_and_watch"
            "auto_trade": DEFAULT_AUTO_TRADE,
            "trade_amount_usdt": DEFAULT_TRADE_AMOUNT,
            "take_profit_pct": DEFAULT_TAKE_PROFIT,
            "max_open_trades": 3,
            "trade_min_score": DEFAULT_TRADE_MIN_SCORE,
        },
        "active_trades": {},      # symbol -> trade dict
        "trade_history": [],      # list of closed trades
        "sent_alerts": {},        # pump alert_key -> timestamp
        "allowed_chats": [],
    }

def load_state() -> dict:
    d = default_state()
    if not os.path.exists(STATE_FILE):
        return d
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        d["portfolio"].update(data.get("portfolio", {}))
        d["settings"].update(data.get("settings", {}))
        d["active_trades"].update(data.get("active_trades", {}))
        d["trade_history"] = data.get("trade_history", [])[-50:]
        sent = data.get("sent_alerts", {})
        if isinstance(sent, list):
            # миграция со старого формата списка
            now = int(time.time())
            d["sent_alerts"] = {k: now for k in sent}
        elif isinstance(sent, dict):
            d["sent_alerts"] = sent
        d["allowed_chats"] = list(set(data.get("allowed_chats", [])))
    except Exception as e:
        print(f"Не удалось прочитать {STATE_FILE}: {e}", file=sys.stderr)
    return d

def _git_sync_state() -> None:
    """Фоновая фиксация изменений состояния в репозиторий GitHub при работе в CI."""
    try:
        import subprocess
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], capture_output=True, timeout=5)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], capture_output=True, timeout=5)
        subprocess.run(["git", "add", STATE_FILE], capture_output=True, timeout=5)
        subprocess.run(["git", "commit", "-m", "Auto-update portfolio [skip ci]"], capture_output=True, timeout=5)
        subprocess.run(["git", "push"], capture_output=True, timeout=10)
    except Exception:
        pass

def save_state(state: dict, sync_git: bool = False) -> None:
    with STATE_LOCK:
        # Очищаем устаревшие алерты (старше 24ч)
        cutoff = int(time.time()) - 86400
        state["sent_alerts"] = {k: ts for k, ts in state["sent_alerts"].items() if ts > cutoff}

        tmp = STATE_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            print(f"Не удалось сохранить {STATE_FILE}: {e}", file=sys.stderr)

        if sync_git and os.environ.get("GITHUB_ACTIONS") == "true":
            threading.Thread(target=_git_sync_state, daemon=True).start()

def normalize_symbol(raw: str) -> str:
    s = raw.strip().upper().replace(" ", "").replace("/", "")
    if s.endswith("USDT"):
        return s
    return s + "USDT"

def get_price(symbol: str) -> Optional[float]:
    try:
        data = http_get_json(f"{BINANCE_BASE}/api/v3/ticker/price?symbol={symbol}", timeout=6)
        return float(data["price"])
    except Exception:
        return None

def get_multiple_prices(symbols: List[str]) -> Dict[str, Optional[float]]:
    """Параллельный опрос цен для списка монет (для моментального рендера портфеля)."""
    if not symbols:
        return {}
    res: Dict[str, Optional[float]] = {}
    with cf.ThreadPoolExecutor(max_workers=min(8, len(symbols))) as pool:
        future_map = {pool.submit(get_price, sym): sym for sym in symbols}
        for fut in cf.as_completed(future_map):
            sym = future_map[fut]
            try:
                res[sym] = fut.result()
            except Exception:
                res[sym] = None
    return res

def portfolio_add(state: dict, symbol: str, qty: float, price: float) -> Tuple[float, float]:
    """Добавляет/докупает позицию и возвращает (новое_количество, средняя_цена)."""
    symbol = normalize_symbol(symbol)
    pos = state["portfolio"].get(symbol)
    if pos:
        old_qty, old_avg = float(pos["qty"]), float(pos["avg_price"])
        new_qty = old_qty + qty
        new_avg = (old_qty * old_avg + qty * price) / new_qty if new_qty > 0 else price
        pos["qty"], pos["avg_price"] = new_qty, new_avg
    else:
        new_qty, new_avg = qty, price
        state["portfolio"][symbol] = {"qty": qty, "avg_price": price, "added_at": int(time.time())}
    save_state(state, sync_git=True)
    return new_qty, new_avg

def portfolio_remove(state: dict, symbol: str) -> bool:
    symbol = normalize_symbol(symbol)
    if symbol in state["portfolio"]:
        state["portfolio"].pop(symbol, None)
        save_state(state, sync_git=True)
        return True
    return False

# ───────────────────────── Binance Spot Trading Engine ─────────────────────────

_binance_time_offset_ms: int = 0
_binance_time_last_sync: float = 0.0

def sync_binance_time(force: bool = False) -> int:
    """
    Синхронизирует локальное время с сервером Binance для предотвращения
    ошибки -1021 Timestamp for this request was outside recvWindow.
    """
    global _binance_time_offset_ms, _binance_time_last_sync
    now_mono = time.monotonic()
    if not force and (now_mono - _binance_time_last_sync < 300):
        return _binance_time_offset_ms
    try:
        url = f"{BINANCE_TRADE_URL}/api/v3/time"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            server_time = int(data["serverTime"])
            local_time = int(time.time() * 1000)
            _binance_time_offset_ms = server_time - local_time
            _binance_time_last_sync = now_mono
    except Exception as e:
        print(f"[Binance Time Sync Warning]: {e}", file=sys.stderr)
    return _binance_time_offset_ms

def sanitize_api_key(val: str) -> str:
    """Очищает API-ключ от лишних кавычек, пробелов и префиксов."""
    if not val:
        return ""
    cleaned = val.strip().strip("'\"`")
    if "=" in cleaned:
        cleaned = cleaned.split("=", 1)[1].strip().strip("'\"`")
    return cleaned

def get_api_credentials(state: Optional[dict] = None) -> Tuple[str, str]:
    """Возвращает (api_key, api_secret) с приоритетом настроек бота над переменными окружения."""
    key = ""
    secret = ""
    if state:
        key = sanitize_api_key(state.get("settings", {}).get("binance_api_key", ""))
        secret = sanitize_api_key(state.get("settings", {}).get("binance_api_secret", ""))
    if not key:
        key = sanitize_api_key(os.environ.get("BINANCE_API_KEY", ""))
    if not secret:
        secret = sanitize_api_key(os.environ.get("BINANCE_API_SECRET", ""))
    return key, secret

def binance_signed_request(
    method: str,
    endpoint: str,
    params: Optional[dict] = None,
    state: Optional[dict] = None,
) -> dict:
    """
    Выполняет защищенный HMAC-SHA256 запрос к торговому API Binance (/api/v3/*).
    """
    api_key, api_secret = get_api_credentials(state)
    if not api_key or not api_secret:
        return {"error": "API-ключи Binance не настроены (BINANCE_API_KEY / BINANCE_API_SECRET)"}

    offset = sync_binance_time()
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000) + offset
    p["recvWindow"] = 5000

    query_str = urllib.parse.urlencode(p)
    sig = hmac.new(api_secret.encode("utf-8"), query_str.encode("utf-8"), hashlib.sha256).hexdigest()
    signed_query = f"{query_str}&signature={sig}"

    headers = {
        "X-MBX-APIKEY": api_key,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PumpPulseBot/2.1",
    }

    url = f"{BINANCE_TRADE_URL}{endpoint}"
    method_up = method.upper()

    if method_up in ("GET", "DELETE"):
        full_url = f"{url}?{signed_query}"
        req = urllib.request.Request(full_url, headers=headers, method=method_up)
    else:  # POST, PUT
        req = urllib.request.Request(
            url,
            data=signed_query.encode("utf-8"),
            headers=headers,
            method=method_up,
        )
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        try:
            err_json = json.loads(err_body)
            return {"error": err_json.get("msg", err_body), "code": err_json.get("code", e.code)}
        except Exception:
            return {"error": f"HTTP {e.code}: {err_body}", "code": e.code}
    except Exception as e:
        return {"error": str(e)}

def get_spot_account_assets(state: Optional[dict] = None) -> Tuple[Dict[str, dict], Optional[str]]:
    """
    Возвращает детализированный баланс спотового аккаунта Binance:
    {asset: {"free": float, "locked": float, "total": float}}
    """
    res = binance_signed_request("GET", "/api/v3/account", state=state)
    if "error" in res or "balances" not in res:
        err_msg = res.get("error", "Неизвестная ошибка")
        return {}, str(err_msg)
    assets: Dict[str, dict] = {}
    for b in res["balances"]:
        free = float(b.get("free", 0.0))
        locked = float(b.get("locked", 0.0))
        total = free + locked
        if total > 0.00000001:
            assets[b["asset"]] = {"free": free, "locked": locked, "total": total}
    return assets, None

def get_spot_balances(state: Optional[dict] = None) -> Tuple[Dict[str, float], Optional[str]]:
    """Возвращает (словарь_балансов_free, текст_ошибки_если_есть)."""
    assets, err = get_spot_account_assets(state=state)
    if err:
        return {}, err
    return {k: v["free"] for k, v in assets.items()}, None

def get_free_usdt_balance(state: Optional[dict] = None) -> float:
    """Возвращает свободный баланс USDT на спотовом кошельке Binance."""
    balances, _ = get_spot_balances(state=state)
    return balances.get("USDT", 0.0)

def format_binance_balance_detailed(state: Optional[dict] = None) -> str:
    """
    Формирует подробную расшифровку баланса Binance:
    - Свободный и заблокированный USDT
    - Оценка каждого актива в USDT по текущему рынку
    - Точный PnL (прирост / убыток) и цена входа
    - Итоговый капитал всего портфеля в USDT
    """
    if state is None:
        state = load_state()

    api_key, api_secret = get_api_credentials(state)
    if not api_key or not api_secret:
        return (
            "⚠️ <b>API-ключи Binance не настроены.</b>\n\n"
            "Чтобы просматривать баланс и совершать сделки, привяжите ключи командой:\n"
            "<code>/api ВАШ_KEY ВАШ_SECRET</code>\n"
            "или через меню ⚙️ Настройки."
        )

    # Синхронизируем открытые позиции и сделки
    sync_trades_and_active_positions(state)

    assets, err = get_spot_account_assets(state)
    if err:
        return f"❌ <b>Ошибка получения баланса Binance:</b>\n<code>{err}</code>"

    usdt_info = assets.get("USDT", {"free": 0.0, "locked": 0.0, "total": 0.0})
    usdt_free = usdt_info["free"]
    usdt_locked = usdt_info["locked"]
    usdt_total = usdt_info["total"]

    # Собираем список всех ненулевых альткоинов/монет
    non_usdt_assets = {k: v for k, v in assets.items() if k != "USDT" and v["total"] > 0.00000001}

    # Запрашиваем цены в USDT для всех найденных монет
    symbols_to_fetch = [f"{a}USDT" for a in non_usdt_assets.keys()]
    prices = get_multiple_prices(symbols_to_fetch) if symbols_to_fetch else {}

    # Запрашиваем 24ч статистику
    tickers = get_24h_tickers()
    ticker_map = {t["symbol"]: float(t.get("priceChangePercent", 0.0)) for t in tickers if "symbol" in t}

    lines = ["💳 <b>Баланс и активы на Binance Spot</b>\n"]

    # Блок USDT
    lines.append("💵 <b>Стейблкоин баланс (USDT):</b>")
    lines.append(f"• <b>Свободно:</b> <code>{usdt_free:,.2f} USDT</code>")
    if usdt_locked > 0.001:
        lines.append(f"• <b>В ордерах:</b> <code>{usdt_locked:,.2f} USDT</code>")
    lines.append(f"• <b>Всего USDT:</b> <code>{usdt_total:,.2f} USDT</code>\n")

    # Блок криптовалютных активов
    total_crypto_value = 0.0
    total_tracked_cost = 0.0
    total_tracked_val = 0.0
    items_lines = []

    active_trades = state.get("active_trades", {}) if state else {}
    portfolio = state.get("portfolio", {}) if state else {}

    for asset, info in sorted(non_usdt_assets.items(), key=lambda x: x[0]):
        tot_qty = info["total"]
        free_qty = info["free"]
        lock_qty = info["locked"]
        pair = f"{asset}USDT"
        price = prices.get(pair)

        # Стейблкоины оцениваем 1:1
        if price is None and asset in STABLE_OR_FIAT:
            price = 1.0

        if price is not None:
            val_usdt = tot_qty * price
            # Скрываем пыль меньше $0.05 если монет микроскопически мало
            if val_usdt < 0.05 and tot_qty < 0.0001:
                continue
            total_crypto_value += val_usdt
            lock_str = f" <i>(в TP: {fmt_qty(lock_qty)})</i>" if lock_qty > 0.000001 else ""
            tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{pair}"
            binance_url = f"https://www.binance.com/en/trade/{asset}_USDT?type=spot"

            # Определяем цену входа из активных сделок бота, портфеля или Binance API
            trade_rec = active_trades.get(pair)
            port_rec = portfolio.get(pair)
            buy_price = 0.0
            if trade_rec:
                buy_price = float(trade_rec.get("buy_price", 0.0))
            elif port_rec:
                buy_price = float(port_rec.get("avg_price", 0.0))

            if buy_price <= 0:
                try:
                    my_tr = binance_signed_request("GET", "/api/v3/myTrades", {"symbol": pair, "limit": 5}, state=state)
                    if isinstance(my_tr, list) and my_tr:
                        buys = [t for t in my_tr if t.get("isBuyer")]
                        if buys:
                            buy_price = float(buys[-1]["price"])
                            portfolio_add(state, pair, tot_qty, buy_price)
                except Exception:
                    pass

            pnl_block = []
            if buy_price > 0:
                cost = tot_qty * buy_price
                pnl = val_usdt - cost
                pnl_pct = (pnl / cost * 100.0) if cost > 0 else 0.0
                sign = "🟢" if pnl >= 0 else "🔴"
                pnl_block.append(f"   ├ 💵 <b>Вложено:</b> <code>{cost:,.2f} USDT</code> (вход: <code>{fmt_price(buy_price)} $</code>)")
                pnl_block.append(f"   ├ 📈 <b>PnL (прирост):</b> {sign} <b>{pnl:+.2f} USDT ({fmt_pct(pnl_pct)})</b>")
                total_tracked_cost += cost
                total_tracked_val += val_usdt

                if trade_rec and float(trade_rec.get("tp_price", 0.0)) > 0:
                    tp_p = float(trade_rec["tp_price"])
                    tp_id = trade_rec.get("tp_order_id")
                    tp_order_tag = f" [Ордер #{tp_id}]" if tp_id else ""
                    pnl_block.append(f"   ├ 🎯 <b>Тейк-профит:</b> <code>{fmt_price(tp_p)} $</code> (+{((tp_p - buy_price)/buy_price*100):.1f}%){tp_order_tag}")
            else:
                chg_24h = ticker_map.get(pair)
                if chg_24h is not None:
                    sign = "🟢" if chg_24h >= 0 else "🔴"
                    pnl_block.append(f"   ├ 📈 <b>Динамика 24ч:</b> {sign} <b>{fmt_pct(chg_24h)}</b>")

            pnl_str = ("\n" + "\n".join(pnl_block)) if pnl_block else ""

            items_lines.append(
                f"• <b>{asset}</b>: <code>{fmt_qty(tot_qty)} {asset}</code> × <code>{fmt_price(price)} $</code> = <b>{val_usdt:,.2f} USDT</b>{lock_str}"
                f"{pnl_str}\n"
                f"   └ 🔗 <a href=\"{tv_url}\">📈 TradingView</a> • <a href=\"{binance_url}\">📊 Binance Spot</a>"
            )
        else:
            items_lines.append(f"• <b>{asset}</b>: <code>{fmt_qty(tot_qty)} {asset}</code> <i>(цена недоступна)</i>")

    if items_lines:
        lines.append("📦 <b>Криптовалютные активы (расшифровка):</b>")
        lines.extend(items_lines)
        lines.append("─────────────────────")
    else:
        lines.append("📦 <i>Других монет на балансе нет (100% средств в USDT).</i>\n─────────────────────")

    # Итоговый блок капитала
    grand_total = usdt_total + total_crypto_value
    pct_usdt = (usdt_total / grand_total * 100.0) if grand_total > 0 else 100.0
    pct_crypto = (total_crypto_value / grand_total * 100.0) if grand_total > 0 else 0.0

    lines.append("💰 <b>ВСЕГО В ПОРТФЕЛЕ (ОБЩИЙ КАПИТАЛ):</b>")
    lines.append(f"• Стейблкоины (USDT): <code>{usdt_total:,.2f} USDT</code> ({pct_usdt:.1f}%)")
    if total_crypto_value > 0:
        lines.append(f"• В криптовалютных активах: <code>{total_crypto_value:,.2f} USDT</code> ({pct_crypto:.1f}%)")
    if total_tracked_cost > 0:
        tot_pnl = total_tracked_val - total_tracked_cost
        tot_pnl_pct = (tot_pnl / total_tracked_cost * 100.0)
        t_sign = "🟢" if tot_pnl >= 0 else "🔴"
        lines.append(f"• 📈 <b>Текущий P/L открытых позиций:</b> {t_sign} <b>{tot_pnl:+.2f} USDT ({fmt_pct(tot_pnl_pct)})</b>")
    lines.append(f"📊 <b>ИТОГО ВСЕГО:</b> <b>{grand_total:,.2f} USDT</b>")

    return "\n".join(lines)

_symbol_filters_cache: Dict[str, dict] = {}

def get_symbol_filters(symbol: str) -> dict:
    """Получает торговые фильтры для символа (LOT_SIZE stepSize, PRICE_FILTER tickSize, minNotional)."""
    global _symbol_filters_cache
    symbol = normalize_symbol(symbol)
    if symbol in _symbol_filters_cache:
        return _symbol_filters_cache[symbol]

    url = f"{BINANCE_TRADE_URL}/api/v3/exchangeInfo?symbol={symbol}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            symbols = data.get("symbols", [])
            if not symbols:
                return {}
            s_info = symbols[0]
            step_size = 1.0
            min_qty = 0.0
            tick_size = 0.01
            min_notional = 5.0

            for f in s_info.get("filters", []):
                ftype = f.get("filterType")
                if ftype == "LOT_SIZE":
                    step_size = float(f.get("stepSize", 1.0))
                    min_qty = float(f.get("minQty", 0.0))
                elif ftype == "PRICE_FILTER":
                    tick_size = float(f.get("tickSize", 0.01))
                elif ftype in ("NOTIONAL", "MIN_NOTIONAL"):
                    min_notional = float(f.get("minNotional", 5.0))

            filters = {
                "step_size": step_size,
                "min_qty": min_qty,
                "tick_size": tick_size,
                "min_notional": min_notional,
                "status": s_info.get("status", "TRADING"),
            }
            _symbol_filters_cache[symbol] = filters
            return filters
    except Exception as e:
        print(f"[get_symbol_filters error for {symbol}]: {e}", file=sys.stderr)
        return {"step_size": 0.0001, "min_qty": 0.0001, "tick_size": 0.0001, "min_notional": 5.0, "status": "TRADING"}

def round_step(val: float, step: float) -> float:
    """Округляет вниз с учетом stepSize биржи."""
    if step <= 0:
        return val
    step_str = f"{step:.10f}".rstrip("0")
    decimals = len(step_str.split(".")[1]) if "." in step_str else 0
    steps = math.floor(val / step)
    return round(steps * step, decimals)

def round_tick(val: float, tick: float) -> float:
    """Округляет цену с учетом tickSize биржи."""
    if tick <= 0:
        return val
    tick_str = f"{tick:.10f}".rstrip("0")
    decimals = len(tick_str.split(".")[1]) if "." in tick_str else 0
    ticks = round(val / tick)
    return round(ticks * tick, decimals)

def fmt_qty_filter(val: float, step: float) -> str:
    """Форматирует количество в строковый вид точно под LOT_SIZE фильтр."""
    rounded = round_step(val, step)
    step_str = f"{step:.10f}".rstrip("0")
    decimals = len(step_str.split(".")[1]) if "." in step_str else 0
    return f"{rounded:.{decimals}f}"

def fmt_price_filter(val: float, tick: float) -> str:
    """Форматирует цену в строковый вид точно под PRICE_FILTER фильтр."""
    rounded = round_tick(val, tick)
    tick_str = f"{tick:.10f}".rstrip("0")
    decimals = len(tick_str.split(".")[1]) if "." in tick_str else 0
    return f"{rounded:.{decimals}f}"

def execute_pump_auto_trade(
    token: str,
    chat_id: Union[str, int],
    state: dict,
    sig: PumpSignal,
    manual_amount: Optional[float] = None,
) -> Optional[dict]:
    """
    Выполняет покупку на Binance Spot по маркету на заданную сумму USDT
    и сразу выставляет лимитный Take-Profit ордер (+3%).
    """
    settings = state.get("settings", {})
    tp_pct = float(settings.get("take_profit_pct", DEFAULT_TAKE_PROFIT))
    max_trades = int(settings.get("max_open_trades", 3))

    active_trades = state.setdefault("active_trades", {})
    portfolio = state.get("portfolio", {})

    # Проверка на повторный вход: монета уже есть в активных сделках или портфеле
    if sig.symbol in active_trades:
        print(f"[AutoTrade] Пропуск {sig.symbol}: уже есть активная сделка")
        return {"error": f"По монете {sig.symbol} уже есть открытая позиция"}

    if sig.symbol in portfolio:
        print(f"[AutoTrade] Пропуск {sig.symbol}: монета уже есть в портфеле")
        send_telegram(
            token, chat_id,
            f"ℹ️ <b>Пропуск сигнала {sig.base}/USDT</b>\n\n"
            f"Монета уже есть в вашем портфеле (средняя цена входа: "
            f"<code>{fmt_price(portfolio[sig.symbol].get('avg_price', 0))} USDT</code>).\n"
            f"Повторная покупка пропущена для защиты от усреднения вниз.",
        )
        return {"error": f"Монета {sig.symbol} уже в портфеле"}

    # Проверка лимита открытых сделок
    if len(active_trades) >= max_trades:
        print(f"[AutoTrade] Достигнут лимит открытых сделок ({len(active_trades)}/{max_trades})")
        return {"error": f"Достигнут лимит активных сделок ({len(active_trades)}/{max_trades})"}

    # Проверка наличия API-ключей
    api_key, api_secret = get_api_credentials(state)
    if not api_key or not api_secret:
        msg = (
            "⚠️ <b>Автоторговля: API-ключи Binance не настроены!</b>\n\n"
            "Для автоматических сделок укажите ключи в <code>.env</code>:\n"
            "<code>BINANCE_API_KEY=...</code>\n"
            "<code>BINANCE_API_SECRET=...</code>\n"
            "или введите команду <code>/api КЛЮЧ СЕКРЕТ</code> в чате бота."
        )
        send_telegram(token, chat_id, msg)
        return {"error": "API keys not configured"}

    # Разрешение суммы ставки по режиму
    if manual_amount is not None:
        trade_amt = float(manual_amount)
    else:
        trade_mode = settings.get("trade_mode", "fixed")
        free_usdt_now = get_free_usdt_balance(state)
        if trade_mode == "all":
            trade_amt = max(0.0, free_usdt_now - 0.01)  # оставляем 0.01 USDT для комиссий
        elif trade_mode.startswith("pct:"):
            try:
                pct = float(trade_mode.split(":")[1]) / 100.0
            except (IndexError, ValueError):
                pct = 0.1
            trade_amt = round(free_usdt_now * pct, 2)
        else:
            trade_amt = float(settings.get("trade_amount_usdt", DEFAULT_TRADE_AMOUNT))

    # Проверка свободного баланса USDT
    free_usdt = get_free_usdt_balance(state)
    if free_usdt < max(trade_amt, 1.0):
        msg = (
            f"⚠️ <b>Автоторговля: Недостаточно USDT для входа в {sig.base}!</b>\n\n"
            f"• Требуется: <code>{trade_amt:.2f} USDT</code>\n"
            f"• Свободно на споте: <code>{free_usdt:.2f} USDT</code>\n\n"
            f"💡 Пополните баланс USDT на Binance или выберите меньший процент ставки в настройках."
        )
        send_telegram(token, chat_id, msg)
        return {"error": "Insufficient USDT balance"}

    # Получение фильтров торговой пары
    filters = get_symbol_filters(sig.symbol)
    if filters.get("status") != "TRADING":
        return {"error": f"Пара {sig.symbol} временно не торгуется на бирже"}

    min_notional = float(filters.get("min_notional", 5.0))
    if trade_amt < min_notional:
        msg = (
            f"⚠️ <b>Сумма ордера меньше минимума биржи!</b>\n\n"
            f"• Выбранная сумма: <code>{trade_amt:.2f} USDT</code>\n"
            f"• Минимальный ордер Binance для {sig.base}/USDT: <code>{min_notional:.1f} USDT</code>\n"
            f"• Свободно на споте: <code>{free_usdt:.2f} USDT</code>\n\n"
            f"💡 <i>Биржа Binance требует минимум {min_notional:.0f} USDT на один ордер. Пополните баланс спота хотя бы до {min_notional:.0f} USDT.</i>"
        )
        send_telegram(token, chat_id, msg)
        return {"error": f"Filter failure: NOTIONAL (amount {trade_amt:.2f} < min {min_notional:.1f} USDT)"}

    step_size = filters.get("step_size", 1.0)
    tick_size = filters.get("tick_size", 0.01)
    entry_pullback_pct = float(settings.get("entry_pullback_pct", DEFAULT_ENTRY_PULLBACK))

    # ── ВАРИАНТ А: УМНЫЙ ВХОД НА МИКРО-ОТКАТЕ (LIMIT BUY PULLBACK) ──
    if entry_pullback_pct > 0.01 and sig.price > 0:
        limit_buy_price_raw = sig.price * (1.0 - (entry_pullback_pct / 100.0))
        limit_buy_price_str = fmt_price_filter(limit_buy_price_raw, tick_size)
        raw_qty = trade_amt / float(limit_buy_price_str)
        qty_str = fmt_qty_filter(raw_qty, step_size)

        # Проверяем, что итоговая сумма не упала ниже min_notional из-за округления вниз
        total_limit_cost = float(qty_str) * float(limit_buy_price_str)
        if total_limit_cost < min_notional:
            # Округляем на один шаг вверх, если позволяет баланс
            raw_qty_ceil = math.ceil(min_notional / float(limit_buy_price_str) / step_size) * step_size
            qty_ceil_str = fmt_qty_filter(raw_qty_ceil, step_size)
            if float(qty_ceil_str) * float(limit_buy_price_str) <= free_usdt:
                qty_str = qty_ceil_str
            else:
                msg = (
                    f"⚠️ <b>Сумма лимитной покупки {sig.base}/USDT меньше минимума биржи!</b>\n\n"
                    f"• Рассчитано: <code>{total_limit_cost:.2f} USDT</code> ({qty_str} {sig.base} × {limit_buy_price_str} $)\n"
                    f"• Минимум биржи: <code>{min_notional:.1f} USDT</code>\n"
                    f"• Доступно на балансе: <code>{free_usdt:.2f} USDT</code>\n\n"
                    f"💡 <i>Пополните баланс хотя бы до {min_notional:.0f} USDT для покупки.</i>"
                )
                send_telegram(token, chat_id, msg)
                return {"error": f"Filter failure: NOTIONAL (limit cost {total_limit_cost:.2f} < min {min_notional:.1f} USDT)"}

        if float(qty_str) <= 0:
            return {"error": "Quantity calculation zero"}

        buy_params = {
            "symbol": sig.symbol,
            "side": "BUY",
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": qty_str,
            "price": limit_buy_price_str,
        }
        buy_res = binance_signed_request("POST", "/api/v3/order", buy_params, state=state)
        if "error" in buy_res:
            err_msg = buy_res.get("error", "Unknown error")
            print(f"[AutoTrade Error Limit Buy]: {err_msg}", file=sys.stderr)
            send_telegram(token, chat_id, f"❌ <b>Ошибка выставления лимитной покупки {sig.base}/USDT:</b>\n<code>{err_msg}</code>")
            return buy_res

        buy_order_id = buy_res.get("orderId")
        entry_record = {
            "symbol": sig.symbol,
            "base": sig.base,
            "order_id": buy_order_id,
            "target_price": float(limit_buy_price_str),
            "signal_price": sig.price,
            "pullback_pct": entry_pullback_pct,
            "qty": float(qty_str),
            "cost_usdt": float(qty_str) * float(limit_buy_price_str),
            "placed_at": int(time.time()),
            "timeout_sec": DEFAULT_ENTRY_TIMEOUT_SEC,
            "best_score": sig.best_score,
            "grade": sig.grade,
        }
        state.setdefault("pending_entries", {})[sig.symbol] = entry_record
        save_state(state, sync_git=True)

        entry_alert = (
            f"🎯 <b>ВЫСТАВЛЕН ЛИМИТНЫЙ ОРДЕР НА ОТКАТ (-{entry_pullback_pct:.1f}%)!</b>\n\n"
            f"⏳ Ждём микро-отката для покупки <b>{sig.base}/USDT</b> без переплаты на хаях:\n"
            f"• Цена сигнала: <code>{fmt_price(sig.price)} $</code>\n"
            f"• Лимитный вход: <code>{limit_buy_price_str} $</code> (дисконт 🟢 <b>-{entry_pullback_pct:.1f}%</b>)\n"
            f"• Сумма: <code>{float(qty_str)*float(limit_buy_price_str):.2f} USDT</code> ({qty_str} {sig.base})\n"
            f"• Ордер: <code>#{buy_order_id} (LIMIT BUY GTC)</code>\n"
            f"• Таймаут ожидания: <code>5 мин</code>\n\n"
            f"💡 <i>При исполнении бот сразу выставит Take-Profit ордер на продажу.</i>"
        )
        send_telegram(token, chat_id, entry_alert)
        return entry_record

    # ── ВАРИАНТ Б: ВХОД ПО РЫНКУ (MARKET BUY) ──
    # Защита от проскальзывания: если цена ушла от сигнальной больше чем на 1.5% —
    # вход отменяем, чтобы не купить на вершине импульса.
    live_price = get_price(sig.symbol)
    if live_price and sig.price > 0:
        slip_pct = (live_price - sig.price) / sig.price * 100.0
        if slip_pct > 1.5:
            msg = (
                f"⚠️ <b>Вход в {sig.base}/USDT отменён: цена улетела.</b>\n\n"
                f"• Цена сигнала: <code>{fmt_price(sig.price)} $</code>\n"
                f"• Цена сейчас: <code>{fmt_price(live_price)} $</code> (<b>+{slip_pct:.2f}%</b>)\n\n"
                f"💡 Покупка на хаях после такого скачка — риск мгновенного отката. Пропускаем."
            )
            send_telegram(token, chat_id, msg)
            return {"error": f"Slippage too high: +{slip_pct:.2f}%"}

    buy_params = {
        "symbol": sig.symbol,
        "side": "BUY",
        "type": "MARKET",
        "quoteOrderQty": f"{trade_amt:.2f}",
    }
    buy_res = binance_signed_request("POST", "/api/v3/order", buy_params, state=state)
    if "error" in buy_res:
        err_msg = buy_res.get("error", "Unknown error")
        print(f"[AutoTrade Error Buy]: {err_msg}", file=sys.stderr)
        send_telegram(
            token,
            chat_id,
            f"❌ <b>Ошибка покупки {sig.base}/USDT:</b>\n<code>{err_msg}</code>",
        )
        return buy_res

    buy_order_id = buy_res.get("orderId")
    cum_quote = float(buy_res.get("cummulativeQuoteQty", trade_amt))
    exec_qty = float(buy_res.get("executedQty", 0.0))

    if exec_qty <= 0:
        send_telegram(token, chat_id, f"❌ Ошибка исполнения покупки {sig.base}: 0 объем")
        return {"error": "Zero executed quantity"}

    avg_buy_price = cum_quote / exec_qty

    # 2. Умный расчет Take-Profit (фиксированный или динамический на основе ATR)
    dynamic_tp_enabled = settings.get("dynamic_tp", DEFAULT_DYNAMIC_TP)
    atr_calculated = False
    try:
        candles_atr = fetch_klines(sig.symbol, limit=25)
        if candles_atr and len(candles_atr) >= 15:
            atr_val = atr(candles_atr, 14)
            if atr_val and atr_val > 0 and avg_buy_price > 0 and dynamic_tp_enabled:
                atr_pct = (atr_val / avg_buy_price) * 100.0
                # Адаптивный TP: не менее выбранного, но учитывает волатильность (до 10%)
                dynamic_tp = max(tp_pct, min(10.0, round(atr_pct * 1.8, 1)))
                if dynamic_tp > tp_pct:
                    tp_pct = dynamic_tp
                    atr_calculated = True
    except Exception as _atr_err:
        print(f"[ATR TP error for {sig.symbol}]: {_atr_err}", file=sys.stderr)

    tp_raw_price = avg_buy_price * (1.0 + (tp_pct / 100.0))
    tp_price_str = fmt_price_filter(tp_raw_price, tick_size)
    tp_qty_str = fmt_qty_filter(exec_qty, step_size)

    # Параметры Stop-Loss и Trailing Stop
    sl_pct = float(settings.get("stop_loss_pct", DEFAULT_STOP_LOSS))
    sl_price = avg_buy_price * (1.0 - (sl_pct / 100.0))
    trailing_activation_pct = float(settings.get("trailing_activation_pct", DEFAULT_TRAILING_ACTIVATION))
    trailing_distance_pct = float(settings.get("trailing_distance_pct", DEFAULT_TRAILING_DISTANCE))

    # 3. Размещение LIMIT SELL GTC ордера (Тейк-профит)
    sell_params = {
        "symbol": sig.symbol,
        "side": "SELL",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": tp_qty_str,
        "price": tp_price_str,
    }
    sell_res = binance_signed_request("POST", "/api/v3/order", sell_params, state=state)

    # Если биржа вернула ошибку баланса из-за удержанной комиссии BNB/монеты — пробуем списать на 0.15% меньше
    if "error" in sell_res:
        err_str = str(sell_res.get("error", ""))
        print(f"[AutoTrade TP Retry for {sig.symbol}]: {err_str}", file=sys.stderr)
        reduced_qty = fmt_qty_filter(exec_qty * 0.9985, step_size)
        if float(reduced_qty) > 0 and reduced_qty != tp_qty_str:
            sell_params["quantity"] = reduced_qty
            sell_res = binance_signed_request("POST", "/api/v3/order", sell_params, state=state)

    tp_order_id = sell_res.get("orderId")
    tp_success = bool(tp_order_id)

    # 4. Сохранение сделки в активные и в портфель со всеми параметрами риск-менеджмента
    trade_record = {
        "symbol": sig.symbol,
        "base": sig.base,
        "buy_order_id": buy_order_id,
        "tp_order_id": tp_order_id,
        "buy_price": avg_buy_price,
        "highest_price": avg_buy_price,
        "tp_price": float(tp_price_str),
        "sl_price": sl_price,
        "trailing_sl": sl_price,
        "sl_pct": sl_pct,
        "trailing_activation_pct": trailing_activation_pct,
        "trailing_distance_pct": trailing_distance_pct,
        "qty": float(sell_params["quantity"]),
        "cost_usdt": cum_quote,
        "tp_pct": tp_pct,
        "opened_at": int(time.time()),
        "signal_score": sig.best_score,
        "signal_source": getattr(sig, "best_tf", "scan"),
        "status": "tp_placed" if tp_success else "unhedged_buy",
    }
    active_trades[sig.symbol] = trade_record
    portfolio_add(state, sig.symbol, float(sell_params["quantity"]), avg_buy_price)
    save_state(state, sync_git=True)

    # 5. Уведомление в Telegram
    expected_gain = (float(sell_params["quantity"]) * float(tp_price_str)) - cum_quote
    tp_status_note = (
        f"• Ордер тейк-профита: <code>#{tp_order_id} (LIMIT SELL GTC)</code>\n"
        f"• Ожидаемая прибыль: <b>+{expected_gain:.2f} USDT (+{tp_pct:.1f}%)</b>\n"
        f"<i>При закрытии заявки средства сразу вернутся в USDT.</i>"
        if tp_success
        else f"⚠️ <i>Не удалось выставить лимитник TP ({sell_res.get('error')}). Монета на спотовом балансе.</i>"
    )

    atr_label = " <i>(динамический ATR)</i>" if atr_calculated else ""
    trade_alert = (
        f"⚡ <b>СДЕЛКА ИСПОЛНЕНА НА BINANCE SPOT!</b>\n\n"
        f"🟢 <b>Покупка:</b> <b>{sig.base}/USDT</b>\n"
        f"• Потрачено: <code>{cum_quote:.2f} USDT</code>\n"
        f"• Куплено: <code>{fmt_qty(float(sell_params['quantity']))} {sig.base}</code>\n"
        f"• Цена исполнения: <code>{fmt_price(avg_buy_price)} USDT</code>\n"
        f"• Импульс Score: <b>{sig.best_score:.1f}</b> ({sig.grade.upper()})\n\n"
        f"🎯 <b>Тейк-профит (+{tp_pct:.1f}%){atr_label}:</b> <code>{tp_price_str} USDT</code>\n"
        f"🛑 <b>Stop-Loss (-{sl_pct:.1f}%):</b> <code>{fmt_price(sl_price)} USDT</code>\n"
        f"🛡 <b>Трейлинг-стоп:</b> автоподтяжка при <code>+{trailing_activation_pct:.1f}%</code> (дистанция <code>{trailing_distance_pct:.1f}%</code>)\n\n"
        f"{tp_status_note}"
    )
    send_telegram(token, chat_id, trade_alert)
    return trade_record

def check_pending_entries(token: str, chat_id: Union[str, int], state: dict) -> None:
    """
    Проверяет исполнение лимитных ордеров на вход в откат (Pullback Limit Buy).
    При исполнении выставляет Take-Profit и переводит в активные сделки.
    При истечении таймаута (5 мин) отменяет ордер.
    """
    pending = state.get("pending_entries", {})
    if not pending:
        return

    now = int(time.time())
    to_remove = []
    updated = False

    for symbol, entry in list(pending.items()):
        order_id = entry.get("order_id")
        if not order_id:
            to_remove.append(symbol)
            continue

        res = binance_signed_request("GET", "/api/v3/order", {"symbol": symbol, "orderId": order_id}, state=state)
        if "error" in res:
            continue

        status = res.get("status")
        if status == "FILLED":
            to_remove.append(symbol)
            updated = True

            cum_quote = float(res.get("cummulativeQuoteQty", entry.get("cost_usdt", 0.0)))
            exec_qty = float(res.get("executedQty", entry.get("qty", 0.0)))
            avg_buy_price = (cum_quote / exec_qty) if exec_qty > 0 else float(entry.get("target_price", 0.0))

            # Расчёт Take-Profit (с динамическим ATR)
            settings = state.get("settings", {})
            tp_pct = float(settings.get("take_profit_pct", DEFAULT_TAKE_PROFIT))
            dynamic_tp_enabled = settings.get("dynamic_tp", DEFAULT_DYNAMIC_TP)
            atr_calculated = False
            try:
                candles_atr = fetch_klines(symbol, limit=25)
                if candles_atr and len(candles_atr) >= 15:
                    atr_val = atr(candles_atr, 14)
                    if atr_val and atr_val > 0 and avg_buy_price > 0 and dynamic_tp_enabled:
                        atr_pct = (atr_val / avg_buy_price) * 100.0
                        dynamic_tp = max(tp_pct, min(10.0, round(atr_pct * 1.8, 1)))
                        if dynamic_tp > tp_pct:
                            tp_pct = dynamic_tp
                            atr_calculated = True
            except Exception:
                pass

            filters = get_symbol_filters(symbol)
            step_size = filters.get("step_size", 1.0)
            tick_size = filters.get("tick_size", 0.01)

            tp_raw_price = avg_buy_price * (1.0 + (tp_pct / 100.0))
            tp_price_str = fmt_price_filter(tp_raw_price, tick_size)
            tp_qty_str = fmt_qty_filter(exec_qty, step_size)

            sl_pct = float(settings.get("stop_loss_pct", DEFAULT_STOP_LOSS))
            sl_price = avg_buy_price * (1.0 - (sl_pct / 100.0))
            trailing_act = float(settings.get("trailing_activation_pct", DEFAULT_TRAILING_ACTIVATION))
            trailing_dist = float(settings.get("trailing_distance_pct", DEFAULT_TRAILING_DISTANCE))

            # Размещение LIMIT SELL GTC (Take-Profit)
            sell_params = {
                "symbol": symbol,
                "side": "SELL",
                "type": "LIMIT",
                "timeInForce": "GTC",
                "quantity": tp_qty_str,
                "price": tp_price_str,
            }
            sell_res = binance_signed_request("POST", "/api/v3/order", sell_params, state=state)
            if "error" in sell_res:
                reduced_qty = fmt_qty_filter(exec_qty * 0.9985, step_size)
                if float(reduced_qty) > 0 and reduced_qty != tp_qty_str:
                    sell_params["quantity"] = reduced_qty
                    sell_res = binance_signed_request("POST", "/api/v3/order", sell_params, state=state)

            tp_order_id = sell_res.get("orderId")
            tp_success = bool(tp_order_id)

            trade_rec = {
                "symbol": symbol,
                "base": entry.get("base", base_asset(symbol)),
                "buy_order_id": order_id,
                "tp_order_id": tp_order_id,
                "buy_price": avg_buy_price,
                "highest_price": avg_buy_price,
                "tp_price": float(tp_price_str),
                "sl_price": sl_price,
                "trailing_sl": sl_price,
                "sl_pct": sl_pct,
                "trailing_activation_pct": trailing_act,
                "trailing_distance_pct": trailing_dist,
                "qty": float(sell_params["quantity"]),
                "cost_usdt": cum_quote,
                "tp_pct": tp_pct,
                "opened_at": int(time.time()),
                "signal_score": entry.get("best_score", 70.0),
                "signal_source": "pullback_limit",
                "status": "tp_placed" if tp_success else "unhedged_buy",
            }
            state.setdefault("active_trades", {})[symbol] = trade_rec
            portfolio_add(state, symbol, float(sell_params["quantity"]), avg_buy_price)

            base = entry.get("base", base_asset(symbol))
            atr_lbl = " <i>(динамический ATR)</i>" if atr_calculated else ""
            tp_note = (
                f"• Ордер тейк-профита: <code>#{tp_order_id} (LIMIT SELL GTC)</code>\n"
                f"<i>Средства автоматически вернутся в USDT при достижении цели.</i>"
                if tp_success else f"⚠️ <i>Не удалось выставить лимитник TP. Монета на споте.</i>"
            )
            sig_p = float(entry.get("signal_price", avg_buy_price))
            pullback_saved = sig_p - avg_buy_price
            pullback_saved_pct = (pullback_saved / sig_p * 100.0) if sig_p > 0 else 0.0

            alert_msg = (
                f"🎯 <b>ВХОД НА ОТКАТЕ ИСПОЛНЕН!</b>\n\n"
                f"✅ <b>{base}/USDT</b> куплен по лучшей цене на Binance Spot!\n"
                f"• Цена сигнала: <code>{fmt_price(sig_p)} $</code>\n"
                f"• Цена исполнения: <code>{fmt_price(avg_buy_price)} $</code> (скидка 🟢 <b>-{pullback_saved_pct:.2f}%</b>)\n"
                f"• Потрачено: <code>{cum_quote:.2f} USDT</code> ({fmt_qty(exec_qty)} {base})\n\n"
                f"🎯 <b>Тейк-профит (+{tp_pct:.1f}%){atr_lbl}:</b> <code>{tp_price_str} $</code>\n"
                f"🛑 <b>Stop-Loss (-{sl_pct:.1f}%):</b> <code>{fmt_price(sl_price)} $</code>\n"
                f"🛡 <b>Трейлинг-стоп:</b> автоподтяжка при <code>+{trailing_act:.1f}%</code> (дистанция <code>{trailing_dist:.1f}%</code>)\n\n"
                f"{tp_note}"
            )
            if chat_id:
                send_telegram(token, chat_id, alert_msg)

        elif status in ("CANCELED", "REJECTED", "EXPIRED"):
            to_remove.append(symbol)
            updated = True

        elif now - entry.get("placed_at", now) >= entry.get("timeout_sec", DEFAULT_ENTRY_TIMEOUT_SEC):
            # Истёк таймаут ожидания отката
            to_remove.append(symbol)
            updated = True
            binance_signed_request("DELETE", "/api/v3/order", {"symbol": symbol, "orderId": order_id}, state=state)
            base = entry.get("base", base_asset(symbol))
            print(f"[Timeout Pullback Order {symbol}]: отменён по истечению таймаута")
            timeout_msg = (
                f"⏱️ <b>Лимитный ордер на откат {base}/USDT отменён.</b>\n"
                f"За 5 минут цена не скорректировалась к <code>{fmt_price(entry.get('target_price', 0))} $</code>. Позиция не открыта во избежание покупки на хаях."
            )
            if chat_id:
                send_telegram(token, chat_id, timeout_msg)

    for s in to_remove:
        pending.pop(s, None)

    if updated:
        save_state(state, sync_git=True)

def check_active_trades(token: str, chat_id: Union[str, int], state: dict) -> None:
    """
    Проверяет исполнение выставленных тейк-профит ордеров на Binance,
    а также отслеживает Stop-Loss и Trailing-Stop по текущей цене в реальном времени.
    """
    active_trades = state.get("active_trades", {})
    if not active_trades:
        return

    symbols_to_remove = []
    updated = False

    # Получаем актуальные цены всех активных монет
    symbols_list = list(active_trades.keys())
    current_prices = get_multiple_prices(symbols_list) if symbols_list else {}

    for symbol, trade in list(active_trades.items()):
        tp_order_id = trade.get("tp_order_id")

        # 1. Проверка исполнения Take-Profit на бирже
        if tp_order_id:
            res = binance_signed_request(
                "GET",
                "/api/v3/order",
                {"symbol": symbol, "orderId": tp_order_id},
                state=state,
            )
            if "error" not in res:
                status = res.get("status")
                if status == "FILLED":
                    symbols_to_remove.append(symbol)
                    updated = True

                    cum_quote = float(res.get("cummulativeQuoteQty", trade.get("qty", 0) * trade.get("tp_price", 0)))
                    cost = float(trade.get("cost_usdt", 0))
                    pnl = cum_quote - cost
                    pnl_pct = (pnl / cost * 100.0) if cost > 0 else trade.get("tp_pct", 2.0)

                    history = state.setdefault("trade_history", [])
                    closed_rec = dict(trade)
                    closed_rec["closed_at"] = int(time.time())
                    closed_rec["sell_price"] = (
                        cum_quote / float(res.get("executedQty", 1.0))
                        if float(res.get("executedQty", 0)) > 0
                        else trade.get("tp_price", 0)
                    )
                    closed_rec["pnl"] = pnl
                    closed_rec["pnl_pct"] = pnl_pct
                    closed_rec["ending_balance"] = get_free_usdt_balance(state)
                    closed_rec["status"] = "tp_filled"
                    history.append(closed_rec)
                    if len(history) > 100:
                        history.pop(0)

                    # Удаляем из отслеживания портфеля
                    portfolio_remove(state, symbol)

                    base = trade.get("base", base_asset(symbol))
                    win_msg = (
                        f"🎉 <b>ТЕЙК-ПРОФИТ СРАБОТАЛ (+{pnl_pct:.2f}%)!</b>\n\n"
                        f"✅ <b>{base}/USDT</b> успешно закрыт на Binance Spot!\n"
                        f"• Вход: <code>{fmt_price(trade.get('buy_price', 0))} USDT</code>\n"
                        f"• Выход: <code>{fmt_price(closed_rec['sell_price'])} USDT</code>\n"
                        f"• Получено: <b>{cum_quote:.2f} USDT</b>\n"
                        f"• Прибыль: 🟢 <b>+{pnl:+.2f} USDT ({fmt_pct(pnl_pct)})</b>\n\n"
                        f"💵 <i>Все средства возвращены в свободный USDT-баланс!</i>"
                    )
                    if chat_id:
                        send_telegram(token, chat_id, win_msg)
                    continue

                elif status == "CANCELED":
                    symbols_to_remove.append(symbol)
                    updated = True
                    base = trade.get("base", base_asset(symbol))
                    warn_msg = (
                        f"⚠️ <b>Тейк-профит ордер #{tp_order_id} по {base}/USDT был отменён на бирже.</b>\n"
                        f"Сделка снята с автоматического мониторинга бота."
                    )
                    if chat_id:
                        send_telegram(token, chat_id, warn_msg)
                    continue

        # 2. Мониторинг цены для Stop-Loss и Трейлинг-стопа
        cur_p = current_prices.get(symbol)
        if cur_p is None:
            cur_p = get_price(symbol)

        if cur_p and cur_p > 0:
            buy_p = float(trade.get("buy_price", cur_p))
            highest = float(trade.get("highest_price", buy_p))

            # Обновляем максимальную цену с момента входа
            if cur_p > highest:
                highest = cur_p
                trade["highest_price"] = highest
                updated = True

            # Расчёт и подтягивание Trailing Stop
            gain_from_buy = ((highest - buy_p) / buy_p * 100.0) if buy_p > 0 else 0.0
            act_pct = float(trade.get("trailing_activation_pct", DEFAULT_TRAILING_ACTIVATION))
            dist_pct = float(trade.get("trailing_distance_pct", DEFAULT_TRAILING_DISTANCE))

            if gain_from_buy >= act_pct:
                candidate_trail = highest * (1.0 - (dist_pct / 100.0))
                current_trail = float(trade.get("trailing_sl", 0.0))
                if candidate_trail > current_trail:
                    trade["trailing_sl"] = candidate_trail
                    updated = True

            # Проверка условий выхода по SL или Трейлингу
            sl_p = float(trade.get("sl_price", 0.0))
            trail_p = float(trade.get("trailing_sl", 0.0))
            effective_stop = max(sl_p, trail_p)

            if effective_stop > 0 and cur_p <= effective_stop:
                is_trailing = (trail_p > sl_p and cur_p <= trail_p and gain_from_buy >= act_pct)
                base = trade.get("base", base_asset(symbol))
                print(f"[StopTrigger for {symbol}]: cur={cur_p} <= stop={effective_stop} (trailing={is_trailing})")

                # Экстренное закрытие по рынку
                execute_emergency_market_sell(token, chat_id, state, symbol)
                symbols_to_remove.append(symbol)
                updated = True

                # Оповещение о стопе
                stop_type = "🛡 <b>СРАБОТАЛ ТРЕЙЛИНГ-СТОП (ПРИБЫЛЬ ЗАФИКСИРОВАНА)</b>" if is_trailing else "🛑 <b>СРАБОТАЛ STOP-LOSS (ЗАЩИТА ДЕПОЗИТА)</b>"
                stop_alert = (
                    f"{stop_type}\n\n"
                    f"Позиция <b>{base}/USDT</b> закрыта по рыночной цене:\n"
                    f"• Вход: <code>{fmt_price(buy_p)} $</code>\n"
                    f"• Выход: <code>{fmt_price(cur_p)} $</code>\n"
                    f"• Пик движения: <code>{fmt_price(highest)} $</code> (+{gain_from_buy:.1f}%)\n"
                    f"• Уровень стопа: <code>{fmt_price(effective_stop)} $</code>\n\n"
                    f"💵 <i>Ордер TP снят, средства возвращены в баланс USDT.</i>"
                )
                if chat_id:
                    send_telegram(token, chat_id, stop_alert)
                continue

    for sym in symbols_to_remove:
        active_trades.pop(sym, None)

    if updated:
        save_state(state, sync_git=True)

def execute_emergency_market_sell(
    token: str,
    chat_id: Union[str, int],
    state: dict,
    symbol: str,
) -> dict:
    """
    Отменяет открытый Take-Profit ордер (если есть) на бирже Binance
    и экстренно продает весь объем монеты по MARKET ордеру в USDT.
    """
    symbol = normalize_symbol(symbol)
    base = base_asset(symbol)

    active_trades = state.setdefault("active_trades", {})
    port = state.setdefault("portfolio", {})

    trade = active_trades.get(symbol)
    port_pos = port.get(symbol)

    if not trade and not port_pos:
        err = f"Актив {base} не найден в открытых сделках или портфеле."
        send_telegram(token, chat_id, f"❌ {err}")
        return {"error": err}

    # 1. Если есть открытый TP ордер на бирже — отменяем его для разблокировки монет
    tp_order_id = trade.get("tp_order_id") if trade else None
    if tp_order_id:
        cancel_res = binance_signed_request(
            "DELETE",
            "/api/v3/order",
            {"symbol": symbol, "orderId": tp_order_id},
            state=state,
        )
        print(f"[Cancel TP order #{tp_order_id} for {symbol}]: {cancel_res}")

    # 2. Получаем реальный спотовый баланс монеты
    assets, err = get_spot_account_assets(state)
    if err:
        send_telegram(token, chat_id, f"❌ <b>Ошибка запроса баланса:</b>\n<code>{err}</code>")
        return {"error": str(err)}

    asset_info = assets.get(base, {"free": 0.0, "locked": 0.0, "total": 0.0})
    qty_to_sell = float(asset_info["free"])
    if qty_to_sell <= 0.00000001:
        qty_to_sell = float(trade.get("qty", 0.0) if trade else port_pos.get("qty", 0.0))

    filters = get_symbol_filters(symbol)
    step_size = filters.get("step_size", 0.0001)
    min_qty = filters.get("min_qty", 0.0)
    qty_str = fmt_qty_filter(qty_to_sell, step_size)

    if float(qty_str) <= 0 or float(qty_str) < min_qty:
        err_msg = f"Недостаточно монет {base} на балансе для продажи (доступно: {qty_to_sell})."
        send_telegram(token, chat_id, f"❌ {err_msg}")
        return {"error": err_msg}

    # 3. Размещаем MARKET SELL ордер
    sell_params = {
        "symbol": symbol,
        "side": "SELL",
        "type": "MARKET",
        "quantity": qty_str,
    }
    sell_res = binance_signed_request("POST", "/api/v3/order", sell_params, state=state)
    if "error" in sell_res:
        # Если не проходит из-за удержанной комиссии BNB/монеты — пробуем списать на 0.2% меньше
        reduced = fmt_qty_filter(float(qty_str) * 0.998, step_size)
        if float(reduced) > 0 and reduced != qty_str:
            sell_params["quantity"] = reduced
            sell_res = binance_signed_request("POST", "/api/v3/order", sell_params, state=state)

    if "error" in sell_res:
        err_msg = sell_res.get("error", "Unknown error")
        send_telegram(token, chat_id, f"❌ <b>Ошибка продажи {base}/USDT:</b>\n<code>{err_msg}</code>")
        return {"error": str(err_msg)}

    # 4. Фиксация результата и расчет PnL
    cum_quote = float(sell_res.get("cummulativeQuoteQty", 0.0))
    exec_qty = float(sell_res.get("executedQty", float(qty_str)))
    sell_price = (cum_quote / exec_qty) if exec_qty > 0 else 0.0

    buy_price = float(trade.get("buy_price", port_pos.get("avg_price", 0.0) if port_pos else 0.0))
    cost = float(trade.get("cost_usdt", exec_qty * buy_price))
    pnl = (cum_quote - cost) if cost > 0 else 0.0
    pnl_pct = (pnl / cost * 100.0) if cost > 0 else 0.0

    # Запись в историю
    history = state.setdefault("trade_history", [])
    closed_rec = dict(trade or port_pos or {})
    closed_rec["symbol"] = symbol
    closed_rec["base"] = base
    closed_rec["closed_at"] = int(time.time())
    closed_rec["buy_price"] = buy_price
    closed_rec["sell_price"] = sell_price
    closed_rec["cost_usdt"] = cost
    closed_rec["pnl"] = pnl
    closed_rec["pnl_pct"] = pnl_pct
    closed_rec["ending_balance"] = get_free_usdt_balance(state)
    closed_rec["status"] = "manual_market_sold"
    history.append(closed_rec)
    if len(history) > 100:
        history.pop(0)

    # Удаляем из активных сделок и портфеля
    active_trades.pop(symbol, None)
    portfolio_remove(state, symbol)
    save_state(state, sync_git=True)

    sign = "🟢" if pnl >= 0 else "🔴"
    result_msg = (
        f"🚨 <b>ПОЗИЦИЯ ЗАКРЫТА ПО РЫНКУ!</b>\n\n"
        f"✅ <b>{base}/USDT</b> успешно продан на Binance Spot.\n"
        f"• Продано: <code>{fmt_qty(exec_qty)} {base}</code>\n"
        f"• Цена выхода: <code>{fmt_price(sell_price)} USDT</code>\n"
        f"• Получено: <b>{cum_quote:,.2f} USDT</b>\n"
        f"• Результат сделки: {sign} <b>{pnl:+.2f} USDT ({fmt_pct(pnl_pct)})</b>\n\n"
        f"💵 <i>Все средства возвращены в свободный баланс USDT.</i>"
    )
    send_telegram(token, chat_id, result_msg, reply_markup=main_keyboard())
    return {"success": True, "cum_quote": cum_quote, "pnl": pnl, "pnl_pct": pnl_pct}

def format_sell_confirmation_prompt(state: dict, symbol: str) -> Tuple[str, dict]:
    """Формирует текст предупреждения и кнопки подтверждения досрочной продажи актива."""
    symbol = normalize_symbol(symbol)
    base = base_asset(symbol)

    active_trades = state.get("active_trades", {})
    port = state.get("portfolio", {})
    trade = active_trades.get(symbol)
    pos = port.get(symbol)

    cur_price = get_price(symbol) or 0.0

    qty = float(trade.get("qty") if trade else pos.get("qty", 0.0) if pos else 0.0)
    buy_price = float(trade.get("buy_price") if trade else pos.get("avg_price", 0.0) if pos else 0.0)
    cost = float(trade.get("cost_usdt", qty * buy_price))
    tp_price = float(trade.get("tp_price", 0.0)) if trade else 0.0

    cur_val = qty * cur_price if cur_price > 0 else cost
    pnl = cur_val - cost
    pnl_pct = (pnl / cost * 100.0) if cost > 0 else 0.0
    sign = "🟢" if pnl >= 0 else "🔴"

    tp_note = f"\n• Тейк-профит цель: <code>{fmt_price(tp_price)} $</code> (+{trade.get('tp_pct', 3.0):.1f}%)" if trade else ""

    text = (
        f"⚠️ <b>ПОДТВЕРЖДЕНИЕ ПРОДАЖИ АКТИВА</b>\n\n"
        f"Вы собираетесь досрочно закрыть позицию <b>{base}/USDT</b> по рынку:\n\n"
        f"• 📦 <b>К продаже:</b> <code>{fmt_qty(qty)} {base}</code>\n"
        f"• 💵 <b>Потрачено на вход:</b> <code>{cost:,.2f} USDT</code> (вход <code>{fmt_price(buy_price)} $</code>)\n"
        f"• 📊 <b>Текущая цена рынка:</b> <code>{fmt_price(cur_price)} $</code>\n"
        f"• 💵 <b>Оценка к получению:</b> <code>{cur_val:,.2f} USDT</code>\n"
        f"• 📈 <b>Текущий результат (P/L):</b> {sign} <b>{pnl:+.2f} USDT ({fmt_pct(pnl_pct)})</b>{tp_note}\n\n"
        f"⚠️ <i>Если на бирже был выставлен Take-Profit ордер, бот автоматически отменит его и продаст монету в USDT прямо сейчас.</i>\n\n"
        f"<b>Подтверждаете продажу по рыночной цене?</b>"
    )

    kb = {
        "inline_keyboard": [
            [
                {"text": f"✅ Да, продать {base} по рынку", "callback_data": f"sell_confirm:{symbol}"},
            ],
            [
                {"text": "❌ Отмена (не продавать)", "callback_data": "port:refresh"},
            ]
        ]
    }
    return text, kb

def sell_assets_menu_kb(state: dict) -> Optional[dict]:
    """Клавиатура выбора актива для досрочной продажи. None — если API-ключи не заданы."""
    k, s = get_api_credentials(state)
    if not (k and s):
        return None  # продажа на бирже невозможна без API-ключей
    active_trades = state.get("active_trades", {})
    port = state.get("portfolio", {})
    all_symbols = sorted(set(list(active_trades.keys()) + list(port.keys())))

    if not all_symbols:
        return {
            "inline_keyboard": [
                [{"text": "🔙 Назад в портфель", "callback_data": "port:refresh"}]
            ]
        }

    prices = get_multiple_prices(all_symbols) if all_symbols else {}
    rows = []
    for s in all_symbols:
        base = base_asset(s)
        tr = active_trades.get(s)
        pos = port.get(s)
        qty = float(tr.get("qty") if tr else pos.get("qty", 0.0) if pos else 0.0)
        buy_p = float(tr.get("buy_price") if tr else pos.get("avg_price", 0.0) if pos else 0.0)
        cost = qty * buy_p
        cur_p = prices.get(s)
        if cur_p is not None and cost > 0:
            val = qty * cur_p
            pnl_pct = (val - cost) / cost * 100.0
            pnl_str = f" ({pnl_pct:+.1f}%)"
        else:
            pnl_str = ""
        rows.append([
            {"text": f"🔴 Продать {base}{pnl_str}", "callback_data": f"sell_prompt:{s}"},
            {"text": f"📈 {base} TV", "url": f"https://www.tradingview.com/chart/?symbol=BINANCE:{s}"},
        ])

    rows.append([{"text": "🔙 Назад в портфель", "callback_data": "port:refresh"}])
    return {"inline_keyboard": rows}

def target_sell_assets_menu_kb(state: dict) -> dict:
    """Клавиатура выбора актива для настройки автопродажи по целевой цене."""
    active_trades = state.get("active_trades", {})
    port = state.get("portfolio", {})
    all_symbols = sorted(set(list(active_trades.keys()) + list(port.keys())))

    if not all_symbols:
        return {
            "inline_keyboard": [
                [{"text": "🔙 Назад в портфель", "callback_data": "port:refresh"}]
            ]
        }

    prices = get_multiple_prices(all_symbols) if all_symbols else {}
    rows = []
    for s in all_symbols:
        base = base_asset(s)
        cur_p = prices.get(s)
        p_str = f" ({fmt_price(cur_p)} $)" if cur_p else ""
        rows.append([
            {"text": f"🎯 Задать TP для {base}{p_str}", "callback_data": f"target_pick:{s}"}
        ])

    rows.append([{"text": "🔙 Назад в портфель", "callback_data": "port:refresh"}])
    return {"inline_keyboard": rows}

def target_sell_coin_presets_kb(symbol: str, cur_price: float, state: dict) -> Tuple[str, dict]:
    """Формирует меню пресетов целевой цены для монеты."""
    base = base_asset(symbol)
    active_trades = state.get("active_trades", {})
    port = state.get("portfolio", {})
    trade = active_trades.get(symbol)
    pos = port.get(symbol)
    qty = float(trade.get("qty") if trade else pos.get("qty", 0.0) if pos else 0.0)
    buy_p = float(trade.get("buy_price") if trade else pos.get("avg_price", cur_price) if pos else cur_price)
    
    tp_price = float(trade.get("tp_price", 0.0)) if trade else 0.0
    cur_tp_str = f"\n• Текущий TP-ордер: <code>{fmt_price(tp_price)} $</code>" if tp_price > 0 else ""

    ref_price = max(cur_price, buy_p) if buy_p > 0 else cur_price

    text = (
        f"🎯 <b>НАСТРОЙКА АВТОПРОДАЖИ (Take-Profit): {base}/USDT</b>\n\n"
        f"• Количество: <code>{fmt_qty(qty)} {base}</code>\n"
        f"• Цена входа (покупка): <code>{fmt_price(buy_p)} $</code>\n"
        f"• Текущий рынок: <code>{fmt_price(cur_price)} $</code>{cur_tp_str}\n\n"
        f"<i>🛡 Защита: TP рассчитывается только в плюс от цены покупки и выше рынка.</i>\n"
        f"<i>Выберите желаемый процент прибыли или введите точную цену:</i>"
    )

    kb = {
        "inline_keyboard": [
            [
                {"text": f"🎯 +1.5% ({fmt_price(ref_price * 1.015)} $)", "callback_data": f"target_preset:{symbol}:1.5"},
                {"text": f"🎯 +2.5% ({fmt_price(ref_price * 1.025)} $)", "callback_data": f"target_preset:{symbol}:2.5"},
            ],
            [
                {"text": f"🎯 +3.5% ({fmt_price(ref_price * 1.035)} $)", "callback_data": f"target_preset:{symbol}:3.5"},
                {"text": f"🎯 +5.0% ({fmt_price(ref_price * 1.050)} $)", "callback_data": f"target_preset:{symbol}:5.0"},
            ],
            [
                {"text": f"🎯 +10.0% ({fmt_price(ref_price * 1.100)} $)", "callback_data": f"target_preset:{symbol}:10.0"},
                {"text": f"🎯 +15.0% ({fmt_price(ref_price * 1.150)} $)", "callback_data": f"target_preset:{symbol}:15.0"},
            ],
            [
                {"text": "✍️ Ввести свою цену вручную", "callback_data": f"target_custom:{symbol}"},
            ],
            [
                {"text": "🔙 Назад к списку монет", "callback_data": "port:targetsell_menu"},
            ]
        ]
    }
    return text, kb

def set_custom_target_sell(token: str, chat_id: Union[str, int], state: dict, symbol: str, target_val: Union[str, float]) -> dict:
    """
    Устанавливает лимитный ордер на продажу (Take-Profit) по заданной цене или проценту на Binance Spot.
    Включает строгую защиту от продажи в минус (ниже цены покупки).
    """
    symbol = normalize_symbol(symbol)
    base = base_asset(symbol)

    cur_price = get_price(symbol)
    if not cur_price or cur_price <= 0:
        msg = f"❌ Не удалось получить текущую цену для <b>{base}/USDT</b>."
        send_telegram(token, chat_id, msg)
        return {"error": "Price fetch failed"}

    active_trades = state.setdefault("active_trades", {})
    port = state.setdefault("portfolio", {})
    trade = active_trades.get(symbol)
    pos = port.get(symbol)

    # 1. Снимаем старый TP ордер, если есть
    old_tp_order_id = trade.get("tp_order_id") if trade else None
    if old_tp_order_id:
        binance_signed_request("DELETE", "/api/v3/order", {"symbol": symbol, "orderId": old_tp_order_id}, state=state)

    # 2. Получаем реальный объем для продажи
    assets, err = get_spot_account_assets(state)
    if err:
        send_telegram(token, chat_id, f"❌ <b>Ошибка Binance API:</b>\n<code>{err}</code>")
        return {"error": str(err)}

    asset_info = assets.get(base, {"free": 0.0, "locked": 0.0, "total": 0.0})
    qty_available = float(asset_info["free"])
    if qty_available <= 0.00000001:
        qty_available = float(asset_info["total"])
    if qty_available <= 0.00000001:
        qty_available = float(trade.get("qty", 0.0) if trade else pos.get("qty", 0.0) if pos else 0.0)

    filters = get_symbol_filters(symbol)
    step_size = filters.get("step_size", 0.0001)
    tick_size = filters.get("tick_size", 0.0001)
    min_qty = filters.get("min_qty", 0.0)
    qty_str = fmt_qty_filter(qty_available, step_size)

    if float(qty_str) <= 0 or float(qty_str) < min_qty:
        err_msg = f"Недостаточно монет {base} на балансе (доступно: {qty_available})."
        send_telegram(token, chat_id, f"❌ {err_msg}")
        return {"error": err_msg}

    # 3. Вычисляем целевую цену
    buy_p = float(trade.get("buy_price", 0.0) if trade else pos.get("avg_price", 0.0) if pos else 0.0)
    if buy_p <= 0:
        # Если в state нет цены покупки — запрашиваем последнюю сделку из Binance API
        try:
            my_tr = binance_signed_request("GET", "/api/v3/myTrades", {"symbol": symbol, "limit": 5}, state=state)
            if isinstance(my_tr, list) and my_tr:
                buys = [t for t in my_tr if t.get("isBuyer")]
                if buys:
                    buy_p = float(buys[-1]["price"])
        except Exception:
            pass

    target_price = 0.0
    if isinstance(target_val, str):
        val_clean = target_val.strip().replace(",", ".")
        if val_clean.endswith("%") or val_clean.startswith("+"):
            pct_num = float(val_clean.lstrip("+").rstrip("%"))
            base_p = max(cur_price, buy_p) if buy_p > 0 else cur_price
            target_price = base_p * (1.0 + (pct_num / 100.0))
        else:
            target_price = float(val_clean)
    else:
        target_price = float(target_val)

    if target_price <= 0:
        send_telegram(token, chat_id, "❌ Некорректная цена автопродажи.")
        return {"error": "Invalid target price"}

    # 🛡 ЗАЩИТА: Запрет установки Take-Profit ниже цены покупки (защита от продажи в минус)
    if buy_p > 0 and target_price <= buy_p:
        send_telegram(
            token, chat_id,
            f"🛡 <b>ЗАЩИТА ОТ УБЫТКА АКТИВИРОВАНА:</b>\n\n"
            f"• Монета: <b>{base}/USDT</b>\n"
            f"• Ваша цена покупки (вход): <code>{fmt_price(buy_p)} $</code>\n"
            f"• Введённая цель TP: <code>{fmt_price(target_price)} $</code>\n\n"
            f"❌ <b>Запрещено выставлять Take-Profit ниже или по цене покупки</b> (это зафиксирует убыток).\n\n"
            f"💡 <i>Укажите цену выше <code>{fmt_price(buy_p)} $</code> или выберите процент прироста (например: <code>+2.5%</code> или <code>+5%</code>).</i>"
        )
        return {"error": "Target below buy price"}

    # 🛡 ЗАЩИТА: Запрет установки Take-Profit ниже текущей цены рынка
    if target_price <= cur_price:
        send_telegram(
            token, chat_id,
            f"⚠️ <b>ВНИМАНИЕ: Целевая цена ниже рынка!</b>\n\n"
            f"• Текущая рыночная цена: <code>{fmt_price(cur_price)} $</code>\n"
            f"• Введённая цель: <code>{fmt_price(target_price)} $</code>\n\n"
            f"Лимитный ордер на продажу (TP) можно выставлять только <b>выше текущей цены рынка</b>.\n"
            f"💡 Для мгновенной продажи по текущей цене используйте кнопку <b>«🔴 Продать актив»</b>."
        )
        return {"error": "Target below market"}

    target_price_str = fmt_price_filter(target_price, tick_size)

    # 4. Размещаем LIMIT SELL GTC ордер на Binance Spot
    sell_params = {
        "symbol": symbol,
        "side": "SELL",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": qty_str,
        "price": target_price_str,
    }
    sell_res = binance_signed_request("POST", "/api/v3/order", sell_params, state=state)
    if "error" in sell_res:
        reduced_qty = fmt_qty_filter(float(qty_str) * 0.9985, step_size)
        if float(reduced_qty) > 0 and reduced_qty != qty_str:
            sell_params["quantity"] = reduced_qty
            sell_res = binance_signed_request("POST", "/api/v3/order", sell_params, state=state)

    if "error" in sell_res:
        err_msg = sell_res.get("error", "Unknown error")
        send_telegram(token, chat_id, f"❌ <b>Ошибка биржи при выставлении ордера:</b>\n<code>{err_msg}</code>")
        return sell_res

    tp_order_id = sell_res.get("orderId")
    buy_p = float(trade.get("buy_price", cur_price) if trade else pos.get("avg_price", cur_price) if pos else cur_price)
    gain_pct = ((float(target_price_str) - buy_p) / buy_p * 100.0) if buy_p > 0 else 0.0
    exp_quote = float(sell_params["quantity"]) * float(target_price_str)

    trade_rec = {
        "symbol": symbol,
        "base": base,
        "buy_order_id": trade.get("buy_order_id") if trade else None,
        "tp_order_id": tp_order_id,
        "buy_price": buy_p,
        "highest_price": max(cur_price, buy_p),
        "tp_price": float(target_price_str),
        "sl_price": trade.get("sl_price", buy_p * 0.98) if trade else buy_p * 0.98,
        "trailing_sl": trade.get("trailing_sl", buy_p * 0.98) if trade else buy_p * 0.98,
        "sl_pct": trade.get("sl_pct", 2.0) if trade else 2.0,
        "trailing_activation_pct": 1.0,
        "trailing_distance_pct": 0.8,
        "qty": float(sell_params["quantity"]),
        "cost_usdt": float(sell_params["quantity"]) * buy_p,
        "tp_pct": gain_pct,
        "opened_at": trade.get("opened_at", int(time.time())) if trade else int(time.time()),
        "signal_score": 80.0,
        "signal_source": "custom_target",
        "status": "tp_placed",
    }
    active_trades[symbol] = trade_rec
    if symbol not in port:
        portfolio_add(state, symbol, float(sell_params["quantity"]), buy_p)
    save_state(state, sync_git=True)

    success_msg = (
        f"🎯 <b>АВТОПРОДАЖА УСПЕШНО ВЫСТАВЛЕНА НА BINANCE SPOT!</b>\n\n"
        f"✅ Ордер <b>LIMIT SELL GTC</b> размещён в стакане биржи:\n"
        f"• Пара: <b>{base}/USDT</b>\n"
        f"• Текущая цена: <code>{fmt_price(cur_price)} $</code>\n"
        f"• 🎯 <b>Цель автопродажи:</b> <code>{target_price_str} $</code> (<b>{gain_pct:+.2f}%</b> от входа)\n"
        f"• Количество к продаже: <code>{fmt_qty(float(sell_params['quantity']))} {base}</code>\n"
        f"• Ожидаемая сумма к получению: <b>{exp_quote:,.2f} USDT</b>\n"
        f"• Номер ордера: <code>#{tp_order_id}</code>\n\n"
        f"💵 <i>Как только цена коснётся {target_price_str} $, ордер моментально исполнится на бирже и средства вернутся в USDT.</i>"
    )
    send_telegram(token, chat_id, success_msg, reply_markup=main_keyboard())
    return {"success": True, "order_id": tp_order_id, "target_price": target_price_str}

def sync_trades_and_active_positions(state: dict) -> None:
    """
    Автоматически синхронизирует открытые позиции и историю сделок с Binance Spot:
    1. Обнаруживает любые купленные активы на спотовом балансе (TRX, SOL, BTC и т.д.).
    2. Привязывает открытые лимитные ордера Take-Profit (/api/v3/openOrders).
    3. Подтягивает историю реальных сделок (входы, выходы, PnL) через /api/v3/myTrades.
    """
    api_key, api_secret = get_api_credentials(state)
    if not api_key or not api_secret:
        return

    try:
        active_trades = state.setdefault("active_trades", {})
        portfolio = state.setdefault("portfolio", {})
        history = state.setdefault("trade_history", [])
        settings = state.get("settings", {})
        tp_pct_default = float(settings.get("take_profit_pct", DEFAULT_TAKE_PROFIT))

        # 1. Получаем все открытые ордера на Binance Spot
        open_orders_res = binance_signed_request("GET", "/api/v3/openOrders", state=state)
        open_orders = open_orders_res if isinstance(open_orders_res, list) else []

        # 2. Получаем балансы спота
        assets, err = get_spot_account_assets(state)
        if err or not assets:
            return

        free_usdt = float(assets.get("USDT", {}).get("free", 0.0))

        # Фильтруем активы (исключая стейблкоины и микро-пыль)
        valid_assets = {}
        for a, info in assets.items():
            if a in STABLE_OR_FIAT or a == "USDT":
                continue
            if float(info.get("total", 0.0)) > 0.00001:
                valid_assets[a] = info

        # Собираем актуальные цены
        syms_to_check = [f"{a}USDT" for a in valid_assets.keys()]
        for s in list(portfolio.keys()) + list(active_trades.keys()):
            if s not in syms_to_check:
                syms_to_check.append(s)

        prices = get_multiple_prices(syms_to_check) if syms_to_check else {}

        # 3. Синхронизируем открытые позиции
        for asset, info in valid_assets.items():
            sym = f"{asset}USDT"
            cur_p = prices.get(sym, 0.0)
            tot_qty = float(info.get("total", 0.0))
            if cur_p > 0 and (tot_qty * cur_p) < 0.30:
                continue

            # Ищем открытый TP ордер на продажу
            sym_tp_order = next((o for o in open_orders if o.get("symbol") == sym and o.get("side") == "SELL"), None)
            tp_order_id = sym_tp_order.get("orderId") if sym_tp_order else None
            tp_price = float(sym_tp_order.get("price", 0.0)) if sym_tp_order else 0.0

            # Определяем цену входа и время
            trade_rec = active_trades.get(sym)
            port_rec = portfolio.get(sym)

            buy_price = float(trade_rec.get("buy_price", 0.0)) if trade_rec else float(port_rec.get("avg_price", 0.0)) if port_rec else 0.0
            opened_at = int(trade_rec.get("opened_at", 0)) if trade_rec else int(port_rec.get("added_at", 0)) if port_rec else 0

            # Если цена входа неизвестна — запрашиваем последнюю покупку из Binance myTrades
            if buy_price <= 0:
                my_trades = binance_signed_request("GET", "/api/v3/myTrades", {"symbol": sym, "limit": 10}, state=state)
                if isinstance(my_trades, list) and my_trades:
                    buys = [t for t in my_trades if t.get("isBuyer")]
                    if buys:
                        last_b = buys[-1]
                        buy_price = float(last_b.get("price", cur_p))
                        opened_at = int(last_b.get("time", 0)) // 1000

            if buy_price <= 0:
                buy_price = cur_p

            if tp_price <= 0 and buy_price > 0:
                tp_price = buy_price * (1.0 + tp_pct_default / 100.0)

            tp_pct_actual = ((tp_price - buy_price) / buy_price * 100.0) if buy_price > 0 else tp_pct_default

            active_trades[sym] = {
                "symbol": sym,
                "base": asset,
                "buy_order_id": trade_rec.get("buy_order_id") if trade_rec else None,
                "tp_order_id": tp_order_id,
                "buy_price": buy_price,
                "highest_price": max(cur_p, buy_price),
                "tp_price": tp_price,
                "sl_price": trade_rec.get("sl_price", buy_price * 0.98) if trade_rec else buy_price * 0.98,
                "trailing_sl": trade_rec.get("trailing_sl", buy_price * 0.98) if trade_rec else buy_price * 0.98,
                "sl_pct": trade_rec.get("sl_pct", 2.0) if trade_rec else 2.0,
                "qty": tot_qty,
                "cost_usdt": tot_qty * buy_price,
                "tp_pct": tp_pct_actual,
                "opened_at": opened_at or int(time.time()),
                "status": "tp_placed" if tp_order_id else "holding",
            }
            if sym not in portfolio:
                portfolio[sym] = {
                    "symbol": sym,
                    "base": asset,
                    "qty": tot_qty,
                    "avg_price": buy_price,
                    "added_at": opened_at or int(time.time()),
                }

        # Очищаем active_trades для монет, которых больше нет на споте
        for sym in list(active_trades.keys()):
            base = active_trades[sym].get("base", base_asset(sym))
            if base not in valid_assets:
                active_trades.pop(sym, None)

        # 4. Синхронизируем историю закрытых сделок через Binance myTrades
        all_traded_syms = list(set(list(portfolio.keys()) + [f"{a}USDT" for a in valid_assets.keys()] + ["TRXUSDT", "BTCUSDT", "SOLUSDT"]))
        for sym in all_traded_syms:
            try:
                my_trades = binance_signed_request("GET", "/api/v3/myTrades", {"symbol": sym, "limit": 15}, state=state)
                if not isinstance(my_trades, list):
                    continue

                existing_trade_ids = {h.get("trade_id") for h in history if h.get("trade_id")}
                sells = [t for t in my_trades if not t.get("isBuyer")]
                buys = [t for t in my_trades if t.get("isBuyer")]

                for s_trade in sells:
                    t_id = s_trade.get("id")
                    if t_id in existing_trade_ids:
                        continue

                    s_time = int(s_trade.get("time", 0)) // 1000
                    s_price = float(s_trade.get("price", 0.0))
                    s_qty = float(s_trade.get("qty", 0.0))
                    s_quote = float(s_trade.get("quoteQty", s_price * s_qty))

                    # Ищем предшествующую покупку
                    prev_buys = [b for b in buys if b.get("time", 0) < s_trade.get("time", 0)]
                    if prev_buys:
                        b_trade = prev_buys[-1]
                        b_price = float(b_trade.get("price", s_price * 0.98))
                        b_time = int(b_trade.get("time", 0)) // 1000
                    else:
                        b_price = s_price * 0.98
                        b_time = s_time - 3600

                    cost = s_qty * b_price
                    pnl = s_quote - cost
                    pnl_pct = (pnl / cost * 100.0) if cost > 0 else 0.0

                    history.append({
                        "symbol": sym,
                        "base": base_asset(sym),
                        "trade_id": t_id,
                        "opened_at": b_time,
                        "closed_at": s_time,
                        "buy_price": b_price,
                        "sell_price": s_price,
                        "qty": s_qty,
                        "cost_usdt": cost,
                        "pnl": pnl,
                        "pnl_pct": pnl_pct,
                        "ending_balance": free_usdt,
                        "status": "tp_filled" if pnl >= 0 else "market_sell",
                    })
            except Exception:
                pass

        # Сортируем историю по времени закрытия
        history.sort(key=lambda x: x.get("closed_at", 0))
        if len(history) > 100:
            del history[:-100]

        save_state(state)
    except Exception as e:
        print(f"⚠️ Ошибка синхронизации сделок с Binance: {e}", file=sys.stderr)

def format_portfolio(state: dict) -> str:
    sync_trades_and_active_positions(state)
    port = state.get("portfolio", {})
    active_trades = state.get("active_trades", {})
    history = state.get("trade_history", [])

    api_key, api_secret = get_api_credentials(state)
    has_api = bool(api_key and api_secret)
    free_usdt_str = ""
    free_usdt = 0.0
    if has_api:
        free_usdt = get_free_usdt_balance(state)
        free_usdt_str = f"💳 <b>Свободно на Binance:</b> <code>{free_usdt:,.2f} USDT</code>\n\n"

    lines = ["<b>💼 Ваш криптовалютный портфель и автосделки</b>\n"]
    if free_usdt_str:
        lines.append(free_usdt_str)

    if not port and not active_trades and not history:
        return (
            "<b>💼 Ваш портфель</b>\n\n"
            + (free_usdt_str or "")
            + "<i>Портфель пока пуст. Нет открытых сделок.</i>\n\n"
            "💡 Включите автоторговлю в настройках ⚙️ или нажмите <b>«➕ Добавить актив»</b>."
        )

    # Получаем актуальные рыночные цены всех активов
    all_symbols = sorted(set(list(port.keys()) + list(active_trades.keys())))
    prices = get_multiple_prices(all_symbols) if all_symbols else {}

    # 1. ⚡ АКТИВНЫЕ АВТОСДЕЛКИ (детально по числам)
    total_trades_cost = 0.0
    total_trades_val = 0.0
    total_expected_gain = 0.0

    if active_trades:
        lines.append("⚡ <b>ОТКРЫТЫЕ АВТОСДЕЛКИ (с авто-TP):</b>")
        for sym, tr in active_trades.items():
            base = tr.get("base", base_asset(sym))
            bp = float(tr.get("buy_price", 0.0))
            tp = float(tr.get("tp_price", 0.0))
            qty = float(tr.get("qty", 0.0))
            cost = float(tr.get("cost_usdt", qty * bp))
            tp_pct = float(tr.get("tp_pct", 3.0))
            tp_order_id = tr.get("tp_order_id", "—")
            src = tr.get("signal_source", "scan")
            score = tr.get("signal_score", 0)

            cur_price = prices.get(sym)
            if cur_price is not None:
                cur_val = qty * cur_price
                trade_pnl = cur_val - cost
                trade_pnl_pct = (trade_pnl / cost * 100.0) if cost > 0 else 0.0
                total_trades_val += cur_val
            else:
                cur_val = cost
                trade_pnl = 0.0
                trade_pnl_pct = 0.0
                total_trades_val += cost

            total_trades_cost += cost
            expected_gain = (qty * tp) - cost
            total_expected_gain += expected_gain
            sign = "🟢" if trade_pnl >= 0 else "🔴"

            tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{sym}"
            binance_url = f"https://www.binance.com/en/trade/{base}_USDT?type=spot"

            lines.append(
                f"{sign} <b>{base}/USDT</b> (Score: {score:.0f} | TF: {src})\n"
                f"   ├ 📦 <b>Куплено:</b> <code>{fmt_qty(qty)} {base}</code> (по <code>{fmt_price(bp)} $</code>)\n"
                f"   ├ 💵 <b>Потрачено:</b> <code>{cost:,.2f} USDT</code>\n"
                f"   ├ 📊 <b>Рынок:</b> <code>{fmt_price(cur_price) if cur_price else '—'} $</code> (оценка: <code>{cur_val:,.2f} $</code>)\n"
                f"   ├ 📈 <b>Текущий P/L:</b> <b>{trade_pnl:+.2f} USDT ({fmt_pct(trade_pnl_pct)})</b>\n"
                f"   ├ 🎯 <b>Тейк-профит:</b> <code>{fmt_price(tp)} $</code> (+{tp_pct:.1f}%)\n"
                f"   ├ 💰 <b>Заработок при TP:</b> <b>+{expected_gain:.2f} USDT</b> (Ордер #{tp_order_id})\n"
                f"   └ 🔗 <a href=\"{tv_url}\">📈 TradingView</a> • <a href=\"{binance_url}\">📊 Binance Spot</a>\n"
            )

        total_trades_pnl = total_trades_val - total_trades_cost
        total_trades_pct = (total_trades_pnl / total_trades_cost * 100.0) if total_trades_cost > 0 else 0.0
        trades_tot_sign = "🟢" if total_trades_pnl >= 0 else "🔴"

        lines.append("─────────────────────")
        lines.append("📊 <b>ВСЕГО ПО ОТКРЫТЫМ СДЕЛКАМ:</b>")
        lines.append(f"• <b>Открыто позиций:</b> <code>{len(active_trades)} шт</code>")
        lines.append(f"• <b>Всего потрачено:</b> <code>{total_trades_cost:,.2f} USDT</code>")
        lines.append(f"• <b>Текущая стоимость:</b> <code>{total_trades_val:,.2f} USDT</code>")
        lines.append(f"• <b>Текущий плавающий P/L:</b> {trades_tot_sign} <b>{total_trades_pnl:+.2f} USDT ({fmt_pct(total_trades_pct)})</b>")
        lines.append(f"• <b>Ожидаемый профит при закрытии TP:</b> 🟢 <b>+{total_expected_gain:.2f} USDT</b>")
        lines.append("─────────────────────")

    # 2. 📂 ДРУГИЕ АКТИВЫ ПОРТФЕЛЯ (не привязанные к активным авто-ордерам)
    manual_symbols = [s for s in sorted(port.keys()) if s not in active_trades]
    if manual_symbols:
        lines.append("📂 <b>ДРУГИЕ АКТИВЫ В ПОРТФЕЛЕ:</b>")
        for symbol in manual_symbols:
            pos = port[symbol]
            qty = float(pos["qty"])
            avg = float(pos["avg_price"])
            cost = qty * avg
            price = prices.get(symbol)
            base = base_asset(symbol)
            tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{symbol}"
            binance_url = f"https://www.binance.com/en/trade/{base}_USDT?type=spot"

            if price is None:
                lines.append(
                    f"⚪ <b>{base}/USDT</b>: {fmt_qty(qty)} {base}\n"
                    f"   ├ Вход: <code>{fmt_price(avg)} $</code> (вложено: {cost:,.2f} $)\n"
                    f"   └ 🔗 <a href=\"{tv_url}\">📈 TradingView</a> • <a href=\"{binance_url}\">📊 Binance Spot</a>\n"
                )
                continue

            val = qty * price
            pnl = val - cost
            pnl_pct = (pnl / cost * 100.0) if cost > 0 else 0.0
            sign = "🟢" if pnl >= 0 else "🔴"

            lines.append(
                f"{sign} <b>{base}/USDT</b>: {fmt_qty(qty)} {base}\n"
                f"   ├ Вход: <code>{fmt_price(avg)} $</code> → Рынок: <code>{fmt_price(price)} $</code>\n"
                f"   ├ Баланс: <code>{val:,.2f} $</code> (вложено: {cost:,.2f} $)\n"
                f"   ├ <b>P/L:</b> <b>{pnl:+.2f} USDT ({fmt_pct(pnl_pct)})</b>\n"
                f"   └ 🔗 <a href=\"{tv_url}\">📈 TradingView</a> • <a href=\"{binance_url}\">📊 Binance Spot</a>\n"
            )
        lines.append("─────────────────────")

    # 3. 🏆 ФИКСИРОВАННЫЙ ЗАРАБОТОК (ИСТОРИЯ ЗАКРЫТЫХ СДЕЛОК)
    if history:
        closed_pnl_sum = sum(float(h.get("pnl", 0.0)) for h in history)
        closed_cost_sum = sum(float(h.get("cost_usdt", 0.0)) for h in history)
        win_count = sum(1 for h in history if float(h.get("pnl", 0.0)) > 0)
        win_rate = (win_count / len(history) * 100.0) if history else 0.0
        hist_sign = "🟢" if closed_pnl_sum >= 0 else "🔴"

        lines.append("🏆 <b>ФИКСИРОВАННЫЙ ЗАРАБОТОК (ИСТОРИЯ):</b>")
        lines.append(f"• <b>Закрыто сделок по TP:</b> <code>{len(history)} шт</code> (Винрейт: <code>{win_rate:.0f}%</code>)")
        lines.append(f"• <b>Всего чистый профит:</b> {hist_sign} <b>{closed_pnl_sum:+.2f} USDT</b>")
        lines.append("─────────────────────")

    # 4. 💼 ОБЩИЙ ИТОГ ВСЕХ ИНВЕСТИЦИЙ
    total_all_cost = 0.0
    total_all_val = 0.0
    for symbol, pos in port.items():
        q = float(pos["qty"])
        a = float(pos["avg_price"])
        c = q * a
        total_all_cost += c
        p = prices.get(symbol)
        total_all_val += (q * p) if p is not None else c

    all_pnl = total_all_val - total_all_cost
    all_pct = (all_pnl / total_all_cost * 100.0) if total_all_cost > 0 else 0.0
    all_sign = "🟢" if all_pnl >= 0 else "🔴"

    lines.append(f"💵 <b>Всего инвестировано:</b> <code>{total_all_cost:,.2f} USDT</code>")
    lines.append(f"📊 <b>Текущая оценка активов:</b> <code>{total_all_val:,.2f} USDT</code>")
    lines.append(f"{all_sign} <b>Общий P/L портфеля:</b> <b>{all_pnl:+.2f} USDT ({fmt_pct(all_pct)})</b>")

    return "\n".join(lines)

def format_trades_and_profit_stats(state: dict) -> str:
    # 1. Синхронизируем открытые позиции и историю реальных сделок с Binance
    sync_trades_and_active_positions(state)

    active_trades = state.get("active_trades", {})
    portfolio = state.get("portfolio", {})
    history = state.get("trade_history", [])
    settings = state.get("settings", {})
    trade_amt = float(settings.get("trade_amount_usdt", DEFAULT_TRADE_AMOUNT))
    tp_pct = float(settings.get("take_profit_pct", DEFAULT_TAKE_PROFIT))
    auto_trade_status = "🟢 Включена" if settings.get("auto_trade", False) else "🔴 Выключена"

    api_key, api_secret = get_api_credentials(state)
    has_api = bool(api_key and api_secret)
    free_usdt = get_free_usdt_balance(state) if has_api else 0.0

    lines = [
        "📊 <b>СТАТИСТИКА АВТОТОРГОВЛИ И РЕАЛИЗОВАННЫЙ ПРОФИТ</b>\n",
        f"• <b>Автоторговля:</b> {auto_trade_status} (ставка: <code>{trade_amt:.0f} $</code>, TP: <code>+{tp_pct:.1f}%</code>)",
        f"• <b>Свободный баланс:</b> <code>{free_usdt:,.2f} USDT</code>\n",
        "─────────────────────"
    ]

    # 2. ОТКРЫТЫЕ СДЕЛКИ И СПОТОВЫЕ АКТИВЫ
    open_items = {}
    for sym, tr in active_trades.items():
        open_items[sym] = dict(tr)
    for sym, pos in portfolio.items():
        if sym not in open_items:
            open_items[sym] = {
                "symbol": sym,
                "base": base_asset(sym),
                "buy_price": float(pos.get("avg_price", 0.0)),
                "qty": float(pos.get("qty", 0.0)),
                "cost_usdt": float(pos.get("qty", 0.0)) * float(pos.get("avg_price", 0.0)),
                "tp_price": float(pos.get("avg_price", 0.0)) * (1.0 + tp_pct / 100.0),
                "tp_pct": tp_pct,
                "opened_at": pos.get("added_at", int(time.time())),
            }

    if open_items:
        symbols = list(open_items.keys())
        prices = get_multiple_prices(symbols) if symbols else {}

        total_cost = 0.0
        total_val = 0.0
        total_exp_gain = 0.0

        lines.append("⚡ <b>ОТКРЫТЫЕ СДЕЛКИ (BINANCE SPOT):</b>\n")
        for sym, tr in open_items.items():
            base = tr.get("base", base_asset(sym))
            bp = float(tr.get("buy_price", 0.0))
            tp = float(tr.get("tp_price", 0.0))
            qty = float(tr.get("qty", 0.0))
            cost = float(tr.get("cost_usdt", qty * bp))
            tp_order_id = tr.get("tp_order_id")
            opened_at = tr.get("opened_at", 0)
            date_str = time.strftime("%d.%m.%Y %H:%M", time.localtime(opened_at)) if opened_at else "В процессе"

            cur_price = prices.get(sym)
            if cur_price is not None:
                cur_val = qty * cur_price
                trade_pnl = cur_val - cost
                trade_pnl_pct = (trade_pnl / cost * 100.0) if cost > 0 else 0.0
                total_val += cur_val
            else:
                cur_val = cost
                trade_pnl = 0.0
                trade_pnl_pct = 0.0
                total_val += cost

            total_cost += cost
            exp_gain = (qty * tp) - cost if tp > 0 else 0.0
            total_exp_gain += exp_gain
            sign = "🟢" if trade_pnl >= 0 else "🔴"

            tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{sym}"
            binance_url = f"https://www.binance.com/en/trade/{base}_USDT?type=spot"

            tp_str = f"<code>{fmt_price(tp)} $</code> (+{tr.get('tp_pct', tp_pct):.1f}%)" if tp > 0 else "не задан"
            tp_order_str = f" [Ордер #{tp_order_id}]" if tp_order_id else ""
            exp_gain_str = f"+{exp_gain:.2f} USDT" if exp_gain > 0 else "—"

            lines.append(
                f"📅 <b>{date_str}</b>\n"
                f"• <b>Актив:</b> {sign} <b>{base}/USDT</b>\n"
                f"• <b>Цена:</b> вход <code>{fmt_price(bp)} $</code> → рынок <code>{fmt_price(cur_price) if cur_price else '—'} $</code>\n"
                f"• <b>Куплено:</b> <code>{fmt_qty(qty)} {base}</code> (Потрачено: <code>{cost:,.2f} USDT</code>)\n"
                f"• <b>Текущий PnL:</b> {sign} <b>{trade_pnl:+.2f} USDT ({fmt_pct(trade_pnl_pct)})</b> (Оценка: <code>{cur_val:,.2f} $</code>)\n"
                f"• <b>Тейк-профит (TP):</b> {tp_str}{tp_order_str}\n"
                f"• 💰 <b>Заработок при TP:</b> <b>{exp_gain_str}</b>\n"
                f"└ 🔗 <a href=\"{tv_url}\">📈 TradingView</a> • <a href=\"{binance_url}\">📊 Binance Spot</a>\n"
            )

        tot_pnl = total_val - total_cost
        tot_pct = (tot_pnl / total_cost * 100.0) if total_cost > 0 else 0.0
        t_sign = "🟢" if tot_pnl >= 0 else "🔴"

        lines.append("📌 <b>ИТОГО В ОТКРЫТЫХ ПОЗИЦИЯХ:</b>")
        lines.append(f"• <b>Позиций:</b> <code>{len(open_items)} шт</code>")
        lines.append(f"• <b>Всего инвестировано:</b> <code>{total_cost:,.2f} USDT</code>")
        lines.append(f"• <b>Текущая стоимость:</b> <code>{total_val:,.2f} USDT</code>")
        lines.append(f"• <b>Плавающий PnL:</b> {t_sign} <b>{tot_pnl:+.2f} USDT ({fmt_pct(tot_pct)})</b>")
        lines.append(f"• <b>Ожидаемый профит при закрытии всех TP:</b> 🟢 <b>+{total_exp_gain:.2f} USDT</b>")
        lines.append("─────────────────────\n")
    else:
        lines.append("⚡ <b>ОТКРЫТЫЕ СДЕЛКИ:</b> <i>Сейчас открытых позиций нет.</i>\n─────────────────────\n")

    # 3. РЕАЛИЗОВАННАЯ ПРИБЫЛЬ И ИСТОРИЯ ЗАКРЫТЫХ СДЕЛОК
    if history:
        closed_pnl_sum = sum(float(h.get("pnl", 0.0)) for h in history)
        closed_cost_sum = sum(float(h.get("cost_usdt", 0.0)) for h in history)
        win_count = sum(1 for h in history if float(h.get("pnl", 0.0)) > 0)
        win_rate = (win_count / len(history) * 100.0) if history else 0.0
        h_sign = "🟢" if closed_pnl_sum >= 0 else "🔴"

        lines.append("🏆 <b>РЕАЛИЗОВАННЫЙ ПРОФИТ (ЗАКРЫТЫЕ СДЕЛКИ):</b>")
        lines.append(f"• <b>Закрыто сделок:</b> <code>{len(history)} шт</code> (Винрейт: <code>{win_rate:.0f}%</code>)")
        lines.append(f"• <b>Суммарный оборот:</b> <code>{closed_cost_sum:,.2f} USDT</code>")
        lines.append(f"• 💰 <b>ЧИСТЫЙ РЕАЛИЗОВАННЫЙ ДОХОД:</b> {h_sign} <b>{closed_pnl_sum:+.2f} USDT</b>\n")

        lines.append("📜 <b>ИСТОРИЯ ЗАКРЫТЫХ СДЕЛОК:</b>\n")
        for h in reversed(history[-10:]):
            base = h.get("base", base_asset(h.get("symbol", "")))
            pnl = float(h.get("pnl", 0.0))
            pnl_pct = float(h.get("pnl_pct", 0.0))
            closed_at = h.get("closed_at", 0)
            date_str = time.strftime("%d.%m.%Y %H:%M", time.localtime(closed_at)) if closed_at else "—"
            bp = float(h.get("buy_price", 0.0))
            sp = float(h.get("sell_price", 0.0))
            qty = float(h.get("qty", 0.0))
            cost = float(h.get("cost_usdt", 0.0))
            revenue = cost + pnl
            bal_end = float(h.get("ending_balance", 0.0))
            bal_end_str = f"<code>{bal_end:,.2f} USDT</code>" if bal_end > 0 else f"<code>{free_usdt:,.2f} USDT</code>"
            p_sign = "🟢" if pnl >= 0 else "🔴"

            lines.append(
                f"📅 <b>{date_str}</b>\n"
                f"• <b>Актив:</b> {p_sign} <b>{base}/USDT</b>\n"
                f"• <b>Цена:</b> вход <code>{fmt_price(bp)} $</code> → продажа <code>{fmt_price(sp)} $</code>\n"
                f"• <b>Куплено:</b> <code>{fmt_qty(qty)} {base}</code> (Потрачено: <code>{cost:,.2f} USDT</code>)\n"
                f"• <b>Продано на сумму:</b> <code>{revenue:,.2f} USDT</code>\n"
                f"• 💰 <b>Заработано:</b> {p_sign} <b>{pnl:+.2f} USDT ({fmt_pct(pnl_pct)})</b>\n"
                f"• 💵 <b>Баланс на конец:</b> {bal_end_str}\n"
                f"─────────────────────"
            )
    else:
        lines.append("🏆 <b>РЕАЛИЗОВАННАЯ ПРИБЫЛЬ:</b> <i>История закрытых сделок пока пуста.</i>\n─────────────────────")

    # 4. ОБЩИЙ БАЛАНС И КАПИТАЛ
    lines.append(f"\n💵 <b>Свободный баланс:</b> <code>{free_usdt:,.2f} USDT</code>")
    if open_items:
        total_open = sum(float(tr.get("cost_usdt", 0.0)) for tr in open_items.values())
        lines.append(f"⚡ <b>В открытых позициях:</b> <code>{total_open:,.2f} USDT</code>")
        lines.append(f"📊 <b>Общий капитал:</b> <code>{(free_usdt + total_open):,.2f} USDT</code>")

    return "\n".join(lines)

# ───────────────────────── Клавиатуры ─────────────────────────

def main_keyboard() -> dict:
    # Компактная клавиатура: добавление/удаление активов доступно
    # через меню «💼 Портфель» (inline-кнопки ➕/🗑) — дублирование убрано.
    return {
        "keyboard": [
            [{"text": "🔍 Скан сейчас"}, {"text": "🐋 Скан китов"}],
            [{"text": "💼 Портфель"}, {"text": "📊 Сделки и Профит"}],
            [{"text": "💳 Баланс Binance"}, {"text": "⚙️ Настройки"}],
            [{"text": "🙈 Скрыть клавиатуру"}],
        ],
        "resize_keyboard": True,
        "is_persistent": False,
    }

def cancel_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "❌ Отмена"}],
            [{"text": "🔍 Скан сейчас"}, {"text": "🐋 Скан китов"}],
            [{"text": "💼 Портфель"}, {"text": "⚙️ Настройки"}],
            [{"text": "🙈 Скрыть клавиатуру"}],
        ],
        "resize_keyboard": True,
        "is_persistent": False,
    }

def portfolio_inline_kb(state: Optional[dict] = None) -> dict:
    rows = [
        [
            {"text": "🔄 Обновить цены", "callback_data": "port:refresh"},
            {"text": "📊 Сделки и Профит", "callback_data": "port:trades_stats"},
        ],
    ]

    # Если есть конкретные монеты — добавляем прямые кнопки быстрой настройки TP
    if state:
        active_trades = state.get("active_trades", {})
        port = state.get("portfolio", {})
        all_syms = sorted(set(list(active_trades.keys()) + list(port.keys())))
        if all_syms:
            tp_buttons = []
            for s in all_syms[:4]:  # До 4 монет на виду
                base = base_asset(s)
                tp_buttons.append({"text": f"🎯 TP: {base}", "callback_data": f"target_pick:{s}"})
            for i in range(0, len(tp_buttons), 2):
                rows.append(tp_buttons[i:i+2])

    rows.extend([
        [
            {"text": "🎯 Все автопродажи (TP)", "callback_data": "port:targetsell_menu"},
            {"text": "🔴 Продать актив", "callback_data": "port:sell_menu"},
        ],
        [
            {"text": "➕ Добавить", "callback_data": "port:add"},
            {"text": "🗑 Удалить", "callback_data": "port:del"},
        ],
        [
            {"text": "📈 Графики (TV / Binance)", "callback_data": "port:charts"},
            {"text": "💳 Баланс Binance", "callback_data": "trade:balance"},
        ],
        [
            {"text": "🔙 Главное меню", "callback_data": "menu:main"},
        ]
    ])
    return {"inline_keyboard": rows}

def portfolio_charts_inline_kb(state: dict) -> dict:
    port = state.get("portfolio", {})
    active_trades = state.get("active_trades", {})
    all_symbols = sorted(set(list(port.keys()) + list(active_trades.keys())))
    if not all_symbols:
        return {
            "inline_keyboard": [
                [{"text": "🔙 Назад в портфель", "callback_data": "port:refresh"}]
            ]
        }
    rows = []
    for s in all_symbols:
        base = base_asset(s)
        tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{s}"
        binance_url = f"https://www.binance.com/en/trade/{base}_USDT?type=spot"
        rows.append([
            {"text": f"📈 {base} TradingView", "url": tv_url},
            {"text": f"📊 {base} Binance", "url": binance_url},
        ])
    rows.append([{"text": "🔙 Назад в портфель", "callback_data": "port:refresh"}])
    return {"inline_keyboard": rows}

def remove_asset_inline_kb(state: dict) -> Optional[dict]:
    port = state.get("portfolio", {})
    if not port:
        return None
    rows = []
    current_row = []
    for s in sorted(port.keys()):
        current_row.append({"text": f"❌ {base_asset(s)}", "callback_data": f"del:{s}"})
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    rows.append([{"text": "🔙 Назад", "callback_data": "port:refresh"}])
    return {"inline_keyboard": rows}

def settings_text(state: dict) -> str:
    s = state["settings"]
    filt_label = "🔥 Только Strong" if s.get("filter_level") == "strong_only" else "⚡ Strong + Watch"
    auto_scan_label = "🟢 Включён" if s.get("autoscan", True) else "🔴 Выключен"
    tp_pct = s.get("take_profit_pct", DEFAULT_TAKE_PROFIT)
    sl_pct = s.get("stop_loss_pct", DEFAULT_STOP_LOSS)
    dyn_tp = s.get("dynamic_tp", DEFAULT_DYNAMIC_TP)
    dyn_tp_label = "🟢 Включён (авто ATR)" if dyn_tp else "🔴 Выключен (фиксированный)"
    trail_act = s.get("trailing_activation_pct", DEFAULT_TRAILING_ACTIVATION)
    trail_dist = s.get("trailing_distance_pct", DEFAULT_TRAILING_DISTANCE)
    entry_pb = float(s.get("entry_pullback_pct", DEFAULT_ENTRY_PULLBACK))
    if entry_pb <= 0.001:
        entry_label = "⚡ По рынку (мгновенно)"
    else:
        entry_label = f"🎯 Откат -{entry_pb:.1f}% (лимитный вход)"

    auto_trade_label = f"🟢 Включена (+{tp_pct:.1f}% TP, -{sl_pct:.1f}% SL)" if s.get("auto_trade", False) else "🔴 Выключена"
    interval_m = s.get("scan_interval_sec", DEFAULT_SCAN_INTERVAL) // 60

    api_key, api_secret = get_api_credentials(state)
    api_status = "🟢 Подключены" if (api_key and api_secret) else "⚪ Не заданы (только сигналы)"

    trade_mode = s.get("trade_mode", "fixed")
    if trade_mode == "all":
        trade_size_label = "💯 Весь свободный баланс USDT"
    elif trade_mode.startswith("pct:"):
        pct_val = trade_mode.split(":")[1]
        trade_size_label = f"{pct_val}% от свободного баланса USDT"
    else:
        trade_amt = s.get("trade_amount_usdt", DEFAULT_TRADE_AMOUNT)
        trade_size_label = f"{trade_amt:.1f} USDT (фиксированно)"

    return (
        "<b>⚙️ Параметры бота Pump Pulse</b>\n\n"
        f"• <b>Автосканирование рынка:</b> {auto_scan_label} (каждые {interval_m} мин)\n"
        f"• <b>Порог Score (MIN_SCORE):</b> <code>{s['min_score']:.0f}</code>\n"
        f"• <b>Уведомления:</b> <code>{filt_label}</code>\n\n"
        "<b>⚡ Спотовая торговля Binance:</b>\n"
        f"• <b>Автоторговля пампов:</b> {auto_trade_label}\n"
        f"• <b>Размер ставки:</b> <code>{trade_size_label}</code>\n"
        f"• <b>Точка входа:</b> <code>{entry_label}</code>\n"
        f"• <b>Базовый Take-Profit:</b> <code>+{tp_pct:.1f}%</code>\n"
        f"• <b>Умный ATR Take-Profit:</b> <code>{dyn_tp_label}</code>\n"
        f"• <b>Stop-Loss:</b> <code>-{sl_pct:.1f}%</code>\n"
        f"• <b>Trailing Stop:</b> автоподтяжка при <code>+{trail_act:.1f}%</code> (отступ <code>{trail_dist:.1f}%</code>)\n"
        f"• <b>Статус API Binance:</b> {api_status}\n\n"
        "<i>Используйте кнопки ниже для быстрой настройки:</i>"
    )

def settings_inline_kb(state: dict) -> dict:
    s = state["settings"]
    autoscan_label = "🔴 Отключить автоскан" if s.get("autoscan", True) else "🟢 Включить автоскан"
    filter_label = "🔔 Сигналы: Только Strong" if s.get("filter_level") == "strong_only" else "🔔 Сигналы: Strong + Watch"
    autotrade_label = "🔴 Выключить автоторговлю" if s.get("auto_trade", False) else "⚡ Включить автоторговлю"
    dyn_tp_btn_label = "🧮 Выкл умный ATR TP" if s.get("dynamic_tp", DEFAULT_DYNAMIC_TP) else "🧮 Вкл умный ATR TP"

    trade_mode = s.get("trade_mode", "fixed")
    cur_sl = float(s.get("stop_loss_pct", DEFAULT_STOP_LOSS))
    cur_tp = float(s.get("take_profit_pct", DEFAULT_TAKE_PROFIT))
    cur_entry = float(s.get("entry_pullback_pct", DEFAULT_ENTRY_PULLBACK))

    def _amt_btn(label: str, mode: str) -> dict:
        active = "✅ " if trade_mode == mode else ""
        return {"text": f"{active}{label}", "callback_data": f"trade:amt:{mode}"}

    def _sl_btn(val: float) -> dict:
        active = "✅ " if abs(cur_sl - val) < 0.05 else ""
        return {"text": f"{active}🛑 SL: -{val:.1f}%", "callback_data": f"trade:sl:{val:.1f}"}

    def _tp_btn(val: float) -> dict:
        active = "✅ " if abs(cur_tp - val) < 0.05 else ""
        return {"text": f"{active}🎯 TP: +{val:.1f}%", "callback_data": f"trade:tp:{val:.1f}"}

    def _entry_btn(label: str, val: float) -> dict:
        active = "✅ " if abs(cur_entry - val) < 0.05 else ""
        return {"text": f"{active}{label}", "callback_data": f"trade:entry:{val:.1f}"}

    return {
        "inline_keyboard": [
            [
                {"text": autotrade_label, "callback_data": "trade:toggle"},
            ],
            # --- Режим ставки ---
            [
                _amt_btn("💯 Весь баланс", "all"),
            ],
            [
                _amt_btn("10% баланса", "pct:10"),
                _amt_btn("20% баланса", "pct:20"),
                _amt_btn("30% баланса", "pct:30"),
            ],
            [
                _amt_btn("40% баланса", "pct:40"),
                _amt_btn("50% баланса", "pct:50"),
            ],
            # --- Точка входа (Откат / Маркет) ---
            [
                _entry_btn("⚡ По рынку", 0.0),
                _entry_btn("🎯 Откат -0.2%", 0.2),
            ],
            [
                _entry_btn("🎯 Откат -0.4% (оптимал)", 0.4),
                _entry_btn("🎯 Откат -0.6%", 0.6),
            ],
            # --- Take Profit ---
            [
                _tp_btn(1.0),
                _tp_btn(1.5),
            ],
            [
                _tp_btn(2.0),
                _tp_btn(2.5),
            ],
            [
                {"text": dyn_tp_btn_label, "callback_data": "trade:dyn_tp:toggle"},
            ],
            # --- Stop Loss ---
            [
                _sl_btn(1.5),
                _sl_btn(2.0),
            ],
            [
                _sl_btn(2.5),
                _sl_btn(3.0),
            ],
            [
                {"text": "💳 Баланс Binance", "callback_data": "trade:balance"},
            ],
            [
                {"text": "Порог Score −5", "callback_data": "score:-5"},
                {"text": "Порог Score +5", "callback_data": "score:+5"},
            ],
            [
                {"text": "Score: 55", "callback_data": "score:set:55"},
                {"text": "Score: 62 (базовый)", "callback_data": "score:set:62"},
                {"text": "Score: 70 (строгий)", "callback_data": "score:set:70"},
            ],
            [
                {"text": autoscan_label, "callback_data": "autoscan:toggle"},
                {"text": filter_label, "callback_data": "filter:toggle"},
            ],
            [
                {"text": "⏱ 3 мин", "callback_data": "interval:180"},
                {"text": "⏱ 5 мин", "callback_data": "interval:300"},
                {"text": "⏱ 10 мин", "callback_data": "interval:600"},
            ],
            [
                {"text": "🔄 Обновить статус", "callback_data": "settings:refresh"},
                {"text": "🔙 Вернуть меню кнопок", "callback_data": "menu:main"},
            ]
        ]
    }

def signal_inline_kb(sig: PumpSignal, state: Optional[dict] = None) -> dict:
    base = sig.base
    binance_url = f"https://www.binance.com/en/trade/{base}_USDT?type=spot"
    tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{sig.symbol}"

    trade_amt = 11.0
    tp_pct = 3.0
    if state:
        trade_amt = float(state.get("settings", {}).get("trade_amount_usdt", DEFAULT_TRADE_AMOUNT))
        tp_pct = float(state.get("settings", {}).get("take_profit_pct", DEFAULT_TAKE_PROFIT))

    return {
        "inline_keyboard": [
            [
                {"text": "📊 Binance Spot", "url": binance_url},
                {"text": "📈 TradingView", "url": tv_url},
            ],
            [
                {"text": f"⚡ Купить {base} (TP +{tp_pct:.0f}%)", "callback_data": f"trade_buy:{sig.symbol}"},
            ],
            [
                {"text": f"➕ Добавить {base} в портфель", "callback_data": f"add_coin:{sig.symbol}:{sig.price}"},
                {"text": "ℹ️ Детали факторов", "callback_data": f"factors:{sig.symbol}:{sig.best_tf}"},
            ]
        ]
    }

def trade_buy_amount_kb(symbol: str, state: dict) -> dict:
    """Клавиатура выбора суммы покупки перед исполнением сигнала."""
    settings = state.get("settings", {})
    default_amt = float(settings.get("trade_amount_usdt", DEFAULT_TRADE_AMOUNT))
    free_usdt = get_free_usdt_balance(state)
    base = base_asset(symbol)
    filters = get_symbol_filters(symbol)
    min_notional = float(filters.get("min_notional", 5.0))

    # Пресеты: показываем только суммы >= min_notional и вмещающиеся в баланс
    presets = [5, 10, 25, 50, 100]
    preset_rows = []
    row = []
    for amt in presets:
        if amt >= min_notional and amt <= free_usdt + 0.5:
            marker = " ✅" if abs(amt - default_amt) < 0.5 else ""
            row.append({"text": f"{amt}${marker}", "callback_data": f"trade_buy_amt:{symbol}:{amt}"})
        if len(row) == 4:
            preset_rows.append(row)
            row = []
    if row:
        preset_rows.append(row)

    # Кнопка «весь свободный баланс» (только если баланс покрывает минимум биржи)
    all_row = []
    if free_usdt >= min_notional:
        all_row.append({"text": f"💰 Весь баланс ({free_usdt:.1f}$)", "callback_data": f"trade_buy_amt:{symbol}:all"})
    all_row.append({"text": "✍️ Ввести вручную", "callback_data": f"trade_buy_custom_amt:{symbol}"})

    return {
        "inline_keyboard": preset_rows + [all_row, [
            {"text": "❌ Отмена", "callback_data": "menu:main"},
        ]]
    }


HELP_TEXT = (
    "<b>🚀 Pump Pulse Scanner 2.1 & Binance Spot Trader</b>\n\n"
    "Бот отслеживает аномальную активность на спотовом рынке Binance (USDT-пары) "
    "и поддерживает автоматическую покупку с мгновенным выставлением Take-Profit (+3%).\n\n"
    "<b>📌 Основные функции:</b>\n"
    "• <b>🔍 Скан сейчас</b> (/scan) — сканирование всего спота прямо сейчас.\n"
    "• <b>💼 Портфель</b> (/portfolio) — баланс Binance, активные сделки и PnL.\n"
    "• <b>⚡ Торговля</b> (/trade) — статус автоторговли и история профита.\n"
    "• <b>🎯 Автопродажи</b> (/targetsell) — настройка целевой цены продажи (TP).\n"
    "• <b>➕ Добавить актив</b> (/add) — внести купленную монету вручную.\n"
    "• <b>🗑 Удалить актив</b> (/del) — убрать позицию в 1 клик.\n"
    "• <b>⚙️ Настройки</b> (/settings) — включение автоторговли, размер ставки и TP.\n"
    "• <b>🔑 Привязка API</b> (/api) — настройка Binance API ключей.\n\n"
    "<b>⚡ Как работает спотовая автоторговля:</b>\n"
    "1. При обнаружении подтверждённого импульса (Score 74+, STRONG) бот проверяет свободный USDT-баланс.\n"
    "2. На бирже размещается спотовый MARKET BUY ордер на указанную сумму (~11 USDT).\n"
    "3. Сразу же выставляется лимитный ордер на продажу (LIMIT SELL GTC) с профитом +3%.\n"
    "4. Когда цена доходит до цели, ордер исполняется и средства автоматически возвращаются в USDT!\n\n"
    "<b>🧠 8 факторов раннего обнаружения импульса:</b>\n"
    "1. <b>Всплеск объёма (25б):</b> резкий приток капитала относительно SMA20.\n"
    "2. <b>Taker Buy агрессия (22б):</b> доминирование покупок по рынку (>60-70%).\n"
    "3. <b>Сжатие волатильности (15б):</b> пружина Bollinger Squeeze перед выстрелом.\n"
    "4. <b>Пробой локальной базы (12б):</b> ранний выход из коридора (+0.3%..+2.5%).\n"
    "5. <b>Структура свечи (10б):</b> бычья формация без длинных верхних теней.\n"
    "6. <b>Окно RSI 14 (8б):</b> коридор 48-65 (ранний старт без перекупленности).\n"
    "7. <b>Тренд EMA 9/21 (4б):</b> выравнивание скользящих вверх.\n"
    "8. <b>Опережение BTC (4б):</b> динамика сильнее биткоина.\n\n"
    "<b>🛡 Защита от слива и верхушек:</b>\n"
    "• Монеты с суточным ростом > +7% или красными свечами (дамп) отсекаются.\n"
    "• Сигналы выдаются <b>до</b> взлёта, а не на пике движения!"
)

# ───────────────────────── Telegram API ─────────────────────────

def api_call(token: str, method: str, payload: Optional[dict] = None, retries: int = 3, timeout: int = 15) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload or {}).encode("utf-8")
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            if attempt == retries:
                raise
            time.sleep(0.5 * attempt)
    return {}

def drop_pending_updates(token: str) -> None:
    try:
        api_call(token, "getUpdates", {"offset": -1, "timeout": 1}, retries=1, timeout=5)
    except Exception:
        pass

def send_telegram(token: str, chat_id: Union[str, int], text: str, reply_markup: Optional[dict] = None, retries: int = 3) -> Optional[int]:
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    for attempt in range(1, retries + 1):
        try:
            res = api_call(token, "sendMessage", payload)
            if res.get("ok"):
                return res["result"]["message_id"]
            time.sleep(0.5 * attempt)
        except Exception as e:
            if attempt == retries:
                print(f"Telegram send error: {e}", file=sys.stderr)
                return None
            time.sleep(0.5 * attempt)
    return None

def edit_message(token: str, chat_id: Union[str, int], message_id: int, text: str, reply_markup: Optional[dict] = None) -> None:
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        api_call(token, "editMessageText", payload, retries=1)
    except Exception as e:
        print(f"Telegram edit error: {e}", file=sys.stderr)

def answer_callback(token: str, callback_id: str, text: Optional[str] = None, show_alert: bool = False) -> None:
    payload: Dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = show_alert
    try:
        api_call(token, "answerCallbackQuery", payload, retries=1)
    except Exception:
        pass

def set_bot_commands(token: str) -> None:
    cmds = [
        {"command": "start", "description": "Главное меню / открыть кнопки"},
        {"command": "menu", "description": "📋 Открыть клавиатуру меню"},
        {"command": "hide", "description": "🙈 Скрыть клавиатуру"},
        {"command": "scan", "description": "Сканер спота сейчас"},
        {"command": "portfolio", "description": "Портфель и PnL"},
        {"command": "trade", "description": "Статус автоторговли и профит"},
        {"command": "targetsell", "description": "🎯 Автопродажа по целевой цене"},
        {"command": "sell", "description": "🔴 Продать актив досрочно"},
        {"command": "add", "description": "Добавить монету в портфель"},
        {"command": "del", "description": "Удалить монету из портфеля"},
        {"command": "settings", "description": "Настройки"},
        {"command": "api", "description": "Указать API ключи Binance"},
        {"command": "help", "description": "Инструкция"},
    ]
    try:
        api_call(token, "setMyCommands", {"commands": cmds}, retries=1)
    except Exception:
        pass

def get_updates(token: str, offset: Optional[int], timeout: int = 40) -> dict:
    return api_call(token, "getUpdates", {"offset": offset, "timeout": timeout}, retries=1, timeout=timeout + 10)

def format_alert(sig: PumpSignal) -> str:
    tf_lines = []
    for r in sig.by_tf:
        tf_lines.append(f"  {r.timeframe}: score {r.score:.0f} ({r.grade})")
    tf_str = "\n".join(tf_lines)

    best = sig.by_tf[0] if sig.by_tf else None
    best_reasons = "\n".join(f"  ✓ {r}" for r in (best.reasons[:3] if best else [])) or ""
    best_risks = "\n".join(f"  • {r}" for r in (best.risks[:2] if best else [])) or ""

    late_note = ""
    if best and best.late:
        late_note = "\n⚠️ <b>Фаза:</b> <i>памп уже идёт — вход рискован</i>"

    grade_emoji = {"strong": "🚀", "watch": "👀", "late": "⚠️"}[sig.grade]
    return (
        f"{grade_emoji} <b>{sig.base}/USDT</b> | TF: <b>{sig.best_tf}</b> | Score: <b>{sig.best_score:.0f}</b> ({sig.grade.upper()}){late_note}\n\n"
        f"💵 Цена: <code>{fmt_price(sig.price)} USDT</code> | 24ч: <code>{fmt_pct(sig.change_24h)}</code> (vs BTC: {fmt_pct(sig.btc_relative_24h)})\n"
        f"🌊 Объём 24ч: <code>{sig.quote_volume_24h:,.0f} USDT</code>\n\n"
        f"<b>📊 Скоринг по ТФ:</b>\n{tf_str}\n"
        f"<b>Почему:</b>\n{best_reasons}\n"
        f"<b>Риски:</b>\n{best_risks}"
    )

def format_factor_breakdown(sig: PumpSignal, tf: str) -> str:
    row = next((r for r in sig.by_tf if r.timeframe == tf), None)
    if not row:
        return f"Нет данных по ТФ {tf}"
    lines = [f"<b>📋 Факторы {sig.base} {tf}</b> (score {row.score:.0f}, {row.grade})\n"]
    for f in row.factors:
        bar_len = int(f.value * 10)
        bar = "█" * bar_len + "░" * (10 - bar_len)
        lines.append(f"{f.label:<18} {bar} {f.value*100:5.0f}%  ({f.weight}б, {f.note})")
    if row.reasons:
        lines.append("\n<b>Причины:</b>")
        lines += [f"  ✓ {r}" for r in row.reasons]
    if row.risks:
        lines.append("\n<b>Риски:</b>")
        lines += [f"  • {r}" for r in row.risks]
    return "\n".join(lines)


# ───────────────────────── Одноразовый отчёт (для CI) ─────────────────────────


def execute_scan_and_report(
    token: str,
    chat_id: Union[str, int],
    state: dict,
    min_score: Optional[float] = None,
) -> None:
    s = state["settings"]
    status_msg_id = send_telegram(token, chat_id, "⏳ <i>Сканирую Binance Spot USDT-пары... Подождите.</i>")
    try:
        signals, meta, top = run_scan(
            min_score=min_score if min_score is not None else s["min_score"],
            min_quote_volume=s.get("min_quote_volume", DEFAULT_MIN_QUOTE_VOLUME),
        )
        duration = meta["duration_ms"] / 1000.0
        btc = meta["btc"]

        if not signals:
            top_lines = "\n".join(
                f"{i}. <b>{c['base']}</b> — score <code>{c['best_score']:.0f}</code> ({c['grade']}, {c['best_tf']}), "
                f"объём {c['vol_ratio']:.1f}×"
                for i, c in enumerate(top, 1)
            ) or "<i>нет данных</i>"
            report_text = (
                f"✅ <b>Скан завершён за {duration:.1f}с</b>\n\n"
                f"• Просканировано пар: <code>{meta['candidates']}</code> из {meta['universe']}\n"
                f"• Порог Score: <code>{meta['min_score']:.0f}</code>\n"
                f"• BTC: <code>{fmt_price(btc['price'])} USDT</code> ({fmt_pct(btc['change24h'])})\n"
                f"• Сигналов: <b>0</b> <i>(порог {meta['min_score']:.0f} не превышен)</i>\n\n"
                f"<b>Ближайшие кандидаты:</b>\n{top_lines}\n\n"
                f"💡 <i>Вы можете купить любого кандидата кнопками ниже или понизить порог Score в ⚙️ Настройки.</i>"
            )
            kb_rows = []
            for c in top[:3]:
                b = c['base']
                sym = c['symbol']
                sc = c['best_score']
                kb_rows.append([
                    {"text": f"⚡ Купить {b} (Score {sc:.0f})", "callback_data": f"trade_buy:{sym}"},
                    {"text": f"📈 {b} TV", "url": f"https://www.tradingview.com/chart/?symbol=BINANCE:{sym}"},
                ])
            kb_rows.append([
                {"text": "⚙️ Настройки порога Score", "callback_data": "menu:settings"},
            ])
            kb = {"inline_keyboard": kb_rows}

            if status_msg_id:
                edit_message(token, chat_id, status_msg_id, report_text, reply_markup=kb)
            else:
                send_telegram(token, chat_id, report_text, reply_markup=kb)
            return

        sent: dict = state.setdefault("sent_alerts", {})
        now = int(time.time())
        sent_count = 0
        filter_level = s.get("filter_level", "strong_and_watch")

        for sig in signals:
            if filter_level == "strong_only" and sig.grade != "strong":
                continue
            if sig.alert_key in sent:
                continue
            sent[sig.alert_key] = now
            sent_count += 1
            kb = signal_inline_kb(sig, state)
            send_telegram(token, chat_id, format_alert(sig), reply_markup=kb)
            time.sleep(0.2)

        if sent_count == 0:
            msg = f"🔔 <b>Новых сигналов нет.</b>\n\n• Скан завершён за {duration:.1f}с\n• Сигналов: {len(signals)} (все уже отправлены ранее)"
            if status_msg_id:
                edit_message(token, chat_id, status_msg_id, msg)
            else:
                send_telegram(token, chat_id, msg)
        else:
            save_state(state)
            header = f"🚀 <b>Найдено {sent_count} новых сигналов!</b> (всего: {len(signals)}, скан за {duration:.1f}с)"
            if status_msg_id:
                edit_message(token, chat_id, status_msg_id, header)
            else:
                send_telegram(token, chat_id, header)
    except Exception as e:
        print(f"❌ Ошибка скана: {e}", file=sys.stderr)
        traceback.print_exc()
        send_telegram(token, chat_id, f"⚠️ <b>Ошибка скана:</b>\n<code>{e}</code>")

def run_oneshot(token: Optional[str], chat_id: Optional[str]) -> None:
    print("=== Режим разового скана (Oneshot) ===")
    state = load_state()
    min_score = state.get("settings", {}).get("min_score", DEFAULT_MIN_SCORE)
    signals, meta, top = run_scan(min_score=min_score)
    print(f"Scan: {meta['duration_ms']/1000:.1f}с, candidates={meta['candidates']}, сигналов={len(signals)}")

    if not token or not chat_id:
        for sig in signals:
            print(format_alert(sig))
            print("─" * 50)
        if not signals:
            for c in top:
                print(f"~ {c['base']}: score={c['best_score']:.0f} ({c['grade']}, {c['best_tf']})")
        return

    now = int(time.time())
    sent: dict = state.setdefault("sent_alerts", {})
    target_chats: Set[str] = set()

    if chat_id and (chat_id.lstrip("-").isdigit() or chat_id.startswith("@")):
        target_chats.add(chat_id)
    for c in state.get("allowed_chats", []):
        cid_str = str(c).strip()
        if cid_str and cid_str != "12345" and (cid_str.lstrip("-").isdigit() or cid_str.startswith("@")):
            target_chats.add(cid_str)

    sent_count = 0
    sym_cd = state.setdefault("symbol_alert_cooldown", {})
    for sig in signals:
        if sig.alert_key in sent:
            print(f"• Пропуск {sig.symbol}: уже отправлен")
            continue
        if now - int(sym_cd.get(sig.symbol, 0)) < 1800:
            print(f"• Пропуск {sig.symbol}: кулдаун 30 мин (анти-спам)")
            continue
        sent[sig.alert_key] = now
        sym_cd[sig.symbol] = now
        sent_count += 1
        kb = signal_inline_kb(sig, state)
        for cid in target_chats:
            ok = send_telegram(token, cid, format_alert(sig), reply_markup=kb)
            print(f"• {sig.symbol} ({sig.grade} {sig.best_score:.0f}) → {cid}: {'sent' if ok else 'fail'}")

    if sent_count > 0:
        save_state(state)
        print(f"Отправлено новых сигналов: {sent_count}")
    else:
        print("Новых сигналов нет.")
        is_manual = (
            os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            or os.environ.get("NOTIFY_EMPTY", "false").lower() in ("true", "1")
            or "--notify-always" in sys.argv
        )
        if is_manual:
            btc = meta["btc"]
            top_lines = "\n".join(
                f"  • {c['base']}: score {c['best_score']:.0f} ({c['grade']}, {c['best_tf']})"
                for c in top[:3]
            ) or "  —"
            msg = (
                f"📡 <b>Pump Scan — отчёт</b>\n\n"
                f"• Просканировано пар: <code>{meta['candidates']}</code>\n"
                f"• Порог: <code>{min_score:.0f}</code>\n"
                f"• BTC: <code>{fmt_price(btc['price'])}</code> ({fmt_pct(btc['change24h'])})\n"
                f"• Сигналов: <b>0</b>\n\n"
                f"<b>Топ кандидатов:</b>\n{top_lines}"
            )
            for cid in target_chats:
                send_telegram(token, cid, msg)
            print("Отправлен статус-отчёт.")

# ───────────────────────── Фоновый автоскан ─────────────────────────

def autoscan_worker(token: str, primary_chat_id: Union[str, int], state: dict, stop_event: threading.Event) -> None:
    print("🤖 Фоновый автоскан запущен.")
    while not stop_event.is_set():
        settings = state["settings"]
        if not settings.get("autoscan", True):
            stop_event.wait(5)
            continue

        interval = int(settings.get("scan_interval_sec", DEFAULT_SCAN_INTERVAL))
        try:
            now = int(time.time())
            target_chats: Set[str] = set()
            primary_str = str(primary_chat_id).strip()
            if primary_str and primary_str != "12345" and (primary_str.lstrip("-").isdigit() or primary_str.startswith("@")):
                target_chats.add(primary_str)
            for c in state.get("allowed_chats", []):
                cid_str = str(c).strip()
                if cid_str and cid_str != "12345" and (cid_str.lstrip("-").isdigit() or cid_str.startswith("@")):
                    target_chats.add(cid_str)

            # Проверка ордеров (входы/TP/SL) вынесена в trade_monitor_worker (каждые 20 сек) —
            # здесь не дублируем, чтобы не было гонок при выставлении TP.

            # 1. Скан пампов
            signals, meta, _top = run_scan(
                min_score=settings.get("min_score", DEFAULT_MIN_SCORE),
                min_quote_volume=settings.get("min_quote_volume", DEFAULT_MIN_QUOTE_VOLUME),
            )
            print(f"[Autoscan] {time.strftime('%H:%M:%S')}: {meta['duration_ms']/1000:.1f}с, "
                  f"candidates={meta['candidates']}, сигналов={len(signals)}")

            filter_level = settings.get("filter_level", "strong_and_watch")
            sent: dict = state.setdefault("sent_alerts", {})
            sym_cooldown: dict = state.setdefault("symbol_alert_cooldown", {})
            new_alerts_count = 0

            for sig in signals:
                if filter_level == "strong_only" and sig.grade != "strong":
                    continue
                if sig.alert_key in sent:
                    continue
                # Анти-спам: не более 1 алерта на монету за 30 минут
                # (иначе при продолжающемся пампе алерт прилетал на каждом новом 5м баре)
                if now - int(sym_cooldown.get(sig.symbol, 0)) < 1800:
                    continue

                sent[sig.alert_key] = now
                sym_cooldown[sig.symbol] = now
                new_alerts_count += 1
                kb = signal_inline_kb(sig, state)

                for cid in target_chats:
                    try:
                        send_telegram(token, cid, format_alert(sig), reply_markup=kb)
                    except Exception as e:
                        print(f"Ошибка отправки в {cid}: {e}", file=sys.stderr)

                # Автоторговля по сигналу
                auto_trade_enabled = settings.get("auto_trade", False)
                trade_min_score = settings.get("trade_min_score", DEFAULT_TRADE_MIN_SCORE)
                if auto_trade_enabled and sig.best_score >= trade_min_score:
                    if primary_str:
                        try:
                            print(f"[AutoTrade] Вход по сигналу {sig.symbol} (score {sig.best_score:.1f})...")
                            execute_pump_auto_trade(token, primary_str, state, sig)
                        except Exception as e:
                            print(f"[AutoTrade Error for {sig.symbol}]: {e}", file=sys.stderr)

            if new_alerts_count > 0:
                save_state(state, sync_git=True)
                print(f"[Autoscan] Новых сигналов: pump={new_alerts_count}, чатов: {len(target_chats)}")

        except Exception as e:
            print(f"[Autoscan Error]: {e}", file=sys.stderr)
            traceback.print_exc()

        stop_event.wait(interval)


def trade_monitor_worker(token: str, primary_chat_id: Union[str, int], state: dict, stop_event: threading.Event) -> None:
    """
    Быстрый монитор торговых ордеров (каждые 20 сек):
    - исполнение лимитных входов на откате (pending_entries)
    - исполнение Take-Profit ордеров на бирже
    - срабатывание Stop-Loss / Trailing-Stop по рыночной цене

    Раньше эти проверки выполнялись только внутри цикла автоскана
    (раз в 3–10 минут) — TP мог исполниться, а бот узнавал об этом
    с задержкой до 10 минут. Теперь реакция ≤ 20 секунд.
    """
    print("⚡ Монитор ордеров (входы/TP/SL) запущен: интервал 20 сек.")
    while not stop_event.is_set():
        try:
            settings = state.get("settings", {})
            if settings.get("auto_trade", False):
                if state.get("pending_entries"):
                    try:
                        check_pending_entries(token, primary_chat_id, state)
                    except Exception as e:
                        print(f"[PendingEntries Check Error]: {e}", file=sys.stderr)
                if state.get("active_trades"):
                    try:
                        check_active_trades(token, primary_chat_id, state)
                    except Exception as e:
                        print(f"[AutoTrade Check Error]: {e}", file=sys.stderr)
        except Exception as e:
            print(f"[TradeMonitor Error]: {e}", file=sys.stderr)
        stop_event.wait(20)


# ───────────────────────── Основной цикл бота ─────────────────────────

def run_bot(token: str, chat_id: Union[str, int]) -> None:
    state = load_state()
    cid_str = str(chat_id).strip()
    if cid_str and cid_str not in [str(c) for c in state.get("allowed_chats", [])]:
        state["allowed_chats"].append(cid_str)
        save_state(state)

    set_bot_commands(token)
    drop_pending_updates(token)

    stop_event = threading.Event()
    scan_thread: Optional[threading.Thread] = None

    def _signal_handler(signum, frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    trade_thread: Optional[threading.Thread] = None
    if RUN_MODE == "bot":
        scan_thread = threading.Thread(target=autoscan_worker, args=(token, chat_id, state, stop_event), daemon=True)
        scan_thread.start()
        # Быстрый монитор торговых ордеров независимо от интервала автоскана
        trade_thread = threading.Thread(target=trade_monitor_worker, args=(token, chat_id, state, stop_event), daemon=True)
        trade_thread.start()

    welcome = (
        "<b>👋 Приветствую в Pump Pulse Scanner 2.1!</b>\n\n"
        "Я сканирую спотовый рынок Binance (USDT-пары) в реальном времени "
        "и мгновенно сообщу о зарождении пампа (Score 58-74+, ДО выстрела).\n"
        "Также умею автоматически торговать на вашем Binance Spot с TP +3%.\n\n"
        "• Выберите действие кнопками ниже\n"
        "• Или напишите: <code>scan</code>, <code>portfolio</code>, <code>trade</code> и т.д."
    )
    send_telegram(token, chat_id, welcome, reply_markup=main_keyboard())

    last_update_id: Optional[int] = None
    user_fsm: Dict[Union[str, int], dict] = {}

    def is_authorized(user_id: Union[str, int]) -> bool:
        if not state.get("allowed_chats"):
            return True
        return str(user_id) in [str(c) for c in state["allowed_chats"]]

    def handle_update(u: dict) -> None:
        nonlocal last_update_id
        last_update_id = u["update_id"]

        if "message" in u:
            msg = u["message"]
            chat = msg.get("chat", {})
            chat_id_local = chat.get("id")
            user_id = msg.get("from", {}).get("id", chat_id_local)
            text = (msg.get("text") or "").strip()

            if not text or not chat_id_local:
                return
            if not is_authorized(user_id):
                print(f"⛔ Игнорирую сообщение от несанкционированного user_id={user_id}")
                return

            if chat_id_local not in [str(c) for c in state.get("allowed_chats", [])] and str(chat_id_local) != str(chat_id):
                state["allowed_chats"].append(str(chat_id_local))
                save_state(state)
                print(f"🔓 Добавлен новый чат: {chat_id_local}")

            text_lower = text.lower()
            if text_lower in ("/start", "start", "старт", "меню", "/menu", "/kb", "kb", "меню", "клавиатура"):
                print(f"-> Показ главного меню для {chat_id_local}")
                send_telegram(token, chat_id_local, "<b>📋 Главное меню Pump Pulse</b>\n\nКлавиатура открыта:", reply_markup=main_keyboard())
                return

            if text_lower in ("/hide", "/скрыть", "/hidekb", "hide", "скрыть", "🙈 скрыть клавиатуру", "скрыть клавиатуру", "скрыть кнопки", "убрать клавиатуру"):
                user_fsm.pop(chat_id_local, None)
                print(f"-> Скрытие клавиатуры для {chat_id_local}")
                send_telegram(
                    token, chat_id_local,
                    "🙈 <b>Клавиатура скрыта.</b>\n\n"
                    "• Чтобы вернуть кнопки в любой момент, отправьте <b>/menu</b> или <b>/start</b>.\n"
                    "• Также все команды доступны через кнопку меню команд <code>[/]</code> слева от поля ввода.",
                    reply_markup={"remove_keyboard": True}
                )
                return

            if text_lower in ("/cancel", "❌ отмена", "отмена", "cancel", "стоп"):
                user_fsm.pop(chat_id_local, None)
                print(f"-> Отмена состояния FSM для {chat_id_local}")
                send_telegram(token, chat_id_local, "✅ Действие отменено. Возврат в главное меню.", reply_markup=main_keyboard())
                return

            # ── FSM: пошаговый ввод ──
            if chat_id_local in user_fsm:
                st = user_fsm[chat_id_local]
                cur_st = st.get("state")

                if cur_st == "waiting_symbol":
                    user_fsm.pop(chat_id_local, None)
                    symbol = normalize_symbol(text)
                    if not symbol.isalnum() or len(symbol) > 20:
                        send_telegram(token, chat_id_local, "❌ Неверный тикер. Попробуйте снова (например, SOL):", reply_markup=cancel_keyboard())
                        return
                    price = get_price(symbol)
                    if price is None:
                        send_telegram(token, chat_id_local, f"❌ Монета <code>{symbol}</code> не найдена на Binance Spot. Попробуйте другой тикер:", reply_markup=cancel_keyboard())
                        return
                    send_telegram(token, chat_id_local, f"✅ Монета: <b>{base_asset(symbol)}/USDT</b>\nТекущая цена: <code>{fmt_price(price)} USDT</code>\n\nВведите <b>количество</b> (например, 10):", reply_markup=cancel_keyboard())
                    user_fsm[chat_id_local] = {"state": "waiting_qty", "data": {"symbol": symbol, "price": price}}
                    return

                if cur_st == "waiting_qty":
                    try:
                        qty = float(text.replace(",", "."))
                        if qty <= 0:
                            raise ValueError()
                    except ValueError:
                        send_telegram(token, chat_id_local, "❌ Неверное количество. Введите число, например 5 или 0.5:", reply_markup=cancel_keyboard())
                        return
                    data = st["data"]
                    symbol = data["symbol"]
                    price = data["price"]
                    new_qty, new_avg = portfolio_add(state, symbol, qty, price)
                    user_fsm.pop(chat_id_local, None)
                    send_telegram(
                        token, chat_id_local,
                        f"✅ <b>{base_asset(symbol)}</b> добавлен в портфель!\n\n"
                        f"• Количество: <code>{fmt_qty(new_qty)}</code>\n"
                        f"• Средняя цена входа: <code>{fmt_price(new_avg)} USDT</code>\n"
                        f"• Текущий PnL: откройте <b>«💼 Портфель»</b>",
                        reply_markup=main_keyboard(),
                    )
                    return

                if cur_st == "waiting_delete_symbol":
                    user_fsm.pop(chat_id_local, None)
                    symbol = normalize_symbol(text)
                    if portfolio_remove(state, symbol):
                        send_telegram(token, chat_id_local, f"✅ <b>{base_asset(symbol)}</b> удалена из портфеля!", reply_markup=main_keyboard())
                    else:
                        send_telegram(token, chat_id_local, f"❌ <b>{base_asset(symbol)}</b> не найдена в портфеле.", reply_markup=main_keyboard())
                    return

                if cur_st == "waiting_api_keys":
                    user_fsm.pop(chat_id_local, None)
                    parts = text.split()
                    if len(parts) >= 2:
                        state["settings"]["binance_api_key"] = sanitize_api_key(parts[0])
                        state["settings"]["binance_api_secret"] = sanitize_api_key(parts[1])
                        save_state(state)
                        send_telegram(token, chat_id_local, "✅ <b>API-ключи Binance сохранены!</b>\n\nТеперь доступны: баланс спота и автоторговля.", reply_markup=main_keyboard())
                    else:
                        send_telegram(token, chat_id_local, "❌ Нужно ввести два значения через пробел:\n<code>/api ВАШ_КЛЮЧ ВАШ_СЕКРЕТ</code>", reply_markup=cancel_keyboard())
                    return

                if cur_st == "waiting_quick_add_qty":
                    try:
                        qty = float(text.replace(",", "."))
                        if qty <= 0:
                            raise ValueError()
                    except ValueError:
                        send_telegram(token, chat_id_local, "❌ Неверное количество. Введите число:", reply_markup=cancel_keyboard())
                        return
                    data = st["data"]
                    symbol = data["symbol"]
                    price = data["price"]
                    new_qty, new_avg = portfolio_add(state, symbol, qty, price)
                    user_fsm.pop(chat_id_local, None)
                    send_telegram(
                        token, chat_id_local,
                        f"✅ <b>{base_asset(symbol)}</b> добавлен в портфель!\n\n"
                        f"• Количество: <code>{fmt_qty(new_qty)}</code>\n"
                        f"• Средняя цена входа: <code>{fmt_price(new_avg)} USDT</code>",
                        reply_markup=main_keyboard(),
                    )
                    return

                if cur_st == "waiting_target_price":
                    data = st.get("data", {})
                    symbol = data.get("symbol", "")
                    user_fsm.pop(chat_id_local, None)
                    set_custom_target_sell(token, chat_id_local, state, symbol, text)
                    return

                if cur_st == "waiting_buy_amount":
                    data = st.get("data", {})
                    sym = data.get("symbol", "")
                    filters = get_symbol_filters(sym)
                    min_notional = float(filters.get("min_notional", 5.0))
                    try:
                        chosen_amt = float(text.replace(",", ".").replace("$", "").strip())
                        if chosen_amt <= 0:
                            raise ValueError()
                    except ValueError:
                        send_telegram(
                            token, chat_id_local,
                            "❌ Неверная сумма. Введите число, например: <code>15</code>",
                            reply_markup=cancel_keyboard(),
                        )
                        return
                    if chosen_amt < min_notional:
                        send_telegram(
                            token, chat_id_local,
                            f"⚠️ <b>Сумма меньше минимума биржи!</b>\n\n"
                            f"• Введено: <code>{chosen_amt:.2f} USDT</code>\n"
                            f"• Минимум на Binance: <code>{min_notional:.0f} USDT</code>\n\n"
                            f"Введите сумму от <code>{min_notional:.0f}</code> USDT:",
                            reply_markup=cancel_keyboard(),
                        )
                        return
                    free_usdt = get_free_usdt_balance(state)
                    if chosen_amt > free_usdt + 0.5:
                        send_telegram(
                            token, chat_id_local,
                            f"❌ Недостаточно средств.\n💰 Доступно: <code>{free_usdt:.2f} USDT</code>",
                            reply_markup=cancel_keyboard(),
                        )
                        return
                    user_fsm.pop(chat_id_local, None)
                    mkt_p = get_price(sym)
                    if mkt_p is None:
                        send_telegram(token, chat_id_local, f"❌ Не удалось получить цену {sym}.", reply_markup=main_keyboard())
                        return
                    base_sym = base_asset(sym)
                    dummy_sig = PumpSignal(
                        symbol=sym,
                        base=base_sym,
                        price=mkt_p,
                        change_24h=0.0,
                        quote_volume_24h=0.0,
                        high_24h=mkt_p,
                        low_24h=mkt_p,
                        btc_relative_24h=0.0,
                        best_tf="manual",
                        best_score=80.0,
                        grade="strong",
                        alert_key=f"manual_{sym}_{int(time.time())}",
                        by_tf=[],
                    )
                    execute_pump_auto_trade(token, chat_id_local, state, dummy_sig, manual_amount=chosen_amt)
                    return


            clean_text = text.lstrip("/")
            cmd = clean_text.split()[0].lower() if clean_text else ""

            is_menu_action = (
                "скан" in text_lower
                or "portfolio" in text_lower
                or "портфел" in text_lower
                or "добав" in text_lower
                or "удал" in text_lower
                or "настрой" in text_lower
                or "баланс" in text_lower
                or "balance" in text_lower
                or "сделк" in text_lower
                or "профит" in text_lower
                or "статистик" in text_lower
                or "trades" in text_lower
                or "stats" in text_lower
                or "помощ" in text_lower
                or "help" in text_lower
                or "меню" in text_lower
            )

            if cmd in ("help", "помощ", "помощь", "info", "инфо", "ℹ️ помощь", "❓ помощь") or (is_menu_action and text_lower in ("help", "помощь", "помощ")):
                print(f"-> Помощь для {chat_id_local}")
                send_telegram(token, chat_id_local, HELP_TEXT, reply_markup=main_keyboard())

            elif cmd in ("settings", "настройки") or (is_menu_action and "настрой" in text_lower):
                print(f"-> Настройки для {chat_id_local}")
                send_telegram(token, chat_id_local, settings_text(state), reply_markup=settings_inline_kb(state))

            elif cmd in ("scan", "скан") or (is_menu_action and "скан" in text_lower):
                print(f"-> Скан для {chat_id_local}")
                execute_scan_and_report(token, chat_id_local, state)

            elif cmd in ("portfolio", "портфель") or (is_menu_action and "портфел" in text_lower):
                print(f"-> Портфель для {chat_id_local}")
                msg_id = send_telegram(token, chat_id_local, "⏳ <i>Загружаю портфель...</i>")
                port_text = format_portfolio(state)
                if msg_id:
                    edit_message(token, chat_id_local, msg_id, port_text, reply_markup=portfolio_inline_kb(state))
                else:
                    send_telegram(token, chat_id_local, port_text, reply_markup=portfolio_inline_kb(state))

            elif cmd in ("trade", "торговля") or (is_menu_action and "торговл" in text_lower):
                print(f"-> Торговля для {chat_id_local}")
                settings = state.get("settings", {})
                api_key, api_secret = get_api_credentials(state)
                api_status = "🟢 Подключены" if (api_key and api_secret) else "🔴 Не заданы"
                trade_amt = settings.get("trade_amount_usdt", DEFAULT_TRADE_AMOUNT)
                tp_pct = settings.get("take_profit_pct", DEFAULT_TAKE_PROFIT)
                active = state.get("active_trades", {})
                history = state.get("trade_history", [])[-5:]
                trade_kb = settings_inline_kb(state)
                lines = [
                    "<b>⚡ Автоторговля Binance Spot</b>\n",
                    f"• API-ключи: {api_status}",
                    f"• Размер ставки: <code>{trade_amt:.1f} USDT</code>",
                    f"• Тейк-профит: <code>+{tp_pct:.1f}%</code>",
                    f"• Открытых сделок: <code>{len(active)}/3</code>\n",
                ]
                if active:
                    lines.append("<b>🎯 Активные сделки:</b>")
                    for sym, tr in active.items():
                        lines.append(
                            f"  • {tr.get('base', base_asset(sym))}/USDT: вход {fmt_price(tr.get('buy_price', 0))} → TP {fmt_price(tr.get('tp_price', 0))} (+{tr.get('tp_pct', 3.0):.1f}%)"
                        )
                    lines.append("")
                if history:
                    lines.append("<b>📜 Последние закрытые:</b>")
                    for h in reversed(history):
                        lines.append(
                            f"  • {h.get('base', '?')}: PnL <b>{h.get('pnl', 0):+.2f} USDT</b> ({h.get('pnl_pct', 0):+.2f}%) — {time.strftime('%d.%m %H:%M', time.localtime(h.get('closed_at', 0)))}"
                        )
                lines.append("\n💡 Настройки — через кнопки «⚙️ Настройки» ниже.")
                send_telegram(token, chat_id_local, "\n".join(lines), reply_markup=trade_kb)

            elif cmd in ("add", "добавить") or (is_menu_action and "добав" in text_lower):
                args = clean_text.split()[1:]
                if args:
                    user_fsm[chat_id_local] = {"state": "waiting_symbol", "data": {}}
                    # если пользователь сразу указал тикер — обрабатываем
                    msg = {"message": {"chat": {"id": chat_id_local}, "from": {"id": user_id}, "text": args[0]}}
                    handle_update(msg)
                    return
                print(f"-> Добавление актива для {chat_id_local}")
                user_fsm[chat_id_local] = {"state": "waiting_symbol", "data": {}}
                send_telegram(
                    token, chat_id_local,
                    "➕ <b>Добавление актива в портфель</b>\n\nВведите тикер монеты (например, <code>SOL</code>, <code>BTC</code> или <code>PEPE</code>):",
                    reply_markup=cancel_keyboard(),
                )

            elif cmd in ("del", "удалить") or (is_menu_action and "удал" in text_lower):
                args = clean_text.split()[1:]
                if args:
                    symbol = normalize_symbol(args[0])
                    if portfolio_remove(state, symbol):
                        send_telegram(token, chat_id_local, f"✅ <b>{base_asset(symbol)}</b> удалена из портфеля!", reply_markup=main_keyboard())
                    else:
                        send_telegram(token, chat_id_local, f"❌ <b>{base_asset(symbol)}</b> не найдена в портфеле.", reply_markup=main_keyboard())
                    return
                print(f"-> Удаление актива для {chat_id_local}")
                kb = remove_asset_inline_kb(state)
                if not kb:
                    send_telegram(token, chat_id_local, "ℹ️ Портфель пуст. Нечего удалять.", reply_markup=main_keyboard())
                    return
                send_telegram(token, chat_id_local, "🗑 <b>Выберите монету для удаления:</b>", reply_markup=kb)

            elif cmd in ("sell", "продать") or (is_menu_action and "прода" in text_lower and "автопрода" not in text_lower):
                print(f"-> Меню досрочной продажи для {chat_id_local}")
                args = clean_text.split()[1:]
                if args:
                    symbol = normalize_symbol(args[0])
                    prompt_text, kb = format_sell_confirmation_prompt(state, symbol)
                    send_telegram(token, chat_id_local, prompt_text, reply_markup=kb)
                else:
                    kb = sell_assets_menu_kb(state)
                    if kb is None:
                        send_telegram(
                            token, chat_id_local,
                            "🔐 <b>Продажа на бирже недоступна: API-ключи Binance не заданы.</b>\n\n"
                            "Привяжите ключи командой <code>/api КЛЮЧ СЕКРЕТ</code> (права: торговля спотом, без вывода).",
                            reply_markup=main_keyboard(),
                        )
                        return
                    prompt_text = (
                        "🔴 <b>ДОСРОЧНАЯ ПРОДАЖА АКТИВОВ (MARKET SELL)</b>\n\n"
                        "Выберите монету ниже для продажи по рыночной цене.\n"
                        "Перед продажей бот покажет <b>окно подтверждения</b> с расчётом PnL и снимет лимитный Take-Profit на бирже."
                    )
                    send_telegram(token, chat_id_local, prompt_text, reply_markup=kb)

            elif cmd in ("targetsell", "tp", "sellat", "цель", "автопродажа") or (is_menu_action and ("автопродаж" in text_lower or "тейк" in text_lower)):
                print(f"-> Настройка автопродажи (TP) для {chat_id_local}")
                args = clean_text.split()[1:]
                if not args:
                    kb = target_sell_assets_menu_kb(state)
                    send_telegram(token, chat_id_local, "🎯 <b>Выберите актив для настройки автопродажи (Take-Profit):</b>", reply_markup=kb)
                elif len(args) == 1:
                    symbol = normalize_symbol(args[0])
                    cur_p = get_price(symbol)
                    if not cur_p:
                        send_telegram(token, chat_id_local, f"❌ Монета <code>{symbol}</code> не найдена.")
                    else:
                        txt, kb = target_sell_coin_presets_kb(symbol, cur_p, state)
                        send_telegram(token, chat_id_local, txt, reply_markup=kb)
                elif len(args) >= 2:
                    symbol = normalize_symbol(args[0])
                    target_val = args[1]
                    set_custom_target_sell(token, chat_id_local, state, symbol, target_val)

            elif cmd in ("api", "ключ"):
                args = clean_text.split()[1:]
                if len(args) >= 2:
                    state["settings"]["binance_api_key"] = sanitize_api_key(args[0])
                    state["settings"]["binance_api_secret"] = sanitize_api_key(args[1])
                    save_state(state)
                    send_telegram(token, chat_id_local, "✅ <b>API-ключи Binance сохранены!</b>", reply_markup=main_keyboard())
                else:
                    print(f"-> Ожидание API ключей для {chat_id_local}")
                    user_fsm[chat_id_local] = {"state": "waiting_api_keys", "data": {}}
                    send_telegram(
                        token, chat_id_local,
                        "🔑 <b>Привязка API ключей Binance</b>\n\n"
                        "Отправьте в одном сообщении через пробел:\n"
                        "<code>/api ВАШ_API_KEY ВАШ_API_SECRET</code>\n\n"
                        "⚠️ <i>Рекомендуется создать ключ с разрешением «Торговля» только на споте, без вывода средств!</i>",
                        reply_markup=cancel_keyboard(),
                    )

            elif "баланс" in text_lower or "balance" in text_lower or cmd in ("balance", "баланс"):
                print(f"-> Детальный баланс для {chat_id_local}")
                msg_id = send_telegram(token, chat_id_local, "⏳ <i>Запрашиваю детальный баланс Binance...</i>")
                bal_text = format_binance_balance_detailed(state)
                if msg_id:
                    edit_message(token, chat_id_local, msg_id, bal_text, reply_markup=portfolio_inline_kb(state))
                else:
                    send_telegram(token, chat_id_local, bal_text, reply_markup=portfolio_inline_kb(state))

            elif (
                "сделк" in text_lower
                or "профит" in text_lower
                or "статистик" in text_lower
                or cmd in ("trades", "stats", "profit", "сделки", "профит", "статистика")
            ):
                print(f"-> Статистика автоторговли для {chat_id_local}")
                msg_id = send_telegram(token, chat_id_local, "⏳ <i>Загружаю статистику сделок и профита...</i>")
                stats_text = format_trades_and_profit_stats(state)
                if msg_id:
                    edit_message(token, chat_id_local, msg_id, stats_text, reply_markup=portfolio_inline_kb(state))
                else:
                    send_telegram(token, chat_id_local, stats_text, reply_markup=portfolio_inline_kb(state))

            elif text_lower in ("ping", "пинг"):
                send_telegram(token, chat_id_local, "🏓 <b>pong</b> — бот на связи!")

            else:
                # Эхо-подсказка для нераспознанных команд
                hint = (
                    "🤔 Не распознал команду.\n\n"
                    "Попробуйте:\n"
                    "• <code>scan</code> — сканер пампов\n"
                    "• <code>portfolio</code> — портфель\n"
                    "• <code>trade</code> — автоторговля\n"
                    "• или кнопки меню ниже ⬇️"
                )
                send_telegram(token, chat_id_local, hint, reply_markup=main_keyboard())

        elif "callback_query" in u:
            cb = u["callback_query"]
            cb_id = cb["id"]
            cb_chat = cb.get("message", {}).get("chat", {}).get("id")
            cb_user = cb.get("from", {}).get("id", cb_chat)
            msg_id = cb.get("message", {}).get("message_id")
            cb_data = cb.get("data", "")

            if not cb_chat:
                answer_callback(token, cb_id, "Ошибка данных")
                return
            if not is_authorized(cb_user):
                answer_callback(token, cb_id, "⛔ Нет доступа")
                return

            print(f"🔘 Callback от {cb_user}: {cb_data}")
            settings = state["settings"]

            if cb_data == "port:refresh":
                answer_callback(token, cb_id, "Обновляю цены...")
                port_text = format_portfolio(state)
                if msg_id:
                    edit_message(token, cb_chat, msg_id, port_text, reply_markup=portfolio_inline_kb(state))
                else:
                    send_telegram(token, cb_chat, port_text, reply_markup=portfolio_inline_kb(state))

            elif cb_data == "port:trades_stats":
                answer_callback(token, cb_id, "Статистика сделок...")
                stats_text = format_trades_and_profit_stats(state)
                if msg_id:
                    edit_message(token, cb_chat, msg_id, stats_text, reply_markup=portfolio_inline_kb(state))
                else:
                    send_telegram(token, cb_chat, stats_text, reply_markup=portfolio_inline_kb(state))

            elif cb_data == "port:charts":
                answer_callback(token, cb_id)
                kb = portfolio_charts_inline_kb(state)
                chart_text = (
                    "<b>📈 Графики активов портфеля:</b>\n\n"
                    "Выберите монету ниже, чтобы открыть живой график на TradingView или спотовый терминал Binance Spot:"
                )
                if msg_id:
                    edit_message(token, cb_chat, msg_id, chart_text, reply_markup=kb)
                else:
                    send_telegram(token, cb_chat, chart_text, reply_markup=kb)

            elif cb_data == "port:add":
                answer_callback(token, cb_id)
                user_fsm[cb_chat] = {"state": "waiting_symbol", "data": {}}
                send_telegram(token, cb_chat, "➕ Введите тикер монеты (например, <code>SOL</code>):", reply_markup=cancel_keyboard())

            elif cb_data == "port:del":
                answer_callback(token, cb_id)
                kb = remove_asset_inline_kb(state)
                if not kb:
                    send_telegram(token, cb_chat, "ℹ️ Портфель пуст.", reply_markup=main_keyboard())
                    return
                send_telegram(token, cb_chat, "🗑 <b>Выберите монету для удаления:</b>", reply_markup=kb)

            elif cb_data.startswith("del:"):
                symbol = cb_data[4:]
                answer_callback(token, cb_id)
                if portfolio_remove(state, symbol):
                    send_telegram(token, cb_chat, f"✅ <b>{base_asset(symbol)}</b> удалена из портфеля!", reply_markup=main_keyboard())
                else:
                    send_telegram(token, cb_chat, f"❌ <b>{base_asset(symbol)}</b> не найдена.", reply_markup=main_keyboard())

            elif cb_data == "port:sell_menu":
                answer_callback(token, cb_id, "Меню продажи...")
                kb = sell_assets_menu_kb(state)
                if kb is None:
                    send_telegram(
                        token, cb_chat,
                        "🔐 <b>Продажа на бирже недоступна: API-ключи Binance не заданы.</b>\n\n"
                        "Привяжите ключи командой <code>/api КЛЮЧ СЕКРЕТ</code>.",
                        reply_markup=main_keyboard(),
                    )
                    return
                prompt_text = (
                    "🔴 <b>ДОСРОЧНАЯ ПРОДАЖА АКТИВОВ (MARKET SELL)</b>\n\n"
                    "Выберите монету ниже для продажи по рыночной цене.\n"
                    "Перед продажей бот покажет <b>окно подтверждения</b> с расчётом PnL и снимет лимитный Take-Profit на бирже."
                )
                if msg_id:
                    edit_message(token, cb_chat, msg_id, prompt_text, reply_markup=kb)
                else:
                    send_telegram(token, cb_chat, prompt_text, reply_markup=kb)

            elif cb_data.startswith("sell_prompt:"):
                sym = cb_data.split(":", 1)[1]
                answer_callback(token, cb_id)
                prompt_text, kb = format_sell_confirmation_prompt(state, sym)
                if msg_id:
                    edit_message(token, cb_chat, msg_id, prompt_text, reply_markup=kb)
                else:
                    send_telegram(token, cb_chat, prompt_text, reply_markup=kb)

            elif cb_data.startswith("sell_confirm:"):
                sym = cb_data.split(":", 1)[1]
                answer_callback(token, cb_id, f"Продаю {base_asset(sym)} по рынку...")
                if msg_id:
                    edit_message(token, cb_chat, msg_id, f"⏳ <i>Закрываю позицию {base_asset(sym)}/USDT на Binance...</i>")
                execute_emergency_market_sell(token, cb_chat, state, sym)

            elif cb_data == "port:targetsell_menu":
                answer_callback(token, cb_id, "Меню автопродажи...")
                kb = target_sell_assets_menu_kb(state)
                txt = "🎯 <b>ВЫБОР МОНЕТЫ ДЛЯ НАСТРОЙКИ АВТОПРОДАЖИ (Take-Profit)</b>\n\nВыберите монету из портфеля ниже, чтобы установить целевую цену продажи:"
                if msg_id:
                    edit_message(token, cb_chat, msg_id, txt, reply_markup=kb)
                else:
                    send_telegram(token, cb_chat, txt, reply_markup=kb)

            elif cb_data.startswith("target_pick:"):
                sym = cb_data.split(":", 1)[1]
                cur_p = get_price(sym)
                if not cur_p:
                    answer_callback(token, cb_id, "Ошибка получения цены")
                    return
                answer_callback(token, cb_id)
                txt, kb = target_sell_coin_presets_kb(sym, cur_p, state)
                if msg_id:
                    edit_message(token, cb_chat, msg_id, txt, reply_markup=kb)
                else:
                    send_telegram(token, cb_chat, txt, reply_markup=kb)

            elif cb_data.startswith("target_preset:"):
                parts = cb_data.split(":")
                sym = parts[1]
                pct = parts[2]
                answer_callback(token, cb_id, f"Выставляю ордер +{pct}%...")
                if msg_id:
                    edit_message(token, cb_chat, msg_id, f"⏳ <i>Размещаю лимитный ордер автопродажи +{pct}% на бирже...</i>")
                set_custom_target_sell(token, cb_chat, state, sym, f"+{pct}%")

            elif cb_data.startswith("target_custom:"):
                sym = cb_data.split(":", 1)[1]
                answer_callback(token, cb_id)
                cur_p = get_price(sym) or 0.0
                user_fsm[cb_chat] = {"state": "waiting_target_price", "data": {"symbol": sym}}
                prompt = (
                    f"🎯 <b>Ручной ввод целевой цены для {base_asset(sym)}/USDT</b>\n\n"
                    f"Текущая рыночная цена: <code>{fmt_price(cur_p)} $</code>\n\n"
                    f"Введите целевую цену (например: <code>{fmt_price(cur_p * 1.05)}</code>) или процент (например: <code>+7.5%</code>):"
                )
                send_telegram(token, cb_chat, prompt, reply_markup=cancel_keyboard())

            elif cb_data in ("menu:main", "menu:show_kb"):
                answer_callback(token, cb_id, "Главное меню")
                send_telegram(token, cb_chat, "<b>📋 Главное меню Pump Pulse</b>\n\nКлавиатура открыта:", reply_markup=main_keyboard())

            elif cb_data == "menu:settings":
                answer_callback(token, cb_id, "Настройки")
                if msg_id:
                    edit_message(token, cb_chat, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))
                else:
                    send_telegram(token, cb_chat, settings_text(state), reply_markup=settings_inline_kb(state))

            elif cb_data == "autoscan:toggle":
                settings["autoscan"] = not settings.get("autoscan", True)
                save_state(state)
                status = "включён 🟢" if settings["autoscan"] else "выключен 🔴"
                answer_callback(token, cb_id, f"Автоскан {status}")
                if msg_id:
                    edit_message(token, cb_chat, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))

            elif cb_data.startswith("interval:"):
                sec = int(cb_data.split(":")[1])
                settings["scan_interval_sec"] = sec
                save_state(state)
                answer_callback(token, cb_id, f"Интервал: {sec // 60} мин")
                if msg_id:
                    edit_message(token, cb_chat, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))

            elif cb_data.startswith("score:set:"):
                val = float(cb_data.split(":")[2])
                settings["min_score"] = val
                save_state(state)
                answer_callback(token, cb_id, f"Порог Score: {val:.0f}")
                if msg_id:
                    edit_message(token, cb_chat, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))

            elif cb_data.startswith("score:"):
                delta = float(cb_data.split(":")[1])
                settings["min_score"] = max(30.0, min(95.0, settings.get("min_score", DEFAULT_MIN_SCORE) + delta))
                save_state(state)
                answer_callback(token, cb_id, f"Порог Score: {settings['min_score']:.0f}")
                if msg_id:
                    edit_message(token, cb_chat, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))

            elif cb_data == "filter:toggle":
                settings["filter_level"] = "strong_only" if settings.get("filter_level") == "strong_and_watch" else "strong_and_watch"
                save_state(state)
                lvl = "Только Strong 🔥" if settings["filter_level"] == "strong_only" else "Strong + Watch ⚡"
                answer_callback(token, cb_id, f"Фильтр: {lvl}")
                if msg_id:
                    edit_message(token, cb_chat, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))

            elif cb_data == "settings:refresh":
                answer_callback(token, cb_id, "Настройки обновлены")
                if msg_id:
                    edit_message(token, cb_chat, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))

            # ── Автоторговля ──
            elif cb_data == "trade:toggle":
                settings["auto_trade"] = not settings.get("auto_trade", False)
                save_state(state)
                st_str = "включена ⚡" if settings["auto_trade"] else "выключена"
                answer_callback(token, cb_id, f"Автоторговля {st_str}")
                if msg_id:
                    edit_message(token, cb_chat, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))

            elif cb_data.startswith("trade:amt:"):
                mode_val = cb_data[len("trade:amt:"):]  # "all", "pct:10", etc.
                settings["trade_mode"] = mode_val
                save_state(state)
                if mode_val == "all":
                    answer_callback(token, cb_id, "Режим: весь свободный баланс USDT")
                elif mode_val.startswith("pct:"):
                    pct = mode_val.split(":")[1]
                    answer_callback(token, cb_id, f"Режим: {pct}% от баланса USDT")
                else:
                    # legacy fixed number
                    try:
                        amt = float(mode_val)
                        settings["trade_amount_usdt"] = amt
                        answer_callback(token, cb_id, f"Ставка: {amt:.0f} USDT")
                    except ValueError:
                        answer_callback(token, cb_id, "Режим обновлён")
                if msg_id:
                    edit_message(token, cb_chat, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))

            elif cb_data.startswith("trade:entry:"):
                pb = float(cb_data.split(":", 2)[2])
                settings["entry_pullback_pct"] = pb
                save_state(state)
                lbl = "По рынку (мгновенно)" if pb <= 0.001 else f"Откат -{pb:.1f}%"
                answer_callback(token, cb_id, f"Вход: {lbl}")
                if msg_id:
                    edit_message(token, cb_chat, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))

            elif cb_data.startswith("trade:tp:"):
                tp = float(cb_data.split(":", 2)[2])
                settings["take_profit_pct"] = tp
                save_state(state)
                answer_callback(token, cb_id, f"Take-Profit: +{tp:.1f}%")
                if msg_id:
                    edit_message(token, cb_chat, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))

            elif cb_data.startswith("trade:sl:"):
                sl = float(cb_data.split(":", 2)[2])
                settings["stop_loss_pct"] = sl
                save_state(state)
                answer_callback(token, cb_id, f"Stop-Loss: -{sl:.1f}%")
                if msg_id:
                    edit_message(token, cb_chat, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))

            elif cb_data == "trade:dyn_tp:toggle":
                settings["dynamic_tp"] = not settings.get("dynamic_tp", DEFAULT_DYNAMIC_TP)
                save_state(state)
                st = "включён 🧮" if settings["dynamic_tp"] else "выключен (фиксированный)"
                answer_callback(token, cb_id, f"Умный ATR TP {st}")
                if msg_id:
                    edit_message(token, cb_chat, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))

            elif cb_data == "trade:balance":
                answer_callback(token, cb_id, "Запрашиваю детальный баланс...")
                bal_text = format_binance_balance_detailed(state)
                if msg_id:
                    edit_message(token, cb_chat, msg_id, bal_text, reply_markup=portfolio_inline_kb(state))
                else:
                    send_telegram(token, cb_chat, bal_text, reply_markup=portfolio_inline_kb(state))

            elif cb_data.startswith("trade_buy:"):
                sym = cb_data.split(":", 1)[1]
                base_sym = base_asset(sym)
                mkt_p = get_price(sym)
                if mkt_p is None:
                    answer_callback(token, cb_id, "❌ Не удалось получить цену")
                    send_telegram(token, cb_chat, f"❌ Не удалось получить цену {sym}.")
                    return
                answer_callback(token, cb_id, f"Выберите сумму для {base_sym}")
                free_usdt = get_free_usdt_balance(state)
                default_amt = float(settings.get("trade_amount_usdt", DEFAULT_TRADE_AMOUNT))
                filters = get_symbol_filters(sym)
                min_notional = float(filters.get("min_notional", 5.0))

                low_balance_warn = ""
                if free_usdt < min_notional:
                    low_balance_warn = (
                        f"\n⚠️ <i>Баланс ({free_usdt:.2f} USDT) меньше минимума ордера Binance ({min_notional:.0f} USDT). "
                        f"Пополните баланс спота для покупки.</i>\n"
                    )

                prompt = (
                    f"⚡ <b>Купить {base_sym}/USDT</b>\n\n"
                    f"💵 Текущая цена: <code>{fmt_price(mkt_p)} USDT</code>\n"
                    f"💰 Свободный баланс: <code>{free_usdt:.2f} USDT</code>\n"
                    f"⚙️ Сумма по умолчанию: <code>{default_amt:.0f}$</code>\n"
                    f"📏 Мин. ордер Binance: <code>{min_notional:.0f} USDT</code>\n"
                    f"{low_balance_warn}\n"
                    f"Выберите сумму покупки:"
                )
                kb = trade_buy_amount_kb(sym, state)
                if msg_id:
                    edit_message(token, cb_chat, msg_id, prompt, reply_markup=kb)
                else:
                    send_telegram(token, cb_chat, prompt, reply_markup=kb)

            elif cb_data.startswith("trade_buy_amt:"):
                # trade_buy_amt:SYMBOL:AMOUNT  (AMOUNT = число или "all")
                parts = cb_data.split(":", 2)
                sym = parts[1]
                amt_str = parts[2] if len(parts) > 2 else "all"
                base_sym = base_asset(sym)
                filters = get_symbol_filters(sym)
                min_notional = float(filters.get("min_notional", 5.0))
                free_usdt = get_free_usdt_balance(state)
                if amt_str == "all":
                    chosen_amt = max(0.0, free_usdt - 0.01)
                else:
                    chosen_amt = float(amt_str)
                if chosen_amt < min_notional:
                    answer_callback(token, cb_id, f"❌ Минимум {min_notional:.0f}$ (у вас {free_usdt:.2f}$)", show_alert=True)
                    send_telegram(
                        token, cb_chat,
                        f"⚠️ <b>Сумма покупки меньше минимума биржи!</b>\n\n"
                        f"• Выбранная сумма: <code>{chosen_amt:.2f} USDT</code>\n"
                        f"• Минимальный ордер Binance: <code>{min_notional:.1f} USDT</code>\n"
                        f"• Свободно на споте: <code>{free_usdt:.2f} USDT</code>\n\n"
                        f"💡 <i>Пополните баланс USDT на Binance хотя бы до {min_notional:.0f} USDT.</i>",
                        reply_markup=main_keyboard(),
                    )
                    return
                answer_callback(token, cb_id, f"Покупаю {base_sym} на {chosen_amt:.1f}$...")
                mkt_p = get_price(sym)
                if mkt_p is None:
                    send_telegram(token, cb_chat, f"❌ Не удалось получить цену {sym}.")
                    return
                dummy_sig = PumpSignal(
                    symbol=sym,
                    base=base_sym,
                    price=mkt_p,
                    change_24h=0.0,
                    quote_volume_24h=0.0,
                    high_24h=mkt_p,
                    low_24h=mkt_p,
                    btc_relative_24h=0.0,
                    best_tf="manual",
                    best_score=80.0,
                    grade="strong",
                    alert_key=f"manual_{sym}_{int(time.time())}",
                    by_tf=[],
                )
                execute_pump_auto_trade(token, cb_chat, state, dummy_sig, manual_amount=chosen_amt)

            elif cb_data.startswith("trade_buy_custom_amt:"):
                sym = cb_data.split(":", 1)[1]
                base_sym = base_asset(sym)
                filters = get_symbol_filters(sym)
                min_notional = float(filters.get("min_notional", 5.0))
                answer_callback(token, cb_id, "Введите сумму вручную")
                user_fsm[cb_chat] = {"state": "waiting_buy_amount", "data": {"symbol": sym}}
                free_usdt = get_free_usdt_balance(state)
                send_telegram(
                    token, cb_chat,
                    f"✍️ <b>Введите сумму в USDT</b> для покупки <b>{base_sym}</b>:\n\n"
                    f"💰 Доступно: <code>{free_usdt:.2f} USDT</code>\n"
                    f"📏 Минимум Binance: <code>{min_notional:.0f} USDT</code>\n\n"
                    f"<i>Напишите число (от {min_notional:.0f} USDT), например: <code>15</code></i>",
                    reply_markup=cancel_keyboard(),
                )

            elif cb_data.startswith("add_coin:"):
                parts = cb_data.split(":")
                symbol = parts[1]
                price = float(parts[2]) if len(parts) > 2 else 0.0
                answer_callback(token, cb_id)
                if price <= 0:
                    price = get_price(symbol) or 0.0
                send_telegram(
                    token, cb_chat,
                    f"➕ <b>Добавление {base_asset(symbol)}</b>\n\n"
                    f"Текущая цена: <code>{fmt_price(price)} USDT</code>\n\n"
                    f"Введите <b>количество</b> монет, которое у вас есть (например, 10):",
                    reply_markup=cancel_keyboard(),
                )
                user_fsm[cb_chat] = {"state": "waiting_quick_add_qty", "data": {"symbol": symbol, "price": price}}

            elif cb_data.startswith("factors:"):
                parts = cb_data.split(":")
                symbol = parts[1]
                tf = parts[2] if len(parts) > 2 else "5m"
                answer_callback(token, cb_id)
                # Восстанавливаем сигнал для разбора факторов
                tickers = get_24h_tickers()
                ticker = next((t for t in tickers if t["symbol"] == symbol), None)
                if not ticker:
                    send_telegram(token, cb_chat, f"❌ Монета {symbol} не найдена.")
                    return
                btc_raw = fetch_klines("BTCUSDT")
                raw = fetch_klines(symbol)
                btc_ticker = next((t for t in tickers if t["symbol"] == "BTCUSDT"), None)
                sig, _ = analyze_symbol(ticker, raw, btc_raw, btc_ticker, min_score=0.0)
                if not sig:
                    send_telegram(token, cb_chat, "❌ Не удалось рассчитать факторы.")
                    return
                send_telegram(token, cb_chat, format_factor_breakdown(sig, tf))

    # ── Polling loop ──
    print("🤖 Бот запущен и слушает сообщения...")
    while not stop_event.is_set():
        try:
            updates = get_updates(token, (last_update_id + 1) if last_update_id is not None else None, timeout=40)
            for u in updates.get("result", []):
                try:
                    handle_update(u)
                except Exception as e:
                    print(f"❌ Ошибка обработки апдейта: {e}", file=sys.stderr)
                    traceback.print_exc()
        except Exception as e:
            if not stop_event.is_set():
                print(f"⚠️ Ошибка получения обновлений: {e}. Пауза 5с.", file=sys.stderr)
                time.sleep(5)

    print("👋 Бот остановлен.")
    if scan_thread:
        scan_thread.join(timeout=2)
    if trade_thread:
        trade_thread.join(timeout=2)

# ───────────────────────── Тесты и CLI ─────────────────────────

def run_test_connection() -> None:
    print("=== Тест соединения с Binance ===")
    try:
        tickers = get_24h_tickers()
        print(f"✅ USDT-спот пар получено: {len(tickers)}")
        btc = next((t for t in tickers if t["symbol"] == "BTCUSDT"), None)
        if btc:
            print(f"✅ BTC: {fmt_price(btc['lastPrice'])} USDT (24ч: {fmt_pct(btc['priceChangePercent'])})")
        raw = fetch_klines("BTCUSDT", limit=3)
        print(f"✅ Klines BTCUSDT: {len(raw)} баров")
        print("\n🎉 Все системные проверки пройдены успешно!")
    except Exception as e:
        print(f"❌ Ошибка: {e}", file=sys.stderr)
        sys.exit(1)

def run_test_trade() -> None:
    print("=== Тест API ключей и размещения сделки (11 USDT на BTCUSDT) ===")
    state = load_state()
    api_key, api_secret = get_api_credentials(state)
    if not api_key or not api_secret:
        print("❌ API-ключи не настроены. Укажите BINANCE_API_KEY и BINANCE_API_SECRET в .env", file=sys.stderr)
        sys.exit(1)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or None
    dummy = PumpSignal(
        symbol="BTCUSDT", base="BTC", price=get_price("BTCUSDT") or 0.0,
        change_24h=0.0, quote_volume_24h=0.0, high_24h=0.0, low_24h=0.0,
        btc_relative_24h=0.0, best_tf="test", best_score=80.0, grade="strong",
        alert_key="test", by_tf=[],
    )
    res = execute_pump_auto_trade(token, chat_id or "", state, dummy, manual_amount=11.0)
    if res and "error" not in res:
        print("✅ Тестовая сделка выполнена!")
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(f"❌ Ошибка: {res.get('error') if res else 'unknown'}", file=sys.stderr)
        sys.exit(1)

def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if "--test" in sys.argv:
        run_test_connection()
        return
    if "--test-trade" in sys.argv:
        run_test_trade()
        return

    if RUN_MODE == "oneshot" or "--oneshot" in sys.argv:
        if not token or not chat_id:
            print("⚠️ TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы — вывожу в консоль.", file=sys.stderr)
        run_oneshot(token, chat_id)
        return

    if not token or not chat_id:
        print("❌ Укажите TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env или переменных окружения.", file=sys.stderr)
        sys.exit(1)

    try:
        run_bot(token, chat_id)
    except KeyboardInterrupt:
        print("\n👋 Остановлено пользователем.")

if __name__ == "__main__":
    main()
