#!/usr/bin/env python3
"""
Рендер графика 15m с индикаторами в PNG — на чистой стандартной библиотеке.

Зачем свой рендер, а не внешний сервис: бот работает и в GitHub Actions, и
локально через watchdog.py, и в проекте нет ни одной сторонней зависимости.
Картинка собирается из тех же свечей, что уже качает сканер, не требует сети
для отрисовки и не имеет лимитов чужого сервиса.

Состав картинки (три панели, тёмная тема):
  • свечи 15m + полосы Боллинджера (20, 2σ);
  • RSI 14 с уровнями 30/50/70;
  • стохастик (14, 3): %K и %D с уровнями 20/80.

Числа НЕ рисуются на картинке: текст отдаётся подписью в Telegram
(`chart_caption`). Это избавляет от растрового шрифта (сотни строк данных) и
даёт выделяемые значения — их можно скопировать, чего не сделать с пикселями.
"""
from __future__ import annotations

import struct
import zlib
from typing import List, Optional, Sequence, Tuple

import pump_bot as pb

# ── Тема ──
BG = (14, 17, 23)
GRID = (32, 38, 48)
BORDER = (52, 60, 72)
UP = (38, 166, 154)
DOWN = (239, 83, 80)
BB_LINE = (91, 107, 140)
BB_MID = (64, 76, 104)
RSI_LINE = (240, 185, 11)
LEVEL = (58, 66, 86)
STOCH_K = (0, 188, 212)
STOCH_D = (233, 30, 99)
LAST_LINE = (120, 132, 156)

PRICE_H = 300
RSI_H = 120
STOCH_H = 120
GAP = 16
PAD_LEFT = 8
PAD_RIGHT = 12          # числа идут в подпись Telegram, шкала на картинке не нужна
PAD_TOP = 10


class Canvas:
    """Простой RGB-холст с минимумом примитивов."""

    def __init__(self, width: int, height: int):
        self.w = width
        self.h = height
        self.buf = bytearray(bytes(BG) * (width * height))
        self._clip: Optional[Tuple[int, int, int, int]] = None

    def set_clip(self, x1: int, y1: int, x2: int, y2: int) -> None:
        """Ограничивает рисование прямоугольником панели: свеча-выброс не
        должна вылезать за рамку и пачкать соседний график."""
        self._clip = (x1, y1, x2, y2)

    def clear_clip(self) -> None:
        self._clip = None

    def px(self, x: int, y: int, color) -> None:
        if not (0 <= x < self.w and 0 <= y < self.h):
            return
        if self._clip:
            cx1, cy1, cx2, cy2 = self._clip
            if not (cx1 <= x <= cx2 and cy1 <= y <= cy2):
                return
        i = (y * self.w + x) * 3
        self.buf[i:i + 3] = bytes(color)

    def hline(self, x1: int, x2: int, y: int, color, dashed: bool = False) -> None:
        step = 6 if dashed else 1
        for x in range(x1, x2):
            if not dashed or ((x // step) % 2 == 0):
                self.px(x, y, color)

    def vline(self, x: int, y1: int, y2: int, color, dashed: bool = False) -> None:
        step = 6 if dashed else 1
        for y in range(y1, y2):
            if not dashed or ((y // step) % 2 == 0):
                self.px(x, y, color)

    def rect(self, x1: int, y1: int, x2: int, y2: int, color, fill: bool = False) -> None:
        if fill:
            for y in range(y1, y2 + 1):
                self.hline(x1, x2 + 1, y, color)
            return
        self.hline(x1, x2 + 1, y1, color)
        self.hline(x1, x2 + 1, y2, color)
        self.vline(x1, y1, y2 + 1, color)
        self.vline(x2, y1, y2 + 1, color)

    def line(self, x1: int, y1: int, x2: int, y2: int, color) -> None:
        """Отрезок по Брезенхэму."""
        dx, dy = abs(x2 - x1), abs(y2 - y1)
        sx = 1 if x1 < x2 else -1
        sy = 1 if y1 < y2 else -1
        err = dx - dy
        while True:
            self.px(x1, y1, color)
            if x1 == x2 and y1 == y2:
                return
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x1 += sx
            if e2 < dx:
                err += dx
                y1 += sy

    def to_png(self) -> bytes:
        raw = bytearray()
        row = self.w * 3
        for y in range(self.h):
            raw.append(0)                      # фильтр «none»
            raw += self.buf[y * row:(y + 1) * row]

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        ihdr = struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0)
        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
                + chunk(b"IEND", b""))


# ── Индикаторы ──

def bollinger(closes: Sequence[float], period: int = 20, mult: float = 2.0):
    """(upper, middle, lower) — по массивам той же длины, None где мало данных."""
    n = len(closes)
    up: List[Optional[float]] = [None] * n
    mid: List[Optional[float]] = [None] * n
    low: List[Optional[float]] = [None] * n
    for i in range(period - 1, n):
        window = list(closes[i - period + 1:i + 1])
        m = pb.sma(window, period)
        sd = pb.stddev(window, period)
        if m is None or sd is None:
            continue
        mid[i] = m
        up[i] = m + mult * sd
        low[i] = m - mult * sd
    return up, mid, low


def rsi_series(closes: Sequence[float], period: int = 14) -> List[Optional[float]]:
    """RSI по всей серии (pb.rsi считает только последнее значение)."""
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    def value(g: float, l: float) -> float:
        if l == 0:
            return 100.0
        rs = g / l
        return 100 - 100 / (1 + rs)
    out[period] = value(avg_gain, avg_loss)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = value(avg_gain, avg_loss)
    return out


def stochastic(candles, k_period: int = 14, d_period: int = 3):
    """(%K, %D) — стохастик по High/Low/Close."""
    n = len(candles)
    k: List[Optional[float]] = [None] * n
    d: List[Optional[float]] = [None] * n
    for i in range(k_period - 1, n):
        window = candles[i - k_period + 1:i + 1]
        hi = max(c.high for c in window)
        lo = min(c.low for c in window)
        if hi <= lo:
            k[i] = 50.0
        else:
            k[i] = (candles[i].close - lo) / (hi - lo) * 100.0
    ks = [v for v in k if v is not None]
    for i in range(len(k)):
        if k[i] is None:
            continue
        window = [v for v in k[max(0, i - d_period + 1):i + 1] if v is not None]
        if len(window) == d_period:
            d[i] = sum(window) / d_period
    return k, d, ks


# ── Отрисовка ──

def _panel_scale(values, top: int, height: int, lo: float, hi: float):
    """Возвращает функцию значение -> y внутри панели."""
    span = (hi - lo) or 1.0
    def to_y(v: float) -> int:
        return int(top + height - (v - lo) / span * height)
    return to_y


def _percentile(values, p: float) -> float:
    """Перцентиль без зависимостей: сглаживает выбросы в диапазоне шкалы."""
    if not values:
        return 0.0
    s = sorted(values)
    k = int(round((len(s) - 1) * p))
    return s[max(0, min(len(s) - 1, k))]


def render_chart(symbol: str, candles, bars: int = 120,
                 width: int = 900, height: int = 620) -> bytes:
    """
    Рисует PNG с тремя панелями. `candles` — свечи 15m (старшие — в конце).
    """
    if len(candles) < 30:
        raise ValueError("нужно минимум 30 свечей для графика")

    data = candles[-bars:] if len(candles) > bars else candles
    closes = [c.close for c in data]
    up, mid, low = bollinger(closes)
    rsi = rsi_series(closes)
    k, d, _ks = stochastic(data)

    c = Canvas(width, height)
    x0, x1 = PAD_LEFT, width - PAD_RIGHT

    # ── Панель цены ──
    p_top = PAD_TOP
    p_bot = p_top + PRICE_H
    pool = [c_.high for c_ in data] + [c_.low for c_ in data]
    pool += [v for v in up if v is not None] + [v for v in low if v is not None]
    # Диапазон по 2-му/98-му перцентилю, а не по абсолютным min/max: один
    # выброс в начале серии иначе растягивает шкалу и сжимает свежую часть
    # графика в узкую полосу — читать индикаторы становится невозможно.
    lo_p, hi_p = _percentile(pool, 0.02), _percentile(pool, 0.98)
    last_close = data[-1].close
    lo_p, hi_p = min(lo_p, last_close), max(hi_p, last_close)
    marg = (hi_p - lo_p) * 0.04 or 1.0
    lo_p, hi_p = lo_p - marg, hi_p + marg
    to_y = _panel_scale(None, p_top, PRICE_H, lo_p, hi_p)

    c.rect(x0 - 1, p_top, x1, p_bot, BORDER)
    for frac in (0.25, 0.5, 0.75):
        y = int(p_top + PRICE_H * frac)
        c.hline(x0, x1, y, GRID, dashed=True)
    c.set_clip(x0, p_top + 1, x1, p_bot - 1)

    step = (x1 - x0) / max(1, len(data))
    body = max(1.0, step * 0.65)

    # полосы Боллинджера: середина пунктиром, чтобы не сливалась с границами
    # полосы Боллинджера: середина отличается цветом, иначе три линии
    # сливаются в одну и по графику нельзя понять, где границы канала
    for series, color in ((up, BB_LINE), (low, BB_LINE), (mid, BB_MID)):
        prev = None
        for i, v in enumerate(series):
            if v is None:
                prev = None
                continue
            x = int(x0 + i * step + step / 2)
            y = to_y(v)
            if prev is not None:
                c.line(prev[0], prev[1], x, y, color)
            prev = (x, y)

    # свечи
    for i, bar in enumerate(data):
        x = int(x0 + i * step + step / 2)
        col = UP if bar.close >= bar.open else DOWN
        c.vline(x, to_y(bar.high), to_y(bar.low), col)
        y_open, y_close = to_y(bar.open), to_y(bar.close)
        ytop, ybot = min(y_open, y_close), max(y_open, y_close)
        c.rect(x - int(body / 2), ytop, x + int(body / 2), max(ybot, ytop + 1), col, fill=True)

    last = data[-1]
    c.hline(x0, x1, to_y(last.close), LAST_LINE, dashed=True)
    c.clear_clip()

    # ── Панель RSI ──
    r_top = p_bot + GAP
    r_bot = r_top + RSI_H
    c.rect(x0 - 1, r_top, x1, r_bot, BORDER)
    to_r = _panel_scale(None, r_top, RSI_H, 0.0, 100.0)
    for lvl in (30, 50, 70):
        col = LEVEL if lvl != 50 else GRID
        c.hline(x0, x1, to_r(lvl), col, dashed=True)
    c.set_clip(x0, r_top + 1, x1, r_bot - 1)
    prev = None
    for i, v in enumerate(rsi):
        if v is None:
            prev = None
            continue
        x = int(x0 + i * step + step / 2)
        y = to_r(max(0.0, min(100.0, v)))
        if prev is not None:
            c.line(prev[0], prev[1], x, y, RSI_LINE)
        prev = (x, y)
    c.clear_clip()

    # ── Панель стохастика ──
    s_top = r_bot + GAP
    s_bot = s_top + STOCH_H
    c.rect(x0 - 1, s_top, x1, s_bot, BORDER)
    to_s = _panel_scale(None, s_top, STOCH_H, 0.0, 100.0)
    for lvl in (20, 50, 80):
        col = LEVEL if lvl != 50 else GRID
        c.hline(x0, x1, to_s(lvl), col, dashed=True)
    c.set_clip(x0, s_top + 1, x1, s_bot - 1)
    for series, color in ((k, STOCH_K), (d, STOCH_D)):
        prev = None
        for i, v in enumerate(series):
            if v is None:
                prev = None
                continue
            x = int(x0 + i * step + step / 2)
            y = to_s(max(0.0, min(100.0, v)))
            if prev is not None:
                c.line(prev[0], prev[1], x, y, color)
            prev = (x, y)
    c.clear_clip()

    return c.to_png()


def chart_caption(symbol: str, candles, bars: int = 120) -> str:
    """Подпись к картинке: числа текстом (их можно выделить и скопировать)."""
    data = candles[-bars:] if len(candles) > bars else candles
    closes = [c.close for c in data]
    up, mid, low = bollinger(closes)
    rsi = rsi_series(closes)
    k, d, _ = stochastic(data)
    base = symbol[:-4] if symbol.endswith("USDT") else symbol

    last = data[-1]
    prev = data[-2] if len(data) > 1 else last
    chg = pb.pct_change(prev.close, last.close)
    vol_sma = pb.sma([c.volume for c in data[:-1]], 20)
    vol_x = (last.volume / vol_sma) if vol_sma else 0.0

    def fmt(v, suffix=""):
        return f"{v:.2f}{suffix}" if v is not None else "—"

    rsi_v = rsi[-1]
    if rsi_v is None:
        rsi_zone = "нет данных"
    elif rsi_v >= 70:
        rsi_zone = "перекупленность"
    elif rsi_v <= 30:
        rsi_zone = "перепроданность"
    elif rsi_v >= 55:
        rsi_zone = "умеренно сильный"
    elif rsi_v <= 45:
        rsi_zone = "умеренно слабый"
    else:
        rsi_zone = "нейтрально"

    bb_up, bb_low = up[-1], low[-1]
    bb_w = ((bb_up - bb_low) / mid[-1] * 100) if (bb_up and bb_low and mid[-1]) else None
    k_v, d_v = k[-1], d[-1]

    lines = [
        f"📉 <b>{base}/USDT · 15m</b>",
        f"Цена <code>{pb.fmt_price(last.close)}</code> ({fmt(chg, '%')} за бар), "
        f"объём <code>{vol_x:.1f}×</code> к SMA20",
        "",
        f"• <b>Боллинджер</b> (20, 2σ): <code>{fmt(bb_low)}</code> … <code>{fmt(bb_up)}</code>"
        + (f", ширина {bb_w:.1f}%" if bb_w is not None else ""),
        f"• <b>RSI 14</b>: <code>{fmt(rsi_v)}</code> — {rsi_zone}",
        f"• <b>Стохастик</b> (14,3): %K <code>{fmt(k_v)}</code> / %D <code>{fmt(d_v)}</code>",
        "",
        "<i>Верх: свечи + Боллинджер · Середина: RSI (уровни 30/50/70) · "
        "Низ: стохастик (20/50/80) — %K бирюзовый, %D розовый.</i>",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    # Самопроверка: строим график по живым данным и сохраняем рядом файл.
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "SOLUSDT"
    raw5 = pb.fetch_klines(sym, limit=500)
    bars15 = pb.aggregate_timeframe(raw5, 900_000)
    print(f"{sym}: свечей 5m={len(raw5)}, свечей 15m={len(bars15)}")
    png = render_chart(sym, bars15)
    out = f"chart_{sym}.png"
    with open(out, "wb") as f:
        f.write(png)
    print(f"PNG: {len(png)} байт → {out}")
    print("-" * 60)
    print(chart_caption(sym, bars15))
