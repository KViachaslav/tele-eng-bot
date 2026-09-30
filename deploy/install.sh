#!/usr/bin/env bash
# Установка бота на Linux-сервер (Ubuntu/Debian) как сервиса systemd.
#
# Запуск из каталога проекта (там же, где main.py):
#     sudo bash deploy/install.sh
#
# Что делает скрипт:
#   1. проверяет Python 3.11+ (и ставит python3-venv, если venv не создаётся);
#   2. создаёт системного пользователя (по умолчанию teleeng) и отдаёт ему проект;
#   3. создаёт .venv и устанавливает зависимости из requirements.txt;
#   4. готовит .env из .env.example (если файла ещё нет);
#   5. подставляет пути в deploy/tele-eng-bot.service, включает автозапуск и стартует.
#
# Переопределяемые переменные: RUN_USER, SERVICE_NAME, PYTHON.
set -euo pipefail

RUN_USER="${RUN_USER:-teleeng}"
SERVICE_NAME="${SERVICE_NAME:-tele-eng-bot}"
PYTHON="${PYTHON:-python3}"
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_TEMPLATE="${PROJECT_DIR}/deploy/${SERVICE_NAME}.service"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_FILE="${PROJECT_DIR}/.env"
VENV_PYTHON="${PROJECT_DIR}/.venv/bin/python"
START=1

log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mxxx\033[0m %s\n' "$*" >&2; exit 1; }

# Выполняет команду от имени сервисного пользователя (скрипт запущен под root).
as_user() {
    if command -v runuser >/dev/null 2>&1; then
        runuser -u "${RUN_USER}" -- "$@"
    else
        sudo -u "${RUN_USER}" -- "$@"
    fi
}

[[ "${EUID}" -eq 0 ]] || die "Запустите с правами root: sudo bash deploy/install.sh"
[[ -f "${PROJECT_DIR}/main.py" ]] || die "main.py не найден в ${PROJECT_DIR}"
[[ -f "${UNIT_TEMPLATE}" ]] || die "Не найден шаблон юнита: ${UNIT_TEMPLATE}"
command -v systemctl >/dev/null 2>&1 || die "systemd не найден — инструкция для Linux-сервера с systemd"

# --- 1. Python ------------------------------------------------------------
command -v "${PYTHON}" >/dev/null 2>&1 || die "Не найден ${PYTHON}: apt-get install -y python3 python3-venv"
if [[ "$("${PYTHON}" -c 'import sys; print(int(sys.version_info >= (3, 11)))')" != "1" ]]; then
    die "Нужен Python 3.11+, найден: $("${PYTHON}" -V 2>&1)"
fi
log "Python: $("${PYTHON}" -V 2>&1)"

# --- 2. Сервисный пользователь -------------------------------------------
if ! id -u "${RUN_USER}" >/dev/null 2>&1; then
    log "Создаю системного пользователя ${RUN_USER}"
    useradd --system --home-dir "${PROJECT_DIR}" --shell /usr/sbin/nologin "${RUN_USER}"
fi
log "Каталог проекта: ${PROJECT_DIR} (владелец ${RUN_USER})"
chown -R "${RUN_USER}:${RUN_USER}" "${PROJECT_DIR}"
install -d -o "${RUN_USER}" -g "${RUN_USER}" "${PROJECT_DIR}/logs"

# --- 3. Виртуальное окружение и зависимости ------------------------------
log "Создаю виртуальное окружение .venv"
if ! as_user "${PYTHON}" -m venv "${PROJECT_DIR}/.venv"; then
    log "venv не создался — устанавливаю python3-venv и пробую снова"
    apt-get update -qq
    apt-get install -y python3-venv
    as_user "${PYTHON}" -m venv "${PROJECT_DIR}/.venv"
fi

log "Устанавливаю зависимости из requirements.txt"
as_user "${VENV_PYTHON}" -m pip install --quiet --upgrade pip
as_user "${VENV_PYTHON}" -m pip install --quiet -r "${PROJECT_DIR}/requirements.txt"

# --- 4. Настройки ---------------------------------------------------------
if [[ ! -f "${ENV_FILE}" ]]; then
    log "Создаю .env из .env.example"
    cp "${PROJECT_DIR}/.env.example" "${ENV_FILE}"
fi
chown "${RUN_USER}:${RUN_USER}" "${ENV_FILE}"
chmod 600 "${ENV_FILE}"

TOKEN="$(grep -E '^BOT_TOKEN=' "${ENV_FILE}" | head -n 1 | cut -d= -f2- | tr -d '[:space:]')"
if [[ -z "${TOKEN}" || "${TOKEN}" == 123456789:* ]]; then
    warn "В ${ENV_FILE} не указан реальный BOT_TOKEN — токен от @BotFather обязателен"
    START=0
fi

# --- 5. Сервис systemd ----------------------------------------------------
log "Устанавливаю юнит: ${UNIT_PATH}"
sed -e "s|@PROJECT_DIR@|${PROJECT_DIR}|g" -e "s|@RUN_USER@|${RUN_USER}|g" \
    "${UNIT_TEMPLATE}" > "${UNIT_PATH}"
chmod 644 "${UNIT_PATH}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" >/dev/null

if [[ "${START}" == "1" ]]; then
    log "Запускаю сервис ${SERVICE_NAME}"
    systemctl restart "${SERVICE_NAME}"
    sleep 3
    systemctl --no-pager --full status "${SERVICE_NAME}" || true
    log "Логи: journalctl -u ${SERVICE_NAME} -f"
else
    warn "Автозапуск включён, но сервис не запущен: впишите BOT_TOKEN в ${ENV_FILE}"
    warn "Затем: systemctl start ${SERVICE_NAME}"
fi
