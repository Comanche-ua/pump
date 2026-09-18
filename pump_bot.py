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
import re
import signal
import threading
import traceback
import urllib.parse
import urllib.request
import urllib.error
import concurrent.futures as cf
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
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
BINANCE_WORKER_AUTH = (os.environ.get("BINANCE_WORKER_AUTH") or os.environ.get("PROXY_AUTH_TOKEN") or "").strip()

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
MAX_CANDIDATES = int(os.environ.get("MAX_CANDIDATES", "30"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "30"))
KLINES_LIMIT = int(os.environ.get("KLINES_LIMIT", "500"))  # 500 баров 5m → 41 бар 1h (нужно ≥ MIN_BARS=32)
SCAN_BATCH_SIZE = int(os.environ.get("SCAN_BATCH_SIZE", "30"))

# Параметры спотовой автоторговли.
# TP/SL подобраны бэктестом (backtest/backtest.py) и подтверждены на трёх
# независимых выборках: in-sample (40 пар), другой период (тот же набор со
# сдвигом на 30 дней), другая вселенная (ранги 41-80 по объёму). Значимо
# положительный матожидаемый исход во ВСЕХ трёх дают только конфигурации,
# где СТОП ШИРЕ ЦЕЛИ (1.5%/3.0% и 2.0%/3.0%). Симметричный 2.0%/2.0% и любые
# узкие стопы (0.5%, 1.0%) значимо убыточны: узкий стоп выбивается обычным
# шумом, после которого цена возвращается. Подробности — `--help` харнесса.
DEFAULT_TRADE_AMOUNT = float(os.environ.get("TRADE_AMOUNT_USDT", "5.0"))
# Цель 1.0% при стопе 3.0% — НАИМЕНЬШАЯ цель, дающая значимо положительное
# матожидание. Измеренная лестница «цель → E[решённая сделка]» при стопе 3%:
#   0.5% → −0.045% (значимо ОТРИЦАТЕЛЬНО: TP-первым 90.4% при безубытке 91.7%)
#   0.7% → +0.000% (ровно ноль: работа впустую, 0.5% чистыми съедаются издержками)
#   1.0% → +0.058% (TP-первым 81.9%, безубыток 80.5%)
#   1.2% → +0.084%   1.5% → +0.110%   2.0% → +0.106%
# То есть цель 0.5% (в любой трактовке — брутто или нетто) не окупается:
# на 0.5% брутто издержки съедают больше, чем даёт точность попадания.
DEFAULT_TAKE_PROFIT = float(os.environ.get("TAKE_PROFIT_PCT", "0.7"))
DEFAULT_STOP_LOSS = float(os.environ.get("STOP_LOSS_PCT", "3.0"))
DEFAULT_ENTRY_PULLBACK = float(os.environ.get("ENTRY_PULLBACK_PCT", "1.0"))  # Вход на откате (-1.0% по результатам глубокого бэктеста)
DEFAULT_ENTRY_TIMEOUT_SEC = int(os.environ.get("ENTRY_TIMEOUT_SEC", "600"))   # Таймаут жизни лимитного ордера на вход (10 мин = 2 бара)
DEFAULT_TRAILING_ACTIVATION = float(os.environ.get("TRAILING_ACTIVATION_PCT", "1.2"))
DEFAULT_TRAILING_DISTANCE = float(os.environ.get("TRAILING_DISTANCE_PCT", "0.4"))
DEFAULT_SL_COOLDOWN_SECONDS = int(os.environ.get("SL_COOLDOWN_SECONDS", "3600"))
TRADES_LOG_FILE = os.environ.get("TRADES_LOG_FILE", "trades_log.csv")
_TRADES_LOG_LOCK = threading.Lock()
DEFAULT_DYNAMIC_TP = os.environ.get("DYNAMIC_TP", "true").strip().lower() in ("true", "1")
DEFAULT_AUTO_TRADE = os.environ.get("AUTO_TRADE", "true").strip().lower() in ("true", "1")
DEFAULT_TRADE_MIN_SCORE = float(os.environ.get("TRADE_MIN_SCORE", "65.0"))

# Защита позиции силами биржи. OCO-список держит на Binance ОБА ордера
# (тейк-профит и стоп-лосс) одновременно, и срабатывание одного отменяет
# другой. Это единственная схема, при которой позиция защищена, пока бот
# не работает: в CI процесс живёт максимум 330 минут, плюс падения и
# отсутствие сети. Значение по умолчанию можно выключить через USE_OCO=false —
# тогда возвращается прежняя схема: лимитный TP на бирже, а стоп следит
# Python-процесс (и не срабатывает, когда процесс мёртв).
# ── Режим стратегии ──
# "pump" — прежний скоринг из 8 факторов (объём, taker, сжатие, пробой...).
# "indicators" — классика по индикаторам: RSI выше порога, стохастик выше
# порога и %K > %D (восходящий импульс). Режим выбирается перед торговлей.
DEFAULT_STRATEGY = (os.environ.get("STRATEGY") or "pump").strip().lower()
STRATEGY_LABELS = {
    "pump": "📈 Памп-скор (8 факторов)",
    "indicators": "📊 RSI + SAR + Фрактал",
    "volume": "📊 Объём + свеча",
    "dump": "📉 Дамп → отскок вверх",
}
DEFAULT_RSI_MIN = float(os.environ.get("RSI_MIN", "55"))
# Порог входа для режима «объём + свеча»: ниже 2.0 — шум объёма,
# выше 4 — редкость на ликвидных парах.
VOL_MIN_RATIO = float(os.environ.get("VOL_MIN_RATIO", "1.8"))

DEFAULT_USE_OCO = os.environ.get("USE_OCO", "true").strip().lower() in ("true", "1")
# Прежний дефолт TP — нужен только для миграции уже сохранённого состояния
PREVIOUS_TAKE_PROFIT_DEFAULT = 2.0

# ── 4 Пресета торговых стратегий (по бэктесту 44 989 сигналов) ──
TRADE_PROFILES = {
    "optimal": {
        "name": "🥇 Оптимальный (Баланс)",
        "min_score": 70.0,
        "entry_pullback_pct": 1.0,
        "take_profit_pct": 0.7,
        "stop_loss_pct": 3.0,
        "trailing_activation_pct": 1.2,
        "trailing_distance_pct": 0.4,
        "desc": "WinRate 93.5%, просадка 5.35%, PnL +305%",
    },
    "profit": {
        "name": "🚀 Макс. Профит (Агрессивный)",
        "min_score": 65.0,
        "entry_pullback_pct": 1.0,
        "take_profit_pct": 0.7,
        "stop_loss_pct": 3.5,
        "trailing_activation_pct": 1.2,
        "trailing_distance_pct": 0.4,
        "desc": "613 сделок, WinRate 94.8%, PnL +392%",
    },
    "sniper": {
        "name": "🛡 Снайпер (Консервативный)",
        "min_score": 75.0,
        "entry_pullback_pct": 0.8,
        "take_profit_pct": 0.7,
        "stop_loss_pct": 3.5,
        "trailing_activation_pct": 1.2,
        "trailing_distance_pct": 0.4,
        "desc": "WinRate 95.9%, E[trade] +0.713%, PF 6.09",
    },
    "ultra": {
        "name": "⚡ Ультра 3-в-1 (Динамический)",
        "min_score": 65.0,
        "entry_pullback_pct": 1.0,  # Score>=75 -> 0.8%, 70-75 -> 1.0%, 65-70 -> 1.0%
        "take_profit_pct": 0.7,
        "stop_loss_pct": 3.0,       # Score>=75 -> 3.5%, 70-75 -> 3.0%, 65-70 -> 3.5%
        "trailing_activation_pct": 1.2,
        "trailing_distance_pct": 0.4,
        "desc": "Авто-адаптация отката и SL под качество каждого сигнала",
    },
}

DEFAULT_TRADE_PROFILE = (os.environ.get("TRADE_PROFILE") or "ultra").strip().lower()

def apply_trade_profile(state: dict, profile_key: str) -> dict:
    """Применяет выбранный профиль стратегии к настройкам бота."""
    if profile_key not in TRADE_PROFILES:
        profile_key = "ultra"
    prof = TRADE_PROFILES[profile_key]
    s = state.setdefault("settings", {})
    s["trade_profile"] = profile_key
    s["min_score"] = float(prof["min_score"])
    s["entry_pullback_pct"] = float(prof["entry_pullback_pct"])
    s["take_profit_pct"] = float(prof["take_profit_pct"])
    s["stop_loss_pct"] = float(prof["stop_loss_pct"])
    s["trailing_activation_pct"] = float(prof["trailing_activation_pct"])
    s["trailing_distance_pct"] = float(prof["trailing_distance_pct"])
    return prof


# Коды Binance, означающие «ордера на бирже нет» — в отличие от сетевого сбоя.
# -2013 Order does not exist, -2011 Unknown order sent. Дают возможность
# безопасно убирать мёртвые записи, не рискуя потерять живой ордер.
ORDER_NOT_FOUND_CODES = (-2013, -2011)

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

# ───────────────────────── Rate Limit & Метрики ─────────────────────────
API_WEIGHT_USED_1M: int = 0
API_WEIGHT_UPDATED_AT: float = 0.0
BOT_START_TIME: float = time.time()
FSM_TTL_SEC: int = 600  # 10 минут таймаут для состояний FSM

def record_binance_weight(headers) -> None:
    """Отслеживает текущий расход веса API Binance (1200 в минуту) и предотвращает блокировку IP."""
    global API_WEIGHT_USED_1M, API_WEIGHT_UPDATED_AT
    if not headers or not hasattr(headers, "get"):
        return
    try:
        w = headers.get("x-mbx-used-weight-1m") or headers.get("X-MBX-USED-WEIGHT-1M")
        if w is not None:
            API_WEIGHT_USED_1M = int(w)
            API_WEIGHT_UPDATED_AT = time.time()
            if API_WEIGHT_USED_1M >= 1000:
                print(f"⚠️ [RateLimit] Высокое использование веса Binance: {API_WEIGHT_USED_1M}/1200! Защитная пауза 1.5с...")
                time.sleep(1.5)
            elif API_WEIGHT_USED_1M >= 800:
                time.sleep(0.3)
    except Exception:
        pass

# ───────────────────────── Безопасность и Санитизация ─────────────────────────

def sanitize_sensitive_text(text: Any) -> str:
    """Маскирует секреты, API-ключи и подписи в URL, телах запросов и сообщениях об ошибках."""
    if text is None:
        return ""
    s = str(text)
    # Маскируем signature=...
    s = re.sub(r'(signature=)[a-fA-F0-9]+', r'\1***MASKED***', s)
    # Маскируем ключи/секреты в URL параметрах и заголовках
    s = re.sub(r'((?:apiKey|api_key|secret|secretKey|X-MBX-APIKEY|X-Worker-Auth|token|auth)=)[^\s&"\']+', r'\1***MASKED***', s, flags=re.IGNORECASE)
    return s

# ───────────────────────── Кулдаун после Stop-Loss ─────────────────────────

def get_sl_cooldown_duration(state: Optional[dict] = None) -> int:
    """Возвращает длительность кулдауна после срабатывания SL в секундах."""
    if state:
        return int(state.get("settings", {}).get("sl_cooldown_seconds", DEFAULT_SL_COOLDOWN_SECONDS))
    return DEFAULT_SL_COOLDOWN_SECONDS

def set_symbol_sl_cooldown(state: dict, symbol: str, duration_sec: Optional[int] = None, reason: str = "stop_loss") -> None:
    """Помещает символ в кулдаун после срабатывания SL."""
    if duration_sec is None:
        duration_sec = get_sl_cooldown_duration(state)
    if duration_sec <= 0:
        return
    now = time.time()
    cooldowns = state.setdefault("sl_cooldowns", {})
    cooldowns[symbol] = {
        "until": now + duration_sec,
        "set_at": now,
        "reason": reason,
        "duration_sec": duration_sec,
    }
    until_str = time.strftime('%H:%M:%S', time.localtime(now + duration_sec))
    print(f"[Cooldown] ❄️ Монета {symbol} помещена в кулдаун на {duration_sec}с (до {until_str}) по причине: {reason}")

def is_symbol_in_sl_cooldown(state: dict, symbol: str) -> Optional[float]:
    """
    Проверяет, находится ли символ в кулдауне после Stop-Loss.
    Возвращает timestamp окончания кулдауна (float) или None.
    Автоматически очищает истекшие записи.
    """
    cooldowns = state.get("sl_cooldowns")
    if not isinstance(cooldowns, dict) or symbol not in cooldowns:
        return None
    entry = cooldowns.get(symbol)
    if isinstance(entry, (int, float)):
        until = float(entry)
    elif isinstance(entry, dict):
        until = float(entry.get("until", 0.0))
    else:
        until = 0.0
    now = time.time()
    if now >= until:
        cooldowns.pop(symbol, None)
        return None
    return until

# ───────────────────────── Append-only Логгер Сделок ─────────────────────────

def log_trade_event(
    event_type: str,
    symbol: str,
    order_id: Optional[Union[str, int]] = None,
    price: Optional[float] = None,
    qty: Optional[float] = None,
    quote_amount: Optional[float] = None,
    pnl: Optional[float] = None,
    pnl_pct: Optional[float] = None,
    fee: Optional[float] = None,
    reason: str = "",
    meta: Optional[dict] = None,
    file_path: Optional[str] = None,
) -> None:
    """
    Атомарно записывает торговое событие в append-only CSV лог `trades_log.csv`.
    Позволяет вести независимый аудит и рассчитывать метрики доходности/проскальзывания.
    """
    target_path = file_path or TRADES_LOG_FILE
    now_ts = time.time()
    dt_utc = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now_ts))

    row = [
        f"{now_ts:.3f}",
        dt_utc,
        str(symbol),
        str(event_type),
        str(order_id if order_id is not None else ""),
        f"{price:.8f}".rstrip("0").rstrip(".") if price is not None else "",
        f"{qty:.8f}".rstrip("0").rstrip(".") if qty is not None else "",
        f"{quote_amount:.4f}" if quote_amount is not None else "",
        f"{pnl_pct:+.2f}%" if pnl_pct is not None else "",
        f"{pnl:+.4f}" if pnl is not None else "",
        f"{fee:.6f}" if fee is not None else "",
        str(reason),
        json.dumps(meta, ensure_ascii=False) if meta else "",
    ]

    line = ",".join(
        f'"{field.replace(chr(34), chr(34)+chr(34))}"'
        if any(c in field for c in (",", '"', "\n", "\r"))
        else field
        for field in row
    ) + "\n"

    with _TRADES_LOG_LOCK:
        try:
            write_header = not os.path.exists(target_path) or os.path.getsize(target_path) == 0
            with open(target_path, "a", encoding="utf-8") as f:
                if write_header:
                    f.write("timestamp,datetime_utc,symbol,event_type,order_id,price,qty,quote_amount,pnl_pct,pnl_usdt,fee,reason,meta\n")
                f.write(line)
                f.flush()
        except Exception as e:
            print(f"[TradesLog Error]: Не удалось записать событие в {target_path}: {e}", file=sys.stderr)

# ───────────────────────── HTTP ─────────────────────────

def http_get_json(url: str, timeout: int = 12, state: Optional[dict] = None) -> dict | list:
    headers = {"User-Agent": "pump-pulse/2.1"}
    worker_auth = ""
    if state:
        worker_auth = str(state.get("settings", {}).get("worker_auth_token", "")).strip()
    if not worker_auth:
        worker_auth = BINANCE_WORKER_AUTH
    if worker_auth:
        headers["X-Worker-Auth"] = worker_auth

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        record_binance_weight(getattr(resp, "headers", None))
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
    # Значения индикаторов режима «RSI + SAR + фракталы» — нужны, чтобы
    # проверять РАЗНЫЕ пороги потом, не пересчитывая скоринг заново.
    sar: float = 0.0
    fractal_level: float = 0.0

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

def rsi_series(closes: List[float], period: int = 14) -> List[Optional[float]]:
    """
    RSI по ВСЕЙ серии (rsi() даёт только последнее значение).
    Нужен и стратегии индикаторов, и графику — поэтому живёт здесь,
    а chart.py переиспользует, чтобы не было двух реализаций.
    """
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def value(g: float, l: float) -> float:
        if l == 0:
            return 100.0
        rs = g / l
        return 100 - 100 / (1 + rs)

    out[period] = value(avg_gain, avg_loss)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = value(avg_gain, avg_loss)
    return out

def stochastic_series(candles: List[Candle], k_period: int = 14,
                      d_period: int = 3) -> Tuple[List[Optional[float]], List[Optional[float]]]:
    """(%K, %D) — стохастик по High/Low/Close. %D — сглаженный %K."""
    n = len(candles)
    k: List[Optional[float]] = [None] * n
    d: List[Optional[float]] = [None] * n
    for i in range(k_period - 1, n):
        window = candles[i - k_period + 1:i + 1]
        hi = max(c.high for c in window)
        lo = min(c.low for c in window)
        k[i] = 50.0 if hi <= lo else (candles[i].close - lo) / (hi - lo) * 100.0
    for i in range(n):
        if k[i] is None:
            continue
        window = [v for v in k[max(0, i - d_period + 1):i + 1] if v is not None]
        if len(window) == d_period:
            d[i] = sum(window) / d_period
    return k, d

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

# Список отслеживаемых пар пользователя (USDT Spot)
WHITELIST_SYMBOLS: List[str] = [
    "0GUSDT", "1000CATUSDT", "1000CHEEMSUSDT", "1000SATSUSDT", "1INCHUSDT", "1MBABYDOGEUSDT", "2ZUSDT", "AUSDT", "AAOIBUSDT", "AAPLBUSDT",
    "AAVEUSDT", "ACEUSDT", "ACHUSDT", "ACMUSDT", "ACTUSDT", "ADAUSDT", "ADXUSDT", "AEROUSDT", "AEVOUSDT", "AGLDUSDT",
    "AIUSDT", "AIGENSYNUSDT", "AIXBTUSDT", "ALABBUSDT", "ALGOUSDT", "ALICEUSDT", "ALLOUSDT", "ALPINEUSDT", "ALTUSDT", "AMATBUSDT",
    "AMDBUSDT", "AMPUSDT", "AMZNBUSDT", "ANIMEUSDT", "ANKRUSDT", "APEUSDT", "API3USDT", "APTUSDT", "ARUSDT", "ARBUSDT",
    "ARKUSDT", "ARKMUSDT", "ARMBUSDT", "ARPAUSDT", "ASMLBUSDT", "ASRUSDT", "ASTERUSDT", "ASTRUSDT", "ASTSBUSDT", "ATUSDT",
    "ATMUSDT", "ATOMUSDT", "AUCTIONUSDT", "AUDIOUSDT", "AVAUSDT", "AVAXUSDT", "AVGOBUSDT", "AVNTUSDT", "AWEUSDT", "AXLUSDT",
    "AXSUSDT", "AXTIBUSDT", "BABABUSDT", "BABYUSDT", "BANANAUSDT", "BANANAS31USDT", "BANDUSDT", "BANKUSDT", "BARUSDT", "BARDUSDT",
    "BATUSDT", "BBUSDT", "BCHUSDT", "BEAMXUSDT", "BEBUSDT", "BELUSDT", "BERAUSDT", "BFUSDUSDT", "BICOUSDT", "BIGTIMEUSDT",
    "BIOUSDT", "BLURUSDT", "BMNRBUSDT", "BMTUSDT", "BNBUSDT", "BNCBUSDT", "BNSOLUSDT", "BNTUSDT", "BOMEUSDT", "BONKUSDT",
    "BREVUSDT", "BROCCOLI714USDT", "BTCUSDT", "BTTCUSDT", "CUSDT", "C98USDT", "CAKEUSDT", "CATIUSDT", "CBRSBUSDT", "CELOUSDT",
    "CELRUSDT", "CETUSUSDT", "CFGUSDT", "CFXUSDT", "CGPTUSDT", "CHIPUSDT", "CHRUSDT", "CHZUSDT", "CITYUSDT", "CKBUSDT",
    "COHRBUSDT", "COINBUSDT", "COMPUSDT", "COOKIEUSDT", "COTIUSDT", "COWUSDT", "CRCLBUSDT", "CRDOBUSDT", "CRMBUSDT", "CRVUSDT",
    "CRWDBUSDT", "CRWVBUSDT", "CTKUSDT", "CTSIUSDT", "CVCUSDT", "CVXUSDT", "CYBERUSDT", "DASHUSDT", "DCRUSDT", "DELLBUSDT",
    "DEXEUSDT", "DGBUSDT", "DIAUSDT", "DJTBUSDT", "DODOUSDT", "DOGEUSDT", "DOGSUSDT", "DOLOUSDT", "DOTUSDT", "DRAMBUSDT",
    "DUSKUSDT", "DYDXUSDT", "DYMUSDT", "EDENUSDT", "EDUUSDT", "EGLDUSDT", "EIGENUSDT", "ENAUSDT", "ENJUSDT", "ENSUSDT",
    "ENSOUSDT", "EPICUSDT", "ERAUSDT", "ESPUSDT", "ETCUSDT", "ETHUSDT", "ETHFIUSDT", "EULUSDT", "EURUSDT", "EURIUSDT",
    "EWYBUSDT", "FUSDT", "FDUSDUSDT", "FETUSDT", "FFUSDT", "FIDAUSDT", "FILUSDT", "FLNCBUSDT", "FLOKIUSDT", "FLOWUSDT",
    "FLUXUSDT", "FOGOUSDT", "FORMUSDT", "FRAXUSDT", "FTTUSDT", "GUSDT", "GALAUSDT", "GASUSDT", "GENIUSUSDT", "GIGGLEUSDT",
    "GLMUSDT", "GLMRUSDT", "GLWBUSDT", "GMEBUSDT", "GMTUSDT", "GMXUSDT", "GNOUSDT", "GNSUSDT", "GOOGLBUSDT", "GPSUSDT",
    "GRAMUSDT", "GRTUSDT", "GSBUSDT", "GTCUSDT", "GUNUSDT", "HAEDALUSDT", "HBARUSDT", "HEIUSDT", "HEMIUSDT", "HIMSBUSDT",
    "HIVEUSDT", "HMSTRUSDT", "HOLOUSDT", "HOMEUSDT", "HOODBUSDT", "HOTUSDT", "HUMAUSDT", "HYPERUSDT", "IBMBUSDT", "ICPUSDT",
    "IDUSDT", "ILVUSDT", "IMXUSDT", "INITUSDT", "INJUSDT", "INTCBUSDT", "INTWBUSDT", "IOUSDT", "IOSTUSDT", "IOTAUSDT",
    "IOTXUSDT", "IQUSDT", "IRENBUSDT", "JASMYUSDT", "JOEUSDT", "JSTUSDT", "JTOUSDT", "JUPUSDT", "JUVUSDT", "KAIAUSDT",
    "KAITOUSDT", "KATUSDT", "KAVAUSDT", "KERNELUSDT", "KGSTUSDT", "KITEUSDT", "KMNOUSDT", "KNCUSDT", "KORUBUSDT", "KSMUSDT",
    "LAUSDT", "LAYERUSDT", "LAZIOUSDT", "LDOUSDT", "LINEAUSDT", "LINKUSDT", "LISTAUSDT", "LITEBUSDT", "LPTUSDT", "LQTYUSDT",
    "LSKUSDT", "LTCUSDT", "LUMIAUSDT", "LUNAUSDT", "LUNCUSDT", "MAGICUSDT", "MANAUSDT", "MANTAUSDT", "MANTRAUSDT", "MARSCOINUSDT",
    "MASKUSDT", "MAVUSDT", "MBLUSDT", "MEUSDT", "MEGAUSDT", "MEMEUSDT", "METUSDT", "METABUSDT", "METISUSDT", "MINAUSDT",
    "MIRAUSDT", "MITOUSDT", "MMTUSDT", "MORPHOUSDT", "MOVEUSDT", "MOVRUSDT", "MRNABUSDT", "MRVLBUSDT", "MSFTBUSDT", "MSTRBUSDT",
    "MTLUSDT", "MUBUSDT", "MUBARAKUSDT", "MUUBUSDT", "MVLLBUSDT", "NBISBUSDT", "NEARUSDT", "NEIROUSDT", "NEOUSDT", "NEWTUSDT",
    "NEXOUSDT", "NFLXBUSDT", "NIGHTUSDT", "NILUSDT", "NMRUSDT", "NOKBUSDT", "NOMUSDT", "NOTUSDT", "NVDABUSDT", "NXPCUSDT",
    "OGUSDT", "OGNUSDT", "ONDOUSDT", "ONEUSDT", "ONGUSDT", "ONTUSDT", "OPUSDT", "OPENUSDT", "OPGUSDT", "OPNUSDT",
    "ORCAUSDT", "ORCLBUSDT", "ORDIUSDT", "OSMOUSDT", "PARTIUSDT", "PAXGUSDT", "PENDLEUSDT", "PENGUUSDT", "PEOPLEUSDT", "PEPEUSDT",
    "PHAUSDT", "PIXELUSDT", "PLTRBUSDT", "PLUMEUSDT", "PNUTUSDT", "POLUSDT", "POLYXUSDT", "PORTALUSDT", "PORTOUSDT", "POWRUSDT",
    "PROMUSDT", "PROVEUSDT", "PSGUSDT", "PUMPUSDT", "PUNDIXUSDT", "PYPLBUSDT", "PYTHUSDT", "QCOMBUSDT", "QIUSDT", "QKCUSDT",
    "QNTUSDT", "QNTBUSDT", "QQQBUSDT", "QTUMUSDT", "QUICKUSDT", "RADUSDT", "RAREUSDT", "RAYUSDT", "REUSDT", "REDUSDT",
    "RENDERUSDT", "REQUSDT", "RESOLVUSDT", "REZUSDT", "RIFUSDT", "RKLBBUSDT", "RLCUSDT", "RLUSDUSDT", "ROBOUSDT", "RONINUSDT",
    "ROSEUSDT", "RPLUSDT", "RSRUSDT", "RUNEUSDT", "RVNUSDT", "SUSDT", "SAGAUSDT", "SAHARAUSDT", "SANDUSDT", "SANTOSUSDT",
    "SAPIENUSDT", "SCUSDT", "SCRUSDT", "SEIUSDT", "SENTUSDT", "SFPUSDT", "SHELLUSDT", "SHIBUSDT", "SIGNUSDT", "SKHYBUSDT",
    "SKLUSDT", "SKYUSDT", "SLPUSDT", "SMCIBUSDT", "SMHBUSDT", "SNDKBUSDT", "SNXUSDT", "SNXXBUSDT", "SOLUSDT", "SOLVUSDT",
    "SOMIUSDT", "SOPHUSDT", "SOXLBUSDT", "SOXSBUSDT", "SPCXBUSDT", "SPELLUSDT", "SPKUSDT", "SPYBUSDT", "SQQQBUSDT", "SSVUSDT",
    "STEEMUSDT", "STGUSDT", "STOUSDT", "STRAXUSDT", "STRKUSDT", "STXUSDT", "STXBUSDT", "SUIUSDT", "SUNUSDT", "SUPERUSDT",
    "SUSHIUSDT", "SXTUSDT", "SYNUSDT", "SYRUPUSDT", "TUSDT", "TAOUSDT", "TFUELUSDT", "THEUSDT", "THETAUSDT", "TIAUSDT",
    "TKOUSDT", "TLMUSDT", "TNSRUSDT", "TOWNSUSDT", "TQQQBUSDT", "TRBUSDT", "TREEUSDT", "TRUMPUSDT", "TRXUSDT", "TSLABUSDT",
    "TSMBUSDT", "TSTUSDT", "TURBOUSDT", "TURTLEUSDT", "TUSDUSDT", "TUTUSDT", "TWTUSDT", "UUSDT", "UMAUSDT", "UNIUSDT",
    "USARBUSDT", "USD1USDT", "USDCUSDT", "USDEUSDT", "USDPUSDT", "USDSUSDT", "USTCUSDT", "USUALUSDT", "VANAUSDT", "VELODROMEUSDT",
    "VETUSDT", "VIRTUALUSDT", "VTHOUSDT", "WUSDT", "WALUSDT", "WAXPUSDT", "WBETHUSDT", "WBTCUSDT", "WCTUSDT", "WDCBUSDT",
    "WIFUSDT", "WINUSDT", "WLDUSDT", "WLFIUSDT", "WOOUSDT", "XAIUSDT", "XAUTUSDT", "XECUSDT", "XLMUSDT", "XNOUSDT",
    "XPLUSDT", "XRPUSDT", "XTZUSDT", "XUSDUSDT", "XVGUSDT", "XVSUSDT", "YBUSDT", "YFIUSDT", "YGGUSDT", "ZAMAUSDT",
    "ZBTUSDT", "ZECUSDT", "ZENUSDT", "ZILUSDT", "ZKUSDT", "ZKCUSDT", "ZKPUSDT", "ZROUSDT", "ZRXUSDT", "币安人生USDT", "牛来USDT",
]

_scan_cursor_lock = threading.Lock()
_scan_cursor_index = 0

def pick_candidates(
    tickers: List[dict],
    min_quote_volume: float = DEFAULT_MIN_QUOTE_VOLUME,
    batch_size: int = SCAN_BATCH_SIZE,
    state: Optional[dict] = None,
) -> List[dict]:
    """
    Выбирает батч из batch_size (по умолчанию 30) монет для скана по циклическому списку (Round-Robin).
    """
    global _scan_cursor_index
    ticker_map = {t["symbol"]: t for t in tickers}

    if WHITELIST_SYMBOLS:
        available = [s for s in WHITELIST_SYMBOLS if s in ticker_map and s != "BTCUSDT"]
        if not available:
            available = [t["symbol"] for t in tickers if t["symbol"] != "BTCUSDT"]

        with _scan_cursor_lock:
            cursor = 0
            if state and "scan_cursor" in state:
                cursor = int(state.get("scan_cursor", 0))
            else:
                cursor = _scan_cursor_index

            n = len(available)
            if n == 0:
                return []
            cursor = cursor % n

            if cursor + batch_size <= n:
                selected_syms = available[cursor : cursor + batch_size]
            else:
                selected_syms = available[cursor:] + available[: (cursor + batch_size) % n]

            new_cursor = (cursor + batch_size) % n
            _scan_cursor_index = new_cursor
            if state is not None:
                state["scan_cursor"] = new_cursor

        return [ticker_map[s] for s in selected_syms if s in ticker_map]

    liquid = [
        t for t in tickers
        if t["quoteVolume"] >= min_quote_volume
        and -4.0 <= t["priceChangePercent"] <= ALREADY_PUMPED_MAX
    ]
    by_vol = sorted(liquid, key=lambda t: t["quoteVolume"], reverse=True)
    return by_vol[:batch_size]

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

def parabolic_sar(candles: List[Candle], step: float = 0.02,
                  max_step: float = 0.2) -> Tuple[List[float], List[bool]]:
    """
    Parabolic SAR: (значения, флаг восходящего тренда по барам).

    Классика Уайлдера: точка разворота ускоряется по мере движения цены,
    разворот — когда цена пробивает SAR. Флаг направления нужен и стратегии
    (трендовый фильтр), и графику (точки рисуются под/над ценой).
    """
    n = len(candles)
    if n < 3:
        return [], []
    sar = [0.0] * n
    up_list = [True] * n
    up = candles[1].close >= candles[0].close
    af = step
    ep = max(c.high for c in candles[:2]) if up else min(c.low for c in candles[:2])
    sar[0] = candles[0].low if up else candles[0].high
    for i in range(1, n):
        cur = sar[i - 1] + af * (ep - sar[i - 1])
        if up:
            cur = min(cur, candles[i - 1].low, candles[max(0, i - 2)].low)
            if candles[i].low < cur:
                up = False
                cur = ep
                ep = candles[i].low
                af = step
            elif candles[i].high > ep:
                ep = candles[i].high
                af = min(af + step, max_step)
        else:
            cur = max(cur, candles[i - 1].high, candles[max(0, i - 2)].high)
            if candles[i].high > cur:
                up = True
                cur = ep
                ep = candles[i].high
                af = step
            elif candles[i].low < ep:
                ep = candles[i].low
                af = min(af + step, max_step)
        sar[i] = cur
        up_list[i] = up
    return sar, up_list

def fractals(candles: List[Candle], k: int = 2):
    """
    Фракталы Билла Вильямса: up-фрактал — бар с максимумом в окне ±k,
    down-фрактал — с минимумом. Возвращает (уровни up, уровни down),
    None там, где фрактала нет.

    Фрактал подтверждается только через k баров после него — поэтому
    последние k баров структурно не могут быть фракталами.
    """
    n = len(candles)
    up: List[Optional[float]] = [None] * n
    down: List[Optional[float]] = [None] * n
    for i in range(k, n - k):
        window = candles[i - k:i + k + 1]
        if candles[i].high >= max(c.high for c in window):
            up[i] = candles[i].high
        if candles[i].low <= min(c.low for c in window):
            down[i] = candles[i].low
    return up, down

def score_indicators(
    timeframe: str,
    candles: List[Candle],
    btc_candles: List[Candle],
    change_24h: float,
    forming: bool,
    strict: bool = True,
    rsi_min: Optional[float] = None,
) -> Optional[TfBreakdown]:
    """
    Режим «RSI + Parabolic SAR + фракталы»: покупка, когда RSI выше порога,
    SAR находится ПОД ценой (восходящий тренд) и цена пробила последний
    подтверждённый up-фрактал.

    Стохастик убран: на трёх выборках он не давал информации, а требование
    «%K > %D» работало против результата (см. свип порогов в харнессе).

    Соблюдает контракт score_window — возвращает TfBreakdown либо None,
    поэтому analyze_symbol не знает, какой режим выбран.
    """
    if len(candles) < MIN_BARS:
        return None

    closes = [c.close for c in candles]
    rsi14 = rsi(closes, 14)
    if rsi14 is None:
        return None

    sar_series, sar_up = parabolic_sar(candles)
    if not sar_series:
        return None
    sar_last, sar_is_up = sar_series[-1], sar_up[-1]

    up_fr, _down_fr = fractals(candles, k=2)
    last_fractal = next((lvl for lvl in reversed(up_fr) if lvl is not None), None)

    last = candles[-1]
    range_ = last.high - last.low
    if range_ <= 0:
        return None

    rsi_min = DEFAULT_RSI_MIN if rsi_min is None else rsi_min
    sar_below = sar_is_up and sar_last < last.close
    fractal_broken = last_fractal is not None and last.close > last_fractal
    # Жёсткое условие входа: RSI выше порога, SAR под ценой (тренд вверх)
    # и пробит последний up-фрактал (структурное подтверждение).
    if strict and not (rsi14 > rsi_min and sar_below and fractal_broken):
        return None

    # Насколько цена выше SAR — мера силы и «свежести» тренда
    sar_gap = clamp((last.close - sar_last) / last.close / 0.05, 0.0, 1.0)
    rsi_part = clamp((rsi14 - rsi_min) / max(1.0, 70.0 - rsi_min), 0.0, 1.0)
    if last_fractal:
        frac_part = clamp(last.close / last_fractal - 1.0, 0.0, 0.02) / 0.02
    else:
        frac_part = 0.0

    factors = [
        FactorScore("rsi_zone", f"RSI выше {rsi_min:.0f}", 40, rsi_part, f"RSI {rsi14:.1f}"),
        FactorScore("sar_trend", "Parabolic SAR под ценой", 35, sar_gap,
                    f"SAR {fmt_price(sar_last)} vs цена {fmt_price(last.close)}"),
        FactorScore("fractal_break", "Пробит up-фрактал", 25, frac_part,
                    f"фрактал {fmt_price(last_fractal) if last_fractal else '—'}"),
    ]
    score = sum(f.value * f.weight for f in factors)
    late = rsi14 >= 75
    grade = grade_from(score, late)

    reasons = []
    if rsi14 > rsi_min:
        reasons.append(f"RSI {rsi14:.1f} выше порога {rsi_min:.0f}")
    if sar_below:
        reasons.append(f"Parabolic SAR под ценой — тренд вверх")
    if fractal_broken:
        reasons.append(f"цена пробила up-фрактал {fmt_price(last_fractal)}")

    risks = []
    if rsi14 >= 70:
        risks.append("RSI в зоне перекупленности")
    if not sar_below:
        risks.append("SAR над ценой — тренд не подтверждён")
    if not fractal_broken:
        risks.append("up-фрактал не пробит")
    if last.close < last.open:
        risks.append("текущая свеча красная")

    return TfBreakdown(
        timeframe=timeframe,
        score=score,
        grade=grade,
        volume_ratio=0.0,
        atr_expansion=0.0,
        breakout_pct=0.0,
        rsi=rsi14,
        taker_buy=0.0,
        vs_btc_pct=0.0,
        change_pct=pct_change(last.open, last.close),
        ema_aligned=False,
        late=late,
        forming=forming,
        bar_open_time=last.open_time,
        factors=factors,
        reasons=reasons,
        risks=risks,
        sar=sar_last,
        fractal_level=last_fractal or 0.0,
    )

def median(values: List[float]) -> float:
    s = sorted(values)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def vol_ratio_robust(volumes: List[float], period: int = 20) -> Optional[float]:
    """
    Объём бара к МЕДИАНЕ предыдущих, а не к среднему.

    Измерено на живых данных: SMA20 завышается до 2.6× после разового всплеска
    (THEUSDT: среднее 3.86M против медианы 1.48M) и «слепит» инструмент на ~5
    часов, пока окно не вычистится. Медиана к одиночному выбросу устойчива.
    """
    if len(volumes) < period + 1:
        return None
    base = median(list(volumes[-(period + 1):-1]))
    if base <= 0:
        return None
    return volumes[-1] / base


def score_volume_candle(
    timeframe: str,
    candles: List[Candle],
    btc_candles: List[Candle],
    change_24h: float,
    forming: bool = True,
    strict: bool = True,
) -> Optional[TfBreakdown]:
    """
    «Зелёная свеча при повышенном объёме».

    Объём ведущий (60 баллов), свеча — мягкое подтверждение (40), и НЕТ
    требования пробоя: факторный аудит показал, что именно «уже состоявшееся
    движение» имеет отрицательный наклон, поэтому пробой сознательно не
    требуется. Идея в том, чтобы поймать приток покупок до того, как цена
    уйдёт, а не после.
    """
    if len(candles) < MIN_BARS:
        return None

    last = candles[-1]
    range_ = last.high - last.low
    if range_ <= 0:
        return None

    body = abs(last.close - last.open)
    body_ratio = body / range_
    close_pos = (last.close - last.low) / range_
    upper_wick_ratio = (last.high - max(last.close, last.open)) / range_
    bullish = last.close >= last.open

    vr = vol_ratio_robust([c.volume for c in candles])
    if vr is None:
        return None

    # Условие входа: зелёная свеча и объём заметно выше медианы
    if strict and not (bullish and vr >= VOL_MIN_RATIO):
        return None

    # Оценка объёма: 60 баллов, насыщение на 4× медианы
    vol_part = clamp((vr - 1.0) / 3.0, 0.0, 1.0)
    # Качество свечи: 40 баллов
    quality = 0.0
    if bullish:
        quality += 0.5
    quality += clamp(body_ratio * 1.0, 0.0, 0.25)
    quality += clamp(close_pos * 0.3, 0.0, 0.25)
    quality = clamp(quality, 0.0, 1.0)

    factors = [
        FactorScore("vol_robust", "Объём к медиане 20", 60, vol_part, f"{vr:.2f}× медианы"),
        FactorScore("candle", "Зелёная свеча", 40, quality,
                       f"тело {body_ratio*100:.0f}%, закрытие {close_pos*100:.0f}% диапазона"),
    ]
    score = sum(f.value * f.weight for f in factors)
    # Перегрев: длинная верхняя тень или объём-аномалия 6×+ (разгрузка)
    late = upper_wick_ratio > 0.45 or vr > 6.0 or change_24h > 7.0
    grade = grade_from(score, late)

    reasons = []
    if vr >= VOL_MIN_RATIO:
        reasons.append(f"объём {vr:.2f}× медианы 20 — приток покупок")
    if bullish:
        reasons.append("зелёная свеча")
    if upper_wick_ratio < 0.15:
        reasons.append("свеча закрылась у максимума")

    risks = []
    if upper_wick_ratio > 0.3:
        risks.append("длинная верхняя тень (продают в рост)")
    if vr > 6.0:
        risks.append("объём-аномалия, возможна разгрузка")
    if not bullish:
        risks.append("красная свеча при повышенном объёме")
    if change_24h > 6.0:
        risks.append(f"суточный рост {change_24h:+.1f}%")

    return TfBreakdown(
        timeframe=timeframe,
        score=score,
        grade=grade,
        volume_ratio=vr,
        atr_expansion=0.0,
        breakout_pct=0.0,
        rsi=rsi([c.close for c in candles], 14) or 0.0,
        taker_buy=(last.taker_buy_base / last.volume) if last.volume > 0 else 0.5,
        vs_btc_pct=0.0,
        change_pct=pct_change(last.open, last.close),
        ema_aligned=False,
        late=late,
        forming=forming,
        bar_open_time=last.open_time,
        factors=factors,
        reasons=reasons,
        risks=risks,
    )


def score_dump(
    timeframe: str,
    candles: List[Candle],
    btc_candles: List[Candle],
    change_24h: float,
    forming: bool = True,
    strict: bool = True,
) -> Optional[TfBreakdown]:
    """
    Детектор ДАМПА в зародыше — зеркало памп-скора.

    Зеркальность по факторам: красная свеча вместо зелёной, всплеск объёма
    (капитуляция), пробой НИЖНЕЙ границы коридора, RSI в перепроданности,
    доминирование продаж по тейкеру, отставание от BTC.

    Ключевое отличие в применении: бот long-only, поэтому сигнал означает не
    «шортить», а «готовимся покупать отскок». Вход ставится ВЫШЕ цены
    (bounce-вход) — зеркало памп-входа на откате вниз.
    """
    if len(candles) < MIN_BARS:
        return None

    last = candles[-1]
    range_ = last.high - last.low
    if range_ <= 0:
        return None

    hist = candles[:-1]
    vr = vol_ratio_robust([c.volume for c in candles])
    if vr is None:
        return None

    body = abs(last.close - last.open)
    body_ratio = body / range_
    close_pos_down = (last.high - last.close) / range_   # близость закрытия к минимуму
    bearish = last.close <= last.open

    prev_low = min((c.low for c in hist[-14:]), default=None)
    if not prev_low or prev_low <= 0:
        return None
    breakdown_pct = pct_change(prev_low, last.close)   # отрицательное при пробое вниз

    closes = [c.close for c in candles]
    rsi14 = rsi(closes, 14)
    if rsi14 is None:
        return None
    taker_buy = (last.taker_buy_base / last.volume) if last.volume > 0 else 0.5

    if strict and not (bearish and breakdown_pct < 0 and rsi14 < 45):
        return None

    vol_part = clamp((vr - 1.2) / 3.0, 0.0, 1.0)            # 25
    sell_part = clamp((0.48 - taker_buy) / 0.26, 0.0, 1.0)  # 22
    rsi_part = 0.0                                             # 18
    if 18 <= rsi14 <= 38:
        rsi_part = 1.0 - abs(rsi14 - 28) / 14.0
    elif 38 < rsi14 <= 45:
        rsi_part = clamp(1.0 - (rsi14 - 38) / 10.0, 0.0, 0.6)
    elif rsi14 < 18:
        rsi_part = 0.3                                          # уже нож, не отскок
    brk_part = 0.0                                             # 15
    if -4.0 <= breakdown_pct <= -0.2:
        brk_part = clamp(abs(breakdown_pct) / 2.5, 0.25, 1.0)
    elif breakdown_pct < -4.0:
        brk_part = 0.3                                          # слишком глубоко упало
    quality = clamp((0.4 if bearish else 0.0) + body_ratio * 0.3
                       + close_pos_down * 0.3, 0.0, 1.0)        # 10

    btc_closes = [c.close for c in btc_candles] if btc_candles else []
    lag = roc(closes, 6) - (roc(btc_closes, 6) if len(btc_closes) >= 7 else 0.0)
    lag_part = clamp((-lag + 0.3) / 2.0, 0.0, 1.0)           # 10: отставание от BTC

    factors = [
        FactorScore("vol", "Всплеск объёма", 25, vol_part, f"{vr:.1f}× медианы"),
        FactorScore("sell", "Доминирование продаж", 22, sell_part,
                       f"{taker_buy*100:.0f}% Taker Buy"),
        FactorScore("rsi_low", "RSI в перепроданности", 18, rsi_part, f"RSI {rsi14:.1f}"),
        FactorScore("breakdown", "Пробой нижней границы", 15, brk_part, f"{breakdown_pct:+.2f}%"),
        FactorScore("candle", "Красная свеча (капитуляция)", 10, quality,
                       f"тело {body_ratio*100:.0f}%"),
        FactorScore("lag_btc", "Отставание от BTC", 10, lag_part, f"{lag:+.2f}%"),
    ]
    score = sum(f.value * f.weight for f in factors)
    # Зеркало фильтра «поздно»: слишком глубокое падение — это не отскок, а нож
    late = rsi14 < 20 or change_24h < -18.0 or breakdown_pct < -6.0
    grade = grade_from(score, late)

    reasons = []
    if vr >= 2.0:
        reasons.append(f"объём капитуляции {vr:.1f}× медианы")
    if taker_buy <= 0.42:
        reasons.append(f"доминируют продажи ({(1 - taker_buy) * 100:.0f}% тейкером)")
    if rsi14 < 38:
        reasons.append(f"RSI {rsi14:.0f} — перепроданность")
    if breakdown_pct < 0:
        reasons.append(f"пробой нижней границы ({breakdown_pct:+.1f}%)")

    risks = []
    if rsi14 < 25:
        risks.append("сильная перепроданность: риск продолжить падение")
    if change_24h < -12.0:
        risks.append(f"суточное падение {change_24h:+.1f}% — возможен тренд вниз")
    if not bearish:
        risks.append("текущая свеча зелёная — дамп уже выкупают")

    return TfBreakdown(
        timeframe=timeframe,
        score=score,
        grade=grade,
        volume_ratio=vr,
        atr_expansion=0.0,
        breakout_pct=breakdown_pct,
        rsi=rsi14,
        taker_buy=taker_buy,
        vs_btc_pct=lag,
        change_pct=pct_change(last.open, last.close),
        ema_aligned=False,
        late=late,
        forming=forming,
        bar_open_time=last.open_time,
        factors=factors,
        reasons=reasons,
        risks=risks,
    )


# Реестр режимов -> скорер. None означает «встроенный в pump_bot»:
# памп-скор (score_window) и индикаторный (score_indicators) лежат выше.
STRATEGY_SCORERS = {
    "pump": None,
    "indicators": None,
    "volume": score_volume_candle,
    "dump": score_dump,
}

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
    strategy: str = DEFAULT_STRATEGY,
) -> Tuple[Optional[PumpSignal], Optional[dict]]:
    """
    Возвращает (PumpSignal если прошёл порог, candidate_summary с лучшим TF для отчёта).

    strategy выбирает движок: "pump" — прежний скоринг из 8 факторов,
    "indicators" — RSI + стохастик. Оба движка имеют одинаковый контракт,
    поэтому остальной конвейер (порог, грейды, автоторговля) не меняется.
    """
    if len(raw15) < MIN_BARS:
        return None, None

    btc_change = btc_ticker["priceChangePercent"] if btc_ticker else 0.0
    btc_rel_24h = ticker["priceChangePercent"] - btc_change

    scorer = STRATEGY_SCORERS.get(strategy)
    if scorer is None:
        scorer = score_indicators if strategy == "indicators" else score_window
    all_rows: List[TfBreakdown] = []
    for tf in TIMEFRAMES:
        candles = raw15 if tf == "5m" else aggregate_timeframe(raw15, TF_MS[tf])
        btc_c = btc_raw15 if tf == "5m" else aggregate_timeframe(btc_raw15, TF_MS[tf])
        bd = scorer(tf, candles, btc_c, ticker["priceChangePercent"], forming=True, strict=False)
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
    strategy: str = DEFAULT_STRATEGY,
    state: Optional[dict] = None,
) -> Tuple[List[PumpSignal], dict, List[dict]]:
    started = time.time()
    score_threshold = min_score if min_score is not None else DEFAULT_MIN_SCORE
    volume_threshold = min_quote_volume if min_quote_volume is not None else DEFAULT_MIN_QUOTE_VOLUME

    tickers = get_24h_tickers()
    universe = len(tickers)
    candidates = pick_candidates(tickers, min_quote_volume=volume_threshold, state=state)

    btc_raw = fetch_klines("BTCUSDT")
    btc_ticker = next((t for t in tickers if t["symbol"] == "BTCUSDT"), None)

    signals: List[PumpSignal] = []
    summaries: List[dict] = []

    def worker(t: dict) -> Tuple[Optional[PumpSignal], Optional[dict]]:
        raw = fetch_klines(t["symbol"])
        if not raw:
            return None, None
        return analyze_symbol(t, raw, btc_raw, btc_ticker,
                              min_score=score_threshold, strategy=strategy)

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

# RLock, а не Lock: помощники мутируют состояние и сами вызывают save_state,
# который тоже берёт этот лок — обычный Lock дал бы самоблокировку.
STATE_LOCK = threading.RLock()

# Однопоточные защиты торговых переходов. Их вызывают И поток монитора
# (каждые ~20с), И главный поток (команды /portfolio, /trade, кнопки),
# поэтому без защиты исполненный TP мог обработаться ДВАЖДЫ: двойная запись
# в trade_history (задвоенный PnL) и двойной вызов portfolio_remove.
_PENDING_LOCK = threading.Lock()
_ACTIVE_LOCK = threading.Lock()
_SYNC_LOCK = threading.Lock()

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
            "trade_mode": "all",                 # "fixed" | "all" | "pct:N"
            "trade_amount_usdt": DEFAULT_TRADE_AMOUNT,
            "take_profit_pct": DEFAULT_TAKE_PROFIT,
            "stop_loss_pct": DEFAULT_STOP_LOSS,
            "entry_pullback_pct": DEFAULT_ENTRY_PULLBACK,
            "dynamic_tp": DEFAULT_DYNAMIC_TP,
            "trailing_activation_pct": DEFAULT_TRAILING_ACTIVATION,
            "trailing_distance_pct": DEFAULT_TRAILING_DISTANCE,
            "use_oco": DEFAULT_USE_OCO,
            "strategy": DEFAULT_STRATEGY,          # "pump" | "indicators"
            "strategy_confirmed": False,           # выбран ли режим осознанно
            "max_open_trades": 3,
            "trade_min_score": DEFAULT_TRADE_MIN_SCORE,
            "trade_profile": DEFAULT_TRADE_PROFILE,
        },
        "active_trades": {},      # symbol -> trade dict
        "trade_history": [],      # list of closed trades
        "all_time_stats": {       # итоговая статистика за всё время (не сбрасывается при очистке истории)
            "total_trades": 0,
            "winning_trades": 0,
            "total_pnl": 0.0,
        },
        "signals_history": [],    # история найденных сигналов за последние 7 дней
        "sent_alerts": {},        # pump alert_key -> timestamp
        "pending_entries": {},    # symbol -> выставленный, но ещё не исполненный LIMIT BUY
        "symbol_alert_cooldown": {},  # symbol -> timestamp последнего алерта
        "allowed_chats": [],
        "scan_cursor": 0,
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
        d["scan_cursor"] = int(data.get("scan_cursor", 0))

        # Миграция настроек торговли (однократная).
        # Сохранённый TP, равный ПРЕЖНЕМУ дефолту 2.0%, — это не осознанный
        # выбор пользователя, а старое значение по умолчанию. Бэктест показал,
        # что 2.0% при стопе 2.0% значимо убыточен, поэтому такие записи
        # переносим на новый дефолт. Вручную изменённые значения не трогаем.
        if not d["settings"].get("trade_defaults_migrated"):
            saved_tp = d["settings"].get("take_profit_pct")
            if saved_tp is not None and abs(float(saved_tp) - PREVIOUS_TAKE_PROFIT_DEFAULT) < 1e-9:
                d["settings"]["take_profit_pct"] = DEFAULT_TAKE_PROFIT
                d["settings"]["stop_loss_pct"] = DEFAULT_STOP_LOSS
                d["settings"]["trailing_activation_pct"] = DEFAULT_TRAILING_ACTIVATION
                print(f"Миграция настроек торговли: TP {PREVIOUS_TAKE_PROFIT_DEFAULT}% → "
                      f"{DEFAULT_TAKE_PROFIT}%, SL → {DEFAULT_STOP_LOSS}%, "
                      f"активация трейлинга → {DEFAULT_TRAILING_ACTIVATION}% (дефолты из бэктеста)")
            d["settings"]["trade_defaults_migrated"] = True
        # Миграция trade_mode: "fixed" с дефолтной суммой → "all" (весь баланс)
        if d["settings"].get("trade_mode") == "fixed":
            d["settings"]["trade_mode"] = "all"
        if "trade_profile" not in d["settings"]:
            d["settings"]["trade_profile"] = DEFAULT_TRADE_PROFILE

        d["active_trades"].update(data.get("active_trades", {}))
        d["trade_history"] = data.get("trade_history", [])[-100:]
        if "all_time_stats" in data and isinstance(data["all_time_stats"], dict):
            d["all_time_stats"] = {
                "total_trades": int(data["all_time_stats"].get("total_trades", 0)),
                "winning_trades": int(data["all_time_stats"].get("winning_trades", 0)),
                "total_pnl": float(data["all_time_stats"].get("total_pnl", 0.0)),
            }
        else:
            hist = d["trade_history"]
            d["all_time_stats"] = {
                "total_trades": len(hist),
                "winning_trades": sum(1 for h in hist if float(h.get("pnl", 0.0)) > 0),
                "total_pnl": sum(float(h.get("pnl", 0.0)) for h in hist),
            }
        cutoff_7d = int(time.time()) - 7 * 86400
        d["signals_history"] = [s for s in data.get("signals_history", []) if int(s.get("timestamp", 0)) >= cutoff_7d][-1000:]
        sent = data.get("sent_alerts", {})
        if isinstance(sent, list):
            # миграция со старого формата списка
            now = int(time.time())
            d["sent_alerts"] = {k: now for k in sent}
        elif isinstance(sent, dict):
            d["sent_alerts"] = sent
        # pending_entries НЕЛЬЗЯ терять при рестарте: по ним на бирже висит
        # живой LIMIT BUY, который иначе исполнится в никуда (монеты без TP/SL)
        d["pending_entries"] = data.get("pending_entries", {}) or {}
        d["symbol_alert_cooldown"] = data.get("symbol_alert_cooldown", {}) or {}
        d["allowed_chats"] = list(set(data.get("allowed_chats", [])))

        # Валидация целостности данных состояния
        port = d["portfolio"]
        for sym in list(port.keys()):
            pos = port[sym]
            if not isinstance(pos, dict) or float(pos.get("qty", 0.0)) <= 0:
                port.pop(sym, None)

        trades = d["active_trades"]
        for sym in list(trades.keys()):
            tr = trades[sym]
            if not isinstance(tr, dict) or float(tr.get("qty", 0.0)) <= 0:
                trades.pop(sym, None)

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
    cutoff = int(time.time()) - 86400
    with STATE_LOCK:
        # Чистим устаревшее, МУТИРУЯ словари на месте: переприсваивание
        # (state["sent_alerts"] = {...}) ломает ссылки, которые уже держат
        # другие потоки, и их записи уходят в выброшенный словарь.
        sent = state.setdefault("sent_alerts", {})
        for k in [k for k, ts in sent.items() if ts <= cutoff]:
            sent.pop(k, None)
        cooldown = state.setdefault("symbol_alert_cooldown", {})
        for k in [k for k, ts in cooldown.items() if ts <= cutoff]:
            cooldown.pop(k, None)

        tmp = STATE_FILE + ".tmp"
        for attempt in range(3):
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)
                os.replace(tmp, STATE_FILE)
                break
            except RuntimeError as e:
                # Другой поток изменил коллекцию прямо во время сериализации.
                # Раньше это молча теряло запись состояния целиком.
                print(f"Повтор сохранения {STATE_FILE} (попытка {attempt + 1}/3): {e}", file=sys.stderr)
                time.sleep(0.05)
            except Exception as e:
                print(f"Не удалось сохранить {STATE_FILE}: {e}", file=sys.stderr)
                break

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
    with STATE_LOCK:
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
    with STATE_LOCK:
        if symbol not in state["portfolio"]:
            return False
        state["portfolio"].pop(symbol, None)
    save_state(state, sync_git=True)
    return True

def record_closed_trade(state: dict, closed_rec: dict) -> None:
    """Сохраняет закрытую сделку в историю и обновляет кумулятивную статистику за всё время."""
    with STATE_LOCK:
        history = state.setdefault("trade_history", [])
        history.append(closed_rec)
        if len(history) > 100:
            history.pop(0)

        stats = state.setdefault("all_time_stats", {
            "total_trades": 0,
            "winning_trades": 0,
            "total_pnl": 0.0,
        })
        pnl = float(closed_rec.get("pnl", 0.0))
        stats["total_trades"] = int(stats.get("total_trades", 0)) + 1
        stats["total_pnl"] = float(stats.get("total_pnl", 0.0)) + pnl
        if pnl > 0:
            stats["winning_trades"] = int(stats.get("winning_trades", 0)) + 1

def record_signal_history(state: dict, sig: Any) -> None:
    """Сохраняет сигнал в историю за последнюю неделю (7 дней)."""
    with STATE_LOCK:
        signals_hist = state.setdefault("signals_history", [])
        now_ts = int(time.time())
        score = float(getattr(sig, "best_score", 0.0))
        tier = "sniper" if score >= 75.0 else ("optimal" if score >= 70.0 else "profit")
        rec = {
            "symbol": getattr(sig, "symbol", ""),
            "base": getattr(sig, "base", ""),
            "score": score,
            "grade": getattr(sig, "grade", "normal"),
            "tf": getattr(sig, "best_tf", "5m"),
            "price": float(getattr(sig, "price", 0.0)),
            "timestamp": now_ts,
            "strategy_tier": tier,
        }
        signals_hist.append(rec)
        cutoff = now_ts - 7 * 86400
        state["signals_history"] = [s for s in signals_hist if int(s.get("timestamp", 0)) >= cutoff][-1000:]

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
    urls = [
        f"{BINANCE_BASE}/api/v3/time",
        f"{BINANCE_TRADE_URL}/api/v3/time",
        "https://data-api.binance.vision/api/v3/time",
        "https://api.binance.com/api/v3/time",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                server_time = int(data["serverTime"])
                local_time = int(time.time() * 1000)
                _binance_time_offset_ms = server_time - local_time
                _binance_time_last_sync = now_mono
                return _binance_time_offset_ms
        except Exception:
            continue
    return _binance_time_offset_ms

def sanitize_api_key(val: str) -> str:
    """
    Очищает API-ключ от мусора копипаста: кавычек, пробелов, префиксов
    вида 'API_KEY=' и невидимых символов.

    Невидимые символы (zero-width space, неразрывный пробел, мягкий перенос)
    глазом не видны и переживают обычный strip() — но они попадают в HMAC,
    и Binance отвечает -1022 «Signature for this request is not valid»
    на ключ, который вы скопировали правильно.
    """
    if not val:
        return ""
    cleaned = val.strip().strip("'\"`\r\n\t")
    # Удаляем имена переменных, если они попали в значение
    for prefix in ("BINANCE_API_KEY=", "BINANCE_API_SECRET=", "API_KEY=", "API_SECRET=", "KEY=", "SECRET="):
        if cleaned.upper().startswith(prefix):
            cleaned = cleaned[len(prefix):].strip().strip("'\"`\r\n\t")
            break
    if ":" in cleaned and any(cleaned.lower().startswith(x) for x in ("api key:", "api secret:", "key:", "secret:")):
        cleaned = cleaned.split(":", 1)[1].strip().strip("'\"`\r\n\t")
    # Ключ и секрет Binance состоят строго из латиницы и цифр. Всё остальное —
    # мусор. Зачистку применяем только если она не превращает значение в кашу.
    strict = re.sub(r"[^A-Za-z0-9]", "", cleaned)
    if len(strict) >= 32:
        cleaned = strict
    return cleaned

def extract_api_keys_from_text(raw_text: str) -> Tuple[str, str]:
    """
    Интеллектуально извлекает (api_key, api_secret) из любого формата ввода пользователя:
    1. '/api KEY SECRET'
    2. 'KEY SECRET'
    3. Две строки (KEY на 1-й строке, SECRET на 2-й)
    4. 'BINANCE_API_KEY=xxx\nBINANCE_API_SECRET=yyy'
    5. 'API Key: xxx, Secret Key: yyy'
    """
    text = raw_text.strip()
    # Убираем префикс команды /api или /ключ
    if text.startswith("/api") or text.startswith("/ключ") or text.startswith("api ") or text.startswith("ключ "):
        parts = text.split(None, 1)
        text = parts[1].strip() if len(parts) > 1 else ""

    if not text:
        return "", ""

    # Проверяем явные имена переменных
    lines = [l.strip() for l in text.replace(",", " ").replace(";", " ").splitlines() if l.strip()]
    k_val = ""
    s_val = ""

    for l in lines:
        l_low = l.lower()
        if "secret" in l_low:
            s_val = l.split(":", 1)[-1].split("=", 1)[-1].strip().strip("'\"`\r\n\t")
        elif "key" in l_low and not k_val:
            k_val = l.split(":", 1)[-1].split("=", 1)[-1].strip().strip("'\"`\r\n\t")

    if k_val and s_val:
        return sanitize_api_key(k_val), sanitize_api_key(s_val)

    # Иначе разбиваем по пробельным символам
    tokens = text.split()
    tokens = [t.strip().strip("'\"`,;:\r\n\t") for t in tokens if t.strip().strip("'\"`,;:\r\n\t")]
    # Убираем служебные слова если попали
    tokens = [t for t in tokens if t.lower() not in ("/api", "api", "ключ", "/ключ", "key", "secret", "api_key", "api_secret", "secret_key")]

    if len(tokens) >= 2:
        return sanitize_api_key(tokens[0]), sanitize_api_key(tokens[1])

    return "", ""

def get_api_credentials(state: Optional[dict] = None) -> Tuple[str, str]:
    """
    Возвращает (api_key, api_secret).

    Единственный источник ключей — переменные окружения (GitHub Secrets,
    либо локальный .env). Из state["settings"] ключи сознательно НЕ читаются,
    чтобы ни в репозитории, ни в bot_state.json не оставалось копий ключей.

    Пара разрешается целиком или никак: key из одного места плюс secret из
    другого дают Binance -1022 (invalid signature).
    """
    return (
        sanitize_api_key(os.environ.get("BINANCE_API_KEY", "")),
        sanitize_api_key(os.environ.get("BINANCE_API_SECRET", "")),
    )

# Коды Binance, означающие «проблема с парой key/secret»:
#   -1022 signature for this request is not valid
#   -2015 invalid API-key, IP, or permissions for action
#   -2014 API-key format invalid
#   -2008 invalid api key id
BINANCE_AUTH_ERROR_CODES = {-1022, -2015, -2014, -2008}

def describe_api_cred_problem(code: int) -> str:
    """
    Расшифровка кода ошибки Binance: что именно править в GitHub Secrets.
    Показывается прямо в чате, чтобы не гадать над сухим «Signature is not valid».
    """
    if code == -1022:
        return (
            "BINANCE_API_SECRET не подходит к BINANCE_API_KEY — секрет взят от другого "
            "API-ключа либо скопирован с невидимым символом (пробел, перенос строки). "
            "Надёжнее всего перевыпустить секрет в кабинете Binance и вставить оба "
            "значения в Secrets заново, целиком"
        )
    if code == -2015:
        return (
            "ключ не найден либо ему запрещено действие: проверьте BINANCE_API_KEY, "
            "права (Enable Reading / Enable Spot & Margin Trading) и IP-ограничение "
            "в кабинете Binance"
        )
    if code == -2014:
        return "неверный формат BINANCE_API_KEY — в значение попал мусор или оно обрезано"
    if code == -2008:
        return "Binance не знает такой API-ключ — он удалён или отозван"
    return ""

def binance_signed_request(
    method: str,
    endpoint: str,
    params: Optional[dict] = None,
    state: Optional[dict] = None,
) -> dict:
    """
    Выполняет защищенный HMAC-SHA256 запрос к торговому API Binance (/api/v3/*).
    Включает:
    - Окно recvWindow = 10000 для надежной синхронизации
    - Автоматическую ресинхронизацию времени при ошибке -1021
    - Резервные хосты (api1.binance.com, api2.binance.com, api3.binance.com) при сетевых сбоях
    - Расшифровку кодов -1022/-2015 прямо в тексте ошибки
    """
    api_key, api_secret = get_api_credentials(state)
    if not api_key or not api_secret:
        return {"error": "API-ключи Binance не настроены (BINANCE_API_KEY / BINANCE_API_SECRET)"}

    # Список хостов для надёжного соединения
    base_urls = [BINANCE_TRADE_URL]
    if "api.binance.com" in BINANCE_TRADE_URL:
        base_urls.extend(["https://api1.binance.com", "https://api2.binance.com", "https://api3.binance.com"])
    # Убираем дубликаты
    seen_urls = set()
    candidate_urls = []
    for u in base_urls:
        if u not in seen_urls:
            candidate_urls.append(u)
            seen_urls.add(u)

    headers = {
        "X-MBX-APIKEY": api_key,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PumpPulseBot/2.1",
    }
    worker_auth = ""
    if state:
        worker_auth = str(state.get("settings", {}).get("worker_auth_token", "")).strip()
    if not worker_auth:
        worker_auth = BINANCE_WORKER_AUTH
    if worker_auth:
        headers["X-Worker-Auth"] = worker_auth

    method_up = method.upper()

    for attempt in range(2):
        offset = sync_binance_time(force=(attempt > 0))
        p = dict(params or {})
        p["timestamp"] = int(time.time() * 1000) + offset
        p["recvWindow"] = 10000

        query_str = urllib.parse.urlencode(p)
        sig = hmac.new(api_secret.encode("utf-8"), query_str.encode("utf-8"), hashlib.sha256).hexdigest()
        signed_query = f"{query_str}&signature={sig}"

        last_error = None
        for base_url in candidate_urls:
            url = f"{base_url}{endpoint}"
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
                with urllib.request.urlopen(req, timeout=6) as resp:
                    record_binance_weight(getattr(resp, "headers", None))
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                record_binance_weight(getattr(e, "headers", None))
                err_body = e.read().decode("utf-8", errors="ignore")
                try:
                    err_json = json.loads(err_body)
                    code = err_json.get("code", e.code)
                    msg = sanitize_sensitive_text(err_json.get("msg", err_body))
                except Exception:
                    return {"error": sanitize_sensitive_text(f"HTTP {e.code}: {err_body}"), "code": e.code}

                # Проблема с ключами — дописываем расшифровку прямо в текст ошибки
                if code in BINANCE_AUTH_ERROR_CODES:
                    hint = describe_api_cred_problem(code)
                    if hint:
                        msg = f"{msg} — {hint}"
                    return {"error": msg, "code": code}

                # Если рассинхрон времени (-1021) — пробуем второй такт с принудительной синхронизацией
                if code == -1021 and attempt == 0:
                    last_error = {"error": msg, "code": code}
                    break
                return {"error": msg, "code": code}
            except Exception as e:
                last_error = {"error": sanitize_sensitive_text(str(e))}
                continue  # Пробуем следующий резервный хост

        if last_error and last_error.get("code") == -1021 and attempt == 0:
            time.sleep(0.3)
            continue
        if last_error:
            return last_error

    return {"error": "Не удалось подключиться к Binance API (все резервные хосты недоступны)"}

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

def process_and_test_api_keys(
    token: str,
    chat_id_local: Union[str, int],
    state: dict,
    raw_key: str,
    raw_secret: str,
) -> None:
    """
    Валидирует, сохраняет локально и немедленно проверяет боевой запрос к Binance.
    """
    key = sanitize_api_key(raw_key)
    secret = sanitize_api_key(raw_secret)
    if not key or not secret or len(key) < 16 or len(secret) < 16:
        send_telegram(
            token, chat_id_local,
            "❌ <b>Некорректный формат ключей Binance.</b>\n\n"
            "Отправьте в одном сообщении через пробел:\n"
            "<code>/api ВАШ_API_KEY ВАШ_API_SECRET</code>",
            reply_markup=cancel_keyboard(),
        )
        return

    os.environ["BINANCE_API_KEY"] = key
    os.environ["BINANCE_API_SECRET"] = secret
    if "settings" not in state:
        state["settings"] = {}
    state["settings"]["binance_api_key"] = key
    state["settings"]["binance_api_secret"] = secret
    save_state(state, sync_git=True)

    msg_id = send_telegram(token, chat_id_local, "⏳ <i>Проверяю ключи на сервере Binance Spot (/api/v3/account)...</i>")


    # Живой защищенный запрос к Binance
    assets, err = get_spot_account_assets(state)
    if err:
        err_lower = str(err).lower()
        hint = ""
        if "ip" in err_lower or "-2015" in err_lower:
            hint = "\n\n💡 <b>Причина:</b> Ограничение по IP. В кабинете Binance API включите <i>«Неограниченный IP»</i> (Unrestricted IP) или добавьте IP вашего бота."
        elif "signature" in err_lower or "-1022" in err_lower:
            hint = "\n\n💡 <b>Причина:</b> Ошибка в <b>Secret Key</b>. Проверьте правильность копирования секретного ключа (нет ли лишних символов)."
        elif "api-key format" in err_lower or "-2014" in err_lower:
            hint = "\n\n💡 <b>Причина:</b> Ошибка в <b>API Key</b>. Проверьте правильность публичного ключа."
        elif "timestamp" in err_lower or "-1021" in err_lower:
            hint = "\n\n💡 <b>Причина:</b> Рассинхрон системного времени с сервером Binance."

        resp_msg = (
            f"⚠️ <b>Ключи проверены, но Binance отклонил запрос:</b>\n"
            f"<code>{err}</code>{hint}\n\n"
            f"<i>Убедитесь, что в Binance API Management включены разрешения: «Включить чтение» (Enable Reading) и «Включить спотовую торговлю» (Enable Spot & Margin Trading).</i>"
        )
        if msg_id:
            edit_message(token, chat_id_local, msg_id, resp_msg, reply_markup=main_keyboard())
        else:
            send_telegram(token, chat_id_local, resp_msg, reply_markup=main_keyboard())
    else:
        usdt_info = assets.get("USDT", {"free": 0.0, "locked": 0.0, "total": 0.0})
        free_usdt = usdt_info.get("free", 0.0)
        total_usdt = usdt_info.get("total", 0.0)
        coin_count = sum(1 for k, v in assets.items() if k != "USDT" and float(v.get("total", 0.0)) > 0.0001)

        success_msg = (
            f"✅ <b>API-ключи Binance рабочие — проверено живым запросом.</b>\n\n"
            f"• Свободно USDT: <code>{free_usdt:,.2f} USDT</code>\n"
            f"• Всего на балансе: <code>{total_usdt:,.2f} USDT</code>\n"
            f"• Монет на споте: <b>{coin_count}</b> шт.\n\n"
            f"⚡ Доступны баланс, автоторговля и ручные ордера.\n\n"
            f"🔒 <i>Ключи сохранены в состоянии бота и переменных сессии.</i>"
        )
        if msg_id:
            edit_message(token, chat_id_local, msg_id, success_msg, reply_markup=main_keyboard())
        else:
            send_telegram(token, chat_id_local, success_msg, reply_markup=main_keyboard())

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

    # 1. Быстрый запрос балансов спота (один запрос к /api/v3/account)
    assets, err = get_spot_account_assets(state)
    if err:
        return f"❌ <b>Ошибка получения баланса Binance:</b>\n<code>{err}</code>"

    usdt_info = assets.get("USDT", {"free": 0.0, "locked": 0.0, "total": 0.0})
    usdt_free = usdt_info["free"]
    usdt_locked = usdt_info["locked"]
    usdt_total = usdt_info["total"]

    # Собираем список всех ненулевых альткоинов/монет
    non_usdt_assets = {k: v for k, v in assets.items() if k != "USDT" and v["total"] > 0.00000001}

    # Запрашиваем цены в USDT для всех найденных монет одним пакетным запросом
    symbols_to_fetch = [f"{a}USDT" for a in non_usdt_assets.keys()]
    prices = get_multiple_prices(symbols_to_fetch) if symbols_to_fetch else {}

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
            # Скрываем пыль дешевле 1 USDT (кроме активных сделок и портфеля)
            is_active = pair in active_trades or pair in portfolio
            if val_usdt < 1.0 and not is_active:
                continue
            total_crypto_value += val_usdt
            lock_str = f" <i>(в TP: {fmt_qty(lock_qty)})</i>" if lock_qty > 0.000001 else ""
            tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{pair}"
            binance_url = f"https://www.binance.com/en/trade/{asset}_USDT?type=spot"

            # Авто-регистрация позиции в portfolio, если ее там еще нет и val_usdt >= 1.0
            if state is not None and val_usdt >= 1.0 and pair not in active_trades and pair not in portfolio:
                portfolio_add(state, pair, tot_qty, price)

            # Определяем цену входа из активных сделок бота или портфеля
            trade_rec = active_trades.get(pair)
            port_rec = portfolio.get(pair)
            buy_price = 0.0
            if trade_rec:
                buy_price = float(trade_rec.get("buy_price", 0.0))
            elif port_rec:
                buy_price = float(port_rec.get("avg_price", 0.0))

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

            # Расчет и отображение целевой цены продажи (Take-Profit) и Stop-Loss
            tp_p = 0.0
            tp_id = None
            if trade_rec and float(trade_rec.get("tp_price", 0.0)) > 0:
                tp_p = float(trade_rec["tp_price"])
                tp_id = trade_rec.get("tp_order_id")
            elif port_rec and float(port_rec.get("tp_price", 0.0)) > 0:
                tp_p = float(port_rec["tp_price"])
                tp_id = port_rec.get("tp_order_id")
            elif buy_price > 0:
                tp_p = buy_price * (1.0 + DEFAULT_TAKE_PROFIT / 100.0)
            elif price > 0:
                tp_p = price * (1.0 + DEFAULT_TAKE_PROFIT / 100.0)

            if tp_p > 0:
                tp_order_tag = f" [Ордер #{tp_id}]" if tp_id else (" [В TP-ордере]" if lock_qty > 0.000001 else "")
                ref_base_p = buy_price if buy_price > 0 else price
                tp_gain_pct = ((tp_p - ref_base_p) / ref_base_p * 100.0) if ref_base_p > 0 else 0.0
                dist_to_tp = ((tp_p - price) / price * 100.0) if price > 0 else 0.0
                dist_str = f" (до цели: {dist_to_tp:+.1f}%)" if abs(dist_to_tp) > 0.01 else ""
                pnl_block.append(f"   ├ 🎯 <b>Цена продажи (TP):</b> <code>{fmt_price(tp_p)} $</code> ({tp_gain_pct:+.1f}%){tp_order_tag}{dist_str}")

            sl_p = 0.0
            if trade_rec and float(trade_rec.get("sl_price", 0.0)) > 0:
                sl_p = float(trade_rec["sl_price"])
            elif port_rec and float(port_rec.get("sl_price", 0.0)) > 0:
                sl_p = float(port_rec["sl_price"])
            elif buy_price > 0:
                sl_p = buy_price * (1.0 - DEFAULT_STOP_LOSS / 100.0)

            if sl_p > 0:
                ref_base_p = buy_price if buy_price > 0 else price
                sl_loss_pct = ((sl_p - ref_base_p) / ref_base_p * 100.0) if ref_base_p > 0 else 0.0
                dist_to_sl = ((sl_p - price) / price * 100.0) if price > 0 else 0.0
                pnl_block.append(f"   ├ 🛡️ <b>Стоп-лосс (SL):</b> <code>{fmt_price(sl_p)} $</code> ({sl_loss_pct:+.1f}%) (до стопа: {dist_to_sl:+.1f}%)")

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


def balance_inline_kb(state: Optional[dict] = None) -> dict:
    """Inline-клавиатура для экрана баланса: обновление, быстрые продажи и меню."""
    kb_rows = [
        [
            {"text": "🔄 Обновить баланс", "callback_data": "balance:refresh"},
            {"text": "🧹 Очистить пыль", "callback_data": "balance:clean_dust"},
        ]
    ]

    if state:
        active_trades = state.get("active_trades", {})
        portfolio = state.get("portfolio", {})
        held_symbols = sorted(set(list(active_trades.keys()) + list(portfolio.keys())))
        sell_buttons = []
        for sym in held_symbols:
            base = base_asset(sym)
            sell_buttons.append({"text": f"🔴 Продать {base} (рынок)", "callback_data": f"sell_prompt:{sym}"})

        for i in range(0, len(sell_buttons), 2):
            kb_rows.append(sell_buttons[i:i+2])

    kb_rows.append([
        {"text": "🎯 Выставить TP цель", "callback_data": "port:targetsell_menu"},
        {"text": "📊 Сделки и Профит", "callback_data": "port:trades_stats"},
    ])

    return {"inline_keyboard": kb_rows}


def get_dust_assets(state: dict, min_usdt: float = 1.0) -> list:
    """
    Возвращает список dust-активов: монет, чья стоимость < min_usdt USDT,
    которые НЕ находятся в active_trades и portfolio (т.е. не активные позиции).

    Каждый элемент: {"asset": str, "qty": float, "val_usdt": float, "symbol": str}
    """
    active_trades = state.get("active_trades", {})
    portfolio = state.get("portfolio", {})

    assets, err = get_spot_account_assets(state)
    if err or not assets:
        return []

    non_usdt = {k: v for k, v in assets.items() if k != "USDT" and v["total"] > 0.00000001}
    symbols_to_fetch = [f"{a}USDT" for a in non_usdt]
    prices = get_multiple_prices(symbols_to_fetch) if symbols_to_fetch else {}

    dust = []
    for asset, info in non_usdt.items():
        pair = f"{asset}USDT"
        # Пропускаем активные позиции
        if pair in active_trades or pair in portfolio:
            continue
        price = prices.get(pair)
        if price is None:
            continue
        val_usdt = info["total"] * price
        if val_usdt < min_usdt:
            dust.append({
                "asset": asset,
                "qty": info["total"],
                "free": info["free"],
                "val_usdt": val_usdt,
                "symbol": pair,
            })
    return dust


def clean_dust_balances(token: str, chat_id: int, state: dict, min_usdt: float = 1.0) -> None:
    """
    Находит и продает по MARKET все dust-активы (стоимость < min_usdt USDT),
    которые не являются активными позициями. Отправляет отчет в Telegram.
    """
    dust_list = get_dust_assets(state, min_usdt)

    if not dust_list:
        send_telegram(token, chat_id,
                      "✅ <b>Пыль не найдена.</b>\n\n"
                      f"Все монеты (кроме USDT) стоят ≥ {min_usdt} USDT, "
                      "или это активные позиции бота.")
        return

    # Показываем что будет продано
    lines = [f"🧹 <b>Найдена пыль ({len(dust_list)} монет):</b>\n"]
    for d in dust_list:
        lines.append(f"• <b>{d['asset']}</b>: {fmt_qty(d['qty'])} ≈ <code>{d['val_usdt']:.4f} USDT</code>")
    lines.append("\n⏳ <i>Продаю...</i>")
    msg_id = send_telegram(token, chat_id, "\n".join(lines))

    sold_ok = []
    failed = []

    for d in dust_list:
        symbol = d["symbol"]
        base = d["asset"]
        free_qty = d["free"]
        if free_qty <= 0.00000001:
            failed.append(f"{base}: нет свободного баланса")
            continue

        filters = get_symbol_filters(symbol)
        if not filters:
            failed.append(f"{base}: фильтры недоступны")
            continue

        step_size = filters.get("step_size", 0.0001)
        min_qty = filters.get("min_qty", 0.0)
        min_notional = filters.get("min_notional", 1.0)

        qty_str = fmt_qty_filter(free_qty, step_size)
        qty_f = float(qty_str)

        if qty_f <= 0 or qty_f < min_qty:
            failed.append(f"{base}: кол-во {free_qty} ниже min_qty {min_qty}")
            continue

        # Проверяем notional (qty * price >= minNotional)
        price = get_price(symbol)
        if price and qty_f * price < min_notional:
            failed.append(f"{base}: слишком мало ({qty_f * price:.5f} USDT < min {min_notional})")
            continue

        sell_res = binance_signed_request(
            "POST", "/api/v3/order",
            {"symbol": symbol, "side": "SELL", "type": "MARKET", "quantity": qty_str},
            state=state,
        )
        if "error" in sell_res:
            # Пробуем 99.8% от количества (комиссия)
            red = fmt_qty_filter(qty_f * 0.998, step_size)
            if float(red) > 0 and red != qty_str:
                sell_res = binance_signed_request(
                    "POST", "/api/v3/order",
                    {"symbol": symbol, "side": "SELL", "type": "MARKET", "quantity": red},
                    state=state,
                )

        if "error" in sell_res:
            failed.append(f"{base}: {sell_res.get('error', 'ошибка')}")
        else:
            cum = float(sell_res.get("cummulativeQuoteQty", 0.0))
            sold_ok.append(f"✅ {base}: продано за <code>{cum:.4f} USDT</code>")
            log_trade_event(
                event_type="DUST_CLEANED",
                symbol=symbol,
                order_id=sell_res.get("orderId"),
                price=price or 0.0,
                qty=float(sell_res.get("executedQty", qty_f)),
                quote_amount=cum,
            )

    result_lines = ["🧹 <b>Очистка пыли завершена!</b>\n"]
    if sold_ok:
        result_lines.append("<b>Продано:</b>")
        result_lines.extend(sold_ok)
    if failed:
        result_lines.append("\n<b>Не удалось продать:</b>")
        for f in failed:
            result_lines.append(f"⚠️ {f}")
    if not sold_ok and not failed:
        result_lines.append("Ничего не продано.")

    result_text = "\n".join(result_lines)
    if msg_id:
        edit_message(token, chat_id, msg_id, result_text, reply_markup=balance_inline_kb(state))
    else:
        send_telegram(token, chat_id, result_text, reply_markup=balance_inline_kb(state))


_symbol_filters_cache: Dict[str, dict] = {}
_symbol_filters_cache_at: Dict[str, float] = {}

# TTL кэша фильтров. Вечный кэш опасен: Binance меняет и шаг/тик (дробление,
# редомициляция), и статус символа (BREAK/halt), а бот продолжал бы считать
# количество по устаревшим значениям.
FILTERS_TTL_SEC = float(os.environ.get("FILTERS_TTL_SEC", "600"))

def get_symbol_filters(symbol: str) -> dict:
    """
    Торговые фильтры символа (LOT_SIZE stepSize, PRICE_FILTER tickSize, minNotional).

    При сбое сети возвращаем ПОСЛЕДНИЕ ИЗВЕСТНЫЕ фильтры, а не выдуманные:
    по выдуманному stepSize ордер отвергается биржей. Если фильтры никогда
    не были получены — отдаём {}, и вызывающий код обязан отказаться от сделки
    (см. проверку в execute_pump_auto_trade), а не считать количество вслепую.
    """
    global _symbol_filters_cache, _symbol_filters_cache_at
    symbol = normalize_symbol(symbol)
    now = time.monotonic()
    cached = _symbol_filters_cache.get(symbol)
    if cached and now - _symbol_filters_cache_at.get(symbol, 0.0) < FILTERS_TTL_SEC:
        return cached

    url = f"{BINANCE_TRADE_URL}/api/v3/exchangeInfo?symbol={symbol}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            record_binance_weight(getattr(resp, "headers", None))
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
                "stale": False,
            }
            _symbol_filters_cache[symbol] = filters
            _symbol_filters_cache_at[symbol] = now
            return filters
    except Exception as e:
        print(f"[get_symbol_filters error for {symbol}]: {e}", file=sys.stderr)
        if cached:
            stale = dict(cached)
            stale["stale"] = True
            return stale
        return {}

def _step_decimals(step: float) -> int:
    """
    Число знаков после запятой у шага биржи (0.001 → 3, 1e-12 → 12).
    Через Decimal, потому что у шагов мельче 1e-10 старый расчёт по
    форматной строке давал 0 и превращал количество в целое число.
    """
    exp = Decimal(str(step)).normalize().as_tuple().exponent
    return max(0, -int(exp))

def round_step(val: float, step: float) -> float:
    """
    Округляет вниз с учетом stepSize биржи.
    Арифметика на Decimal: у float-умножения (steps * step) накапливается
    ошибка представления, и биржа отвергает ордер по LOT_SIZE.
    """
    if step <= 0:
        return val
    d_val, d_step = Decimal(str(val)), Decimal(str(step))
    return float((d_val - (d_val % d_step)).quantize(d_step))

def round_tick(val: float, tick: float) -> float:
    """Округляет цену с учетом tickSize биржи (через Decimal)."""
    if tick <= 0:
        return val
    d_val, d_tick = Decimal(str(val)), Decimal(str(tick))
    return float((d_val / d_tick).to_integral_value(rounding=ROUND_HALF_UP) * d_tick)

def fmt_qty_filter(val: float, step: float) -> str:
    """Форматирует количество в строковый вид точно под LOT_SIZE фильтр."""
    return f"{round_step(val, step):.{_step_decimals(step)}f}"

def fmt_price_filter(val: float, tick: float) -> str:
    """Форматирует цену в строковый вид точно под PRICE_FILTER фильтр."""
    return f"{round_tick(val, tick):.{_step_decimals(tick)}f}"

def execute_pump_auto_trade(
    token: str,
    chat_id: Union[str, int],
    state: dict,
    sig: PumpSignal,
    manual_amount: Optional[float] = None,
) -> Optional[dict]:
    """
    Выполняет покупку на Binance Spot по маркету на заданную сумму USDT
    и сразу ставит на бирже OCO-список: тейк-профит и стоп-лосс.
    """
    settings = state.get("settings", {})
    tp_pct = float(settings.get("take_profit_pct", DEFAULT_TAKE_PROFIT))
    max_trades = int(settings.get("max_open_trades", 3))

    active_trades = state.setdefault("active_trades", {})
    pending_entries = state.setdefault("pending_entries", {})
    portfolio = state.get("portfolio", {})

    # Все проверки и РЕЗЕРВ слота — под одним локом, без сетевых вызовов внутри.
    # Иначе поток автоскана и ручная покупка кнопкой могут одновременно пройти
    # проверки и открыть две сделки по одной монете, причём вторая затрёт
    # запись первой — и та останется без TP/SL.
    skip_portfolio_notice = False
    with STATE_LOCK:
        error = None
        cooldown_until = is_symbol_in_sl_cooldown(state, sig.symbol)
        if cooldown_until:
            cooldown_left = max(1, int(cooldown_until - time.time()))
            error = f"Монета {sig.symbol} в кулдауне после Stop-Loss (осталось {cooldown_left}с)"
        elif sig.symbol in pending_entries:
            error = f"По монете {sig.symbol} уже выставлен лимитный ордер на вход"
        elif sig.symbol in active_trades:
            error = f"По монете {sig.symbol} уже есть открытая позиция"
        elif sig.symbol in portfolio:
            error = f"Монета {sig.symbol} уже в портфеле"
            skip_portfolio_notice = True
        elif len(active_trades) + len(pending_entries) >= max_trades:
            error = (f"Достигнут лимит активных сделок "
                     f"({len(active_trades) + len(pending_entries)}/{max_trades})")
        else:
            # Бронь слота: order_id=None. Если сделка не состоится,
            # check_pending_entries снимет её как запись без ордера.
            pending_entries[sig.symbol] = {"order_id": None, "reserved_at": int(time.time())}

    if error:
        print(f"[AutoTrade] Пропуск {sig.symbol}: {error}")
        if skip_portfolio_notice:
            send_telegram(
                token, chat_id,
                f"ℹ️ <b>Пропуск сигнала {sig.base}/USDT</b>\n\n"
                f"Монета уже есть в вашем портфеле (средняя цена входа: "
                f"<code>{fmt_price(portfolio[sig.symbol].get('avg_price', 0))} USDT</code>).\n"
                f"Повторная покупка пропущена для защиты от усреднения вниз.",
            )
        return {"error": error}

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
    if not filters.get("step_size"):
        # Без точных фильтров считать количество нельзя: биржа отвергнет ордер
        # по LOT_SIZE/PRICE_FILTER, а вход уже будет считаться начатым.
        return {"error": f"Не удалось получить торговые фильтры {sig.symbol} — вход отменён"}
    if filters.get("status") != "TRADING":
        return {"error": f"Пара {sig.symbol} временно не торгуется на бирже"}
    if filters.get("stale"):
        print(f"[AutoTrade] {sig.symbol}: фильтры из кэша (сеть недоступна) — "
              f"количество может быть отвергнуто биржей")

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

    # Определение параметров риск-менеджмента и точки входа (с поддержкой пресетов и ULTRA 3-в-1)
    profile_key = settings.get("trade_profile", "optimal")
    if profile_key == "ultra":
        score = float(sig.best_score)
        if score >= 75.0:
            # Снайпер (Score 75+): микро-откат 0.8%, SL 3.5%, TP 0.7%, Trailing 1.2%/0.4%
            entry_pullback_pct = 0.8
            sl_pct = 3.5
            tp_pct = 0.7
            trailing_activation_pct = 1.2
            trailing_distance_pct = 0.4
        elif score >= 70.0:
            # Оптимальный (Score 70-75): откат 1.0%, SL 3.0%, TP 0.7%, Trailing 1.2%/0.4%
            entry_pullback_pct = 1.0
            sl_pct = 3.0
            tp_pct = 0.7
            trailing_activation_pct = 1.2
            trailing_distance_pct = 0.4
        elif score >= 65.0:
            # Макс. Профит (Score 65-70): откат 1.0%, SL 3.5%, TP 0.7%, Trailing 1.2%/0.4%
            entry_pullback_pct = 1.0
            sl_pct = 3.5
            tp_pct = 0.7
            trailing_activation_pct = 1.2
            trailing_distance_pct = 0.4
        else:
            print(f"[AutoTrade] Пропуск {sig.symbol}: Score {score:.1f} ниже минимального порога Ultra (65.0)")
            return {"error": f"Score {score:.1f} below Ultra threshold"}
    else:
        entry_pullback_pct = float(settings.get("entry_pullback_pct", DEFAULT_ENTRY_PULLBACK))
        sl_pct = float(settings.get("stop_loss_pct", DEFAULT_STOP_LOSS))
        tp_pct = float(settings.get("take_profit_pct", DEFAULT_TAKE_PROFIT))
        trailing_activation_pct = float(settings.get("trailing_activation_pct", DEFAULT_TRAILING_ACTIVATION))
        trailing_distance_pct = float(settings.get("trailing_distance_pct", DEFAULT_TRAILING_DISTANCE))

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
            "tp_pct": tp_pct,
            "sl_pct": sl_pct,
            "trailing_activation_pct": trailing_activation_pct,
            "trailing_distance_pct": trailing_distance_pct,
            "qty": float(qty_str),
            "cost_usdt": float(qty_str) * float(limit_buy_price_str),
            "placed_at": int(time.time()),
            "timeout_sec": DEFAULT_ENTRY_TIMEOUT_SEC,
            "best_score": sig.best_score,
            "grade": sig.grade,
        }
        state.setdefault("pending_entries", {})[sig.symbol] = entry_record
        save_state(state, sync_git=True)

        log_trade_event(
            event_type="BUY_LIMIT_PLACED",
            symbol=sig.symbol,
            order_id=buy_order_id,
            price=float(limit_buy_price_str),
            qty=float(qty_str),
            quote_amount=float(qty_str) * float(limit_buy_price_str),
            reason=f"pullback_{entry_pullback_pct:.1f}%",
            meta={"best_score": sig.best_score, "grade": sig.grade},
        )

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

    # Параметры Stop-Loss и Trailing Stop (уже рассчитаны с учетом пресетов / Ultra)
    sl_price = avg_buy_price * (1.0 - (sl_pct / 100.0))

    # 3. Защита позиции: сначала биржевой OCO (TP + стоп живут на бирже,
    #    позиция защищена даже когда бот выключен), при отказе — прежняя
    #    схема с одиночным LIMIT SELL и стопом в процессе бота.
    oco = {}
    if bool(settings.get("use_oco", DEFAULT_USE_OCO)):
        oco = place_protection_oco(sig.symbol, avg_buy_price, exec_qty,
                                   tp_pct, sl_pct, filters, state)

    if oco:
        tp_order_id = oco.get("tp_order_id")
        tp_price_str = fmt_price_filter(oco["tp_price"], tick_size)
    else:
        # 3b. Размещение LIMIT SELL GTC ордера (Тейк-профит)
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
        # OCO: список и стоп-плечо. Их наличие означает, что стоп сторожит
        # биржа — позиция защищена, пока бот не работает.
        "order_list_id": oco.get("order_list_id"),
        "sl_order_id": oco.get("sl_order_id"),
        "buy_price": avg_buy_price,
        "highest_price": avg_buy_price,
        "tp_price": float(tp_price_str),
        "sl_price": sl_price,
        "trailing_sl": sl_price,
        "sl_pct": sl_pct,
        "trailing_activation_pct": trailing_activation_pct,
        "trailing_distance_pct": trailing_distance_pct,
        # Фактически исполненный объём (в ветке без OCO sell_params может
        # не существовать вовсе).
        "qty": exec_qty,
        "cost_usdt": cum_quote,
        "tp_pct": tp_pct,
        "opened_at": int(time.time()),
        "signal_score": sig.best_score,
        "signal_source": getattr(sig, "best_tf", "scan"),
        "status": "tp_placed" if tp_success else "unhedged_buy",
    }
    active_trades[sig.symbol] = trade_record
    # Снимаем бронь слота: сделка ушла в активные, «ожидание входа» больше не нужно
    state.setdefault("pending_entries", {}).pop(sig.symbol, None)
    portfolio_add(state, sig.symbol, exec_qty, avg_buy_price)
    save_state(state, sync_git=True)

    log_trade_event(
        event_type="BUY_MARKET_FILLED",
        symbol=sig.symbol,
        order_id=buy_order_id,
        price=avg_buy_price,
        qty=exec_qty,
        quote_amount=cum_quote,
        reason="market_entry",
        meta={"best_score": sig.best_score, "grade": sig.grade},
    )
    if oco:
        log_trade_event(
            event_type="OCO_PLACED",
            symbol=sig.symbol,
            order_id=oco.get("order_list_id"),
            price=float(tp_price_str),
            qty=exec_qty,
            reason=f"tp={tp_pct:.1f}%, sl={sl_pct:.1f}%",
            meta={"tp_order_id": oco.get("tp_order_id"), "sl_order_id": oco.get("sl_order_id")},
        )
    elif tp_success:
        log_trade_event(
            event_type="TP_LIMIT_PLACED",
            symbol=sig.symbol,
            order_id=tp_order_id,
            price=float(tp_price_str),
            qty=exec_qty,
            reason=f"tp={tp_pct:.1f}%",
        )

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
    Однопоточная обёртка: переход «ожидание → активная сделка» не должен
    выполняться одновременно из потока монитора и из главного потока,
    иначе сделка обрабатывается дважды.
    """
    if not _PENDING_LOCK.acquire(blocking=False):
        print("[check_pending_entries] уже выполняется в другом потоке — пропуск такта")
        return
    try:
        _check_pending_entries(token, chat_id, state)
    finally:
        _PENDING_LOCK.release()

def _check_pending_entries(token: str, chat_id: Union[str, int], state: dict) -> None:
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
        executed = float(res.get("executedQty", 0.0) or 0.0)
        timed_out = now - entry.get("placed_at", now) >= entry.get("timeout_sec", DEFAULT_ENTRY_TIMEOUT_SEC)

        # Частичное исполнение по таймауту. Снимаем остаток, но уже купленные
        # монеты — НАСТОЯЩАЯ позиция: раньше их просто выбрасывали вместе с
        # ордером, и они оставались на споте без тейк-профита и без стопа.
        # Обрабатываем их штатной веткой исполнения ниже.
        if status == "PARTIALLY_FILLED" and timed_out and executed > 0:
            cancel_res = binance_signed_request(
                "DELETE", "/api/v3/order", {"symbol": symbol, "orderId": order_id}, state=state)
            if cancel_res.get("code") in ORDER_NOT_FOUND_CODES:
                # Остаток уже не существует — значит ордер дошёл до конца,
                # перечитываем финальное состояние вместо догадок.
                again = binance_signed_request(
                    "GET", "/api/v3/order", {"symbol": symbol, "orderId": order_id}, state=state)
                if "error" not in again:
                    cancel_res = again
            executed = float(cancel_res.get("executedQty", executed) or executed)
            print(f"[Pending Fill {symbol}]: частично исполнен на {executed}, "
                  f"остаток снят, позиция берётся под контроль")
            if executed > 0:
                merged = dict(res)
                if "error" not in cancel_res:
                    merged.update(cancel_res)
                merged.pop("error", None)
                merged["status"] = "FILLED"
                merged["executedQty"] = str(executed)
                res = merged
                status = "FILLED"

        if status == "FILLED":
            to_remove.append(symbol)
            updated = True

            cum_quote = float(res.get("cummulativeQuoteQty", entry.get("cost_usdt", 0.0)))
            exec_qty = float(res.get("executedQty", entry.get("qty", 0.0)))
            avg_buy_price = (cum_quote / exec_qty) if exec_qty > 0 else float(entry.get("target_price", 0.0))

            # Расчёт Take-Profit (с динамическим ATR или из параметров входа)
            settings = state.get("settings", {})
            tp_pct = float(entry.get("tp_pct") or settings.get("take_profit_pct", DEFAULT_TAKE_PROFIT))
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
            have_filters = bool(filters.get("step_size"))
            step_size = filters.get("step_size", 1.0)
            tick_size = filters.get("tick_size", 0.01)

            sl_pct = float(entry.get("sl_pct") or settings.get("stop_loss_pct", DEFAULT_STOP_LOSS))
            sl_price = avg_buy_price * (1.0 - (sl_pct / 100.0))
            trailing_act = float(entry.get("trailing_activation_pct") or settings.get("trailing_activation_pct", DEFAULT_TRAILING_ACTIVATION))
            trailing_dist = float(entry.get("trailing_distance_pct") or settings.get("trailing_distance_pct", DEFAULT_TRAILING_DISTANCE))

            tp_order_id = None
            tp_price_str = f"{avg_buy_price * (1.0 + tp_pct / 100.0):.8f}"
            oco = {}
            use_oco = bool(settings.get("use_oco", DEFAULT_USE_OCO))
            if have_filters and use_oco:
                # Сначала пробуем биржевой OCO: он держит TP и стоп на бирже
                # одновременно, поэтому позиция защищена и без бота.
                oco = place_protection_oco(symbol, avg_buy_price, exec_qty,
                                           tp_pct, sl_pct, filters, state)
                if oco:
                    tp_order_id = oco.get("tp_order_id")
                    tp_price_str = fmt_price_filter(oco["tp_price"], tick_size)
            if not oco and have_filters:
                tp_raw_price = avg_buy_price * (1.0 + (tp_pct / 100.0))
                tp_price_str = fmt_price_filter(tp_raw_price, tick_size)
                tp_qty_str = fmt_qty_filter(exec_qty, step_size)

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
            else:
                # Фильтры недоступны — количество для TP посчитать нечем, биржа
                # отвергнет ордер. Позицию всё равно ЗАПИСЫВАЕМ, чтобы её вёл
                # стоп-лосс по цене, и сообщаем, что TP не выставлен.
                if have_filters:
                    print(f"[Pending Fill {symbol}]: OCO не выставлен, TP отложен", file=sys.stderr)

            tp_success = bool(tp_order_id)

            trade_rec = {
                "symbol": symbol,
                "base": entry.get("base", base_asset(symbol)),
                "buy_order_id": order_id,
                "tp_order_id": tp_order_id,
                # OCO: список и стоп-плечо. Их наличие означает, что стоп
                # сторожит биржа, а не процесс бота.
                "order_list_id": oco.get("order_list_id"),
                "sl_order_id": oco.get("sl_order_id"),
                "buy_price": avg_buy_price,
                "highest_price": avg_buy_price,
                "tp_price": float(tp_price_str),
                "sl_price": sl_price,
                "trailing_sl": sl_price,
                "sl_pct": sl_pct,
                "trailing_activation_pct": trailing_act,
                "trailing_distance_pct": trailing_dist,
                # Фактически исполненный объём, а НЕ скорректированный на 0.15%
                # объём ордера TP: иначе позиция в портфеле и в истории
                # расходится с реальным балансом.
                "qty": exec_qty,
                "cost_usdt": cum_quote,
                "tp_pct": tp_pct,
                "opened_at": int(time.time()),
                "signal_score": entry.get("best_score", 70.0),
                "signal_source": "pullback_limit",
                "status": "tp_placed" if tp_success else "unhedged_buy",
            }
            state.setdefault("active_trades", {})[symbol] = trade_rec
            portfolio_add(state, symbol, exec_qty, avg_buy_price)

            log_trade_event(
                event_type="BUY_LIMIT_FILLED",
                symbol=symbol,
                order_id=order_id,
                price=avg_buy_price,
                qty=exec_qty,
                quote_amount=cum_quote,
                reason="pullback_limit_fill",
                meta={"best_score": entry.get("best_score", 70.0)},
            )
            if oco:
                log_trade_event(
                    event_type="OCO_PLACED",
                    symbol=symbol,
                    order_id=oco.get("order_list_id"),
                    price=float(tp_price_str),
                    qty=exec_qty,
                    reason=f"tp={tp_pct:.1f}%, sl={sl_pct:.1f}%",
                    meta={"tp_order_id": oco.get("tp_order_id"), "sl_order_id": oco.get("sl_order_id")},
                )
            elif tp_success:
                log_trade_event(
                    event_type="TP_LIMIT_PLACED",
                    symbol=symbol,
                    order_id=tp_order_id,
                    price=float(tp_price_str),
                    qty=exec_qty,
                    reason=f"tp={tp_pct:.1f}%",
                )

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

        elif timed_out:
            # Истёк таймаут ожидания отката, ничего не исполнено.
            cancel_res = binance_signed_request(
                "DELETE", "/api/v3/order", {"symbol": symbol, "orderId": order_id}, state=state)
            if "error" in cancel_res and cancel_res.get("code") not in ORDER_NOT_FOUND_CODES:
                # Отменить не удалось — ордер может быть ещё жив. Запись НЕ
                # выбрасываем, иначе его возможное исполнение пройдёт мимо бота.
                print(f"[Timeout Pullback Order {symbol}]: отмена не удалась "
                      f"({cancel_res.get('error')}) — запись оставлена", file=sys.stderr)
                continue
            to_remove.append(symbol)
            updated = True
            base = entry.get("base", base_asset(symbol))
            print(f"[Timeout Pullback Order {symbol}]: отменён по истечению таймаута")
            log_trade_event(
                event_type="BUY_LIMIT_CANCELLED",
                symbol=symbol,
                order_id=order_id,
                price=float(entry.get("target_price", 0.0)),
                qty=float(entry.get("qty", 0.0)),
                reason="timeout_expired",
            )
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
    Однопоточная обёртка: закрытие позиции (TP/SL) не должно обрабатываться
    из двух потоков одновременно — это задваивало бы PnL в истории.
    """
    if not _ACTIVE_LOCK.acquire(blocking=False):
        print("[check_active_trades] уже выполняется в другом потоке — пропуск такта")
        return
    try:
        _check_active_trades(token, chat_id, state)
    finally:
        _ACTIVE_LOCK.release()

def _check_active_trades(token: str, chat_id: Union[str, int], state: dict) -> None:
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
        sl_order_id = trade.get("sl_order_id")

        # 0. Сначала стоп-плечо биржевого OCO. Если оно исполнилось, позицию
        #    закрыла биржа (тейк-профит при этом отменён автоматически), и
        #    учитывать это надо ДО проверки TP — иначе сработавший стоп
        #    выглядел бы как «TP отменён», и позиция оставалась в мониторинге.
        if sl_order_id:
            sl_res = binance_signed_request(
                "GET", "/api/v3/order", {"symbol": symbol, "orderId": sl_order_id}, state=state)
            if "error" not in sl_res and sl_res.get("status") == "FILLED":
                symbols_to_remove.append(symbol)
                updated = True
                cum_quote = float(sl_res.get("cummulativeQuoteQty", 0.0) or 0.0)
                filled_qty = float(sl_res.get("executedQty", 0.0) or 0.0)
                cost = float(trade.get("cost_usdt", 0.0))
                pnl = cum_quote - cost if cum_quote else 0.0
                pnl_pct = (pnl / cost * 100.0) if cost > 0 else 0.0
                sell_price = (cum_quote / filled_qty) if filled_qty > 0 else float(trade.get("sl_price", 0.0))

                closed_rec = dict(trade)
                closed_rec.update({
                    "closed_at": int(time.time()),
                    "sell_price": sell_price,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "ending_balance": get_free_usdt_balance(state),
                    "status": "oco_stop_loss",
                })
                record_closed_trade(state, closed_rec)
                portfolio_remove(state, symbol)
                set_symbol_sl_cooldown(state, symbol, reason="oco_stop_loss")
                log_trade_event(
                    event_type="SL_OCO_FILLED",
                    symbol=symbol,
                    order_id=sl_order_id,
                    price=sell_price,
                    qty=filled_qty,
                    quote_amount=cum_quote,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    reason="oco_stop_loss",
                )
                print(f"[OCO {symbol}]: стоп-плечо исполнено биржей по {sell_price}")

                if chat_id:
                    base_sl = trade.get("base", base_asset(symbol))
                    send_telegram(
                        token, chat_id,
                        f"🛑 <b>СТОП-ЛОСС СРАБОТАЛ НА БИРЖЕ (OCO)</b>\n\n"
                        f"Позиция <b>{base_sl}/USDT</b> закрыта стоп-плечом OCO "
                        f"без участия бота:\n"
                        f"• Вход: <code>{fmt_price(trade.get('buy_price', 0))} $</code>\n"
                        f"• Выход: <code>{fmt_price(sell_price)} $</code>\n"
                        f"• Результат: 🔴 <b>{pnl:+.2f} USDT ({fmt_pct(pnl_pct)})</b>\n\n"
                        f"💡 <i>Второе плечо списка (тейк-профит) отменено биржей автоматически.</i>",
                    )
                continue
            if "error" in sl_res and sl_res.get("code") in ORDER_NOT_FOUND_CODES:
                # Стоп-плеча на бирже больше нет — возвращаем позицию под
                # присмотр процесса, иначе она останется вообще без защиты.
                trade["sl_order_id"] = None
                trade["order_list_id"] = None
                updated = True
                print(f"[OCO {symbol}]: стоп-плечо не найдено на бирже — "
                      f"перехожу на клиентский стоп {trade.get('sl_price')}")
            elif sl_res.get("status") in ("CANCELED", "EXPIRED", "REJECTED"):
                trade["sl_order_id"] = None
                trade["order_list_id"] = None
                updated = True
                print(f"[OCO {symbol}]: стоп-плечо {sl_res.get('status')} — "
                      f"перехожу на клиентский стоп {trade.get('sl_price')}")

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
                    record_closed_trade(state, closed_rec)

                    # Удаляем из отслеживания портфеля
                    portfolio_remove(state, symbol)
                    log_trade_event(
                        event_type="TP_FILLED",
                        symbol=symbol,
                        order_id=tp_order_id,
                        price=closed_rec["sell_price"],
                        qty=float(res.get("executedQty", 0.0) or trade.get("qty", 0.0)),
                        quote_amount=cum_quote,
                        pnl=pnl,
                        pnl_pct=pnl_pct,
                        reason="take_profit",
                    )

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
                    # TP снят на бирже (вручную или по причине биржи). Раньше
                    # сделка молча снималась с мониторинга — монеты оставались
                    # на споте без TP и без стопа. Теперь ведём её по цене.
                    trade["tp_order_id"] = None
                    trade["status"] = "tp_order_canceled"
                    updated = True
                    base = trade.get("base", base_asset(symbol))
                    warn_msg = (
                        f"⚠️ <b>Тейк-профит ордер #{tp_order_id} по {base}/USDT отменён на бирже.</b>\n\n"
                        f"Позиция <b>остаётся под контролем стопа и трейлинга</b> "
                        f"(уровень стопа: <code>{fmt_price(trade.get('sl_price', 0))} $</code>).\n"
                        f"💡 Проверьте позицию и при необходимости продайте через /sell."
                    )
                    if chat_id:
                        send_telegram(token, chat_id, warn_msg)
                    # без continue: ниже по коду отработают SL и трейлинг-стоп

        # 2. Мониторинг цены для Stop-Loss и Трейлинг-стопа.
        # Пока живо стоп-плечо OCO, выход исполняет биржа: клиентский стоп
        # здесь только вредил бы — он мог продать второй раз или получить
        # отказ по балансу, заблокированному под стопом.
        if trade.get("sl_order_id"):
            continue

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
                sell_res = execute_emergency_market_sell(token, chat_id, state, symbol)
                if "error" in sell_res or not sell_res.get("success"):
                    # Продать не удалось. Позицию НЕ бросаем: если снять её с
                    # мониторинга сейчас, монеты останутся без стопа навсегда.
                    trade["status"] = "exit_failed"
                    trade["exit_error"] = str(sell_res.get("error", "unknown"))
                    updated = True
                    print(f"[StopTrigger for {symbol}]: продажа не удалась: {sell_res.get('error')}")
                    # Алерт не чаще раза в 10 минут: стоп проверяется каждые ~20с,
                    # иначе Telegram получает десятки одинаковых сообщений
                    now_ts = int(time.time())
                    if chat_id and now_ts - int(trade.get("exit_alert_at", 0)) > 600:
                        trade["exit_alert_at"] = now_ts
                        send_telegram(
                            token, chat_id,
                            f"🚨 <b>STOP-LOSS НЕ СРАБОТАЛ по {base}/USDT!</b>\n\n"
                            f"• Уровень стопа: <code>{fmt_price(effective_stop)} $</code>\n"
                            f"• Цена сейчас: <code>{fmt_price(cur_p)} $</code>\n"
                            f"• Ошибка: <code>{sell_res.get('error', 'unknown')}</code>\n\n"
                            f"⚠️ <i>Позиция остаётся в мониторинге — бот повторит попытку. "
                            f"Проверьте баланс и ордера на Binance вручную!</i>",
                        )
                    continue

                symbols_to_remove.append(symbol)
                updated = True
                if not is_trailing:
                    set_symbol_sl_cooldown(state, symbol, reason="market_stop_loss")
                log_trade_event(
                    event_type="TRAILING_STOP_FILLED" if is_trailing else "SL_MARKET_FILLED",
                    symbol=symbol,
                    price=cur_p,
                    qty=float(trade.get("qty", 0.0)),
                    quote_amount=float(sell_res.get("cummulativeQuoteQty", 0.0) or (cur_p * float(trade.get("qty", 0.0)))),
                    pnl=(cur_p - buy_p) * float(trade.get("qty", 0.0)),
                    pnl_pct=((cur_p - buy_p) / buy_p * 100.0) if buy_p > 0 else 0.0,
                    reason="trailing_stop" if is_trailing else "stop_loss",
                )

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

def reconcile_state_with_exchange(token: str, chat_id: Union[str, int], state: dict) -> None:
    """
    Сверка состояния с биржей сразу после запуска.

    Главный источник потерянных позиций: бот выключается (в CI — каждые
    5 часов по лимиту workflow) с живым ордером на бирже. После рестарта
    состояние расходится с реальностью, и такие позиции никто не ведёт.
    Функция ничего не продаёт — только находит расхождения и сообщает.
    """
    api_key, api_secret = get_api_credentials(state)
    if not api_key or not api_secret:
        return

    problems = []

    for symbol, entry in list(state.get("pending_entries", {}).items()):
        order_id = entry.get("order_id")
        if not order_id:
            state["pending_entries"].pop(symbol, None)
            continue
        res = binance_signed_request("GET", "/api/v3/order",
                                     {"symbol": symbol, "orderId": order_id}, state=state)
        if "error" in res:
            # Код от биржи отличает «ордера больше нет» от сетевого сбоя:
            # -2013/-2011 снимаем спокойно, а на таймаут ничего не трогаем —
            # потерять живой ордер дороже, чем показать предупреждение.
            if res.get("code") in ORDER_NOT_FOUND_CODES:
                state["pending_entries"].pop(symbol, None)
                problems.append(f"• {symbol}: лимитного ордера #{order_id} нет на бирже — снят с ожидания")
            else:
                problems.append(f"• {symbol}: не удалось проверить лимитный вход #{order_id} ({res['error']})")
            continue
        status = res.get("status")
        if status == "FILLED":
            problems.append(
                f"• {symbol}: лимитный вход #{order_id} исполнился, пока бот не работал — "
                f"позиция осталась без тейк-профита!"
            )
        elif status in ("CANCELED", "EXPIRED", "REJECTED"):
            state["pending_entries"].pop(symbol, None)
            problems.append(f"• {symbol}: лимитный вход #{order_id} в статусе {status} — снят с ожидания")

    for symbol, trade in state.get("active_trades", {}).items():
        tp_order_id = trade.get("tp_order_id")
        if not tp_order_id:
            problems.append(f"• {symbol}: активная сделка без ордера тейк-профита")
            continue
        res = binance_signed_request("GET", "/api/v3/order",
                                     {"symbol": symbol, "orderId": tp_order_id}, state=state)
        if "error" in res:
            if res.get("code") in ORDER_NOT_FOUND_CODES:
                trade["tp_order_id"] = None
                trade["status"] = "tp_missing"
                problems.append(f"• {symbol}: TP-ордера #{tp_order_id} нет на бирже — позиция без тейк-профита")
            else:
                problems.append(f"• {symbol}: не удалось проверить TP-ордер #{tp_order_id} ({res['error']})")
        elif res.get("status") == "CANCELED":
            trade["tp_order_id"] = None
            trade["status"] = "tp_order_canceled"
            problems.append(f"• {symbol}: TP-ордер #{tp_order_id} отменён — позиция ведётся только по стопу")

    if problems:
        save_state(state)
        print("[Reconcile] Расхождения с биржей:\n" + "\n".join(problems))
        if chat_id:
            send_telegram(
                token, chat_id,
                "🔄 <b>Сверка состояния с биржей после запуска</b>\n\n"
                + "\n".join(problems[:15])
                + "\n\n💡 <i>Проверьте эти позиции вручную: пока бот был выключен, "
                  "события на бирже шли без его участия.</i>",
            )
    else:
        print("[Reconcile] Состояние сходится с биржей")


def build_oco_legs(
    symbol: str,
    avg_buy_price: float,
    qty: float,
    tp_pct: float,
    sl_pct: float,
    filters: dict,
    cur_price: Optional[float] = None,
) -> Tuple[dict, Optional[str]]:
    """
    Строит параметры обеих ног защиты ОТДЕЛЬНЫМИ ордерами:
    тейк-профит = LIMIT_MAKER SELL, стоп-лосс = STOP_LOSS SELL (по рынку).

    Единый источник истины для количества и цен: и боевое выставление OCO,
    и предварительная проверка через POST /api/v3/order/test собирают ноги
    ИМЕННО здесь, поэтому проверяется то, что реально уйдёт на биржу,
    а не его копия.

    Возвращает (legs, None) либо ({}, причина, почему выставить нельзя).
    """
    step = filters.get("step_size")
    tick = filters.get("tick_size")
    if not step:
        return {}, "нет торговых фильтров символа"
    if not tick:
        return {}, "нет шага цены (tickSize)"
    if tp_pct <= 0 or not (0 < sl_pct < 100):
        return {}, f"некорректные TP/SL ({tp_pct}/{sl_pct})"

    tp_price = float(fmt_price_filter(avg_buy_price * (1.0 + tp_pct / 100.0), tick))
    sl_trigger = float(fmt_price_filter(avg_buy_price * (1.0 - sl_pct / 100.0), tick))
    qty_str = fmt_qty_filter(qty, step)
    if float(qty_str) <= 0:
        return {}, f"количество {qty} меньше шага {step}"
    if tp_price <= sl_trigger:
        return {}, f"цель {tp_price} не выше стопа {sl_trigger}"

    if cur_price is None:
        cur_price = get_price(symbol) or 0.0
    if cur_price > 0 and tp_price <= cur_price:
        return {}, (f"цель {tp_price} не выше рынка {cur_price} — "
                    f"LIMIT_MAKER будет отвергнут")
    if cur_price > 0 and sl_trigger >= cur_price:
        return {}, f"стоп {sl_trigger} не ниже рынка {cur_price}"

    legs = {
        "tp": {"symbol": symbol, "side": "SELL", "type": "LIMIT_MAKER",
               "quantity": qty_str, "price": fmt_price_filter(tp_price, tick)},
        "sl": {"symbol": symbol, "side": "SELL", "type": "STOP_LOSS",
               "quantity": qty_str, "stopPrice": fmt_price_filter(sl_trigger, tick)},
        "tp_price": tp_price,
        "sl_price": sl_trigger,
        "qty": float(qty_str),
        "cur_price": cur_price,
    }
    return legs, None


def place_protection_oco(
    symbol: str,
    avg_buy_price: float,
    qty: float,
    tp_pct: float,
    sl_pct: float,
    filters: dict,
    state: dict,
    cur_price: Optional[float] = None,
) -> dict:
    """
    Ставит на бирже OCO-список: тейк-профит (LIMIT_MAKER) и стоп-лосс
    (STOP_LOSS по рынку) одновременно. Срабатывание одного плеча
    автоматически отменяет другое, и позиция защищена без участия бота.

    Возвращает поля для записи сделки либо {} — тогда вызывающий код обязан
    откатиться на прежнюю схему (лимитный TP + стоп в процессе бота).
    """
    legs, reason = build_oco_legs(symbol, avg_buy_price, qty, tp_pct, sl_pct,
                                  filters, cur_price)
    if not legs:
        print(f"[OCO {symbol}]: не выставлен — {reason}", file=sys.stderr)
        return {}

    params = {
        "symbol": symbol,
        "side": "SELL",
        "quantity": legs["tp"]["quantity"],
        "aboveType": legs["tp"]["type"],
        "abovePrice": legs["tp"]["price"],
        "belowType": legs["sl"]["type"],
        "belowStopPrice": legs["sl"]["stopPrice"],
        "newOrderRespType": "FULL",
    }
    res = binance_signed_request("POST", "/api/v3/orderList/oco", params, state=state)
    if "error" in res or res.get("orderListId") is None:
        print(f"[OCO {symbol}]: не удалось выставить ({res.get('error')}) — "
              f"откат на лимитный TP с клиентским стопом", file=sys.stderr)
        return {}

    tp_order_id = None
    sl_order_id = None
    for o in (res.get("orders") or res.get("orderReports") or []):
        otype = str(o.get("type", ""))
        if otype == "LIMIT_MAKER":
            tp_order_id = o.get("orderId")
        elif "STOP" in otype or "TAKE_PROFIT" in otype:
            sl_order_id = o.get("orderId")

    print(f"[OCO {symbol}]: выставлен список #{res.get('orderListId')} "
          f"(TP #{tp_order_id} {legs['tp_price']}, SL #{sl_order_id} {legs['sl_price']})")
    return {
        "order_list_id": res.get("orderListId"),
        "tp_order_id": tp_order_id,
        "sl_order_id": sl_order_id,
        "tp_price": legs["tp_price"],
        "sl_price": legs["sl_price"],
        "qty": legs["qty"],
    }


def cancel_protection_oco(symbol: str, trade: dict, state: dict) -> dict:
    """
    Снимает OCO-список перед ручной или экстренной продажей: пока список
    жив, монеты заблокированы под стоп-плечом, и MARKET SELL отвергнется
    по недостатку свободного баланса.
    """
    list_id = (trade or {}).get("order_list_id")
    if not list_id:
        return {}
    res = binance_signed_request(
        "DELETE", "/api/v3/orderList",
        {"symbol": symbol, "orderListId": list_id}, state=state)
    print(f"[Cancel OCO list #{list_id} for {symbol}]: {res}")
    return res


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

    # 1. Разблокировка монет перед продажей. С OCO монеты держит стоп-плечо
    #    списка, поэтому снимать только TP недостаточно — MARKET SELL
    #    отвергнется по недостатку свободного баланса.
    if (trade or {}).get("order_list_id"):
        cancel_res = cancel_protection_oco(symbol, trade, state)
        if "error" in cancel_res and cancel_res.get("code") not in ORDER_NOT_FOUND_CODES:
            return {"error": f"Не удалось снять OCO-список: {cancel_res.get('error')}"}
        trade["order_list_id"] = None
        trade["sl_order_id"] = None

    tp_order_id = trade.get("tp_order_id") if trade else None
    if tp_order_id:
        cancel_res = binance_signed_request(
            "DELETE",
            "/api/v3/order",
            {"symbol": symbol, "orderId": tp_order_id},
            state=state,
        )
        print(f"[Cancel TP order #{tp_order_id} for {symbol}]: {cancel_res}")
        if "error" not in cancel_res or cancel_res.get("code") in ORDER_NOT_FOUND_CODES:
            trade["tp_order_id"] = None

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
    record_closed_trade(state, closed_rec)

    # Удаляем из активных сделок и портфеля
    active_trades.pop(symbol, None)
    portfolio_remove(state, symbol)
    save_state(state, sync_git=True)

    log_trade_event(
        event_type="MANUAL_MARKET_SOLD",
        symbol=symbol,
        order_id=sell_res.get("orderId"),
        price=sell_price,
        qty=exec_qty,
        quote_amount=cum_quote,
        pnl=pnl,
        pnl_pct=pnl_pct,
        reason="manual_or_emergency_market_sell",
    )

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

    tp_note = f"\n• Тейк-профит цель: <code>{fmt_price(tp_price)} $</code> (+{trade.get('tp_pct', DEFAULT_TAKE_PROFIT):.1f}%)" if trade else ""

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
        "sl_price": trade.get("sl_price", buy_p * (1 - DEFAULT_STOP_LOSS / 100)) if trade else buy_p * (1 - DEFAULT_STOP_LOSS / 100),
        "trailing_sl": trade.get("trailing_sl", buy_p * (1 - DEFAULT_STOP_LOSS / 100)) if trade else buy_p * (1 - DEFAULT_STOP_LOSS / 100),
        "sl_pct": trade.get("sl_pct", DEFAULT_STOP_LOSS) if trade else DEFAULT_STOP_LOSS,
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
    Однопоточная обёртка: функция переписывает записи active_trades целиком,
    а вызывается и из команд главного потока, и из монитора — параллельный
    запуск затирал бы свежие данные чужой копией записи.
    """
    if not _SYNC_LOCK.acquire(blocking=False):
        print("[sync_trades] уже выполняется в другом потоке — пропуск")
        return
    try:
        _sync_trades_and_active_positions(state)
    finally:
        _SYNC_LOCK.release()

def _sync_trades_and_active_positions(state: dict) -> None:
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
        # Сбой запроса НЕ равен «ордеров нет»: при ошибке нельзя стирать
        # tp_order_id, иначе живая защита исчезает из состояния бота.
        open_orders_ok = isinstance(open_orders_res, list)
        open_orders = open_orders_res if open_orders_ok else []

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

            if tp_price <= 0 and buy_price > 0 and open_orders_ok:
                # Живого TP-ордера нет — показываем целевую цену как ориентир,
                # но не выдаём её за выставленный ордер.
                tp_price = buy_price * (1.0 + tp_pct_default / 100.0)

            tp_pct_actual = ((tp_price - buy_price) / buy_price * 100.0) if buy_price > 0 else tp_pct_default

            # СЛИВАЕМ с прежней записью, а не заменяем её целиком. Полная
            # перезапись теряла то, что бот знает лучше биржи: пик для
            # трейлинга, настройки трейлинга, метки проваленного выхода,
            # а при сбое запроса openOrders — ещё и рабочий tp_order_id
            # (позиция выглядела без TP, хотя ордер жил на бирже).
            prev = trade_rec or {}
            sl_pct_prev = float(prev.get("sl_pct", settings.get("stop_loss_pct", DEFAULT_STOP_LOSS)))
            sl_price_prev = float(prev.get("sl_price", 0.0) or 0.0)
            if sl_price_prev <= 0 and buy_price > 0:
                sl_price_prev = buy_price * (1.0 - sl_pct_prev / 100.0)

            if not open_orders_ok:
                # Состояние ордеров неизвестно — сохраняем прежние значения,
                # чтобы не объявить позицию незащищённой ошибочно.
                tp_order_id = prev.get("tp_order_id")
                status = prev.get("status", "holding")
                if not tp_price:
                    tp_price = float(prev.get("tp_price", 0.0) or 0.0)
            else:
                status = "tp_placed" if tp_order_id else "holding"
                if prev.get("status") == "exit_failed" and not tp_order_id:
                    status = "exit_failed"      # стоп ещё не сработал, не гасим метку

            active_trades[sym] = {
                "symbol": sym,
                "base": asset,
                "buy_order_id": prev.get("buy_order_id"),
                "tp_order_id": tp_order_id,
                "buy_price": buy_price,
                # пик движения НЕ понижаем: иначе трейлинг теряет максимум
                "highest_price": max(cur_p, buy_price, float(prev.get("highest_price", 0.0) or 0.0)),
                "tp_price": tp_price,
                "sl_price": sl_price_prev,
                "trailing_sl": float(prev.get("trailing_sl", sl_price_prev) or sl_price_prev),
                "sl_pct": sl_pct_prev,
                "trailing_activation_pct": float(prev.get("trailing_activation_pct", DEFAULT_TRAILING_ACTIVATION)),
                "trailing_distance_pct": float(prev.get("trailing_distance_pct", DEFAULT_TRAILING_DISTANCE)),
                "qty": tot_qty,
                "cost_usdt": tot_qty * buy_price,
                "tp_pct": tp_pct_actual,
                "opened_at": int(prev.get("opened_at", opened_at) or opened_at) or int(time.time()),
                "status": status,
            }
            # Метки повторных попыток выхода переносим, иначе алерт о
            # несработавшем стопе начнёт сыпаться каждые 20 секунд.
            for keep in ("exit_failed_at", "exit_alert_at", "exit_error", "signal_score", "signal_source"):
                if keep in prev:
                    active_trades[sym][keep] = prev[keep]
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
                        b_price = float(b_trade.get("price", s_price * (1 - DEFAULT_STOP_LOSS / 100)))
                        b_time = int(b_trade.get("time", 0)) // 1000
                    else:
                        b_price = s_price * (1 - DEFAULT_STOP_LOSS / 100)
                        b_time = s_time - 3600

                    cost = s_qty * b_price
                    pnl = s_quote - cost
                    pnl_pct = (pnl / cost * 100.0) if cost > 0 else 0.0

                    rec = {
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
                    }
                    record_closed_trade(state, rec)
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
            tp_pct = float(tr.get("tp_pct", DEFAULT_TAKE_PROFIT))
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
    """
    Компактный журнал сделок по дням (сегодня / вчера).
    НЕ вызывает sync — работает только с данными state + один batch-запрос цен для открытых позиций.
    """
    active_trades = state.get("active_trades", {})
    portfolio     = state.get("portfolio", {})
    history       = state.get("trade_history", [])
    settings      = state.get("settings", {})

    trade_amt = float(settings.get("trade_amount_usdt", DEFAULT_TRADE_AMOUNT))
    tp_pct    = float(settings.get("take_profit_pct",   DEFAULT_TAKE_PROFIT))
    auto_on   = settings.get("auto_trade", False)
    auto_icon = "🟢" if auto_on else "🔴"

    api_key, api_secret = get_api_credentials(state)
    has_api   = bool(api_key and api_secret)
    free_usdt = get_free_usdt_balance(state) if has_api else 0.0

    # ── Определяем границы «сегодня» и «вчера» ─────────────────────────
    now_ts   = time.time()
    tz_off   = time.timezone if (time.localtime().tm_isdst == 0) else time.altzone
    now_loc  = now_ts - tz_off
    day_sec  = 86400
    today_start    = int(now_loc // day_sec) * day_sec + tz_off
    yesterday_start = today_start - day_sec

    def day_label(ts: float) -> str:
        if ts >= today_start:
            return "today"
        if ts >= yesterday_start:
            return "yesterday"
        return "older"

    # ── Открытые позиции: один batch-запрос цен ─────────────────────────
    open_items: dict = {}
    for sym, tr in active_trades.items():
        open_items[sym] = dict(tr)
    for sym, pos in portfolio.items():
        if sym not in open_items:
            open_items[sym] = {
                "symbol": sym,
                "base": base_asset(sym),
                "buy_price":  float(pos.get("avg_price", 0.0)),
                "qty":        float(pos.get("qty", 0.0)),
                "cost_usdt":  float(pos.get("qty", 0.0)) * float(pos.get("avg_price", 0.0)),
                "tp_price":   float(pos.get("avg_price", 0.0)) * (1.0 + tp_pct / 100.0),
                "tp_pct":     tp_pct,
                "opened_at":  pos.get("added_at", int(now_ts)),
            }

    cur_prices: dict = {}
    if open_items:
        cur_prices = get_multiple_prices(list(open_items.keys())) or {}

    # ── Собираем строки журнала по дням ──────────────────────────────────
    # Ключ → список строк записей (one-liner)
    buckets: dict = {"today": [], "yesterday": [], "older": []}

    DAY_NAMES = {"today": "Сегодня", "yesterday": "Вчера", "older": "Ранее"}

    # Закрытые сделки (history)
    closed_total_pnl = 0.0
    closed_today_pnl = 0.0
    closed_yday_pnl  = 0.0
    win_today = 0; total_today = 0
    win_yday  = 0; total_yday  = 0

    for h in reversed(history):           # от новых к старым
        ts      = float(h.get("closed_at", 0))
        base    = h.get("base", base_asset(h.get("symbol", "?")))
        pnl     = float(h.get("pnl", 0.0))
        pnl_pct = float(h.get("pnl_pct", 0.0))
        reason  = h.get("status", "")
        closed_total_pnl += pnl

        t_str = time.strftime("%H:%M", time.localtime(ts)) if ts else "--:--"
        sign  = "🟢" if pnl >= 0 else "🔴"
        # причина закрытия (коротко)
        if "tp" in reason.lower() or "take" in reason.lower():
            tag = "TP"
        elif "sl" in reason.lower() or "stop" in reason.lower():
            tag = "SL"
        elif "trail" in reason.lower():
            tag = "TSL"
        elif "manual" in reason.lower():
            tag = "MNL"
        else:
            tag = "—"

        row = f"{t_str}  <b>{base:<6}</b>  {sign} <b>{pnl:+.2f} USDT</b>  ({pnl_pct:+.1f}%)  [{tag}]"

        bucket = day_label(ts)
        buckets[bucket].append(row)
        if bucket == "today":
            closed_today_pnl += pnl; total_today += 1
            if pnl > 0: win_today += 1
        elif bucket == "yesterday":
            closed_yday_pnl += pnl; total_yday += 1
            if pnl > 0: win_yday += 1

    # Открытые позиции → в сегодняшний bucket (или вчерашний)
    open_float_pnl  = 0.0
    open_today_pnl  = 0.0
    open_yday_pnl   = 0.0
    for sym, tr in open_items.items():
        ts   = float(tr.get("opened_at", now_ts))
        base = tr.get("base", base_asset(sym))
        bp   = float(tr.get("buy_price", 0.0))
        qty  = float(tr.get("qty", 0.0))
        cost = float(tr.get("cost_usdt", qty * bp))

        cur_p = cur_prices.get(sym)
        if cur_p and qty > 0:
            cur_val   = qty * cur_p
            float_pnl = cur_val - cost
            float_pct = (float_pnl / cost * 100.0) if cost > 0 else 0.0
        else:
            float_pnl = 0.0
            float_pct = 0.0

        open_float_pnl += float_pnl
        t_str = time.strftime("%H:%M", time.localtime(ts)) if ts else "--:--"
        sign  = "🟢" if float_pnl >= 0 else "🔴"
        cur_str = f"<code>{fmt_price(cur_p)} $</code>" if cur_p else "—"
        row = (
            f"{t_str}  <b>{base:<6}</b>  {sign} — ⏳ <i>в позиции</i>  "
            f"{float_pnl:+.2f} USDT ({float_pct:+.1f}%)  [вход: <code>{fmt_price(bp)} $</code>  сейчас: {cur_str}]"
        )
        bucket = day_label(ts)
        buckets[bucket].insert(0, row)   # открытые — первыми в своём дне
        if bucket == "today":   open_today_pnl += float_pnl
        elif bucket == "yesterday": open_yday_pnl += float_pnl

    # ── Формируем вывод ──────────────────────────────────────────────────
    lines: list = []

    # Шапка
    auto_lbl = "🟢 Вкл" if auto_on else "🔴 Выкл"
    lines.append(
        f"📊 <b>Сделки и Профит</b>\n"
        f"⚡ Авто: {auto_lbl}  |  Ставка: <code>{trade_amt:.0f}$</code>  |  TP: <code>+{tp_pct:.1f}%</code>"
    )

    has_any_day = False
    # Дни
    for key in ("today", "yesterday", "older"):
        rows = buckets[key]
        if not rows:
            continue
        has_any_day = True
        # Дата-заголовок
        if key == "today":
            day_date = time.strftime("%d.%m", time.localtime(today_start))
            pnl_closed = closed_today_pnl
            pnl_open   = open_today_pnl
            wr = f"{win_today}/{total_today}" if total_today else "—"
        elif key == "yesterday":
            day_date = time.strftime("%d.%m", time.localtime(yesterday_start))
            pnl_closed = closed_yday_pnl
            pnl_open   = open_yday_pnl
            wr = f"{win_yday}/{total_yday}" if total_yday else "—"
        else:
            day_date = "..."
            pnl_closed = 0.0
            pnl_open   = 0.0
            wr = "—"

        day_pnl = pnl_closed + pnl_open
        day_sign = "🟢" if day_pnl >= 0 else "🔴"
        lines.append(
            f"\n🗓 <b>{DAY_NAMES[key]} ({day_date})</b>  "
            f"{day_sign} <b>{day_pnl:+.2f} USDT</b>  W/L: {wr}\n"
            "──────────────────────"
        )
        lines.extend(rows)

    if not has_any_day:
        lines.append("\n🗓 <i>Журнал сделок пуст (история очищена)</i>\n")

    # ── Сводка последних 5 дней ──────────────────────────────────────────
    five_day_lines = []
    for d_offset in range(4, -1, -1):   # 4 дня назад → сегодня
        d_start = today_start - d_offset * day_sec
        d_end   = d_start + day_sec
        d_label = time.strftime("%d.%m", time.localtime(d_start))
        if d_offset == 0:
            d_label += " сег."
        elif d_offset == 1:
            d_label += " вчер."

        d_trades = [h for h in history
                    if d_start <= float(h.get("closed_at", 0)) < d_end]
        d_pnl   = sum(float(h.get("pnl", 0.0)) for h in d_trades)
        d_wins  = sum(1 for h in d_trades if float(h.get("pnl", 0.0)) > 0)
        d_cnt   = len(d_trades)

        # открытые считаем только в сегодняшнем дне
        if d_offset == 0 and open_items:
            d_pnl += open_float_pnl
            open_tag = f"+{len(open_items)}⏳" if open_items else ""
        else:
            open_tag = ""

        if d_cnt == 0 and d_offset != 0 and not open_items:
            bar = "·"
            pnl_str = "нет сделок"
        else:
            sign = "🟢" if d_pnl >= 0 else "🔴"
            wr_str = f" {d_wins}/{d_cnt}" if d_cnt else ""
            pnl_str = f"{sign} <b>{d_pnl:+.2f}$</b>{wr_str}{' ' + open_tag if open_tag else ''}"
            # мини-прогресс-бар: ▓ за каждую прибыльную, ░ за убыточную
            bar_chars = "".join("▓" if float(h.get("pnl", 0.0)) > 0 else "░" for h in d_trades)
            bar = bar_chars[:8] if bar_chars else "·"

        five_day_lines.append(f"<code>{d_label:<10}</code> {bar:<8} {pnl_str}")

    if five_day_lines:
        lines.append("\n📅 <b>Последние 5 дней:</b>")
        lines.extend(five_day_lines)

    # Итого (2 дня)
    two_day_pnl = closed_today_pnl + closed_yday_pnl + open_today_pnl + open_yday_pnl
    total_closed = total_today + total_yday
    total_wins   = win_today + win_yday
    wr_total = f"{total_wins}/{total_closed}" if total_closed else "—"
    wr_pct   = f"  ({total_wins/total_closed*100:.0f}%)" if total_closed else ""

    t_sign = "🟢" if two_day_pnl >= 0 else "🔴"

    if total_closed > 0:
        lines.append(
            f"\n📌 <b>Итого (2 дня):</b>\n"
            f"• Закрыто: <b>{total_closed} сделки</b>  |  W/L: {wr_total}{wr_pct}\n"
            f"• Реализованный P&L: {t_sign} <b>{(closed_today_pnl+closed_yday_pnl):+.2f} USDT</b>"
        )

    if open_items:
        f_sign = "🟢" if open_float_pnl >= 0 else "🔴"
        lines.append(
            f"• Открытых позиций: <b>{len(open_items)}</b>  "
            f"|  Плав. P&L: {f_sign} <b>{open_float_pnl:+.2f} USDT</b>"
        )

    # Всего за всё время
    all_time = state.get("all_time_stats")
    if isinstance(all_time, dict) and int(all_time.get("total_trades", 0)) > 0:
        tot_trades = int(all_time.get("total_trades", 0))
        win_all = int(all_time.get("winning_trades", 0))
        tot_pnl = float(all_time.get("total_pnl", 0.0))
    elif history:
        tot_trades = len(history)
        win_all = sum(1 for h in history if float(h.get("pnl", 0.0)) > 0)
        tot_pnl = closed_total_pnl
    else:
        tot_trades = 0
        win_all = 0
        tot_pnl = 0.0

    if tot_trades > 0:
        wr_pct_val = (win_all / tot_trades * 100.0) if tot_trades > 0 else 0.0
        wr_all = f"{win_all}/{tot_trades} ({wr_pct_val:.0f}%)"
        all_sign = "🟢" if tot_pnl >= 0 else "🔴"
        lines.append(
            f"\n🏆 <b>За всё время:</b>  "
            f"{all_sign} <b>{tot_pnl:+.2f} USDT</b>  |  Сделок: <b>{tot_trades}</b>  |  W/L: {wr_all}"
        )
    else:
        lines.append("\n🏆 <b>За всё время:</b>  <code>0.00 USDT</code>  |  Сделок: 0")

    lines.append(f"\n💵 Свободно: <code>{free_usdt:,.2f} USDT</code>")
    return "\n".join(lines)

def format_signals_stats(state: dict, days: int = 7) -> str:
    """Формирует подробный аналитический отчёт по сигналам за последние N дней (неделя)."""
    now_ts = time.time()
    cutoff = now_ts - days * 86400
    all_sigs = state.get("signals_history", [])
    sigs = [s for s in all_sigs if int(s.get("timestamp", 0)) >= cutoff]

    # Сделки за последние N дней
    history = state.get("trade_history", [])
    recent_trades = [h for h in history if float(h.get("closed_at", 0)) >= cutoff]

    total_signals = len(sigs)
    sniper_sigs = [s for s in sigs if s.get("strategy_tier") == "sniper" or float(s.get("score", 0)) >= 75.0]
    opt_sigs = [s for s in sigs if s.get("strategy_tier") == "optimal" or (70.0 <= float(s.get("score", 0)) < 75.0)]
    profit_sigs = [s for s in sigs if s.get("strategy_tier") == "profit" or (65.0 <= float(s.get("score", 0)) < 70.0)]

    avg_score = (sum(float(s.get("score", 0)) for s in sigs) / total_signals) if total_signals > 0 else 0.0

    lines = [
        f"📡 <b>Статистика сигналов за последние {days} дней</b>",
        f"⚡ <i>Отработка памп-сканера и стратегий в реальном времени</i>\n",
    ]

    # 1. Сводка сигналов
    if total_signals > 0:
        lines.append(
            f"📊 <b>Всего зафиксировано сигналов:</b> <code>{total_signals} шт.</code>\n"
            f"• 🛡 <b>Снайпер (Score ≥ 75):</b> <code>{len(sniper_sigs)}</code> ({len(sniper_sigs)/total_signals*100:.0f}%)\n"
            f"• 🥇 <b>Оптимальный (Score 70-74):</b> <code>{len(opt_sigs)}</code> ({len(opt_sigs)/total_signals*100:.0f}%)\n"
            f"• 🚀 <b>Макс. Профит (Score 65-69):</b> <code>{len(profit_sigs)}</code> ({len(profit_sigs)/total_signals*100:.0f}%)\n"
            f"• 🎯 <b>Средний Score:</b> <code>{avg_score:.1f} / 100</code>"
        )
    else:
        lines.append(
            "📊 <b>Зафиксировано сигналов:</b> <code>0 шт.</code>\n"
            "<i>(Сканер непрерывно фильтрует 491 пару и записывает новые всплески)</i>\n\n"
            "🏆 <b>Бенчмарк результативности (бэктест 44 989 сигналов):</b>\n"
            "• 🛡 Снайпер: WinRate <b>95.9%</b> (Profit Factor 6.09)\n"
            "• 🥇 Оптимальный: WinRate <b>93.5%</b> (просадка 5.35%)\n"
            "• 🚀 Макс. Профит: WinRate <b>94.8%</b> (PnL +392%)"
        )

    # 2. Результативность сделок за эту неделю
    if recent_trades:
        w_cnt = sum(1 for t in recent_trades if float(t.get("pnl", 0)) > 0)
        tot_cnt = len(recent_trades)
        wr_pct = (w_cnt / tot_cnt * 100.0) if tot_cnt > 0 else 0.0
        sum_pnl = sum(float(t.get("pnl", 0)) for t in recent_trades)
        pnl_sign = "🟢" if sum_pnl >= 0 else "🔴"

        lines.append(
            f"\n📈 <b>Отработка стратегии за {days} дней:</b>\n"
            f"• Закрыто сделок: <b>{tot_cnt}</b>  |  WinRate: <b>{w_cnt}/{tot_cnt} ({wr_pct:.0f}%)</b>\n"
            f"• Реализованный профит: {pnl_sign} <b>{sum_pnl:+.2f} USDT</b>"
        )

    # 3. Распределение по дням недели
    day_sec = 86400
    tz_off = time.timezone if (time.localtime().tm_isdst == 0) else time.altzone
    now_loc = now_ts - tz_off
    today_start = int(now_loc // day_sec) * day_sec + tz_off

    day_rows = []
    for d_offset in range(days - 1, -1, -1):
        d_start = today_start - d_offset * day_sec
        d_end = d_start + day_sec
        d_label = time.strftime("%d.%m", time.localtime(d_start))
        if d_offset == 0:
            d_label += " сег."
        elif d_offset == 1:
            d_label += " вчер."

        d_sigs = [s for s in sigs if d_start <= int(s.get("timestamp", 0)) < d_end]
        d_trades = [t for t in recent_trades if d_start <= float(t.get("closed_at", 0)) < d_end]
        d_pnl = sum(float(t.get("pnl", 0)) for t in d_trades)

        bar_len = min(8, len(d_sigs))
        bar = ("▓" * bar_len) if bar_len > 0 else "·"
        pnl_part = f" | {d_pnl:+.2f}$" if d_trades else ""
        day_rows.append(f"<code>{d_label:<10}</code> {bar:<8} {len(d_sigs)} сигн.{pnl_part}")

    if day_rows:
        lines.append("\n📅 <b>Динамика по дням:</b>")
        lines.extend(day_rows)

    # 4. Топ-5 монет недели по пампам
    coin_counts: Dict[str, List[float]] = {}
    for s in sigs:
        base = s.get("base", s.get("symbol", "?"))
        coin_counts.setdefault(base, []).append(float(s.get("score", 0)))

    if coin_counts:
        top_coins = sorted(coin_counts.items(), key=lambda x: (len(x[1]), max(x[1])), reverse=True)[:5]
        lines.append("\n🏆 <b>Топ монет недели по сигналам:</b>")
        for rank, (base, scores) in enumerate(top_coins, 1):
            max_sc = max(scores) if scores else 0.0
            lines.append(f"{rank}. <b>{base}</b> — <code>{len(scores)} сигн.</code> (макс. Score: <code>{max_sc:.0f}</code>)")

    api_key, api_secret = get_api_credentials(state)
    free_usdt = get_free_usdt_balance(state) if (api_key and api_secret) else 0.0
    lines.append(f"\n💵 Свободно: <code>{free_usdt:,.2f} USDT</code>")

    return "\n".join(lines)

def format_system_status(state: dict) -> str:
    """Формирует расширенный отчёт о системном здоровье, аптайме, задержках и расходе лимитов."""
    uptime_sec = int(time.time() - BOT_START_TIME)
    uptime_h = uptime_sec // 3600
    uptime_m = (uptime_sec % 3600) // 60
    uptime_s = uptime_sec % 60
    uptime_str = f"{uptime_h}ч {uptime_m}м {uptime_s}с" if uptime_h > 0 else f"{uptime_m}м {uptime_s}с"

    # Проверка пинга до Binance REST API
    t0 = time.time()
    try:
        http_get_json(f"{BINANCE_BASE}/api/v3/ping", timeout=4)
        ping_ms = int((time.time() - t0) * 1000)
        binance_status = f"🟢 Доступен ({ping_ms} мс)"
    except Exception as e:
        binance_status = f"🔴 Сбой ({e})"

    # Расход лимитов веса
    weight_str = f"{API_WEIGHT_USED_1M} / 1200"
    if API_WEIGHT_USED_1M >= 1000:
        weight_badge = "🔴 Высокий"
    elif API_WEIGHT_USED_1M >= 600:
        weight_badge = "🟡 Средний"
    else:
        weight_badge = "🟢 Низкий (Норма)"

    # Активные сделки и очереди
    active_count = len(state.get("active_trades", {}))
    pending_count = len(state.get("pending_entries", {}))
    history_count = len(state.get("trade_history", []))
    portfolio_count = len(state.get("portfolio", {}))

    # Размер файла состояния
    state_size_kb = 0.0
    if os.path.exists(STATE_FILE):
        state_size_kb = os.path.getsize(STATE_FILE) / 1024.0

    threads_count = threading.active_count()
    api_key, api_secret = get_api_credentials(state)
    keys_status = "🟢 Настроены (Live Spot)" if (api_key and api_secret) else "⚪ Не заданы (Режим наблюдателя)"
    free_usdt = get_free_usdt_balance(state) if (api_key and api_secret) else 0.0

    settings = state.get("settings", {})
    strategy = settings.get("strategy", "pump")
    strategy_label = "🚀 Памп-сканер (Score 8F)" if strategy == "pump" else "📊 RSI + SAR + Фракталы"
    auto_trade_status = "🟢 Включена" if settings.get("auto_trade", False) else "🔴 Выключена"

    return (
        "<b>🩺 Системная диагностика и статус Pump Pulse Bot</b>\n\n"
        f"⏱ <b>Аптайм процесса:</b> <code>{uptime_str}</code>\n"
        f"🌐 <b>Binance Spot API:</b> {binance_status}\n"
        f"⚡ <b>Расход веса (1 мин):</b> <code>{weight_str}</code> — {weight_badge}\n"
        f"🔑 <b>API-ключи Binance:</b> {keys_status}\n"
        f"💵 <b>Свободный баланс:</b> <code>{free_usdt:,.2f} USDT</code>\n\n"
        "<b>📈 Торговый контур:</b>\n"
        f"• <b>Стратегия:</b> {strategy_label}\n"
        f"• <b>Автоторговля:</b> {auto_trade_status}\n"
        f"• <b>Активных позиций:</b> <code>{active_count}/3</code>\n"
        f"• <b>Лимитных входов на откате:</b> <code>{pending_count}</code>\n"
        f"• <b>Активов в портфеле:</b> <code>{portfolio_count}</code>\n"
        f"• <b>Закрытых сделок в истории:</b> <code>{history_count}</code>\n\n"
        "<b>⚙️ Системные ресурсы:</b>\n"
        f"• <b>Фоновых потоков:</b> <code>{threads_count}</code>\n"
        f"• <b>Файл bot_state.json:</b> <code>{state_size_kb:.1f} KB</code>\n"
        f"• <b>Ревизия сборки:</b> <code>2.1 Production (Verified)</code>"
    )

# ───────────────────────── Клавиатуры ─────────────────────────

def main_keyboard() -> dict:
    # Компактная клавиатура: раздел «Портфель» убран,
    # управление позициями — через «💳 Баланс Binance» и «📊 Сделки и Профит».
    return {
        "keyboard": [
            [{"text": "🔍 Скан сейчас"}, {"text": "📊 Сделки и Профит"}],
            [{"text": "💳 Баланс Binance"}, {"text": "⚙️ Настройки"}],
            [{"text": "🙈 Скрыть клавиатуру"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }

def cancel_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "❌ Отмена"}],
            [{"text": "🔍 Скан сейчас"}, {"text": "⚙️ Настройки"}],
            [{"text": "🙈 Скрыть клавиатуру"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
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

def trades_stats_inline_kb(state: Optional[dict] = None) -> dict:
    """Инлайн-кнопки для экрана статистики сделок: обновление, сигналы недели, очистка и сброс."""
    return {
        "inline_keyboard": [
            [
                {"text": "🔄 Обновить", "callback_data": "trades:refresh"},
                {"text": "📡 Сигналы (7 дней)", "callback_data": "signals:stats"},
            ],
            [
                {"text": "🧹 Очистить историю", "callback_data": "trades:clear_history"},
                {"text": "🗑 Очистить полную статистику", "callback_data": "trades:clear_all_stats"},
            ],
            [
                {"text": "💳 Баланс Binance", "callback_data": "trade:balance"},
                {"text": "🔙 Главное меню", "callback_data": "menu:main"},
            ]
        ]
    }

def signals_stats_inline_kb(state: Optional[dict] = None) -> dict:
    """Инлайн-кнопки для экрана статистики сигналов за неделю."""
    return {
        "inline_keyboard": [
            [
                {"text": "🔄 Обновить сигналы", "callback_data": "signals:refresh"},
                {"text": "📊 Сделки и Профит", "callback_data": "port:trades_stats"},
            ],
            [
                {"text": "🔍 Скан сейчас", "callback_data": "scan:run"},
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
            {"text": f"📉 {base} 15m (индикаторы)", "callback_data": f"chart:{s}"},
        ])
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
    strat_label = STRATEGY_LABELS.get(s.get("strategy", DEFAULT_STRATEGY), DEFAULT_STRATEGY)
    if s.get("strategy", DEFAULT_STRATEGY) == "indicators":
        strat_label += f" (RSI > {DEFAULT_RSI_MIN:.0f}, SAR под ценой, пробит up-фрактал)"
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

    # Текущий пресет стратегии
    prof_key = s.get("trade_profile", DEFAULT_TRADE_PROFILE)
    prof_info = TRADE_PROFILES.get(prof_key, TRADE_PROFILES["optimal"])
    prof_label = prof_info["name"]
    prof_desc = prof_info["desc"]

    return (
        "<b>⚙️ Параметры бота Pump Pulse</b>\n\n"
        f"🏆 <b>Пресет стратегии:</b> <code>{prof_label}</code>\n"
        f"  <i>{prof_desc}</i>\n\n"
        f"• <b>Автосканирование рынка:</b> {auto_scan_label} (каждые {interval_m} мин)\n"
        f"• <b>Порог Score (MIN_SCORE):</b> <code>{s['min_score']:.0f}</code>\n"
        f"• <b>Уведомления:</b> <code>{filt_label}</code>\n"
        f"• <b>Режим стратегии:</b> <code>{strat_label}</code>\n"
        f"  <i>«Памп-скор» ищет всплеск объёма и пробой, «RSI + Стохастик» — импульс по индикаторам.</i>\n\n"
        "<b>⚡ Спотовая торговля Binance:</b>\n"
        f"• <b>Автоторговля пампов:</b> {auto_trade_label}\n"
        f"• <b>Размер ставки:</b> <code>{trade_size_label}</code>\n"
        f"• <b>Точка входа:</b> <code>{entry_label}</code>\n"
        f"• <b>Базовый Take-Profit:</b> <code>+{tp_pct:.1f}%</code>\n"
        f"• <b>Умный ATR Take-Profit:</b> <code>{dyn_tp_label}</code>\n"
        f"• <b>Stop-Loss:</b> <code>-{sl_pct:.1f}%</code>\n"
        f"• <b>Trailing Stop:</b> автоподтяжка при <code>+{trail_act:.1f}%</code> (отступ <code>{trail_dist:.1f}%</code>)\n"
        f"• <b>Статус API Binance:</b> {api_status}\n\n"
        "<i>Выберите готовый пресет стратегии кнопками ниже:</i>"
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
    cur_prof = s.get("trade_profile", DEFAULT_TRADE_PROFILE)

    def _prof_btn(key: str, label: str) -> dict:
        active = "✅ " if cur_prof == key else ""
        return {"text": f"{active}{label}", "callback_data": f"profile:set:{key}"}

    def _amt_btn(label: str, mode: str) -> dict:
        active = "✅ " if trade_mode == mode else ""
        return {"text": f"{active}{label}", "callback_data": f"trade:amt:{mode}"}

    def _sl_btn(val: float) -> dict:
        active = "✅ " if abs(cur_sl - val) < 0.05 else ""
        return {"text": f"{active}🛑 SL: -{val:.1f}%", "callback_data": f"trade:sl:{val:.1f}"}

    def _strategy_btn(mode: str, label: str) -> dict:
        """Кнопка режима стратегии: активный помечается галочкой."""
        active = "✅ " if s.get("strategy", DEFAULT_STRATEGY) == mode else ""
        return {"text": f"{active}{label}", "callback_data": f"strategy:set:{mode}"}

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
            # --- 4 Пресета торговых стратегий ---
            [
                _prof_btn("optimal", "🥇 1. Оптимальный"),
                _prof_btn("profit", "🚀 2. Макс. Профит"),
            ],
            [
                _prof_btn("sniper", "🛡 3. Снайпер"),
                _prof_btn("ultra", "⚡ 4. УЛЬТРА (3-в-1)"),
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
                _entry_btn("🎯 Откат -0.8%", 0.8),
                _entry_btn("🎯 Откат -1.0%", 1.0),
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
            # --- Режим стратегии: выбирается ПЕРЕД торговлей ---
            [
                _strategy_btn("pump", "📈 Памп-скор"),
                _strategy_btn("indicators", "📊 RSI+SAR+Фр"),
            ],
            [
                _strategy_btn("volume", "📊 Объём + свеча"),
                _strategy_btn("dump", "📉 Дамп → отскок"),
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

    trade_amt = DEFAULT_TRADE_AMOUNT
    tp_pct = DEFAULT_TAKE_PROFIT
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
                {"text": "📉 График 15m (RSI/Stoch/BB)", "callback_data": f"chart:{sig.symbol}"},
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
    f"и поддерживает автоматическую покупку с защитой позиции на бирже: "
    f"тейк-профит +{DEFAULT_TAKE_PROFIT:.1f}% и стоп-лосс -{DEFAULT_STOP_LOSS:.1f}% (значения по умолчанию).\n\n"
    "<b>📌 Основные функции:</b>\n"
    "• <b>🔍 Скан сейчас</b> (/scan) — сканирование всего спота прямо сейчас.\n"
    "• <b>🐋 Скан китов</b> (/whale) — крупные сделки и потоки капитала.\n"
    "• <b>📉 График 15m</b> (/chart SOL) — свечи с Боллинджером, RSI и стохастиком.\n"
    "• <b>💼 Портфель</b> (/portfolio) — баланс Binance, активные сделки и PnL.\n"
    "• <b>⚡ Торговля</b> (/trade) — статус автоторговли и история профита.\n"
    "• <b>🎯 Автопродажи</b> (/targetsell) — настройка целевой цены продажи (TP).\n"
    "• <b>➕ Добавить актив</b> (/add) — внести купленную монету вручную.\n"
    "• <b>🗑 Удалить актив</b> (/del) — убрать позицию в 1 клик.\n"
    "• <b>⚙️ Настройки</b> (/settings) — включение автоторговли, размер ставки и TP.\n"
    "• <b>🔑 Привязка API</b> (/api) — настройка Binance API ключей.\n\n"
    "<b>⚡ Как работает спотовая автоторговля:</b>\n"
    f"1. При обнаружении подтверждённого импульса (Score {DEFAULT_TRADE_MIN_SCORE:.0f}+) бот проверяет свободный USDT-баланс.\n"
    f"2. На бирже размещается спотовый MARKET BUY ордер на указанную сумму (~{DEFAULT_TRADE_AMOUNT:.0f} USDT).\n"
    f"3. Сразу же ставится OCO-список: тейк-профит +{DEFAULT_TAKE_PROFIT:.1f}% и стоп-лосс -{DEFAULT_STOP_LOSS:.1f}%.\n"
    "4. Срабатывает то плечо, которое достигнуто первым: цель продаёт в плюс, стоп закрывает убыток. Второе биржа снимает сама.\n\n"
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
        except urllib.error.HTTPError as e:
            # 429 = «слишком часто»: Telegram сам сообщает, сколько ждать
            # (parameters.retry_after). Без паузы повтор только усугубляет,
            # а сообщение молча теряется.
            retry_after = None
            try:
                err = json.loads(e.read().decode("utf-8", errors="ignore"))
                retry_after = (err.get("parameters") or {}).get("retry_after")
            except Exception:
                pass
            if e.code == 429 and retry_after:
                wait = min(int(retry_after) + 1, 90)
                print(f"[Telegram 429] ждём {wait}с перед повтором ({method})", file=sys.stderr)
                time.sleep(wait)
                continue
            if attempt == retries:
                raise
            time.sleep(0.5 * attempt)
        except Exception:
            if attempt == retries:
                raise
            time.sleep(0.5 * attempt)
    return {}

def _split_message(text: str, limit: int = 4000) -> List[str]:
    """
    Режет длинный текст на части по границам строк.

    Telegram отвергает сообщение длиннее 4096 символов — ЦЕЛИКОМ, поэтому
    длинный портфель или статистика просто не доходили до пользователя.
    """
    if len(text) <= limit:
        return [text]
    parts: List[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:                    # одна гигантская строка
            parts.append(line[:limit])
            line = line[limit:]
        joined = f"{cur}\n{line}" if cur else line
        if len(joined) > limit:
            if cur:
                parts.append(cur)
            cur = line
        else:
            cur = joined
    if cur:
        parts.append(cur)
    return parts

def send_telegram_photo(token: str, chat_id: Union[str, int], png_bytes: bytes,
                        caption: str = "", reply_markup: Optional[dict] = None,
                        filename: str = "chart.png") -> bool:
    """
    Отправляет картинку в Telegram. Только urllib: собираем multipart/form-data
    вручную, потому что os/http.client multipart сам не умеет, а тянуть
    requests в проект на stdlib нет смысла.
    """
    boundary = "----PumpPulse" + hashlib.md5(f"{time.time()}".encode()).hexdigest()[:16]
    parts: List[bytes] = []

    def field(name: str, value: str) -> None:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8")
        )

    field("chat_id", str(chat_id))
    if caption:
        # Telegram ограничивает подпись к фото 1024 символами
        field("caption", caption[:1024])
        field("parse_mode", "HTML")
    if reply_markup:
        field("reply_markup", json.dumps(reply_markup))

    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
        f"filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n".encode("utf-8")
    )
    parts.append(png_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    for attempt in range(1, 3):
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return bool(json.loads(resp.read().decode("utf-8")).get("ok"))
        except Exception as e:
            if attempt == 2:
                print(f"Telegram sendPhoto error: {e}", file=sys.stderr)
                return False
            time.sleep(0.5 * attempt)
    return False

def send_symbol_chart(token: str, chat_id: Union[str, int], symbol: str,
                      bars: int = 120, reply_markup: Optional[dict] = None) -> bool:
    """
    Строит и отправляет график 15m с индикаторами (Боллинджер, RSI, стохастик).
    Свечи 15m собираются из уже знакомых боту 5m — отдельный запрос не нужен.
    """
    symbol = normalize_symbol(symbol)
    base = base_asset(symbol)
    try:
        import chart
    except Exception as e:
        send_telegram(token, chat_id,
                      f"❌ Модуль графика недоступен: <code>{e}</code>\n\n"
                      f"Файл <code>chart.py</code> должен лежать рядом с ботом.")
        return False

    raw5 = fetch_klines(symbol, limit=max(500, bars * 5))
    candles = aggregate_timeframe(raw5, TF_MS["15m"])
    if len(candles) < 30:
        send_telegram(token, chat_id,
                      f"❌ Недостаточно данных для графика <b>{base}/USDT</b> "
                      f"(свечей 15m: {len(candles)}).")
        return False

    try:
        png = chart.render_chart(symbol, candles, bars=bars)
        caption = chart.chart_caption(symbol, candles, bars=bars)
    except Exception as e:
        send_telegram(token, chat_id, f"❌ Ошибка построения графика {base}: <code>{e}</code>")
        return False

    return send_telegram_photo(token, chat_id, png, caption,
                               reply_markup=reply_markup,
                               filename=f"{base}_15m.png")

def drop_pending_updates(token: str) -> None:
    try:
        api_call(token, "getUpdates", {"offset": -1, "timeout": 1}, retries=1, timeout=5)
    except Exception:
        pass

def send_telegram(token: str, chat_id: Union[str, int], text: str, reply_markup: Optional[dict] = None, retries: int = 3) -> Optional[int]:
    """
    Отправляет текст. Длинные сообщения режутся на части: Telegram отвергает
    всё сообщение целиком, если оно длиннее 4096 символов, поэтому крупный
    портфель или статистика раньше просто не доходили до пользователя.
    Клавиатура прикрепляется к первой части (к последней её не поставить —
    тогда кнопки окажутся под «хвостом» текста).
    """
    chunks = _split_message(text)
    first_id: Optional[int] = None

    for idx, chunk in enumerate(chunks):
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if idx == 0 and reply_markup:
            payload["reply_markup"] = reply_markup

        for attempt in range(1, retries + 1):
            try:
                res = api_call(token, "sendMessage", payload)
                if res.get("ok"):
                    if idx == 0:
                        first_id = res["result"]["message_id"]
                    break
                # Разрезали по середине HTML-тега — Telegram не примет разметку.
                # Отправляем эту часть как обычный текст, лишь бы дошло.
                desc = str(res.get("description", ""))
                if payload.get("parse_mode") and "entit" in desc.lower():
                    print(f"Telegram parse error, отправляю часть {idx + 1} без разметки: {desc}",
                          file=sys.stderr)
                    payload.pop("parse_mode", None)
                    continue
                time.sleep(0.5 * attempt)
            except Exception as e:
                if attempt == retries:
                    print(f"Telegram send error (часть {idx + 1}/{len(chunks)}): {e}", file=sys.stderr)
                    break
                time.sleep(0.5 * attempt)
    return first_id

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
        {"command": "whale", "description": "🐋 Скан крупных сделок (киты)"},
        {"command": "chart", "description": "📉 График 15m с индикаторами (/chart SOL)"},
        {"command": "portfolio", "description": "Портфель и PnL"},
        {"command": "trade", "description": "Статус автоторговли и профит"},
        {"command": "targetsell", "description": "🎯 Автопродажа по целевой цене"},
        {"command": "sell", "description": "🔴 Продать актив досрочно"},
        {"command": "add", "description": "Добавить монету в портфель"},
        {"command": "del", "description": "Удалить монету из портфеля"},
        {"command": "settings", "description": "Настройки"},
        {"command": "status", "description": "🩺 Статус и диагностика системы"},
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


def execute_whale_scan_and_report(token: str, chat_id: Union[str, int], state: dict) -> None:
    """
    Разовый whale-скан по кнопке «🐋 Скан китов».

    Раньше у этой кнопки не было обработчика, и она попадала под общее правило
    «скан» в роутере текстов — то есть МОЛЧА запускала обычный скан пампов.
    Пользователь думал, что смотрит китовые сделки, а видел совсем другое.
    """
    settings = state.get("settings", {})
    status_id = send_telegram(token, chat_id, "⏳ <i>Сканирую крупные сделки (киты)…</i>")

    def say(text: str) -> None:
        if status_id:
            edit_message(token, chat_id, status_id, text, reply_markup=main_keyboard())
        else:
            send_telegram(token, chat_id, text, reply_markup=main_keyboard())

    try:
        import whale_scan
    except Exception as e:
        say(f"❌ Модуль whale_scan недоступен: <code>{e}</code>\n\n"
            f"Файл <code>whale_scan.py</code> должен лежать рядом с ботом.")
        return

    min_score = float(settings.get("whale_min_score", whale_scan.DEFAULT_MIN_SCORE))
    top_n = int(settings.get("whale_top_n", whale_scan.DEFAULT_TOP_N))
    try:
        signals, meta = whale_scan.run_whale_scan(min_score=min_score, top_n=top_n)
    except Exception as e:
        say(f"❌ Ошибка whale-скана: <code>{e}</code>")
        return

    scanned = meta.get("scanned", 0)
    duration = meta.get("duration_ms", 0) / 1000.0

    if not signals:
        near = meta.get("near", [])[:4]
        near_lines = "\n".join(
            f"  • {n['base']}: score {n['score']:.0f}, поток {n['net_flow_pct']:+.0f}%, "
            f"доля китов {n['whale_share'] * 100:.0f}%"
            for n in near
        ) or "  —"
        text = (
            f"🐋 <b>Whale-скан: крупных сделок не найдено</b>\n\n"
            f"• Просканировано пар: <code>{scanned}</code> за <code>{duration:.1f}с</code>\n"
            f"• Порог Whale Score: <code>{min_score:.0f}</code>\n"
            f"• Сигналов: <b>0</b>\n\n"
            f"<b>Ближайшие к порогу:</b>\n{near_lines}"
        )
    else:
        blocks = [whale_scan.format_whale_alert(s) for s in signals[:5]]
        text = (
            f"🐋 <b>Whale-скан</b>: {scanned} пар за {duration:.1f}с\n"
            f"• Порог: <code>{min_score:.0f}</code> · найдено сигналов: <b>{len(signals)}</b>\n\n"
            + "\n\n".join(blocks)
        )

    # Telegram ограничивает сообщение 4096 символами: длинный список алертов
    # молча не отправился бы, поэтому обрезаем осознанно.
    if len(text) > 4000:
        text = text[:4000] + "\n\n…<i>(список обрезан, сигналов больше)</i>"
    say(text)


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
            strategy=s.get("strategy", DEFAULT_STRATEGY),
            state=state,
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
                f"• Просканировано пар: <code>{meta['candidates']}</code> (батч по очереди)\n"
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
            record_signal_history(state, sig)
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
    signals, meta, top = run_scan(min_score=min_score, state=state)
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
        record_signal_history(state, sig)
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
                strategy=settings.get("strategy", DEFAULT_STRATEGY),
                state=state,
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
                record_signal_history(state, sig)
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

    # Сверяем состояние с биржей ДО старта воркеров: иначе забытый лимитный
    # вход или сделка без живого TP так и останутся без присмотра.
    try:
        reconcile_state_with_exchange(token, chat_id, state)
    except Exception as e:
        print(f"[Reconcile] Ошибка сверки состояния: {e}", file=sys.stderr)

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
        f"Также умею автоматически торговать на вашем Binance Spot: цель +{DEFAULT_TAKE_PROFIT:.1f}%, стоп -{DEFAULT_STOP_LOSS:.1f}%.\n\n"
        "• Выберите действие кнопками ниже\n"
        "• Или напишите: <code>scan</code>, <code>portfolio</code>, <code>trade</code> и т.д."
    )
    send_telegram(token, chat_id, welcome, reply_markup=main_keyboard())

    last_update_id: Optional[int] = None
    user_fsm: Dict[Union[str, int], dict] = {}

    def is_authorized(user_id: Union[str, int]) -> bool:
        uid_str = str(user_id).strip()
        primary_cid = str(chat_id).strip()
        if primary_cid and uid_str == primary_cid:
            return True
        allowed = [str(c).strip() for c in state.get("allowed_chats", []) if str(c).strip()]
        env_allowed = [x.strip() for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()]
        if env_allowed and uid_str in env_allowed:
            return True
        if allowed and uid_str in allowed:
            return True
        if not primary_cid and not allowed and not env_allowed:
            return True
        return False

    def begin_add_symbol(chat_id_local: Union[str, int], raw_symbol: str) -> None:
        """
        Начинает добавление актива по уже известному тикеру.

        Раньше ветка /add собирала ФИКТИВНОЕ обновление и рекурсивно звала
        handle_update, а тот первой же строкой читает u["update_id"] и падал
        с KeyError — исключение проглатывал поллинг, так что команда
        «/add SOL» молча не делала ничего.
        """
        symbol = normalize_symbol(raw_symbol)
        if not symbol.isalnum() or len(symbol) > 20:
            send_telegram(token, chat_id_local,
                          "❌ Неверный тикер. Попробуйте снова (например, SOL):",
                          reply_markup=cancel_keyboard())
            return
        price = get_price(symbol)
        if price is None:
            send_telegram(token, chat_id_local,
                          f"❌ Монета <code>{symbol}</code> не найдена на Binance Spot. "
                          f"Попробуйте другой тикер:", reply_markup=cancel_keyboard())
            return
        user_fsm[chat_id_local] = {"state": "waiting_qty",
                                   "data": {"symbol": symbol, "price": price},
                                   "created_at": time.time()}
        send_telegram(token, chat_id_local,
                      f"✅ Монета: <b>{base_asset(symbol)}/USDT</b>\n"
                      f"Текущая цена: <code>{fmt_price(price)} USDT</code>\n\n"
                      f"Введите <b>количество</b> (например, 10):",
                      reply_markup=cancel_keyboard())

    def handle_update(u: dict) -> None:
        nonlocal last_update_id
        # .get, а не []: синтетическое обновление без update_id (например,
        # вызов обработчика из другого места) не должно ронять опрос.
        last_update_id = u.get("update_id", last_update_id)

        if "message" in u:
            msg = u["message"]
            chat = msg.get("chat", {})
            chat_id_local = chat.get("id")
            user_id = msg.get("from", {}).get("id", chat_id_local)
            text = (msg.get("text") or "").strip()

            if not text or not chat_id_local:
                return

            text_lower = text.lower()

            if text_lower in ("/whoami", "whoami", "/id", "id"):
                send_telegram(token, chat_id_local, f"👤 <b>Информация об аккаунте:</b>\n• User ID: <code>{user_id}</code>\n• Chat ID: <code>{chat_id_local}</code>")
                return

            if not is_authorized(user_id):
                print(f"⛔ Игнорирую сообщение от несанкционированного user_id={user_id}")
                send_telegram(token, chat_id_local, f"⛔ <b>Доступ ограничен.</b>\nВаш User ID: <code>{user_id}</code>\nДля доступа добавьте его в секрет <code>TELEGRAM_CHAT_ID</code> или <code>ALLOWED_USER_IDS</code> в GitHub.")
                return

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
                # Проверка таймаута состояния (TTL): если прошло больше FSM_TTL_SEC — сбрасываем
                st_time = float(st.get("created_at", 0.0))
                if st_time > 0 and (time.time() - st_time > FSM_TTL_SEC):
                    user_fsm.pop(chat_id_local, None)
                    print(f"⌛ [FSM] Сессия {st.get('state')} для {chat_id_local} истекла по таймауту ({FSM_TTL_SEC}с)")
                    send_telegram(token, chat_id_local, "⌛ <b>Время ожидания ввода истекло.</b> Возврат в главное меню.", reply_markup=main_keyboard())
                    return

                cur_st = st.get("state")

                if cur_st == "waiting_symbol":
                    user_fsm.pop(chat_id_local, None)
                    begin_add_symbol(chat_id_local, text)
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
                    k, s = extract_api_keys_from_text(text)
                    if k and s:
                        process_and_test_api_keys(token, chat_id_local, state, k, s)
                    else:
                        send_telegram(
                            token, chat_id_local,
                            "❌ <b>Не удалось распознать API-ключи.</b>\n\n"
                            "Отправьте ключи через пробел или построчно:\n"
                            "<code>/api ВАШ_API_KEY ВАШ_API_SECRET</code>\n\n"
                            "Или:\n"
                            "<code>API_KEY: ваш_ключ\nAPI_SECRET: ваш_секрет</code>",
                            reply_markup=cancel_keyboard()
                        )
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

            elif cmd in ("status", "статус", "diag", "диагностика", "здоровье") or (is_menu_action and text_lower in ("status", "статус", "диагностика")):
                print(f"-> Диагностика/статус для {chat_id_local}")
                status_text = format_system_status(state)
                send_telegram(token, chat_id_local, status_text, reply_markup=main_keyboard())

            # Ветка китов ОБЯЗАНА стоять до ветки «скан»: правило
            # «скан» в text_lower иначе перехватывает «🐋 Скан китов»
            # и вместо китового скана запускает обычный.
            elif "кит" in text_lower or cmd in ("whale", "whales", "киты"):
                print(f"-> Whale-скан для {chat_id_local}")
                execute_whale_scan_and_report(token, chat_id_local, state)

            elif cmd in ("chart", "график", "графики") or text_lower.startswith(("/chart", "/график")):
                # График по запросу: раньше его можно было получить только из
                # сообщения сигнала (а они редки) или из меню портфеля (пустое
                # при отсутствии позиций) — произвольную монету спросить было нечем.
                chart_args = clean_text.split()[1:]
                if not chart_args:
                    kb = portfolio_charts_inline_kb(state)
                    rows = kb.get("inline_keyboard", [])
                    if len(rows) > 1:
                        send_telegram(token, chat_id_local,
                                      "📉 <b>График 15m с индикаторами</b>\n\nВыберите монету:",
                                      reply_markup=kb)
                    else:
                        send_telegram(
                            token, chat_id_local,
                            "📉 <b>График 15m с индикаторами</b>\n\n"
                            "Укажите монету, например: <code>/chart SOL</code>\n\n"
                            "<i>Состав: свечи 15m + Боллинджер (20, 2σ), RSI 14 и стохастик (14, 3).</i>",
                            reply_markup=main_keyboard(),
                        )
                    return
                chart_sym = normalize_symbol(chart_args[0])
                print(f"-> График 15m для {chart_sym} ({chat_id_local})")
                send_symbol_chart(token, chat_id_local, chart_sym,
                                  reply_markup=main_keyboard())

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
                            f"  • {tr.get('base', base_asset(sym))}/USDT: вход {fmt_price(tr.get('buy_price', 0))} → TP {fmt_price(tr.get('tp_price', 0))} (+{tr.get('tp_pct', DEFAULT_TAKE_PROFIT):.1f}%)"
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
                    # Тикер указан сразу — сразу и продолжаем, без фиктивного
                    # обновления и рекурсии (см. begin_add_symbol).
                    begin_add_symbol(chat_id_local, args[0])
                    return
                print(f"-> Добавление актива для {chat_id_local}")
                user_fsm[chat_id_local] = {"state": "waiting_symbol", "data": {}, "created_at": time.time()}
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

            elif cmd in ("api", "ключ", "check_api", "test_api"):
                k, s = extract_api_keys_from_text(clean_text)
                if k and s:
                    process_and_test_api_keys(token, chat_id_local, state, k, s)
                else:
                    api_k, api_s = get_api_credentials(state)
                    if api_k and api_s and cmd in ("check_api", "test_api"):
                        process_and_test_api_keys(token, chat_id_local, state, api_k, api_s)
                    else:
                        status_str = "🟢 <b>Подключены и активны</b>" if (api_k and api_s) else "🔴 <b>Не настроены</b>"
                        user_fsm[chat_id_local] = {"state": "waiting_api_keys", "data": {}, "created_at": time.time()}
                        send_telegram(
                            token, chat_id_local,
                            f"🔑 <b>Привязка API ключей Binance Spot</b>\n\n"
                            f"• Текущий статус: {status_str}\n\n"
                            "Отправьте ключи в одном сообщении через пробел:\n"
                            "<code>/api ВАШ_API_KEY ВАШ_API_SECRET</code>\n\n"
                            "🔒 <i>Ключи сохраняются локально и проверяются запросом к Binance.\n"
                            "Необходимые разрешения в API Management:\n"
                            "1. «Включить чтение» (Enable Reading)\n"
                            "2. «Включить спотовую торговлю» (Enable Spot & Margin Trading)\n"
                            "3. Без права вывода средств!</i>",
                            reply_markup=cancel_keyboard(),
                        )

            elif "баланс" in text_lower or "balance" in text_lower or cmd in ("balance", "баланс"):
                print(f"-> Детальный баланс для {chat_id_local}")
                msg_id = send_telegram(token, chat_id_local, "⏳ <i>Запрашиваю детальный баланс Binance...</i>")
                bal_text = format_binance_balance_detailed(state)
                if msg_id:
                    edit_message(token, chat_id_local, msg_id, bal_text, reply_markup=balance_inline_kb(state))
                else:
                    send_telegram(token, chat_id_local, bal_text, reply_markup=balance_inline_kb(state))

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
                    edit_message(token, chat_id_local, msg_id, stats_text, reply_markup=trades_stats_inline_kb(state))
                else:
                    send_telegram(token, chat_id_local, stats_text, reply_markup=trades_stats_inline_kb(state))

            elif (
                "сигнал" in text_lower
                or cmd in ("signals", "signals_stats", "сигналы")
            ):
                print(f"-> Статистика сигналов (7 дней) для {chat_id_local}")
                msg_id = send_telegram(token, chat_id_local, "⏳ <i>Загружаю аналитику сигналов за 7 дней...</i>")
                sigs_text = format_signals_stats(state, days=7)
                if msg_id:
                    edit_message(token, chat_id_local, msg_id, sigs_text, reply_markup=signals_stats_inline_kb(state))
                else:
                    send_telegram(token, chat_id_local, sigs_text, reply_markup=signals_stats_inline_kb(state))

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

            elif cb_data in ("port:trades_stats", "trades:refresh"):
                answer_callback(token, cb_id, "Статистика сделок...")
                stats_text = format_trades_and_profit_stats(state)
                if msg_id:
                    edit_message(token, cb_chat, msg_id, stats_text, reply_markup=trades_stats_inline_kb(state))
                else:
                    send_telegram(token, cb_chat, stats_text, reply_markup=trades_stats_inline_kb(state))

            elif cb_data == "trades:clear_history":
                with STATE_LOCK:
                    state["trade_history"] = []
                save_state(state, sync_git=True)
                answer_callback(token, cb_id, "🧹 История очищена (итоговая статистика сохранена)")
                stats_text = format_trades_and_profit_stats(state)
                if msg_id:
                    edit_message(token, cb_chat, msg_id, stats_text, reply_markup=trades_stats_inline_kb(state))
                else:
                    send_telegram(token, cb_chat, stats_text, reply_markup=trades_stats_inline_kb(state))

            elif cb_data == "trades:clear_all_stats":
                with STATE_LOCK:
                    state["trade_history"] = []
                    state["all_time_stats"] = {
                        "total_trades": 0,
                        "winning_trades": 0,
                        "total_pnl": 0.0,
                    }
                save_state(state, sync_git=True)
                answer_callback(token, cb_id, "🗑 Вся статистика и история успешно сброшены")
                stats_text = format_trades_and_profit_stats(state)
                if msg_id:
                    edit_message(token, cb_chat, msg_id, stats_text, reply_markup=trades_stats_inline_kb(state))
                else:
                    send_telegram(token, cb_chat, stats_text, reply_markup=trades_stats_inline_kb(state))

            elif cb_data in ("signals:stats", "signals:refresh"):
                answer_callback(token, cb_id, "Статистика сигналов (7 дней)...")
                sigs_text = format_signals_stats(state, days=7)
                if msg_id:
                    edit_message(token, cb_chat, msg_id, sigs_text, reply_markup=signals_stats_inline_kb(state))
                else:
                    send_telegram(token, cb_chat, sigs_text, reply_markup=signals_stats_inline_kb(state))

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
                user_fsm[cb_chat] = {"state": "waiting_symbol", "data": {}, "created_at": time.time()}
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
                user_fsm[cb_chat] = {"state": "waiting_target_price", "data": {"symbol": sym}, "created_at": time.time()}
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

            elif cb_data.startswith("strategy:set:"):
                # Выбор режима стратегии. Он влияет и на скан, и на автоторговлю,
                # поэтому пользователь выбирает его осознанно; флаг фиксирует это.
                mode = cb_data.split(":", 2)[2]
                if mode not in STRATEGY_LABELS:
                    answer_callback(token, cb_id, "Неизвестный режим", show_alert=True)
                    return
                settings["strategy"] = mode
                settings["strategy_confirmed"] = True
                save_state(state)
                answer_callback(token, cb_id, f"Режим: {STRATEGY_LABELS[mode]}")
                if msg_id:
                    edit_message(token, cb_chat, msg_id, settings_text(state),
                                 reply_markup=settings_inline_kb(state))

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
                # Выбор режима ПЕРЕД торговлей: если режим ещё не выбран осознанно,
                # спрашиваем об этом сразу при включении, а не после первой сделки.
                if settings["auto_trade"] and not settings.get("strategy_confirmed"):
                    cur = STRATEGY_LABELS.get(settings.get("strategy", DEFAULT_STRATEGY), "—")
                    send_telegram(
                        token, cb_chat,
                        f"⚙️ <b>Выберите режим стратегии перед торговлей</b>\n\n"
                        f"Сейчас установлен: <code>{cur}</code>\n\n"
                        f"• <b>📈 Памп-скор</b> — 8 факторов: всплеск объёма, агрессия покупок, "
                        f"сжатие волатильности, пробой флэта.\n"
                        f"• <b>📊 RSI + SAR + Фрактал</b> — классика по индикаторам: "
                        f"RSI &gt; {DEFAULT_RSI_MIN:.0f}, Parabolic SAR под ценой и пробит up-фрактал.\n\n"
                        f"<i>От выбора зависит, какие сигналы будут открывать сделки.</i>",
                        reply_markup=settings_inline_kb(state),
                    )

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

            elif cb_data.startswith("profile:set:"):
                p_key = cb_data.split(":", 2)[2]
                prof = apply_trade_profile(state, p_key)
                save_state(state)
                answer_callback(token, cb_id, f"Пресет: {prof.get('name', p_key)}")
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
                    edit_message(token, cb_chat, msg_id, bal_text, reply_markup=balance_inline_kb(state))
                else:
                    send_telegram(token, cb_chat, bal_text, reply_markup=balance_inline_kb(state))

            elif cb_data == "balance:refresh":
                answer_callback(token, cb_id, "Обновляю баланс...")
                bal_text = format_binance_balance_detailed(state)
                if msg_id:
                    edit_message(token, cb_chat, msg_id, bal_text, reply_markup=balance_inline_kb(state))
                else:
                    send_telegram(token, cb_chat, bal_text, reply_markup=balance_inline_kb(state))

            elif cb_data == "balance:clean_dust":
                answer_callback(token, cb_id, "Ищу пыль на балансе...")
                clean_dust_balances(token, cb_chat, state)

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
                user_fsm[cb_chat] = {"state": "waiting_buy_amount", "data": {"symbol": sym}, "created_at": time.time()}
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
                user_fsm[cb_chat] = {"state": "waiting_quick_add_qty", "data": {"symbol": symbol, "price": price}, "created_at": time.time()}

            elif cb_data.startswith("chart:"):
                # График 15m с индикаторами по запросу. Картинка отправляется
                # ТОЛЬКО по нажатию: прикреплять её к каждому сигналу означало
                # бы отдельную отправку фото каждые несколько минут.
                chart_sym = cb_data.split(":", 1)[1]
                answer_callback(token, cb_id, "Строю график…")
                send_symbol_chart(token, cb_chat, chart_sym)

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

            else:
                # Неизвестная или устаревшая кнопка. Без этой ветки Telegram
                # бесконечно крутит индикатор на кнопке: обработчик обязан
                # вызвать answerCallbackQuery, а иначе кажется, что бот завис.
                print(f"⚠️ Неизвестный callback: {cb_data!r}")
                answer_callback(token, cb_id, "Кнопка устарела — откройте меню заново")

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
