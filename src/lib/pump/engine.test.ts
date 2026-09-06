import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { analyzeTimeframe } from "./engine.ts";
import { aggregateTimeframe, rsi, sma, TF_MS } from "./indicators.ts";
import { isUsdtSpot } from "./universe.ts";
import type { Candle } from "./types.ts";

function c(
  i: number,
  o: number,
  h: number,
  l: number,
  cl: number,
  vol: number,
  takerFrac = 0.55,
): Candle {
  const step = 15 * 60 * 1000;
  const openTime = 1_700_000_000_000 + i * step;
  return {
    openTime,
    open: o,
    high: h,
    low: l,
    close: cl,
    volume: vol,
    quoteVolume: vol * cl,
    trades: 400,
    takerBuyBase: vol * takerFrac,
    closeTime: openTime + step - 1,
  };
}

/** Range chop, then a single impulse candle. */
function grindThenImpulse(opts: {
  impulseVol: number;
  impulsePct: number;
  wickFrac?: number;
  taker?: number;
  bearish?: boolean;
}): Candle[] {
  const out: Candle[] = [];
  for (let i = 0; i < 40; i++) {
    const mid = 100 + Math.sin(i * 0.85) * 0.35;
    const o = mid - 0.04;
    const cl = mid + 0.03 * (i % 2 === 0 ? 1 : -1);
    out.push(c(i, o, Math.max(o, cl) + 0.08, Math.min(o, cl) - 0.08, cl, 100 + (i % 4) * 3, 0.52));
  }
  const o = out[out.length - 1]!.close;
  const span = o * (opts.impulsePct / 100);
  const cl = opts.bearish ? o - span : o + span;
  const wick = span * (opts.wickFrac ?? 0.08);
  const h = Math.max(o, cl) + wick;
  const l = Math.min(o, cl) - span * 0.05;
  out.push(c(40, o, h, l, cl, opts.impulseVol, opts.taker ?? 0.66));
  return out;
}

describe("universe", () => {
  it("keeps USDT spot and drops stables/lev", () => {
    assert.equal(isUsdtSpot("SOLUSDT"), true);
    assert.equal(isUsdtSpot("BTCUSDT"), true);
    assert.equal(isUsdtSpot("USDCUSDT"), false);
    assert.equal(isUsdtSpot("SOLUPUSDT"), false);
    assert.equal(isUsdtSpot("ETHBTC"), false);
  });
});

describe("indicators", () => {
  it("sma and rsi are stable on a flat series", () => {
    const vals = Array.from({ length: 30 }, () => 10);
    assert.equal(sma(vals, 20), 10);
    const closes = Array.from({ length: 20 }, (_, i) => 100 + i);
    const r = rsi(closes, 14);
    assert.ok(r !== null && r > 70);
  });

  it("aggregates 15m into 1h", () => {
    const candles = Array.from({ length: 8 }, (_, i) => c(i, 10, 11, 9, 10.5, 2));
    const hourly = aggregateTimeframe(candles, TF_MS["1h"]);
    assert.ok(hourly.length >= 1);
    assert.equal(hourly[0]!.volume, 8);
  });
});

describe("pump engine", () => {
  it("scores a volume+breakout impulse as strong/watch, not late", () => {
    const candles = grindThenImpulse({ impulseVol: 380, impulsePct: 1.1 });
    const row = analyzeTimeframe("15m", candles, candles, 5);
    assert.ok(row, "expected a signal");
    assert.notEqual(row!.grade, "late");
    assert.ok(row!.score >= 60, `score ${row!.score}`);
    assert.ok(row!.volumeRatio > 2);
  });

  it("rejects a thin-volume drift", () => {
    const candles = grindThenImpulse({ impulseVol: 110, impulsePct: 0.4 });
    const row = analyzeTimeframe("15m", candles, candles, 1);
    assert.equal(row, null);
  });

  it("rejects a rejection wick fakeout", () => {
    const candles = grindThenImpulse({
      impulseVol: 400,
      impulsePct: 0.3,
      wickFrac: 8,
      taker: 0.4,
    });
    const row = analyzeTimeframe("15m", candles, candles, 2);
    assert.equal(row, null);
  });

  it("rejects a red candle", () => {
    const candles = grindThenImpulse({ impulseVol: 400, impulsePct: 1.2, bearish: true });
    const row = analyzeTimeframe("15m", candles, candles, 2);
    assert.equal(row, null);
  });

  it("flags a stretched 24h move as late when score still prints", () => {
    const candles = grindThenImpulse({ impulseVol: 520, impulsePct: 1.4 });
    const row = analyzeTimeframe("15m", candles, candles, 28);
    if (row) assert.equal(row.grade, "late");
  });
});
