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
        Цель 0.5-0.7% при стопе 3% измерена как нулевая или отрицательная:
        издержки съедают всю точность попадания (безубыток 86-92%).
        Минимальная положительная цель по бэктесту — 1.0%.
        """
        self.assertGreaterEqual(
            pb.DEFAULT_TAKE_PROFIT, 1.0,
            "цель ниже 1.0% при стопе 3% не окупается по бэктесту",
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
