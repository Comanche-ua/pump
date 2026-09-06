export function AlgoPanel() {
  return (
    <article className="flex max-w-2xl flex-col gap-6">
      <header>
        <p className="text-xs uppercase tracking-widest text-subtle">Алгоритм</p>
        <h2 className="mt-1 text-2xl font-medium tracking-tight">Как PULSE отличает памп от шума</h2>
        <p className="mt-2 text-sm text-muted">
          Только спот-пары к USDT. Кросс-курсы, стейблы и плечевые токены отброшены. Скан не
          торгует — только сигнал, чтобы не входить в уже растянутый ход.
        </p>
      </header>

      <ol className="flex flex-col gap-4">
        {STEPS.map((s, i) => (
          <li key={s.title} className="rounded-lg bg-bg-elevated p-4 shadow-[var(--shadow-border)]">
            <p className="font-mono text-xs tabular-nums text-subtle">{String(i + 1).padStart(2, "0")}</p>
            <h3 className="mt-1 font-medium">{s.title}</h3>
            <p className="mt-1 text-sm text-muted">{s.body}</p>
          </li>
        ))}
      </ol>
    </article>
  );
}

const STEPS = [
  {
    title: "Вселенная USDT",
    body: "Берём 24ч тикеры Binance, оставляем ликвидный спот к USDT (оборот от порога, по умолчанию $1.5M). Без USDC/FDUSD, без UP/DOWN.",
  },
  {
    title: "Кандидаты, не весь рынок",
    body: "Klines дороги. Сканируем топ по импульсу × объёму, плюс крупнейшие по обороту. BTC всегда рядом — для относительной силы.",
  },
  {
    title: "Один ряд 15м → 30м и 1ч",
    body: "Качаем 15-минутные свечи и агрегируем в 30м/1ч, чтобы три таймфрейма смотрели на одну и ту же ленту без рассинхрона.",
  },
  {
    title: "Жёсткие отсечки",
    body: "Красная свеча, объём < 1.35× SMA20, доджи, длинная верхняя тень, уже +35% за сутки — не сигнал. Лучше пропустить, чем ложный памп.",
  },
  {
    title: "Скоринг 0–100",
    body: "Веса: объём 22, пробой Donchian20 16, ATR 12, качество свечи 12, агрессия тейкера 10, EMA 8, окно RSI 8, vs BTC 7, ускорение ROC 5.",
  },
  {
    title: "Ранний импульс, не FOMO",
    body: "RSI 54–72 — окно входа. RSI > 80 или сильный ход за 24ч помечается ПОЗДНО. Климакс объёма (>6×) — предупреждение о конце волны.",
  },
  {
    title: "Классы сигнала",
    body: "ПАМП ≥ 74 без растяжения. СМОТРЕТЬ 60–73. ПОЗДНО — высокий скор, но уже протянут. В Telegram по умолчанию уходит только ПАМП.",
  },
];
