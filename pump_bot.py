#!/usr/bin/env python3
"""
Простой скрипт: смотрит спот-пары USDT на Binance, ищет резкий рост
(объём + цена) и шлёт сигнал в Telegram. Без сервера, без базы данных —
просто один запуск = одна проверка. Расписание задаёт GitHub Actions.

Настройки — через переменные окружения (см. .github/workflows/pump-scan.yml):
    TELEGRAM_BOT_TOKEN   - токен бота от @BotFather
    TELEGRAM_CHAT_ID     - куда слать сообщения
    MIN_VOLUME_RATIO     - во сколько раз объём последней свечи должен
                           превышать средний объём (по умолчанию 1.8)
    MIN_PRICE_CHANGE_PCT - минимальный рост цены за последнюю свечу, %
                           (по умолчанию 3.0)
    MIN_QUOTE_VOLUME_USDT- минимальный суточный оборот в USDT, чтобы
                           не ловить мусорные низколиквидные монеты
                           (по умолчанию 1_000_000)
    INTERVAL             - таймфрейм свечей: 15m / 30m / 1h (по умолчанию 15m)
"""

import os
import sys
import time
import concurrent.futures as cf
import urllib.request
import json

BINANCE_BASE = "https://api.binance.com"

MIN_VOLUME_RATIO = float(os.environ.get("MIN_VOLUME_RATIO", "1.8"))
MIN_PRICE_CHANGE_PCT = float(os.environ.get("MIN_PRICE_CHANGE_PCT", "3.0"))
MIN_QUOTE_VOLUME_USDT = float(os.environ.get("MIN_QUOTE_VOLUME_USDT", "1000000"))
INTERVAL = os.environ.get("INTERVAL", "15m")
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "20"))

# Монеты, которые не хотим сканировать: стейблы и плечевые токены
EXCLUDE_SUBSTRINGS = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
STABLECOINS = {"USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "USDPUSDT", "DAIUSDT", "EURUSDT"}


def http_get_json(url: str, timeout: int = 10):
    req = urllib.request.Request(url, headers={"User-Agent": "pump-scan/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_usdt_universe():
    """Все торгуемые спот-пары к USDT, кроме плечевых токенов и стейблов."""
    info = http_get_json(f"{BINANCE_BASE}/api/v3/exchangeInfo")
    symbols = []
    for s in info["symbols"]:
        symbol = s["symbol"]
        if (
            s.get("status") == "TRADING"
            and s.get("quoteAsset") == "USDT"
            and s.get("isSpotTradingAllowed", True)
            and symbol not in STABLECOINS
            and not any(x in symbol for x in EXCLUDE_SUBSTRINGS)
        ):
            symbols.append(symbol)
    return symbols


def get_24h_quote_volumes():
    """Быстрый предварительный фильтр по суточному обороту (один запрос на всех)."""
    data = http_get_json(f"{BINANCE_BASE}/api/v3/ticker/24hr")
    return {d["symbol"]: float(d["quoteVolume"]) for d in data}


def check_symbol(symbol: str):
    """Тянет последние свечи и решает, похоже ли это на памп."""
    url = (
        f"{BINANCE_BASE}/api/v3/klines?symbol={symbol}"
        f"&interval={INTERVAL}&limit=21"
    )
    try:
        klines = http_get_json(url, timeout=8)
    except Exception:
        return None

    if len(klines) < 21:
        return None

    # kline: [openTime, open, high, low, close, volume, closeTime, ...]
    closed = klines[:-1]  # последняя свеча может быть ещё не закрыта
    last = closed[-1]
    prev20 = closed[:-1]

    last_open = float(last[1])
    last_close = float(last[4])
    last_volume = float(last[5])

    avg_volume = sum(float(c[5]) for c in prev20) / len(prev20)
    if avg_volume <= 0:
        return None

    volume_ratio = last_volume / avg_volume
    price_change_pct = (last_close - last_open) / last_open * 100

    is_green = last_close > last_open
    passes = (
        is_green
        and volume_ratio >= MIN_VOLUME_RATIO
        and price_change_pct >= MIN_PRICE_CHANGE_PCT
    )
    if not passes:
        return None

    return {
        "symbol": symbol,
        "price": last_close,
        "change_pct": price_change_pct,
        "volume_ratio": volume_ratio,
    }


def send_telegram(token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps(
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def format_alert(hit: dict) -> str:
    base = hit["symbol"].replace("USDT", "")
    return (
        f"🚀 <b>{base}/USDT</b>\n"
        f"Цена: {hit['price']:.6g}\n"
        f"Рост за свечу ({INTERVAL}): +{hit['change_pct']:.2f}%\n"
        f"Объём: x{hit['volume_ratio']:.2f} от среднего"
    )


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("Ошибка: задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID", file=sys.stderr)
        sys.exit(1)

    started = time.time()
    universe = get_usdt_universe()
    volumes = get_24h_quote_volumes()

    candidates = [
        s for s in universe if volumes.get(s, 0) >= MIN_QUOTE_VOLUME_USDT
    ]
    print(f"Пар в вселенной: {len(universe)}, после фильтра по объёму: {len(candidates)}")

    hits = []
    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for result in pool.map(check_symbol, candidates):
            if result:
                hits.append(result)

    hits.sort(key=lambda h: h["change_pct"], reverse=True)

    print(f"Найдено сигналов: {len(hits)}, время: {time.time() - started:.1f}с")

    for hit in hits:
        try:
            send_telegram(token, chat_id, format_alert(hit))
            print(f"Отправлено: {hit['symbol']}")
        except Exception as e:
            print(f"Ошибка отправки {hit['symbol']}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
