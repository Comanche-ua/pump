/**
 * Headless scanner for GitHub Actions / cron.
 * TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.
 */
import { runScan } from "./lib/binance/run-scan.ts";
import { formatAlertHtml, sendTelegramMessage } from "./lib/telegram/send.ts";

const token = process.env.TELEGRAM_BOT_TOKEN ?? "";
const chatId = process.env.TELEGRAM_CHAT_ID ?? "";

if (!token || !chatId) {
  console.error("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID");
  process.exit(1);
}

const result = await runScan({
  timeframes: ["15m", "30m", "1h"],
  minScore: Number(process.env.PULSE_MIN_SCORE ?? 74),
  minQuoteVolume: Number(process.env.PULSE_MIN_VOLUME ?? 1_500_000),
});

if (!result.ok) {
  console.error(result.error);
  process.exit(1);
}

const pumps = result.signals.filter((s) => s.grade === "strong");
console.log(
  `universe=${result.universe} candidates=${result.candidates} strong=${pumps.length} ${result.durationMs}ms`,
);

for (const s of pumps) {
  const tf = s.byTf.find((t) => t.timeframe === s.bestTf) ?? s.byTf[0]!;
  const sent = await sendTelegramMessage(
    token,
    chatId,
    formatAlertHtml({
      grade: s.grade,
      base: s.base,
      symbol: s.symbol,
      tf: s.bestTf,
      score: s.bestScore,
      price: s.price,
      change24h: s.change24h,
      reasons: tf.reasons,
      risks: tf.risks,
      volumeRatio: tf.volumeRatio,
      rsi: tf.rsi,
      vsBtc: s.btcRelative24h,
    }),
  );
  console.log(s.symbol, sent.ok ? "sent" : sent.error);
}
