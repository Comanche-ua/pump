@echo off
chcp 65001 > nul
title Pump Pulse Scanner 2.0 - Telegram Bot
echo ===================================================
echo   Pump Pulse Scanner 2.0 - Запуск Telegram-бота
echo ===================================================
echo.

if not exist .env (
    if exist .env.example (
        echo [!] Файл .env не найден. Создаю .env из шаблона .env.example...
        copy .env.example .env > nul
        echo [!] Пожалуйста, откройте файл .env и укажите ваш TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID!
        echo.
        pause
        notepad .env
        exit /b
    )
)

echo [*] Запуск бота в интерактивном режиме с кнопками и меню...
echo [*] Чтобы остановить бота, нажмите Ctrl+C или закройте это окно.
echo.

python pump_bot.py

if errorlevel 1 (
    echo.
    echo [!] Бот завершился с ошибкой.
    pause
)
