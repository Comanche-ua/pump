import { n as TSS_SERVER_FUNCTION, t as createServerFn } from "./ssr.mjs";
import { a as sendTelegramMessage, r as detectTelegramChat, t as DEFAULT_SCAN } from "./send-Dx-1cmTz.mjs";
//#region node_modules/.nitro/vite/services/ssr/assets/pulse-CmKrfGe4.js
var createServerRpc = (serverFnMeta, splitImportFn) => {
	const url = "/_serverFn/" + serverFnMeta.id;
	return Object.assign(splitImportFn, {
		url,
		serverFnMeta,
		[TSS_SERVER_FUNCTION]: true
	});
};
function clamp(n, lo, hi) {
	return Math.min(hi, Math.max(lo, n));
}
function sma(values, period) {
	if (values.length < period) return null;
	let sum = 0;
	for (let i = values.length - period; i < values.length; i++) sum += values[i];
	return sum / period;
}
function ema(values, period) {
	if (values.length < period) return null;
	const k = 2 / (period + 1);
	let prev = 0;
	for (let i = 0; i < period; i++) prev += values[i];
	prev /= period;
	for (let i = period; i < values.length; i++) prev = values[i] * k + prev * (1 - k);
	return prev;
}
/** Wilder RSI. */
function rsi(closes, period = 14) {
	if (closes.length < period + 1) return null;
	let gain = 0;
	let loss = 0;
	for (let i = 1; i <= period; i++) {
		const d = closes[i] - closes[i - 1];
		if (d >= 0) gain += d;
		else loss -= d;
	}
	gain /= period;
	loss /= period;
	for (let i = period + 1; i < closes.length; i++) {
		const d = closes[i] - closes[i - 1];
		gain = (gain * (period - 1) + Math.max(d, 0)) / period;
		loss = (loss * (period - 1) + Math.max(-d, 0)) / period;
	}
	if (loss === 0) return 100;
	return 100 - 100 / (1 + gain / loss);
}
function trueRange(curr, prev) {
	return Math.max(curr.high - curr.low, Math.abs(curr.high - prev.close), Math.abs(curr.low - prev.close));
}
/** Wilder ATR. */
function atr(candles, period = 14) {
	if (candles.length < period + 1) return null;
	let val = 0;
	for (let i = 1; i <= period; i++) val += trueRange(candles[i], candles[i - 1]);
	val /= period;
	for (let i = period + 1; i < candles.length; i++) val = (val * (period - 1) + trueRange(candles[i], candles[i - 1])) / period;
	return val;
}
function donchianHigh(candles, period) {
	if (candles.length < period) return null;
	let h = -Infinity;
	for (let i = candles.length - period; i < candles.length; i++) h = Math.max(h, candles[i].high);
	return h;
}
function pctChange(from, to) {
	if (from === 0) return 0;
	return (to - from) / from * 100;
}
function aggregateTimeframe(candles, ms) {
	const groups = /* @__PURE__ */ new Map();
	for (const c of candles) {
		const key = Math.floor(c.openTime / ms) * ms;
		const arr = groups.get(key);
		if (arr) arr.push(c);
		else groups.set(key, [c]);
	}
	const keys = [...groups.keys()].sort((a, b) => a - b);
	const out = [];
	for (const key of keys) {
		const g = groups.get(key);
		const first = g[0];
		const last = g[g.length - 1];
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
			closeTime: last.closeTime
		});
	}
	return out;
}
var TF_MS = {
	"15m": 9e5,
	"30m": 18e5,
	"1h": 36e5
};
var MIN_BARS = 32;
function gradeFrom(score, late) {
	if (score < 60) return "none";
	if (late && score >= 66) return "late";
	if (score >= 74) return "strong";
	if (score >= 60) return "watch";
	return "none";
}
function roc(closes, bars) {
	if (closes.length < bars + 1) return 0;
	const prev = closes[closes.length - 1 - bars];
	const last = closes[closes.length - 1];
	return pctChange(prev, last);
}
function scoreWindow(timeframe, candles, btc, change24h, forming, strict) {
	if (candles.length < MIN_BARS) return null;
	const last = candles[candles.length - 1];
	const hist = candles.slice(0, -1);
	const closes = candles.map((c) => c.close);
	const volSma = sma(hist.map((c) => c.volume), 20);
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
	const takerBuy = last.volume > 0 ? last.takerBuyBase / last.volume : .5;
	const lookback = timeframe === "1h" ? 3 : timeframe === "30m" ? 4 : 6;
	const assetRoc = roc(closes, lookback);
	const btcCloses = btc.map((c) => c.close);
	const vsBtcPct = assetRoc - (btcCloses.length >= lookback + 1 ? roc(btcCloses, lookback) : 0);
	const accel = assetRoc - roc(closes.slice(0, -1), Math.max(1, lookback - 1));
	const changePct = pctChange(last.open, last.close);
	if (strict) {
		if (!bullish) return null;
		if (volRatio < 1.35) return null;
		if (bodyRatio < .28 && closePos < .55) return null;
		if (upperWickRatio > .52) return null;
	}
	const late = rsiPrev !== null && rsiPrev >= 72 || change24h >= 22 || breakoutPct > 4.5;
	const volFactor = clamp((volRatio - 1.35) / 2.65, 0, 1);
	const breakoutFactor = last.close > priorHigh ? clamp(breakoutPct / 1.2, 0, 1) : clamp(1 - Math.abs(breakoutPct) / .45, 0, .35);
	const atrFactor = clamp((atrExpansion - 1.05) / 1.7, 0, 1);
	const quality = clamp(.45 * bodyRatio + .55 * closePos - Math.max(0, upperWickRatio - .22) * 1.8, 0, 1);
	const takerFactor = clamp((takerBuy - .5) / .16, 0, 1);
	const emaFactor = emaAligned ? 1 : last.close > (ema9 ?? last.close) ? .45 : .1;
	let rsiFactor = 0;
	if (rsi14 >= 54 && rsi14 <= 72) rsiFactor = 1;
	else if (rsi14 > 72 && rsi14 <= 78) rsiFactor = .55;
	else if (rsi14 > 78 && rsi14 < 84) rsiFactor = .2;
	else if (rsi14 >= 50 && rsi14 < 54) rsiFactor = .4;
	const vsBtcFactor = clamp((vsBtcPct + .15) / 1.4, 0, 1);
	const accelFactor = clamp(accel / 1.2, 0, 1);
	const factors = [
		{
			id: "volume",
			label: "Всплеск объёма",
			weight: 22,
			value: volFactor,
			note: `${volRatio.toFixed(2)}× к SMA20`
		},
		{
			id: "breakout",
			label: "Пробой Donchian 20",
			weight: 16,
			value: breakoutFactor,
			note: `${breakoutPct >= 0 ? "+" : ""}${breakoutPct.toFixed(2)}%`
		},
		{
			id: "atr",
			label: "Расширение ATR",
			weight: 12,
			value: atrFactor,
			note: `${atrExpansion.toFixed(2)}× ATR14`
		},
		{
			id: "candle",
			label: "Качество свечи",
			weight: 12,
			value: quality,
			note: `тело ${(bodyRatio * 100).toFixed(0)}% · закрытие ${(closePos * 100).toFixed(0)}%`
		},
		{
			id: "taker",
			label: "Агрессия тейкера",
			weight: 10,
			value: takerFactor,
			note: `${(takerBuy * 100).toFixed(0)}% market buy`
		},
		{
			id: "ema",
			label: "Тренд EMA 9/21",
			weight: 8,
			value: emaFactor,
			note: emaAligned ? "close > EMA9 > EMA21" : "нет выравнивания"
		},
		{
			id: "rsi",
			label: "Окно RSI 14",
			weight: 8,
			value: rsiFactor,
			note: rsi14.toFixed(1)
		},
		{
			id: "btc",
			label: "Сила vs BTC",
			weight: 7,
			value: vsBtcFactor,
			note: `${vsBtcPct >= 0 ? "+" : ""}${vsBtcPct.toFixed(2)}%`
		},
		{
			id: "accel",
			label: "Ускорение ROC",
			weight: 5,
			value: accelFactor,
			note: `${accel >= 0 ? "+" : ""}${accel.toFixed(2)} п.п.`
		}
	];
	const score = factors.reduce((s, f) => s + f.value * f.weight, 0);
	const grade = gradeFrom(score, late);
	if (strict && grade === "none") return null;
	const reasons = [];
	if (volRatio >= 2) reasons.push(`объём ${volRatio.toFixed(1)}× среднего`);
	if (last.close > priorHigh) reasons.push("пробой 20-барного максимума");
	if (emaAligned) reasons.push("EMA выровнены вверх");
	if (takerBuy >= .58) reasons.push("доминируют market buy");
	if (rsi14 >= 54 && rsi14 <= 72) reasons.push("RSI в раннем импульсе, не перекуплен");
	if (vsBtcPct >= .4) reasons.push("обгоняет BTC");
	if (atrExpansion >= 1.8) reasons.push("расширение волатильности");
	if (!bullish) reasons.push("последняя свеча ещё не бычья");
	const risks = [];
	if (late) risks.push("уже растянут — риск опоздавшего входа");
	if (rsi14 >= 78) risks.push("RSI высокий, возможна разгрузка");
	if (upperWickRatio > .28) risks.push("верхняя тень — продавцы защищаются");
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
		risks
	};
}
/**
* Multi-factor USDT pump score.
*
* Designed to catch *early confirmed impulses*, not already-extended FOMO.
* Scores the forming bar and the last closed bar, then keeps the stronger one.
*/
function analyzeTimeframe(timeframe, candles, btc, change24h) {
	const live = scoreWindow(timeframe, candles, btc, change24h, true, true);
	const closed = scoreWindow(timeframe, candles.slice(0, -1), btc, change24h, false, true);
	if (live && closed) return live.score >= closed.score ? live : closed;
	return live ?? closed;
}
function previewTimeframe(timeframe, candles, btc, change24h) {
	const live = scoreWindow(timeframe, candles, btc, change24h, true, false);
	const closed = scoreWindow(timeframe, candles.slice(0, -1), btc, change24h, false, false);
	if (live && closed) return live.score >= closed.score ? live : closed;
	return live ?? closed;
}
function pickBest(rows) {
	const rank = (g) => g === "strong" ? 3 : g === "watch" ? 2 : g === "late" ? 1 : 0;
	return [...rows].sort((a, b) => {
		const g = rank(b.grade) - rank(a.grade);
		if (g !== 0) return g;
		return b.score - a.score;
	})[0];
}
var STABLE_OR_FIAT = /* @__PURE__ */ new Set([
	"USDC",
	"FDUSD",
	"TUSD",
	"BUSD",
	"DAI",
	"EUR",
	"EURI",
	"AEUR",
	"TRY",
	"BRL",
	"ARS",
	"IDRT",
	"UAH",
	"NGN",
	"GBP",
	"AUD",
	"USDP",
	"USDS",
	"USD1",
	"PYUSD",
	"RLUSD",
	"BFUSD"
]);
var LEV_RE = /(UP|DOWN|BULL|BEAR)USDT$/;
function isUsdtSpot(symbol) {
	if (!symbol.endsWith("USDT")) return false;
	if (symbol.includes("_") || symbol.includes("USDUSDT")) return false;
	if (LEV_RE.test(symbol)) return false;
	const base = symbol.slice(0, -4);
	if (STABLE_OR_FIAT.has(base)) return false;
	if (base.length < 2) return false;
	return true;
}
function baseAsset(symbol) {
	return symbol.endsWith("USDT") ? symbol.slice(0, -4) : symbol;
}
var HOSTS = [
	"https://data-api.binance.vision",
	"https://api.binance.com",
	"https://api1.binance.com"
];
var FETCH_MS = 12e3;
var state = { host: HOSTS[0] };
async function fetchJson(path) {
	const ordered = [state.host, ...HOSTS.filter((h) => h !== state.host)];
	let lastErr = null;
	for (const host of ordered) try {
		const res = await fetch(`${host}${path}`, {
			headers: { accept: "application/json" },
			signal: AbortSignal.timeout(FETCH_MS)
		});
		if (!res.ok) {
			lastErr = /* @__PURE__ */ new Error(`Binance ${res.status}`);
			continue;
		}
		const data = await res.json();
		state.host = host;
		return {
			data,
			host
		};
	} catch (err) {
		lastErr = err instanceof Error ? err : new Error(String(err));
	}
	throw lastErr ?? /* @__PURE__ */ new Error("Binance unreachable");
}
function num(v) {
	const n = typeof v === "number" ? v : Number(v);
	return Number.isFinite(n) ? n : 0;
}
function parseTicker(raw) {
	return {
		symbol: String(raw.symbol ?? ""),
		lastPrice: num(raw.lastPrice),
		priceChangePercent: num(raw.priceChangePercent),
		highPrice: num(raw.highPrice),
		lowPrice: num(raw.lowPrice),
		quoteVolume: num(raw.quoteVolume),
		volume: num(raw.volume)
	};
}
function parseKline(row) {
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
		takerBuyBase: num(row[9])
	};
}
async function fetchTickers() {
	const { data, host } = await fetchJson("/api/v3/ticker/24hr");
	if (!Array.isArray(data)) throw new Error("Unexpected ticker payload");
	return {
		tickers: data.filter((row) => !!row && typeof row === "object").map(parseTicker).filter((t) => t.symbol),
		host
	};
}
async function fetchKlines(symbol, limit = 200) {
	const { data } = await fetchJson(`/api/v3/klines?symbol=${encodeURIComponent(symbol)}&interval=15m&limit=${limit}`);
	if (!Array.isArray(data)) return [];
	const candles = [];
	for (const row of data) {
		const c = parseKline(row);
		if (c) candles.push(c);
	}
	return candles;
}
async function mapPool(items, limit, fn) {
	const out = new Array(items.length);
	let cursor = 0;
	async function worker() {
		while (cursor < items.length) {
			const i = cursor++;
			out[i] = await fn(items[i]);
		}
	}
	const n = Math.min(limit, items.length);
	await Promise.all(Array.from({ length: n }, () => worker()));
	return out;
}
var klineCache = /* @__PURE__ */ new Map();
var KLINE_TTL = 2e4;
var tickerMemo = null;
var TICKER_TTL = 12e3;
async function tickersCached() {
	if (tickerMemo && Date.now() - tickerMemo.at < TICKER_TTL) return tickerMemo;
	const fresh = await fetchTickers();
	tickerMemo = {
		at: Date.now(),
		...fresh
	};
	return tickerMemo;
}
async function klinesCached(symbol) {
	const hit = klineCache.get(symbol);
	if (hit && Date.now() - hit.at < KLINE_TTL) return hit.candles;
	const candles = await fetchKlines(symbol, 200);
	klineCache.set(symbol, {
		at: Date.now(),
		candles
	});
	return candles;
}
function pickCandidates(tickers, input) {
	const liquid = tickers.filter((t) => isUsdtSpot(t.symbol) && t.quoteVolume >= input.minQuoteVolume && t.priceChangePercent <= input.alreadyPumpedMax);
	const watch = new Set(input.watchlist.map((s) => s.toUpperCase()));
	const byChange = [...liquid].sort((a, b) => b.priceChangePercent - a.priceChangePercent);
	const byHeat = [...liquid].sort((a, b) => {
		const ha = Math.log10(a.quoteVolume + 1) * Math.max(a.priceChangePercent, 0);
		return Math.log10(b.quoteVolume + 1) * Math.max(b.priceChangePercent, 0) - ha;
	});
	const byVol = [...liquid].sort((a, b) => b.quoteVolume - a.quoteVolume);
	const picked = /* @__PURE__ */ new Map();
	for (const t of liquid) if (watch.has(t.symbol)) picked.set(t.symbol, t);
	for (const t of byChange.slice(0, 16)) picked.set(t.symbol, t);
	for (const t of byHeat.slice(0, 16)) picked.set(t.symbol, t);
	for (const t of byVol.slice(0, 12)) picked.set(t.symbol, t);
	picked.delete("BTCUSDT");
	return [...picked.values()].slice(0, input.maxCandidates);
}
function tfCandles(raw15, tf) {
	if (tf === "15m") return raw15;
	return aggregateTimeframe(raw15, TF_MS[tf]);
}
async function runScan(partial = {}) {
	const input = {
		...DEFAULT_SCAN,
		...partial
	};
	const started = Date.now();
	try {
		const { tickers, host } = await tickersCached();
		const universe = tickers.filter((t) => isUsdtSpot(t.symbol));
		const btcTicker = tickers.find((t) => t.symbol === "BTCUSDT") ?? null;
		const movers = [...universe].sort((a, b) => b.priceChangePercent - a.priceChangePercent).slice(0, 14).map((t) => ({
			symbol: t.symbol,
			base: baseAsset(t.symbol),
			price: t.lastPrice,
			change24h: t.priceChangePercent,
			quoteVolume24h: t.quoteVolume
		}));
		const candidates = pickCandidates(tickers, input);
		const need = ["BTCUSDT", ...candidates.map((t) => t.symbol)];
		const klines = await mapPool([...new Set(need)], 6, async (symbol) => {
			try {
				return {
					symbol,
					candles: await klinesCached(symbol)
				};
			} catch {
				return {
					symbol,
					candles: []
				};
			}
		});
		const bySymbol = new Map(klines.map((k) => [k.symbol, k.candles]));
		const btc15 = bySymbol.get("BTCUSDT") ?? [];
		const signals = [];
		const closest = [];
		for (const t of candidates) {
			if (t.priceChangePercent > input.alreadyPumpedMax) continue;
			const raw15 = bySymbol.get(t.symbol) ?? [];
			if (raw15.length < 40) continue;
			const rows = [];
			const previews = [];
			for (const tf of input.timeframes) {
				const series = tfCandles(raw15, tf);
				const btcSeries = tfCandles(btc15, tf);
				const row = analyzeTimeframe(tf, series, btcSeries, t.priceChangePercent);
				if (row && row.score >= input.minScore) rows.push(row);
				const preview = previewTimeframe(tf, series, btcSeries, t.priceChangePercent);
				if (preview) previews.push(preview);
			}
			const btcRel = btcTicker ? t.priceChangePercent - btcTicker.priceChangePercent : 0;
			const toSignal = (best, pack, grade = best.grade) => {
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
					byTf: pack
				};
			};
			if (rows.length) {
				const sig = toSignal(pickBest(rows), rows);
				if (sig) signals.push(sig);
			} else if (previews.length) {
				const best = [...previews].sort((a, b) => b.score - a.score)[0];
				if (best.volumeRatio >= 1.15) closest.push({
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
					byTf: previews
				});
			}
		}
		signals.sort((a, b) => {
			const order = {
				strong: 0,
				watch: 1,
				late: 2
			};
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
			btc: btcTicker ? {
				price: btcTicker.lastPrice,
				change24h: btcTicker.priceChangePercent
			} : null,
			signals,
			closest: closest.slice(0, 8),
			movers
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
			movers: []
		};
	}
}
async function getSymbolKlines(symbol, tf) {
	return tfCandles(await klinesCached(symbol.toUpperCase()), tf);
}
var scanMarket_createServerFn_handler = createServerRpc({
	id: "90d8df1024e79a5766f0ef8152bc6586dad1af67cbb4fafe38f8ae34e5d3a9a4",
	name: "scanMarket",
	filename: "src/lib/server/pulse.ts"
}, (opts) => scanMarket.__executeServer(opts));
var scanMarket = createServerFn({ method: "POST" }).validator((data) => data).handler(scanMarket_createServerFn_handler, async ({ data }) => runScan(data ?? {}));
var fetchSymbolChart_createServerFn_handler = createServerRpc({
	id: "982cf8053b5280b37dbc7618e9b971c1062dd43e8955390265b7876235a5070a",
	name: "fetchSymbolChart",
	filename: "src/lib/server/pulse.ts"
}, (opts) => fetchSymbolChart.__executeServer(opts));
var fetchSymbolChart = createServerFn({ method: "POST" }).validator((data) => data).handler(fetchSymbolChart_createServerFn_handler, async ({ data }) => {
	return (await getSymbolKlines(data.symbol, data.timeframe)).map((c) => ({
		t: c.openTime,
		o: c.open,
		h: c.high,
		l: c.low,
		c: c.close,
		v: c.volume
	}));
});
var pingTelegram_createServerFn_handler = createServerRpc({
	id: "20a1501ea31ab31877d5af2cba4e0094eed54602f4bd1ba000514001de94eaf3",
	name: "pingTelegram",
	filename: "src/lib/server/pulse.ts"
}, (opts) => pingTelegram.__executeServer(opts));
var pingTelegram = createServerFn({ method: "POST" }).validator((data) => data).handler(pingTelegram_createServerFn_handler, async ({ data }) => sendTelegramMessage(data.token, data.chatId, data.text));
var findTelegramChat_createServerFn_handler = createServerRpc({
	id: "85885db196ec472c08bc2c35fa89baa86abe8eb5066abb08c37e5621af05da56",
	name: "findTelegramChat",
	filename: "src/lib/server/pulse.ts"
}, (opts) => findTelegramChat.__executeServer(opts));
var findTelegramChat = createServerFn({ method: "POST" }).validator((data) => data).handler(findTelegramChat_createServerFn_handler, async ({ data }) => detectTelegramChat(data.token));
//#endregion
export { fetchSymbolChart_createServerFn_handler, findTelegramChat_createServerFn_handler, pingTelegram_createServerFn_handler, scanMarket_createServerFn_handler };
