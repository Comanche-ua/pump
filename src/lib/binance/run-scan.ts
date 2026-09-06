import { analyzeTimeframe, pickBest, previewTimeframe } from "../pump/engine.ts";
import { aggregateTimeframe, TF_MS } from "../pump/indicators.ts";
import type {
  Candle,
  Mover,
  PumpSignal,
  ScanInput,
  ScanResult,
  Ticker24h,
  Timeframe,
  TfBreakdown,
} from "../pump/types.ts";
import { DEFAULT_SCAN } from "../pump/types.ts";
import { baseAsset, isUsdtSpot } from "../pump/universe.ts";
import { fetchKlines, fetchTickers, mapPool } from "./http.ts";

const klineCache = new Map<string, { at: number; candles: Candle[] }>();
const KLINE_TTL = 20_000;
let tickerMemo: { at: number; tickers: Ticker24h[]; host: string } | null = null;
const TICKER_TTL = 12_000;

async function tickersCached() {
  if (tickerMemo && Date.now() - tickerMemo.at < TICKER_TTL) return tickerMemo;
  const fresh = await fetchTickers();
  tickerMemo = { at: Date.now(), ...fresh };
  return tickerMemo;
}

async function klinesCached(symbol: string) {
  const hit = klineCache.get(symbol);
  if (hit && Date.now() - hit.at < KLINE_TTL) return hit.candles;
  const candles = await fetchKlines(symbol, 200);
  klineCache.set(symbol, { at: Date.now(), candles });
  return candles;
}

function pickCandidates(tickers: Ticker24h[], input: ScanInput): Ticker24h[] {
  const liquid = tickers.filter(
    (t) =>
      isUsdtSpot(t.symbol) &&
      t.quoteVolume >= input.minQuoteVolume &&
      t.priceChangePercent <= input.alreadyPumpedMax,
  );
  const watch = new Set(input.watchlist.map((s) => s.toUpperCase()));
  const byChange = [...liquid].sort((a, b) => b.priceChangePercent - a.priceChangePercent);
  const byHeat = [...liquid].sort((a, b) => {
    const ha = Math.log10(a.quoteVolume + 1) * Math.max(a.priceChangePercent, 0);
    const hb = Math.log10(b.quoteVolume + 1) * Math.max(b.priceChangePercent, 0);
    return hb - ha;
  });
  const byVol = [...liquid].sort((a, b) => b.quoteVolume - a.quoteVolume);
  const picked = new Map<string, Ticker24h>();
  for (const t of liquid) {
    if (watch.has(t.symbol)) picked.set(t.symbol, t);
  }
  for (const t of byChange.slice(0, 16)) picked.set(t.symbol, t);
  for (const t of byHeat.slice(0, 16)) picked.set(t.symbol, t);
  for (const t of byVol.slice(0, 12)) picked.set(t.symbol, t);
  picked.delete("BTCUSDT");
  const list = [...picked.values()].slice(0, input.maxCandidates);
  return list;
}

function tfCandles(raw15: Candle[], tf: Timeframe): Candle[] {
  if (tf === "15m") return raw15;
  return aggregateTimeframe(raw15, TF_MS[tf]);
}

export async function runScan(partial: Partial<ScanInput> = {}): Promise<ScanResult> {
  const input: ScanInput = { ...DEFAULT_SCAN, ...partial };
  const started = Date.now();
  try {
    const { tickers, host } = await tickersCached();
    const universe = tickers.filter((t) => isUsdtSpot(t.symbol));
    const btcTicker = tickers.find((t) => t.symbol === "BTCUSDT") ?? null;
    const movers: Mover[] = [...universe]
      .sort((a, b) => b.priceChangePercent - a.priceChangePercent)
      .slice(0, 14)
      .map((t) => ({
        symbol: t.symbol,
        base: baseAsset(t.symbol),
        price: t.lastPrice,
        change24h: t.priceChangePercent,
        quoteVolume24h: t.quoteVolume,
      }));

    const candidates = pickCandidates(tickers, input);
    const need = ["BTCUSDT", ...candidates.map((t) => t.symbol)];
    const unique = [...new Set(need)];

    const klines = await mapPool(unique, 6, async (symbol) => {
      try {
        return { symbol, candles: await klinesCached(symbol) };
      } catch {
        return { symbol, candles: [] as Candle[] };
      }
    });
    const bySymbol = new Map(klines.map((k) => [k.symbol, k.candles]));
    const btc15 = bySymbol.get("BTCUSDT") ?? [];

    const signals: PumpSignal[] = [];
    const closest: PumpSignal[] = [];
    for (const t of candidates) {
      if (t.priceChangePercent > input.alreadyPumpedMax) continue;
      const raw15 = bySymbol.get(t.symbol) ?? [];
      if (raw15.length < 40) continue;
      const rows: TfBreakdown[] = [];
      const previews: TfBreakdown[] = [];
      for (const tf of input.timeframes) {
        const series = tfCandles(raw15, tf);
        const btcSeries = tfCandles(btc15, tf);
        const row = analyzeTimeframe(tf, series, btcSeries, t.priceChangePercent);
        if (row && row.score >= input.minScore) rows.push(row);
        const preview = previewTimeframe(tf, series, btcSeries, t.priceChangePercent);
        if (preview) previews.push(preview);
      }
      const btcRel = btcTicker ? t.priceChangePercent - btcTicker.priceChangePercent : 0;
      const toSignal = (best: TfBreakdown, pack: TfBreakdown[], grade = best.grade): PumpSignal | null => {
        if (grade === "none") return null;
        return {
          symbol: t.symbol,
          base: baseAsset(t.symbol),
          price: t.lastPrice,
          change24h: t.priceChangePercent,
          quoteVolume24h: t.quoteVolume,
          high24h: t.highPrice,
          low24h: t.lowPrice,
          btcRelative24h: btcRel,
          bestTf: best.timeframe,
          bestScore: best.score,
          grade,
          alertKey: `${t.symbol}:${best.timeframe}:${best.barOpenTime}`,
          byTf: pack,
        };
      };
      if (rows.length) {
        const best = pickBest(rows);
        const sig = toSignal(best, rows);
        if (sig) signals.push(sig);
      } else if (previews.length) {
        const best = [...previews].sort((a, b) => b.score - a.score)[0]!;
        if (best.volumeRatio >= 1.15) {
          closest.push({
          symbol: t.symbol,
          base: baseAsset(t.symbol),
          price: t.lastPrice,
          change24h: t.priceChangePercent,
          quoteVolume24h: t.quoteVolume,
          high24h: t.highPrice,
          low24h: t.lowPrice,
          btcRelative24h: btcRel,
          bestTf: best.timeframe,
          bestScore: best.score,
          grade: best.grade === "none" ? "watch" : best.grade,
          alertKey: `${t.symbol}:${best.timeframe}:${best.barOpenTime}:near`,
          byTf: previews,
        });
        }
      }
    }

    signals.sort((a, b) => {
      const order = { strong: 0, watch: 1, late: 2 };
      const g = order[a.grade] - order[b.grade];
      if (g !== 0) return g;
      return b.bestScore - a.bestScore;
    });

    closest.sort((a, b) => b.bestScore - a.bestScore);

    return {
      ok: true,
      scannedAt: Date.now(),
      durationMs: Date.now() - started,
      source: host,
      universe: universe.length,
      candidates: candidates.length,
      btc: btcTicker
        ? { price: btcTicker.lastPrice, change24h: btcTicker.priceChangePercent }
        : null,
      signals,
      closest: closest.slice(0, 8),
      movers,
    };
  } catch (err) {
    return {
      ok: false,
      error: err instanceof Error ? err.message : "Scan failed",
      scannedAt: Date.now(),
      durationMs: Date.now() - started,
      source: "",
      universe: 0,
      candidates: 0,
      btc: null,
      signals: [],
      closest: [],
      movers: [],
    };
  }
}

export async function getSymbolKlines(symbol: string, tf: Timeframe) {
  const raw = await klinesCached(symbol.toUpperCase());
  return tfCandles(raw, tf);
}
