export type TelegramResult = { ok: boolean; error?: string; chatId?: string };

function api(token: string, method: string) {
  return `https://api.telegram.org/bot${encodeURIComponent(token)}/${method}`;
}

export async function sendTelegramMessage(
  token: string,
  chatId: string,
  text: string,
): Promise<TelegramResult> {
  const trimmed = token.trim();
  const chat = chatId.trim();
  if (!trimmed || !chat) return { ok: false, error: "Нужны token и chat ID" };
  try {
    const res = await fetch(api(trimmed, "sendMessage"), {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        chat_id: chat,
        text,
        parse_mode: "HTML",
        disable_web_page_preview: true,
      }),
      signal: AbortSignal.timeout(10_000),
    });
    const body = (await res.json()) as { ok?: boolean; description?: string };
    if (!body.ok) return { ok: false, error: body.description ?? `Telegram ${res.status}` };
    return { ok: true, chatId: chat };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "Telegram failed" };
  }
}

export async function detectTelegramChat(token: string): Promise<TelegramResult> {
  const trimmed = token.trim();
  if (!trimmed) return { ok: false, error: "Вставьте token бота" };
  try {
    const res = await fetch(api(trimmed, "getUpdates?limit=20"), {
      signal: AbortSignal.timeout(10_000),
    });
    const body = (await res.json()) as {
      ok?: boolean;
      description?: string;
      result?: Array<{
        message?: { chat?: { id?: number; title?: string; username?: string } };
      }>;
    };
    if (!body.ok) return { ok: false, error: body.description ?? "getUpdates failed" };
    const updates = body.result ?? [];
    for (let i = updates.length - 1; i >= 0; i--) {
      const id = updates[i]?.message?.chat?.id;
      if (id !== undefined) return { ok: true, chatId: String(id) };
    }
    return {
      ok: false,
      error: "Нет сообщений. Откройте бота в Telegram и напишите /start, затем повторите.",
    };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "Telegram failed" };
  }
}

export function formatAlertHtml(opts: {
  grade: string;
  base: string;
  symbol: string;
  tf: string;
  score: number;
  price: number;
  change24h: number;
  reasons: string[];
  risks: string[];
  volumeRatio: number;
  rsi: number;
  vsBtc: number;
}) {
  const title =
    opts.grade === "strong" ? "PUMP" : opts.grade === "late" ? "LATE" : "WATCH";
  const reasons = opts.reasons.slice(0, 4).map((r) => `• ${r}`).join("\n");
  const risks = opts.risks.length
    ? `\nРиски:\n${opts.risks.slice(0, 3).map((r) => `• ${r}`).join("\n")}`
    : "";
  return [
    `<b>PULSE · ${title}</b>  ${opts.base}/USDT`,
    `${opts.tf}  score ${opts.score.toFixed(0)}  vol ${opts.volumeRatio.toFixed(1)}×  RSI ${opts.rsi.toFixed(0)}`,
    `Цена <code>${opts.price}</code>   24ч ${opts.change24h >= 0 ? "+" : ""}${opts.change24h.toFixed(2)}%   vs BTC ${opts.vsBtc >= 0 ? "+" : ""}${opts.vsBtc.toFixed(2)}%`,
    reasons,
    risks,
  ]
    .filter(Boolean)
    .join("\n");
}
