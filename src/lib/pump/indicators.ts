import type { Candle } from "./types.ts";

export function clamp(n: number, lo: number, hi: number) {
  return Math.min(hi, Math.max(lo, n));
}

export function sma(values: number[], period: number): number | null {
  if (values.length < period) return null;
  let sum = 0;
  for (let i = values.length - period; i < values.length; i++) sum += values[i]!;
  return sum / period;
}

export function ema(values: number[], period: number): number | null {
  if (values.length < period) return null;
  const k = 2 / (period + 1);
  let prev = 0;
  for (let i = 0; i < period; i++) prev += values[i]!;
  prev /= period;
  for (let i = period; i < values.length; i++) {
    prev = values[i]! * k + prev * (1 - k);
  }
  return prev;
}

/** Wilder RSI. */
export function rsi(closes: number[], period = 14): number | null {
  if (closes.length < period + 1) return null;
  let gain = 0;
  let loss = 0;
  for (let i = 1; i <= period; i++) {
    const d = closes[i]! - closes[i - 1]!;
    if (d >= 0) gain += d;
    else loss -= d;
  }
  gain /= period;
  loss /= period;
  for (let i = period + 1; i < closes.length; i++) {
    const d = closes[i]! - closes[i - 1]!;
    gain = (gain * (period - 1) + Math.max(d, 0)) / period;
    loss = (loss * (period - 1) + Math.max(-d, 0)) / period;
  }
  if (loss === 0) return 100;
  const rs = gain / loss;
  return 100 - 100 / (1 + rs);
}

export function trueRange(curr: Candle, prev: Candle) {
  return Math.max(
    curr.high - curr.low,
    Math.abs(curr.high - prev.close),
    Math.abs(curr.low - prev.close),
  );
}

/** Wilder ATR. */
export function atr(candles: Candle[], period = 14): number | null {
  if (candles.length < period + 1) return null;
  let val = 0;
  for (let i = 1; i <= period; i++) val += trueRange(candles[i]!, candles[i - 1]!);
  val /= period;
  for (let i = period + 1; i < candles.length; i++) {
    val = (val * (period - 1) + trueRange(candles[i]!, candles[i - 1]!)) / period;
  }
  return val;
}

export function donchianHigh(candles: Candle[], period: number): number | null {
  if (candles.length < period) return null;
  let h = -Infinity;
  for (let i = candles.length - period; i < candles.length; i++) {
    h = Math.max(h, candles[i]!.high);
  }
  return h;
}

export function vwap(candles: Candle[]): number | null {
  let pv = 0;
  let vol = 0;
  for (const c of candles) {
    const typical = (c.high + c.low + c.close) / 3;
    pv += typical * c.quoteVolume;
    vol += c.quoteVolume;
  }
  if (vol <= 0) return null;
  return pv / vol;
}

export function pctChange(from: number, to: number) {
  if (from === 0) return 0;
  return ((to - from) / from) * 100;
}

export function aggregateTimeframe(candles: Candle[], ms: number): Candle[] {
  const groups = new Map<number, Candle[]>();
  for (const c of candles) {
    const key = Math.floor(c.openTime / ms) * ms;
    const arr = groups.get(key);
    if (arr) arr.push(c);
    else groups.set(key, [c]);
  }
  const keys = [...groups.keys()].sort((a, b) => a - b);
  const out: Candle[] = [];
  for (const key of keys) {
    const g = groups.get(key)!;
    const first = g[0]!;
    const last = g[g.length - 1]!;
    let high = -Infinity;
    let low = Infinity;
    let volume = 0;
    let quoteVolume = 0;
    let trades = 0;
    let takerBuyBase = 0;
    for (const c of g) {
      high = Math.max(high, c.high);
      low = Math.min(low, c.low);
      volume += c.volume;
      quoteVolume += c.quoteVolume;
      trades += c.trades;
      takerBuyBase += c.takerBuyBase;
    }
    out.push({
      openTime: key,
      open: first.open,
      high,
      low,
      close: last.close,
      volume,
      quoteVolume,
      trades,
      takerBuyBase,
      closeTime: last.closeTime,
    });
  }
  return out;
}

export const TF_MS = {
  "15m": 15 * 60 * 1000,
  "30m": 30 * 60 * 1000,
  "1h": 60 * 60 * 1000,
} as const;
