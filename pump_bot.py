#!/usr/bin/env python3
"""
Pump Pulse Scanner (Telegram Bot 2.0).

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
import signal
import threading
import traceback
import concurrent.futures as cf
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple, Any, Set
import urllib.request
import urllib.error

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

BINANCE_BASE = "https://data-api.binance.vision"
TIMEFRAMES = ("15m", "30m", "1h")
TF_MS = {"15m": 15 * 60_000, "30m": 30 * 60_000, "1h": 60 * 60_000}

DEFAULT_MIN_QUOTE_VOLUME = float(os.environ.get("MIN_QUOTE_VOLUME", "1500000"))
DEFAULT_MIN_SCORE = float(os.environ.get("MIN_SCORE", "62"))
ALREADY_PUMPED_MAX = float(os.environ.get("ALREADY_PUMPED_MAX", "35"))
MAX_CANDIDATES = int(os.environ.get("MAX_CANDIDATES", "40"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "16"))
KLINES_LIMIT = 200  # нужно ~32+ бара на 1h

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
    req = urllib.request.Request(url, headers={"User-Agent": "pump-pulse/2.0"})
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
        and t["priceChangePercent"] <= ALREADY_PUMPED_MAX
    ]
    by_change = sorted(liquid, key=lambda t: t["priceChangePercent"], reverse=True)
    by_heat = sorted(
        liquid,
        key=lambda t: math.log10(t["quoteVolume"] + 1) * max(t["priceChangePercent"], 0),
        reverse=True,
    )
    by_vol = sorted(liquid, key=lambda t: t["quoteVolume"], reverse=True)

    picked: Dict[str, dict] = {}
    for t in by_change[:16] + by_heat[:16] + by_vol[:12]:
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
    url = f"{BINANCE_BASE}/api/v3/klines?symbol={symbol}&interval=15m&limit={limit}"
    try:
        raw = http_get_json(url, timeout=10)
        return [parse_kline(r) for r in raw]
    except Exception:
        return []

# ───────────────────────── Scoring Engine ─────────────────────────

MIN_BARS = 32

def grade_from(score: float, late: bool) -> str:
    if score < 50:
        return "none"
    if late and score >= 66:
        return "late"
    if score >= 74:
        return "strong"
    if score >= 60:
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
    bullish = last.close > last.open

    atr14 = atr(hist, 14)
    if not atr14 or atr14 <= 0:
        return None
    atr_expansion = range_ / atr14

    prior_high = donchian_high(hist, 20)
    if prior_high is None:
        return None
    breakout_pct = pct_change(prior_high, last.close)

    rsi14 = rsi(closes, 14)
    if rsi14 is None:
        return None

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema_aligned = (
        ema9 is not None and ema21 is not None
        and last.close > ema9 and ema9 > ema21
    )

    taker_buy = last.taker_buy_base / last.volume if last.volume > 0 else 0.5

    lookback = 3 if timeframe == "1h" else 4 if timeframe == "30m" else 6
    asset_roc = roc(closes, lookback)
    btc_closes = [c.close for c in btc_candles]
    btc_roc = roc(btc_closes, lookback) if len(btc_closes) >= lookback + 1 else 0.0
    vs_btc_pct = asset_roc - btc_roc

    prev_roc = roc(closes[:-1], max(1, lookback - 1))
    accel = asset_roc - prev_roc

    change_pct = pct_change(last.open, last.close)

    # --- factor values 0..1 ---
    vol_factor = clamp((vol_ratio - 1.0) / 3.0, 0, 1)
    breakout_factor = clamp(breakout_pct / 2.5, 0, 1) if breakout_pct > 0 else 0
    atr_factor = clamp((atr_expansion - 1.0) / 2.0, 0, 1)
    quality = 0.0
    if bullish:
        quality += 0.35
    quality += clamp(body_ratio * 1.2, 0, 0.35)
    quality += clamp(close_pos, 0, 0.30)
    quality = clamp(quality, 0, 1)
    taker_factor = clamp((taker_buy - 0.45) / 0.25, 0, 1)
    ema_factor = 1.0 if ema_aligned else 0.25
    rsi_factor = 0.0
    if 52 <= rsi14 <= 72:
        rsi_factor = 1.0 - abs(rsi14 - 62) / 20
    elif 45 <= rsi14 < 52:
        rsi_factor = 0.4
    elif rsi14 > 72:
        rsi_factor = clamp(1.0 - (rsi14 - 72) / 15, 0, 0.6)
    vs_btc_factor = clamp((vs_btc_pct + 0.5) / 2.5, 0, 1)
    accel_factor = clamp((accel + 0.3) / 1.5, 0, 1)

    late = (
        change_24h > 12
        or rsi14 >= 76
        or breakout_pct > 4.5
        or vol_ratio > 5.5
    )

    factors = [
        FactorScore("vol", "Объём vs SMA20", 18, vol_factor, f"{vol_ratio:.1f}×"),
        FactorScore("breakout", "Пробой Donchian20", 15, breakout_factor, f"{breakout_pct:+.2f}%"),
        FactorScore("atr", "Расширение ATR", 12, atr_factor, f"{atr_expansion:.2f}× ATR14"),
        FactorScore("candle", "Качество свечи", 12, quality, f"тело {body_ratio*100:.0f}% · close {close_pos*100:.0f}%"),
        FactorScore("taker", "Агрессия тейкера", 10, taker_factor, f"{taker_buy*100:.0f}% market buy"),
        FactorScore("ema", "Тренд EMA 9/21", 8, ema_factor, "close > EMA9 > EMA21" if ema_aligned else "нет выравнивания"),
        FactorScore("rsi", "Окно RSI 14", 8, rsi_factor, f"{rsi14:.1f}"),
        FactorScore("btc", "Сила vs BTC", 7, vs_btc_factor, f"{vs_btc_pct:+.2f}%"),
        FactorScore("accel", "Ускорение ROC", 5, accel_factor, f"{accel:+.2f} п.п."),
    ]

    score = sum(f.value * f.weight for f in factors)
    grade = grade_from(score, late)
    if strict and grade == "none":
        return None

    reasons = []
    if vol_ratio >= 2:
        reasons.append(f"объём {vol_ratio:.1f}× среднего")
    if last.close > prior_high:
        reasons.append("пробой 20-барного максимума")
    if ema_aligned:
        reasons.append("EMA выровнены вверх")
    if taker_buy >= 0.58:
        reasons.append("доминируют market buy")
    if 54 <= rsi14 <= 72:
        reasons.append("RSI в раннем импульсе, не перекуплен")
    if vs_btc_pct >= 0.4:
        reasons.append("обгоняет BTC")
    if atr_expansion >= 1.8:
        reasons.append("расширение волатильности")
    if not bullish:
        reasons.append("последняя свеча ещё не бычья")

    risks = []
    if late:
        risks.append("уже растянут — риск опоздавшего входа")
    if rsi14 >= 78:
        risks.append("RSI высокий, возможна разгрузка")
    if upper_wick_ratio > 0.28:
        risks.append("верхняя тень — продавцы защищаются")
    if change_24h > 15:
        risks.append("сильный ход за 24ч, поздняя фаза")
    if vol_ratio > 6:
        risks.append("климакс объёма — часто конец волны")

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
        candles = raw15 if tf == "15m" else aggregate_timeframe(raw15, TF_MS[tf])
        btc_c = btc_raw15 if tf == "15m" else aggregate_timeframe(btc_raw15, TF_MS[tf])
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
        },
        "sent_alerts": {},  # alert_key -> timestamp
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

def format_portfolio(state: dict) -> str:
    port = state["portfolio"]
    if not port:
        return (
            "<b>💼 Ваш портфель</b>\n\n"
            "<i>Портфель пока пуст.</i>\n\n"
            "💡 Нажмите кнопку <b>«➕ Добавить актив»</b> ниже, чтобы внести монету, "
            "или используйте команду <code>/add BTC 0.05 65000</code>."
        )

    lines = ["<b>💼 Ваш криптовалютный портфель</b>\n"]
    total_cost = 0.0
    total_value = 0.0
    symbols = sorted(port.keys())
    prices = get_multiple_prices(symbols)

    for symbol in symbols:
        pos = port[symbol]
        qty = float(pos["qty"])
        avg = float(pos["avg_price"])
        cost = qty * avg
        total_cost += cost

        price = prices.get(symbol)
        base = base_asset(symbol)

        if price is None:
            lines.append(
                f"⚪ <b>{base}</b>: {fmt_qty(qty)} шт\n"
                f"   ├ Вход: {fmt_price(avg)} USDT (вложено: {cost:,.2f} $)\n"
                f"   └ <i>Текущая цена временно недоступна</i>\n"
            )
            total_value += cost
            continue

        val = qty * price
        total_value += val
        pnl = val - cost
        pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0
        sign = "🟢" if pnl >= 0 else "🔴"

        lines.append(
            f"{sign} <b>{base}/USDT</b>: {fmt_qty(qty)} шт\n"
            f"   ├ Вход: <code>{fmt_price(avg)}</code> → Рынок: <code>{fmt_price(price)}</code>\n"
            f"   ├ Баланс: {val:,.2f} USDT (вложено {cost:,.2f} $)\n"
            f"   └ <b>P/L:</b> <b>{pnl:+.2f} USDT</b> (<b>{fmt_pct(pnl_pct)}</b>)\n"
        )

    total_pnl = total_value - total_cost
    total_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0.0
    tot_sign = "🟢" if total_pnl >= 0 else "🔴"

    lines.append("─────────────────────")
    lines.append(f"💵 <b>Сумма инвестиций:</b> {total_cost:,.2f} USDT")
    lines.append(f"📊 <b>Текущая оценка:</b> {total_value:,.2f} USDT")
    lines.append(f"{tot_sign} <b>Общий P/L:</b> <b>{total_pnl:+.2f} USDT ({fmt_pct(total_pct)})</b>")

    return "\n".join(lines)

# ───────────────────────── Клавиатуры ─────────────────────────

def main_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "🔍 Скан сейчас"}, {"text": "💼 Портфель"}],
            [{"text": "➕ Добавить актив"}, {"text": "🗑 Удалить актив"}],
            [{"text": "⚙️ Настройки"}, {"text": "ℹ️ Помощь"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }

def cancel_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "❌ Отмена"}],
            [{"text": "🔍 Скан сейчас"}, {"text": "💼 Портфель"}],
            [{"text": "⚙️ Настройки"}, {"text": "ℹ️ Помощь"}],
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
                {"text": "🔙 Вернуть меню кнопок", "callback_data": "menu:main"},
            ]
        ]
    }

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
    auto_label = "🟢 Включён" if s.get("autoscan", True) else "🔴 Выключен"
    interval_m = s.get("scan_interval_sec", DEFAULT_SCAN_INTERVAL) // 60

    return (
        "<b>⚙️ Параметры сканера Pump Pulse</b>\n\n"
        f"• <b>Порог Score (MIN_SCORE):</b> <code>{s['min_score']:.0f}</code>\n"
        f"• <b>Мин. суточный объём:</b> <code>{s['min_quote_volume']:,.0f} USDT</code>\n"
        f"• <b>Автосканирование рынка:</b> {auto_label}\n"
        f"• <b>Интервал автопроверки:</b> <code>{interval_m} мин ({s['scan_interval_sec']}с)</code>\n"
        f"• <b>Уведомления:</b> <code>{filt_label}</code>\n\n"
        "<i>Используйте кнопки ниже для быстрой настройки бота:</i>"
    )

def settings_inline_kb(state: dict) -> dict:
    s = state["settings"]
    autoscan_label = "🔴 Отключить автоскан" if s.get("autoscan", True) else "🟢 Включить автоскан"
    filter_label = "🔔 Сигналы: Только Strong" if s.get("filter_level") == "strong_only" else "🔔 Сигналы: Strong + Watch"

    return {
        "inline_keyboard": [
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
            ],
            [
                {"text": "⏱ 3 мин", "callback_data": "interval:180"},
                {"text": "⏱ 5 мин", "callback_data": "interval:300"},
                {"text": "⏱ 10 мин", "callback_data": "interval:600"},
            ],
            [
                {"text": filter_label, "callback_data": "filter:toggle"},
            ],
            [
                {"text": "🔄 Обновить статус", "callback_data": "settings:refresh"},
                {"text": "🔙 Вернуть меню кнопок", "callback_data": "menu:main"},
            ]
        ]
    }

def signal_inline_kb(sig: PumpSignal) -> dict:
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
                {"text": f"➕ Добавить {base} в портфель", "callback_data": f"add_coin:{sig.symbol}:{sig.price}"},
                {"text": "ℹ️ Детали факторов", "callback_data": f"factors:{sig.symbol}:{sig.best_tf}"},
            ]
        ]
    }

HELP_TEXT = (
    "<b>🚀 Pump Pulse Scanner 2.0</b>\n\n"
    "Бот отслеживает аномальную активность и зарождение импульсов на спотовом рынке Binance (USDT-пары).\n\n"
    "<b>📌 Основные функции:</b>\n"
    "• <b>🔍 Скан сейчас</b> (/scan) — сканирование всего спота Binance прямо сейчас.\n"
    "• <b>💼 Портфель</b> (/portfolio) — отслеживание ваших монет и реального PnL.\n"
    "• <b>➕ Добавить актив</b> (/add) — внести купленную монету.\n"
    "• <b>🗑 Удалить актив</b> — убрать позицию в 1 клик.\n"
    "• <b>⚙️ Настройки</b> (/settings) — изменить чувствительность и параметры автоскана.\n\n"
    "<b>🧠 Как устроен алгоритм скоринга:</b>\n"
    "Система анализирует 3 таймфрейма (15m, 30m, 1h) по 9 ключевым факторам:\n"
    "1. <b>Объём:</b> всплеск относительно 20-периодной SMA (до 18 баллов).\n"
    "2. <b>Breakout:</b> пробой 20-барного Donchian High (до 15 баллов).\n"
    "3. <b>ATR Expansion:</b> расширение диапазона свечи относительно ATR14 (до 12 баллов).\n"
    "4. <b>Качество свечи:</b> размер тела, положение закрытия, бычий характер (до 12 баллов).\n"
    "5. <b>Taker Buy:</b> агрессия рыночных покупателей (до 10 баллов).\n"
    "6. <b>Тренд EMA:</b> выравнивание Close > EMA 9 > EMA 21 (до 8 баллов).\n"
    "7. <b>RSI 14:</b> фаза импульса без критической перекупленности (до 8 баллов).\n"
    "8. <b>Относительно BTC:</b> опережение динамики биткоина (до 7 баллов).\n"
    "9. <b>Ускорение ROC:</b> нарастание темпа роста (до 5 баллов).\n\n"
    "<b>Категории сигналов:</b>\n"
    "• 🔥 <b>STRONG</b> (Score 74+) — мощный чистый импульс.\n"
    "• ⚡ <b>WATCH</b> (Score 60–73) — зарождающийся импульс под наблюдение.\n"
    "• ⚠️ <b>LATE</b> — сильный скор, но актив уже сильно растянут.\n"
)

# ───────────────────────── Telegram API ─────────────────────────

def api_call(token: str, method: str, payload: dict, timeout: int = 25) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore").lower()
        # Игнорируем штатные незначительные ошибки (устаревший callback, неизмененное сообщение и т.п.)
        benign_errors = (
            "message is not modified",
            "query is too old",
            "query id is invalid",
            "message to edit not found",
            "bot was blocked by the user",
        )
        if not any(be in err_body for be in benign_errors):
            print(f"Telegram API HTTPError ({method}): {e.code} {err_body}", file=sys.stderr)
        return {}
    except Exception as e:
        print(f"Telegram error ({method}): {e}", file=sys.stderr)
        return {}

def drop_pending_updates(token: str) -> int:
    """
    Сбрасывает старые апдейты, накопившиеся в очереди Telegram пока бот был оффлайн.
    Возвращает следующий актуальный offset.
    """
    try:
        body = api_call(token, "getUpdates", {"offset": -1, "timeout": 0}, timeout=10)
        updates = body.get("result", [])
        if updates:
            return updates[-1]["update_id"] + 1
    except Exception:
        pass
    return 0

def send_telegram(
    token: str,
    chat_id: str | int,
    text: str,
    reply_markup: Optional[dict] = None,
    disable_preview: bool = True,
) -> Optional[int]:
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    body = api_call(token, "sendMessage", payload)
    if body.get("ok"):
        return body["result"]["message_id"]

    # Если Telegram жалуется на HTML-разметку (например некорректный тег) - шлем чистым текстом
    err_desc = body.get("description", "")
    if "parse entities" in err_desc.lower():
        payload.pop("parse_mode", None)
        body = api_call(token, "sendMessage", payload)
        if body.get("ok"):
            return body["result"]["message_id"]

    if err_desc and "blocked by the user" not in err_desc.lower():
        print(f"⚠️ Ошибка sendMessage в chat {chat_id}: {err_desc}", file=sys.stderr)
    return None

def edit_message(
    token: str,
    chat_id: str | int,
    message_id: int,
    text: str,
    reply_markup: Optional[dict] = None,
    disable_preview: bool = True,
) -> bool:
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    body = api_call(token, "editMessageText", payload)
    if body.get("ok"):
        return True

    err_desc = body.get("description", "")
    if "parse entities" in err_desc.lower():
        payload.pop("parse_mode", None)
        body = api_call(token, "editMessageText", payload)
        return bool(body.get("ok"))
    return False

def answer_callback(token: str, callback_id: str, text: Optional[str] = None, show_alert: bool = False) -> bool:
    payload: Dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = show_alert
    body = api_call(token, "answerCallbackQuery", payload)
    return bool(body.get("ok"))

def set_bot_commands(token: str) -> bool:
    commands = [
        {"command": "scan", "description": "🔍 Сканировать рынок Binance прямо сейчас"},
        {"command": "portfolio", "description": "💼 Открыть портфель и текущий PnL"},
        {"command": "add", "description": "➕ Добавить монету в портфель"},
        {"command": "settings", "description": "⚙️ Настройки скоринга и автоскана"},
        {"command": "help", "description": "ℹ️ Справка по стратегии и сигналам"},
        {"command": "cancel", "description": "❌ Отменить ввод или действие"},
    ]
    res = api_call(token, "setMyCommands", {"commands": commands})
    return bool(res.get("ok"))

def get_updates(token: str, offset: int, timeout: int = 15) -> List[dict]:
    payload: Dict[str, Any] = {"timeout": timeout}
    if offset > 0:
        payload["offset"] = offset
    body = api_call(token, "getUpdates", payload, timeout=timeout + 10)
    if not body.get("ok"):
        desc = body.get("description", "")
        if desc:
            if "conflict" in desc.lower():
                print(f"⚠️ Telegram webhook активен! Сбрасываю webhook для режима polling...", file=sys.stderr)
                api_call(token, "deleteWebhook", {"drop_pending_updates": False})
            else:
                print(f"⚠️ Ошибка getUpdates: {desc}", file=sys.stderr)
        return []
    return body.get("result", [])

def format_alert(sig: PumpSignal) -> str:
    tf = next((t for t in sig.by_tf if t.timeframe == sig.best_tf), sig.by_tf[0])
    title = {"strong": "🔥 PUMP PULSE", "late": "⚠️ LATE PULSE", "watch": "⚡ WATCH PULSE"}.get(sig.grade, "SIGNAL")
    reasons = "\n".join(f"  ✓ {r}" for r in tf.reasons[:4])
    risks = ""
    if tf.risks:
        risks = "\n<b>Факторы риска:</b>\n" + "\n".join(f"  • {r}" for r in tf.risks[:3])

    vol_str = f"{tf.volume_ratio:.1f}×" if tf.volume_ratio < 100 else ">99×"

    return (
        f"<b>{title} · {sig.base}/USDT</b>\n\n"
        f"🎯 <b>Score:</b> <code>{sig.best_score:.0f}/100</code> | <b>TF:</b> <code>{sig.best_tf}</code>\n"
        f"💵 <b>Цена:</b> <code>{fmt_price(sig.price)} USDT</code>\n"
        f"📊 <b>Суточный рост 24ч:</b> <code>{fmt_pct(sig.change_24h)}</code>\n"
        f"⚡ <b>Относительно BTC:</b> <code>{fmt_pct(sig.btc_relative_24h)}</code>\n"
        f"🌊 <b>Всплеск объёма:</b> <code>{vol_str} SMA20</code>\n"
        f"📈 <b>RSI (14):</b> <code>{tf.rsi:.1f}</code> | <b>Taker Buy:</b> <code>{tf.taker_buy*100:.0f}%</code>\n\n"
        f"<b>Драйверы импульса:</b>\n{reasons}"
        f"{risks}"
    )

def format_factor_breakdown(sig_candidate: dict) -> str:
    return (
        f"<b>ℹ️ Анализ монеты {sig_candidate.get('base', '')}/USDT</b>\n"
        f"• Лучший таймфрейм: <code>{sig_candidate.get('best_tf')}</code>\n"
        f"• Оценка скоринга: <code>{sig_candidate.get('best_score', 0):.0f}/100</code>\n"
        f"• Категория: <code>{sig_candidate.get('grade')}</code>\n"
        f"• Всплеск объёма: <code>{sig_candidate.get('vol_ratio', 0):.1f}×</code>\n"
        f"• Изменение за 24ч: <code>{fmt_pct(sig_candidate.get('change_24h', 0))}</code>\n"
    )

# ───────────────────────── Сканирование с красивым отчётом ─────────────────────────

def execute_scan_and_report(token: str, chat_id: str | int, state: dict) -> None:
    """Выполняет ручной скан и отправляет статус пользователю."""
    status_msg_id = send_telegram(
        token,
        chat_id,
        "🔍 <i>Сканирую спотовый рынок Binance (USDT пары)... Пожалуйста, подождите.</i>"
    )

    try:
        s = state["settings"]
        signals, meta, top_candidates = run_scan(
            min_score=s.get("min_score"),
            min_quote_volume=s.get("min_quote_volume"),
        )

        btc_info = meta.get("btc", {})
        btc_p = btc_info.get("price")
        btc_c = btc_info.get("change24h")
        btc_line = f"BTC: {fmt_price(btc_p)} USDT ({fmt_pct(btc_c)})" if btc_p is not None else ""

        filter_level = s.get("filter_level", "strong_and_watch")
        if filter_level == "strong_only":
            filtered_signals = [sig for sig in signals if sig.grade == "strong"]
        else:
            filtered_signals = [sig for sig in signals if sig.grade in ("strong", "watch")]

        duration_sec = meta["duration_ms"] / 1000.0

        if filtered_signals:
            summary_text = (
                f"✅ <b>Сканирование завершено за {duration_sec:.1f}с</b>\n\n"
                f"• Проверено пар: <code>{meta['universe']}</code>\n"
                f"• Отобрано кандидатов: <code>{meta['candidates']}</code>\n"
                f"• Порог Score: <code>{meta['min_score']:.0f}</code>\n"
                f"• Найдено импульсов: <b>{len(filtered_signals)}</b>\n"
                f"• Рынок: <i>{btc_line}</i>\n\n"
                f"Ниже представлены подробные сигналы:"
            )
            if status_msg_id:
                edit_message(token, chat_id, status_msg_id, summary_text)
            else:
                send_telegram(token, chat_id, summary_text)

            for sig in filtered_signals[:5]:
                card_text = format_alert(sig)
                kb = signal_inline_kb(sig)
                send_telegram(token, chat_id, card_text, reply_markup=kb)
                time.sleep(0.15)

            send_telegram(
                token,
                chat_id,
                f"🔘 Найдено импульсов: <b>{len(filtered_signals)}</b>. Главное меню активно:",
                reply_markup=main_keyboard(),
            )
        else:
            # Если сигналов выше порога нет, показываем ближайших кандидатов (пульс рынка)
            top_lines = []
            for i, c in enumerate(top_candidates[:4], 1):
                top_lines.append(
                    f"{i}. <b>{c['base']}</b> — Score <code>{c['best_score']:.0f}</code> "
                    f"({c['best_tf']}), объём {c['vol_ratio']:.1f}×, 24ч {fmt_pct(c['change_24h'])}"
                )
            cand_block = "\n".join(top_lines) if top_lines else "<i>Нет данных</i>"

            report_text = (
                f"🔍 <b>Результаты сканирования ({duration_sec:.1f}с)</b>\n\n"
                f"• Проверено спотовых пар: <code>{meta['universe']}</code>\n"
                f"• Активных кандидатов: <code>{meta['candidates']}</code>\n"
                f"• Текущий порог сигнала: <code>{meta['min_score']:.0f}</code>\n"
                f"• Сигналов выше порога: <b>0</b> <i>(рынок спокойный)</i>\n"
                f"• Рыночный фон: <i>{btc_line}</i>\n\n"
                f"<b>Ближайшие кандидаты по активности:</b>\n{cand_block}\n\n"
                f"💡 <i>Совет: если хотите видеть более ранние движения, снизьте MIN_SCORE в Настройках до 55.</i>"
            )
            if status_msg_id:
                edit_message(token, chat_id, status_msg_id, report_text)
            else:
                send_telegram(token, chat_id, report_text)

            send_telegram(
                token,
                chat_id,
                "🔘 Сканирование завершено. Главное меню активно:",
                reply_markup=main_keyboard(),
            )
    except Exception as e:
        print(f"❌ Ошибка сканирования: {e}", file=sys.stderr)
        traceback.print_exc()
        send_telegram(
            token,
            chat_id,
            f"⚠️ <b>Ошибка при сканировании рынка:</b>\n<code>{e}</code>\n\nПожалуйста, повторите попытку через минуту.",
            reply_markup=main_keyboard(),
        )

# ───────────────────────── Фоновый автоскан ─────────────────────────

def autoscan_worker(token: str, stop_event: threading.Event) -> None:
    """Фоновый поток периодического сканирования рынка."""
    print("Фоновый поток автосканирования запущен.")
    while not stop_event.is_set():
        try:
            state = load_state()
            settings = state.get("settings", {})
            if not settings.get("autoscan", True):
                stop_event.wait(10)
                continue

            interval = settings.get("scan_interval_sec", DEFAULT_SCAN_INTERVAL)
            min_score = settings.get("min_score", DEFAULT_MIN_SCORE)
            min_vol = settings.get("min_quote_volume", DEFAULT_MIN_QUOTE_VOLUME)
            filter_level = settings.get("filter_level", "strong_and_watch")

            signals, meta, _ = run_scan(min_score=min_score, min_quote_volume=min_vol)

            if filter_level == "strong_only":
                active_signals = [s for s in signals if s.grade == "strong"]
            else:
                active_signals = [s for s in signals if s.grade in ("strong", "watch")]

            now = int(time.time())
            sent_alerts: dict = state.setdefault("sent_alerts", {})
            new_alerts_count = 0

            # Список чатов для оповещения
            env_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
            target_chats: Set[str] = set()
            if env_chat and (env_chat.lstrip("-").isdigit() or env_chat.startswith("@")):
                target_chats.add(env_chat)
            for c in state.get("allowed_chats", []):
                cid_str = str(c).strip()
                if cid_str and cid_str != "12345" and (cid_str.lstrip("-").isdigit() or cid_str.startswith("@")):
                    target_chats.add(cid_str)

            for sig in active_signals:
                if sig.alert_key in sent_alerts:
                    continue

                sent_alerts[sig.alert_key] = now
                new_alerts_count += 1
                text = format_alert(sig)
                kb = signal_inline_kb(sig)

                for cid in target_chats:
                    try:
                        send_telegram(token, cid, text, reply_markup=kb)
                    except Exception as e:
                        print(f"Ошибка отправки алерта в {cid}: {e}", file=sys.stderr)

            if new_alerts_count > 0:
                save_state(state)
                print(f"[Autoscan] Отправлено {new_alerts_count} новых сигналов в {len(target_chats)} чат(ов)")

        except Exception as e:
            print(f"[Autoscan Error]: {e}", file=sys.stderr)

        # Ожидание следующего цикла с возможностью быстрого прерывания
        state_curr = load_state()
        sleep_sec = state_curr.get("settings", {}).get("scan_interval_sec", DEFAULT_SCAN_INTERVAL)
        stop_event.wait(max(15, sleep_sec))

# ───────────────────────── Интерактивный бот ─────────────────────────

def run_bot(token: str) -> None:
    print("Инициализация Telegram-бота Pump Pulse...")
    set_bot_commands(token)

    state = load_state()
    primary_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    allowed_env = [x.strip() for x in os.environ.get("TELEGRAM_ALLOWED_CHATS", "").split(",") if x.strip()]
    if primary_chat and primary_chat not in state["allowed_chats"]:
        state["allowed_chats"].append(primary_chat)
    for c in allowed_env:
        if c not in state["allowed_chats"]:
            state["allowed_chats"].append(c)
    save_state(state)

    # Приветственное сообщение в primary_chat (если настроен)
    if primary_chat:
        send_telegram(
            token,
            primary_chat,
            "🚀 <b>Pump Pulse Scanner 2.0 запущен и готов к работе!</b>\n\n"
            "Используйте кнопки меню ниже для управления ботом.",
            reply_markup=main_keyboard(),
        )

    # Запуск фонового автоскана
    stop_event = threading.Event()
    scan_thread = threading.Thread(target=autoscan_worker, args=(token, stop_event), daemon=True)
    scan_thread.start()

    # Сбрасываем старый webhook при старте, чтобы getUpdates гарантированно работал
    api_call(token, "deleteWebhook", {"drop_pending_updates": False})

    # FSM context: chat_id -> {"state": str, "data": dict}
    user_fsm: Dict[str, dict] = {}
    last_offset = 0

    print("Бот успешно запущен. Ожидание входящих сообщений (long polling)...")

    def is_authorized(chat_id_str: str) -> bool:
        # Если задан явный белый список TELEGRAM_ALLOWED_CHATS, то проверяем его
        if allowed_env:
            return chat_id_str in allowed_env or (primary_chat and chat_id_str == primary_chat)
        return True

    def handle_update(upd: dict) -> None:
        nonlocal state
        # ── 1. Обработка Callback Query (нажатия на Inline-кнопки) ──
        if "callback_query" in upd:
            cb = upd["callback_query"]
            cb_id = cb["id"]
            cb_data = cb.get("data", "")
            sender = cb.get("from", {})
            msg = cb.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", sender.get("id", "")))
            msg_id = msg.get("message_id")
            user_tag = sender.get("username") or sender.get("first_name") or chat_id
            print(f"🔘 [Callback @{user_tag} ({chat_id})]: {cb_data}")

            if not is_authorized(chat_id):
                answer_callback(token, cb_id, "Доступ ограничен.", show_alert=True)
                return

            state = load_state()
            settings = state["settings"]

            # Изменение порога Score
            if cb_data == "score:+5":
                settings["min_score"] = min(95.0, settings["min_score"] + 5)
                save_state(state)
                answer_callback(token, cb_id, f"MIN_SCORE: {settings['min_score']:.0f}")
                if msg_id:
                    edit_message(token, chat_id, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))

            elif cb_data == "score:-5":
                settings["min_score"] = max(45.0, settings["min_score"] - 5)
                save_state(state)
                answer_callback(token, cb_id, f"MIN_SCORE: {settings['min_score']:.0f}")
                if msg_id:
                    edit_message(token, chat_id, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))

            elif cb_data.startswith("score:set:"):
                val = float(cb_data.split(":")[-1])
                settings["min_score"] = val
                save_state(state)
                answer_callback(token, cb_id, f"MIN_SCORE установлен на {val:.0f}")
                if msg_id:
                    edit_message(token, chat_id, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))

            elif cb_data == "autoscan:toggle":
                settings["autoscan"] = not settings.get("autoscan", True)
                save_state(state)
                status_str = "включён" if settings["autoscan"] else "выключен"
                answer_callback(token, cb_id, f"Автоскан {status_str}")
                if msg_id:
                    edit_message(token, chat_id, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))

            elif cb_data.startswith("interval:"):
                sec = int(cb_data.split(":")[-1])
                settings["scan_interval_sec"] = sec
                save_state(state)
                answer_callback(token, cb_id, f"Интервал: {sec // 60} мин")
                if msg_id:
                    edit_message(token, chat_id, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))

            elif cb_data == "filter:toggle":
                cur = settings.get("filter_level", "strong_and_watch")
                settings["filter_level"] = "strong_only" if cur == "strong_and_watch" else "strong_and_watch"
                save_state(state)
                mode_str = "Только Strong 🔥" if settings["filter_level"] == "strong_only" else "Strong + Watch ⚡"
                answer_callback(token, cb_id, f"Фильтр: {mode_str}")
                if msg_id:
                    edit_message(token, chat_id, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))

            elif cb_data == "settings:refresh":
                answer_callback(token, cb_id, "Настройки обновлены")
                if msg_id:
                    edit_message(token, chat_id, msg_id, settings_text(state), reply_markup=settings_inline_kb(state))

            # Управление портфелем через Callback
            elif cb_data == "port:refresh":
                answer_callback(token, cb_id, "Цены обновлены")
                port_text = format_portfolio(state)
                if msg_id:
                    edit_message(token, chat_id, msg_id, port_text, reply_markup=portfolio_inline_kb())

            elif cb_data == "port:add":
                answer_callback(token, cb_id)
                user_fsm[chat_id] = {"state": "waiting_portfolio_input", "data": {}}
                prompt_text = (
                    "➕ <b>Добавление актива в портфель</b>\n\n"
                    "Отправьте сообщение в формате:\n"
                    "<code>ТИКЕР КОЛИЧЕСТВО [ЦЕНА]</code>\n\n"
                    "<b>Примеры:</b>\n"
                    "• <code>SOL 2.5 140.5</code> — 2.5 SOL по $140.5\n"
                    "• <code>BTC 0.05</code> — 0.05 BTC <i>(цена подставится с биржи автоматически!)</i>\n"
                    "• <code>PEPE 500000 0.0000115</code>\n\n"
                    "Для отмены нажмите кнопку <b>«❌ Отмена»</b> ниже."
                )
                send_telegram(token, chat_id, prompt_text, reply_markup=cancel_keyboard())

            elif cb_data == "port:del":
                del_kb = remove_asset_inline_kb(state)
                if del_kb:
                    answer_callback(token, cb_id)
                    if msg_id:
                        edit_message(
                            token, chat_id, msg_id,
                            "🗑 <b>Выберите актив для удаления из портфеля:</b>",
                            reply_markup=del_kb,
                        )
                else:
                    answer_callback(token, cb_id, "Портфель пуст!", show_alert=True)

            elif cb_data.startswith("del:"):
                sym = cb_data.split(":", 1)[1]
                if portfolio_remove(state, sym):
                    answer_callback(token, cb_id, f"{base_asset(sym)} удалён из портфеля", show_alert=True)
                else:
                    answer_callback(token, cb_id, "Актив не найден.")
                port_text = format_portfolio(state)
                if msg_id:
                    edit_message(token, chat_id, msg_id, port_text, reply_markup=portfolio_inline_kb())

            # Быстрое добавление из карточки сигнала
            elif cb_data.startswith("add_coin:"):
                parts = cb_data.split(":")
                sym = parts[1]
                p_val = float(parts[2]) if len(parts) > 2 else (get_price(sym) or 0.0)
                user_fsm[chat_id] = {
                    "state": "waiting_quick_add_qty",
                    "data": {"symbol": sym, "price": p_val}
                }
                answer_callback(token, cb_id)
                send_telegram(
                    token,
                    chat_id,
                    f"➕ <b>Добавление {base_asset(sym)} в портфель</b>\n\n"
                    f"Фиксированная цена: <code>{fmt_price(p_val)} USDT</code>\n"
                    f"Введите желаемое количество монет (например: <code>10</code> или <code>0.5</code>):",
                    reply_markup=cancel_keyboard(),
                )

            elif cb_data.startswith("factors:"):
                parts = cb_data.split(":")
                sym = parts[1]
                tf = parts[2] if len(parts) > 2 else "15m"
                answer_callback(token, cb_id, f"Анализ {base_asset(sym)} ({tf})", show_alert=False)
                send_telegram(
                    token,
                    chat_id,
                    f"📊 <b>Детальный разбор индикаторов {base_asset(sym)}/USDT ({tf})</b>\n\n"
                    f"• Объём vs SMA20: импульс выше среднего\n"
                    f"• Donchian 20: фиксация пробоя максимумов\n"
                    f"• Тренд EMA 9/21: бычья структура\n"
                    f"• Taker Buy Ratio: преобладание покупок по рынку\n"
                    f"• RSI: в рабочем коридоре без перегрева\n\n"
                    f"<i>График доступен по кнопкам под сигналом.</i>"
                )

            elif cb_data == "menu:main":
                user_fsm.pop(chat_id, None)
                answer_callback(token, cb_id)
                send_telegram(
                    token,
                    chat_id,
                    "🔘 <b>Главное меню клавиатуры активно:</b>",
                    reply_markup=main_keyboard(),
                )

            else:
                answer_callback(token, cb_id)
            return

        # ── 2. Обработка обычных сообщений ──
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            return

        raw_text = msg.get("text") or msg.get("caption") or ""
        text = raw_text.strip()
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))
        if not chat_id:
            return

        sender = msg.get("from", {})
        user_tag = sender.get("username") or sender.get("first_name") or chat_id
        if text:
            print(f"📩 [Message @{user_tag} ({chat_id})]: {text}")

        # Автоматически регистрируем chat_id для получения алертов автоскана
        if chat_id not in state["allowed_chats"]:
            state["allowed_chats"].append(chat_id)
            save_state(state)

        if not is_authorized(chat_id):
            print(f"⛔ Доступ запрещён для {chat_id}")
            send_telegram(
                token,
                chat_id,
                "⛔ <b>Доступ ограничен.</b> Ваш Chat ID не авторизован для управления ботом.",
            )
            return

        if not text:
            # Пользователь прислал стикер, фото или файл без текста
            send_telegram(
                token,
                chat_id,
                "👋 Выберите действие в меню ниже:",
                reply_markup=main_keyboard(),
            )
            return

        clean_text = text.strip()
        cmd = clean_text.split()[0].lower().split("@")[0] if clean_text else ""
        text_lower = clean_text.lower()

        # Если нажата любая кнопка главного меню — сбрасываем FSM и сразу выполняем действие
        is_menu_action = (
            cmd in ("/start", "/scan", "/portfolio", "/port", "/settings", "/help", "/del", "/delete", "/cancel")
            or "скан" in text_lower
            or "портфель" in text_lower
            or "настройк" in text_lower
            or "помощь" in text_lower
            or "справка" in text_lower
            or "удалить" in text_lower
            or "отмена" in text_lower
        )
        if is_menu_action and chat_id in user_fsm:
            print(f"-> Сброс FSM для {chat_id} по кнопке меню: {clean_text}")
            user_fsm.pop(chat_id, None)

        # Обработка отмены в любом состоянии
        if cmd == "/cancel" or "отмена" in text_lower:
            user_fsm.pop(chat_id, None)
            print(f"-> Отмена действия для {chat_id}")
            send_telegram(
                token,
                chat_id,
                "❌ Действие отменено. Главное меню активно.",
                reply_markup=main_keyboard(),
            )
            return

        # ── Проверка FSM-состояния пользователя ──
        if chat_id in user_fsm:
            fsm = user_fsm[chat_id]
            cur_st = fsm.get("state")

            if cur_st == "waiting_portfolio_input":
                # Ожидаем ввод: ТИКЕР КОЛИЧЕСТВО [ЦЕНА]
                clean_text = text.replace(",", " ")
                tokens = [t.strip() for t in clean_text.split() if t.strip()]

                if len(tokens) < 2:
                    send_telegram(
                        token,
                        chat_id,
                        "⚠️ Неверный формат. Введите тикер и количество (и при желании цену входа).\n"
                        "Пример: <code>SOL 2.5 140.5</code> или <code>BTC 0.05</code>\n\n"
                        "Для выхода нажмите <b>«❌ Отмена»</b>.",
                        reply_markup=cancel_keyboard(),
                    )
                    return

                raw_sym = tokens[0].upper()
                sym = normalize_symbol(raw_sym)

                try:
                    qty = float(tokens[1])
                    if qty <= 0:
                        raise ValueError
                except ValueError:
                    send_telegram(
                        token,
                        chat_id,
                        "⚠️ Ошибка: количество должно быть положительным числом.\nПопробуйте ещё раз:",
                        reply_markup=cancel_keyboard(),
                    )
                    return

                if len(tokens) >= 3:
                    try:
                        price = float(tokens[2])
                        if price <= 0:
                            raise ValueError
                    except ValueError:
                        send_telegram(
                            token,
                            chat_id,
                            "⚠️ Ошибка: цена должна быть положительным числом.\nПопробуйте ещё раз:",
                            reply_markup=cancel_keyboard(),
                        )
                        return
                else:
                    # Автоматическое получение текущей цены с Binance
                    mkt_price = get_price(sym)
                    if mkt_price is None:
                        send_telegram(
                            token,
                            chat_id,
                            f"⚠️ Монета <code>{sym}</code> не найдена на споте Binance. "
                            f"Проверьте правильность тикера или укажите цену вручную (например: <code>{raw_sym} {qty} 10.5</code>).",
                            reply_markup=cancel_keyboard(),
                        )
                        return
                    price = mkt_price

                new_qty, new_avg = portfolio_add(state, sym, qty, price)
                user_fsm.pop(chat_id, None)

                send_telegram(
                    token,
                    chat_id,
                    f"✅ <b>Актив успешно сохранён в портфель!</b>\n\n"
                    f"• Монета: <b>{base_asset(sym)}/USDT</b>\n"
                    f"• Количество: <code>{fmt_qty(new_qty)}</code>\n"
                    f"• Средняя цена входа: <code>{fmt_price(new_avg)} USDT</code>\n"
                    f"• Сумма позиции: <code>{(new_qty * new_avg):,.2f} USDT</code>",
                    reply_markup=main_keyboard(),
                )
                # Показываем обновленный портфель
                send_telegram(token, chat_id, format_portfolio(state), reply_markup=portfolio_inline_kb())
                return

            elif cur_st == "waiting_quick_add_qty":
                try:
                    qty = float(text.replace(",", ".").strip())
                    if qty <= 0:
                        raise ValueError
                except ValueError:
                    send_telegram(
                        token,
                        chat_id,
                        "⚠️ Введите корректное положительное число (например, <code>5</code> или <code>0.25</code>):",
                        reply_markup=cancel_keyboard(),
                    )
                    return

                sym = fsm["data"]["symbol"]
                price = fsm["data"]["price"]
                new_qty, new_avg = portfolio_add(state, sym, qty, price)
                user_fsm.pop(chat_id, None)

                send_telegram(
                    token,
                    chat_id,
                    f"✅ <b>Позиция {base_asset(sym)} добавлена!</b>\n\n"
                    f"• Количество: <code>{fmt_qty(new_qty)}</code>\n"
                    f"• Вход: <code>{fmt_price(new_avg)} USDT</code>",
                    reply_markup=main_keyboard(),
                )
                send_telegram(token, chat_id, format_portfolio(state), reply_markup=portfolio_inline_kb())
                return

        # ── Основные команды и Reply-кнопки ──
        if cmd == "/start" or text_lower in ("/start", "start"):
            print(f"-> Отправка приветствия для {chat_id}")
            welcome_text = (
                "👋 <b>Добро пожаловать в Pump Pulse Scanner 2.0!</b>\n\n"
                "Я непрерывно сканирую спотовый рынок Binance на предмет зарождения "
                "мощных импульсов (пампов) с помощью multi-TF скоринга объёма, "
                "пробоев Donchian, ATR, RSI и покупательской агрессии.\n\n"
                "<b>Выберите действие в меню ниже:</b>"
            )
            send_telegram(token, chat_id, welcome_text, reply_markup=main_keyboard())

        elif cmd == "/scan" or "скан" in text_lower:
            print(f"-> Запуск ручного сканирования для {chat_id}")
            execute_scan_and_report(token, chat_id, state)

        elif cmd in ("/portfolio", "/port") or "портфель" in text_lower:
            print(f"-> Отправка портфеля для {chat_id}")
            port_text = format_portfolio(state)
            send_telegram(token, chat_id, port_text, reply_markup=portfolio_inline_kb())

        elif cmd == "/add" or "добавить" in text_lower:
            args = clean_text.split()[1:]
            if len(args) >= 2:
                raw_sym = args[0].upper()
                sym = normalize_symbol(raw_sym)
                try:
                    qty = float(args[1])
                    price = float(args[2]) if len(args) >= 3 else (get_price(sym) or 0.0)
                    if qty <= 0 or price <= 0:
                        raise ValueError
                    new_qty, new_avg = portfolio_add(state, sym, qty, price)
                    print(f"-> Добавлен актив {sym} для {chat_id}")
                    send_telegram(
                        token,
                        chat_id,
                        f"✅ <b>Актив {base_asset(sym)} успешно добавлен!</b>\n\n"
                        f"• Количество: <code>{fmt_qty(new_qty)}</code>\n"
                        f"• Вход: <code>{fmt_price(new_avg)} USDT</code>\n"
                        f"• Сумма позиции: <code>{(new_qty * new_avg):,.2f} USDT</code>",
                        reply_markup=main_keyboard(),
                    )
                    send_telegram(token, chat_id, format_portfolio(state), reply_markup=portfolio_inline_kb())
                except Exception:
                    send_telegram(token, chat_id, "⚠️ Ошибка формата. Пример: <code>/add SOL 2.5 140.5</code>", reply_markup=main_keyboard())
            else:
                print(f"-> Диалог добавления актива для {chat_id}")
                user_fsm[chat_id] = {"state": "waiting_portfolio_input", "data": {}}
                prompt_text = (
                    "➕ <b>Добавление актива в портфель</b>\n\n"
                    "Отправьте тикер, количество и цену (опционально):\n"
                    "<code>ТИКЕР КОЛИЧЕСТВО [ЦЕНА]</code>\n\n"
                    "<b>Примеры:</b>\n"
                    "• <code>SOL 2.5 140.5</code> — 2.5 SOL по $140.5\n"
                    "• <code>BTC 0.05</code> — текущая цена возьмётся с Binance!\n\n"
                    "Или нажмите <b>«❌ Отмена»</b>."
                )
                send_telegram(token, chat_id, prompt_text, reply_markup=cancel_keyboard())

        elif cmd in ("/del", "/delete") or "удалить" in text_lower:
            print(f"-> Диалог удаления для {chat_id}")
            del_kb = remove_asset_inline_kb(state)
            if del_kb:
                send_telegram(
                    token,
                    chat_id,
                    "🗑 <b>Выберите актив для удаления:</b>",
                    reply_markup=del_kb,
                )
            else:
                send_telegram(
                    token,
                    chat_id,
                    "💼 <b>Портфель пуст!</b> Нечего удалять.",
                    reply_markup=main_keyboard(),
                )

        elif cmd == "/settings" or "настройк" in text_lower:
            print(f"-> Открытие настроек для {chat_id}")
            send_telegram(
                token,
                chat_id,
                settings_text(state),
                reply_markup=settings_inline_kb(state),
            )

        elif cmd == "/help" or "помощь" in text_lower or "справка" in text_lower:
            print(f"-> Отправка справки для {chat_id}")
            send_telegram(token, chat_id, HELP_TEXT, reply_markup=main_keyboard())

        else:
            print(f"-> Запрос инфо по тикеру: {clean_text}")
            cand_sym = normalize_symbol(clean_text)
            pr = get_price(cand_sym)
            if pr is not None:
                base = base_asset(cand_sym)
                info_text = (
                    f"🪙 <b>Актив: {base}/USDT</b>\n"
                    f"• Текущая цена: <code>{fmt_price(pr)} USDT</code>\n\n"
                    f"Для управления используйте кнопки меню ниже."
                )
                send_telegram(token, chat_id, info_text, reply_markup=main_keyboard())
            else:
                send_telegram(
                    token,
                    chat_id,
                    "Команда не распознана. Воспользуйтесь кнопками меню или /help.",
                    reply_markup=main_keyboard(),
                )

    try:
        while True:
            try:
                updates = get_updates(token, offset=last_offset, timeout=20)
            except Exception as e:
                print(f"⚠️ Ошибка сети при getUpdates: {e}", file=sys.stderr)
                time.sleep(2)
                continue

            for upd in updates:
                last_offset = upd.get("update_id", last_offset) + 1
                try:
                    handle_update(upd)
                except Exception as e:
                    print(f"❌ Ошибка обработки обновления {upd.get('update_id')}: {e}", file=sys.stderr)
                    traceback.print_exc()

    except KeyboardInterrupt:
        print("\nОстановка бота по сигналу пользователя...")
    finally:
        stop_event.set()
        save_state(state)
        print("Состояние сохранено. Бот остановлен.")

# ───────────────────────── Режим Oneshot (CLI / Cron) ─────────────────────────

def run_oneshot(token: Optional[str], chat_id: Optional[str]) -> None:
    print("=== Режим разового сканирования (Oneshot) ===")
    state = load_state()
    settings = state.get("settings", {})
    min_score = settings.get("min_score", DEFAULT_MIN_SCORE)
    min_vol = settings.get("min_quote_volume", DEFAULT_MIN_QUOTE_VOLUME)
    filter_level = settings.get("filter_level", "strong_and_watch")

    signals, meta, summaries = run_scan(
        min_score=min_score,
        min_quote_volume=min_vol,
    )

    if filter_level == "strong_only":
        active_signals = [s for s in signals if s.grade == "strong"]
    else:
        active_signals = [s for s in signals if s.grade in ("strong", "watch", "late")]

    duration_sec = meta["duration_ms"] / 1000.0
    print(
        f"Сканирование завершено за {duration_sec:.1f}с: пар={meta['universe']}, "
        f"отобрано={meta['candidates']}, активных сигналов={len(active_signals)}"
    )

    if token and chat_id:
        now = int(time.time())
        sent_alerts: dict = state.setdefault("sent_alerts", {})
        sent_count = 0

        target_chats: Set[str] = set()
        if chat_id and (chat_id.lstrip("-").isdigit() or chat_id.startswith("@")):
            target_chats.add(chat_id)
        for c in state.get("allowed_chats", []):
            cid_str = str(c).strip()
            if cid_str and cid_str != "12345" and (cid_str.lstrip("-").isdigit() or cid_str.startswith("@")):
                target_chats.add(cid_str)

        for sig in active_signals:
            if sig.alert_key in sent_alerts:
                print(f"• Пропуск {sig.symbol} ({sig.best_tf}): алерт для этого бара уже был отправлен ранее")
                continue

            sent_alerts[sig.alert_key] = now
            sent_count += 1
            text = format_alert(sig)
            kb = signal_inline_kb(sig)

            for cid in target_chats:
                ok = send_telegram(token, cid, text, reply_markup=kb)
                print(f"• {sig.symbol} ({sig.grade} {sig.best_score:.0f}) → {cid}: {'sent' if ok else 'fail'}")

        if sent_count > 0:
            save_state(state)
            print(f"Успешно отправлено новых сигналов: {sent_count}")
        else:
            print("Новых уникальных сигналов выше порога не обнаружено.")

            # Если запуск был выполнен вручную (workflow_dispatch) или включен NOTIFY_EMPTY, отправляем отчет о спокойном рынке
            is_manual = (
                os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
                or os.environ.get("NOTIFY_EMPTY", "false").lower() in ("true", "1")
                or "--notify-always" in sys.argv
            )
            if is_manual:
                top_cand_str = ""
                if summaries:
                    top_cand_str = "\n".join(
                        f"  • {c['base']}: Score {c['best_score']:.0f} ({c['best_tf']}), объём {c['vol_ratio']:.1f}×"
                        for c in summaries[:3]
                    )
                btc_info = meta.get("btc", {})
                btc_p = btc_info.get("price")
                btc_c = btc_info.get("change24h")
                btc_str = f"BTC: {fmt_price(btc_p)} USDT ({fmt_pct(btc_c)})" if btc_p is not None else ""

                status_msg = (
                    f"🔍 <b>Pump Pulse Scanner — Отчёт сканирования</b>\n\n"
                    f"• Проверено спотовых пар: <code>{meta['universe']}</code>\n"
                    f"• Порог сигнала (Score): <code>{min_score:.0f}</code>\n"
                    f"• Сигналов выше порога: <b>0</b> <i>(рынок спокоен)</i>\n"
                    f"• Фон: <i>{btc_str}</i>\n\n"
                    f"<b>Ближайшие кандидаты по активности:</b>\n{top_cand_str or 'Нет данных'}"
                )
                for cid in target_chats:
                    send_telegram(token, cid, status_msg)
                print(f"Отправлен статус-отчёт о сканировании в Telegram.")
    else:
        for s in active_signals:
            print(f"[{s.grade.upper()}] {s.symbol} Score: {s.best_score:.0f} Price: {s.price} 24h: {s.change_24h:+.2f}%")

def run_test_connection(token: str, chat_id: str) -> None:
    """Тест подключения бота к Telegram API и отправка тестового сообщения."""
    print("=== Проверка подключения к Telegram API ===")
    if not token:
        print("❌ Ошибка: TELEGRAM_BOT_TOKEN не задан!", file=sys.stderr)
        return
    me = api_call(token, "getMe", {})
    if not me.get("ok"):
        print(f"❌ Ошибка токена бота: {me.get('description')}", file=sys.stderr)
        return
    bot_info = me["result"]
    print(f"✅ Бот авторизован: @{bot_info.get('username')} ({bot_info.get('first_name')})")

    if not chat_id:
        print("⚠️ Внимание: TELEGRAM_CHAT_ID не задан!", file=sys.stderr)
        return

    test_msg = (
        f"🔔 <b>Тестовое уведомление от Pump Pulse Scanner!</b>\n\n"
        f"Бот <b>@{bot_info.get('username')}</b> успешно подключён к вашему чату.\n"
        f"• Chat ID: <code>{chat_id}</code>\n"
        f"• Статус: 🟢 <b>Связь установлена, бот готов к отправке сигналов!</b>"
    )
    res = send_telegram(token, chat_id, test_msg)
    if res:
        print(f"✅ Тестовое сообщение успешно доставлено в chat_id {chat_id} (msg_id: {res})!")
    else:
        print(
            f"❌ Не удалось отправить сообщение в chat_id {chat_id}.\n"
            f"   Возможные причины:\n"
            f"   1. Вы не нажали кнопку Start (/start) в диалоге с ботом в Telegram.\n"
            f"   2. Указан неверный chat_id (нужен цифровой ID, а не @username).\n"
            f"   3. Если это канал/группа — бот должен быть добавлен туда администратором.",
            file=sys.stderr,
        )

# ───────────────────────── Main ─────────────────────────

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    # Проверка тестового режима
    if "--test" in sys.argv:
        run_test_connection(token, chat_id)
        return

    # Проверка аргументов командной строки: --oneshot или --bot
    mode = RUN_MODE
    if "--oneshot" in sys.argv:
        mode = "oneshot"
    elif "--bot" in sys.argv:
        mode = "bot"

    if mode == "oneshot":
        run_oneshot(token, chat_id)
        return

    # Режим интерактивного бота
    if not token:
        print(
            "\n"
            "⚠️  Внимание: TELEGRAM_BOT_TOKEN не задан!\n"
            "   Для работы Telegram-бота необходимо указать токен бота.\n"
            "   Вы можете создать файл .env на основе .env.example:\n"
            "      TELEGRAM_BOT_TOKEN=ваш_токен_от_BotFather\n"
            "      TELEGRAM_CHAT_ID=ваш_chat_id\n\n"
            "   Либо передать их через переменные окружения.\n"
            "   Для проверки логики сканера в терминале запустите:\n"
            "      python pump_bot.py --oneshot\n",
            file=sys.stderr,
        )
        sys.exit(1)

    run_bot(token)

if __name__ == "__main__":
    main()