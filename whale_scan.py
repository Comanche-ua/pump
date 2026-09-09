#!/usr/bin/env python3
"""
Whale Scan Engine 1.0 — детектор действий китов на спотовом рынке Binance.

Режимы работы:
  1. ОТДЕЛЬНЫЙ (автономный):
       python whale_scan.py                — скан в консоль
       python whale_scan.py --oneshot      — разовый скан + алерты в Telegram (для CI/cron)
       python whale_scan.py --symbol SOL   — глубокий анализ одной монеты
  2. СОВМЕСТНЫЙ: импортируется pump_bot.py как модуль `whale_scan`.
     Сигналы приходят в тот же Telegram-бот и могут включать автоторговлю.

Что детектируем как «действия китов»:
  • Крупные принты — сделки от WHALE_MIN_NOTIONAL_USDT (по умолч. $100 000)
    и одновременно >= WHALE_MEDIAN_MULT x медиана пары;
  • Доля китовского объёма в общем потоке за окно (по умолч. 15 минут);
  • Чистый поток китов: (покупки - продажи) / китовский объём, %;
  • Концентрация: топ-20 крупнейших сделок как доля китовского объёма;
  • Ускорение: рост медианного размера сделки во второй половине окна;
  • Стены в стакане: бид/аск топ-10 уровней (с оговоркой о спуфинге);
  • Опционально — подтверждение фьючерсами (OI + Taker Buy/Sell, публичные эндпоинты).
"""

from __future__ import annotations

import os
import sys
import time
import json
import urllib.request
import urllib.error
import concurrent.futures as cf
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ───────────────────────── Config ─────────────────────────

BINANCE_BASE = os.environ.get("BINANCE_DATA_BASE", "https://data-api.binance.vision")
FAPI_BASE = os.environ.get("BINANCE_FAPI_BASE", "https://fapi.binance.com")

WINDOW_SEC = int(os.environ.get("WHALE_WINDOW_SEC", "900"))            # окно анализа сделок, сек
AGG_TRADES_LIMIT = int(os.environ.get("WHALE_TRADES_LIMIT", "1000"))
DEFAULT_MIN_NOTIONAL = float(os.environ.get("WHALE_MIN_NOTIONAL_USDT", "100000"))  # мин. размер принта-кита, $
DEFAULT_MIN_SCORE = float(os.environ.get("WHALE_MIN_SCORE", "60"))
DEFAULT_TOP_N = int(os.environ.get("WHALE_TOP_N", "30"))
DEFAULT_MIN_QUOTE_VOLUME = float(os.environ.get("MIN_QUOTE_VOLUME", "1200000"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "16"))
WHALE_MEDIAN_MULT = float(os.environ.get("WHALE_MEDIAN_MULT", "25"))   # кит = сделка >= 25x медианы пары
USE_FUTURES_CONFIRM = os.environ.get("WHALE_FUTURES_CONFIRM", "true").strip().lower() in ("true", "1")
STATE_FILE = os.environ.get("WHALE_STATE_FILE", "whale_state.json")

STABLE_OR_FIAT = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "EUR", "EURI", "AEUR",
    "TRY", "BRL", "ARS", "IDRT", "UAH", "NGN", "GBP", "AUD",
    "USDP", "USDS", "USD1", "PYUSD", "RLUSD", "BFUSD",
}
LEV_RE = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")

# ───────────────────────── Types ─────────────────────────

@dataclass
class WhalePrint:
    ts_ms: int
    price: float
    qty: float
    notional: float
    side: str            # "BUY" | "SELL" (по стороне тейкера)

@dataclass
class WhaleSignal:
    symbol: str
    base: str
    price: float
    window_sec: int
    window_start: int    # ms
    window_end: int      # ms
    total_notional: float
    whale_notional: float
    whale_share: float          # 0..1 — доля китовского объёма в потоке
    whale_buys: int
    whale_sells: int
    whale_buy_notional: float
    whale_sell_notional: float
    net_flow_pct: float         # -100..+100
    concentration_top20: float  # 0..1
    median_trade_usdt: float
    biggest_print_usdt: float
    whale_threshold_usdt: float
    accel_ratio: float
    window_change_pct: float
    bid_wall_ratio: float       # бид/аск топ-10
    oi_change_pct: Optional[float]
    taker_ratio: Optional[float]
    score: float
    grade: str                  # accumulation | distribution | watch | none
    alert_key: str
    factors: List[Tuple[str, float, float]] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    prints: List[WhalePrint] = field(default_factory=list)

# ───────────────────────── Helpers ─────────────────────────

def base_of(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol

def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))

def pct_change(a: float, b: float) -> float:
    return 0.0 if a == 0 else (b - a) / a * 100.0

def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0

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

def http_get_json(url: str, timeout: int = 10, retries: int = 2):
    """GET с ретраями и экспоненциальной паузой (улучшение отказоустойчивости)."""
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "whale-scan/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
    raise last_err  # type: ignore[misc]

# ───────────────────────── Data fetchers ─────────────────────────

def fetch_agg_trades(symbol: str, window_sec: int = WINDOW_SEC) -> List[WhalePrint]:
    """
    Агрегированные сделки за окно. aggTrades отдаёт максимум `limit` записей
    от startTime — если сделок в окне больше лимита, добираем самые свежие
    из последней минуты и мержим по ID (улучшение: не теряем свежие принты).
    """
    now_ms = int(time.time() * 1000)
    start = now_ms - window_sec * 1000
    url = (f"{BINANCE_BASE}/api/v3/aggTrades?symbol={symbol}"
           f"&startTime={start}&endTime={now_ms}&limit={AGG_TRADES_LIMIT}")
    try:
        raw = http_get_json(url, timeout=10)
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    if len(raw) >= AGG_TRADES_LIMIT:
        tail_url = (f"{BINANCE_BASE}/api/v3/aggTrades?symbol={symbol}"
                    f"&startTime={now_ms - 60_000}&endTime={now_ms}&limit={AGG_TRADES_LIMIT}")
        try:
            raw2 = http_get_json(tail_url, timeout=10)
            if isinstance(raw2, list):
                seen = {r.get("a") for r in raw}
                raw.extend([r for r in raw2 if r.get("a") not in seen])
        except Exception:
            pass
    prints: List[WhalePrint] = []
    for r in raw:
        try:
            qty = float(r.get("q", 0)); pr = float(r.get("p", 0))
            if qty <= 0 or pr <= 0:
                continue
            # m (isBuyerMaker)=True → покупатель мейкер → тейкер продавал = MARKET SELL
            prints.append(WhalePrint(
                ts_ms=int(r.get("T", 0)),
                price=pr, qty=qty, notional=pr * qty,
                side="SELL" if r.get("m") else "BUY",
            ))
        except Exception:
            continue
    prints.sort(key=lambda p: p.ts_ms)
    return prints

def fetch_depth_ratio(symbol: str, levels: int = 10) -> float:
    """Отношение объёма топ-N бидам к топ-N аскам (стены ликвидности)."""
    try:
        data = http_get_json(f"{BINANCE_BASE}/api/v3/depth?symbol={symbol}&limit=100", timeout=8)
        bids = data.get("bids", [])[:levels]
        asks = data.get("asks", [])[:levels]
        bid_n = sum(float(b[0]) * float(b[1]) for b in bids)
        ask_n = sum(float(a[0]) * float(a[1]) for a in asks)
        if ask_n <= 0:
            return 10.0 if bid_n > 0 else 1.0
        return bid_n / ask_n
    except Exception:
        return 1.0

def fetch_futures_confirm(symbol: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Публичные фьючерсные данные (без API-ключа):
    изменение Open Interest за ~30 мин и средний Taker Buy/Sell ratio.
    Для пар без фьючерса возвращает (None, None) — бонусный фактор просто 0.
    """
    oi_change: Optional[float] = None
    taker_ratio: Optional[float] = None
    try:
        oi = http_get_json(f"{FAPI_BASE}/futures/data/openInterestHist?symbol={symbol}&period=5m&limit=6", timeout=8)
        if isinstance(oi, list) and len(oi) >= 2:
            first = float(oi[0].get("sumOpenInterest", 0))
            last = float(oi[-1].get("sumOpenInterest", 0))
            if first > 0:
                oi_change = (last - first) / first * 100.0
    except Exception:
        pass
    try:
        tk = http_get_json(f"{FAPI_BASE}/futures/data/takerlongshortRatio?symbol={symbol}&period=5m&limit=6", timeout=8)
        if isinstance(tk, list) and tk:
            ratios = [float(x["buySellRatio"]) for x in tk if x.get("buySellRatio")]
            if ratios:
                taker_ratio = sum(ratios) / len(ratios)
    except Exception:
        pass
    return oi_change, taker_ratio

# ───────────────────────── Analyzer ─────────────────────────

def analyze_whale(
    symbol: str,
    trades: List[WhalePrint],
    price: float,
    bid_wall_ratio: float,
    futures: Tuple[Optional[float], Optional[float]],
    now_ms: int,
    min_score: float = DEFAULT_MIN_SCORE,
) -> Optional[WhaleSignal]:
    if len(trades) < 20:
        return None
    total_notional = sum(p.notional for p in trades)
    if total_notional <= 0:
        return None

    med = _median([p.notional for p in trades])
    whale_threshold = max(DEFAULT_MIN_NOTIONAL, med * WHALE_MEDIAN_MULT)
    prints = sorted((p for p in trades if p.notional >= whale_threshold),
                    key=lambda p: -p.notional)
    whale_notional = sum(p.notional for p in prints)
    whale_share = whale_notional / total_notional if total_notional > 0 else 0.0

    buy_p = [p for p in prints if p.side == "BUY"]
    sell_p = [p for p in prints if p.side == "SELL"]
    buy_n = sum(p.notional for p in buy_p)
    sell_n = sum(p.notional for p in sell_p)
    net_flow_pct = ((buy_n - sell_n) / whale_notional * 100.0) if whale_notional > 0 else 0.0
    concentration = (sum(p.notional for p in prints[:20]) / whale_notional) if whale_notional > 0 else 0.0

    t0, t1 = trades[0].ts_ms, trades[-1].ts_ms
    mid = t0 + (t1 - t0) / 2
    older = [p.notional for p in trades if p.ts_ms < mid]
    recent = [p.notional for p in trades if p.ts_ms >= mid]
    med_older, med_recent = _median(older), _median(recent)
    accel = (med_recent / med_older) if med_older > 0 else 1.0
    chg = pct_change(trades[0].price, trades[-1].price)
    oi_change, taker_ratio = futures if futures else (None, None)

    # ── ФАКТОРЫ WHALE SCORE (всего 100 баллов) ──
    share_f = clamp01((whale_share - 0.02) / 0.18)                 # 25б: 2%→20% доля китов
    net_f = clamp01((net_flow_pct + 25.0) / 100.0)                 # 25б: поток -25%→+75%
    conc_f = clamp01((concentration - 0.35) / 0.35)                # 15б: концентрация 35%→70%
    buy_dom = len(buy_p) / max(1, len(prints))
    dom_f = clamp01((buy_dom - 0.40) / 0.30)                       # 10б: покупки 40%→70% принтов
    accel_f = clamp01((accel - 1.0) / 2.0)                         # 10б: ускорение 1x→3x
    wall_f = clamp01((bid_wall_ratio - 1.0) / 1.5)                 # 10б: стена 1x→2.5x
    fut_f = 0.0                                                    # 5б: подтверждение фьючерсами
    if oi_change is not None and oi_change > 1.0:
        fut_f += 0.5
    if taker_ratio is not None and taker_ratio > 1.05:
        fut_f += 0.5

    factors: List[Tuple[str, float, float]] = [
        ("Доля китов в потоке", share_f * 25.0, 25.0),
        ("Чистый поток (BUY-SELL)", net_f * 25.0, 25.0),
        ("Концентрация топ-20 принтов", conc_f * 15.0, 15.0),
        ("Доминирование китовых покупок", dom_f * 10.0, 10.0),
        ("Ускорение размера сделок", accel_f * 10.0, 10.0),
        ("Стена заявок в стакане", wall_f * 10.0, 10.0),
        ("Подтверждение фьючерсами (OI/Taker)", fut_f * 5.0, 5.0),
    ]
    score = sum(f[1] for f in factors)

    if score < min_score:
        grade = "none"
    elif net_flow_pct >= 15:
        grade = "accumulation"
    elif net_flow_pct <= -15:
        grade = "distribution"
    else:
        grade = "watch"

    reasons: List[str] = []
    if whale_share >= 0.12:
        reasons.append(f"китовский объём {whale_share*100:.0f}% всего потока за {WINDOW_SEC//60} мин")
    if net_flow_pct >= 40:
        reasons.append(f"чистый приток +{net_flow_pct:.0f}% — киты покупают агрессивнее, чем продают")
    if net_flow_pct <= -40:
        reasons.append(f"чистый отток {net_flow_pct:.0f}% — киты распродают позиции")
    if concentration >= 0.60 and prints:
        reasons.append(f"топ-20 сделок держит {concentration*100:.0f}% китовского объёма — целенаправленный набор")
    if accel >= 1.8:
        reasons.append(f"медианный размер сделки вырос в {accel:.1f}× за окно")
    if bid_wall_ratio >= 1.8:
        reasons.append(f"стена покупок в стакане ×{bid_wall_ratio:.1f} к стене продаж")
    if oi_change is not None and oi_change > 2.0:
        reasons.append(f"Open Interest +{oi_change:.1f}% за 30 мин (новые длинные позиции)")
    if taker_ratio is not None and taker_ratio >= 1.15:
        reasons.append(f"на фьючерсах доминируют покупки (Taker ratio {taker_ratio:.2f})")

    risks: List[str] = []
    if len(prints) <= 2:
        risks.append(f"всего {len(prints)} крупная сделка за окно — статистически слабая выборка")
    if bid_wall_ratio >= 1.8:
        risks.append("стены в стакане могут быть спуфингом (снимаются перед касанием)")
    if grade == "distribution":
        risks.append("распродажа китов — не входить в лонг")
    if chg < 0 and net_flow_pct > 20:
        risks.append("цена пока не реагирует на покупки (поглощение) — ждите подтверждения пробоя")
    if chg > 3.0:
        risks.append(f"цена уже {chg:+.1f}% за окно — не входить по верхам")

    return WhaleSignal(
        symbol=symbol,
        base=base_of(symbol),
        price=price,
        window_sec=WINDOW_SEC,
        window_start=t0,
        window_end=t1,
        total_notional=total_notional,
        whale_notional=whale_notional,
        whale_share=whale_share,
        whale_buys=len(buy_p),
        whale_sells=len(sell_p),
        whale_buy_notional=buy_n,
        whale_sell_notional=sell_n,
        net_flow_pct=net_flow_pct,
        concentration_top20=concentration,
        median_trade_usdt=med,
        biggest_print_usdt=prints[0].notional if prints else 0.0,
        whale_threshold_usdt=whale_threshold,
        accel_ratio=accel,
        window_change_pct=chg,
        bid_wall_ratio=bid_wall_ratio,
        oi_change_pct=oi_change,
        taker_ratio=taker_ratio,
        score=score,
        grade=grade,
        alert_key=f"WHALE:{symbol}:{t1 // 3_600_000}",   # дедуп: не чаще 1 алерта в час на монету
        factors=factors,
        reasons=reasons,
        risks=risks,
        prints=prints[:20],
    )

# ───────────────────────── Universe / Scan ─────────────────────────

def get_24h_tickers() -> List[dict]:
    data = http_get_json(f"{BINANCE_BASE}/api/v3/ticker/24hr", timeout=15)
    out = []
    for d in data:
        sym = d.get("symbol", "")
        if not is_usdt_spot(sym):
            continue
        try:
            out.append({
                "symbol": sym,
                "lastPrice": float(d["lastPrice"]),
                "priceChangePercent": float(d["priceChangePercent"]),
                "quoteVolume": float(d["quoteVolume"]),
            })
        except Exception:
            continue
    return out

def run_whale_scan(
    min_score: Optional[float] = None,
    top_n: Optional[int] = None,
    min_quote_volume: Optional[float] = None,
    tickers: Optional[List[dict]] = None,
) -> Tuple[List[WhaleSignal], dict]:
    """Скан топ-N ликвидных USDT-пар. Возвращает (сигналы, мета с ближайшими кандидатами)."""
    started = time.time()
    ms = min_score if min_score is not None else DEFAULT_MIN_SCORE
    tn = top_n if top_n is not None else DEFAULT_TOP_N
    vq = min_quote_volume if min_quote_volume is not None else DEFAULT_MIN_QUOTE_VOLUME

    if tickers is None:
        tickers = get_24h_tickers()
    liquid = [t for t in tickers if t.get("quoteVolume", 0) >= vq]
    targets = sorted(liquid, key=lambda t: t["quoteVolume"], reverse=True)[:tn]

    def worker(t: dict) -> Optional[WhaleSignal]:
        sym = t["symbol"]
        trades = fetch_agg_trades(sym)
        if len(trades) < 20:
            return None
        bid_ratio = fetch_depth_ratio(sym)
        fut = fetch_futures_confirm(sym) if USE_FUTURES_CONFIRM else (None, None)
        price = trades[-1].price if trades else t.get("lastPrice", 0.0)
        return analyze_whale(sym, trades, price, bid_ratio, fut,
                             int(time.time() * 1000), min_score=ms)

    results: List[WhaleSignal] = []
    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for sig in pool.map(worker, targets):
            if sig:
                results.append(sig)

    results.sort(key=lambda s: -s.score)
    signals = [s for s in results if s.grade != "none"]
    near = [
        {
            "symbol": s.symbol, "base": s.base, "score": s.score,
            "net_flow_pct": s.net_flow_pct, "whale_share": s.whale_share,
            "whale_buys": s.whale_buys, "whale_sells": s.whale_sells,
        }
        for s in results if s.grade == "none"
    ][:5]

    meta = {
        "universe": len(liquid),
        "scanned": len(targets),
        "duration_ms": int((time.time() - started) * 1000),
        "min_score": ms,
        "near": near,
    }
    return signals, meta

def deep_dive(symbol: str, min_score: float = 0.0) -> Optional[WhaleSignal]:
    """Глубокий анализ одной монеты (команда /whale BTC или whale_scan.py --symbol BTC)."""
    symbol = symbol.strip().upper().replace("/", "")
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    trades = fetch_agg_trades(symbol)
    if len(trades) < 20:
        return None
    price = trades[-1].price
    bid_ratio = fetch_depth_ratio(symbol)
    fut = fetch_futures_confirm(symbol) if USE_FUTURES_CONFIRM else (None, None)
    return analyze_whale(symbol, trades, price, bid_ratio, fut,
                         int(time.time() * 1000), min_score=min_score)

# ───────────────────────── Formatters ─────────────────────────

def fmt_usd(val: Optional[float]) -> str:
    if val is None:
        return "—"
    if val >= 1_000_000:
        return f"${val/1_000_000:.2f}M"
    if val >= 1_000:
        return f"${val/1_000:.1f}K"
    return f"${val:.0f}"

def fmt_price(val: Optional[float]) -> str:
    if val is None:
        return "—"
    if val == 0:
        return "0.00"
    a = abs(val)
    if a >= 1000:
        return f"{val:,.2f}"
    if a >= 1:
        return f"{val:.4f}".rstrip("0").rstrip(".")
    if a >= 0.0001:
        return f"{val:.6f}".rstrip("0").rstrip(".")
    return f"{val:.8f}".rstrip("0").rstrip(".")

def fmt_time(ts_ms: int) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(ts_ms / 1000))

_WHALE_TITLES = {
    "accumulation": "🐋 АККУМУЛЯЦИЯ КИТОВ",
    "distribution": "🐋 РАЗГРУЗКА КИТОВ (SELL)",
    "watch": "🐋 АКТИВНОСТЬ КИТОВ",
}

def format_whale_alert(sig: WhaleSignal) -> str:
    title = _WHALE_TITLES.get(sig.grade, "🐋 WHALE SCAN")
    reasons = "\n".join(f"  ✓ {r}" for r in sig.reasons[:5]) or "  • данных мало"
    risks = ""
    if sig.risks:
        risks = "\n<b>Факторы риска:</b>\n" + "\n".join(f"  • {r}" for r in sig.risks[:3])

    flow_emoji = "📥" if sig.net_flow_pct >= 0 else "📤"
    fut_line = ""
    if sig.oi_change_pct is not None:
        fut_line = f"📦 <b>Open Interest (фьюч.):</b> <code>{sig.oi_change_pct:+.1f}% за 30м</code>\n"
        if sig.taker_ratio is not None:
            fut_line += f"⚔️ <b>Taker Buy/Sell (фьюч.):</b> <code>{sig.taker_ratio:.2f}</code>\n"

    return (
        f"<b>{title} · {sig.base}/USDT</b>\n\n"
        f"🎯 <b>Whale Score:</b> <code>{sig.score:.0f}/100</code>\n"
        f"💵 <b>Цена:</b> <code>{fmt_price(sig.price)} USDT</code> "
        f"<i>({sig.window_change_pct:+.2f}% за {sig.window_sec//60} мин)</i>\n"
        f"🌊 <b>Китовский объём:</b> <code>{fmt_usd(sig.whale_notional)}</code> "
        f"<i>({sig.whale_share*100:.0f}% потока, медиана {fmt_usd(sig.median_trade_usdt)})</i>\n"
        f"🖨 <b>Принты китов (≥{fmt_usd(sig.whale_threshold_usdt)}):</b> "
        f"<code>{sig.whale_buys} BUY / {sig.whale_sells} SELL</code>\n"
        f"{flow_emoji} <b>Чистый поток китов:</b> <code>{sig.net_flow_pct:+.0f}%</code>\n"
        f"📊 <b>Концентрация топ-20:</b> <code>{sig.concentration_top20*100:.0f}%</code> | "
        f"<b>Стена бид/аск:</b> <code>{sig.bid_wall_ratio:.1f}×</code>\n"
        f"{fut_line}\n"
        f"<b>Что произошло:</b>\n{reasons}"
        f"{risks}"
    )

def format_whale_detail(sig: WhaleSignal) -> str:
    base_text = format_whale_alert(sig)
    factor_lines = "\n".join(
        f"  • {name}: <code>{got:.1f}/{mx:.0f}</code>"
        for name, got, mx in sig.factors
    )
    print_lines = []
    for i, p in enumerate(sig.prints[:10], 1):
        icon = "🟢 BUY " if p.side == "BUY" else "🔴 SELL"
        print_lines.append(
            f"{i}. {icon} <code>{fmt_usd(p.notional)}</code> @ <code>{fmt_price(p.price)}</code>  <i>{fmt_time(p.ts_ms)} UTC</i>"
        )
    prints_block = "\n".join(print_lines) if print_lines else "<i>крупных принтов за окно не найдено</i>"
    return (
        f"{base_text}\n\n"
        f"<b>🧬 Разбор Whale Score:</b>\n{factor_lines}\n\n"
        f"<b>🔎 Топ-{min(10, len(sig.prints))} крупнейших принтов (последние {sig.window_sec//60} мин):</b>\n"
        f"{prints_block}"
    )

# ───────────────────────── Standalone state (дедуп алертов в CI) ─────────────────────────

def load_sent_alerts() -> Dict[str, int]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        sent = data.get("sent_alerts", {})
        return sent if isinstance(sent, dict) else {}
    except Exception:
        return {}

def save_sent_alerts(sent: Dict[str, int]) -> None:
    cutoff = int(time.time()) - 86400
    sent = {k: ts for k, ts in sent.items() if ts > cutoff}
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"sent_alerts": sent}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Не удалось сохранить {STATE_FILE}: {e}", file=sys.stderr)

# ───────────────────────── Standalone Telegram sender ─────────────────────────

def send_telegram_simple(token: str, chat_id: str, text: str) -> bool:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return bool(json.loads(resp.read().decode("utf-8")).get("ok"))
    except Exception as e:
        print(f"Telegram error: {e}", file=sys.stderr)
        return False

# ───────────────────────── Standalone modes ─────────────────────────

def run_oneshot() -> None:
    """Разовый скан для CI/cron: шлёт алерты в Telegram, если настроены токен/chat_id."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    print(f"=== Whale Scan oneshot (окно {WINDOW_SEC//60} мин, порог {DEFAULT_MIN_SCORE:.0f}, принт ≥ ${DEFAULT_MIN_NOTIONAL:,.0f}) ===")

    signals, meta = run_whale_scan()
    print(f"scanned={meta['scanned']} из {meta['universe']} за {meta['duration_ms']/1000:.1f}с, сигналов={len(signals)}")

    if not token or not chat_id:
        for s in signals:
            print(f"[{s.grade.upper()}] {s.symbol} score={s.score:.0f} "
                  f"net_flow={s.net_flow_pct:+.0f}% share={s.whale_share*100:.0f}% "
                  f"prints={s.whale_buys}B/{s.whale_sells}S")
        for n in meta["near"][:5]:
            print(f"  ~ {n['base']}: score={n['score']:.0f} (порог {meta['min_score']:.0f})")
        return

    sent = load_sent_alerts()
    now = int(time.time())
    count = 0
    for sig in signals:
        if sig.alert_key in sent:
            print(f"• Пропуск {sig.symbol}: алерт за этот час уже отправлен")
            continue
        sent[sig.alert_key] = now
        count += 1
        ok = send_telegram_simple(token, chat_id, format_whale_alert(sig))
        print(f"• {sig.symbol} ({sig.grade} {sig.score:.0f}) → chat {chat_id}: {'sent' if ok else 'fail'}")

    if count > 0:
        save_sent_alerts(sent)
        print(f"Отправлено новых whale-сигналов: {count}")
    else:
        print("Новых whale-сигналов нет.")
        is_manual = (
            os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            or os.environ.get("NOTIFY_EMPTY", "false").lower() in ("true", "1")
        )
        if is_manual:
            near_lines = "\n".join(
                f"  • {n['base']}: score {n['score']:.0f}, поток {n['net_flow_pct']:+.0f}%, "
                f"доля китов {n['whale_share']*100:.0f}% ({n['whale_buys']}B/{n['whale_sells']}S)"
                for n in meta["near"][:4]
            ) or "  —"
            send_telegram_simple(
                token, chat_id,
                f"🐋 <b>Whale Scan — отчёт</b>\n\n"
                f"• Просканировано топ-пар: <code>{meta['scanned']}</code>\n"
                f"• Порог Whale Score: <code>{meta['min_score']:.0f}</code>\n"
                f"• Сигналов: <b>0</b> <i>(киты спят)</i>\n\n"
                f"<b>Ближайшие к порогу:</b>\n{near_lines}",
            )
            print("Отправлен статус-отчёт о спокойном рынке.")

def main() -> None:
    if "--symbol" in sys.argv:
        i = sys.argv.index("--symbol")
        sym = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
        if not sym:
            print("Укажите тикер: python whale_scan.py --symbol BTC", file=sys.stderr)
            sys.exit(1)
        sig = deep_dive(sym)
        if not sig:
            print(f"Не удалось загрузить сделки по {sym} (пара не найдена или биржа недоступна).", file=sys.stderr)
            sys.exit(1)
        print(format_whale_detail(sig))
        return
    if "--oneshot" in sys.argv:
        run_oneshot()
        return
    # По умолчанию — консольный скан
    signals, meta = run_whale_scan()
    print(f"=== Whale Scan: {meta['scanned']} пар за {meta['duration_ms']/1000:.1f}с, сигналов: {len(signals)} ===")
    for s in signals:
        print(format_whale_alert(s))
        print("─" * 50)
    if not signals:
        for n in meta["near"][:5]:
            print(f"~ {n['base']}: score={n['score']:.0f} (порог {meta['min_score']:.0f}), поток {n['net_flow_pct']:+.0f}%")

if __name__ == "__main__":
    main()
