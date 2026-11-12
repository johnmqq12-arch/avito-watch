#!/usr/bin/env bash
# Запуск Avito Watch в контейнере.
# По умолчанию браузер работает «headed» внутри виртуального дисплея Xvfb:
# так вы в любой момент можете открыть noVNC и пройти капчу руками.
set -euo pipefail

APP_DIR="${APP_DIR:-/app}"
cd "$APP_DIR"
mkdir -p data data/profile data/captcha

if [[ ! -f config.yaml ]]; then
  cp config.example.yaml config.yaml
  echo "[entrypoint] Создан config.yaml из примера — отредактируйте sources под себя"
fi

HEADLESS="${AW_HEADLESS:-0}"
VNC_PORT="${VNC_PORT:-6080}"
VNC_PASSWORD="${VNC_PASSWORD:-}"

if [[ "$HEADLESS" == "0" || "$HEADLESS" == "false" ]]; then
  echo "[entrypoint] Поднимаю виртуальный дисплей :99"
  Xvfb :99 -screen 0 1440x900x24 -nolisten tcp &
  sleep 2

  VNC_ARGS=(-display :99 -forever -shared -rfbport 5900 -noxdamage -quiet)
  if [[ -n "$VNC_PASSWORD" ]]; then
    VNC_ARGS+=(-passwd "$VNC_PASSWORD")
  else
    echo "[entrypoint] ВНИМАНИЕ: VNC без пароля. Задайте VNC_PASSWORD в .env или закройте порт наружу."
    VNC_ARGS+=(-nopw)
  fi
  x11vnc "${VNC_ARGS[@]}" &
  sleep 1

  websockify --web=/usr/share/novnc "$VNC_PORT" localhost:5900 &
  echo "[entrypoint] noVNC: http://localhost:${VNC_PORT}/vnc.html?autoconnect=1&resize=scale"
  export DISPLAY=:99
else
  echo "[entrypoint] Headless-режим: ручное прохождение капчи через noVNC недоступно"
fi

# Чистим зависший лок профиля после некорректной остановки контейнера
rm -f data/profile/SingletonLock data/profile/SingletonCookie data/profile/SingletonSocket 2>/dev/null || true

exec "$@"