import { i as __toESM } from "../_runtime.mjs";
import { n as require_react } from "../_libs/@radix-ui/react-compose-refs+[...].mjs";
import { n as require_jsx_runtime } from "../_libs/radix-ui__react-context+react.mjs";
import { n as Slot } from "../_libs/@radix-ui/react-primitive+[...].mjs";
import { n as TSS_SERVER_FUNCTION, r as getServerFnById, t as createServerFn } from "./ssr.mjs";
import { i as formatAlertHtml, n as TIMEFRAMES } from "./send-Dx-1cmTz.mjs";
import { a as Bot, i as Radar, n as TrendingUp, o as Activity, r as RefreshCw } from "../_libs/lucide-react.mjs";
import { t as useQuery } from "../_libs/tanstack__react-query.mjs";
import { n as toast } from "../_libs/sonner.mjs";
import { n as clsx, t as cva } from "../_libs/class-variance-authority+clsx.mjs";
import { t as twMerge } from "../_libs/tailwind-merge.mjs";
import { n as SwitchThumb, t as Switch$1 } from "../_libs/@radix-ui/react-switch+[...].mjs";
import { n as create, t as persist } from "../_libs/zustand.mjs";
import { a as ResponsiveContainer, i as Area, n as YAxis, o as Tooltip, r as XAxis, t as AreaChart } from "../_libs/recharts+[...].mjs";
//#region node_modules/.nitro/vite/services/ssr/assets/routes-BhP9vTFk.js
var import_react = /* @__PURE__ */ __toESM(require_react());
var import_jsx_runtime = require_jsx_runtime();
function AlgoPanel() {
	return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("article", {
		className: "flex max-w-2xl flex-col gap-6",
		children: [/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("header", { children: [
			/* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", {
				className: "text-xs uppercase tracking-widest text-subtle",
				children: "Алгоритм"
			}),
			/* @__PURE__ */ (0, import_jsx_runtime.jsx)("h2", {
				className: "mt-1 text-2xl font-medium tracking-tight",
				children: "Как PULSE отличает памп от шума"
			}),
			/* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", {
				className: "mt-2 text-sm text-muted",
				children: "Только спот-пары к USDT. Кросс-курсы, стейблы и плечевые токены отброшены. Скан не торгует — только сигнал, чтобы не входить в уже растянутый ход."
			})
		] }), /* @__PURE__ */ (0, import_jsx_runtime.jsx)("ol", {
			className: "flex flex-col gap-4",
			children: STEPS.map((s, i) => /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("li", {
				className: "rounded-lg bg-bg-elevated p-4 shadow-[var(--shadow-border)]",
				children: [
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", {
						className: "font-mono text-xs tabular-nums text-subtle",
						children: String(i + 1).padStart(2, "0")
					}),
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)("h3", {
						className: "mt-1 font-medium",
						children: s.title
					}),
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", {
						className: "mt-1 text-sm text-muted",
						children: s.body
					})
				]
			}, s.title))
		})]
	});
}
var STEPS = [
	{
		title: "Вселенная USDT",
		body: "Берём 24ч тикеры Binance, оставляем ликвидный спот к USDT (оборот от порога, по умолчанию $1.5M). Без USDC/FDUSD, без UP/DOWN."
	},
	{
		title: "Кандидаты, не весь рынок",
		body: "Klines дороги. Сканируем топ по импульсу × объёму, плюс крупнейшие по обороту. BTC всегда рядом — для относительной силы."
	},
	{
		title: "Один ряд 15м → 30м и 1ч",
		body: "Качаем 15-минутные свечи и агрегируем в 30м/1ч, чтобы три таймфрейма смотрели на одну и ту же ленту без рассинхрона."
	},
	{
		title: "Жёсткие отсечки",
		body: "Красная свеча, объём < 1.35× SMA20, доджи, длинная верхняя тень, уже +35% за сутки — не сигнал. Лучше пропустить, чем ложный памп."
	},
	{
		title: "Скоринг 0–100",
		body: "Веса: объём 22, пробой Donchian20 16, ATR 12, качество свечи 12, агрессия тейкера 10, EMA 8, окно RSI 8, vs BTC 7, ускорение ROC 5."
	},
	{
		title: "Ранний импульс, не FOMO",
		body: "RSI 54–72 — окно входа. RSI > 80 или сильный ход за 24ч помечается ПОЗДНО. Климакс объёма (>6×) — предупреждение о конце волны."
	},
	{
		title: "Классы сигнала",
		body: "ПАМП ≥ 74 без растяжения. СМОТРЕТЬ 60–73. ПОЗДНО — высокий скор, но уже протянут. В Telegram по умолчанию уходит только ПАМП."
	}
];
function cn(...inputs) {
	return twMerge(clsx(inputs));
}
var buttonVariants = cva("inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium transition-[opacity,transform,background-color,box-shadow] duration-[var(--motion-quick)] ease-[var(--ease-out)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-40 active:scale-[0.98]", {
	variants: {
		variant: {
			default: "bg-primary text-primary-fg hover:opacity-90",
			ghost: "bg-transparent text-fg hover:bg-bg-subtle",
			outline: "bg-transparent text-fg shadow-[var(--shadow-border)] hover:shadow-[var(--shadow-border-hover)]",
			pump: "bg-pump text-bg hover:opacity-90"
		},
		size: {
			default: "h-11 px-4",
			sm: "h-9 px-3 text-xs",
			icon: "size-11"
		}
	},
	defaultVariants: {
		variant: "default",
		size: "default"
	}
});
function Button({ className, variant, size, asChild = false, ...props }) {
	return /* @__PURE__ */ (0, import_jsx_runtime.jsx)(asChild ? Slot : "button", {
		className: cn(buttonVariants({
			variant,
			size
		}), className),
		...props
	});
}
function Input({ className, ...props }) {
	return /* @__PURE__ */ (0, import_jsx_runtime.jsx)("input", {
		className: cn("h-11 w-full rounded-md bg-bg-subtle px-3 text-sm text-fg shadow-[var(--shadow-border)] outline-none placeholder:text-subtle focus-visible:ring-2 focus-visible:ring-ring", className),
		...props
	});
}
function Switch({ className, ...props }) {
	return /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Switch$1, {
		className: cn("peer inline-flex h-6 w-11 shrink-0 cursor-pointer items-center rounded-full bg-bg-subtle shadow-[var(--shadow-border)] transition-colors data-[state=checked]:bg-pump", className),
		...props,
		children: /* @__PURE__ */ (0, import_jsx_runtime.jsx)(SwitchThumb, { className: "pointer-events-none block size-5 translate-x-0.5 rounded-full bg-fg transition-transform data-[state=checked]:translate-x-5 data-[state=checked]:bg-bg" })
	});
}
var createSsrRpc = (functionId) => {
	const url = "/_serverFn/" + functionId;
	const serverFnMeta = { id: functionId };
	const fn = async (...args) => {
		return (await getServerFnById(functionId, { origin: "server" }))(...args);
	};
	return Object.assign(fn, {
		url,
		serverFnMeta,
		[TSS_SERVER_FUNCTION]: true
	});
};
var scanMarket = createServerFn({ method: "POST" }).validator((data) => data).handler(createSsrRpc("90d8df1024e79a5766f0ef8152bc6586dad1af67cbb4fafe38f8ae34e5d3a9a4"));
var fetchSymbolChart = createServerFn({ method: "POST" }).validator((data) => data).handler(createSsrRpc("982cf8053b5280b37dbc7618e9b971c1062dd43e8955390265b7876235a5070a"));
var pingTelegram = createServerFn({ method: "POST" }).validator((data) => data).handler(createSsrRpc("20a1501ea31ab31877d5af2cba4e0094eed54602f4bd1ba000514001de94eaf3"));
var findTelegramChat = createServerFn({ method: "POST" }).validator((data) => data).handler(createSsrRpc("85885db196ec472c08bc2c35fa89baa86abe8eb5066abb08c37e5621af05da56"));
var defaults = {
	botToken: "",
	chatId: "",
	autoNotify: true,
	notifyWatch: false,
	notifyLate: false,
	minScore: 62,
	minQuoteVolume: 15e5,
	alreadyPumpedMax: 35,
	intervalSec: 45,
	timeframes: [
		"15m",
		"30m",
		"1h"
	]
};
var useSettings = create()(persist((set) => ({
	...defaults,
	hydrated: false,
	patch: (p) => set(p)
}), {
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
		timeframes: s.timeframes
	}),
	onRehydrateStorage: () => () => {
		useSettings.setState({ hydrated: true });
	}
}));
function BotPanel() {
	const s = useSettings();
	const [busy, setBusy] = (0, import_react.useState)(null);
	async function onTest() {
		setBusy("test");
		const res = await pingTelegram({ data: {
			token: s.botToken,
			chatId: s.chatId,
			text: "<b>PULSE</b> подключён. Алерты по USDT-пампам будут приходить сюда."
		} });
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
	function toggleTf(tf) {
		const next = s.timeframes.includes(tf) ? s.timeframes.filter((t) => t !== tf) : [...s.timeframes, tf];
		s.patch({ timeframes: next.length ? next : [tf] });
	}
	return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
		className: "flex max-w-xl flex-col gap-8",
		children: [
			/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("header", { children: [
				/* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", {
					className: "text-xs uppercase tracking-widest text-subtle",
					children: "Telegram-бот"
				}),
				/* @__PURE__ */ (0, import_jsx_runtime.jsx)("h2", {
					className: "mt-1 text-2xl font-medium tracking-tight",
					children: "Уведомления о пампе"
				}),
				/* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", {
					className: "mt-2 text-sm text-muted",
					children: "Создайте бота у @BotFather, вставьте token. Напишите боту /start, затем подтяните chat ID. Token хранится только в этом браузере."
				})
			] }),
			/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("section", {
				className: "flex flex-col gap-3",
				children: [
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)("label", {
						className: "text-xs text-muted",
						htmlFor: "token",
						children: "Bot token"
					}),
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)(Input, {
						id: "token",
						type: "password",
						autoComplete: "off",
						placeholder: "123456:ABC…",
						value: s.botToken,
						onChange: (e) => s.patch({ botToken: e.target.value })
					}),
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)("label", {
						className: "text-xs text-muted",
						htmlFor: "chat",
						children: "Chat ID"
					}),
					/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
						className: "flex gap-2",
						children: [/* @__PURE__ */ (0, import_jsx_runtime.jsx)(Input, {
							id: "chat",
							placeholder: "-100… или ваш id",
							value: s.chatId,
							onChange: (e) => s.patch({ chatId: e.target.value })
						}), /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Button, {
							variant: "outline",
							onClick: onChat,
							disabled: !s.botToken || busy !== null,
							children: busy === "chat" ? "…" : "Найти"
						})]
					}),
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)(Button, {
						onClick: onTest,
						disabled: !s.botToken || !s.chatId || busy !== null,
						children: busy === "test" ? "Отправка…" : "Тестовый ping"
					})
				]
			}),
			/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("section", {
				className: "flex flex-col gap-4",
				children: [
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)(Row, {
						label: "Автоалерты",
						hint: "Новый ПАМП уходит в Telegram",
						checked: s.autoNotify,
						onChange: (v) => s.patch({ autoNotify: v })
					}),
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)(Row, {
						label: "Смотреть",
						hint: "Присылать средний класс",
						checked: s.notifyWatch,
						onChange: (v) => s.patch({ notifyWatch: v })
					}),
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)(Row, {
						label: "Поздно",
						hint: "FOMO-предупреждения",
						checked: s.notifyLate,
						onChange: (v) => s.patch({ notifyLate: v })
					})
				]
			}),
			/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("section", {
				className: "flex flex-col gap-4",
				children: [
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", {
						className: "text-sm font-medium",
						children: "Таймфреймы"
					}),
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", {
						className: "flex gap-2",
						children: TIMEFRAMES.map((tf) => {
							const on = s.timeframes.includes(tf);
							return /* @__PURE__ */ (0, import_jsx_runtime.jsx)("button", {
								type: "button",
								onClick: () => toggleTf(tf),
								className: on ? "h-11 rounded-md bg-primary px-4 text-sm text-primary-fg" : "h-11 rounded-md px-4 text-sm text-muted shadow-[var(--shadow-border)]",
								children: tf
							}, tf);
						})
					}),
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)(Field, {
						label: "Мин. score",
						value: s.minScore,
						min: 50,
						max: 90,
						step: 1,
						onChange: (v) => s.patch({ minScore: v })
					}),
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)(Field, {
						label: "Мин. оборот 24ч, USDT млн",
						value: Math.round(s.minQuoteVolume / 1e6),
						min: .5,
						max: 50,
						step: .5,
						onChange: (v) => s.patch({ minQuoteVolume: v * 1e6 })
					}),
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)(Field, {
						label: "Уже прокачан, % за 24ч — отсечка",
						value: s.alreadyPumpedMax,
						min: 10,
						max: 80,
						step: 1,
						onChange: (v) => s.patch({ alreadyPumpedMax: v })
					}),
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)(Field, {
						label: "Интервал скана, сек",
						value: s.intervalSec,
						min: 20,
						max: 180,
						step: 5,
						onChange: (v) => s.patch({ intervalSec: v })
					})
				]
			})
		]
	});
}
function Row({ label, hint, checked, onChange }) {
	return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
		className: "flex items-center justify-between gap-4",
		children: [/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", { children: [/* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", {
			className: "text-sm",
			children: label
		}), /* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", {
			className: "text-xs text-muted",
			children: hint
		})] }), /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Switch, {
			checked,
			onCheckedChange: onChange
		})]
	});
}
function Field({ label, value, min, max, step, onChange }) {
	return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("label", {
		className: "flex flex-col gap-2 text-sm",
		children: [/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("span", {
			className: "flex justify-between text-muted",
			children: [label, /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", {
				className: "font-mono tabular-nums text-fg",
				children: value
			})]
		}), /* @__PURE__ */ (0, import_jsx_runtime.jsx)("input", {
			type: "range",
			min,
			max,
			step,
			value,
			onChange: (e) => onChange(Number(e.target.value)),
			className: "w-full accent-primary"
		})]
	});
}
function formatUsd(n) {
	if (!Number.isFinite(n)) return "—";
	const abs = Math.abs(n);
	if (abs >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
	if (abs >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
	if (abs >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
	return n.toFixed(0);
}
function formatPrice(n) {
	if (!Number.isFinite(n)) return "—";
	const abs = Math.abs(n);
	if (abs >= 1e3) return n.toLocaleString("en-US", { maximumFractionDigits: 2 });
	if (abs >= 1) return n.toFixed(4);
	if (abs >= .01) return n.toFixed(5);
	return n.toPrecision(4);
}
function formatPct(n, digits = 2) {
	if (!Number.isFinite(n)) return "—";
	return `${n > 0 ? "+" : ""}${n.toFixed(digits)}%`;
}
function formatAgo(ts) {
	const s = Math.max(0, Math.round((Date.now() - ts) / 1e3));
	if (s < 5) return "сейчас";
	if (s < 60) return `${s}с назад`;
	const m = Math.floor(s / 60);
	if (m < 60) return `${m}м назад`;
	return `${Math.floor(m / 60)}ч назад`;
}
function MarketPanel({ movers, onOpen }) {
	return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
		className: "flex flex-col gap-4",
		children: [/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("header", { children: [
			/* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", {
				className: "text-xs uppercase tracking-widest text-subtle",
				children: "Рынок"
			}),
			/* @__PURE__ */ (0, import_jsx_runtime.jsx)("h2", {
				className: "mt-1 text-2xl font-medium tracking-tight",
				children: "Лидеры USDT за 24ч"
			}),
			/* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", {
				className: "mt-2 text-sm text-muted",
				children: "Это ещё не сигнал. Памп подтверждается объёмом, пробоем и RSI на 15м / 30м / 1ч."
			})
		] }), /* @__PURE__ */ (0, import_jsx_runtime.jsx)("ul", {
			className: "flex flex-col gap-1",
			children: movers.map((m) => /* @__PURE__ */ (0, import_jsx_runtime.jsx)("li", { children: /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("button", {
				type: "button",
				onClick: () => onOpen(m.symbol),
				className: "flex h-14 w-full items-center justify-between gap-3 rounded-md px-3 text-left hover:bg-bg-subtle",
				children: [/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("span", {
					className: "font-medium",
					children: [m.base, /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", {
						className: "ml-1 text-xs text-subtle",
						children: "USDT"
					})]
				}), /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("span", {
					className: "flex items-center gap-4 font-mono text-sm tabular-nums",
					children: [
						/* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", {
							className: "text-muted",
							children: formatPrice(m.price)
						}),
						/* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", {
							className: "hidden text-subtle sm:inline",
							children: formatUsd(m.quoteVolume24h)
						}),
						/* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", {
							className: cn(m.change24h >= 0 ? "text-pump" : "text-dump", "w-16 text-right"),
							children: formatPct(m.change24h)
						})
					]
				})]
			}) }, m.symbol))
		})]
	});
}
function PulseMark({ className }) {
	return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("svg", {
		viewBox: "0 0 32 32",
		className: cn("text-primary", className),
		"aria-hidden": true,
		children: [/* @__PURE__ */ (0, import_jsx_runtime.jsx)("rect", {
			width: "32",
			height: "32",
			rx: "6",
			fill: "currentColor",
			className: "text-bg-subtle"
		}), /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("g", {
			fill: "none",
			stroke: "currentColor",
			strokeWidth: "2",
			strokeLinecap: "round",
			className: "text-primary",
			children: [
				/* @__PURE__ */ (0, import_jsx_runtime.jsx)("path", { d: "M7 23a11 11 0 0 1 18 0" }),
				/* @__PURE__ */ (0, import_jsx_runtime.jsx)("path", { d: "M11 20.5a6.5 6.5 0 0 1 10 0" }),
				/* @__PURE__ */ (0, import_jsx_runtime.jsx)("path", { d: "M16 6.5v16" })
			]
		})]
	});
}
function PriceChart({ symbol, timeframe }) {
	const q = useQuery({
		queryKey: [
			"klines",
			symbol,
			timeframe
		],
		queryFn: () => fetchSymbolChart({ data: {
			symbol,
			timeframe
		} }),
		staleTime: 2e4
	});
	if (q.isLoading) return /* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", { className: "h-48 animate-pulse rounded-md bg-bg-subtle" });
	if (!q.data?.length) return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("p", {
		className: "text-sm text-muted",
		children: ["Нет свечей для ", symbol]
	});
	const data = q.data.map((row) => ({
		t: new Date(row.t).toLocaleTimeString("ru-RU", {
			hour: "2-digit",
			minute: "2-digit"
		}),
		c: row.c
	}));
	const stroke = (q.data.at(-1)?.c ?? 0) >= (q.data[0]?.c ?? 0) ? "var(--color-pump)" : "var(--color-dump)";
	return /* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", {
		className: "h-48",
		children: /* @__PURE__ */ (0, import_jsx_runtime.jsx)(ResponsiveContainer, {
			width: "100%",
			height: "100%",
			children: /* @__PURE__ */ (0, import_jsx_runtime.jsxs)(AreaChart, {
				data,
				margin: {
					top: 8,
					right: 8,
					left: 0,
					bottom: 0
				},
				children: [
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)("defs", { children: /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("linearGradient", {
						id: "pulseFill",
						x1: "0",
						y1: "0",
						x2: "0",
						y2: "1",
						children: [/* @__PURE__ */ (0, import_jsx_runtime.jsx)("stop", {
							offset: "0%",
							stopColor: stroke,
							stopOpacity: .28
						}), /* @__PURE__ */ (0, import_jsx_runtime.jsx)("stop", {
							offset: "100%",
							stopColor: stroke,
							stopOpacity: 0
						})]
					}) }),
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)(XAxis, {
						dataKey: "t",
						hide: true
					}),
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)(YAxis, {
						domain: ["auto", "auto"],
						width: 64,
						tick: {
							fill: "var(--color-subtle)",
							fontSize: 11,
							fontFamily: "var(--font-mono)"
						},
						tickFormatter: (v) => formatPrice(v),
						axisLine: false,
						tickLine: false
					}),
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)(Tooltip, {
						contentStyle: {
							background: "var(--color-bg-elevated)",
							border: "1px solid var(--color-border)",
							borderRadius: 8,
							color: "var(--color-fg)",
							fontFamily: "var(--font-mono)",
							fontSize: 12
						},
						formatter: (v) => [formatPrice(v), "Цена"]
					}),
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)(Area, {
						type: "monotone",
						dataKey: "c",
						stroke,
						fill: "url(#pulseFill)",
						strokeWidth: 1.6
					})
				]
			})
		})
	});
}
var badgeVariants = cva("inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium tracking-wide", {
	variants: { tone: {
		strong: "bg-pump/15 text-pump",
		watch: "bg-watch/15 text-watch",
		late: "bg-late/15 text-late",
		mute: "bg-bg-subtle text-muted"
	} },
	defaultVariants: { tone: "mute" }
});
function Badge({ className, tone, ...props }) {
	return /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", {
		className: cn(badgeVariants({ tone }), className),
		...props
	});
}
var GRADE_LABEL$1 = {
	strong: "ПАМП",
	watch: "СМОТРЕТЬ",
	late: "ПОЗДНО"
};
function SignalDetail({ signal }) {
	const best = signal.byTf.find((t) => t.timeframe === signal.bestTf) ?? signal.byTf[0];
	return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
		className: "flex flex-col gap-5",
		children: [
			/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("header", {
				className: "flex flex-wrap items-start justify-between gap-3",
				children: [/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", { children: [
					/* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", {
						className: "text-xs uppercase tracking-widest text-subtle",
						children: "Сигнал"
					}),
					/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("h2", {
						className: "text-2xl font-medium tracking-tight",
						children: [signal.base, /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", {
							className: "text-muted",
							children: "/USDT"
						})]
					}),
					/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("p", {
						className: "mt-1 font-mono text-sm tabular-nums text-muted",
						children: [
							formatPrice(signal.price),
							" · 24ч ",
							formatPct(signal.change24h),
							" · ",
							formatUsd(signal.quoteVolume24h)
						]
					})
				] }), /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
					className: "flex items-center gap-2",
					children: [/* @__PURE__ */ (0, import_jsx_runtime.jsx)(Badge, {
						tone: signal.grade,
						children: GRADE_LABEL$1[signal.grade]
					}), /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", {
						className: "font-mono text-lg tabular-nums",
						children: signal.bestScore.toFixed(0)
					})]
				})]
			}),
			/* @__PURE__ */ (0, import_jsx_runtime.jsx)(PriceChart, {
				symbol: signal.symbol,
				timeframe: signal.bestTf
			}),
			/* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", {
				className: "grid grid-cols-3 gap-2",
				children: signal.byTf.map((tf) => /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
					className: "rounded-md bg-bg-subtle px-3 py-2",
					children: [
						/* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", {
							className: "text-xs text-subtle",
							children: tf.timeframe
						}),
						/* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", {
							className: "font-mono text-sm tabular-nums",
							children: tf.score.toFixed(0)
						}),
						/* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", {
							className: "text-xs text-muted",
							children: formatPct(tf.changePct, 2)
						})
					]
				}, tf.timeframe))
			}),
			/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("section", { children: [
				/* @__PURE__ */ (0, import_jsx_runtime.jsx)("h3", {
					className: "mb-2 text-sm font-medium",
					children: "Почему это памп"
				}),
				/* @__PURE__ */ (0, import_jsx_runtime.jsx)("ul", {
					className: "flex flex-col gap-1.5",
					children: best.reasons.map((r) => /* @__PURE__ */ (0, import_jsx_runtime.jsx)("li", {
						className: "text-sm text-fg",
						children: r
					}, r))
				}),
				best.risks.length > 0 && /* @__PURE__ */ (0, import_jsx_runtime.jsx)("ul", {
					className: "mt-3 flex flex-col gap-1.5",
					children: best.risks.map((r) => /* @__PURE__ */ (0, import_jsx_runtime.jsx)("li", {
						className: "text-sm text-late",
						children: r
					}, r))
				})
			] }),
			/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("section", { children: [/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("h3", {
				className: "mb-3 text-sm font-medium",
				children: ["Факторы · ", best.timeframe]
			}), /* @__PURE__ */ (0, import_jsx_runtime.jsx)("ul", {
				className: "flex flex-col gap-3",
				children: best.factors.map((f) => /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("li", { children: [/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
					className: "mb-1 flex items-baseline justify-between gap-3",
					children: [/* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", {
						className: "text-sm",
						children: f.label
					}), /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", {
						className: "font-mono text-xs tabular-nums text-muted",
						children: f.note
					})]
				}), /* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", {
					className: "h-1.5 overflow-hidden rounded-full bg-bg-subtle",
					children: /* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", {
						className: cn("h-full rounded-full bg-primary"),
						style: { width: `${Math.round(f.value * 100)}%` }
					})
				})] }, f.id))
			})] })
		]
	});
}
var GRADE_LABEL = {
	strong: "ПАМП",
	watch: "СМОТРЕТЬ",
	late: "ПОЗДНО"
};
function SignalList({ signals, closest, selected, onSelect, tfFilter }) {
	const rows = tfFilter === "all" ? signals : signals.filter((s) => s.byTf.some((t) => t.timeframe === tfFilter));
	return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
		className: "flex flex-col gap-4",
		children: [rows.length ? /* @__PURE__ */ (0, import_jsx_runtime.jsx)("ul", {
			className: "flex flex-col gap-2",
			children: rows.map((s) => /* @__PURE__ */ (0, import_jsx_runtime.jsx)("li", { children: /* @__PURE__ */ (0, import_jsx_runtime.jsx)(SignalButton, {
				signal: s,
				active: selected === s.symbol,
				onSelect
			}) }, s.symbol))
		}) : /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
			className: "flex flex-col items-center justify-center gap-3 px-4 py-8 text-center",
			children: [/* @__PURE__ */ (0, import_jsx_runtime.jsx)(Activity, { className: "size-6 text-subtle" }), /* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", {
				className: "text-sm text-muted",
				children: "Подтверждённого пампа нет. Фильтры отсекают тонкий объём, тени и уже растянутые ходы."
			})]
		}), closest.length > 0 && /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", { children: [/* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", {
			className: "mb-2 text-xs uppercase tracking-widest text-subtle",
			children: "Ближайшие сетапы"
		}), /* @__PURE__ */ (0, import_jsx_runtime.jsx)("ul", {
			className: "flex flex-col gap-2",
			children: closest.map((s) => /* @__PURE__ */ (0, import_jsx_runtime.jsx)("li", { children: /* @__PURE__ */ (0, import_jsx_runtime.jsx)(SignalButton, {
				signal: s,
				active: selected === s.symbol,
				onSelect,
				muted: true
			}) }, s.symbol))
		})] })]
	});
}
function SignalButton({ signal: s, active, onSelect, muted }) {
	const tf = s.byTf.find((t) => t.timeframe === s.bestTf) ?? s.byTf[0];
	return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("button", {
		type: "button",
		onClick: () => onSelect(s.symbol),
		className: cn("flex w-full flex-col gap-2 rounded-lg bg-bg-elevated px-4 py-3 text-left shadow-[var(--shadow-border)] transition-[box-shadow,background-color] duration-[var(--motion-quick)]", active && "bg-bg-subtle shadow-[var(--shadow-border-hover)]", muted && "opacity-80"),
		children: [
			/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
				className: "flex items-center justify-between gap-3",
				children: [/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
					className: "flex items-baseline gap-2",
					children: [/* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", {
						className: "font-medium tracking-tight",
						children: s.base
					}), /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", {
						className: "text-xs text-subtle",
						children: "USDT"
					})]
				}), muted ? /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Badge, { children: "рядом" }) : /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Badge, {
					tone: s.grade,
					children: GRADE_LABEL[s.grade]
				})]
			}),
			/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
				className: "flex items-end justify-between gap-3",
				children: [/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", { children: [/* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", {
					className: "font-mono text-sm tabular-nums",
					children: formatPrice(s.price)
				}), /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("p", {
					className: cn("font-mono text-xs tabular-nums", s.change24h >= 0 ? "text-pump" : "text-dump"),
					children: ["24ч ", formatPct(s.change24h)]
				})] }), /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
					className: "text-right",
					children: [/* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", {
						className: "font-mono text-sm tabular-nums text-fg",
						children: s.bestScore.toFixed(0)
					}), /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("p", {
						className: "text-xs text-subtle",
						children: [
							s.bestTf,
							" · vol ",
							tf.volumeRatio.toFixed(1),
							"×"
						]
					})]
				})]
			}),
			/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("p", {
				className: "text-xs text-muted",
				children: [
					"оборот ",
					formatUsd(s.quoteVolume24h),
					" · vs BTC ",
					formatPct(s.btcRelative24h, 1)
				]
			})
		]
	});
}
var SENT_KEY = "pulse-sent-alerts";
function loadSent() {
	try {
		const raw = localStorage.getItem(SENT_KEY);
		const arr = raw ? JSON.parse(raw) : [];
		return new Set(arr.slice(-400));
	} catch {
		return /* @__PURE__ */ new Set();
	}
}
function saveSent(set) {
	localStorage.setItem(SENT_KEY, JSON.stringify([...set].slice(-400)));
}
function Dashboard() {
	const settings = useSettings();
	const [tab, setTab] = (0, import_react.useState)("signals");
	const [tfFilter, setTfFilter] = (0, import_react.useState)("all");
	const [selected, setSelected] = (0, import_react.useState)(null);
	const primed = (0, import_react.useRef)(false);
	(0, import_react.useEffect)(() => {
		const tg = window.Telegram?.WebApp;
		tg?.ready();
		tg?.expand();
	}, []);
	const scan = useQuery({
		queryKey: [
			"scan",
			settings.minScore,
			settings.minQuoteVolume,
			settings.alreadyPumpedMax,
			settings.timeframes.join(",")
		],
		queryFn: () => scanMarket({ data: {
			minScore: settings.minScore,
			minQuoteVolume: settings.minQuoteVolume,
			alreadyPumpedMax: settings.alreadyPumpedMax,
			timeframes: settings.timeframes
		} }),
		refetchInterval: settings.intervalSec * 1e3,
		staleTime: 1e4
	});
	(0, import_react.useEffect)(() => {
		if (!scan.data?.ok || !settings.autoNotify || !settings.botToken || !settings.chatId) {
			if (scan.data?.ok) primed.current = true;
			return;
		}
		const sent = loadSent();
		const fresh = [];
		for (const s of scan.data.signals) {
			if (sent.has(s.alertKey)) continue;
			if (s.grade === "watch" && !settings.notifyWatch) continue;
			if (s.grade === "late" && !settings.notifyLate) continue;
			if (s.grade === "strong" || settings.notifyWatch || settings.notifyLate) {
				if (s.grade === "strong" || s.grade === "watch" && settings.notifyWatch || s.grade === "late" && settings.notifyLate) fresh.push(s);
			}
		}
		if (!primed.current) {
			for (const s of fresh) sent.add(s.alertKey);
			saveSent(sent);
			primed.current = true;
			return;
		}
		(async () => {
			for (const s of fresh) {
				const tf = s.byTf.find((t) => t.timeframe === s.bestTf) ?? s.byTf[0];
				const res = await pingTelegram({ data: {
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
						vsBtc: s.btcRelative24h
					})
				} });
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
		settings.notifyLate
	]);
	const signals = scan.data?.signals ?? [];
	const closest = scan.data?.closest ?? [];
	const active = (0, import_react.useMemo)(() => {
		const pool = [...signals, ...closest];
		return pool.find((s) => s.symbol === selected) ?? pool[0] ?? null;
	}, [
		signals,
		closest,
		selected
	]);
	(0, import_react.useEffect)(() => {
		if (!selected) {
			const first = signals[0] ?? closest[0];
			if (first) setSelected(first.symbol);
		}
	}, [
		selected,
		signals,
		closest
	]);
	const strongN = signals.filter((s) => s.grade === "strong").length;
	return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
		className: "flex min-h-dvh flex-col bg-bg text-fg",
		children: [
			/* @__PURE__ */ (0, import_jsx_runtime.jsx)("header", {
				className: "sticky top-0 z-20 border-b border-border bg-bg/95 px-4 py-3 backdrop-blur-sm",
				children: /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
					className: "mx-auto flex max-w-6xl items-center justify-between gap-3",
					children: [/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
						className: "flex items-center gap-3",
						children: [/* @__PURE__ */ (0, import_jsx_runtime.jsx)(PulseMark, { className: "size-8" }), /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", { children: [/* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", {
							className: "text-sm font-medium tracking-tight",
							children: "PULSE"
						}), /* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", {
							className: "text-xs text-subtle",
							children: "USDT · Binance"
						})] })]
					}), /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
						className: "flex items-center gap-3",
						children: [
							scan.data?.btc && /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("p", {
								className: "hidden font-mono text-xs tabular-nums text-muted sm:block",
								children: [
									"BTC ",
									formatPrice(scan.data.btc.price),
									" ",
									/* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", {
										className: scan.data.btc.change24h >= 0 ? "text-pump" : "text-dump",
										children: formatPct(scan.data.btc.change24h, 1)
									})
								]
							}),
							/* @__PURE__ */ (0, import_jsx_runtime.jsx)("nav", {
								className: "hidden items-center gap-1 lg:flex",
								children: TABS.map((t) => /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Button, {
									variant: tab === t.id ? "default" : "ghost",
									size: "sm",
									onClick: () => setTab(t.id),
									children: t.label
								}, t.id))
							}),
							/* @__PURE__ */ (0, import_jsx_runtime.jsxs)(Button, {
								variant: "outline",
								size: "sm",
								onClick: () => void scan.refetch(),
								disabled: scan.isFetching,
								children: [/* @__PURE__ */ (0, import_jsx_runtime.jsx)(RefreshCw, { className: cn("size-3.5", scan.isFetching && "animate-spin") }), scan.isFetching ? "Скан" : "Обновить"]
							})
						]
					})]
				})
			}),
			/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("main", {
				className: "mx-auto flex w-full max-w-6xl flex-1 flex-col gap-4 px-4 py-4 pb-24 lg:pb-6",
				children: [
					/* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
						className: "flex flex-wrap items-center gap-2 text-xs text-muted",
						children: [
							/* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: cn("size-1.5 rounded-full", scan.isError || scan.data?.ok === false ? "bg-dump" : "bg-pump") }),
							scan.data?.ok === false ? scan.data.error : scan.isLoading ? "Первый скан рынка…" : `${scan.data?.universe ?? 0} пар USDT · ${scan.data?.candidates ?? 0} кандидатов · ${strongN} памп`,
							scan.data?.scannedAt ? /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("span", { children: ["· ", formatAgo(scan.data.scannedAt)] }) : null
						]
					}),
					tab === "signals" && /* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", {
						className: "flex gap-2",
						children: ["all", ...TIMEFRAMES].map((tf) => /* @__PURE__ */ (0, import_jsx_runtime.jsx)("button", {
							type: "button",
							onClick: () => setTfFilter(tf),
							className: cn("h-11 rounded-md px-3 text-sm", tfFilter === tf ? "bg-primary text-primary-fg" : "text-muted shadow-[var(--shadow-border)]"),
							children: tf === "all" ? "все ТФ" : tf
						}, tf))
					}),
					tab === "signals" && /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", {
						className: "grid gap-6 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]",
						children: [
							/* @__PURE__ */ (0, import_jsx_runtime.jsx)("section", { children: scan.isLoading ? /* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", {
								className: "flex flex-col gap-2",
								children: Array.from({ length: 5 }).map((_, i) => /* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", { className: "h-24 animate-pulse rounded-lg bg-bg-elevated" }, i))
							}) : /* @__PURE__ */ (0, import_jsx_runtime.jsx)(SignalList, {
								signals,
								closest,
								selected: active?.symbol ?? null,
								onSelect: setSelected,
								tfFilter
							}) }),
							/* @__PURE__ */ (0, import_jsx_runtime.jsx)("section", {
								className: "hidden rounded-xl bg-bg-elevated p-5 shadow-[var(--shadow-border)] lg:block",
								children: active ? /* @__PURE__ */ (0, import_jsx_runtime.jsx)(SignalDetail, { signal: active }) : /* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", {
									className: "text-sm text-muted",
									children: "Выберите сигнал, чтобы разобрать факторы."
								})
							}),
							active && /* @__PURE__ */ (0, import_jsx_runtime.jsx)("section", {
								className: "rounded-xl bg-bg-elevated p-5 shadow-[var(--shadow-border)] lg:hidden",
								children: /* @__PURE__ */ (0, import_jsx_runtime.jsx)(SignalDetail, { signal: active })
							})
						]
					}),
					tab === "market" && /* @__PURE__ */ (0, import_jsx_runtime.jsx)(MarketPanel, {
						movers: scan.data?.movers ?? [],
						onOpen: (symbol) => {
							setSelected(symbol);
							setTab("signals");
						}
					}),
					tab === "algo" && /* @__PURE__ */ (0, import_jsx_runtime.jsx)(AlgoPanel, {}),
					tab === "bot" && /* @__PURE__ */ (0, import_jsx_runtime.jsx)(BotPanel, {})
				]
			}),
			/* @__PURE__ */ (0, import_jsx_runtime.jsx)("nav", {
				className: "fixed inset-x-0 bottom-0 z-20 border-t border-border bg-bg/95 pb-[env(safe-area-inset-bottom)] backdrop-blur-sm lg:hidden",
				children: /* @__PURE__ */ (0, import_jsx_runtime.jsx)("ul", {
					className: "mx-auto grid max-w-6xl grid-cols-4",
					children: TABS.map((t) => /* @__PURE__ */ (0, import_jsx_runtime.jsx)("li", { children: /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("button", {
						type: "button",
						onClick: () => setTab(t.id),
						className: cn("flex h-14 w-full flex-col items-center justify-center gap-1 text-xs", tab === t.id ? "text-fg" : "text-subtle"),
						children: [/* @__PURE__ */ (0, import_jsx_runtime.jsx)(t.icon, { className: "size-4" }), t.label]
					}) }, t.id))
				})
			})
		]
	});
}
var TABS = [
	{
		id: "signals",
		label: "Сигналы",
		icon: Radar
	},
	{
		id: "market",
		label: "Рынок",
		icon: TrendingUp
	},
	{
		id: "algo",
		label: "Алгоритм",
		icon: Activity
	},
	{
		id: "bot",
		label: "Бот",
		icon: Bot
	}
];
function Home() {
	return /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Dashboard, {});
}
//#endregion
export { Home as component };
