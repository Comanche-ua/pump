import type { Candle, Ticker24h } from "../pump/types.ts";

const HOSTS = [
  "https://data-api.binance.vision",
  "https://api.binance.com",
  "https://api1.binance.com",
];

const FETCH_MS = 12_000;

export type HostState = { host: string };

const state: HostState = { host: HOSTS[0]! };

async function fetchJson(path: string): Promise<{ data: unknown; host: string }> {
  const ordered = [state.host, ...HOSTS.filter((h) => h !== state.host)];
  let lastErr: Error | null = null;
  for (const host of ordered) {
    try {
      const res = await fetch(`${host}${path}`, {
        headers: { accept: "application/json" },
        signal: AbortSignal.timeout(FETCH_MS),
      });
      if (!res.ok) {
        lastErr = new Error(`Binance ${res.status}`);
        continue;
      }
      const data: unknown = await res.json();
      state.host = host;
      return { data, host };
    } catch (err) {
      lastErr = err instanceof Error ? err : new Error(String(err));
    }
  }
  throw lastErr ?? new Error("Binance unreachable");
}

function num(v: unknown) {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : 0;
}

export function parseTicker(raw: Record<string, unknown>): Ticker24h {
  return {
    symbol: String(raw.symbol ?? ""),
    lastPrice: num(raw.lastPrice),
    priceChangePercent: num(raw.priceChangePercent),
    highPrice: num(raw.highPrice),
    lowPrice: num(raw.lowPrice),
    quoteVolume: num(raw.quoteVolume),
    volume: num(raw.volume),
  };
}

export function parseKline(row: unknown): Candle | null {
  if (!Array.isArray(row) || row.length < 11) return null;
  return {
    openTime: num(row[0]),
    open: num(row[1]),
    high: num(row[2]),
    low: num(row[3]),
    close: num(row[4]),
    volume: num(row[5]),
    closeTime: num(row[6]),
    quoteVolume: num(row[7]),
    trades: num(row[8]),
    takerBuyBase: num(row[9]),
  };
}

export async function fetchTickers(): Promise<{ tickers: Ticker24h[]; host: string }> {
  const { data, host } = await fetchJson("/api/v3/ticker/24hr");
  if (!Array.isArray(data)) throw new Error("Unexpected ticker payload");
  const tickers = data
    .filter((row): row is Record<string, unknown> => !!row && typeof row === "object")
    .map(parseTicker)
    .filter((t) => t.symbol);
  return { tickers, host };
}

export async function fetchKlines(symbol: string, limit = 200): Promise<Candle[]> {
  const path = `/api/v3/klines?symbol=${encodeURIComponent(symbol)}&interval=15m&limit=${limit}`;
  const { data } = await fetchJson(path);
  if (!Array.isArray(data)) return [];
  const candles: Candle[] = [];
  for (const row of data) {
    const c = parseKline(row);
    if (c) candles.push(c);
  }
  return candles;
}

export async function mapPool<T, R>(
  items: T[],
  limit: number,
  fn: (item: T) => Promise<R>,
): Promise<R[]> {
  const out: R[] = new Array(items.length);
  let cursor = 0;
  async function worker() {
    while (cursor < items.length) {
      const i = cursor++;
      out[i] = await fn(items[i]!);
    }
  }
  const n = Math.min(limit, items.length);
  await Promise.all(Array.from({ length: n }, () => worker()));
  return out;
}
