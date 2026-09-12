#!/usr/bin/env python3
"""
Предварительная проверка ордеров бота на РЕАЛЬНОЙ бирже — без создания ордеров.

Что проверяет: обе ноги защиты позиции (тейк-профит LIMIT_MAKER и стоп-лосс
STOP_LOSS), а также ордера входа (рыночный и лимитный на откате).
Параметры строит ТА ЖЕ функция `pump_bot.build_oco_legs`, что и боевое
выставление, поэтому проверяется ровно то, что уйдёт на биржу, а не копия.

Два режима:
  1) без ключей (по умолчанию) — только публичные данные биржи: реальные
     фильтры (LOT_SIZE/PRICE_FILTER/NOTIONAL) и реальная цена. Ловит
     количество не по шагу, цену не по тику, номинал ниже минимума и
     нарушение правила «LIMIT_MAKER выше рынка».
  2) --live — дополнительно отправляет каждую ногу в POST /api/v3/order/test:
     биржа валидирует ордер (подпись, фильтры, средства) и НЕ создаёт его.
     Требуются API-ключи: BINANCE_API_KEY / BINANCE_API_SECRET в окружении
     или .env, либо в bot_state.json.

ВАЖНО: этот инструмент не умеет создавать ордера. Единственный торговый
эндпоинт, который он вызывает — /api/v3/order/test.

    python tools/validate_orders.py
    python tools/validate_orders.py --symbols SOL,ADA,DOGE --notional 15
    python tools/validate_orders.py --live
"""
import argparse
import os
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import pump_bot as pb

TEST_ENDPOINT = "/api/v3/order/test"


def derive_qty(symbol: str, price: float, filters: dict, notional: float) -> float:
    """Количество под заданный номинал, округлённое вниз по шагу биржи."""
    if price <= 0:
        return 0.0
    return float(pb.fmt_qty_filter(notional / price, filters.get("step_size", 1.0)))


def local_checks(legs: dict, price: float, filters: dict) -> list:
    """Проверки, которые можно сделать без ключей — на реальных фильтрах."""
    step = Decimal(str(filters.get("step_size", 0)))
    tick = Decimal(str(filters.get("tick_size", 0)))
    min_qty = Decimal(str(filters.get("min_qty", 0) or 0))
    min_notional = Decimal(str(filters.get("min_notional", 0) or 0))
    qty = Decimal(legs["tp"]["quantity"])
    tp_price = Decimal(legs["tp"]["price"])
    sl_stop = Decimal(legs["sl"]["stopPrice"])
    dump = Decimal(str(price))

    return [
        ("количество кратно stepSize", step > 0 and qty % step == 0, f"{qty} mod {step} = {qty % step}"),
        ("количество >= minQty", qty >= min_qty, f"{qty} >= {min_qty}"),
        ("номинал TP >= minNotional", qty * tp_price >= min_notional,
         f"{qty * tp_price} >= {min_notional}"),
        ("цена TP кратна tickSize", tick > 0 and tp_price % tick == 0,
         f"{tp_price} mod {tick} = {tp_price % tick}"),
        ("стоп кратен tickSize", tick > 0 and sl_stop % tick == 0,
         f"{sl_stop} mod {tick} = {sl_stop % tick}"),
        ("TP выше рынка (LIMIT_MAKER)", dump <= 0 or tp_price > dump, f"TP {tp_price} > рынок {dump}"),
        ("стоп ниже рынка", dump <= 0 or sl_stop < dump, f"стоп {sl_stop} < рынок {dump}"),
        ("TP выше стопа", tp_price > sl_stop, f"{tp_price} > {sl_stop}"),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="SOLUSDT,ADAUSDT,DOGEUSDT",
                    help="пары через запятую (можно базовые тикеры: SOL,ADA)")
    ap.add_argument("--notional", type=float, default=15.0,
                    help="USDT на которые считаем тестовое количество")
    ap.add_argument("--tp", type=float, default=pb.DEFAULT_TAKE_PROFIT)
    ap.add_argument("--sl", type=float, default=pb.DEFAULT_STOP_LOSS)
    ap.add_argument("--live", action="store_true",
                    help="дополнительно отправить ноги в /api/v3/order/test")
    args = ap.parse_args()

    state = pb.load_state()
    keys_ready = all(pb.get_api_credentials(state))
    key_source = ("bot_state.json" if state.get("settings", {}).get("binance_api_key")
                  else "окружение (env)") if keys_ready else "нет"

    print("=" * 78)
    print(f"ПРОВЕРКА ОРДЕРОВ  TP={args.tp}%  SL={args.sl}%  "
          f"номинал≈{args.notional} USDT")
    print(f"Режим: {'БОЕВОЙ (order/test) ' if args.live else 'локальный (без ключей) '}"
          f"| ключи: {'есть из ' + key_source if keys_ready else 'НЕТ'} "
          f"| эндпоинт: {TEST_ENDPOINT if args.live else 'не вызывается'}")
    print("=" * 78)

    if args.live and not keys_ready:
        print("⚠️  Ключей нет (BINANCE_API_KEY / BINANCE_API_SECRET), "
              "боевой режим невозможен — выполняю локальные проверки.")

    symbols = []
    for raw in args.symbols.split(","):
        symbols.append(pb.normalize_symbol(raw.strip()))

    # Балансы: без монеты на счету SELL-ноги проверить нельзя — биржа ответит
    # -2010 «insufficient balance», и это НЕ ошибка наших параметров, а
    # отсутствие позиции. Такие ноги помечаем как непроверенные, не как провал.
    balances = {}
    if keys_ready:
        balances, bal_err = pb.get_spot_balances(state)
        if bal_err:
            print(f"⚠️  Не удалось получить балансы: {bal_err}")
        else:
            held_str = ", ".join(f"{k}={v:g}" for k, v in sorted(
                (k, v) for k, v in balances.items() if v > 0)[:12])
            print(f"На счету: {held_str or 'пусто'}")

    total_failed = 0
    for symbol in symbols:
        print(f"\n── {symbol} " + "─" * (60 - len(symbol)))
        filters = pb.get_symbol_filters(symbol)
        if not filters.get("step_size"):
            print("  ❌ фильтры символа недоступны — сделка по нему в боте отменяется")
            total_failed += 1
            continue
        if filters.get("status") != "TRADING":
            print(f"  ❌ статус {filters.get('status')} — бот откажется торговать")
            total_failed += 1
            continue

        price = pb.get_price(symbol) or 0.0
        if price <= 0:
            print("  ❌ не удалось получить цену")
            total_failed += 1
            continue

        # Количество: от реального баланса, если он есть, иначе от номинала
        qty = 0.0
        held = balances.get(pb.base_asset(symbol), 0.0) if balances else 0.0
        if held > 0:
            qty = held
        if qty <= 0:
            qty = derive_qty(symbol, price, filters, args.notional)

        print(f"  фильтры: step={filters['step_size']} tick={filters['tick_size']} "
              f"minQty={filters.get('min_qty')} minNotional={filters.get('min_notional')}"
              f"{' (stale)' if filters.get('stale') else ''}")
        print(f"  рынок: {price}   тестовое количество: {qty}")

        legs, reason = pb.build_oco_legs(symbol, price, qty, args.tp, args.sl, filters, price)
        if not legs:
            print(f"  ❌ ноги не построены: {reason}")
            total_failed += 1
            continue

        for name, order in (("тейк-профит (верхняя нога OCO)", legs["tp"]),
                            ("стоп-лосс (нижняя нога OCO)", legs["sl"])):
            print(f"\n  {name}:")
            print(f"    {order}")

        print("\n  локальные проверки:")
        for title, ok, detail in local_checks(legs, price, filters):
            print(f"    {'✅' if ok else '❌'} {title}: {detail}")
            if not ok:
                total_failed += 1

        entry_legs = {
            "вход по рынку": {"symbol": symbol, "side": "BUY", "type": "MARKET",
                              "quoteOrderQty": f"{args.notional:.2f}"},
            "вход на откате -0.4%": {
                "symbol": symbol, "side": "BUY", "type": "LIMIT", "timeInForce": "GTC",
                "quantity": legs["tp"]["quantity"],
                "price": pb.fmt_price_filter(price * (1 - pb.DEFAULT_ENTRY_PULLBACK / 100.0),
                                             filters["tick_size"])},
        }

        if args.live and keys_ready:
            have_position = held > 0
            print("\n  ответы биржи на order/test (ордера НЕ создаются):")
            if not have_position:
                print(f"    ⏭ TP  LIMIT_MAKER и SL  STOP_LOSS — не проверены: "
                      f"на счету нет {pb.base_asset(symbol)}.")
                print(f"       SELL без позиции биржа отвергает кодом -2010, "
                      f"это не дефект параметров ордера, а отсутствие монеты.")
            legs_to_send = [("TP  LIMIT_MAKER", legs["tp"]),
                            ("SL  STOP_LOSS", legs["sl"])] if have_position else []
            for label, order in legs_to_send + [
                    ("BUY MARKET", entry_legs["вход по рынку"]),
                    ("BUY LIMIT (откат)", entry_legs["вход на откате -0.4%"])]:
                res = pb.binance_signed_request("POST", TEST_ENDPOINT, order, state=state)
                if "error" in res:
                    code = res.get("code")
                    # -2010 на SELL — не дефект параметров ордера, а нехватка
                    # свободного баланса монеты (нет на счету либо заблокирована
                    # под другим ордером). Такое не считаем провалом проверки.
                    if code == -2010 and label.startswith(("TP", "SL")):
                        print(f"    ⏭ {label}: code={code} {res['error']}")
                        print(f"       — нехватка свободного {pb.base_asset(symbol)}: "
                              f"не проверено, параметры ордера тут ни при чём")
                        continue
                    print(f"    ❌ {label}: code={code} {res['error']}")
                    print(f"       параметры: {order}")
                    total_failed += 1
                else:
                    print(f"    ✅ {label}: принято биржей ({res or '{}'})")

    print("\n" + "=" * 78)
    print(f"ИТОГ: {'все проверки пройдены' if total_failed == 0 else f'проблем: {total_failed}'}")
    if not args.live:
        print("Боевая проверка не выполнялась. С ключами: python tools/validate_orders.py --live")
    print("=" * 78)
    return 1 if total_failed else 0


if __name__ == "__main__":
    sys.exit(main())
