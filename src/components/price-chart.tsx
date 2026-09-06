import { useQuery } from "@tanstack/react-query";
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { fetchSymbolChart } from "@/lib/server/pulse";
import type { Timeframe } from "@/lib/pump/types";
import { formatPrice } from "@/lib/format";

export function PriceChart({ symbol, timeframe }: { symbol: string; timeframe: Timeframe }) {
  const q = useQuery({
    queryKey: ["klines", symbol, timeframe],
    queryFn: () => fetchSymbolChart({ data: { symbol, timeframe } }),
    staleTime: 20_000,
  });

  if (q.isLoading) {
    return <div className="h-48 animate-pulse rounded-md bg-bg-subtle" />;
  }
  if (!q.data?.length) {
    return <p className="text-sm text-muted">Нет свечей для {symbol}</p>;
  }

  const data = q.data.map((row) => ({
    t: new Date(row.t).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }),
    c: row.c,
  }));
  const up = (q.data.at(-1)?.c ?? 0) >= (q.data[0]?.c ?? 0);
  const stroke = up ? "var(--color-pump)" : "var(--color-dump)";

  return (
    <div className="h-48">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="pulseFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={stroke} stopOpacity={0.28} />
              <stop offset="100%" stopColor={stroke} stopOpacity={0} />
            </linearGradient>
          </defs>
          <XAxis dataKey="t" hide />
          <YAxis
            domain={["auto", "auto"]}
            width={64}
            tick={{ fill: "var(--color-subtle)", fontSize: 11, fontFamily: "var(--font-mono)" }}
            tickFormatter={(v: number) => formatPrice(v)}
            axisLine={false}
            tickLine={false}
          />
          <Tooltip
            contentStyle={{
              background: "var(--color-bg-elevated)",
              border: "1px solid var(--color-border)",
              borderRadius: 8,
              color: "var(--color-fg)",
              fontFamily: "var(--font-mono)",
              fontSize: 12,
            }}
            formatter={(v: number) => [formatPrice(v), "Цена"]}
          />
          <Area type="monotone" dataKey="c" stroke={stroke} fill="url(#pulseFill)" strokeWidth={1.6} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
