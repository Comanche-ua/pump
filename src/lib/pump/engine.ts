import { atr, clamp, donchianHigh, ema, pctChange, rsi, sma } from "./indicators.ts";
import type { Candle, FactorScore, Grade, TfBreakdown, Timeframe } from "./types.ts";

const MIN_BARS = 32;

function gradeFrom(score: number, late: boolean): Grade | "none" {
  if (score < 60) return "none";
  if (late && score >= 66) return "late";
  if (score >= 74) return "strong";
  if (score >= 60) return "watch";
  return "none";
}

function roc(closes: number[], bars: number) {
  if (closes.length < bars + 1) return 0;
  const prev = closes[closes.length - 1 - bars]!;
  const last = closes[closes.length - 1]!;
  return pctChange(prev, last);
}

function scoreWindow(
  timeframe: Timeframe,
  candles: Candle[],
  btc: Candle[],
  change24h: number,
  forming: boolean,
  strict: boolean,
): TfBreakdown | null {
  if (candles.length < MIN_BARS) return null;

  const last = candles[candles.length - 1]!;
  const hist = candles.slice(0, -1);
  const closes = candles.map((c) => c.close);
  const vols = hist.map((c) => c.volume);

  const volSma = sma(vols, 20);
  if (!volSma || volSma <= 0) return null;

  const volRatio = last.volume / volSma;
  const range = last.high - last.low;
  if (range <= 0) return null;

  const body = Math.abs(last.close - last.open);
  const upperWick = last.high - Math.max(last.close, last.open);
  const closePos = (last.close - last.low) / range;
  const bodyRatio = body / range;
  const upperWickRatio = upperWick / range;
  const bullish = last.close > last.open;

  const atr14 = atr(hist, 14);
  if (!atr14 || atr14 <= 0) return null;
  const atrExpansion = range / atr14;

  const priorHigh = donchianHigh(hist, 20);
  if (!priorHigh) return null;
  const breakoutPct = pctChange(priorHigh, last.close);

  const rsi14 = rsi(closes, 14);
  if (rsi14 === null) return null;
  const rsiPrev = rsi(closes.slice(0, -1), 14);

  const ema9 = ema(closes, 9);
  const ema21 = ema(closes, 21);
  const emaAligned = ema9 !== null && ema21 !== null && last.close > ema9 && ema9 > ema21;

  const takerBuy = last.volume > 0 ? last.takerBuyBase / last.volume : 0.5;

  const lookback = timeframe === "1h" ? 3 : timeframe === "30m" ? 4 : 6;
  const assetRoc = roc(closes, lookback);
  const btcCloses = btc.map((c) => c.close);
  const btcRoc = btcCloses.length >= lookback + 1 ? roc(btcCloses, lookback) : 0;
  const vsBtcPct = assetRoc - btcRoc;

  const prevRoc = roc(closes.slice(0, -1), Math.max(1, lookback - 1));
  const accel = assetRoc - prevRoc;

  const changePct = pctChange(last.open, last.close);

  if (strict) {
    if (!bullish) return null;
    if (volRatio < 1.35) return null;
    if (bodyRatio < 0.28 && closePos < 0.55) return null;
    if (upperWickRatio > 0.52) return null;
  }

  const late = (rsiPrev !== null && rsiPrev >= 72) || change24h >= 22 || breakoutPct > 4.5;

  const volFactor = clamp((volRatio - 1.35) / 2.65, 0, 1);
  const breakoutFactor =
    last.close > priorHigh
      ? clamp(breakoutPct / 1.2, 0, 1)
      : clamp(1 - Math.abs(breakoutPct) / 0.45, 0, 0.35);
  const atrFactor = clamp((atrExpansion - 1.05) / 1.7, 0, 1);
  const quality = clamp(0.45 * bodyRatio + 0.55 * closePos - Math.max(0, upperWickRatio - 0.22) * 1.8, 0, 1);
  const takerFactor = clamp((takerBuy - 0.5) / 0.16, 0, 1);
  const emaFactor = emaAligned ? 1 : last.close > (ema9 ?? last.close) ? 0.45 : 0.1;
  let rsiFactor = 0;
  if (rsi14 >= 54 && rsi14 <= 72) rsiFactor = 1;
  else if (rsi14 > 72 && rsi14 <= 78) rsiFactor = 0.55;
  else if (rsi14 > 78 && rsi14 < 84) rsiFactor = 0.2;
  else if (rsi14 >= 50 && rsi14 < 54) rsiFactor = 0.4;
  const vsBtcFactor = clamp((vsBtcPct + 0.15) / 1.4, 0, 1);
  const accelFactor = clamp(accel / 1.2, 0, 1);

  const factors: FactorScore[] = [
    {
      id: "volume",
      label: "Всплеск объёма",
      weight: 22,
      value: volFactor,
      note: `${volRatio.toFixed(2)}× к SMA20`,
    },
    {
      id: "breakout",
      label: "Пробой Donchian 20",
      weight: 16,
      value: breakoutFactor,
      note: `${breakoutPct >= 0 ? "+" : ""}${breakoutPct.toFixed(2)}%`,
    },
    {
      id: "atr",
      label: "Расширение ATR",
      weight: 12,
      value: atrFactor,
      note: `${atrExpansion.toFixed(2)}× ATR14`,
    },
    {
      id: "candle",
      label: "Качество свечи",
      weight: 12,
      value: quality,
      note: `тело ${(bodyRatio * 100).toFixed(0)}% · закрытие ${(closePos * 100).toFixed(0)}%`,
    },
    {
      id: "taker",
      label: "Агрессия тейкера",
      weight: 10,
      value: takerFactor,
      note: `${(takerBuy * 100).toFixed(0)}% market buy`,
    },
    {
      id: "ema",
      label: "Тренд EMA 9/21",
      weight: 8,
      value: emaFactor,
      note: emaAligned ? "close > EMA9 > EMA21" : "нет выравнивания",
    },
    {
      id: "rsi",
      label: "Окно RSI 14",
      weight: 8,
      value: rsiFactor,
      note: rsi14.toFixed(1),
    },
    {
      id: "btc",
      label: "Сила vs BTC",
      weight: 7,
      value: vsBtcFactor,
      note: `${vsBtcPct >= 0 ? "+" : ""}${vsBtcPct.toFixed(2)}%`,
    },
    {
      id: "accel",
      label: "Ускорение ROC",
      weight: 5,
      value: accelFactor,
      note: `${accel >= 0 ? "+" : ""}${accel.toFixed(2)} п.п.`,
    },
  ];

  const score = factors.reduce((s, f) => s + f.value * f.weight, 0);
  const grade = gradeFrom(score, late);
  if (strict && grade === "none") return null;

  const reasons: string[] = [];
  if (volRatio >= 2) reasons.push(`объём ${volRatio.toFixed(1)}× среднего`);
  if (last.close > priorHigh) reasons.push("пробой 20-барного максимума");
  if (emaAligned) reasons.push("EMA выровнены вверх");
  if (takerBuy >= 0.58) reasons.push("доминируют market buy");
  if (rsi14 >= 54 && rsi14 <= 72) reasons.push("RSI в раннем импульсе, не перекуплен");
  if (vsBtcPct >= 0.4) reasons.push("обгоняет BTC");
  if (atrExpansion >= 1.8) reasons.push("расширение волатильности");
  if (!bullish) reasons.push("последняя свеча ещё не бычья");

  const risks: string[] = [];
  if (late) risks.push("уже растянут — риск опоздавшего входа");
  if (rsi14 >= 78) risks.push("RSI высокий, возможна разгрузка");
  if (upperWickRatio > 0.28) risks.push("верхняя тень — продавцы защищаются");
  if (change24h > 15) risks.push("сильный ход за 24ч, поздняя фаза");
  if (volRatio > 6) risks.push("климакс объёма — часто конец волны");

  return {
    timeframe,
    score,
    grade,
    volumeRatio: volRatio,
    atrExpansion,
    breakoutPct,
    rsi: rsi14,
    takerBuy,
    vsBtcPct,
    changePct,
    emaAligned,
    late,
    forming,
    barOpenTime: last.openTime,
    factors,
    reasons,
    risks,
  };
}

/**
 * Multi-factor USDT pump score.
 *
 * Designed to catch *early confirmed impulses*, not already-extended FOMO.
 * Scores the forming bar and the last closed bar, then keeps the stronger one.
 */
export function analyzeTimeframe(
  timeframe: Timeframe,
  candles: Candle[],
  btc: Candle[],
  change24h: number,
): TfBreakdown | null {
  const live = scoreWindow(timeframe, candles, btc, change24h, true, true);
  const closed = scoreWindow(timeframe, candles.slice(0, -1), btc, change24h, false, true);
  if (live && closed) return live.score >= closed.score ? live : closed;
  return live ?? closed;
}

export function previewTimeframe(
  timeframe: Timeframe,
  candles: Candle[],
  btc: Candle[],
  change24h: number,
): TfBreakdown | null {
  const live = scoreWindow(timeframe, candles, btc, change24h, true, false);
  const closed = scoreWindow(timeframe, candles.slice(0, -1), btc, change24h, false, false);
  if (live && closed) return live.score >= closed.score ? live : closed;
  return live ?? closed;
}

export function pickBest(rows: TfBreakdown[]): TfBreakdown {
  const rank = (g: Grade | "none") => (g === "strong" ? 3 : g === "watch" ? 2 : g === "late" ? 1 : 0);
  return [...rows].sort((a, b) => {
    const g = rank(b.grade) - rank(a.grade);
    if (g !== 0) return g;
    return b.score - a.score;
  })[0]!;
}
