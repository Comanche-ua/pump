import { useQuery } from "@tanstack/react-query";
import { Activity, Bot, Radar, RefreshCw, TrendingUp } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { AlgoPanel } from "@/components/algo-panel";
import { BotPanel } from "@/components/bot-panel";
import { MarketPanel } from "@/components/market-panel";
import { PulseMark } from "@/components/pulse-mark";
import { SignalDetail } from "@/components/signal-detail";
import { SignalList } from "@/components/signal-list";
import { Button } from "@/components/ui/button";
import { formatAgo, formatPct, formatPrice } from "@/lib/format";
import type { AppTab } from "@/lib/settings";
import { useSettings } from "@/lib/settings";
import { pingTelegram, scanMarket } from "@/lib/server/pulse";
import { TIMEFRAMES, type PumpSignal, type Timeframe } from "@/lib/pump/types";
import { formatAlertHtml } from "@/lib/telegram/send";
import { cn } from "@/lib/utils";

const SENT_KEY = "pulse-sent-alerts";

function loadSent(): Set<string> {
  try {
    const raw = localStorage.getItem(SENT_KEY);
    const arr = raw ? (JSON.parse(raw) as string[]) : [];
    return new Set(arr.slice(-400));
  } catch {
    return new Set();
  }
}

function saveSent(set: Set<string>) {
  localStorage.setItem(SENT_KEY, JSON.stringify([...set].slice(-400)));
}

export function Dashboard() {
  const settings = useSettings();
  const [tab, setTab] = useState<AppTab>("signals");
  const [tfFilter, setTfFilter] = useState<Timeframe | "all">("all");
  const [selected, setSelected] = useState<string | null>(null);
  const primed = useRef(false);

  useEffect(() => {
    const tg = (window as unknown as { Telegram?: { WebApp?: { ready: () => void; expand: () => void } } })
      .Telegram?.WebApp;
    tg?.ready();
    tg?.expand();
  }, []);

  const scan = useQuery({
    queryKey: [
      "scan",
      settings.minScore,
      settings.minQuoteVolume,
      settings.alreadyPumpedMax,
      settings.timeframes.join(","),
    ],
    queryFn: () =>
      scanMarket({
        data: {
          minScore: settings.minScore,
          minQuoteVolume: settings.minQuoteVolume,
          alreadyPumpedMax: settings.alreadyPumpedMax,
          timeframes: settings.timeframes,
        },
      }),
    refetchInterval: settings.intervalSec * 1000,
    staleTime: 10_000,
  });

  useEffect(() => {
    if (!scan.data?.ok || !settings.autoNotify || !settings.botToken || !settings.chatId) {
      if (scan.data?.ok) primed.current = true;
      return;
    }
    const sent = loadSent();
    const fresh: PumpSignal[] = [];
    for (const s of scan.data.signals) {
      if (sent.has(s.alertKey)) continue;
      if (s.grade === "watch" && !settings.notifyWatch) continue;
      if (s.grade === "late" && !settings.notifyLate) continue;
      if (s.grade === "strong" || settings.notifyWatch || settings.notifyLate) {
        if (s.grade === "strong" || (s.grade === "watch" && settings.notifyWatch) || (s.grade === "late" && settings.notifyLate)) {
          fresh.push(s);
        }
      }
    }
    if (!primed.current) {
      for (const s of fresh) sent.add(s.alertKey);
      saveSent(sent);
      primed.current = true;
      return;
    }
    void (async () => {
      for (const s of fresh) {
        const tf = s.byTf.find((t) => t.timeframe === s.bestTf) ?? s.byTf[0]!;
        const res = await pingTelegram({
          data: {
            token: settings.botToken,
            chatId: settings.chatId,
            text: formatAlertHtml({
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
          },
        });
        sent.add(s.alertKey);
        if (res.ok) toast.success(`${s.base} · алерт ушёл`);
      }
      saveSent(sent);
    })();
  }, [
    scan.dataUpdatedAt,
    scan.data,
    settings.autoNotify,
    settings.botToken,
    settings.chatId,
    settings.notifyWatch,
    settings.notifyLate,
  ]);

  const signals = scan.data?.signals ?? [];
  const closest = scan.data?.closest ?? [];
  const active = useMemo(() => {
    const pool = [...signals, ...closest];
    return pool.find((s) => s.symbol === selected) ?? pool[0] ?? null;
  }, [signals, closest, selected]);

  useEffect(() => {
    if (!selected) {
      const first = signals[0] ?? closest[0];
      if (first) setSelected(first.symbol);
    }
  }, [selected, signals, closest]);

  const strongN = signals.filter((s) => s.grade === "strong").length;

  return (
    <div className="flex min-h-dvh flex-col bg-bg text-fg">
      <header className="sticky top-0 z-20 border-b border-border bg-bg/95 px-4 py-3 backdrop-blur-sm">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <PulseMark className="size-8" />
            <div>
              <p className="text-sm font-medium tracking-tight">PULSE</p>
              <p className="text-xs text-subtle">USDT · Binance</p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            {scan.data?.btc && (
              <p className="hidden font-mono text-xs tabular-nums text-muted sm:block">
                BTC {formatPrice(scan.data.btc.price)}{" "}
                <span className={scan.data.btc.change24h >= 0 ? "text-pump" : "text-dump"}>
                  {formatPct(scan.data.btc.change24h, 1)}
                </span>
              </p>
            )}
            <nav className="hidden items-center gap-1 lg:flex">
              {TABS.map((t) => (
                <Button
                  key={t.id}
                  variant={tab === t.id ? "default" : "ghost"}
                  size="sm"
                  onClick={() => setTab(t.id)}
                >
                  {t.label}
                </Button>
              ))}
            </nav>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void scan.refetch()}
              disabled={scan.isFetching}
            >
              <RefreshCw className={cn("size-3.5", scan.isFetching && "animate-spin")} />
              {scan.isFetching ? "Скан" : "Обновить"}
            </Button>
          </div>
        </div>
      </header>

      <main className="mx-auto flex w-full max-w-6xl flex-1 flex-col gap-4 px-4 py-4 pb-24 lg:pb-6">
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
          <span className={cn("size-1.5 rounded-full", scan.isError || scan.data?.ok === false ? "bg-dump" : "bg-pump")} />
          {scan.data?.ok === false
            ? scan.data.error
            : scan.isLoading
              ? "Первый скан рынка…"
              : `${scan.data?.universe ?? 0} пар USDT · ${scan.data?.candidates ?? 0} кандидатов · ${strongN} памп`}
          {scan.data?.scannedAt ? <span>· {formatAgo(scan.data.scannedAt)}</span> : null}
        </div>

        {tab === "signals" && (
          <div className="flex gap-2">
            {(["all", ...TIMEFRAMES] as const).map((tf) => (
              <button
                key={tf}
                type="button"
                onClick={() => setTfFilter(tf)}
                className={cn(
                  "h-11 rounded-md px-3 text-sm",
                  tfFilter === tf
                    ? "bg-primary text-primary-fg"
                    : "text-muted shadow-[var(--shadow-border)]",
                )}
              >
                {tf === "all" ? "все ТФ" : tf}
              </button>
            ))}
          </div>
        )}

        {tab === "signals" && (
          <div className="grid gap-6 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
            <section>
              {scan.isLoading ? (
                <div className="flex flex-col gap-2">
                  {Array.from({ length: 5 }).map((_, i) => (
                    <div key={i} className="h-24 animate-pulse rounded-lg bg-bg-elevated" />
                  ))}
                </div>
              ) : (
                <SignalList
                  signals={signals}
                  closest={closest}
                  selected={active?.symbol ?? null}
                  onSelect={setSelected}
                  tfFilter={tfFilter}
                />
              )}
            </section>
            <section className="hidden rounded-xl bg-bg-elevated p-5 shadow-[var(--shadow-border)] lg:block">
              {active ? (
                <SignalDetail signal={active} />
              ) : (
                <p className="text-sm text-muted">Выберите сигнал, чтобы разобрать факторы.</p>
              )}
            </section>
            {active && (
              <section className="rounded-xl bg-bg-elevated p-5 shadow-[var(--shadow-border)] lg:hidden">
                <SignalDetail signal={active} />
              </section>
            )}
          </div>
        )}

        {tab === "market" && (
          <MarketPanel
            movers={scan.data?.movers ?? []}
            onOpen={(symbol) => {
              setSelected(symbol);
              setTab("signals");
            }}
          />
        )}
        {tab === "algo" && <AlgoPanel />}
        {tab === "bot" && <BotPanel />}
      </main>

      <nav className="fixed inset-x-0 bottom-0 z-20 border-t border-border bg-bg/95 pb-[env(safe-area-inset-bottom)] backdrop-blur-sm lg:hidden">
        <ul className="mx-auto grid max-w-6xl grid-cols-4">
          {TABS.map((t) => (
            <li key={t.id}>
              <button
                type="button"
                onClick={() => setTab(t.id)}
                className={cn(
                  "flex h-14 w-full flex-col items-center justify-center gap-1 text-xs",
                  tab === t.id ? "text-fg" : "text-subtle",
                )}
              >
                <t.icon className="size-4" />
                {t.label}
              </button>
            </li>
          ))}
        </ul>
      </nav>
    </div>
  );
}

const TABS: { id: AppTab; label: string; icon: typeof Radar }[] = [
  { id: "signals", label: "Сигналы", icon: Radar },
  { id: "market", label: "Рынок", icon: TrendingUp },
  { id: "algo", label: "Алгоритм", icon: Activity },
  { id: "bot", label: "Бот", icon: Bot },
];
