export const TIMEFRAMES = ["15m", "30m", "1h"] as const;
export type Timeframe = (typeof TIMEFRAMES)[number];

export type Grade = "strong" | "watch" | "late";

export type Candle = {
  openTime: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  quoteVolume: number;
  trades: number;
  takerBuyBase: number;
  closeTime: number;
};

export type Ticker24h = {
  symbol: string;
  lastPrice: number;
  priceChangePercent: number;
  highPrice: number;
  lowPrice: number;
  quoteVolume: number;
  volume: number;
};

export type FactorScore = {
  id: string;
  label: string;
  weight: number;
  value: number;
  note: string;
};

export type TfBreakdown = {
  timeframe: Timeframe;
  score: number;
  grade: Grade | "none";
  volumeRatio: number;
  atrExpansion: number;
  breakoutPct: number;
  rsi: number;
  takerBuy: number;
  vsBtcPct: number;
  changePct: number;
  emaAligned: boolean;
  late: boolean;
  forming: boolean;
  barOpenTime: number;
  factors: FactorScore[];
  reasons: string[];
  risks: string[];
};

export type PumpSignal = {
  symbol: string;
  base: string;
  price: number;
  change24h: number;
  quoteVolume24h: number;
  high24h: number;
  low24h: number;
  btcRelative24h: number;
  bestTf: Timeframe;
  bestScore: number;
  grade: Grade;
  alertKey: string;
  byTf: TfBreakdown[];
};

export type Mover = {
  symbol: string;
  base: string;
  price: number;
  change24h: number;
  quoteVolume24h: number;
};

export type ScanInput = {
  minQuoteVolume: number;
  minScore: number;
  alreadyPumpedMax: number;
  maxCandidates: number;
  timeframes: Timeframe[];
  watchlist: string[];
};

export type ScanResult = {
  ok: boolean;
  error?: string;
  scannedAt: number;
  durationMs: number;
  source: string;
  universe: number;
  candidates: number;
  btc: { price: number; change24h: number } | null;
  signals: PumpSignal[];
  closest: PumpSignal[];
  movers: Mover[];
};

export const DEFAULT_SCAN: ScanInput = {
  minQuoteVolume: 1_500_000,
  minScore: 62,
  alreadyPumpedMax: 35,
  maxCandidates: 40,
  timeframes: ["15m", "30m", "1h"],
  watchlist: [],
};
