#!/usr/bin/env python3
import os, sys, time, signal, subprocess
from datetime import datetime

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

BOT_SCRIPT = 'pump_bot.py'
RESTART_DELAY_SEC = 5
DAILY_RESTART_SEC = 24 * 3600
FILE_DIR = os.path.dirname(os.path.abspath(__file__))

def log(msg):
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{now_str}] [Watchdog] {msg}', flush=True)

def main():
    bot_path = os.path.join(FILE_DIR, BOT_SCRIPT)
    if not os.path.exists(bot_path):
        log(f'[ERROR] File {bot_path} not found!')
        sys.exit(1)
    log('🚀 Запуск службы Watchdog для Pump Pulse Bot...')
    log(f'📁 Рабочая директория: {FILE_DIR}')
    log(f'⏱️ Интервал авто-рестарта при сбое: {RESTART_DELAY_SEC}с')
    running = True
    def handle_signal(sig, frame):
        nonlocal running
        log('🛑 Получен сигнал остановки (Ctrl+C)...')
        running = False
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    crash_count = 0
    while running:
        log(f'▶️ Запуск {BOT_SCRIPT}...')
        proc_start = time.time()
        try:
            cmd = [sys.executable, '-u', bot_path] + sys.argv[1:]
            proc = subprocess.Popen(cmd, cwd=FILE_DIR, stdout=sys.stdout, stderr=sys.stderr)
            while running:
                ret = proc.poll()
                if ret is not None:
                    duration = time.time() - proc_start
                    if ret == 0:
                        log(f'ℹ️ Бот завершил работу (код 0, работал {duration:.0f}с).')
                    else:
                        crash_count += 1
                        log(f'⚠️ Бот аварийно завершил работу (код {ret}, работал {duration:.0f}с, сбоев: {crash_count}).')
                    break
                if time.time() - proc_start >= DAILY_RESTART_SEC:
                    log('🔄 24 часа прошло. Мягкий плановый перезапуск...')
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    break
                time.sleep(2)
        except Exception as e:
            log(f'❌ Ошибка запуска: {e}')
        if not running:
            break
        log(f'⏳ Ожидание {RESTART_DELAY_SEC}с перед рестартом...')
        time.sleep(RESTART_DELAY_SEC)
    log('👋 Watchdog остановлен.')

if __name__ == '__main__':
    main()
