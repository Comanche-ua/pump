import { PriceChart } from "@/components/price-chart";
import { Badge } from "@/components/ui/badge";
import { formatPct, formatPrice, formatUsd } from "@/lib/format";
import type { Grade, PumpSignal } from "@/lib/pump/types";
import { cn } from "@/lib/utils";

const GRADE_LABEL: Record<Grade, string> = {
  strong: "ПАМП",
  watch: "СМОТРЕТЬ",
  late: "ПОЗДНО",
};

export function SignalDetail({ signal }: { signal: PumpSignal }) {
  const best = signal.byTf.find((t) => t.timeframe === signal.bestTf) ?? signal.byTf[0]!;

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs uppercase tracking-widest text-subtle">Сигнал</p>
          <h2 className="text-2xl font-medium tracking-tight">
            {signal.base}
            <span className="text-muted">/USDT</span>
          </h2>
          <p className="mt-1 font-mono text-sm tabular-nums text-muted">
            {formatPrice(signal.price)} · 24ч {formatPct(signal.change24h)} · {formatUsd(signal.quoteVolume24h)}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Badge tone={signal.grade}>{GRADE_LABEL[signal.grade]}</Badge>
          <span className="font-mono text-lg tabular-nums">{signal.bestScore.toFixed(0)}</span>
        </div>
      </header>

      <PriceChart symbol={signal.symbol} timeframe={signal.bestTf} />

      <div className="grid grid-cols-3 gap-2">
        {signal.byTf.map((tf) => (
          <div key={tf.timeframe} className="rounded-md bg-bg-subtle px-3 py-2">
            <p className="text-xs text-subtle">{tf.timeframe}</p>
            <p className="font-mono text-sm tabular-nums">{tf.score.toFixed(0)}</p>
            <p className="text-xs text-muted">{formatPct(tf.changePct, 2)}</p>
          </div>
        ))}
      </div>

      <section>
        <h3 className="mb-2 text-sm font-medium">Почему это памп</h3>
        <ul className="flex flex-col gap-1.5">
          {best.reasons.map((r) => (
            <li key={r} className="text-sm text-fg">
              {r}
            </li>
          ))}
        </ul>
        {best.risks.length > 0 && (
          <ul className="mt-3 flex flex-col gap-1.5">
            {best.risks.map((r) => (
              <li key={r} className="text-sm text-late">
                {r}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section>
        <h3 className="mb-3 text-sm font-medium">Факторы · {best.timeframe}</h3>
        <ul className="flex flex-col gap-3">
          {best.factors.map((f) => (
            <li key={f.id}>
              <div className="mb-1 flex items-baseline justify-between gap-3">
                <span className="text-sm">{f.label}</span>
                <span className="font-mono text-xs tabular-nums text-muted">{f.note}</span>
              </div>
              <div className="h-1.5 overflow-hidden rounded-full bg-bg-subtle">
                <div
                  className={cn("h-full rounded-full bg-primary")}
                  style={{ width: `${Math.round(f.value * 100)}%` }}
                />
              </div>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
