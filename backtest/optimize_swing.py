#!/usr/bin/env python3
"""
Высокоскоростной оптимизатор параметров стратегии Лонг-Свинг (+20%+).
1. Параллельно извлекает сигналы на всех ядрах CPU.
2. Мгновенно тестирует сотни комбинаций риск-менеджмента для максимизации Винрейта и Профит-Фактора.

Запуск:
    python backtest/optimize_swing.py --days 20 --symbols 80
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
sys.path.insert(0, HERE)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import swing_engine as se
from backtest_swing import load_or_fetch_history, TOP_50_SYMBOLS, CACHE_DIR
import pump_bot as pb


def get_universe_symbols(limit: int = 80) -> List[str]:
    """Возвращает пул ликвидных пар из кэша, топа и вайтлиста бота."""
    symbols = list(TOP_50_SYMBOLS)
    if hasattr(pb, "WHITELIST_SYMBOLS") and pb.WHITELIST_SYMBOLS:
        for s in pb.WHITELIST_SYMBOLS:
            if s.endswith("USDT") and s not in symbols:
                symbols.append(s)
    return symbols[:limit]


def _extract_signals_for_symbol(args: Tuple[str, List[List[float]], float]) -> List[Tuple[se.SwingSignal, List[List[float]]]]:
    """Извлекает сигналы и будущие бары для одного символа."""
    sym, klines, min_score = args
    extracted = []
    warmup_bars = 400
    step = 12
    last_signal_bar = -100
    n_bars = len(klines)

    for i in range(warmup_bars, n_bars - 288, step):
        if i - last_signal_bar < 48:
            continue

        sub_klines = klines[max(0, i - 600) : i + 1]
        sig = se.analyze_swing_setup(sym, sub_klines, min_score=min_score)
        if sig:
            last_signal_bar = i
            future = klines[i + 1:]
            extracted.append((sig, future))
    return extracted


def fast_simulate_trade(
    signal: se.SwingSignal,
    future_candles_5m: List[List[float]],
    be_activation_pct: float,
    trailing_activation_pct: float,
    trailing_distance_pct: float,
    max_holding_bars: int = 288 * 10,
) -> Tuple[float, str, float]:
    """
    Быстрая симуляция удержания.
    Возвращает (pnl_pct, exit_reason, hours_held).
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

        gain = (highest_price - entry_price) / entry_price * 100.0
        if gain >= be_activation_pct and not be_reached:
            be_reached = True
            trailing_sl = max(trailing_sl, be_price)

        if gain >= trailing_activation_pct:
            trailing_active = True
            current_trail = highest_price * (1.0 - trailing_distance_pct / 100.0)
            trailing_sl = max(trailing_sl, current_trail)

        if high >= target_price:
            exit_price = target_price
            exit_reason = "take_profit"
            exit_bar = idx
            break

        active_stop = trailing_sl if (be_reached or trailing_active) else sl_price
        if low <= active_stop:
            exit_price = active_stop
            exit_reason = "trailing_sl" if trailing_active else ("breakeven" if be_reached else "stop_loss")
            exit_bar = idx
            break

    if exit_price is None:
        last_idx = min(len(future_candles_5m) - 1, max_holding_bars - 1)
        exit_price = future_candles_5m[last_idx][4]
        exit_reason = "timeout"
        exit_bar = max_holding_bars

    pnl_pct = (exit_price - entry_price) / entry_price * 100.0
    return pnl_pct, exit_reason, exit_bar * 5.0 / 60.0


def main():
    parser = argparse.ArgumentParser(description="Оптимизация параметров стратегии Лонг-Свинг")
    parser.add_argument("--days", type=int, default=20, help="Дней истории")
    parser.add_argument("--symbols", type=int, default=50, help="Количество пар")
    args = parser.parse_args()

    symbols = get_universe_symbols(limit=args.symbols)
    print(f"📥 Загрузка данных по {len(symbols)} активам Binance Spot...", flush=True)

    loaded_klines: Dict[str, List[List[float]]] = {}
    with cf.ThreadPoolExecutor(max_workers=16) as executor:
        future_map = {executor.submit(load_or_fetch_history, sym, args.days): sym for sym in symbols}
        for fut in cf.as_completed(future_map):
            sym = future_map[fut]
            try:
                res = fut.result()
                if res and len(res) >= 500:
                    loaded_klines[sym] = res
            except Exception:
                pass

    print(f"✅ Готово к расчету: {len(loaded_klines)} пар.", flush=True)
    print("⚡ Извлечение сигналов на всех ядрах CPU...", flush=True)

    # Предварительное извлечение сигналов с базовым порогом score >= 55
    task_args = [(sym, klines, 55.0) for sym, klines in loaded_klines.items()]
    all_raw_signals: List[Tuple[se.SwingSignal, List[List[float]]]] = []
    with cf.ProcessPoolExecutor() as p_exec:
        for sig_list in p_exec.map(_extract_signals_for_symbol, task_args):
            all_raw_signals.extend(sig_list)

    print(f"🎯 Найдено {len(all_raw_signals)} потенциальных сетапов. Запуск комбинаторного Grid Search...", flush=True)

    score_thresholds = [55.0, 60.0, 65.0, 70.0, 75.0]
    be_options = [4.5, 6.0, 8.0, 10.0]
    trail_act_options = [10.0, 12.0, 15.0, 18.0]
    trail_dist_options = [2.0, 3.0, 4.0]

    results = []

    for sc in score_thresholds:
        # Фильтруем сигналы по Score
        filtered_signals = [item for item in all_raw_signals if item[0].score >= sc]
        if not filtered_signals:
            continue

        for be in be_options:
            for ta in trail_act_options:
                for td in trail_dist_options:
                    wins_pnl = []
                    losses_pnl = []
                    tp_cnt = 0
                    trail_cnt = 0
                    be_cnt = 0
                    sl_cnt = 0
                    total_pnl = 0.0
                    hours_list = []

                    for sig, future in filtered_signals:
                        pnl, reason, hrs = fast_simulate_trade(sig, future, be, ta, td)
                        total_pnl += pnl
                        hours_list.append(hrs)
                        if pnl > 0:
                            wins_pnl.append(pnl)
                        else:
                            losses_pnl.append(abs(pnl))

                        if reason == "take_profit":
                            tp_cnt += 1
                        elif reason == "trailing_sl":
                            trail_cnt += 1
                        elif reason == "breakeven":
                            be_cnt += 1
                        elif reason == "stop_loss":
                            sl_cnt += 1

                    total_trades = len(filtered_signals)
                    win_rate = len(wins_pnl) / total_trades * 100.0 if total_trades > 0 else 0.0
                    total_gain = sum(wins_pnl)
                    total_loss = sum(losses_pnl)
                    profit_factor = (total_gain / total_loss) if total_loss > 0 else 99.0
                    avg_win = (total_gain / len(wins_pnl)) if wins_pnl else 0.0
                    avg_loss = (total_loss / len(losses_pnl)) if losses_pnl else 0.0
                    avg_trade = total_pnl / total_trades if total_trades > 0 else 0.0
                    avg_hrs = sum(hours_list) / len(hours_list) if hours_list else 0.0

                    results.append({
                        "score": sc,
                        "be": be,
                        "trail_act": ta,
                        "trail_dist": td,
                        "trades": total_trades,
                        "win_rate": win_rate,
                        "profit_factor": profit_factor,
                        "net_pnl": total_pnl,
                        "avg_trade": avg_trade,
                        "avg_win": avg_win,
                        "avg_loss": avg_loss,
                        "rr_ratio": (avg_win / avg_loss) if avg_loss > 0 else 0.0,
                        "tp_cnt": tp_cnt,
                        "trail_cnt": trail_cnt,
                        "be_cnt": be_cnt,
                        "sl_cnt": sl_cnt,
                        "avg_hrs": avg_hrs,
                    })

    # 1. Топ по Винрейту (Win Rate)
    by_winrate = sorted(results, key=lambda r: (r["win_rate"], r["profit_factor"]), reverse=True)

    print("\n" + "=" * 105)
    print("🏆 ТОП-5 КОНФИГУРАЦИЙ ПО МАКСИМАЛЬНОМУ ВИНРЕЙТУ (WIN RATE %)")
    print("=" * 105)
    print(f"{'#':<3} {'Score':<6} {'BE':<6} {'Trail':<7} {'Dist':<5} {'Сделок':<7} {'WinRate':<9} {'PF':<6} {'Net PnL':<10} {'AvgWin':<8} {'AvgLoss':<8} {'R:R':<5}")
    print("-" * 105)
    for i, r in enumerate(by_winrate[:5], 1):
        print(
            f"{i:<3} {r['score']:<6.0f} +{r['be']:<5.1f}% +{r['trail_act']:<6.1f}% {r['trail_dist']:<5.1f}% "
            f"{r['trades']:<7} {r['win_rate']:<8.1f}% {r['profit_factor']:<6.2f} {r['net_pnl']:<+9.1f}% "
            f"+{r['avg_win']:<7.1f}% -{r['avg_loss']:<7.1f}% {r['rr_ratio']:<4.2f}"
        )

    # 2. Топ по Профит-Фактору (Profit Factor)
    by_pf = sorted(results, key=lambda r: (r["profit_factor"], r["win_rate"]), reverse=True)

    print("\n" + "=" * 105)
    print("🏆 ТОП-5 КОНФИГУРАЦИЙ ПО МАКСИМАЛЬНОМУ ПРОФИТ-ФАКТОРУ (PROFIT FACTOR)")
    print("=" * 105)
    print(f"{'#':<3} {'Score':<6} {'BE':<6} {'Trail':<7} {'Dist':<5} {'Сделок':<7} {'WinRate':<9} {'PF':<6} {'Net PnL':<10} {'AvgWin':<8} {'AvgLoss':<8} {'R:R':<5}")
    print("-" * 105)
    for i, r in enumerate(by_pf[:5], 1):
        print(
            f"{i:<3} {r['score']:<6.0f} +{r['be']:<5.1f}% +{r['trail_act']:<6.1f}% {r['trail_dist']:<5.1f}% "
            f"{r['trades']:<7} {r['win_rate']:<8.1f}% {r['profit_factor']:<6.2f} {r['net_pnl']:<+9.1f}% "
            f"+{r['avg_win']:<7.1f}% -{r['avg_loss']:<7.1f}% {r['rr_ratio']:<4.2f}"
        )
    print("=" * 105)


if __name__ == "__main__":
    main()
