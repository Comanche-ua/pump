import { formatPct, formatPrice, formatUsd } from "@/lib/format";
import type { Mover } from "@/lib/pump/types";
import { cn } from "@/lib/utils";

export function MarketPanel({
  movers,
  onOpen,
}: {
  movers: Mover[];
  onOpen: (symbol: string) => void;
}) {
  return (
    <div className="flex flex-col gap-4">
      <header>
        <p className="text-xs uppercase tracking-widest text-subtle">Рынок</p>
        <h2 className="mt-1 text-2xl font-medium tracking-tight">Лидеры USDT за 24ч</h2>
        <p className="mt-2 text-sm text-muted">
          Это ещё не сигнал. Памп подтверждается объёмом, пробоем и RSI на 15м / 30м / 1ч.
        </p>
      </header>
      <ul className="flex flex-col gap-1">
        {movers.map((m) => (
          <li key={m.symbol}>
            <button
              type="button"
              onClick={() => onOpen(m.symbol)}
              className="flex h-14 w-full items-center justify-between gap-3 rounded-md px-3 text-left hover:bg-bg-subtle"
            >
              <span className="font-medium">
                {m.base}
                <span className="ml-1 text-xs text-subtle">USDT</span>
              </span>
              <span className="flex items-center gap-4 font-mono text-sm tabular-nums">
                <span className="text-muted">{formatPrice(m.price)}</span>
                <span className="hidden text-subtle sm:inline">{formatUsd(m.quoteVolume24h)}</span>
                <span className={cn(m.change24h >= 0 ? "text-pump" : "text-dump", "w-16 text-right")}>
                  {formatPct(m.change24h)}
                </span>
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
