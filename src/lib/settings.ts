import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { Timeframe } from "./pump/types";

export type AppTab = "signals" | "market" | "algo" | "bot";

type Persisted = {
  botToken: string;
  chatId: string;
  autoNotify: boolean;
  notifyWatch: boolean;
  notifyLate: boolean;
  minScore: number;
  minQuoteVolume: number;
  alreadyPumpedMax: number;
  intervalSec: number;
  timeframes: Timeframe[];
};

type SettingsState = Persisted & {
  hydrated: boolean;
  patch: (p: Partial<Persisted>) => void;
};

const defaults: Persisted = {
  botToken: "",
  chatId: "",
  autoNotify: true,
  notifyWatch: false,
  notifyLate: false,
  minScore: 62,
  minQuoteVolume: 1_500_000,
  alreadyPumpedMax: 35,
  intervalSec: 45,
  timeframes: ["15m", "30m", "1h"],
};

export const useSettings = create<SettingsState>()(
  persist(
    (set) => ({
      ...defaults,
      hydrated: false,
      patch: (p) => set(p),
    }),
    {
      name: "pulse-usdt-settings",
      partialize: (s) => ({
        botToken: s.botToken,
        chatId: s.chatId,
        autoNotify: s.autoNotify,
        notifyWatch: s.notifyWatch,
        notifyLate: s.notifyLate,
        minScore: s.minScore,
        minQuoteVolume: s.minQuoteVolume,
        alreadyPumpedMax: s.alreadyPumpedMax,
        intervalSec: s.intervalSec,
        timeframes: s.timeframes,
      }),
      onRehydrateStorage: () => () => {
        useSettings.setState({ hydrated: true });
      },
    },
  ),
);
