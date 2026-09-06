import { Activity } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { formatPct, formatPrice, formatUsd } from "@/lib/format";
import type { Grade, PumpSignal, Timeframe } from "@/lib/pump/types";
import { cn } from "@/lib/utils";

const GRADE_LABEL: Record<Grade, string> = {
  strong: "ПАМП",
  watch: "СМОТРЕТЬ",
  late: "ПОЗДНО",
};

export function SignalList({
  signals,
  closest,
  selected,
  onSelect,
  tfFilter,
}: {
  signals: PumpSignal[];
  closest: PumpSignal[];
  selected: string | null;
  onSelect: (symbol: string) => void;
  tfFilter: Timeframe | "all";
}) {
  const rows =
    tfFilter === "all"
      ? signals
      : signals.filter((s) => s.byTf.some((t) => t.timeframe === tfFilter));

  return (
    <div className="flex flex-col gap-4">
      {rows.length ? (
        <ul className="flex flex-col gap-2">
          {rows.map((s) => (
            <li key={s.symbol}>
              <SignalButton signal={s} active={selected === s.symbol} onSelect={onSelect} />
            </li>
          ))}
        </ul>
      ) : (
        <div className="flex flex-col items-center justify-center gap-3 px-4 py-8 text-center">
          <Activity className="size-6 text-subtle" />
          <p className="text-sm text-muted">
            Подтверждённого пампа нет. Фильтры отсекают тонкий объём, тени и уже растянутые ходы.
          </p>
        </div>
      )}
      {closest.length > 0 && (
        <div>
          <p className="mb-2 text-xs uppercase tracking-widest text-subtle">Ближайшие сетапы</p>
          <ul className="flex flex-col gap-2">
            {closest.map((s) => (
              <li key={s.symbol}>
                <SignalButton signal={s} active={selected === s.symbol} onSelect={onSelect} muted />
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function SignalButton({
  signal: s,
  active,
  onSelect,
  muted,
}: {
  signal: PumpSignal;
  active: boolean;
  onSelect: (symbol: string) => void;
  muted?: boolean;
}) {
  const tf = s.byTf.find((t) => t.timeframe === s.bestTf) ?? s.byTf[0]!;
  return (
    <button
      type="button"
      onClick={() => onSelect(s.symbol)}
      className={cn(
        "flex w-full flex-col gap-2 rounded-lg bg-bg-elevated px-4 py-3 text-left shadow-[var(--shadow-border)] transition-[box-shadow,background-color] duration-[var(--motion-quick)]",
        active && "bg-bg-subtle shadow-[var(--shadow-border-hover)]",
        muted && "opacity-80",
      )}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-baseline gap-2">
          <span className="font-medium tracking-tight">{s.base}</span>
          <span className="text-xs text-subtle">USDT</span>
        </div>
        {muted ? <Badge>рядом</Badge> : <Badge tone={s.grade}>{GRADE_LABEL[s.grade]}</Badge>}
      </div>
      <div className="flex items-end justify-between gap-3">
        <div>
          <p className="font-mono text-sm tabular-nums">{formatPrice(s.price)}</p>
          <p
            className={cn(
              "font-mono text-xs tabular-nums",
              s.change24h >= 0 ? "text-pump" : "text-dump",
            )}
          >
            24ч {formatPct(s.change24h)}
          </p>
        </div>
        <div className="text-right">
          <p className="font-mono text-sm tabular-nums text-fg">{s.bestScore.toFixed(0)}</p>
          <p className="text-xs text-subtle">
            {s.bestTf} · vol {tf.volumeRatio.toFixed(1)}×
          </p>
        </div>
      </div>
      <p className="text-xs text-muted">
        оборот {formatUsd(s.quoteVolume24h)} · vs BTC {formatPct(s.btcRelative24h, 1)}
      </p>
    </button>
  );
}
