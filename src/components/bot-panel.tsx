import { useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { findTelegramChat, pingTelegram } from "@/lib/server/pulse";
import { useSettings } from "@/lib/settings";
import { TIMEFRAMES, type Timeframe } from "@/lib/pump/types";

export function BotPanel() {
  const s = useSettings();
  const [busy, setBusy] = useState<"test" | "chat" | null>(null);

  async function onTest() {
    setBusy("test");
    const res = await pingTelegram({
      data: {
        token: s.botToken,
        chatId: s.chatId,
        text: "<b>PULSE</b> подключён. Алерты по USDT-пампам будут приходить сюда.",
      },
    });
    setBusy(null);
    if (res.ok) toast.success("Тестовое сообщение отправлено");
    else toast.error(res.error ?? "Ошибка Telegram");
  }

  async function onChat() {
    setBusy("chat");
    const res = await findTelegramChat({ data: { token: s.botToken } });
    setBusy(null);
    if (res.ok && res.chatId) {
      s.patch({ chatId: res.chatId });
      toast.success(`Chat ID: ${res.chatId}`);
    } else toast.error(res.error ?? "Не найден чат");
  }

  function toggleTf(tf: Timeframe) {
    const has = s.timeframes.includes(tf);
    const next = has ? s.timeframes.filter((t) => t !== tf) : [...s.timeframes, tf];
    s.patch({ timeframes: next.length ? next : [tf] });
  }

  return (
    <div className="flex max-w-xl flex-col gap-8">
      <header>
        <p className="text-xs uppercase tracking-widest text-subtle">Telegram-бот</p>
        <h2 className="mt-1 text-2xl font-medium tracking-tight">Уведомления о пампе</h2>
        <p className="mt-2 text-sm text-muted">
          Создайте бота у @BotFather, вставьте token. Напишите боту /start, затем подтяните chat
          ID. Token хранится только в этом браузере.
        </p>
      </header>

      <section className="flex flex-col gap-3">
        <label className="text-xs text-muted" htmlFor="token">
          Bot token
        </label>
        <Input
          id="token"
          type="password"
          autoComplete="off"
          placeholder="123456:ABC…"
          value={s.botToken}
          onChange={(e) => s.patch({ botToken: e.target.value })}
        />
        <label className="text-xs text-muted" htmlFor="chat">
          Chat ID
        </label>
        <div className="flex gap-2">
          <Input
            id="chat"
            placeholder="-100… или ваш id"
            value={s.chatId}
            onChange={(e) => s.patch({ chatId: e.target.value })}
          />
          <Button variant="outline" onClick={onChat} disabled={!s.botToken || busy !== null}>
            {busy === "chat" ? "…" : "Найти"}
          </Button>
        </div>
        <Button onClick={onTest} disabled={!s.botToken || !s.chatId || busy !== null}>
          {busy === "test" ? "Отправка…" : "Тестовый ping"}
        </Button>
      </section>

      <section className="flex flex-col gap-4">
        <Row
          label="Автоалерты"
          hint="Новый ПАМП уходит в Telegram"
          checked={s.autoNotify}
          onChange={(v) => s.patch({ autoNotify: v })}
        />
        <Row
          label="Смотреть"
          hint="Присылать средний класс"
          checked={s.notifyWatch}
          onChange={(v) => s.patch({ notifyWatch: v })}
        />
        <Row
          label="Поздно"
          hint="FOMO-предупреждения"
          checked={s.notifyLate}
          onChange={(v) => s.patch({ notifyLate: v })}
        />
      </section>

      <section className="flex flex-col gap-4">
        <p className="text-sm font-medium">Таймфреймы</p>
        <div className="flex gap-2">
          {TIMEFRAMES.map((tf) => {
            const on = s.timeframes.includes(tf);
            return (
              <button
                key={tf}
                type="button"
                onClick={() => toggleTf(tf)}
                className={
                  on
                    ? "h-11 rounded-md bg-primary px-4 text-sm text-primary-fg"
                    : "h-11 rounded-md px-4 text-sm text-muted shadow-[var(--shadow-border)]"
                }
              >
                {tf}
              </button>
            );
          })}
        </div>
        <Field
          label="Мин. score"
          value={s.minScore}
          min={50}
          max={90}
          step={1}
          onChange={(v) => s.patch({ minScore: v })}
        />
        <Field
          label="Мин. оборот 24ч, USDT млн"
          value={Math.round(s.minQuoteVolume / 1_000_000)}
          min={0.5}
          max={50}
          step={0.5}
          onChange={(v) => s.patch({ minQuoteVolume: v * 1_000_000 })}
        />
        <Field
          label="Уже прокачан, % за 24ч — отсечка"
          value={s.alreadyPumpedMax}
          min={10}
          max={80}
          step={1}
          onChange={(v) => s.patch({ alreadyPumpedMax: v })}
        />
        <Field
          label="Интервал скана, сек"
          value={s.intervalSec}
          min={20}
          max={180}
          step={5}
          onChange={(v) => s.patch({ intervalSec: v })}
        />
      </section>
    </div>
  );
}

function Row({
  label,
  hint,
  checked,
  onChange,
}: {
  label: string;
  hint: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div>
        <p className="text-sm">{label}</p>
        <p className="text-xs text-muted">{hint}</p>
      </div>
      <Switch checked={checked} onCheckedChange={onChange} />
    </div>
  );
}

function Field({
  label,
  value,
  min,
  max,
  step,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (v: number) => void;
}) {
  return (
    <label className="flex flex-col gap-2 text-sm">
      <span className="flex justify-between text-muted">
        {label}
        <span className="font-mono tabular-nums text-fg">{value}</span>
      </span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full accent-primary"
      />
    </label>
  );
}
