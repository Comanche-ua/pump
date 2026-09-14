# 🖥 Инструкция по переезду Pump Pulse Bot на VPS

В отличие от GitHub Actions (где runner ограничен по времени и требует коммитов состояния в git), на VPS бот работает **24/7 автономно**, мгновенно реагируя на рынок и исполняя стоп-ордера без задержек.

---

## 🚀 Быстрый старт (Ubuntu / Debian VPS)

### 1. Подключение к серверу и загрузка проекта
```bash
# Подключитесь к VPS по SSH
ssh root@YOUR_VPS_IP

# Склонируйте репозиторий или скопируйте файлы в папку /opt/pump-bot
git clone <URL_ВАШЕГО_РЕПОЗИТОРИЯ> /opt/pump-bot
cd /opt/pump-bot
```

### 2. Автоматическая установка в 1 команду
```bash
chmod +x deploy/setup_vps.sh
./deploy/setup_vps.sh
```

### 3. Настройка переменных окружения
Откройте конфигурационный файл и укажите ваши ключи Telegram и Binance:
```bash
nano /opt/pump-bot/.env
```

Пример `.env`:
```ini
TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
TELEGRAM_CHAT_ID=987654321

BINANCE_API_KEY=your_binance_api_key
BINANCE_API_SECRET=your_binance_api_secret

# При необходимости обхода гео-блокировок:
# BINANCE_DATA_BASE=https://your-worker.workers.dev
# BINANCE_WORKER_AUTH=your-secret-token

RUN_MODE=bot
AUTO_TRADE=false
TRADE_AMOUNT_USDT=11.0
TAKE_PROFIT_PCT=1.0
STOP_LOSS_PCT=3.0
ENTRY_PULLBACK_PCT=0.4
SL_COOLDOWN_SECONDS=3600
```

### 4. Управление службой (Systemd)

| Действие | Команда |
|---|---|
| **Запуск бота** | `sudo systemctl start pump-bot` |
| **Остановка бота** | `sudo systemctl stop pump-bot` |
| **Перезапуск** | `sudo systemctl restart pump-bot` |
| **Просмотр логов в реальном времени** | `sudo journalctl -u pump-bot -f` |
| **Статус службы** | `sudo systemctl status pump-bot` |

---

## 🛡 Преимущества Systemd перед скриптами запуска
1. **Автоматический перезапуск**: если бот упадет из-за сбоя сети или памяти, systemd перезапустит его через 10 секунд.
2. **Автозапуск при перезагрузке сервера**: бот продолжит работу сразу после ребута VPS.
3. **Безопасное логирование**: все логи пишутся в системный журнал `journald` с ротацией и не забивают диск.
