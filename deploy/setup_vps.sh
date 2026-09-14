#!/usr/bin/env bash
# ==============================================================================
# Скрипт автоматического развертывания Pump Pulse Bot на Ubuntu / Debian VPS
# ==============================================================================
set -e

INSTALL_DIR="/opt/pump-bot"
SERVICE_NAME="pump-bot"

echo "=========================================================="
echo "🚀 Установка и настройка Pump Pulse Bot на VPS"
echo "=========================================================="

# 1. Обновление пакетов и установка зависимостей
echo "📦 Обновление системных пакетов и установка Python3..."
sudo apt-get update
sudo apt-get install -y python3 python3-pip python3-venv git curl

# 2. Создание рабочей директории
echo "📁 Подготовка директории $INSTALL_DIR..."
sudo mkdir -p "$INSTALL_DIR"
sudo chown -R $USER:$USER "$INSTALL_DIR"

# Копирование файлов проекта в целевую директорию (если скрипт запущен из репозитория)
CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "$CURRENT_DIR" != "$INSTALL_DIR" ]; then
    echo "📋 Копирование файлов проекта..."
    cp -r "$CURRENT_DIR"/* "$INSTALL_DIR/" 2>/dev/null || true
fi

# 3. Создание виртуального окружения
echo "🐍 Настройка виртуального окружения Python..."
cd "$INSTALL_DIR"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
if [ -f "requirements.txt" ]; then
    ./venv/bin/pip install -r requirements.txt
else
    ./venv/bin/pip install python-dotenv requests certifi
fi

# 4. Проверка файла .env
if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo "⚠️ Файл .env не найден! Создаем шаблон..."
    cat << 'EOF' > "$INSTALL_DIR/.env"
# Telegram Bot Token & Chat ID
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Binance API Credentials
BINANCE_API_KEY=
BINANCE_API_SECRET=

# Cloudflare Worker / Proxy (опционально)
# BINANCE_DATA_BASE=https://your-worker.workers.dev
# BINANCE_WORKER_AUTH=your-secret-token

# Торговые настройки
RUN_MODE=bot
AUTO_TRADE=false
TRADE_AMOUNT_USDT=11.0
TAKE_PROFIT_PCT=1.0
STOP_LOSS_PCT=3.0
ENTRY_PULLBACK_PCT=0.4
SL_COOLDOWN_SECONDS=3600
EOF
    echo "💡 Заполните $INSTALL_DIR/.env вашими ключами перед запуском!"
fi

# 5. Установка и регистрация systemd сервиса
echo "⚙️ Установка systemd сервиса..."
sudo cp "$INSTALL_DIR/deploy/pump-bot.service" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}.service"

echo "=========================================================="
echo "✅ Установка завершена!"
echo ""
echo "Полезные команды:"
echo "• Отредактировать настройки: nano $INSTALL_DIR/.env"
echo "• Запустить бота:            sudo systemctl start $SERVICE_NAME"
echo "• Остановить бота:           sudo systemctl stop $SERVICE_NAME"
echo "• Перезапустить бота:        sudo systemctl restart $SERVICE_NAME"
echo "• Просмотр логов:            sudo journalctl -u $SERVICE_NAME -f"
echo "• Проверить статус:          sudo systemctl status $SERVICE_NAME"
echo "=========================================================="
