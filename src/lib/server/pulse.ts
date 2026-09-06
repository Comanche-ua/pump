import { createServerFn } from "@tanstack/react-start";
import { getSymbolKlines, runScan } from "../binance/run-scan.ts";
import type { ScanInput, Timeframe } from "../pump/types.ts";
import { detectTelegramChat, sendTelegramMessage } from "../telegram/send.ts";

export const scanMarket = createServerFn({ method: "POST" })
  .validator((data: Partial<ScanInput>) => data)
  .handler(async ({ data }) => runScan(data ?? {}));

export const fetchSymbolChart = createServerFn({ method: "POST" })
  .validator((data: { symbol: string; timeframe: Timeframe }) => data)
  .handler(async ({ data }) => {
    const candles = await getSymbolKlines(data.symbol, data.timeframe);
    return candles.map((c) => ({
      t: c.openTime,
      o: c.open,
      h: c.high,
      l: c.low,
      c: c.close,
      v: c.volume,
    }));
  });

export const pingTelegram = createServerFn({ method: "POST" })
  .validator((data: { token: string; chatId: string; text: string }) => data)
  .handler(async ({ data }) => sendTelegramMessage(data.token, data.chatId, data.text));

export const findTelegramChat = createServerFn({ method: "POST" })
  .validator((data: { token: string }) => data)
  .handler(async ({ data }) => detectTelegramChat(data.token));
