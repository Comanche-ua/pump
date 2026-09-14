#!/usr/bin/env python3
"""
Бэктест сигнального движка Pump Pulse.

Гоняет РЕАЛЬНЫЙ движок из pump_bot.py (analyze_symbol -> score_window ->
pick_best -> grade_from) по историческим свечам Binance и измеряет, что
происходит ПОСЛЕ сигнала: сначала TP или сначала SL.

Это не реимплементация скоринга. Любая правка в pump_bot.py сразу
отражается на результатах, поэтому харнесс служит защитой от регрессии
при калибровке весов и порогов.

    python backtest.py --fetch  --days 14 --symbols 40
    python backtest.py --replay
    python backtest.py --replay --limit 12 --min-score 65     # быстрая итерация
    python backtest.py --replay --pullback 0.4 --timeout-bars 1

Для каждого сигнала сохраняется 96 баров (8ч) вперёд в АБСОЛЮТНЫХ ценах
(high/low/close по барам), поэтому вход (по рынку или лимитом на откате),
TP, SL и горизонт пересчитываются в памяти без повторного скоринга.

Артефакты: cache/<SYMBOL>_5m.csv.gz, summaries.csv.gz, signals.csv
"""
from __future__ import annotations

import csv
import gzip
import math
import os
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import pump_bot as pb

CACHE_DIR = os.path.join(HERE, "cache")
SUMMARIES_GZ = os.path.join(HERE, "summaries.csv.gz")
SIGNALS_CSV = os.path.join(HERE, "signals.csv")

# Один и тот же набор конфигураций для in-sample и out-of-sample прогонов:
# без этого сравнение «до/после» нечестное.
GRID = (
    (0.5, 3.0, 96), (0.7, 3.0, 96),
    (1.0, 3.0, 96), (1.25, 3.0, 96), (1.5, 3.0, 96), (2.0, 3.0, 96),
    (1.0, 1.0, 48), (1.5, 2.0, 96), (2.0, 2.0, 48),
)

INTERVAL = "5m"
BAR_MS = 300_000
BARS_PER_DAY = 288
WARMUP = pb.KLINES_LIMIT + BARS_PER_DAY + 12   # окно + 24ч-тикер + запас
MAX_LOOK = 96         # потолок горизонта: 96 баров = 8 часов
TAG = ""              # суффикс ВЫВОДА: другой прогон не затирает предыдущий
CACHE_TAG = ""        # суффикс КЭША свечей: он общий для всех стратегий,
                      # привязывать к нему стратегию нельзя — иначе прогон
                      # ищет несуществующие файлы и молча даёт ноль сигналов

FIELDS = ("open_time", "open", "high", "low", "close", "volume",
          "quote_volume", "trades", "taker_buy_base", "close_time")


# ───────────────────────── Сеть / кэш ─────────────────────────

def _get_json(url, retries=4):
    last = None
    for attempt in range(retries):
        try:
            return pb.http_get_json(url, timeout=25)
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


def cache_path(symbol, interval=INTERVAL):
    return os.path.join(CACHE_DIR, f"{symbol}_{interval}{'_' + CACHE_TAG if CACHE_TAG else ''}.csv.gz")


def tagged_path(path):
    """Добавляет суффикс прогона, чтобы out-of-sample не затирал in-sample."""
    if not TAG:
        return path
    root, ext = os.path.splitext(path)
    return f"{root}_{TAG}{ext}"


def save_klines(symbol, raw, interval=INTERVAL):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with gzip.open(cache_path(symbol, interval), "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        for r in raw:
            w.writerow(
                [int(r[0])]
                + [repr(float(r[i])) for i in (1, 2, 3, 4, 5, 7)]
                + [int(r[8]), repr(float(r[9])), int(r[6])]
            )
    return len(raw)


def load_klines(symbol, interval=INTERVAL):
    path = cache_path(symbol, interval)
    if not os.path.exists(path):
        return []
    out = []
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            out.append(pb.Candle(
                int(row[0]), float(row[1]), float(row[2]), float(row[3]),
                float(row[4]), float(row[5]), float(row[6]), int(row[7]),
                float(row[8]), int(row[9]),
            ))
    return out


def fetch_klines_range(symbol, start_ms, end_ms, interval=INTERVAL, bar_ms=BAR_MS):
    out = []
    cur = start_ms
    while cur < end_ms:
        url = (f"{pb.BINANCE_BASE}/api/v3/klines?symbol={symbol}&interval={interval}"
               f"&startTime={cur}&endTime={end_ms}&limit=1000")
        raw = _get_json(url)
        if not raw:
            break
        out.extend(raw)
        nxt = int(raw[-1][0]) + bar_ms
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(0.12)
    return out


def top_symbols(n, min_quote_volume, skip=0):
    # is_usdt_spot в pump_bot.py пропускает не-ASCII тикеры Binance
    # ('牛来USDT', '币安人生USDT') — их нельзя подставить в URL, klines
    # по ним всегда пустые. В харнессе отбрасываем.
    tickers = [
        t for t in pb.get_24h_tickers()
        if t["quoteVolume"] >= min_quote_volume and t["symbol"].isascii()
    ]
    tickers.sort(key=lambda t: -t["quoteVolume"])
    # skip — срез вселенной: out-of-sample проверяется на ДРУГИХ парах
    symbols = [t["symbol"] for t in tickers[skip:skip + n]]
    if "BTCUSDT" not in symbols:
        symbols.append("BTCUSDT")
    return symbols


def cmd_fetch(days, n_symbols, refresh, interval, end_days_ago=0, skip=0):
    interval_ms = 60_000 if interval == "1m" else BAR_MS
    os.makedirs(CACHE_DIR, exist_ok=True)
    symbols = top_symbols(n_symbols, pb.DEFAULT_MIN_QUOTE_VOLUME, skip=skip)
    end_ms = int(time.time() * 1000) - end_days_ago * 86400_000
    start_ms = end_ms - days * 86400_000
    label = f" (сдвиг назад {end_days_ago} дн., ранги {skip + 1}..{skip + n_symbols})" if (end_days_ago or skip) else ""
    print(f"Загрузка {len(symbols)} пар за {days} дн. ({interval}){label} → {CACHE_DIR}")
    failed = []
    for i, sym in enumerate(symbols, 1):
        if not refresh and os.path.exists(cache_path(sym, interval)):
            have = load_klines(sym, interval)
            if have and have[-1].open_time >= end_ms - 3 * interval_ms:
                print(f"  [{i}/{len(symbols)}] {sym}: кэш актуален ({len(have)} свечей)")
                continue
        try:
            raw = fetch_klines_range(sym, start_ms, end_ms, interval, interval_ms)
        except Exception as e:
            failed.append(sym)
            print(f"  [{i}/{len(symbols)}] {sym}: ОШИБКА {type(e).__name__}: {e}")
            continue
        n = save_klines(sym, raw, interval)
        print(f"  [{i}/{len(symbols)}] {sym}: {n} свечей")
    if failed:
        print(f"Не загружено: {', '.join(failed)}")


def cached_symbols(interval=INTERVAL):
    if not os.path.isdir(CACHE_DIR):
        return []
    suffix = f"_{interval}{'_' + CACHE_TAG if CACHE_TAG else ''}.csv.gz"
    return sorted(name[:-len(suffix)] for name in os.listdir(CACHE_DIR) if name.endswith(suffix))


# ───────────────────────── Реплей ─────────────────────────

def build_5m_from_1m(bars_1m):
    """
    Собирает 5m-серию из 1m-свечей и возвращает (свечи, подбары по бакетам).
    Подбары нужны, чтобы точно восстановить ЧАСТИЧНЫЙ текущий бар на момент
    скана: 5m-бар, прошедший на phase, = агрегат первых round(phase*5) минут.
    Без интерполяции и без заглядывания в будущее.
    """
    groups = {}
    order = []
    for c in bars_1m:
        key = (c.open_time // BAR_MS) * BAR_MS
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(c)
    candles = [aggregate_bars(groups[key], key) for key in order]
    subbars = [groups[key] for key in order]
    return candles, subbars


def aggregate_bars(g, key):
    first, last = g[0], g[-1]
    return pb.Candle(
        open_time=key, open=first.open, high=max(c.high for c in g),
        low=min(c.low for c in g), close=last.close,
        volume=sum(c.volume for c in g),
        quote_volume=sum(c.quote_volume for c in g),
        trades=sum(c.trades for c in g),
        taker_buy_base=sum(c.taker_buy_base for c in g),
        close_time=last.close_time,
    )


def path_vectors(candles, entry_idx, max_look):
    """Абсолютные уровни баров после entry_idx: (highs, lows, closes)."""
    end = min(entry_idx + 1 + max_look, len(candles))
    return (
        [candles[j].high for j in range(entry_idx + 1, end)],
        [candles[j].low for j in range(entry_idx + 1, end)],
        [candles[j].close for j in range(entry_idx + 1, end)],
    )


def synthetic_ticker(symbol, candles, upto, current=None):
    """24ч-тикер, собранный из свечей на момент скана (current — частичный бар)."""
    cur = current or candles[upto]
    last_price = cur.close
    first = candles[max(0, upto - BARS_PER_DAY)]
    tail = candles[max(0, upto - BARS_PER_DAY + 1): upto] + [cur]
    return {
        "symbol": symbol,
        "lastPrice": last_price,
        "priceChangePercent": pb.pct_change(first.close, last_price),
        "highPrice": max(c.high for c in tail),
        "lowPrice": min(c.low for c in tail),
        "quoteVolume": sum(c.quote_volume for c in tail),
        "volume": sum(c.volume for c in tail),
    }


def replay_symbol(symbol, min_score, cooldown_bars, btc_map, btc_change_map, phase,
                  strategy="pump"):
    subbars = None
    elapsed = 5
    btc_partial = {}
    if phase < 1.0:
        bars_1m = load_klines(symbol, "1m")
        btc_1m = load_klines("BTCUSDT", "1m")
        if not bars_1m or not btc_1m:
            return [], []
        candles, subbars = build_5m_from_1m(bars_1m)
        btc_all, btc_subbars = build_5m_from_1m(btc_1m)
        elapsed = max(1, min(5, int(phase * 5 + 0.5)))
        btc_partial = {c.open_time: aggregate_bars(sb[:elapsed], c.open_time)
                       for c, sb in zip(btc_all, btc_subbars)}
    else:
        candles = load_klines(symbol, INTERVAL)

    signals, summaries = [], []
    n = len(candles)
    if n < WARMUP + MAX_LOOK:
        return signals, summaries

    cooldown_until = -1
    for i in range(WARMUP, n - MAX_LOOK):
        win = candles[i - pb.KLINES_LIMIT + 1: i + 1]
        current = None
        if subbars:
            current = aggregate_bars(subbars[i][:elapsed], candles[i].open_time)
            win = win[:-1] + [current]
        btc_win = [btc_map.get(c.open_time) for c in win]
        if None in btc_win:
            continue
        if subbars:
            btc_win = btc_win[:-1] + [btc_partial.get(candles[i].open_time, btc_win[-1])]

        ticker = synthetic_ticker(symbol, candles, i, current)
        btc_ticker = {
            "symbol": "BTCUSDT",
            "lastPrice": btc_win[-1].close,
            "priceChangePercent": btc_change_map.get(candles[i].open_time, 0.0),
        }
        sig, summary = pb.analyze_symbol(ticker, win, btc_win, btc_ticker,
                                         min_score=min_score, strategy=strategy)
        if summary:
            summaries.append((symbol, candles[i].open_time, round(summary["best_score"], 2),
                              summary["best_tf"], summary["grade"],
                              round(summary["change_24h"], 3), round(summary["vol_ratio"], 3)))
        if not sig or i < cooldown_until:
            continue

        hi, lo, cl = path_vectors(candles, i, MAX_LOOK)
        best_bd = next((r for r in sig.by_tf if r.timeframe == sig.best_tf), None)
        signals.append({
            "symbol": symbol, "open_time": candles[i].open_time,
            "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(candles[i].open_time / 1000)),
            "entry": win[-1].close, "score": round(sig.best_score, 2),
            "grade": sig.grade, "tf": sig.best_tf,
            "change_24h": round(sig.change_24h, 3),
            # Сырые значения индикаторов: по ним свип порогов считается
            # из уже собранной выборки, без повторного скоринга.
            "rsi": round(best_bd.rsi, 2) if best_bd else 0.0,
            "sar": round(best_bd.sar, 6) if best_bd else 0.0,
            "fractal_level": round(best_bd.fractal_level, 6) if best_bd else 0.0,
            "hi": hi, "lo": lo, "cl": cl,
            "feats": signal_features(sig),
        })
        cooldown_until = i + cooldown_bars
    return signals, summaries


def signal_features(sig):
    """
    Значения всех 8 факторов и сырых метрик того таймфрейма, чей скор стал
    итоговым — сырьё для факторного аудита.
    """
    bd = next((r for r in sig.by_tf if r.timeframe == sig.best_tf), None)
    if bd is None:
        return {}
    feats = {f.id: round(f.value, 4) for f in bd.factors}
    feats.update({
        "vol_ratio": round(bd.volume_ratio, 4),
        "atr_exp": round(bd.atr_expansion, 4),
        "breakout_pct": round(bd.breakout_pct, 4),
        "rsi_raw": round(bd.rsi, 3),
        "taker_buy": round(bd.taker_buy, 5),
        "vs_btc_pct": round(bd.vs_btc_pct, 4),
        "bar_chg_pct": round(bd.change_pct, 4),
        "ema_aligned": int(bd.ema_aligned),
        "is_late": int(bd.late),
    })
    return feats


FACTORS = (
    ("vol", "Фактор «объём» (25б)"),
    ("taker", "Фактор «агрессия покупок» (22б)"),
    ("squeeze", "Фактор «сжатие пружины» (15б)"),
    ("breakout", "Фактор «пробой флэта» (12б)"),
    ("candle", "Фактор «структура свечи» (10б)"),
    ("rsi", "Фактор «окно RSI» (8б)"),
    ("ema", "Фактор «тренд EMA» (4б)"),
    ("btc", "Фактор «опережение BTC» (4б)"),
    ("vol_ratio", "СЫРОЕ: объём / SMA20"),
    ("taker_buy", "СЫРОЕ: доля taker buy"),
    ("breakout_pct", "СЫРОЕ: пробой коридора, %"),
    ("rsi_raw", "СЫРОЕ: RSI14"),
    ("vs_btc_pct", "СЫРОЕ: опережение BTC, %"),
    ("bar_chg_pct", "СЫРОЕ: ход триггерного бара, %"),
    ("change_24h", "СЫРОЕ: изменение за 24ч, %"),
)


def _worker(job):
    # На Windows процессы-потомки импортируют модуль заново и НЕ наследуют
    # глобальные переменные, поэтому тег КЭША и стратегию передаём в задании.
    global CACHE_TAG
    (symbol, min_score, cooldown_bars, btc_map, btc_change_map, phase,
     cache_tag, strategy) = job
    CACHE_TAG = cache_tag
    signals, summaries = replay_symbol(
        symbol, min_score, cooldown_bars, btc_map, btc_change_map, phase, strategy)
    return symbol, signals, summaries


# ───────────────────────── Метрики ─────────────────────────

def resolve_entry(sig, pullback, timeout_bars):
    """
    Возвращает (entry_price, start_index, filled) для модели входа.
    pullback=0 — вход по рынку на закрытии триггерного бара; иначе лимит
    на откате, живущий timeout_bars баров (как ENTRY_PULLBACK_PCT бота).
    """
    base = sig["entry"]
    if pullback < 0:
        # Bounce-вход: лимит ВЫШЕ цены. Для стратегии дампа зеркало «отката
        # вниз» — это «отскок вверх»: покупаем подтверждённое восстановление,
        # а не падающий нож.
        level = base * (1 + abs(pullback) / 100.0)
        for j in range(min(timeout_bars, len(sig["hi"]))):
            if sig["hi"][j] >= level:
                return level, j + 1, True
        return 0.0, 0, False
    if pullback == 0:
        return base, 0, True
    level = base * (1 - pullback / 100.0)
    for j in range(min(timeout_bars, len(sig["lo"]))):
        if sig["lo"][j] <= level:
            return level, j + 1, True
    return 0.0, 0, False


def wilson_ci(k, n, z=1.96):
    """Доверительный интервал доли по Уилсону."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    center = p + z * z / (2 * n)
    rad = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (center - rad) / d, (center + rad) / d


def verdict(ci_lo, ci_hi, breakeven):
    """✓ — интервал целиком выше безубытка, ✗ — целиком ниже, · — неопределённо."""
    if ci_lo > breakeven:
        return "✓"
    if ci_hi < breakeven:
        return "✗"
    return "·"


def evaluate(signals, tp_pct, sl_pct, look, fee_pct, slip_pct,
             pullback=0.0, timeout_bars=0):
    """Исходы при данной стратегии входа и выхода."""
    res = {"tp": 0, "sl": 0, "both": 0, "timeout": 0, "nofill": 0}
    pnl = 0.0
    for s in signals:
        entry, start, filled = resolve_entry(s, pullback, timeout_bars)
        if not filled:
            res["nofill"] += 1
            continue
        tp_level = entry * (1 + tp_pct / 100.0)
        sl_level = entry * (1 - sl_pct / 100.0)
        kind = "timeout"
        j = start
        end = min(start + look, len(s["hi"]))
        for j in range(start, end):
            hit_tp = s["hi"][j] >= tp_level
            hit_sl = s["lo"][j] <= sl_level
            if hit_tp and hit_sl:
                kind = "both"
                break
            if hit_tp:
                kind = "tp"
                break
            if hit_sl:
                kind = "sl"
                break
        else:
            j = max(start, end - 1)
        res[kind] += 1
        if kind == "tp":
            pnl += tp_pct - fee_pct
        elif kind in ("sl", "both"):
            pnl += -sl_pct - fee_pct - slip_pct
        else:
            close = s["cl"][j] if j < len(s["cl"]) else entry
            pnl += (close / entry - 1) * 100.0 - fee_pct - slip_pct
    taken = res["tp"] + res["sl"] + res["both"] + res["timeout"]
    decided = res["tp"] + res["sl"] + res["both"]
    tp_first = res["tp"] / decided if decided else 0.0
    exp_decided = 0.0
    if decided:
        sl_part = (res["sl"] + res["both"]) / decided
        exp_decided = tp_first * (tp_pct - fee_pct) + sl_part * (-sl_pct - fee_pct - slip_pct)
    ci_lo, ci_hi = wilson_ci(res["tp"], decided)
    return {
        "n": len(signals), "taken": taken, "out": res, "decided": decided,
        "decided_rate": decided / taken if taken else 0.0,
        "tp_first": tp_first, "ci_lo": ci_lo, "ci_hi": ci_hi,
        "exp_decided": exp_decided,
        "exp_per_trade": pnl / taken if taken else 0.0,
    }


def breakeven_rate(tp_pct, sl_pct, fee_pct, slip_pct):
    win = tp_pct - fee_pct
    loss = sl_pct + fee_pct + slip_pct
    return loss / (win + loss) if (win + loss) else 0.0


def grid_table(signals, grid, fee_pct, slip_pct, pullback, timeout_bars, title):
    print(f"\n── {title} ──")
    print(f"{'TP%':>5} {'SL%':>5} {'гориз':>6} {'исполн':>7} {'решено':>7} {'TP-первым':>10} "
          f"{'95% ДИ':>16} {'BE':>6} {'E[сдел]':>9} {'E[решён]':>9} {'':>2}")
    for tp_pct, sl_pct, look in grid:
        r = evaluate(signals, tp_pct, sl_pct, look, fee_pct, slip_pct, pullback, timeout_bars)
        be = breakeven_rate(tp_pct, sl_pct, fee_pct, slip_pct)
        mark = verdict(r["ci_lo"], r["ci_hi"], be) if r["decided"] >= 20 else " "
        print(f"{tp_pct:5.1f} {sl_pct:5.1f} {look:5d}б "
              f"{100.0*r['taken']/r['n'] if r['n'] else 0:6.1f}% {r['decided_rate']*100:6.1f}% "
              f"{r['tp_first']*100:9.1f}% "
              f"{r['ci_lo']*100:7.1f}..{r['ci_hi']*100:5.1f}% "
              f"{be*100:5.1f}% {r['exp_per_trade']:+8.3f}% {r['exp_decided']:+8.3f}% {mark:>2}")


def quartile_split(signals, key, q=4):
    """Делит сигналы на q равных по числу групп по возрастанию значения key."""
    pairs = [(s["feats"].get(key, s.get(key)), s) for s in signals]
    pairs = [(v, s) for v, s in pairs if v is not None]
    pairs.sort(key=lambda t: t[0])
    out = []
    total = len(pairs)
    for i in range(q):
        chunk = pairs[i * total // q:(i + 1) * total // q]
        if chunk:
            out.append((chunk[0][0], chunk[-1][0], [s for _, s in chunk]))
    return out


def factor_analysis(signals, tp_pct, sl_pct, look, fee_pct, slip_pct, entry_model):
    """
    Несёт ли фактор информацию? Сигналы делятся на квартили по значению
    фактора; если TP-первым растёт от Q1 к Q4 — фактор что-то предсказывает.
    """
    be = breakeven_rate(tp_pct, sl_pct, fee_pct, slip_pct)
    print("\n" + "═" * 96)
    print(f"ФАКТОРНЫЙ АУДИТ — TP={tp_pct}%/SL={sl_pct}%/{look}б, вход по рынку, "
          f"порог безубытка {be*100:.1f}%")
    print("═" * 96)
    print(f"  {'фактор':32s} {'Q1':>7} {'Q2':>7} {'Q3':>7} {'Q4':>7}   {'Q4-Q1':>7}  n/q  вывод")
    for key, label in FACTORS:
        groups = quartile_split(signals, key)
        if len(groups) < 4:
            continue
        rows = []
        for lo_v, hi_v, chunk in groups:
            r = evaluate(chunk, tp_pct, sl_pct, look, fee_pct, slip_pct, *entry_model)
            rows.append(r)
        rates = [r["tp_first"] * 100 for r in rows]
        delta = rates[-1] - rates[0]
        # значимость разницы крайних квартилей: ДИ не пересекаются?
        strong_up = rows[-1]["ci_lo"] > be
        strong_dn = rows[0]["ci_hi"] < be
        if strong_up:
            note = "★ Q4 значимо выше BE"
        elif abs(delta) < 3.0:
            note = "— информации нет"
        else:
            note = f"{'↑' if delta > 0 else '↓'} разница {delta:+.1f}пп, ДИ пересекаются"
        print(f"  {label:32s} " + " ".join(f"{v:6.1f}%" for v in rates)
              + f"   {delta:+6.1f}пп  {rows[0]['taken']:4d} {note}")
    print("\n  ★ = крайний квартиль значимо выше безубытка (ДИ по Уилсону не накрывает BE).")
    print("     Разница Q4-Q1 без ★ — в пределах шума, торговать по ней нельзя.")


def indicator_sweep(signals, fee_pct, slip_pct, tp=1.5, sl=3.0, look=96):
    """
    Матрица порогов для режима «RSI + стохастик».

    Скоринг заново НЕ пересчитывается: в выборке уже лежат сырые RSI, %K, %D и
    путь цены каждого бара, поэтому любые комбинации порогов считаются в памяти.
    Цель — проверить не правило, а его границы: возможно, «край зоны» (RSI не
    выше 68, стохастик только что развернулся) ведёт себя иначе, чем «цена уже
    растёт», и именно это делает правило информативным или бесполезным.
    """
    if not signals:
        return
    be = breakeven_rate(tp, sl, fee_pct, slip_pct)
    print("\n" + "═" * 92)
    print(f"СВИП ПОРОГОВ режима RSI+Стохастик (TP={tp}%/SL={sl}%/{look}б; безубыток {be*100:.1f}%)")
    print("  фильтр: RSI в пределах, SAR под ценой, пробит up-фрактал")
    print("═" * 92)
    print(f"  {'RSI от':>7} {'до':>7} {'SAR<цена':>9} {'фрактал':>8} {'n':>6} "
          f"{'решено':>7} {'TP-первым':>10} {'E[решён]':>10}  итог")

    rows = []
    for rsi_lo in (45, 50, 55, 60):
        for rsi_hi in (68, 75, 1000):
            for need_sar in (True, False):
                for need_fr in (True, False):
                    if not need_sar and not need_fr:
                        continue
                    sel = []
                    for s in signals:
                        if not (rsi_lo <= s["rsi"] <= rsi_hi):
                            continue
                        if need_sar and not (0 < s["sar"] < s["entry"]):
                            continue
                        if need_fr and not (0 < s["fractal_level"] < s["entry"]):
                            continue
                        sel.append(s)
                    if len(sel) < 150:
                        continue
                    r = evaluate(sel, tp, sl, look, fee_pct, slip_pct)
                    if r["decided"] < 60:
                        continue
                    rows.append((r["exp_decided"], rsi_lo, rsi_hi, need_sar, need_fr, r))

    if not rows:
        print("  (ни одна комбинация не дала достаточной выборки)")
        return
    rows.sort(reverse=True, key=lambda x: x[0])
    for exp, rsi_lo, rsi_hi, need_sar, need_fr, r in rows[:14]:
        hi_lbl = "∞" if rsi_hi > 100 else f"{rsi_hi}"
        mark = verdict(r["ci_lo"], r["ci_hi"], be)
        print(f"  {rsi_lo:7.0f} {hi_lbl:>7} {str(need_sar):>9} {str(need_fr):>8} {r['taken']:6d} "
              f"{r['decided_rate']*100:6.1f}% {r['tp_first']*100:9.1f}% "
              f"{exp:+9.3f}%  {mark}")
    print("\n  ✓ — ДИ значимо выше безубытка · ✗ — значимо ниже · · — в пределах шума")
    print("  базовая версия (RSI>55, SAR под ценой, фрактал пробит) — RSI 55 / ∞ / True / True")


def report(signals, summaries, min_score, fee_pct, slip_pct, grid, pullback, timeout_bars,
           tp_pct, sl_pct, brief=False):
    print("\n" + "═" * 90)
    print(f"БАЗА: порог={min_score:.0f}  комиссия={fee_pct}%  проскальзывание стопа={slip_pct}%")
    print("═" * 90)

    if summaries:
        scores = sorted(r[2] for r in summaries)
        print(f"Баров отсканировано: {len(summaries)}  |  скор: max={scores[-1]:.1f} "
              f"p99={scores[int(.99*len(scores))-1]:.1f} p95={scores[int(.95*len(scores))-1]:.1f} "
              f"median={statistics.median(scores):.1f}")
        print("  баров ≥ порога:  " + "  ".join(
            f"≥{t}: {sum(1 for x in scores if x >= t)}" for t in (50, 58, 65, 70, 80, 90)))

    if not signals:
        print("\nСигналов нет.")
        return

    grades = {}
    for s in signals:
        grades[s["grade"]] = grades.get(s["grade"], 0) + 1
    print(f"Сигналов (кулдаун 30м): {len(signals)}  →  {grades}")

    grid_table(signals, grid, fee_pct, slip_pct, 0.0, 0,
               "ВХОД ПО РЫНКУ (на закрытии триггерного бара)")
    if brief:
        return
    grid_table(signals, grid, fee_pct, slip_pct, pullback, timeout_bars,
               f"ВХОД ЛИМИТОМ НА ОТКАТЕ -{pullback}% (жизнь {timeout_bars} бар.)")

    print(f"\n── Исходы: рынок vs откат (TP={tp_pct}%/SL={sl_pct}%/48б) ──")
    for label, pb_, tb in (("рынок", 0.0, 0), (f"откат -{pullback}%", pullback, timeout_bars)):
        r = evaluate(signals, tp_pct, sl_pct, 48, fee_pct, slip_pct, pb_, tb)
        o = r["out"]
        print(f"  {label:12s} исполнено={r['taken']:5d}/{r['n']}  "
              f"TP={o['tp']:4d} SL={o['sl']+o['both']:4d} нерешено={o['timeout']:4d}  "
              f"TP-первым={r['tp_first']*100:5.1f}%  E[решён]={r['exp_decided']:+.3f}%")

    entry_model = (pullback, timeout_bars) if pullback != 0 else (0.0, 0)
    be22 = breakeven_rate(tp_pct, sl_pct, fee_pct, slip_pct)
    cfg = f"TP={tp_pct}%/SL={sl_pct}%/48б"
    print(f"\n── По грейду ({cfg}, {'рынок' if not pullback else f'откат -{pullback}%'}; "
          f"BE={be22*100:.1f}%) ──")
    for g in ("strong", "watch", "late"):
        rows = [s for s in signals if s["grade"] == g]
        if not rows:
            continue
        r = evaluate(rows, tp_pct, sl_pct, 48, fee_pct, slip_pct, *entry_model)
        print(f"  {g:7s} n={r['taken']:5d} решено={r['decided_rate']*100:5.1f}% "
              f"TP-первым={r['tp_first']*100:5.1f}% [{r['ci_lo']*100:.1f}..{r['ci_hi']*100:.1f}] "
              f"E[решён]={r['exp_decided']:+.3f}% {verdict(r['ci_lo'], r['ci_hi'], be22)}")

    print(f"\n── По таймфрейму ({cfg}) ──")
    for tf in ("5m", "15m", "1h"):
        rows = [s for s in signals if s["tf"] == tf]
        if not rows:
            print(f"  {tf:4s} — сигналов нет")
            continue
        r = evaluate(rows, tp_pct, sl_pct, 48, fee_pct, slip_pct, *entry_model)
        print(f"  {tf:4s} n={r['taken']:5d} решено={r['decided_rate']*100:5.1f}% "
              f"TP-первым={r['tp_first']*100:5.1f}% [{r['ci_lo']*100:.1f}..{r['ci_hi']*100:.1f}] "
              f"E[решён]={r['exp_decided']:+.3f}% {verdict(r['ci_lo'], r['ci_hi'], be22)}")

    print(f"\n── По скору ({cfg}) ──")
    for lo_s, hi_s in ((58, 65), (65, 70), (70, 80), (80, 101)):
        rows = [s for s in signals if lo_s <= s["score"] < hi_s]
        if not rows:
            continue
        r = evaluate(rows, tp_pct, sl_pct, 48, fee_pct, slip_pct, *entry_model)
        print(f"  скор {lo_s:3d}-{hi_s:3d}: n={r['taken']:5d} решено={r['decided_rate']*100:5.1f}% "
              f"TP-первым={r['tp_first']*100:5.1f}% [{r['ci_lo']*100:.1f}..{r['ci_hi']*100:.1f}] "
              f"E[решён]={r['exp_decided']:+.3f}% {verdict(r['ci_lo'], r['ci_hi'], be22)}")

    print("\n── Достижимые движения от цены входа (MFE/MAE, вход по рынку) ──")
    for look in (12, 48, 96):
        rows = [s for s in signals if len(s["hi"]) >= look]
        if not rows:
            continue
        mfe = sorted((max(s["hi"][:look]) / s["entry"] - 1) * 100 for s in rows)
        mae = sorted((min(s["lo"][:look]) / s["entry"] - 1) * 100 for s in rows)
        q = lambda v, p: v[min(len(v) - 1, int(len(v) * p))]
        print(f"  {look:3d}б ({look*5//60:2d}ч): MFE медиана={q(mfe,.5):+.2f}% p75={q(mfe,.75):+.2f}% "
              f"p90={q(mfe,.9):+.2f}%  |  MAE медиана={q(mae,.5):+.2f}% p25={q(mae,.25):+.2f}%")


def cmd_replay(tp_pct, sl_pct, lookahead, min_score, cooldown_min, fee_pct, slip_pct,
               limit_symbols, jobs, pullback, timeout_bars, phase, match_1m, brief=False,
               strategy="pump"):
    if phase < 1.0:
        symbols = cached_symbols("1m")
    else:
        symbols = cached_symbols(INTERVAL)
        if match_1m:
            # сравнение фаз должно идти на ОДНИХ И ТЕХ ЖЕ парах
            symbols = sorted(set(symbols) & set(cached_symbols("1m")))
    if limit_symbols:
        symbols = symbols[:limit_symbols]
    if not symbols:
        print("Кэш пуст. Сначала: python backtest.py --fetch", file=sys.stderr)
        sys.exit(1)

    btc_candles = load_klines("BTCUSDT")
    if not btc_candles:
        print("Нет BTCUSDT в кэше — перезапустите --fetch", file=sys.stderr)
        sys.exit(1)
    btc_map = {c.open_time: c for c in btc_candles}
    btc_change_map = {}
    for k, c in enumerate(btc_candles):
        if k >= BARS_PER_DAY:
            btc_change_map[c.open_time] = pb.pct_change(btc_candles[k - BARS_PER_DAY].close, c.close)

    cooldown_bars = max(1, cooldown_min // 5)
    jobs_list = [(s, min_score, cooldown_bars, btc_map, btc_change_map, phase,
                  CACHE_TAG, strategy) for s in symbols]
    all_signals, all_summaries = [], []
    t0 = time.time()
    print(f"Реплей {len(symbols)} пар в {jobs} процессов "
          f"(порог {min_score:.0f}, phase={phase})...")
    done = 0
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        for symbol, signals, summaries in pool.map(_worker, jobs_list):
            done += 1
            all_signals.extend(signals)
            all_summaries.extend(summaries)
            print(f"  [{done}/{len(symbols)}] {symbol:12s} сигналов={len(signals):3d}  ({time.time()-t0:.0f}с)")

    if not all_summaries:
        print("Реплей не дал ни одного бара — проверьте кэш.", file=sys.stderr)
        sys.exit(1)

    summaries_path = tagged_path(SUMMARIES_GZ)
    signals_path = tagged_path(SIGNALS_CSV)
    with gzip.open(summaries_path, "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(("symbol", "open_time", "score", "tf", "grade", "change_24h", "vol_ratio"))
        w.writerows(all_summaries)

    feat_keys = sorted({k for s in all_signals for k in s.get("feats", {})})
    with open(signals_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "time", "open_time", "score", "grade", "tf", "change_24h",
                    "entry", "mfe12", "mae12", "ret12"] + feat_keys)
        for s in all_signals:
            r12 = (s["cl"][11] / s["entry"] - 1) * 100 if len(s["cl"]) >= 12 else ""
            w.writerow([s["symbol"], s["time"], s["open_time"], s["score"], s["grade"], s["tf"],
                        s["change_24h"], repr(s["entry"]),
                        round((s["hi"][11] / s["entry"] - 1) * 100, 3) if len(s["hi"]) >= 12 else "",
                        round((s["lo"][11] / s["entry"] - 1) * 100, 3) if len(s["lo"]) >= 12 else "",
                        round(r12, 3) if r12 != "" else ""]
                       + [s.get("feats", {}).get(k, "") for k in feat_keys])

    print(f"\nРеплей {len(symbols)} пар за {time.time()-t0:.0f}с → {summaries_path}, {signals_path}")

    grid = list(GRID)
    if strategy == "indicators":
        indicator_sweep(all_signals, fee_pct, slip_pct)
    if not brief:
        factor_analysis(all_signals, tp_pct, sl_pct, 48, fee_pct, slip_pct, (0.0, 0))
        factor_analysis(all_signals, 1.0, 2.0, 96, fee_pct, slip_pct, (0.0, 0))
    report(all_signals, all_summaries, min_score, fee_pct, slip_pct, grid, pullback, timeout_bars,
           tp_pct, sl_pct, brief=brief)


def main():
    global TAG
    args = sys.argv[1:]

    def flag(name, default):
        return args[args.index(name) + 1] if name in args else default

    strategy = flag("--strategy", "pump").strip().lower()
    if strategy not in pb.STRATEGY_LABELS:
        print(f"Неизвестная стратегия {strategy!r}; доступны: {', '.join(pb.STRATEGY_LABELS)}",
              file=sys.stderr)
        sys.exit(2)

    CACHE_TAG = flag("--tag", "")
    TAG = CACHE_TAG
    # Результаты другой стратегии не должны затирать прогоны памп-скора,
    # но КЭШ свечей общий: стратегия влияет на вывод, а не на данные.
    if strategy != "pump":
        TAG = f"{TAG}_{strategy}" if TAG else strategy

    if "--fetch" in args:
        cmd_fetch(
            days=int(flag("--days", 14)),
            n_symbols=int(flag("--symbols", 40)),
            refresh="--refresh" in args,
            interval=flag("--interval", INTERVAL),
            end_days_ago=int(flag("--end-days-ago", 0)),
            skip=int(flag("--skip", 0)),
        )
        return

    cmd_replay(
        tp_pct=float(flag("--tp", pb.DEFAULT_TAKE_PROFIT)),
        sl_pct=float(flag("--sl", pb.DEFAULT_STOP_LOSS)),
        lookahead=int(flag("--lookahead", 12)),
        min_score=float(flag("--min-score", pb.DEFAULT_MIN_SCORE)),
        cooldown_min=int(flag("--cooldown", 30)),
        fee_pct=float(flag("--fee", 0.2)),
        slip_pct=float(flag("--slip", 0.1)),
        limit_symbols=int(flag("--limit", 0)),
        jobs=int(flag("--jobs", 2)),
        pullback=float(flag("--pullback", pb.DEFAULT_ENTRY_PULLBACK)),
        timeout_bars=int(flag("--timeout-bars", 1)),
        phase=float(flag("--phase", 1.0)),
        match_1m="--match-1m" in args,
        brief="--brief" in args,
        strategy=strategy,
    )


if __name__ == "__main__":
    main()
