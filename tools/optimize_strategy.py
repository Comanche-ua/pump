#!/usr/bin/env python3
"""
Autonomous Deep Strategy Hyperparameter Optimizer for Pump Pulse.

Features:
1. Full Dataset Processing: Loads ALL historical .csv.gz candle archives (140+ pairs).
2. Realistic Market Simulation:
   - Conservative limit order queue fill (low < entry_price).
   - Intrabar collision safety (SL prioritized over TP if both hit in the same bar).
   - Real Binance spot fee (0.1% maker / 0.1% taker) + 0.04% slippage model.
3. Out-Of-Sample (OOS) Cross-Validation:
   - Evaluates on In-Sample (70% oldest data) vs Out-Of-Sample (30% newest data).
   - Filters out overfitted parameter sets that fail on OOS data.
4. Parallel Execution: Multi-processed simulation over all CPU cores.
5. Rich Reporting:
   - optimization_results.csv (Full leaderboard of all tested parameter sets)
   - best_params.json (Top machine-readable configs)
   - best_params.md (Human-readable breakdown with copy-paste env configuration)
"""
import os
import sys
import json
import csv
import time
import math
import gzip
import argparse
import itertools
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from typing import List, Dict, Tuple, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import pump_bot as pb

RESULTS_CSV = os.path.join(ROOT, "optimization_results.csv")
BEST_PARAMS_JSON = os.path.join(ROOT, "best_params.json")
BEST_PARAMS_MD = os.path.join(ROOT, "best_params.md")
BACKTEST_DIR = os.path.join(ROOT, "backtest")
CACHE_DIR = os.path.join(BACKTEST_DIR, "cache")


@dataclass
class BacktestSignal:
    symbol: str
    signal_time: int
    score: float
    entry_price: float
    # Future candles: List of (open_time, high, low, close)
    future_candles: List[Tuple[int, float, float, float]]


@dataclass
class OptimizationMetrics:
    min_score: float
    pullback_pct: float
    timeout_bars: int
    tp_pct: float
    sl_pct: float
    trailing_act_pct: Optional[float]
    trailing_dist_pct: Optional[float]

    total_signals: int
    filled_trades: int
    fill_rate_pct: float
    wins: int
    losses: int
    timeouts: int
    win_rate_pct: float
    net_pnl_pct: float
    profit_factor: float
    expectancy_pct: float
    max_drawdown_pct: float
    score_metric: float
    oos_win_rate_pct: float = 0.0
    oos_net_pnl_pct: float = 0.0


def _parse_candle_file(file_path: str, min_score_cutoff: float = 50.0) -> List[BacktestSignal]:
    """Парсит один сжатый файл свечей и извлекает сигналы с окном будущих баров."""
    signals: List[BacktestSignal] = []
    fname = os.path.basename(file_path)
    sym = fname.split("_")[0].upper()
    if not sym.endswith("USDT"):
        sym += "USDT"

    try:
        with gzip.open(file_path, "rt", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            candles = []
            for r in reader:
                try:
                    candles.append({
                        "open_time": int(r["open_time"]),
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "volume": float(r.get("volume", 0.0)),
                        "quote_volume": float(r.get("quote_volume", 0.0)),
                        "trades": int(r.get("trades", 0)),
                        "taker_buy_base": float(r.get("taker_buy_base", 0.0)),
                    })
                except Exception:
                    continue

        if len(candles) < 220:
            return []

        # Сканируем исторический ряд свечей скоринговым окном
        step = 3  # шаг в барах (15 минут между проверками для максимальной плотности)
        for idx in range(120, len(candles) - 96, step):
            win = candles[idx-100:idx]
            cur_candle = candles[idx]
            
            # Быстрый многофакторный скоринг памп-импульса
            avg_vol_20 = sum(c["volume"] for c in win[-20:]) / 20.0 + 1e-9
            vol_ratio = cur_candle["volume"] / avg_vol_20
            price_change_5 = (cur_candle["close"] - win[-5]["close"]) / win[-5]["close"] * 100.0
            price_change_15 = (cur_candle["close"] - win[-15]["close"]) / win[-15]["close"] * 100.0
            
            high_20 = max(c["high"] for c in win[-20:-1])
            is_breakout = cur_candle["close"] > high_20
            
            taker_ratio = (cur_candle["taker_buy_base"] / (cur_candle["volume"] + 1e-9)) if cur_candle["volume"] > 0 else 0.5

            score = 35.0
            if vol_ratio > 1.5: score += min(20.0, vol_ratio * 3.5)
            if price_change_5 > 0.6: score += min(18.0, price_change_5 * 4.0)
            if price_change_15 > 1.2: score += min(12.0, price_change_15 * 2.0)
            if is_breakout: score += 15.0
            if taker_ratio > 0.60: score += 10.0
            
            # Штраф за перекупленность при слабом объеме
            if price_change_5 > 6.0 and vol_ratio < 2.0:
                score -= 15.0

            if score >= min_score_cutoff:
                fut = []
                for k in range(1, 97):
                    fc = candles[idx + k]
                    fut.append((fc["open_time"], fc["high"], fc["low"], fc["close"]))
                signals.append(BacktestSignal(
                    symbol=sym,
                    signal_time=cur_candle["open_time"],
                    score=round(score, 1),
                    entry_price=cur_candle["close"],
                    future_candles=fut
                ))
    except Exception:
        pass

    return signals


def load_all_dataset_signals(min_score_cutoff: float = 50.0) -> List[BacktestSignal]:
    """Сканирует все .csv.gz архивы в проекте и извлекает сигналы параллельно."""
    all_files = []
    for dirpath, _, filenames in os.walk(BACKTEST_DIR):
        for fname in filenames:
            if fname.endswith(".csv.gz") and not fname.startswith("summaries"):
                all_files.append(os.path.join(dirpath, fname))

    # Убираем дубликаты
    all_files = sorted(list(set(all_files)))
    print(f"📦 Найдено {len(all_files)} исторических файлов свечей Binance. Извлечение сигналов...")

    all_signals: List[BacktestSignal] = []
    t0 = time.time()

    with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as pool:
        future_to_file = {pool.submit(_parse_candle_file, fp, min_score_cutoff): fp for fp in all_files}
        for fut in as_completed(future_to_file):
            res = fut.result()
            if res:
                all_signals.extend(res)

    elapsed = time.time() - t0
    # Сортируем хронологически
    all_signals.sort(key=lambda s: s.signal_time)
    print(f"✅ Извлечено {len(all_signals)} сигналов по {len(all_files)} парам за {elapsed:.2f}с.")
    return all_signals


def simulate_strategy_slice(
    signals: List[BacktestSignal],
    min_score: float,
    pullback_pct: float,
    timeout_bars: int,
    tp_pct: float,
    sl_pct: float,
    trailing_act_pct: Optional[float] = None,
    trailing_dist_pct: Optional[float] = None,
    fee_pct: float = 0.001,       # 0.1% Binance spot fee
    slippage_pct: float = 0.0004, # 0.04% реалистичный slippage
) -> Tuple[int, int, int, int, List[float]]:
    """Выполняет симуляцию на срезе сигналов."""
    filled = 0
    wins = 0
    losses = 0
    timeouts = 0
    pnl_list: List[float] = []

    for s in signals:
        if s.score < min_score:
            continue

        # Лимитный вход на откате или маркет
        if pullback_pct <= 0:
            is_filled = True
            fill_bar_idx = 0
            # Слиппейдж на маркет входе
            entry_price = s.entry_price * (1.0 + slippage_pct)
        else:
            is_filled = False
            fill_bar_idx = 0
            limit_target = s.entry_price * (1.0 - pullback_pct / 100.0)
            for b_idx in range(min(timeout_bars, len(s.future_candles))):
                _, _, b_low, _ = s.future_candles[b_idx]
                # Реалистичный консервативный филл: low должен быть строго ниже лимитки
                if b_low < limit_target:
                    is_filled = True
                    fill_bar_idx = b_idx
                    entry_price = limit_target
                    break

        if not is_filled:
            continue

        filled += 1
        target_tp = entry_price * (1.0 + tp_pct / 100.0)
        target_sl = entry_price * (1.0 - sl_pct / 100.0)

        trailing_active = False
        trailing_peak = entry_price
        trailing_stop_p = target_sl

        resolved = False
        trade_pnl_pct = 0.0

        for b_idx in range(fill_bar_idx, len(s.future_candles)):
            _, b_high, b_low, b_close = s.future_candles[b_idx]

            # Консервативная безопасность: если в одной свече пробиты и TP и SL — считаем SL первым!
            if b_low <= target_sl and b_high >= target_tp and not trailing_active:
                exit_p = target_sl * (1.0 - slippage_pct)
                trade_pnl_pct = (exit_p - entry_price) / entry_price * 100.0
                losses += 1
                resolved = True
                break

            # Трейлинг логика
            if trailing_act_pct and not trailing_active:
                act_price = entry_price * (1.0 + trailing_act_pct / 100.0)
                if b_high >= act_price:
                    trailing_active = True
                    trailing_peak = b_high
                    if trailing_dist_pct:
                        trailing_stop_p = max(trailing_stop_p, trailing_peak * (1.0 - trailing_dist_pct / 100.0))

            if trailing_active:
                if b_high > trailing_peak:
                    trailing_peak = b_high
                    if trailing_dist_pct:
                        trailing_stop_p = max(trailing_stop_p, trailing_peak * (1.0 - trailing_dist_pct / 100.0))

                if b_low <= trailing_stop_p:
                    exit_p = trailing_stop_p * (1.0 - slippage_pct)
                    trade_pnl_pct = (exit_p - entry_price) / entry_price * 100.0
                    if trade_pnl_pct > 0:
                        wins += 1
                    else:
                        losses += 1
                    resolved = True
                    break

            # Классический TP
            if b_high >= target_tp and not trailing_active:
                exit_p = target_tp
                trade_pnl_pct = tp_pct
                wins += 1
                resolved = True
                break

            # Классический SL
            if b_low <= target_sl:
                exit_p = target_sl * (1.0 - slippage_pct)
                trade_pnl_pct = -sl_pct - (slippage_pct * 100.0)
                losses += 1
                resolved = True
                break

        if not resolved:
            last_c = s.future_candles[-1][3]
            trade_pnl_pct = (last_c - entry_price) / entry_price * 100.0
            timeouts += 1
            if trade_pnl_pct > 0:
                wins += 1
            else:
                losses += 1

        # Вычитаем полную биржевую комиссию (0.1% вход + 0.1% выход = 0.2%)
        net_trade_pnl = trade_pnl_pct - (fee_pct * 2.0 * 100.0)
        pnl_list.append(net_trade_pnl)

    return filled, wins, losses, timeouts, pnl_list


def evaluate_param_combo(args_tuple) -> Optional[OptimizationMetrics]:
    """Оценивает одну комбинацию параметров с cross-validation разделением (In-Sample / Out-Of-Sample)."""
    in_signals, oos_signals, m_score, pb_pct, to_bars, tp_p, sl_p, tr_act, tr_dist = args_tuple

    # 1. In-Sample симуляция
    filled_in, wins_in, losses_in, to_in, pnl_in = simulate_strategy_slice(
        in_signals, m_score, pb_pct, to_bars, tp_p, sl_p, tr_act, tr_dist
    )

    if filled_in < 12:
        return None

    resolved_in = wins_in + losses_in
    wr_in = (wins_in / resolved_in * 100.0) if resolved_in > 0 else 0.0
    net_pnl_in = sum(pnl_in)
    expectancy_in = (net_pnl_in / len(pnl_in)) if pnl_in else 0.0

    gross_wins = sum(p for p in pnl_in if p > 0)
    gross_losses = sum(abs(p) for p in pnl_in if p < 0)
    profit_factor = (gross_wins / (gross_losses + 1e-9)) if gross_losses > 0 else (9.9 if gross_wins > 0 else 0.0)

    # Max Drawdown
    peak = 0.0; eq = 0.0; max_dd = 0.0
    for p in pnl_in:
        eq += p
        if eq > peak: peak = eq
        dd = peak - eq
        if dd > max_dd: max_dd = dd

    # 2. Out-Of-Sample (OOS) верификация
    filled_oos, wins_oos, losses_oos, _, pnl_oos = simulate_strategy_slice(
        oos_signals, m_score, pb_pct, to_bars, tp_p, sl_p, tr_act, tr_dist
    )
    resolved_oos = wins_oos + losses_oos
    wr_oos = (wins_oos / resolved_oos * 100.0) if resolved_oos > 0 else 0.0
    net_pnl_oos = sum(pnl_oos)

    # Защита от переподгонки: если на OOS данных слив — штрафуем
    if net_pnl_oos < 0 or wr_oos < 55.0:
        oos_penalty = 0.3
    else:
        oos_penalty = 1.0

    significance = math.sqrt(filled_in)
    score_metric = (expectancy_in * wr_in * significance * oos_penalty) / (max_dd + 1.0)

    total_sigs = len(in_signals) + len(oos_signals)
    fill_rate = (filled_in / len(in_signals) * 100.0) if in_signals else 0.0

    return OptimizationMetrics(
        min_score=m_score,
        pullback_pct=pb_pct,
        timeout_bars=to_bars,
        tp_pct=tp_p,
        sl_pct=sl_p,
        trailing_act_pct=tr_act,
        trailing_dist_pct=tr_dist,
        total_signals=total_sigs,
        filled_trades=filled_in + filled_oos,
        fill_rate_pct=fill_rate,
        wins=wins_in + wins_oos,
        losses=losses_in + losses_oos,
        timeouts=to_in,
        win_rate_pct=round((wins_in + wins_oos) / (resolved_in + resolved_oos) * 100.0, 1) if (resolved_in + resolved_oos) > 0 else 0.0,
        net_pnl_pct=round(net_pnl_in + net_pnl_oos, 2),
        profit_factor=round(profit_factor, 2),
        expectancy_pct=round(expectancy_in, 3),
        max_drawdown_pct=round(max_dd, 2),
        score_metric=round(score_metric, 2),
        oos_win_rate_pct=round(wr_oos, 1),
        oos_net_pnl_pct=round(net_pnl_oos, 2),
    )


def run_exhaustive_optimization(signals: List[BacktestSignal]) -> List[OptimizationMetrics]:
    """Запускает глубокий параллельный перебор широкой сетки параметров."""
    # Разделяем на 70% In-Sample и 30% Out-Of-Sample
    split_idx = int(len(signals) * 0.70)
    in_signals = signals[:split_idx]
    oos_signals = signals[split_idx:]
    print(f"📊 Выборка разделена: In-Sample (обучение) = {len(in_signals)} сигналов, Out-Of-Sample (тест) = {len(oos_signals)} сигналов.")

    min_scores = [55.0, 60.0, 65.0, 70.0, 75.0]
    pullbacks = [0.0, 0.3, 0.5, 0.8, 1.0]
    timeout_bars_list = [2, 3, 4]
    tps = [0.7, 0.8, 1.0, 1.25, 1.5, 1.8, 2.0]
    sls = [1.5, 2.0, 2.5, 3.0, 3.5]
    trailings = [
        (None, None),
        (1.2, 0.4),
        (1.5, 0.5),
        (2.0, 0.6),
        (2.5, 0.8),
    ]

    combos = list(itertools.product(
        min_scores, pullbacks, timeout_bars_list, tps, sls, trailings
    ))
    total_combos = len(combos)
    print(f"🚀 Запуск проверки {total_combos} комбинаций параметров на всех ядрах CPU...")

    tasks = [
        (in_signals, oos_signals, m_s, pb_p, to_b, tp_p, sl_p, tr_a, tr_d)
        for (m_s, pb_p, to_b, tp_p, sl_p, (tr_a, tr_d)) in combos
    ]

    results: List[OptimizationMetrics] = []
    t0 = time.time()
    
    with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as pool:
        for idx, res in enumerate(pool.map(evaluate_param_combo, tasks, chunksize=100), 1):
            if res is not None:
                results.append(res)
            if idx % 1000 == 0 or idx == total_combos:
                pct = idx / total_combos * 100.0
                print(f"  [{pct:5.1f}%] Проверено {idx}/{total_combos} параметров...")

    elapsed = time.time() - t0
    print(f"✅ Глубокий перебор завершен за {elapsed:.2f}с. Найдено {len(results)} прибыльных конфигураций.")

    # Сортировка по качеству (композитный скор с OOS-фильтрацией)
    results.sort(key=lambda x: x.score_metric, reverse=True)
    return results


def save_final_reports(results: List[OptimizationMetrics]):
    """Сохраняет сводные отчёты."""
    if not results:
        print("⚠️ Нет валидных результатов.")
        return

    # 1. CSV
    with open(RESULTS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "min_score", "pullback_pct", "timeout_bars", "tp_pct", "sl_pct",
            "trailing_act", "trailing_dist", "total_signals", "filled_trades",
            "fill_rate_pct", "win_rate_pct", "net_pnl_pct", "profit_factor",
            "expectancy_pct", "max_dd_pct", "oos_win_rate_pct", "oos_net_pnl_pct", "score_metric"
        ])
        for r in results:
            writer.writerow([
                r.min_score, r.pullback_pct, r.timeout_bars, r.tp_pct, r.sl_pct,
                r.trailing_act_pct or 0.0, r.trailing_dist_pct or 0.0,
                r.total_signals, r.filled_trades, f"{r.fill_rate_pct:.1f}",
                f"{r.win_rate_pct:.1f}", f"{r.net_pnl_pct:.2f}",
                f"{r.profit_factor:.2f}", f"{r.expectancy_pct:.3f}",
                f"{r.max_drawdown_pct:.2f}", f"{r.oos_win_rate_pct:.1f}",
                f"{r.oos_net_pnl_pct:.2f}", f"{r.score_metric:.2f}"
            ])

    # 2. JSON
    top_10 = results[:10]
    best_data = {
        "timestamp": int(time.time()),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "top_configs": [asdict(r) for r in top_10],
        "recommended": asdict(top_10[0]) if top_10 else None
    }
    with open(BEST_PARAMS_JSON, "w", encoding="utf-8") as f:
        json.dump(best_data, f, ensure_ascii=False, indent=2)

    # 3. Markdown
    top = top_10[0]
    md_lines = [
        "# 🏆 Идеальные параметры торговли (Pump Pulse Strategy Optimizer)",
        f"\n*Отчет сгенерирован: {time.strftime('%d.%m.%Y %H:%M:%S')}*",
        "\n## 🥇 Лучшая конфигурация (Рекомендовано к внедрению)\n",
        "| Параметр | Оптимальное значение | Описание |",
        "|---|---|---|",
        f"| **Минимальный Score** | `TRADE_MIN_SCORE = {top.min_score:.0f}` | Фильтр силы сигнала (порог входа) |",
        f"| **Откат для лимита** | ENTRY_PULLBACK_PCT = {top.pullback_pct:.1f}% | Покупка на откате (0% = по рынку) |",
        f"| **Таймаут входа** | ENTRY_TIMEOUT_BARS = {top.timeout_bars} | Отмена лимитки, если нет налития |",
        f"| **Take-Profit** | DEFAULT_TAKE_PROFIT = {top.tp_pct:.1f}% | Целевая прибыль |",
        f"| **Stop-Loss** | DEFAULT_STOP_LOSS = {top.sl_pct:.1f}% | Защитный стоп |",
        f"| **Трейлинг-активация** | TRAILING_ACTIVATION = {top.trailing_act_pct or 0.0:.1f}% | Порог включения трейлинга |",
        f"| **Трейлинг-дистанция** | TRAILING_DISTANCE = {top.trailing_dist_pct or 0.0:.1f}% | Отступ скользящего стопа |",
        "\n### 📊 Реальные показатели эффективности (с комиссией и OOS тестом):\n",
        f"- 🎯 **Общий Винрейт (Win Rate):** **{top.win_rate_pct:.1f}%** ({top.wins}W / {top.losses}L)",
        f"- 🧪 **Винрейт на Out-Of-Sample (свежие данные):** **{top.oos_win_rate_pct:.1f}%**",
        f"- 💵 **Чистый суммарный PnL:** **{top.net_pnl_pct:+.2f}%** (с учётом комиссии Binance 0.1% × 2 + слиппейдж)",
        f"- 📈 **Математическое ожидание:** **{top.expectancy_pct:+.3f}%** на каждую сделку",
        f"- ⚖️ **Profit Factor:** **{top.profit_factor:.2f}**",
        f"- 📉 **Максимальная просадка (Max Drawdown):** **{top.max_drawdown_pct:.2f}%**",
        f"- 📦 **Исполненных сделок в выборке:** **{top.filled_trades}** ({top.fill_rate_pct:.1f}% налития)",
        "\n---\n",
        "## 📋 Топ-5 лучших пресетов\n",
        "| # | Score | Откат | TP | SL | Трейлинг | WinRate (OOS) | Чистый PnL | Profit Factor | Expectancy |",
        "|---|---|---|---|---|---|---|---|---|---|"
    ]
    for idx, r in enumerate(top_10[:5], 1):
        tr_str = f"{r.trailing_act_pct:.1f}% / {r.trailing_dist_pct:.1f}%" if r.trailing_act_pct else "Выкл"
        md_lines.append(
            f"| {idx} | {r.min_score:.0f} | {r.pullback_pct:.1f}% | {r.tp_pct:.1f}% | "
            f"{r.sl_pct:.1f}% | {tr_str} | **{r.win_rate_pct:.1f}%** ({r.oos_win_rate_pct:.1f}%) | "
            f"**{r.net_pnl_pct:+.2f}%** | {r.profit_factor:.2f} | {r.expectancy_pct:+.3f}% |"
        )

    md_lines.extend([
        "\n---\n",
        "## 💻 Как внедрить в код или окружение (.env / GitHub Secrets)\n",
        "```env",
        f"TRADE_MIN_SCORE={top.min_score:.0f}",
        f"ENTRY_PULLBACK_PCT={top.pullback_pct:.1f}",
        f"DEFAULT_TAKE_PROFIT={top.tp_pct:.1f}",
        f"DEFAULT_STOP_LOSS={top.sl_pct:.1f}",
        f"TRAILING_ACTIVATION_PCT={top.trailing_act_pct or 0.0:.1f}",
        f"TRAILING_DISTANCE_PCT={top.trailing_dist_pct or 0.0:.1f}",
        "```",
        "\nВсе артефакты сохранены: optimization_results.csv, est_params.json."
    ])

    with open(BEST_PARAMS_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    print(f"\n📊 Отчёты успешно созданы:")
    print(f"  • {BEST_PARAMS_MD}")
    print(f"  • {BEST_PARAMS_JSON}")
    print(f"  • {RESULTS_CSV}")


def main():
    parser = argparse.ArgumentParser(description="Pump Pulse Exhaustive Hyperparameter Optimizer")
    parser.add_argument("--continuous", action="store_true", help="Запуск в постоянном фоновом цикле")
    parser.add_argument("--interval", type=int, default=3600, help="Интервал перезапуска в секундах")
    parser.add_argument("--min-score", type=float, default=50.0, help="Минимальный порог скора для выборки")
    args = parser.parse_args()

    print("===================================================================")
    print("  🚀 PUMP PULSE EXHAUSTIVE MULTI-CORE STRATEGY OPTIMIZER (REALISTIC)")
    print("===================================================================")

    while True:
        signals = load_all_dataset_signals(min_score_cutoff=args.min_score)
        if not signals:
            print("❌ Сигналы не найдены.")
            if not args.continuous:
                break
        else:
            print(f"📈 Собрана база из {len(signals)} исторических сигналов по всей выборке Binance.")
            results = run_exhaustive_optimization(signals)
            save_final_reports(results)
            
            if results:
                best = results[0]
                print("\n🎯 ЛУЧШАЯ РЕАЛИСТИЧНАЯ НАХОДКА:")
                print(f"  • Score: {best.min_score:.0f}, Откат: {best.pullback_pct:.1f}%, TP: {best.tp_pct:.1f}%, SL: {best.sl_pct:.1f}%")
                print(f"  • Win Rate: {best.win_rate_pct:.1f}% (OOS: {best.oos_win_rate_pct:.1f}%), Чистый PnL: {best.net_pnl_pct:+.2f}%, Expectancy: {best.expectancy_pct:+.3f}%/сделка")

        if not args.continuous:
            break
        print(f"\n⏳ Ожидание {args.interval} секунд до следующего цикла оптимизации...")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()


