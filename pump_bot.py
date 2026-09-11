#!/usr/bin/env python3
"""
Pump Pulse Scanner (Telegram Bot 2.1) + Whale Scan (режим «Скан действий китов»).

Сканирует USDT-спот на Binance, считает multi-TF score
(объём, breakout, ATR, RSI, EMA, taker buy, vs BTC, ускорение)
и предоставляет полнофункциональный Telegram-интерфейс:
  - Интерактивное меню и Reply-кнопки
  - Фоновый автоскан рынка в реальном времени с защитой от дублирования
  - Режим «🐋 Скан действий китов»: крупные принты, чистый поток, стены, OI
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

# ───────────────────────── Whale Scan (совместный режим) ─────────────────────────
# Модуль лежит рядом в репозитории (whale_scan.py). Если его нет — бот работает
# как раньше, без режима китов (безопасная деградация).
try:
    import whale_scan as ws
    WHALE_MODULE_OK = True
except Exception as _whale_import_err:
    ws = None  # type: ignore[assignment]
    WHALE_MODULE_OK = False
    print(f"⚠️ Модуль whale_scan.py недоступен, режим «Скан китов» отключён: {_whale_import_err}", file=sys.stderr)

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
DEFAULT_TAKE_PROFIT = float(os.environ.get("TAKE_PROFIT_PCT", "3.0"))
DEFAULT_AUTO_TRADE = os.environ.get("AUTO_TRADE", "false").strip().lower() in ("true", "1")
DEFAULT_TRADE_MIN_SCORE = float(os.environ.get("TRADE_MIN_SCORE", "70.0"))

# Параметры режима «Скан действий китов»
DEFAULT_WHALE_MIN_SCORE = float(os.environ.get("WHALE_MIN_SCORE", "60"))
DEFAULT_WHALE_AUTOSCAN = os.environ.get("WHALE_AUTOSCAN", "true").strip().lower() in ("true", "1")
DEFAULT_WHALE_TOP_N = int(os.environ.get("WHALE_TOP_N", "30"))

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

    # ── ФАКТОРЫ СКОРИНГА В ЗАРОДЫШЕ (EARLY PUMP ENGINE) ──
    # 1. Всплеск объема на покупку (25б): ищем от 2.0x до 5.0x
    vol_factor = clamp((vol_ratio - 1.2) / 3.0, 0.0, 1.0)

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
        or not bullish  # Исключаем красные свечи (дампы)
    )

    factors = [
        FactorScore("vol", "Всплеск объёма", 25, vol_factor, f"{vol_ratio:.1f}× SMA20"),
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
            # ── Режим «Скан действий китов» ──
            "whale_autoscan": DEFAULT_WHALE_AUTOSCAN,
            "whale_min_score": DEFAULT_WHALE_MIN_SCORE,
        },
        "active_trades": {},      # symbol -> trade dict
        "trade_history": [],      # list of closed trades
        "sent_alerts": {},        # pump alert_key -> timestamp
        "sent_whale_alerts": {},  # whale alert_key -> timestamp
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
        sent_w = data.get("sent_whale_alerts", {})
        d["sent_whale_alerts"] = sent_w if isinstance(sent_w, dict) else {}
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
        state["sent_whale_alerts"] = {k: ts for k, ts in state["sent_whale_alerts"].items() if ts > cutoff}

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

def get_api_credentials(state: Optional[dict] = None) -> Tuple[str, str]:
    """Возвращает (api_key, api_secret) с приоритетом настроек бота над переменными окружения."""
    key = ""
    secret = ""
    if state:
        key = state.get("settings", {}).get("binance_api_key", "").strip()
        secret = state.get("settings", {}).get("binance_api_secret", "").strip()
    if not key:
        key = os.environ.get("BINANCE_API_KEY", "").strip()
    if not secret:
        secret = os.environ.get("BINANCE_API_SECRET", "").strip()
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
    - Итоговый капитал всего портфеля в USDT
    """
    api_key, api_secret = get_api_credentials(state)
    if not api_key or not api_secret:
        return (
            "⚠️ <b>API-ключи Binance не настроены.</b>\n\n"
            "Чтобы просматривать баланс и совершать сделки, привяжите ключи командой:\n"
            "<code>/api ВАШ_KEY ВАШ_SECRET</code>\n"
            "или через меню ⚙️ Настройки."
        )

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

    lines = ["💳 <b>Баланс и активы на Binance Spot</b>\n"]

    # Блок USDT
    lines.append("💵 <b>Стейблкоин баланс (USDT):</b>")
    lines.append(f"• <b>Свободно:</b> <code>{usdt_free:,.2f} USDT</code>")
    if usdt_locked > 0.001:
        lines.append(f"• <b>В ордерах покупки:</b> <code>{usdt_locked:,.2f} USDT</code>")
    lines.append(f"• <b>Всего USDT:</b> <code>{usdt_total:,.2f} USDT</code>\n")

    # Блок криптовалютных активов
    total_crypto_value = 0.0
    items_lines = []

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
            lock_str = f" <i>(в TP-ордерах: {fmt_qty(lock_qty)})</i>" if lock_qty > 0.000001 else ""
            tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{pair}"
            binance_url = f"https://www.binance.com/en/trade/{asset}_USDT?type=spot"
            items_lines.append(
                f"• <b>{asset}</b>: <code>{fmt_qty(tot_qty)} {asset}</code> × <code>{fmt_price(price)} $</code> = <b>{val_usdt:,.2f} USDT</b>{lock_str}\n"
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
    trade_amt = float(manual_amount or settings.get("trade_amount_usdt", DEFAULT_TRADE_AMOUNT))
    tp_pct = float(settings.get("take_profit_pct", DEFAULT_TAKE_PROFIT))
    max_trades = int(settings.get("max_open_trades", 3))

    active_trades = state.setdefault("active_trades", {})

    # Проверка на повторный вход в ту же монету
    if sig.symbol in active_trades:
        print(f"[AutoTrade] Пропуск {sig.symbol}: уже есть активная сделка")
        return {"error": f"По монете {sig.symbol} уже есть открытая позиция"}

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

    # Проверка свободного баланса USDT
    free_usdt = get_free_usdt_balance(state)
    if free_usdt < trade_amt:
        msg = (
            f"⚠️ <b>Автоторговля: Недостаточно USDT для входа в {sig.base}!</b>\n\n"
            f"• Требуется: <code>{trade_amt:.2f} USDT</code>\n"
            f"• Свободно на споте: <code>{free_usdt:.2f} USDT</code>\n\n"
            f"💡 Пополните баланс USDT на Binance или уменьшите ставку в настройках."
        )
        send_telegram(token, chat_id, msg)
        return {"error": "Insufficient USDT balance"}

    # Получение фильтров торговой пары
    filters = get_symbol_filters(sig.symbol)
    if filters.get("status") != "TRADING":
        return {"error": f"Пара {sig.symbol} временно не торгуется на бирже"}

    # 1. Размещение MARKET BUY ордера
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

    # 2. Расчет цены и объема Take-Profit (+3%)
    tp_raw_price = avg_buy_price * (1.0 + (tp_pct / 100.0))
    step_size = filters.get("step_size", 1.0)
    tick_size = filters.get("tick_size", 0.01)

    tp_price_str = fmt_price_filter(tp_raw_price, tick_size)
    tp_qty_str = fmt_qty_filter(exec_qty, step_size)

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

    # 4. Сохранение сделки в активные и в портфель
    trade_record = {
        "symbol": sig.symbol,
        "base": sig.base,
        "buy_order_id": buy_order_id,
        "tp_order_id": tp_order_id,
        "buy_price": avg_buy_price,
        "tp_price": float(tp_price_str),
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

    trade_alert = (
        f"⚡ <b>СДЕЛКА ИСПОЛНЕНА НА BINANCE SPOT!</b>\n\n"
        f"🟢 <b>Покупка:</b> <b>{sig.base}/USDT</b>\n"
        f"• Потрачено: <code>{cum_quote:.2f} USDT</code>\n"
        f"• Куплено: <code>{fmt_qty(float(sell_params['quantity']))} {sig.base}</code>\n"
        f"• Цена исполнения: <code>{fmt_price(avg_buy_price)} USDT</code>\n"
        f"• Импульс Score: <b>{sig.best_score:.1f}</b> ({sig.grade.upper()})\n\n"
        f"🎯 <b>Тейк-профит (+{tp_pct:.1f}%):</b>\n"
        f"• Цена продажи: <code>{tp_price_str} USDT</code>\n"
        f"{tp_status_note}"
    )
    send_telegram(token, chat_id, trade_alert)
    return trade_record

def check_active_trades(token: str, chat_id: Union[str, int], state: dict) -> None:
    """
    Проверяет исполнение выставленных тейк-профит ордеров на Binance.
    При исполнении фиксирует прибыль и возвращает средства в USDT.
    """
    active_trades = state.get("active_trades", {})
    if not active_trades:
        return

    symbols_to_remove = []
    updated = False

    for symbol, trade in list(active_trades.items()):
        tp_order_id = trade.get("tp_order_id")
        if not tp_order_id:
            continue

        res = binance_signed_request(
            "GET",
            "/api/v3/order",
            {"symbol": symbol, "orderId": tp_order_id},
            state=state,
        )
        if "error" in res:
            continue

        status = res.get("status")
        if status == "FILLED":
            symbols_to_remove.append(symbol)
            updated = True

            cum_quote = float(res.get("cummulativeQuoteQty", trade.get("qty", 0) * trade.get("tp_price", 0)))
            cost = float(trade.get("cost_usdt", 0))
            pnl = cum_quote - cost
            pnl_pct = (pnl / cost * 100.0) if cost > 0 else trade.get("tp_pct", 3.0)

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

    for sym in symbols_to_remove:
        active_trades.pop(sym, None)

    if updated:
        save_state(state, sync_git=True)

def format_portfolio(state: dict) -> str:
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

# ───────────────────────── Клавиатуры ─────────────────────────

def main_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "🔍 Скан сейчас"}, {"text": "🐋 Скан китов"}],
            [{"text": "💼 Портфель"}, {"text": "➕ Добавить актив"}],
            [{"text": "🗑 Удалить актив"}, {"text": "⚙️ Настройки"}],
            [{"text": "💳 Баланс Binance"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }

def cancel_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "❌ Отмена"}],
            [{"text": "🔍 Скан сейчас"}, {"text": "🐋 Скан китов"}],
            [{"text": "💼 Портфель"}, {"text": "⚙️ Настройки"}],
            [{"text": "💳 Баланс Binance"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }

def portfolio_inline_kb() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "🔄 Обновить цены", "callback_data": "port:refresh"},
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
        ]
    }

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
    auto_trade_label = "🟢 Включена (+3% TP)" if s.get("auto_trade", False) else "🔴 Выключена"
    whale_scan_label = "🟢 Включён" if s.get("whale_autoscan", DEFAULT_WHALE_AUTOSCAN) else "🔴 Выключен"
    interval_m = s.get("scan_interval_sec", DEFAULT_SCAN_INTERVAL) // 60
    trade_amt = s.get("trade_amount_usdt", DEFAULT_TRADE_AMOUNT)
    tp_pct = s.get("take_profit_pct", DEFAULT_TAKE_PROFIT)

    api_key, api_secret = get_api_credentials(state)
    api_status = "🟢 Подключены" if (api_key and api_secret) else "⚪ Не заданы (только сигналы)"

    return (
        "<b>⚙️ Параметры бота Pump Pulse</b>\n\n"
        f"• <b>Автосканирование рынка:</b> {auto_scan_label} (каждые {interval_m} мин)\n"
        f"• <b>Порог Score (MIN_SCORE):</b> <code>{s['min_score']:.0f}</code>\n"
        f"• <b>Уведомления:</b> <code>{filt_label}</code>\n\n"
        "<b>⚡ Спотовая торговля Binance:</b>\n"
        f"• <b>Автоторговля пампов:</b> {auto_trade_label}\n"
        f"• <b>Размер покупки:</b> <code>{trade_amt:.1f} USDT</code>\n"
        f"• <b>Тейк-профит (TP):</b> <code>+{tp_pct:.1f}%</code> (выставляется сразу)\n"
        f"• <b>Статус API Binance:</b> {api_status}\n\n"
        "<b>🐋 Скан действий китов:</b>\n"
        f"• <b>Автоскан китов:</b> {whale_scan_label}\n"
        f"• <b>Порог Whale Score:</b> <code>{s.get('whale_min_score', DEFAULT_WHALE_MIN_SCORE):.0f}</code>\n\n"
        "<i>Используйте кнопки ниже для быстрой настройки:</i>"
    )

def settings_inline_kb(state: dict) -> dict:
    s = state["settings"]
    autoscan_label = "🔴 Отключить автоскан" if s.get("autoscan", True) else "🟢 Включить автоскан"
    filter_label = "🔔 Сигналы: Только Strong" if s.get("filter_level") == "strong_only" else "🔔 Сигналы: Strong + Watch"
    autotrade_label = "🔴 Выключить автоторговлю" if s.get("auto_trade", False) else "⚡ Включить автоторговлю"
    whale_autoscan_label = "🔴 Выключить скан китов" if s.get("whale_autoscan", DEFAULT_WHALE_AUTOSCAN) else "🐋 Включить скан китов"

    return {
        "inline_keyboard": [
            [
                {"text": autotrade_label, "callback_data": "trade:toggle"},
            ],
            [
                {"text": "💰 11 USDT", "callback_data": "trade:amt:11"},
                {"text": "💰 25 USDT", "callback_data": "trade:amt:25"},
                {"text": "💰 50 USDT", "callback_data": "trade:amt:50"},
            ],
            [
                {"text": "🎯 TP: +2%", "callback_data": "trade:tp:2"},
                {"text": "🎯 TP: +3%", "callback_data": "trade:tp:3"},
                {"text": "🎯 TP: +5%", "callback_data": "trade:tp:5"},
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
                {"text": whale_autoscan_label, "callback_data": "whale:toggle"},
            ],
            [
                {"text": "🐋 Порог −5", "callback_data": "whale:score:-5"},
                {"text": "🐋 Порог +5", "callback_data": "whale:score:+5"},
            ],
            [
                {"text": "🐋 55", "callback_data": "whale:score:set:55"},
                {"text": "🐋 65 (базовый)", "callback_data": "whale:score:set:65"},
                {"text": "🐋 75 (строгий)", "callback_data": "whale:score:set:75"},
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
                {"text": f"⚡ Купить на {trade_amt:.0f}$ (TP +{tp_pct:.0f}%)", "callback_data": f"trade_buy:{sig.symbol}"},
            ],
            [
                {"text": f"➕ Добавить {base} в портфель", "callback_data": f"add_coin:{sig.symbol}:{sig.price}"},
                {"text": "ℹ️ Детали факторов", "callback_data": f"factors:{sig.symbol}:{sig.best_tf}"},
            ]
        ]
    }

def whale_inline_kb(sig) -> dict:
    """Клавиатура для карточки whale-сигнала (sig — объект whale_scan.WhaleSignal)."""
    base = sig.base
    binance_url = f"https://www.binance.com/en/trade/{base}_USDT?type=spot"
    tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{sig.symbol}"
    return {
        "inline_keyboard": [
            [
                {"text": "📊 Binance Spot", "url": binance_url},
                {"text": "📈 TradingView", "url": tv_url},
            ],
            [
                {"text": "🔎 Детали китов", "callback_data": f"whale:details:{sig.symbol}"},
            ],
            [
                {"text": f"➕ Добавить {base} в портфель", "callback_data": f"add_coin:{sig.symbol}:{sig.price}"},
                {"text": "🐋 Проверить другую монету", "callback_data": "whale:symbol_prompt"},
            ],
        ]
    }

HELP_TEXT = (
    "<b>🚀 Pump Pulse Scanner 2.1 & Binance Spot Trader + Whale Scan</b>\n\n"
    "Бот отслеживает аномальную активность на спотовом рынке Binance (USDT-пары) "
    "и поддерживает автоматическую покупку с мгновенным выставлением Take-Profit (+3%).\n\n"
    "<b>📌 Основные функции:</b>\n"
    "• <b>🔍 Скан сейчас</b> (/scan) — сканирование всего спота прямо сейчас.\n"
    "• <b>🐋 Скан китов</b> (/whale) — детектор крупных сделок (от $100k) на споте.\n"
    "• <b>🐋 /whale BTC</b> — мгновенный глубокий анализ китов по одной монете.\n"
    "• <b>💼 Портфель</b> (/portfolio) — баланс Binance, активные сделки и PnL.\n"
    "• <b>⚡ Торговля</b> (/trade) — статус автоторговли и история профита.\n"
    "• <b>➕ Добавить актив</b> (/add) — внести купленную монету вручную.\n"
    "• <b>🗑 Удалить актив</b> (/del) — убрать позицию в 1 клик.\n"
    "• <b>⚙️ Настройки</b> (/settings) — включение автоторговли, размер ставки и TP.\n"
    "• <b>🔑 Привязка API</b> (/api) — настройка Binance API ключей.\n\n"
    "<b>🐋 Как работает «Скан действий китов»:</b>\n"
    "1. За окно 15 минут анализируются агрегированные сделки (aggTrades) топ-30 пар по ликвидности.\n"
    "2. «Принт кита» — сделка от $100 000 и одновременно ≥ 25× медианы пары.\n"
    "3. Считаются 7 факторов (100 баллов): доля китов в потоке (25), чистый поток BUY−SELL (25), "
    "концентрация топ-20 принтов (15), доминирование покупок (10), ускорение размера сделок (10), "
    "стены в стакане (10), подтверждение фьючерсами OI/Taker (5).\n"
    "4. Оценки: 🐋 АККУМУЛЯЦИЯ (киты набирают — бычий сигнал), РАЗГРУЗКА (киты продают — не входить!), АКТИВНОСТЬ.\n"
    "5. Работает и как режим в этом боте (кнопка 🐋 + автоскан), и как отдельный workflow whale-scan.yml.\n\n"
    "<b>⚡ Как работает спотовая автоторговля:</b>\n"
    "1. При обнаружении подтверждённого импульса (Score 74+, STRONG) или аккумуляции китов бот проверяет свободный USDT-баланс.\n"
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

def answer_callback(token: str, callback_id: str, text: Optional[str] = None) -> None:
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = False
    try:
        api_call(token, "answerCallbackQuery", payload, retries=1)
    except Exception:
        pass

def set_bot_commands(token: str) -> None:
    cmds = [
        {"command": "start", "description": "Главное меню"},
        {"command": "scan", "description": "Сканер спота сейчас"},
        {"command": "whale", "description": "🐋 Скан действий китов"},
        {"command": "portfolio", "description": "Портфель и PnL"},
        {"command": "add", "description": "Добавить монету в портфель"},
        {"command": "del", "description": "Удалить монету из портфеля"},
        {"command": "trade", "description": "Статус автоторговли"},
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

# ───────────────────────── Режим «Скан действий китов» (интеграция) ─────────────────────────

def execute_whale_scan_and_report(token: str, chat_id: Union[str, int], state: dict) -> None:
    """Запуск полного скана китов по топ-парам с отправкой результатов в чат."""
    if not WHALE_MODULE_OK:
        send_telegram(token, chat_id, "⚠️ Модуль <code>whale_scan.py</code> не найден рядом с ботом. Добавьте файл в репозиторий.", reply_markup=main_keyboard())
        return
    status_msg_id = send_telegram(token, chat_id, "🐋 <i>Сканирую крупные сделки (китов) на спотовом рынке Binance... Подождите.</i>")
    try:
        s = state["settings"]
        signals, meta = ws.run_whale_scan(
            min_score=s.get("whale_min_score", DEFAULT_WHALE_MIN_SCORE),
            top_n=DEFAULT_WHALE_TOP_N,
        )
        duration = meta["duration_ms"] / 1000.0
        if signals:
            summary_text = (
                f"🐋 <b>Скан китов завершён за {duration:.1f}с</b>\n\n"
                f"• Проверено пар: <code>{meta['universe']}</code>\n"
                f"• Просканировано топ-пар: <code>{meta['scanned']}</code>\n"
                f"• Порог Whale Score: <code>{meta['min_score']:.0f}</code>\n"
                f"• Активность китов найдена: <b>{len(signals)}</b>\n\n"
                f"Ниже — подробные карточки:"
            )
            if status_msg_id:
                edit_message(token, chat_id, status_msg_id, summary_text)
            else:
                send_telegram(token, chat_id, summary_text)
            for sig in signals[:5]:
                send_telegram(token, chat_id, ws.format_whale_alert(sig), reply_markup=whale_inline_kb(sig))
                time.sleep(0.15)
            send_telegram(token, chat_id, "🔘 Скан китов завершён. Главное меню активно:", reply_markup=main_keyboard())
        else:
            near_lines = "\n".join(
                f"{i}. <b>{n['base']}</b> — score <code>{n['score']:.0f}</code>, "
                f"поток {n['net_flow_pct']:+.0f}%, доля китов {n['whale_share']*100:.0f}% "
                f"({n['whale_buys']}B/{n['whale_sells']}S)"
                for i, n in enumerate(meta["near"][:4], 1)
            ) or "<i>нет данных</i>"
            report_text = (
                f"🐋 <b>Скан китов завершён за {duration:.1f}с</b>\n\n"
                f"• Просканировано топ-пар: <code>{meta['scanned']}</code>\n"
                f"• Порог Whale Score: <code>{meta['min_score']:.0f}</code>\n"
                f"• Сигналов: <b>0</b> <i>(киты спят)</i>\n\n"
                f"<b>Ближайшие к порогу:</b>\n{near_lines}\n\n"
                f"💡 <i>Снизьте порог кнопками 🐋 в Настройках, чтобы ловить активность раньше.</i>"
            )
            if status_msg_id:
                edit_message(token, chat_id, status_msg_id, report_text)
            else:
                send_telegram(token, chat_id, report_text)
            send_telegram(token, chat_id, "🔘 Главное меню активно:", reply_markup=main_keyboard())
    except Exception as e:
        print(f"❌ Ошибка скана китов: {e}", file=sys.stderr)
        traceback.print_exc()
        send_telegram(token, chat_id, f"⚠️ <b>Ошибка скана китов:</b>\n<code>{e}</code>", reply_markup=main_keyboard())

def whale_deep_dive(token: str, chat_id: Union[str, int], symbol: str) -> None:
    """Глубокий анализ китов по одной монете (команда /whale BTC или кнопка «Детали китов»)."""
    if not WHALE_MODULE_OK:
        send_telegram(token, chat_id, "⚠️ Модуль <code>whale_scan.py</code> не найден.", reply_markup=main_keyboard())
        return
    symbol = normalize_symbol(symbol)
    status_msg_id = send_telegram(token, chat_id, f"🐋 <i>Загружаю последние крупные сделки {symbol}...</i>")
    try:
        sig = ws.deep_dive(symbol)
        if not sig:
            text = (f"⚠️ Не удалось загрузить сделки по <code>{symbol}</code> "
                    f"(пара не найдена на споте или биржа недоступна).")
            if status_msg_id:
                edit_message(token, chat_id, status_msg_id, text)
            else:
                send_telegram(token, chat_id, text)
            return
        text = ws.format_whale_detail(sig)
        if status_msg_id:
            edit_message(token, chat_id, status_msg_id, text, reply_markup=whale_inline_kb(sig))
        else:
            send_telegram(token, chat_id, text, reply_markup=whale_inline_kb(sig))
    except Exception as e:
        print(f"❌ Ошибка детализации китов: {e}", file=sys.stderr)
        send_telegram(token, chat_id, f"⚠️ <b>Ошибка анализа:</b>\n<code>{e}</code>", reply_markup=main_keyboard())

def run_whale_oneshot(token: Optional[str], chat_id: Optional[str]) -> None:
    """Разовый whale-скан для CLI: python pump_bot.py --whale"""
    print("=== Режим разового скана китов (Whale Oneshot) ===")
    if not WHALE_MODULE_OK:
        print("❌ Модуль whale_scan.py недоступен", file=sys.stderr)
        return
    state = load_state()
    min_score = state.get("settings", {}).get("whale_min_score", DEFAULT_WHALE_MIN_SCORE)
    signals, meta = ws.run_whale_scan(min_score=min_score, top_n=DEFAULT_WHALE_TOP_N)
    print(f"Whale scan: {meta['duration_ms']/1000:.1f}с, scanned={meta['scanned']}, сигналов={len(signals)}")
    if not token or not chat_id:
        for s in signals:
            print(f"[{s.grade.upper()}] {s.symbol} score={s.score:.0f} net_flow={s.net_flow_pct:+.0f}% "
                  f"share={s.whale_share*100:.0f}% prints={s.whale_buys}B/{s.whale_sells}S")
        for n in meta["near"][:5]:
            print(f"  ~ {n['base']}: score={n['score']:.0f} (порог {meta['min_score']:.0f})")
        return
    now = int(time.time())
    sent: dict = state.setdefault("sent_whale_alerts", {})
    target_chats: Set[str] = set()
    if chat_id and (str(chat_id).lstrip("-").isdigit() or str(chat_id).startswith("@")):
        target_chats.add(str(chat_id))
    for c in state.get("allowed_chats", []):
        cid_str = str(c).strip()
        if cid_str and cid_str != "12345" and (cid_str.lstrip("-").isdigit() or cid_str.startswith("@")):
            target_chats.add(cid_str)
    sent_count = 0
    for sig in signals:
        if sig.alert_key in sent:
            print(f"• Пропуск {sig.symbol}: алерт за этот час уже отправлен")
            continue
        sent[sig.alert_key] = now
        sent_count += 1
        for cid in target_chats:
            ok = send_telegram(token, cid, ws.format_whale_alert(sig), reply_markup=whale_inline_kb(sig))
            print(f"• {sig.symbol} ({sig.grade} {sig.score:.0f}) → {cid}: {'sent' if ok else 'fail'}")
    if sent_count > 0:
        save_state(state)
        print(f"Отправлено новых whale-сигналов: {sent_count}")
    else:
        print("Новых whale-сигналов нет.")
        is_manual = (
            os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            or os.environ.get("NOTIFY_EMPTY", "false").lower() in ("true", "1")
            or "--notify-always" in sys.argv
        )
        if is_manual:
            near_lines = "\n".join(
                f"  • {n['base']}: score {n['score']:.0f}, поток {n['net_flow_pct']:+.0f}%, доля китов {n['whale_share']*100:.0f}%"
                for n in meta["near"][:4]
            ) or "  —"
            msg = (
                f"🐋 <b>Whale Scan — отчёт</b>\n\n"
                f"• Просканировано топ-пар: <code>{meta['scanned']}</code>\n"
                f"• Порог Whale Score: <code>{min_score:.0f}</code>\n"
                f"• Сигналов: <b>0</b> <i>(киты спят)</i>\n\n"
                f"<b>Ближайшие к порогу:</b>\n{near_lines}"
            )
            for cid in target_chats:
                send_telegram(token, cid, msg)
            print("Отправлен статус-отчёт whale-сканера.")

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
                f"• Сигналов: <b>0</b>\n\n"
                f"<b>Ближайшие кандидаты (ниже порога):</b>\n{top_lines}"
            )
            if status_msg_id:
                edit_message(token, chat_id, status_msg_id, report_text)
            else:
                send_telegram(token, chat_id, report_text)
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
    for sig in signals:
        if sig.alert_key in sent:
            print(f"• Пропуск {sig.symbol}: уже отправлен")
            continue
        sent[sig.alert_key] = now
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

            # Проверяем исполнение тейк-профитов по активным сделкам
            if settings.get("auto_trade", False) and state.get("active_trades"):
                try:
                    check_active_trades(token, primary_chat_id, state)
                except Exception as e:
                    print(f"[AutoTrade Check Error]: {e}", file=sys.stderr)

            # 1. Скан пампов
            signals, meta, _top = run_scan(
                min_score=settings.get("min_score", DEFAULT_MIN_SCORE),
                min_quote_volume=settings.get("min_quote_volume", DEFAULT_MIN_QUOTE_VOLUME),
            )
            print(f"[Autoscan] {time.strftime('%H:%M:%S')}: {meta['duration_ms']/1000:.1f}с, "
                  f"candidates={meta['candidates']}, сигналов={len(signals)}")

            filter_level = settings.get("filter_level", "strong_and_watch")
            sent: dict = state.setdefault("sent_alerts", {})
            new_alerts_count = 0

            for sig in signals:
                if filter_level == "strong_only" and sig.grade != "strong":
                    continue
                if sig.alert_key in sent:
                    continue

                sent[sig.alert_key] = now
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

            # 2. Режим «Скан действий китов» (совместный: свои алерты + опционально автоторговля)
            new_whale_count = 0
            if WHALE_MODULE_OK and settings.get("whale_autoscan", DEFAULT_WHALE_AUTOSCAN):
                try:
                    whale_signals, _wmeta = ws.run_whale_scan(
                        min_score=settings.get("whale_min_score", DEFAULT_WHALE_MIN_SCORE),
                        top_n=DEFAULT_WHALE_TOP_N,
                    )
                    sent_whales: dict = state.setdefault("sent_whale_alerts", {})
                    for wsig in whale_signals:
                        if wsig.alert_key in sent_whales:
                            continue
                        sent_whales[wsig.alert_key] = now
                        new_whale_count += 1
                        wtext = ws.format_whale_alert(wsig)
                        wkb = whale_inline_kb(wsig)
                        for cid in target_chats:
                            try:
                                send_telegram(token, cid, wtext, reply_markup=wkb)
                            except Exception as e:
                                print(f"Ошибка отправки whale-алерта в {cid}: {e}", file=sys.stderr)

                        # Совместный режим: автопокупка по подтверждённой аккумуляции китов
                        if (settings.get("auto_trade", False)
                                and wsig.grade == "accumulation"
                                and wsig.score >= settings.get("trade_min_score", DEFAULT_TRADE_MIN_SCORE)):
                            if primary_str:
                                try:
                                    print(f"[WhaleScan] Автопокупка по китам {wsig.symbol} (score {wsig.score:.1f})...")
                                    whale_sig = PumpSignal(
                                        symbol=wsig.symbol, base=wsig.base, price=wsig.price,
                                        change_24h=0.0, quote_volume_24h=0.0,
                                        high_24h=wsig.price, low_24h=wsig.price,
                                        btc_relative_24h=0.0, best_tf="whale",
                                        best_score=wsig.score, grade="strong",
                                        alert_key=f"whale_trade_{wsig.symbol}_{now}", by_tf=[],
                                    )
                                    execute_pump_auto_trade(token, primary_str, state, whale_sig)
                                except Exception as e:
                                    print(f"[WhaleScan AutoTrade Error for {wsig.symbol}]: {e}", file=sys.stderr)
                except Exception as e:
                    print(f"[WhaleScan Error]: {e}", file=sys.stderr)

            if new_alerts_count > 0 or new_whale_count > 0:
                save_state(state, sync_git=True)
                print(f"[Autoscan] Новых сигналов: pump={new_alerts_count}, whale={new_whale_count}, чатов: {len(target_chats)}")

        except Exception as e:
            print(f"[Autoscan Error]: {e}", file=sys.stderr)
            traceback.print_exc()

        stop_event.wait(interval)

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

    if RUN_MODE == "bot":
        scan_thread = threading.Thread(target=autoscan_worker, args=(token, chat_id, state, stop_event), daemon=True)
        scan_thread.start()

    welcome = (
        "<b>👋 Приветствую в Pump Pulse Scanner 2.1 + Whale Scan!</b>\n\n"
        "Я сканирую спотовый рынок Binance (USDT-пары) в реальном времени "
        "и мгновенно сообщу о зарождении пампа (Score 58-74+, ДО выстрела).\n"
        "Также умею детектировать <b>действия китов</b> (крупные сделки от $100k) и "
        "автоматически торговать на вашем Binance Spot с TP +3%.\n\n"
        "🐋 <b>Новинка:</b> кнопка «Скан китов» в меню и команда /whale.\n\n"
        "• Выберите действие кнопками ниже\n"
        "• Или напишите: <code>scan</code>, <code>whale</code>, <code>whale SOL</code>, <code>portfolio</code> и т.д."
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
            if text_lower in ("/start", "start", "старт", "меню", "/menu"):
                print(f"-> Показ главного меню для {chat_id_local}")
                send_telegram(token, chat_id_local, "<b>📋 Главное меню Pump Pulse</b>\n\nВыберите действие:", reply_markup=main_keyboard())
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
                        state["settings"]["binance_api_key"] = parts[0].strip()
                        state["settings"]["binance_api_secret"] = parts[1].strip()
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

                if cur_st == "waiting_whale_symbol":
                    user_fsm.pop(chat_id_local, None)
                    whale_deep_dive(token, chat_id_local, text)
                    return

            # ── Reply-кнопки и команды ──
            clean_text = text.lstrip("/")
            cmd = clean_text.split()[0].lower() if clean_text else ""

            is_menu_action = (
                "скан" in text_lower
                or "кит" in text_lower
                or "portfolio" in text_lower
                or "портфел" in text_lower
                or "добав" in text_lower
                or "удал" in text_lower
                or "настрой" in text_lower
                or "баланс" in text_lower
                or "balance" in text_lower
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

            elif cmd in ("scan", "скан") or (is_menu_action and "скан" in text_lower and "кит" not in text_lower):
                print(f"-> Скан для {chat_id_local}")
                execute_scan_and_report(token, chat_id_local, state)

            elif cmd == "whale" or "кит" in text_lower:
                print(f"-> Запуск скана китов для {chat_id_local}")
                args = clean_text.split()[1:]
                if args:
                    whale_deep_dive(token, chat_id_local, args[0])
                else:
                    execute_whale_scan_and_report(token, chat_id_local, state)

            elif cmd in ("portfolio", "портфель") or (is_menu_action and "портфел" in text_lower):
                print(f"-> Портфель для {chat_id_local}")
                msg_id = send_telegram(token, chat_id_local, "⏳ <i>Загружаю портфель...</i>")
                port_text = format_portfolio(state)
                if msg_id:
                    edit_message(token, chat_id_local, msg_id, port_text, reply_markup=portfolio_inline_kb())
                else:
                    send_telegram(token, chat_id_local, port_text, reply_markup=portfolio_inline_kb())

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
                    f"• Открытых сделок: <code>{len(active)}/3</code>",
                    f"• Автоскан китов: <code>{'🟢' if settings.get('whale_autoscan', True) else '🔴'}</code>\n",
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

            elif cmd in ("api", "ключ"):
                args = clean_text.split()[1:]
                if len(args) >= 2:
                    state["settings"]["binance_api_key"] = args[0].strip()
                    state["settings"]["binance_api_secret"] = args[1].strip()
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
                    edit_message(token, chat_id_local, msg_id, bal_text, reply_markup=portfolio_inline_kb())
                else:
                    send_telegram(token, chat_id_local, bal_text, reply_markup=portfolio_inline_kb())

            elif text_lower in ("ping", "пинг"):
                send_telegram(token, chat_id_local, "🏓 <b>pong</b> — бот на связи!")

            else:
                # Эхо-подсказка для нераспознанных команд
                hint = (
                    "🤔 Не распознал команду.\n\n"
                    "Попробуйте:\n"
                    "• <code>scan</code> — сканер пампов\n"
                    "• <code>whale</code> — скан китов\n"
                    "• <code>whale SOL</code> — анализ китов по монете\n"
                    "• <code>portfolio</code> — портфель\n"
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
                    edit_message(token, cb_chat, msg_id, port_text, reply_markup=portfolio_inline_kb())
                else:
                    send_telegram(token, cb_chat, port_text, reply_markup=portfolio_inline_kb())

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

            elif cb_data == "menu:main":
                answer_callback(token, cb_id, "Главное меню")
                send_telegram(token, cb_chat, "<b>📋 Главное меню Pump Pulse</b>\n\nВыберите действие:", reply_markup=main_keyboard())

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
                amt = float(cb_data.split(":")[2])
                settings["trade_amount_usdt"] = amt
                save_state(state)
                answer_callback(token, cb_id, f"Ставка: {amt:.0f} USDT")
                if msg_id:
                    edit_message(token, cb_chat, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))

            elif cb_data.startswith("trade:tp:"):
                tp = float(cb_data.split(":")[2])
                settings["take_profit_pct"] = tp
                save_state(state)
                answer_callback(token, cb_id, f"TP: +{tp:.0f}%")
                if msg_id:
                    edit_message(token, cb_chat, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))

            elif cb_data == "trade:balance":
                answer_callback(token, cb_id, "Запрашиваю детальный баланс...")
                bal_text = format_binance_balance_detailed(state)
                if msg_id:
                    edit_message(token, cb_chat, msg_id, bal_text, reply_markup=portfolio_inline_kb())
                else:
                    send_telegram(token, cb_chat, bal_text, reply_markup=portfolio_inline_kb())

            elif cb_data.startswith("trade_buy:"):
                sym = cb_data.split(":", 1)[1]
                answer_callback(token, cb_id, f"Покупаю {base_asset(sym)}...")
                mkt_p = get_price(sym)
                if mkt_p is None:
                    send_telegram(token, cb_chat, f"❌ Не удалось получить цену {sym}.")
                    return
                dummy_sig = PumpSignal(
                    symbol=sym,
                    base=base_asset(sym),
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
                execute_pump_auto_trade(token, cb_chat, state, dummy_sig)

            # ── Режим «Скан действий китов» ──
            elif cb_data == "whale:toggle":
                settings["whale_autoscan"] = not settings.get("whale_autoscan", DEFAULT_WHALE_AUTOSCAN)
                save_state(state)
                st_str = "включён 🟢" if settings["whale_autoscan"] else "выключен 🔴"
                answer_callback(token, cb_id, f"Скан китов {st_str}")
                if msg_id:
                    edit_message(token, cb_chat, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))

            elif cb_data.startswith("whale:score:"):
                part = cb_data.rsplit(":", 1)[-1]
                if part == "-5":
                    settings["whale_min_score"] = max(30.0, settings.get("whale_min_score", DEFAULT_WHALE_MIN_SCORE) - 5)
                elif part == "+5":
                    settings["whale_min_score"] = min(95.0, settings.get("whale_min_score", DEFAULT_WHALE_MIN_SCORE) + 5)
                else:
                    settings["whale_min_score"] = float(part)
                save_state(state)
                answer_callback(token, cb_id, f"Whale Score порог: {settings['whale_min_score']:.0f}")
                if msg_id:
                    edit_message(token, cb_chat, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))

            elif cb_data == "whale:scan_now":
                answer_callback(token, cb_id, "Сканирую действия китов...")
                execute_whale_scan_and_report(token, cb_chat, state)

            elif cb_data == "whale:symbol_prompt":
                answer_callback(token, cb_id)
                user_fsm[cb_chat] = {"state": "waiting_whale_symbol", "data": {}}
                send_telegram(
                    token, cb_chat,
                    "🐋 <b>Проверка конкретной монеты</b>\n\n"
                    "Введите тикер (например <code>BTC</code>, <code>SOL</code> или <code>PEPE</code>):",
                    reply_markup=cancel_keyboard(),
                )

            elif cb_data.startswith("whale:details:"):
                sym = cb_data.split(":", 2)[2]
                answer_callback(token, cb_id, f"Анализ китов: {ws.base_of(sym) if WHALE_MODULE_OK else sym}")
                whale_deep_dive(token, cb_chat, sym)

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
        if WHALE_MODULE_OK:
            sig = ws.deep_dive("BTC")
            print(f"✅ Whale deep_dive BTC: {'OK' if sig else 'нет данных'}")
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
    if "--whale" in sys.argv:
        run_whale_oneshot(token, chat_id)
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
