const STABLE_OR_FIAT = new Set([
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
  "BFUSD",
]);

const LEV_RE = /(UP|DOWN|BULL|BEAR)USDT$/;

export function isUsdtSpot(symbol: string) {
  if (!symbol.endsWith("USDT")) return false;
  if (symbol.includes("_") || symbol.includes("USDUSDT")) return false;
  if (LEV_RE.test(symbol)) return false;
  const base = symbol.slice(0, -4);
  if (STABLE_OR_FIAT.has(base)) return false;
  if (base.length < 2) return false;
  return true;
}

export function baseAsset(symbol: string) {
  return symbol.endsWith("USDT") ? symbol.slice(0, -4) : symbol;
}
