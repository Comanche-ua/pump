#!/usr/bin/env python3
"""
Регрессионные тесты на денежные пути pump_bot.

Проверяют ровно те дефекты, из-за которых позиция могла остаться без
присмотра или состояние терялось. Сеть не используется: все обращения
к Binance и Telegram подменяются заглушками.

    python tests/test_trade_safety.py
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pump_bot as pb


def make_trade(**over):
    trade = {
        "symbol": "TESTUSDT", "base": "TEST",
        "buy_price": 100.0, "qty": 10.0, "cost_usdt": 1000.0,
        "tp_price": 102.0, "tp_pct": 2.0, "sl_price": 98.0,
        "trailing_sl": 0.0, "highest_price": 100.0,
        "tp_order_id": 555, "status": "holding",
        "trailing_activation_pct": 1.0, "trailing_distance_pct": 0.8,
    }
    trade.update(over)
    return trade


def make_5m_candles(n=600):
    """Детерминированная синтетика 5m: цена по синусоиде, объём волной."""
    import math as _m
    out = []
    t0 = 1_700_000_000_000
    for i in range(n):
        base = 100.0 + 5.0 * _m.sin(i / 25.0) + 0.02 * i
        o = base
        cl = base + 0.3 * _m.sin(i / 3.0)
        hi = max(o, cl) + 0.4
        lo = min(o, cl) - 0.4
        vol = 1000.0 + 400.0 * _m.sin(i / 7.0)
        out.append(pb.Candle(
            open_time=t0 + i * 300_000, open=o, high=hi, low=lo, close=cl,
            volume=vol, quote_volume=vol * cl, trades=int(vol / 10),
            taker_buy_base=vol * 0.55, close_time=t0 + i * 300_000 + 299_999,
        ))
    return out


class TradeSafetyTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self._old_state_file = pb.STATE_FILE
        pb.STATE_FILE = self.tmp.name
        self.sent = []
        self._old_send = pb.send_telegram
        self._old_price = pb.get_price
        self._old_prices = pb.get_multiple_prices
        self._old_filters = pb.get_symbol_filters
        self._old_fetch = pb.fetch_klines
        self._old_emergency = pb.execute_emergency_market_sell
        pb.send_telegram = lambda token, chat, text, **kw: self.sent.append(text)
        self.price = 99.0
        pb.get_price = lambda sym: self.price
        pb.get_multiple_prices = lambda syms: {s: self.price for s in syms}
        pb._symbol_filters_cache.clear()
        pb._symbol_filters_cache_at.clear()

    def tearDown(self):
        pb.STATE_FILE = self._old_state_file
        pb.send_telegram = self._old_send
        pb.get_price = self._old_price
        pb.get_multiple_prices = self._old_prices
        pb.get_symbol_filters = self._old_filters
        pb.fetch_klines = self._old_fetch
        pb.execute_emergency_market_sell = self._old_emergency
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)
        if os.path.exists(self.tmp.name + ".tmp"):
            os.unlink(self.tmp.name + ".tmp")

    # ── состояние ──────────────────────────────────────────────

    def test_pending_entries_survive_restart(self):
        """Живой лимитный вход не должен теряться при рестарте бота."""
        state = pb.default_state()
        state["pending_entries"]["TESTUSDT"] = {"order_id": 777, "target_price": 99.6}
        pb.save_state(state)

        restored = pb.load_state()
        self.assertIn("TESTUSDT", restored["pending_entries"])
        self.assertEqual(restored["pending_entries"]["TESTUSDT"]["order_id"], 777)

    def test_pruning_keeps_dict_identity(self):
        """
        Чистка старых алертов должна мутировать словарь на месте: если
        переприсвоить его, записи из других потоков уйдут в выброшенный dict.
        """
        state = pb.default_state()
        state["sent_alerts"]["OLD"] = 1
        state["symbol_alert_cooldown"]["OLD"] = 1
        held = state["sent_alerts"]          # ссылка, как у другого потока
        pb.save_state(state)
        pb.STATE_FILE  # noqa: B018 — файл нам тут не нужен, важен сам факт чистки
        self.assertIs(state["sent_alerts"], held)
        self.assertNotIn("OLD", held)

    # ── отменённый тейк-профит ─────────────────────────────────

    def test_canceled_tp_keeps_position_monitored(self):
        """TP отменён на бирже → позицию нельзя молча снимать с мониторинга."""
        state = pb.default_state()
        state["active_trades"]["TESTUSDT"] = make_trade()
        pb.binance_signed_request = lambda m, p, params, **kw: {"status": "CANCELED"}

        pb.check_active_trades("tok", "1", state)

        self.assertIn("TESTUSDT", state["active_trades"],
                      "позиция выпала из мониторинга после отмены TP")
        self.assertIsNone(state["active_trades"]["TESTUSDT"]["tp_order_id"])
        self.assertEqual(state["active_trades"]["TESTUSDT"]["status"], "tp_order_canceled")

    # ── проваленный стоп-лосс ──────────────────────────────────

    def test_failed_stop_loss_keeps_position(self):
        """Неудачная продажа по стопу → позиция остаётся под мониторингом."""
        state = pb.default_state()
        state["active_trades"]["TESTUSDT"] = make_trade()
        pb.binance_signed_request = lambda m, p, params, **kw: {"status": "NEW"}
        self.price = 97.0                                  # ниже стопа 98
        pb.execute_emergency_market_sell = lambda *a, **kw: {"error": "NOTIONAL too small"}

        pb.check_active_trades("tok", "1", state)

        self.assertIn("TESTUSDT", state["active_trades"],
                      "позиция брошена после проваленной продажи по стопу")
        self.assertEqual(state["active_trades"]["TESTUSDT"]["status"], "exit_failed")
        self.assertTrue(any("STOP-LOSS НЕ СРАБОТАЛ" in m for m in self.sent))

    def test_successful_stop_loss_closes_position(self):
        """Успешная продажа по стопу → позиция закрывается штатно."""
        state = pb.default_state()
        state["active_trades"]["TESTUSDT"] = make_trade()
        pb.binance_signed_request = lambda m, p, params, **kw: {"status": "NEW"}
        self.price = 97.0
        pb.execute_emergency_market_sell = lambda *a, **kw: {"success": True, "pnl": -30.0}

        pb.check_active_trades("tok", "1", state)

        self.assertNotIn("TESTUSDT", state["active_trades"])

    # ── защита от дублей ───────────────────────────────────────

    def test_auto_trade_rejects_symbol_with_pending_entry(self):
        """Повторный сигнал не должен затирать живой лимитный ордер."""
        state = pb.default_state()
        state["pending_entries"]["TESTUSDT"] = {"order_id": 777}
        sig = pb.PumpSignal(
            symbol="TESTUSDT", base="TEST", price=100.0, change_24h=1.0,
            quote_volume_24h=2_000_000.0, high_24h=101.0, low_24h=99.0,
            btc_relative_24h=0.5, best_tf="5m", best_score=80.0,
            grade="strong", alert_key="k", by_tf=[],
        )
        res = pb.execute_pump_auto_trade("tok", "1", state, sig)
        self.assertIn("error", res)
        self.assertEqual(state["pending_entries"]["TESTUSDT"]["order_id"], 777)

    def test_pending_entry_counts_toward_slot_limit(self):
        """Ожидающий вход занимает слот, иначе можно выставить лишние ордера."""
        state = pb.default_state()
        state["settings"]["max_open_trades"] = 1
        state["settings"]["trade_mode"] = "fixed"
        state["settings"]["trade_amount_usdt"] = 11.0
        state["pending_entries"]["OTHERUSDT"] = {"order_id": 1}
        state["settings"]["binance_api_key"] = "k"
        state["settings"]["binance_api_secret"] = "s"
        sig = pb.PumpSignal(
            symbol="NEWUSDT", base="NEW", price=1.0, change_24h=1.0,
            quote_volume_24h=2_000_000.0, high_24h=1.01, low_24h=0.99,
            btc_relative_24h=0.5, best_tf="5m", best_score=80.0,
            grade="strong", alert_key="k", by_tf=[],
        )
        res = pb.execute_pump_auto_trade("tok", "1", state, sig)
        self.assertIn("лимит", res.get("error", "").lower())

    # ── сверка с биржей после рестарта ─────────────────────────

    def test_reconcile_detects_filled_pending_entry(self):
        """Исполненный во время простоя лимитный вход обязан стать видимым."""
        state = pb.default_state()
        state["settings"]["binance_api_key"] = "k"
        state["settings"]["binance_api_secret"] = "s"
        state["pending_entries"]["TESTUSDT"] = {"order_id": 777}
        pb.binance_signed_request = lambda m, p, params, **kw: {"status": "FILLED"}

        pb.reconcile_state_with_exchange("tok", "1", state)

        self.assertTrue(any("без тейк-профита" in m for m in self.sent),
                        f"нет предупреждения об исполненном входе: {self.sent}")

    def test_reconcile_drops_canceled_pending_entry(self):
        """Отменённый лимитный вход убирается из ожидания."""
        state = pb.default_state()
        state["settings"]["binance_api_key"] = "k"
        state["settings"]["binance_api_secret"] = "s"
        state["pending_entries"]["TESTUSDT"] = {"order_id": 777}
        pb.binance_signed_request = lambda m, p, params, **kw: {"status": "CANCELED"}

        pb.reconcile_state_with_exchange("tok", "1", state)

        self.assertNotIn("TESTUSDT", state["pending_entries"])

    def test_reconcile_keeps_pending_entry_on_network_error(self):
        """Ошибка сети не должна удалять запись о живом ордере."""
        state = pb.default_state()
        state["settings"]["binance_api_key"] = "k"
        state["settings"]["binance_api_secret"] = "s"
        state["pending_entries"]["TESTUSDT"] = {"order_id": 777}
        pb.binance_signed_request = lambda m, p, params, **kw: {"error": "timeout"}

        pb.reconcile_state_with_exchange("tok", "1", state)

        self.assertIn("TESTUSDT", state["pending_entries"])

    def test_reconcile_skips_without_api_keys(self):
        """Без ключей сверка не должна ничего трогать."""
        state = pb.default_state()
        state["pending_entries"]["TESTUSDT"] = {"order_id": 777}
        pb.reconcile_state_with_exchange("tok", "1", state)
        self.assertIn("TESTUSDT", state["pending_entries"])
        self.assertEqual(self.sent, [])

    # ── инварианты денежных дефолтов (из бэктеста) ─────────────

    def test_stop_loss_is_wider_than_take_profit(self):
        """
        ГЛАВНЫЙ инвариант: стоп обязан быть шире цели. Подтверждено на трёх
        независимых выборках — узкий стоп выбивается шумом, после которого
        цена возвращается, и переводит плюсовую сделку в минус. Симметричный
        TP=SL=2% и любые узкие стопы значимо убыточны.
        """
        self.assertGreater(
            pb.DEFAULT_STOP_LOSS, pb.DEFAULT_TAKE_PROFIT,
            "стоп должен быть ШИРЕ цели, иначе конфигурация убыточна по бэктесту",
        )

    def test_trailing_does_not_preempt_take_profit(self):
        """
        Трейлинг не должен срабатывать раньше лимитного TP: эффективный стоп
        = max(sl_price, trailing_sl), поэтому активация трейлинга обязана быть
        выше цели — иначе реальный выход задаёт трейлинг, а не TP.
        """
        self.assertGreater(
            pb.DEFAULT_TRAILING_ACTIVATION, pb.DEFAULT_TAKE_PROFIT,
            "активация трейлинга ниже TP — трейлинг перебьёт тейк-профит",
        )

    def test_defaults_are_visible_in_state(self):
        """Дефолты должны быть явно в state, а не только в fallback-константах."""
        s = pb.default_state()["settings"]
        self.assertEqual(s["take_profit_pct"], pb.DEFAULT_TAKE_PROFIT)
        self.assertEqual(s["stop_loss_pct"], pb.DEFAULT_STOP_LOSS)
        self.assertEqual(s["trailing_activation_pct"], pb.DEFAULT_TRAILING_ACTIVATION)

    def test_old_default_take_profit_is_migrated(self):
        """Сохранённый TP=2.0% — это старый дефолт, а не выбор; мигрируем."""
        import json as _json
        with open(pb.STATE_FILE, "w", encoding="utf-8") as f:
            _json.dump({"settings": {"take_profit_pct": 2.0, "auto_trade": True}}, f)
        s = pb.load_state()
        self.assertEqual(s["settings"]["take_profit_pct"], pb.DEFAULT_TAKE_PROFIT)
        self.assertEqual(s["settings"]["stop_loss_pct"], pb.DEFAULT_STOP_LOSS)
        self.assertTrue(s["settings"]["auto_trade"], "миграция не должна трогать другие настройки")

    def test_manual_take_profit_is_preserved(self):
        """Осознанно выставленный TP миграция трогать не имеет права."""
        import json as _json
        with open(pb.STATE_FILE, "w", encoding="utf-8") as f:
            _json.dump({"settings": {"take_profit_pct": 4.5}}, f)
        s = pb.load_state()
        self.assertEqual(s["settings"]["take_profit_pct"], 4.5)

    # ── однопоточность торговых переходов ──────────────────────

    def test_concurrent_tp_handling_is_skipped(self):
        """
        Монитор и главный поток не должны обрабатывать один и тот же TP
        одновременно: это задваивало бы PnL в истории сделок.
        """
        state = pb.default_state()
        state["active_trades"]["TESTUSDT"] = make_trade()
        calls = []

        def fake(method, endpoint, params, **kw):
            calls.append(endpoint)
            return {"status": "FILLED"}

        pb.binance_signed_request = fake
        pb._ACTIVE_LOCK.acquire()
        try:
            pb.check_active_trades("tok", "1", state)
        finally:
            pb._ACTIVE_LOCK.release()

        self.assertEqual(calls, [], "второй поток всё равно пошёл на биржу")
        self.assertIn("TESTUSDT", state["active_trades"])
        self.assertEqual(state["trade_history"], [], "история не должна пополняться дважды")

    def test_concurrent_pending_handling_is_skipped(self):
        """Переход «ожидание → активная» тоже должен быть однопоточным."""
        state = pb.default_state()
        state["pending_entries"]["TESTUSDT"] = {"order_id": 777}
        calls = []
        pb.binance_signed_request = lambda m, e, p, **kw: calls.append(e) or {"status": "NEW"}

        pb._PENDING_LOCK.acquire()
        try:
            pb.check_pending_entries("tok", "1", state)
        finally:
            pb._PENDING_LOCK.release()

        self.assertEqual(calls, [])
        self.assertIn("TESTUSDT", state["pending_entries"])

    def test_concurrent_sync_is_skipped(self):
        """
        Синхронизация переписывает записи active_trades целиком, поэтому
        параллельный запуск затирал бы свежие данные чужой копией.
        """
        state = pb.default_state()
        state["settings"].update({"binance_api_key": "k", "binance_api_secret": "s"})
        calls = []
        pb.binance_signed_request = lambda m, e, p, **kw: calls.append(e) or {}

        pb._SYNC_LOCK.acquire()
        try:
            pb.sync_trades_and_active_positions(state)
        finally:
            pb._SYNC_LOCK.release()

        self.assertEqual(calls, [])

    def test_stale_slot_reservation_is_cleaned(self):
        """Бронь слота без ордера (сорвавшийся вход) не должна висеть вечно."""
        state = pb.default_state()
        state["pending_entries"]["TESTUSDT"] = {"order_id": None, "reserved_at": int(time.time())}
        pb.check_pending_entries("tok", "1", state)
        self.assertNotIn("TESTUSDT", state["pending_entries"])

    def test_state_lock_is_reentrant(self):
        """
        portfolio_add мутирует состояние и сам вызывает save_state, который
        тоже берёт STATE_LOCK — обычный Lock дал бы самоблокировку.
        """
        state = pb.default_state()
        pb.STATE_LOCK.acquire()
        try:
            pb.portfolio_add(state, "TESTUSDT", 1.0, 100.0)
        finally:
            pb.STATE_LOCK.release()
        self.assertIn("TESTUSDT", state["portfolio"])

    # ── округление количества под фильтры биржи ────────────────

    def test_quantity_is_exact_multiple_of_step(self):
        """
        Количество обязано быть строго кратно stepSize: арифметика на float
        накапливает ошибку представления, биржа отвечает LOT_SIZE и ордер
        не выставляется вообще.
        """
        from decimal import Decimal
        for val, step in ((12.3456789, 0.001), (1234.9999, 0.01),
                          (0.999999, 0.0001), (5.55555, 0.00001), (100.0, 1.0)):
            s = pb.fmt_qty_filter(val, step)
            self.assertEqual(
                Decimal(s) % Decimal(str(step)), 0,
                f"{val}/{step}: {s} не кратно шагу",
            )

    def test_quantity_never_exceeds_requested(self):
        """Округление вниз: нельзя продать больше, чем есть на балансе."""
        for val, step in ((12.3459, 0.001), (0.9999999, 0.0001), (7.999, 1.0)):
            self.assertLessEqual(float(pb.fmt_qty_filter(val, step)), val + 1e-12)

    def test_tiny_step_does_not_collapse_to_integer(self):
        """
        Регрессия: у шагов мельче 1e-10 прежний расчёт числа знаков давал 0,
        и количество превращалось в целое (12.345 → 12).
        """
        self.assertEqual(pb.fmt_qty_filter(12.345, 1e-12), "12.345000000000")

    def test_price_rounds_to_tick(self):
        from decimal import Decimal
        for val, tick in ((77.123456789, 0.01), (0.00000123456, 0.00000001)):
            s = pb.fmt_price_filter(val, tick)
            self.assertEqual(Decimal(s) % Decimal(str(tick)), 0, f"{val}/{tick}: {s}")

    # ── синхронизация не должна терять то, что знает бот ───────

    def _sync_with_broken_open_orders(self, state):
        def fake(method, endpoint, params=None, **kw):
            if endpoint == "/api/v3/openOrders":
                return {"error": "timeout", "code": -1001}
            if endpoint == "/api/v3/account":
                return {"balances": [{"asset": "TEST", "free": "10", "locked": "0"}]}
            if endpoint == "/api/v3/myTrades":
                return []
            return {}

        pb.binance_signed_request = fake
        state["settings"].update({"binance_api_key": "k", "binance_api_secret": "s"})
        pb.sync_trades_and_active_positions(state)

    def test_sync_keeps_tp_order_id_when_open_orders_fail(self):
        """
        Сбой запроса openOrders НЕ означает «ордеров нет». Раньше это стирало
        tp_order_id, и позиция выглядела незащищённой, хотя TP жил на бирже.
        """
        state = pb.default_state()
        state["active_trades"]["TESTUSDT"] = make_trade(tp_order_id=555)
        self._sync_with_broken_open_orders(state)
        self.assertEqual(state["active_trades"]["TESTUSDT"]["tp_order_id"], 555)

    def test_sync_does_not_lower_trailing_peak(self):
        """Пик движения не понижаем, иначе трейлинг теряет максимум."""
        state = pb.default_state()
        state["active_trades"]["TESTUSDT"] = make_trade(highest_price=120.0)
        self._sync_with_broken_open_orders(state)
        self.assertEqual(state["active_trades"]["TESTUSDT"]["highest_price"], 120.0)

    def test_sync_preserves_trailing_config_and_exit_markers(self):
        """
        Полная перезапись записи теряла настройки трейлинга и метку времени
        последнего алерта о несработавшем стопе — алерты начинали дублироваться.
        """
        state = pb.default_state()
        state["active_trades"]["TESTUSDT"] = make_trade(
            trailing_activation_pct=1.7, trailing_distance_pct=0.4,
            exit_alert_at=12345, status="exit_failed")
        self._sync_with_broken_open_orders(state)
        rec = state["active_trades"]["TESTUSDT"]
        self.assertEqual(rec["trailing_activation_pct"], 1.7)
        self.assertEqual(rec["trailing_distance_pct"], 0.4)
        self.assertEqual(rec["exit_alert_at"], 12345)
        self.assertEqual(rec["status"], "exit_failed")

    def test_sync_stop_uses_settings_not_hardcoded_2pct(self):
        """
        Раньше при пересборке записи стоп брался как buy*0.98 и sl_pct=2.0,
        что молча противоречило новому дефолту 3%.
        """
        state = pb.default_state()
        state["settings"]["stop_loss_pct"] = 3.0
        state["active_trades"]["TESTUSDT"] = make_trade()
        state["active_trades"]["TESTUSDT"].pop("sl_price", None)
        state["active_trades"]["TESTUSDT"].pop("sl_pct", None)
        self._sync_with_broken_open_orders(state)
        rec = state["active_trades"]["TESTUSDT"]
        self.assertAlmostEqual(rec["sl_pct"], 3.0)
        self.assertAlmostEqual(rec["sl_price"], 100.0 * 0.97)

    # ── фильтры биржи ──────────────────────────────────────────

    def test_filters_not_fabricated_on_failure(self):
        """
        При сбое сети фильтры НЕ выдумываются: по выдуманному stepSize ордер
        отвергается биржей. Пустой словарь заставляет отказаться от сделки.
        """
        old_url = pb.BINANCE_TRADE_URL
        pb.BINANCE_TRADE_URL = "http://127.0.0.1:9"
        try:
            self.assertEqual(pb.get_symbol_filters("TESTUSDT"), {})
        finally:
            pb.BINANCE_TRADE_URL = old_url

    def test_filters_cache_refetches_after_ttl(self):
        """Вечный кэш скрывал смену шага и статуса символа (BREAK/halt)."""
        calls = []

        class FakeResp:
            def read(self):
                return json.dumps({"symbols": [{
                    "status": "TRADING",
                    "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                                {"filterType": "NOTIONAL", "minNotional": "5"}],
                }]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        old_urlopen = pb.urllib.request.urlopen
        old_ttl = pb.FILTERS_TTL_SEC
        pb.urllib.request.urlopen = lambda *a, **kw: calls.append(1) or FakeResp()
        try:
            pb.FILTERS_TTL_SEC = 3600.0
            first = pb.get_symbol_filters("TESTUSDT")
            pb.get_symbol_filters("TESTUSDT")
            self.assertEqual(len(calls), 1, "кэш должен переиспользоваться в пределах TTL")
            self.assertEqual(first["step_size"], 0.001)

            pb.FILTERS_TTL_SEC = 0.0
            pb.get_symbol_filters("TESTUSDT")
            self.assertEqual(len(calls), 2, "после TTL фильтры должны перечитываться")
        finally:
            pb.urllib.request.urlopen = old_urlopen
            pb.FILTERS_TTL_SEC = old_ttl

    # ── частичное исполнение входа ─────────────────────────────

    def _pending_entry(self, **over):
        entry = {"order_id": 777, "base": "TEST", "target_price": 99.6,
                 "placed_at": int(time.time()) - 600, "timeout_sec": 300}
        entry.update(over)
        return entry

    def test_partial_fill_at_timeout_becomes_monitored_position(self):
        """
        Раньше частично исполненный вход по таймауту отменялся вместе с уже
        купленными монетами: они оставались на споте без TP и без стопа.
        """
        state = pb.default_state()
        state["pending_entries"]["TESTUSDT"] = self._pending_entry()
        state["settings"].update({"binance_api_key": "k", "binance_api_secret": "s"})
        pb.get_symbol_filters = lambda sym: {"step_size": 0.001, "tick_size": 0.01,
                                             "min_notional": 5.0, "status": "TRADING"}
        pb.fetch_klines = lambda *a, **kw: []

        def fake(method, endpoint, params=None, **kw):
            if endpoint == "/api/v3/order" and method == "GET":
                return {"status": "PARTIALLY_FILLED", "executedQty": "5",
                        "cummulativeQuoteQty": "500", "orderId": 777}
            if endpoint == "/api/v3/order" and method == "DELETE":
                return {"status": "CANCELED", "executedQty": "5",
                        "cummulativeQuoteQty": "500", "orderId": 777}
            if endpoint == "/api/v3/order" and method == "POST":
                return {"orderId": 999}
            if endpoint == "/api/v3/account":
                return {"balances": [{"asset": "USDT", "free": "100", "locked": "0"}]}
            return {}

        pb.binance_signed_request = fake
        pb.check_pending_entries("tok", "1", state)

        self.assertNotIn("TESTUSDT", state["pending_entries"])
        self.assertIn("TESTUSDT", state["active_trades"],
                      "купленные монеты остались без присмотра")
        rec = state["active_trades"]["TESTUSDT"]
        self.assertAlmostEqual(rec["qty"], 5.0)
        self.assertAlmostEqual(rec["buy_price"], 100.0)
        self.assertEqual(rec["tp_order_id"], 999)

    def test_timeout_cancel_failure_keeps_record(self):
        """
        Если отменить ордер не удалось, он может быть ещё жив — выбрасывать
        запись нельзя, иначе возможное исполнение пройдёт мимо бота.
        """
        state = pb.default_state()
        state["pending_entries"]["TESTUSDT"] = self._pending_entry()
        state["settings"].update({"binance_api_key": "k", "binance_api_secret": "s"})

        def fake(method, endpoint, params=None, **kw):
            if method == "DELETE":
                return {"error": "Internal error", "code": -1001}
            return {"status": "NEW", "executedQty": "0"}

        pb.binance_signed_request = fake
        pb.check_pending_entries("tok", "1", state)

        self.assertIn("TESTUSDT", state["pending_entries"])

    # ── биржевой OCO: TP и стоп живут на бирже ─────────────────

    FILTERS = {"step_size": 0.001, "min_qty": 0.001, "tick_size": 0.01,
               "min_notional": 5.0, "status": "TRADING", "stale": False}

    def test_oco_places_both_legs(self):
        """OCO обязан содержать обе ноги: лимитный TP и стоп-лосс."""
        sent = []

        def fake(method, endpoint, params=None, **kw):
            sent.append((endpoint, dict(params or {})))
            return {"orderListId": 42, "orders": [
                {"orderId": 1, "type": "LIMIT_MAKER", "side": "SELL"},
                {"orderId": 2, "type": "STOP_LOSS", "side": "SELL"},
            ]}

        pb.binance_signed_request = fake
        state = pb.default_state()
        oco = pb.place_protection_oco("TESTUSDT", 100.0, 10.0, 1.5, 3.0,
                                      self.FILTERS, state, cur_price=100.0)
        self.assertEqual(oco["order_list_id"], 42)
        self.assertEqual(oco["tp_order_id"], 1)
        self.assertEqual(oco["sl_order_id"], 2)
        self.assertAlmostEqual(oco["tp_price"], 101.5)
        self.assertAlmostEqual(oco["sl_price"], 97.0)
        endpoint, params = sent[0]
        self.assertEqual(endpoint, "/api/v3/orderList/oco")
        self.assertEqual(params["aboveType"], "LIMIT_MAKER")
        self.assertEqual(params["belowType"], "STOP_LOSS")
        self.assertEqual(params["side"], "SELL")

    def test_oco_refused_when_price_already_above_target(self):
        """
        LIMIT_MAKER обязан стоять выше рынка, иначе биржа отвергнет ордер —
        в этом случае функция должна честно отказаться, а не отправить его.
        """
        calls = []
        pb.binance_signed_request = lambda *a, **kw: calls.append(1) or {}
        state = pb.default_state()
        oco = pb.place_protection_oco("TESTUSDT", 100.0, 10.0, 1.5, 3.0,
                                      self.FILTERS, state, cur_price=200.0)
        self.assertEqual(oco, {})
        self.assertEqual(calls, [], "ордер всё равно отправлен на биржу")

    def test_exchange_stop_fill_closes_position(self):
        """
        Стоп-плечо исполнилось на бирже → позиция закрыта, запись в истории,
        повторной продажи процессом быть не должно.
        """
        state = pb.default_state()
        state["active_trades"]["TESTUSDT"] = make_trade(
            sl_order_id=888, order_list_id=777, sl_price=97.0)
        posted = []

        def fake(method, endpoint, params=None, **kw):
            if endpoint == "/api/v3/order":
                return {"status": "FILLED", "cummulativeQuoteQty": "970",
                        "executedQty": "10"}
            if endpoint == "/api/v3/account":
                return {"balances": [{"asset": "USDT", "free": "1000", "locked": "0"}]}
            if method == "POST":
                posted.append((endpoint, dict(params or {})))
            return {}

        pb.binance_signed_request = fake
        self.price = 96.0
        pb.check_active_trades("tok", "1", state)

        self.assertNotIn("TESTUSDT", state["active_trades"])
        self.assertEqual(len(state["trade_history"]), 1)
        rec = state["trade_history"][0]
        self.assertEqual(rec["status"], "oco_stop_loss")
        self.assertAlmostEqual(rec["sell_price"], 97.0)
        self.assertAlmostEqual(rec["pnl"], -30.0)
        self.assertEqual(posted, [], "процесс продал позицию второй раз")
        self.assertTrue(any("СТОП-ЛОСС СРАБОТАЛ НА БИРЖЕ" in m for m in self.sent))

    def test_client_stop_is_skipped_while_exchange_stop_is_live(self):
        """
        Пока живо стоп-плечо OCO, клиентский стоп не должен вмешиваться:
        иначе двойная продажа или отказ по заблокированному балансу.
        """
        state = pb.default_state()
        state["active_trades"]["TESTUSDT"] = make_trade(
            sl_order_id=888, order_list_id=777, sl_price=98.0)
        sells = []

        def fake(method, endpoint, params=None, **kw):
            if endpoint == "/api/v3/order":
                return {"status": "NEW"}
            if endpoint == "/api/v3/orderList":
                sells.append(endpoint)
            return {}

        pb.binance_signed_request = fake
        self.price = 90.0                       # глубоко ниже стопа
        pb.check_active_trades("tok", "1", state)

        self.assertIn("TESTUSDT", state["active_trades"])
        self.assertEqual(sells, [], "процесс вмешался в защиту биржи")

    def test_client_stop_resumes_when_exchange_stop_disappears(self):
        """
        Если стоп-плечо исчезло с биржи, позицию обязан подхватить процесс —
        иначе она останется без защиты вообще.
        """
        state = pb.default_state()
        state["active_trades"]["TESTUSDT"] = make_trade(
            sl_order_id=888, order_list_id=777, sl_price=98.0)
        pb.binance_signed_request = lambda m, e, p=None, **kw: (
            {"status": "CANCELED"} if e == "/api/v3/order" else {})
        pb.check_active_trades("tok", "1", state)
        rec = state["active_trades"]["TESTUSDT"]
        self.assertIsNone(rec["sl_order_id"])
        self.assertIsNone(rec["order_list_id"])

        # теперь клиентский стоп снова работает
        closed = []
        pb.binance_signed_request = lambda m, e, p=None, **kw: {"status": "NEW"}
        pb.execute_emergency_market_sell = lambda *a, **kw: closed.append(1) or {"success": True}
        self.price = 90.0
        pb.check_active_trades("tok", "1", state)
        self.assertEqual(len(closed), 1, "клиентский стоп не сработал")
        self.assertNotIn("TESTUSDT", state["active_trades"])

    def test_emergency_sell_cancels_oco_list_first(self):
        """
        Пока OCO жив, монеты заблокированы под стопом — продажа без снятия
        списка получит отказ по свободному балансу.
        """
        state = pb.default_state()
        state["active_trades"]["TESTUSDT"] = make_trade(
            sl_order_id=888, order_list_id=777, qty=10.0, cost_usdt=1000.0)
        order = []

        def fake(method, endpoint, params=None, **kw):
            order.append((method, endpoint, dict(params or {})))
            if endpoint == "/api/v3/orderList":
                return {"orderListId": 777, "status": "ALL_DONE"}
            if endpoint == "/api/v3/account":
                return {"balances": [{"asset": "TEST", "free": "10", "locked": "0"},
                                     {"asset": "USDT", "free": "0", "locked": "0"}]}
            if method == "POST":
                return {"orderId": 5, "status": "FILLED",
                        "cummulativeQuoteQty": "970", "executedQty": "10"}
            return {}

        pb.binance_signed_request = fake
        pb.get_symbol_filters = lambda sym: dict(self.FILTERS)
        res = pb.execute_emergency_market_sell("tok", "1", state, "TESTUSDT")

        endpoints = [e for _, e, _ in order]
        self.assertIn("/api/v3/orderList", endpoints, "OCO-список не снят перед продажей")
        self.assertLess(endpoints.index("/api/v3/orderList"),
                        endpoints.index("/api/v3/order"),
                        "продажа отправлена раньше снятия списка")
        self.assertTrue(res.get("success"))
        self.assertNotIn("TESTUSDT", state["active_trades"])

    def test_emergency_sell_stops_if_oco_cancel_fails(self):
        """Не сняв список, продавать нельзя — иначе отказ по балансу."""
        state = pb.default_state()
        state["active_trades"]["TESTUSDT"] = make_trade(
            sl_order_id=888, order_list_id=777)
        posts = []

        def fake(method, endpoint, params=None, **kw):
            if endpoint == "/api/v3/orderList":
                return {"error": "Internal error", "code": -1001}
            if method == "POST":
                posts.append(endpoint)
            return {}

        pb.binance_signed_request = fake
        res = pb.execute_emergency_market_sell("tok", "1", state, "TESTUSDT")
        self.assertIn("error", res)
        self.assertEqual(posts, [], "продажа отправлена при живом OCO-списке")
        self.assertIn("TESTUSDT", state["active_trades"])

    def test_oco_enabled_by_default(self):
        """
        Без биржевого OCO позиция остаётся без стопа, пока бот выключен:
        в CI процесс живёт максимум 330 минут, плюс падения и обрывы сети.
        """
        self.assertTrue(pb.default_state()["settings"]["use_oco"])

    def test_take_profit_is_not_below_measured_breakeven_zone(self):
        """
        Цель с учётом отката на входе (1.0% pullback) протестирована по 141 паре:
        Оптимальный диапазон TP — 0.7%..2.0% (чистый винрейт 93.5%+).
        Минимальный порог положительного матожидания — 0.6%.
        """
        self.assertGreaterEqual(
            pb.DEFAULT_TAKE_PROFIT, 0.6,
            "цель ниже 0.6% не окупается по бэктесту",
        )

    # ── инструмент предпроверки ордеров (tools/validate_orders.py) ──

    def _validator(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "tools"))
        import validate_orders
        return validate_orders

    def test_validator_accepts_wellformed_legs(self):
        v = self._validator()
        filters = {"step_size": 0.001, "tick_size": 0.01, "min_qty": 0.001,
                   "min_notional": 5.0, "status": "TRADING"}
        legs, reason = pb.build_oco_legs("TESTUSDT", 100.0, 10.0, 1.0, 3.0,
                                         filters, cur_price=100.0)
        self.assertIsNone(reason)
        failed = [t for t, ok, _d in v.local_checks(legs, 100.0, filters) if not ok]
        self.assertEqual(failed, [], f"ложные срабатывания: {failed}")

    def test_validator_flags_notional_below_minimum(self):
        """Номинал ниже minNotional биржа отвергнет — инструмент обязан это поймать."""
        v = self._validator()
        filters = {"step_size": 1e-05, "tick_size": 0.01, "min_qty": 1e-05,
                   "min_notional": 5.0, "status": "TRADING"}
        legs, reason = pb.build_oco_legs("BTCUSDT", 77463.0, 3e-05, 1.0, 3.0,
                                         filters, cur_price=77463.0)
        self.assertIsNone(reason)
        failed = [t for t, ok, _d in v.local_checks(legs, 77463.0, filters) if not ok]
        self.assertTrue(any("minNotional" in t for t in failed),
                        f"нарушение минимума не поймано: {failed}")

    def test_validator_flags_limit_maker_below_market(self):
        """
        Верхняя нога OCO — LIMIT_MAKER, она обязана стоять выше рынка,
        иначе биржа отвергнет её как снимающую ликвидность.
        """
        v = self._validator()
        filters = {"step_size": 0.001, "tick_size": 0.01, "min_qty": 0.001,
                   "min_notional": 5.0, "status": "TRADING"}
        legs, reason = pb.build_oco_legs("TESTUSDT", 100.0, 10.0, 1.0, 3.0,
                                         filters, cur_price=100.0)
        # рынок ушёл выше цели: тот же набор ног, но проверка обязана сработать
        failed = [t for t, ok, _d in v.local_checks(legs, 105.0, filters) if not ok]
        self.assertTrue(any("LIMIT_MAKER" in t for t in failed),
                        f"правило LIMIT_MAKER не проверяется: {failed}")

    # ── логика меню: кнопки должны делать то, что обещают ─────

    def _source(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "pump_bot.py"), encoding="utf-8") as f:
            return f.read()

    def test_whale_button_runs_whale_scan_not_pump_scan(self):
        """
        Кнопка «🐋 Скан китов» обязана запускать китовый скан и её ветка
        должна стоять РАНЬШЕ общей ветки «скан»: иначе правило
        `"скан" in text_lower` перехватывает её и молча делает обычный скан.
        """
        src = self._source()
        i_whale = src.find('elif "кит" in text_lower')
        i_scan = src.find('elif cmd in ("scan", "скан")')
        self.assertNotEqual(i_whale, -1, "ветка китовой кнопки пропала")
        self.assertNotEqual(i_scan, -1)
        self.assertLess(i_whale, i_scan, "ветка китов должна идти до ветки «скан»")
        self.assertTrue(hasattr(pb, "execute_whale_scan_and_report"))

    def test_no_stale_percentages_in_ui_texts(self):
        """
        После смены дефолтов UI не должен обещать старые числа: ранее в девяти
        местах было жёстко вписано «+3%» и стоп `0.98` (2%), пока бот торговал
        другими значениями — пользователь видел одно, бот делал другое.
        """
        import re
        stale = []
        for line_no, line in enumerate(self._source().splitlines(), 1):
            if re.search(r"\+3%|0\.98\b", line):
                stale.append(f"{line_no}: {line.strip()[:70]}")
        self.assertEqual(stale, [], f"устаревшие числа в UI: {stale}")

    def test_help_text_shows_actual_defaults(self):
        """Справка обязана показывать реальные TP/SL, а не зашитые числа."""
        self.assertIn(f"+{pb.DEFAULT_TAKE_PROFIT:.1f}%", pb.HELP_TEXT)
        self.assertIn(f"-{pb.DEFAULT_STOP_LOSS:.1f}%", pb.HELP_TEXT)
        self.assertIn(f"Score {pb.DEFAULT_TRADE_MIN_SCORE:.0f}+", pb.HELP_TEXT)

    def test_unknown_callback_gets_answered(self):
        """
        Обработчик неизвестной кнопки обязан вызвать answerCallbackQuery,
        иначе Telegram бесконечно крутит индикатор и бот выглядит зависшим.
        """
        src = self._source()
        i_factors = src.find('elif cb_data.startswith("factors:")')
        i_else = src.find("Кнопка устарела", i_factors)
        self.assertNotEqual(i_else, -1, "нет fallback для неизвестных callback-ов")
        self.assertIn("answer_callback(token, cb_id", src[i_else - 400:i_else + 100])

    # ── график 15m ─────────────────────────────────────────────

    def test_chart_png_is_structurally_valid(self):
        """
        Картинка обязана быть настоящим PNG. Проверяем не «файл создан»,
        а структуру: сигнатуру, IHDR (8 бит, truecolor) и то, что IDAT
        распаковывается ровно в height*(1 + width*3) байт.
        """
        import struct
        import zlib
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import chart as ch

        candles15 = pb.aggregate_timeframe(make_5m_candles(600), 900_000)
        png = ch.render_chart("TESTUSDT", candles15)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"), "нет сигнатуры PNG")

        pos, idat, ihdr = 8, b"", None
        while pos + 8 <= len(png):
            ln = struct.unpack(">I", png[pos:pos + 4])[0]
            tag = png[pos + 4:pos + 8]
            data = png[pos + 8:pos + 8 + ln]
            if tag == b"IHDR":
                ihdr = struct.unpack(">IIBBBBB", data)
            elif tag == b"IDAT":
                idat += data
            pos += 12 + ln
        self.assertIsNotNone(ihdr, "нет чанка IHDR")
        w, h, depth, ctype = ihdr[0], ihdr[1], ihdr[2], ihdr[3]
        self.assertEqual((depth, ctype), (8, 2), "ожидается 8 бит truecolor")
        raw = zlib.decompress(idat)
        self.assertEqual(len(raw), h * (1 + w * 3), "размер распакованных данных не совпал")

    def test_chart_handles_short_and_flat_series(self):
        """Мало свечей или полностью плоская цена не должны ломать рендер."""
        import chart as ch
        with self.assertRaises(ValueError):
            ch.render_chart("TESTUSDT", make_5m_candles(20)[:20])

        flat = [pb.Candle(open_time=i * 900_000, open=50.0, high=50.0, low=50.0,
                          close=50.0, volume=1.0, quote_volume=50.0, trades=1,
                          taker_buy_base=0.5, close_time=i * 900_000 + 899_999)
                for i in range(60)]
        png = ch.render_chart("FLATUSDT", flat)
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_chart_caption_contains_indicators(self):
        """Числа индикаторов приходят подписью: их можно выделить и скопировать."""
        import chart as ch
        candles15 = pb.aggregate_timeframe(make_5m_candles(600), 900_000)
        caption = ch.chart_caption("TESTUSDT", candles15)
        for expected in ("15m", "Боллинджер", "RSI 14", "Стохастик", "%K", "%D"):
            self.assertIn(expected, caption)

    def test_send_symbol_chart_builds_and_sends_photo(self):
        """Кнопка графика должна реально собрать картинку и отправить фото."""
        captured = {}
        self._old_photo = pb.send_telegram_photo
        pb.send_telegram_photo = lambda tok, chat, png, caption="", **kw: (
            captured.update(png=png, caption=caption) or True)
        pb.fetch_klines = lambda sym, limit=250: make_5m_candles(600)
        try:
            ok = pb.send_symbol_chart("tok", "1", "TESTUSDT")
        finally:
            pb.send_telegram_photo = self._old_photo
        self.assertTrue(ok)
        self.assertTrue(captured["png"].startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn("RSI 14", captured["caption"])

    def test_chart_command_is_reachable(self):
        """
        График произвольной монеты должен запрашиваться командой: из сигнала
        его не получить (сигналы редки), а меню портфеля пусто без позиций.
        """
        src = self._source()
        self.assertIn('"command": "chart"', src, "нет /chart в списке команд бота")
        self.assertIn('elif cmd in ("chart", "график"', src, "нет ветки обработки /chart")
        self.assertIn("/chart SOL", pb.HELP_TEXT, "команда не упомянута в справке")

    # ── рантайм: /add, лимиты Telegram ─────────────────────────

    def test_add_command_does_not_feed_synthetic_update(self):
        """
        «/add SOL» не должен собирать фиктивное обновление: handle_update
        читает update_id, и рекурсия падала с KeyError, который проглатывал
        поллинг — команда молча не работала вовсе.
        """
        src = self._source()
        self.assertNotIn("handle_update(msg)", src,
                         "вернулась рекурсия с фиктивным обновлением")
        self.assertIn("def begin_add_symbol(", src)
        self.assertGreaterEqual(src.count("begin_add_symbol(chat_id_local"), 2,
                                "обе точки входа (FSM и /add) должны звать общий шаг")
        self.assertIn('u.get("update_id"', src,
                      "handle_update снова требует update_id и упадёт на синтетике")

    def test_long_message_is_chunked(self):
        """
        Telegram отвергает сообщение длиннее 4096 символов ЦЕЛИКОМ — раньше
        длинный портфель просто не доходил до пользователя.
        """
        lines = [f"• строка {i} с каким-то текстом" for i in range(400)]
        text = "\n".join(lines)                       # заметно больше 4000
        chunks = pb._split_message(text)
        self.assertGreater(len(chunks), 1, "длинный текст не разрезан")
        for c in chunks:
            self.assertLessEqual(len(c), 4000)
        # ничего не потеряли и не порвали строки
        self.assertEqual("\n".join(chunks), text)

    def test_chunking_keeps_short_message_intact(self):
        self.assertEqual(pb._split_message("короткое сообщение"), ["короткое сообщение"])

    def test_chunking_survives_single_giant_line(self):
        """Одна строка длиннее лимита тоже должна быть разрезана."""
        chunks = pb._split_message("x" * 9000)
        self.assertTrue(all(len(c) <= 4000 for c in chunks))
        self.assertEqual("".join(chunks), "x" * 9000)

    def test_reply_markup_goes_to_first_chunk_only(self):
        """Клавиатура крепится к первой части, иначе окажется под хвостом текста."""
        sent = []
        real_send = self._old_send          # setUp подменяет send_telegram заглушкой
        old_api = pb.api_call
        pb.api_call = lambda token, method, payload=None, **kw: (
            sent.append(dict(payload or {})) or {"ok": True, "result": {"message_id": 1}})
        try:
            big = "\n".join(f"строка {i}" for i in range(500))
            real_send("tok", "1", big, reply_markup={"keyboard": []})
        finally:
            pb.api_call = old_api
        self.assertGreater(len(sent), 1, "длинный текст не разрезан на части")
        self.assertIn("reply_markup", sent[0])
        self.assertNotIn("reply_markup", sent[1])

    def test_api_call_waits_on_429(self):
        """
        При 429 Telegram сообщает retry_after, и пауза обязательна: повтор
        сразу же усугубляет, а сообщение теряется.
        """
        import urllib.error
        sleeps = []

        def fake_urlopen(req, timeout=15):
            raise urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests", {},
                __import__("io").BytesIO(b'{"parameters":{"retry_after":2}}'))

        old_open, old_sleep = pb.urllib.request.urlopen, pb.time.sleep
        pb.urllib.request.urlopen = fake_urlopen
        pb.time.sleep = lambda s: sleeps.append(s)
        try:
            res = pb.api_call("tok", "sendMessage", {"chat_id": 1, "text": "x"})
        finally:
            pb.urllib.request.urlopen = old_open
            pb.time.sleep = old_sleep
        self.assertEqual(res, {})
        self.assertTrue(any(s >= 2 for s in sleeps),
                        f"не выдержана пауза retry_after: {sleeps}")

    # ── движок: таймфреймы ─────────────────────────────────────

    def test_every_timeframe_can_produce_bars(self):
        """
        Заявлен мульти-TF 5m/15m/1h, но при глубине 250 свечей 5m часовая
        серия давала всего 21 бар при MIN_BARS=32 — то есть таймфрейм 1h не
        мог дать НИ ОДНОГО сигнала, и оценка шла только по двум ТФ.
        Инвариант: для каждого объявленного ТФ окно глубиной KLINES_LIMIT
        обязано давать минимум MIN_BARS баров.
        """
        candles = make_5m_candles(pb.KLINES_LIMIT)
        for tf in pb.TIMEFRAMES:
            series = candles if tf == "5m" else pb.aggregate_timeframe(candles, pb.TF_MS[tf])
            self.assertGreaterEqual(
                len(series), pb.MIN_BARS,
                f"ТФ {tf}: из {pb.KLINES_LIMIT} свечей 5m получается {len(series)} баров "
                f"при MIN_BARS={pb.MIN_BARS} — этот ТФ не участвует в оценке",
            )

    # ── режим стратегии: RSI + стохастик ───────────────────────

    def _flat_then_rise(self, flat=40, rise=3):
        """Плоский участок, затем резкий подъём: RSI высокий, %K > %D."""
        out, t0 = [], 1_700_000_000_000
        price = 100.0
        for i in range(flat + rise):
            up = i >= flat
            o = price
            cl = price + (2.0 if up else 0.0)
            out.append(pb.Candle(
                open_time=t0 + i * 900_000, open=o, high=max(o, cl) + 0.5,
                low=min(o, cl) - 0.5, close=cl, volume=100.0, quote_volume=10000.0,
                trades=10, taker_buy_base=55.0, close_time=t0 + i * 900_000 + 899_999))
            price = cl
        return out

    def test_strategy_modes_are_declared(self):
        self.assertIn("pump", pb.STRATEGY_LABELS)
        self.assertIn("indicators", pb.STRATEGY_LABELS)
        self.assertIn(pb.DEFAULT_STRATEGY, pb.STRATEGY_LABELS)

    def test_parabolic_sar_follows_trend_direction(self):
        """SAR обязан стоять ПОД ценой на росте и НАД ценой на падении."""
        rising = self._flat_then_rise(flat=5, rise=25)
        sar, up = pb.parabolic_sar(rising)
        self.assertTrue(up[-1], "на росте SAR должен быть в восходящем режиме")
        self.assertLess(sar[-1], rising[-1].close, "SAR восходящего тренда должен быть под ценой")

        falling = [pb.Candle(
            open_time=c.open_time, open=c.close, high=c.close + 0.5, low=c.close - 0.5,
            close=c.close - 1.0, volume=100.0, quote_volume=1000.0, trades=5,
            taker_buy_base=50.0, close_time=c.close_time)
            for c in reversed(rising)]
        sar_f, up_f = pb.parabolic_sar(falling)
        self.assertFalse(up_f[-1], "на падении SAR должен быть в нисходящем режиме")
        self.assertGreater(sar_f[-1], falling[-1].close, "SAR падения должен быть над ценой")

    def test_fractals_find_local_extremes(self):
        """Фрактал — локальный экстремум окна ±2, и он не может быть на краю."""
        candles = self._flat_then_rise(flat=20, rise=0)
        flat_price = candles[10].close
        peak = pb.Candle(open_time=candles[11].open_time, open=flat_price,
                         high=flat_price + 5.0, low=flat_price, close=flat_price + 1.0,
                         volume=100.0, quote_volume=1000.0, trades=5,
                         taker_buy_base=50.0, close_time=candles[11].close_time)
        candles.insert(11, peak)
        up, down = pb.fractals(candles, k=2)
        self.assertEqual(up[11], flat_price + 5.0, "максимум окна не распознан как up-фрактал")
        # последние k баров структурно не могут быть фракталом (ещё не подтверждены)
        self.assertTrue(all(v is None for v in up[-2:]))

    def test_score_indicators_uses_rsi_sar_fractal(self):
        """
        Новое правило: RSI выше порога, SAR под ценой, пробит up-фрактал.
        Стохастик убран — на трёх выборках он не давал информации.
        """
        candles = self._flat_then_rise(flat=30, rise=25)
        bd = pb.score_indicators("15m", candles, [], 0.0, True, strict=False)
        self.assertIsNotNone(bd)
        self.assertEqual([f.id for f in bd.factors], ["rsi_zone", "sar_trend", "fractal_break"])
        self.assertGreater(bd.sar, 0.0, "значение SAR должно попадать в разбор")

        # С невозможным порогом RSI правило молчит (на монотонном росте RSI=100)
        self.assertIsNone(pb.score_indicators("15m", candles, [], 0.0, True,
                                              strict=True, rsi_min=101.0))

    def test_settings_expose_strategy_switch(self):
        state = pb.default_state()
        self.assertEqual(state["settings"]["strategy"], pb.DEFAULT_STRATEGY)
        kb = pb.settings_inline_kb(state)
        cbs = [b.get("callback_data") for row in kb["inline_keyboard"] for b in row]
        self.assertIn("strategy:set:pump", cbs)
        self.assertIn("strategy:set:indicators", cbs)
        self.assertIn("Режим стратегии", pb.settings_text(state))

    def test_scan_and_autoscan_pass_strategy(self):
        """Режим обязан доходить до скана, иначе выбор кнопкой ни на что не влияет."""
        src = self._source()
        self.assertGreaterEqual(
            src.count('strategy=settings.get("strategy"'), 1,
            "автоскан не передаёт выбранный режим")
        self.assertGreaterEqual(
            src.count('strategy=s.get("strategy"'), 1,
            "ручной скан не передаёт выбранный режим")

    # ── модуль стратегий: объём + зелёная свеча ────────────────

    def _strategies(self):
        # Стратегии живут в самом pump_bot: всё в одном файле.
        return pb

    def _vol_candles(self, last_volume: float, bullish: bool = True):
        """40 свечей ровного объёма 100, последняя — заданного объёма."""
        out, t0, price = [], 1_700_000_000_000, 100.0
        for i in range(40):
            last = i == 39
            vol = last_volume if last else 100.0
            o = price
            cl = price + (1.0 if (bullish or not last) else -1.0) if last else price
            out.append(pb.Candle(
                open_time=t0 + i * 900_000, open=o, high=max(o, cl) + 0.3,
                low=min(o, cl) - 0.3, close=cl, volume=vol, quote_volume=vol * cl,
                trades=int(vol / 10), taker_buy_base=vol * 0.6,
                close_time=t0 + i * 900_000 + 899_999))
            price = cl
        return out

    def test_volume_scorer_fires_on_green_candle_with_volume(self):
        """Правило: зелёная свеча И объём выше порога (по умолчанию 1.8× медианы)."""
        st = self._strategies()
        hot = self._vol_candles(last_volume=300.0, bullish=True)
        bd = st.score_volume_candle("15m", hot, [], 0.0, True, strict=True)
        self.assertIsNotNone(bd, "зелёная свеча при 3× объёма должна давать сигнал")
        self.assertEqual([f.id for f in bd.factors], ["vol_robust", "candle"])
        self.assertAlmostEqual(bd.volume_ratio, 3.0, places=3)

        quiet = self._vol_candles(last_volume=100.0, bullish=True)
        self.assertIsNone(st.score_volume_candle("15m", quiet, [], 0.0, True, strict=True),
                          "при объёме на уровне медианы сигнала быть не должно")

        red = self._vol_candles(last_volume=300.0, bullish=False)
        self.assertIsNone(st.score_volume_candle("15m", red, [], 0.0, True, strict=True),
                          "красная свеча при объёме — не сигнал на покупку")

    def test_volume_baseline_is_median_not_mean(self):
        """
        База объёма обязана быть медианой: среднее завышается разовым всплеском
        (на живых данных — до 2.6×), после чего инструмент «слепнет» на часы.
        """
        st = self._strategies()
        volumes = [100.0] * 19 + [1000.0] + [300.0]     # один выброс в прошлом
        ratio = st.vol_ratio_robust(volumes, period=20)
        median_base = sorted(volumes[-21:-1])[10]        # медиана предыдущих 20 = 100
        self.assertAlmostEqual(ratio, 300.0 / median_base, places=6)
        self.assertGreater(ratio, 300.0 / 145.0,
                           "похоже, база считается средним (145), а не медианой (100)")

    def test_strategy_registry_and_labels_agree(self):
        """
        Каждый режим из реестра обязан быть выбираемым в интерфейсе, иначе
        стратегия есть в коде, но включить её нельзя.
        """
        for mode in pb.STRATEGY_SCORERS:
            self.assertIn(mode, pb.STRATEGY_LABELS, f"режим {mode} не виден в настройках")
        self.assertIsNotNone(pb.STRATEGY_SCORERS["volume"])
        self.assertIsNotNone(pb.STRATEGY_SCORERS["dump"])
        self.assertIsNone(pb.STRATEGY_SCORERS["pump"], "встроенный скорер берётся из pump_bot")

    # ── Диагностика, Rate Limit и Целостность состояния ─────────

    def test_record_binance_weight_tracking(self):
        """Проверка фиксации расхода веса Binance из заголовков ответа."""
        headers = {"x-mbx-used-weight-1m": "450"}
        pb.record_binance_weight(headers)
        self.assertEqual(pb.API_WEIGHT_USED_1M, 450)
        self.assertGreater(pb.API_WEIGHT_UPDATED_AT, 0)

        # Безопасность при отсутствии заголовков или нестандартных объектах
        pb.record_binance_weight(None)
        pb.record_binance_weight({})
        self.assertEqual(pb.API_WEIGHT_USED_1M, 450)

    def test_format_system_status_renders_diagnostics(self):
        """Отчёт диагностики обязан содержать аптайм, статус API и размер базы."""
        state = pb.default_state()
        state["portfolio"]["SOLUSDT"] = {"qty": 2.0, "avg_price": 100.0, "added_at": int(time.time())}
        text = pb.format_system_status(state)
        self.assertIn("Системная диагностика", text)
        self.assertIn("Аптайм процесса", text)
        self.assertIn("Расход веса", text)
        self.assertIn("bot_state.json", text)
        self.assertIn("1", text)  # 1 актив в портфеле

    def test_load_state_prunes_corrupted_portfolio_and_trades(self):
        """load_state обязан очищать битые записи (отрицательные/нулевые количества)."""
        state = pb.default_state()
        state["portfolio"]["CORRUPT1"] = {"qty": 0.0, "avg_price": 10.0}
        state["portfolio"]["CORRUPT2"] = {"qty": -5.0, "avg_price": 10.0}
        state["portfolio"]["VALID"] = {"qty": 1.5, "avg_price": 10.0}
        state["active_trades"]["CORRUPT_TRADE"] = {"qty": 0.0, "buy_price": 10.0}
        state["active_trades"]["VALID_TRADE"] = {"qty": 2.0, "buy_price": 10.0}
        pb.save_state(state)

        loaded = pb.load_state()
        self.assertIn("VALID", loaded["portfolio"])
        self.assertNotIn("CORRUPT1", loaded["portfolio"])
        self.assertNotIn("CORRUPT2", loaded["portfolio"])
        self.assertIn("VALID_TRADE", loaded["active_trades"])
        self.assertNotIn("CORRUPT_TRADE", loaded["active_trades"])

    def test_sanitize_sensitive_text(self):
        """Санитизатор обязан маскировать подписи HMAC и API-секреты в любых строках."""
        raw_url = "https://api.binance.com/api/v3/order?symbol=BTCUSDT&timestamp=1600000000&signature=d3b07384d113edec49eaa6238ad5ff00"
        sanitized = pb.sanitize_sensitive_text(raw_url)
        self.assertNotIn("d3b07384d113edec49eaa6238ad5ff00", sanitized)
        self.assertIn("signature=***MASKED***", sanitized)

        raw_err = "Failed with apiKey=super_secret_123 and secret=my_key_pass"
        sanitized_err = pb.sanitize_sensitive_text(raw_err)
        self.assertNotIn("super_secret_123", sanitized_err)
        self.assertNotIn("my_key_pass", sanitized_err)
        self.assertIn("apiKey=***MASKED***", sanitized_err)
        self.assertIn("secret=***MASKED***", sanitized_err)

    def test_sl_cooldown_management(self):
        """Проверка установки кулдауна после SL и блокировки повторных сделок."""
        state = pb.default_state()
        symbol = "COOLDOWNUSDT"
        
        # 1. Символ изначально не в кулдауне
        self.assertIsNone(pb.is_symbol_in_sl_cooldown(state, symbol))

        # 2. Устанавливаем кулдаун на 100 секунд
        pb.set_symbol_sl_cooldown(state, symbol, duration_sec=100, reason="test_sl")
        until = pb.is_symbol_in_sl_cooldown(state, symbol)
        self.assertIsNotNone(until)
        self.assertGreater(until, time.time())

        # 3. execute_pump_auto_trade обязан отвергнуть сигнал по монете в кулдауне
        sig = pb.PumpSignal(
            symbol=symbol,
            base="COOLDOWN",
            price=10.0,
            change_24h=2.5,
            quote_volume_24h=5000000.0,
            high_24h=10.5,
            low_24h=9.5,
            btc_relative_24h=1.0,
            best_tf="5m",
            best_score=80.0,
            grade="strong",
            alert_key=f"{symbol}_5m",
            by_tf=[],
        )
        res = pb.execute_pump_auto_trade("fake_token", 12345, state, sig)
        self.assertIsNotNone(res)
        self.assertIn("кулдаун", res.get("error", ""))

        # 4. Истекший кулдаун автоматически очищается
        state["sl_cooldowns"][symbol]["until"] = time.time() - 10
        self.assertIsNone(pb.is_symbol_in_sl_cooldown(state, symbol))
        self.assertNotIn(symbol, state["sl_cooldowns"])

    def test_trades_log_csv(self):
        """Проверка корректной записи и форматирования CSV лога сделок."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tf:
            temp_csv = tf.name

        try:
            # Записываем тестовое событие входа
            pb.log_trade_event(
                event_type="BUY_MARKET_FILLED",
                symbol="LOGUSDT",
                order_id=999111,
                price=25.50,
                qty=10.0,
                quote_amount=255.0,
                reason="market_entry",
                file_path=temp_csv,
            )
            # Записываем событие выхода
            pb.log_trade_event(
                event_type="TP_FILLED",
                symbol="LOGUSDT",
                order_id=999222,
                price=26.00,
                qty=10.0,
                quote_amount=260.0,
                pnl=5.0,
                pnl_pct=1.96,
                reason="take_profit",
                file_path=temp_csv,
            )

            with open(temp_csv, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]

            # Должен быть заголовок + 2 записи
            self.assertEqual(len(lines), 3)
            self.assertTrue(lines[0].startswith("timestamp,datetime_utc,symbol,event_type"))
            self.assertIn("LOGUSDT,BUY_MARKET_FILLED,999111,25.5,10,255.0000", lines[1])
            self.assertIn("LOGUSDT,TP_FILLED,999222,26,10,260.0000,+1.96%,+5.0000", lines[2])
        finally:
            if os.path.exists(temp_csv):
                try:
                    os.remove(temp_csv)
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main(verbosity=2)


