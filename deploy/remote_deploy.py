"""Обновление бота на сервере по SSH прямо с ноутбука.

Код уезжает на сервер через git (``git push`` + ``git pull``), но остановка и
запуск сервиса, установка зависимостей и пакетный перевод словаря требуют SSH.
На Windows встроенный ``ssh`` пароль из скрипта не принимает (``SSH_ASKPASS`` в
неинтерактивной сессии не срабатывает), поэтому используется ``paramiko``::

    .\\.venv\\Scripts\\python.exe -m pip install paramiko

Доступы берутся из ``.deploy.env`` в корне проекта (файл добавлен в
``.gitignore``, в репозиторий не попадает)::

    SSH_HOST=192.168.1.50
    SSH_USER=your-ssh-user
    SSH_PASSWORD=...
    SSH_PROJECT_DIR=/opt/tele_eng
    SSH_SERVICE=tele-eng-bot
    SSH_RUN_USER=teleeng
    MYMEMORY_EMAIL=you@example.com

Команды::

    python deploy/remote_deploy.py status                 # что на сервере сейчас
    python deploy/remote_deploy.py update                 # стоп -> git pull -> pip -> старт
    python deploy/remote_deploy.py translate --limit 500   # добор русских переводов словаря
    python deploy/remote_deploy.py copy-translations       # перенести переводы из локального bot.db

Пароль сервера в командах не светится: ``sudo -S`` читает его из stdin, а токен
бота в отчёт не попадает (маскируется).
"""
from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import paramiko
except ImportError:  # pragma: no cover - зависимость нужна только для деплоя
    sys.exit(
        "Нужен paramiko: .\\.venv\\Scripts\\python.exe -m pip install paramiko "
        "(или .venv/bin/pip install paramiko)"
    )

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".deploy.env"
#: Лог пакетного перевода на сервере: это разовый прогон, в проекте ему не место.
REMOTE_LOG = "/tmp/tele_eng_translate.log"

#: Статистика серверной базы: слова, наличие русских строк, пользователи.
DB_STATS = """\
import sqlite3
c = sqlite3.connect({db!r})
cols = [r[1] for r in c.execute('pragma table_info(words)')]
q = lambda s: c.execute(s).fetchone()[0]
print('words_total', q('select count(*) from words'))
print('has_ru_columns', 'russian_definition' in cols, 'russian_example' in cols)
if 'russian_definition' in cols:
    filled = q("select count(*) from words where coalesce(russian_definition, '') <> ''")
    print('ru_filled', filled)
    print('ru_empty', q('select count(*) from words') - filled)
print('users', q('select count(*) from users'))
print('user_words', q('select count(*) from user_words'))
print('last_sent', q('select max(sent_at) from delivery_log'))
"""

#: Дозапись настроек перевода в серверный ``.env`` (файл принадлежит run_user).
ENV_PATCH = """\
#!/bin/sh
cd {project} || exit 1
for kv in "TRANSLATION_ENABLED=true" "MYMEMORY_EMAIL={email}"; do
    key=${{kv%%=*}}
    grep -q "^$key=" .env || echo "$kv" >> .env
done
grep -E '^(TRANSLATION_ENABLED|MYMEMORY_EMAIL)=' .env
"""

#: Пакетный перевод: запускается в фоне (setsid), лог — в REMOTE_LOG.
BATCH = """\
#!/bin/sh
cd {project} || exit 1
exec setsid .venv/bin/python scripts/translate_words.py \\
    --limit {limit} --learning-first --sleep {sleep} --email {email} \\
    >> {log} 2>&1 < /dev/null
"""

#: Перенос готовых переводов: заполняет только пустые строки серверной базы.
MERGE = """\
import json, sqlite3
rows = json.load(open({payload!r}, encoding='utf-8'))
c = sqlite3.connect({db!r})
applied = 0
for r in rows:
    cur = c.execute(
        "update words set "
        "russian_definition = coalesce(nullif(russian_definition, ''), ?), "
        "russian_example = coalesce(nullif(russian_example, ''), ?) "
        "where word = ?",
        (r['rd'] or None, r['re'] or None, r['word']),
    )
    applied += cur.rowcount
c.commit()
q = lambda s: c.execute(s).fetchone()[0]
print('rows_from_local', len(rows))
print('rows_applied', applied)
print('ru_filled', q("select count(*) from words where coalesce(russian_definition, '') <> ''"))
"""

@dataclass
class Result:
    """Результат удалённой команды: команда, вывод и код возврата."""

    cmd: str
    out: str
    code: int

    @property
    def ok(self) -> bool:
        return self.code == 0

    def report(self) -> None:
        """Печатает команду и её вывод — это и есть журнал деплоя."""
        status = "ok" if self.ok else f"код {self.code}"
        print(f"\n$ {self.cmd}  [{status}]")
        if self.out.strip():
            print(self.out.strip())


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    """Читает ``.deploy.env`` (``KEY=VALUE``, решётки — комментарии)."""
    if not path.is_file():
        sys.exit(f"Нет файла {path} — скопируйте .deploy.env.example и впишите доступы.")
    data: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    for required in ("SSH_HOST", "SSH_USER", "SSH_PASSWORD"):
        if not data.get(required):
            sys.exit(f"В {path} не заполнено {required}")
    return data


class Remote:
    """SSH-сессия с обёртками: sudo с паролем из stdin, временные скрипты, SFTP."""

    def __init__(self, env: dict[str, str]) -> None:
        self.host = env["SSH_HOST"]
        self.port = int(env.get("SSH_PORT", "22"))
        self.user = env["SSH_USER"]
        self.password = env["SSH_PASSWORD"]
        self.project = env.get("SSH_PROJECT_DIR", "/opt/tele_eng")
        self.service = env.get("SSH_SERVICE", "tele-eng-bot")
        self.run_user = env.get("SSH_RUN_USER", "teleeng")
        self.email = env.get("MYMEMORY_EMAIL", "")
        self.python = f"{self.project}/.venv/bin/python"
        self.client = paramiko.SSHClient()

    def __enter__(self) -> "Remote":
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            self.host,
            port=self.port,
            username=self.user,
            password=self.password,
            timeout=15,
            allow_agent=False,
            look_for_keys=False,
        )
        print(f"Подключился: {self.user}@{self.host}:{self.port}")
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.client.close()

    def run(
        self,
        cmd: str,
        *,
        script: str | None = None,
        password_stdin: bool = False,
        timeout: int = 300,
    ) -> Result:
        """Выполняет команду.

        ``script`` уходит в stdin (удобно для многострочных скриптов без кавычек),
        ``password_stdin`` отдаёт пароль команде ``sudo -S`` — она уже должна
        начинаться с ``sudo -S -p ''``.
        """
        stdin, stdout, stderr = self.client.exec_command(cmd, timeout=timeout)
        if password_stdin:
            stdin.write(self.password + "\n")
        if script is not None:
            stdin.write(script)
        if password_stdin or script is not None:
            stdin.flush()
            try:
                stdin.channel.shutdown_write()
            except Exception:  # noqa: BLE001 - канал мог закрыться сам
                pass
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        return Result(cmd, out + err, stdout.channel.recv_exit_status())

    def sudo(self, cmd: str, *, timeout: int = 300) -> Result:
        """Команда с правами root: пароль подаётся в stdin и в вывод не попадает."""
        return self.run(f"sudo -S -p '' {cmd}", password_stdin=True, timeout=timeout)

    def as_run_user(self, script: str, *, timeout: int = 900) -> Result:
        """Выполняет shell-скрипт от имени ``run_user`` (владельца проекта)."""
        name = "/tmp/tele_eng_step.sh"
        self.run(f"cat > {name}", script=script)
        try:
            return self.sudo(f"sudo -u {self.run_user} sh {name}", timeout=timeout)
        finally:
            self.run(f"rm -f {name}")

    def run_python(self, code: str, *, timeout: int = 300) -> Result:
        """Выполняет python-код интерпретатором venv от имени ``run_user``."""
        name = "/tmp/tele_eng_step.py"
        self.run(f"cat > {name}", script=code)
        try:
            return self.sudo(f"sudo -u {self.run_user} {self.python} {name}", timeout=timeout)
        finally:
            self.run(f"rm -f {name}")

    def put_text(self, text: str, remote_path: str) -> int:
        """Заливает текстовый файл (UTF-8) и возвращает размер в байтах."""
        payload = text.encode("utf-8")
        with self.client.open_sftp() as sftp:
            sftp.putfo(io.BytesIO(payload), remote_path)
        return len(payload)


def dump_local_translations(db_path: Path) -> list[dict[str, str]]:
    """Слова с готовым русским текстом из локальной базы (подключение только на чтение).

    Перенос готовых переводов экономит суточный лимит MyMemory: сервер не
    переспрашивает сервис о том, что уже переведено на ноутбуке.
    """
    if not db_path.is_file():
        sys.exit(f"Нет локальной базы {db_path}")
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "select word, russian_definition, russian_example from words "
            "where coalesce(russian_definition, '') <> '' "
            "or coalesce(russian_example, '') <> ''"
        ).fetchall()
    finally:
        conn.close()
    return [{"word": word, "rd": rd or "", "re": re or ""} for word, rd, re in rows]


def cmd_status(remote: Remote) -> int:
    """Что на сервере сейчас: ревизия, сервис, настройки перевода, база, свежие логи."""
    remote.run(f"git -C {remote.project} log --oneline -1").report()
    remote.run(f"git -C {remote.project} status --short --branch").report()
    remote.sudo(f"systemctl is-active {remote.service}; systemctl is-enabled {remote.service}").report()
    remote.sudo(f"systemctl --no-pager status {remote.service} | head -10").report()
    remote.sudo(
        f"grep -E '^(TRANSLATION_ENABLED|MYMEMORY_EMAIL|BOT_TOKEN)=' {remote.project}/.env "
        "| sed -e 's/^\\(BOT_TOKEN=\\).*/\\1<hidden>/'"
    ).report()
    remote.run_python(DB_STATS.format(db=f"{remote.project}/bot.db")).report()
    remote.sudo("pgrep -af translate_words.py || echo 'пакетный перевод не идёт'").report()
    remote.run("tail -5 /tmp/*translate*.log 2>/dev/null || echo 'логов перевода нет'").report()
    remote.sudo(f"journalctl -u {remote.service} -n 12 --no-pager").report()
    return 0


def cmd_update(remote: Remote) -> int:
    """Остановить сервис, обновить код из git, поставить зависимости, запустить."""
    print("\n=== Ревизия до обновления ===")
    remote.run(f"git -C {remote.project} log --oneline -1").report()

    print("\n=== Обновление ===")
    remote.sudo(f"systemctl stop {remote.service}").report()
    pull = remote.as_run_user(
        f"#!/bin/sh\nset -e\ncd {remote.project}\nGIT_TERMINAL_PROMPT=0 git pull --ff-only\n",
        timeout=300,
    )
    pull.report()
    if not pull.ok:
        print("\n!!! git pull не прошёл — поднимаю сервис на прежней ревизии")
        remote.sudo(f"systemctl start {remote.service}").report()
        return 1
    remote.run(f"git -C {remote.project} log --oneline -1").report()

    remote.as_run_user(
        f"#!/bin/sh\nset -e\ncd {remote.project}\n.venv/bin/python -m pip install -r requirements.txt\n",
        timeout=900,
    ).report()
    remote.as_run_user(ENV_PATCH.format(project=remote.project, email=remote.email)).report()

    print("\n=== Запуск ===")
    remote.sudo(f"systemctl start {remote.service}").report()
    # Миграции схемы выполняются при старте (колонки русского текста) — даём время.
    time.sleep(10)
    remote.sudo(f"systemctl is-active {remote.service}").report()
    remote.run_python(DB_STATS.format(db=f"{remote.project}/bot.db")).report()
    remote.sudo(f"journalctl -u {remote.service} -n 20 --no-pager").report()
    return 0


def cmd_translate(remote: Remote, *, limit: int, sleep: float, wait: int) -> int:
    """Запускает пакетный перевод словаря на сервере (в фоне) и показывает лог.

    Повторный прогон, когда перевод уже идёт, не запускается: два процесса зря
    делили бы суточный лимит MyMemory.
    """
    running = remote.sudo("pgrep -af translate_words.py")
    if running.ok and running.out.strip():
        print("\nПеревод уже идёт — второй запуск не нужен:")
        print(running.out.strip())
    else:
        batch = BATCH.format(
            project=remote.project,
            limit=limit,
            sleep=sleep,
            email=remote.email,
            log=REMOTE_LOG,
        )
        remote.run("cat > /tmp/tele_eng_batch.sh", script=batch)
        # `sudo -b` уводит команду в фон: SSH-канал закрывается сразу, а перевод
        # продолжает идти на сервере и пишет лог в REMOTE_LOG.
        try:
            remote.sudo(f"-b sudo -u {remote.run_user} sh /tmp/tele_eng_batch.sh", timeout=30)
        except Exception as exc:  # noqa: BLE001 - канал может закрыться на фоне
            print(f"Фоновый запуск: {exc} (это нормально, проверяю процесс ниже)")
        remote.run("rm -f /tmp/tele_eng_batch.sh")
        time.sleep(wait)
        remote.sudo("pgrep -af translate_words.py || echo 'ПРОЦЕСС НЕ ЗАПУСТИЛСЯ'").report()

    remote.run("tail -25 /tmp/*translate*.log 2>/dev/null || echo 'логов пока нет'").report()
    remote.run_python(DB_STATS.format(db=f"{remote.project}/bot.db")).report()
    print("\nПрогресс и итог: python deploy/remote_deploy.py status")
    return 0


def cmd_copy_translations(remote: Remote) -> int:
    """Переносит готовые русские строки из локальной базы в серверную.

    Заполняются только пустые строки: то, что уже есть на сервере, не затирается.
    """
    rows = dump_local_translations(ROOT / "bot.db")
    print(f"Готовых переводов в локальной базе: {len(rows)}")
    if not rows:
        print("Переносить нечего.")
        return 0

    payload_path = "/tmp/tele_eng_translations.json"
    size = remote.put_text(json.dumps(rows, ensure_ascii=False), payload_path)
    print(f"Загружено на сервер: {payload_path} ({size} байт)")
    try:
        result = remote.run_python(
            MERGE.format(payload=payload_path, db=f"{remote.project}/bot.db"),
            timeout=600,
        )
        result.report()
    finally:
        remote.run(f"rm -f {payload_path}")
    return 0 if result.ok else 1


def build_parser() -> argparse.ArgumentParser:
    """Разбор аргументов командной строки."""
    parser = argparse.ArgumentParser(
        prog="deploy/remote_deploy.py",
        description="Обновление бота на сервере по SSH (доступы — в .deploy.env).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="что на сервере сейчас: ревизия, сервис, настройки, база, логи")
    sub.add_parser("update", help="остановить сервис, git pull, зависимости, запустить")
    translate = sub.add_parser("translate", help="пакетный перевод словаря на сервере (в фоне)")
    translate.add_argument("--limit", type=int, default=500, help="сколько слов за прогон")
    translate.add_argument("--sleep", type=float, default=0.6, help="пауза между запросами, сек")
    translate.add_argument("--wait", type=int, default=20, help="секунд до показа лога")
    sub.add_parser("copy-translations", help="перенести готовые переводы из локального bot.db")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Точка входа: подключается к серверу и выполняет выбранную команду."""
    # На Windows консоль по умолчанию cp1251: «●» из `systemctl status` и русские
    # заголовки ломали бы print, поэтому вывод принудительно переводим в UTF-8.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):  # pragma: no cover - поток может быть переопределён
            pass
    args = build_parser().parse_args(argv)
    env = load_env()
    with Remote(env) as remote:
        if args.command == "status":
            return cmd_status(remote)
        if args.command == "update":
            return cmd_update(remote)
        if args.command == "translate":
            return cmd_translate(remote, limit=args.limit, sleep=args.sleep, wait=args.wait)
        if args.command == "copy-translations":
            return cmd_copy_translations(remote)
    return 2


if __name__ == "__main__":
    sys.exit(main())


