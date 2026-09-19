#!/usr/bin/env python3
"""
Движок исторического бэктеста стратегии Лонг-Свинг (+20%+) на 50+ парах Binance Spot.

Запуск:
    python backtest/backtest_swing.py --symbols 50 --days 30
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import gzip
import json
import os
import sys
import time
import urllib.request
from typing import Dict, List, Tuple, Any

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(HERE)
sys.path.insert(0, ROOT_DIR)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import swing_engine as se

CACHE_DIR = os.path.join(HERE, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

TOP_50_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT",
    "LINKUSDT", "NEARUSDT", "SUIUSDT", "APTUSDT", "INJUSDT", "TIAUSDT", "RENDERUSDT", "FETUSDT",
    "TAOUSDT", "ARBUSDT", "OPUSDT", "PEPEUSDT", "WIFUSDT", "FLOKIUSDT", "BONKUSDT", "TONUSDT",
    "NOTUSDT", "FTMUSDT", "GALAUSDT", "SANDUSDT", "AAVEUSDT", "UNIUSDT", "MKRUSDT", "PENDLEUSDT",
    "RUNEUSDT", "STXUSDT", "IMXUSDT", "SEIUSDT", "JUPUSDT", "PYTHUSDT", "WLDUSDT", "LDOUSDT",
    "GRTUSDT", "FILUSDT", "ICPUSDT", "VETUSDT", "TRXUSDT", "AUDIOUSDT", "DYDXUSDT", "MANAUSDT",
    "KASUSDT", "STRKUSDT"
]


def fetch_binance_klines(symbol: str, interval: str = "5m", limit: int = 1000, end_time: int = None) -> List[List[Any]]:
    """Загружает исторические свечи напрямую с Binance REST API."""
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    if end_time:
        url += f"&endTime={end_time}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data


def load_or_fetch_history(symbol: str, days: int = 30, force_fetch: bool = False) -> List[List[float]]:
    """Загружает или кэширует 5m свечи для символа за указанное количество дней."""
    cache_file = os.path.join(CACHE_DIR, f"{symbol}_5m_swing.csv.gz")
    if not force_fetch and os.path.exists(cache_file):
        try:
            klines = []
            with gzip.open(cache_file, "rt", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                for row in reader:
                    klines.append([int(row[0])] + [float(x) for x in row[1:]])
            if len(klines) >= 500:
                return klines
        except Exception:
            pass

    total_bars = days * 288
    all_raw = []
    curr_end = None

    while len(all_raw) < total_bars:
        try:
            batch = fetch_binance_klines(symbol, "5m", limit=1000, end_time=curr_end)
            if not batch:
                break
            all_raw = batch + all_raw
            curr_end = int(batch[0][0]) - 1
            if len(batch) < 1000:
                break
            time.sleep(0.05)
        except Exception as e:
            break

    all_raw.sort(key=lambda x: int(x[0]))
    parsed = []
    for r in all_raw:
        parsed.append([
            int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[7])
        ])

    if parsed:
        with gzip.open(cache_file, "wt", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["open_time", "open", "high", "low", "close", "volume", "quote_volume"])
            for row in parsed:
                writer.writerow(row)

    return parsed


def simulate_swing_trade(
    signal: se.SwingSignal,
    future_candles_5m: List[List[float]],
    max_holding_bars: int = 288 * 10,
) -> Dict[str, Any]:
    """
    Симулирует удержание сделки Лонг-Свинг с безубытком при +8% и трейлингом при +15%+.
    """
    entry_price = signal.price
    sl_price = signal.sl_price
    target_price = signal.target_price
    be_price = entry_price * 1.005

    be_reached = False
    trailing_active = False
    highest_price = entry_price
    trailing_sl = sl_price

    exit_price = None
    exit_reason = None
    exit_bar = None

    for idx, candle in enumerate(future_candles_5m[:max_holding_bars]):
        high = candle[2]
        low = candle[3]

        if high > highest_price:
            highest_price = high

        gain_from_entry = (highest_price - entry_price) / entry_price * 100.0
        if gain_from_entry >= signal.be_activation_pct and not be_reached:
            be_reached = True
            trailing_sl = max(trailing_sl, be_price)

        if gain_from_entry >= signal.trailing_activation_pct:
            trailing_active = True
            current_trail = highest_price * (1.0 - signal.trailing_distance_pct / 100.0)
            trailing_sl = max(trailing_sl, current_trail)

        if high >= target_price:
            exit_price = target_price
            exit_reason = "take_profit"
            exit_bar = idx
            break

        current_active_stop = trailing_sl if (be_reached or trailing_active) else sl_price
        if low <= current_active_stop:
            exit_price = current_active_stop
            exit_reason = "trailing_sl" if trailing_active else ("breakeven" if be_reached else "stop_loss")
            exit_bar = idx
            break

    if exit_price is None:
        exit_price = future_candles_5m[min(len(future_candles_5m) - 1, max_holding_bars - 1)][4]
        exit_reason = "timeout_exit"
        exit_bar = max_holding_bars

    pnl_pct = (exit_price - entry_price) / entry_price * 100.0
    return {
        "symbol": signal.symbol,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "pnl_pct": pnl_pct,
        "exit_reason": exit_reason,
        "bars_held": exit_bar,
        "hours_held": round(exit_bar * 5 / 60, 1),
        "score": signal.score,
        "target_pct": signal.estimated_target_pct,
    }


def _simulate_symbol_trades(sym: str, klines: List[List[float]], min_score: float) -> List[Dict[str, Any]]:
    trades = []
    warmup_bars = 400
    step = 12  # Проверка каждый 1 час (12 свечей 5м) для свинга
    last_signal_bar = -100

    for i in range(warmup_bars, len(klines) - 288, step):
        if i - last_signal_bar < 48:
            continue

        sub_klines = klines[max(0, i - 600) : i + 1]
        sig = se.analyze_swing_setup(sym, sub_klines, min_score=min_score)
        if sig:
            last_signal_bar = i
            future = klines[i + 1:]
            trade_res = simulate_swing_trade(sig, future)
            trades.append(trade_res)
    return trades


def _worker_task(args):
    sym, klines, min_score = args
    return _simulate_symbol_trades(sym, klines, min_score)


def run_swing_backtest(symbols: List[str], days: int = 30, min_score: float = 70.0) -> Dict[str, Any]:
    """Запускает многопоточный бэктест свинг-стратегии по 50+ парам Binance."""
    print(f"📥 Загрузка данных по {len(symbols)} активам...", flush=True)

    loaded_klines: Dict[str, List[List[float]]] = {}
    with cf.ThreadPoolExecutor(max_workers=12) as executor:
        future_map = {executor.submit(load_or_fetch_history, sym, days): sym for sym in symbols}
        for fut in cf.as_completed(future_map):
            sym = future_map[fut]
            try:
                res = fut.result()
                if res and len(res) >= 500:
                    loaded_klines[sym] = res
            except Exception:
                pass

    print(f"🚀 Параллельный расчет на всех ядрах CPU ({len(loaded_klines)} пар)...", flush=True)
    all_trades = []
    task_args = [(sym, klines, min_score) for sym, klines in loaded_klines.items()]
    with cf.ProcessPoolExecutor() as executor:
        for t_list in executor.map(_worker_task, task_args):
            all_trades.extend(t_list)

    if not all_trades:
        print("❌ Сделок не найдено.")
        return {}

    wins = [t for t in all_trades if t["pnl_pct"] > 0]
    losses = [t for t in all_trades if t["pnl_pct"] <= 0]
    tp_hits = [t for t in all_trades if t["exit_reason"] == "take_profit"]
    trail_hits = [t for t in all_trades if t["exit_reason"] == "trailing_sl"]
    be_hits = [t for t in all_trades if t["exit_reason"] == "breakeven"]
    sl_hits = [t for t in all_trades if t["exit_reason"] == "stop_loss"]

    win_rate = len(wins) / len(all_trades) * 100.0
    total_gain = sum(t["pnl_pct"] for t in wins)
    total_loss = abs(sum(t["pnl_pct"] for t in losses))
    profit_factor = (total_gain / total_loss) if total_loss > 0 else 99.0
    net_pnl = sum(t["pnl_pct"] for t in all_trades)
    avg_trade = net_pnl / len(all_trades)
    avg_win = (total_gain / len(wins)) if wins else 0.0
    avg_loss = (total_loss / len(losses)) if losses else 0.0
    avg_hours = sum(t["hours_held"] for t in all_trades) / len(all_trades)

    print("\n" + "=" * 70)
    print("📊 РЕЗУЛЬТАТЫ БЭКТЕСТА СТРАТЕГИИ ЛОНГ-СВИНГ (+20%+)")
    print("=" * 70)
    print(f"• Всего проверенных активов:  {len(loaded_klines)}")
    print(f"• Всего свинг-сделок:        {len(all_trades)}")
    print(f"• Винрейт (Win Rate):         {win_rate:.1f}% ({len(wins)} в плюс / {len(losses)} в минус)")
    print(f"• Профит-фактор (PF):        {profit_factor:.2f}")
    print(f"• Суммарный PnL:             {net_pnl:+.1f}%")
    print(f"• Средний результат сделки:   {avg_trade:+.2f}%")
    print(f"• Средний профит выигрыша:   +{avg_win:.2f}%")
    print(f"• Средний убыток:            -{avg_loss:.2f}%")
    print(f"• Среднее время удержания:    {avg_hours:.1f} ч.")
    print("-" * 70)
    print("📈 Распределение исходов:")
    print(f"  🎯 Take-Profit (+20%+):     {len(tp_hits)} ({len(tp_hits)/len(all_trades)*100:.1f}%)")
    print(f"  📈 Trailing Profit:         {len(trail_hits)} ({len(trail_hits)/len(all_trades)*100:.1f}%)")
    print(f"  🛡️ Безубыток (Breakeven):   {len(be_hits)} ({len(be_hits)/len(all_trades)*100:.1f}%)")
    print(f"  🛑 Стоп-лосс (SL):          {len(sl_hits)} ({len(sl_hits)/len(all_trades)*100:.1f}%)")
    print("=" * 70)

    return {
        "total_trades": len(all_trades),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "net_pnl": net_pnl,
        "avg_trade": avg_trade,
    }


def main():
    parser = argparse.ArgumentParser(description="Бэктест свинг-стратегии 5m-4h на 50+ активах")
    parser.add_argument("--symbols", type=int, default=50, help="Количество активов")
    parser.add_argument("--days", type=int, default=30, help="Дней истории")
    parser.add_argument("--min-score", type=float, default=70.0, help="Минимальный Score для входа")
    args = parser.parse_args()

    symbols = TOP_50_SYMBOLS[:args.symbols]
    run_swing_backtest(symbols, days=args.days, min_score=args.min_score)


if __name__ == "__main__":
    main()
