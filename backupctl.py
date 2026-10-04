#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backupctl — управление и мониторинг демона backupd.

  backupctl status        состояние демона и последней копии
  backupctl check         проверка для мониторинга (коды Nagios: 0/1/2/3)
  backupctl list          список имеющихся копий
  backupctl backup-now    запустить копирование немедленно (SIGUSR1)
  backupctl reload        перечитать конфиг (SIGHUP)
  backupctl config        показать конфигурацию
  backupctl set K V       изменить параметр, например: set backup.interval 2h
  backupctl logs          последние записи журнала
  backupctl start|stop|restart|enable|disable   управление через systemctl
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import backupd  # noqa: E402
from backupd import KNOWN_KEYS, ConfigError, load_config  # noqa: E402

UNIT = "backupd"
EXIT_OK, EXIT_WARN, EXIT_CRIT, EXIT_UNKNOWN = 0, 1, 2, 3


# ------------------------------ вспомогательное ------------------------------
def human_size(n) -> str:
    n = float(n or 0)
    for unit in ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ"):
        if n < 1024 or unit == "ТиБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024


def human_delta(sec: float) -> str:
    sec = int(abs(sec))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = [f"{d} д" if d else "", f"{h} ч" if h else "", f"{m} мин" if m else ""]
    return " ".join(p for p in parts if p) or f"{s} с"


def fmt_ts(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "—"


def read_status(cfg) -> dict:
    st = backupd.Status(cfg.statusfile)
    st.load()
    return st.data


def daemon_pid(st: dict):
    """pid работающего демона или None (с защитой от повторного использования pid)."""
    pid = st.get("pid")
    if not pid or st.get("state") == "stopped":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            if b"backupd" not in f.read():
                return None
    except OSError:
        pass
    return pid


def load(args):
    try:
        return load_config(args.config)
    except ConfigError as e:
        print(f"Ошибка конфигурации: {e}", file=sys.stderr)
        sys.exit(EXIT_UNKNOWN)


# ------------------------------ команды ------------------------------
def cmd_status(args):
    cfg = load(args)
    st = read_status(cfg)
    pid = daemon_pid(st)
    now = time.time()
    state_ru = {"idle": "ожидание", "running": "идёт копирование", "stopped": "остановлен"}
    print(f"Демон:            {'работает (pid %d)' % pid if pid else 'НЕ ЗАПУЩЕН'}"
          + (f", состояние: {state_ru.get(st.get('state'), st.get('state'))}" if pid else ""))
    print(f"Источник:         {cfg.source}")
    print(f"Назначение:       {cfg.destination}  (режим: {cfg.mode}, хранить: {cfg.keep or 'все'})")
    print(f"Интервал:         {human_delta(cfg.interval)}")
    if not st:
        print("Статус:           файл статуса ещё не создан")
        return EXIT_OK
    res = {"ok": "успешно", "warning": "с предупреждениями", "failed": "ОШИБКА",
           "interrupted": "прервана"}.get(st.get("last_status"), "—")
    print(f"Последняя копия:  {fmt_ts(st.get('last_run_ts'))} — {res}")
    if st.get("last_success_ts"):
        print(f"  успешная:       {fmt_ts(st['last_success_ts'])} "
              f"({human_delta(now - st['last_success_ts'])} назад)")
        print(f"  файл:           {st.get('last_archive')}")
        print(f"  размер/файлов:  {human_size(st.get('last_size'))} / {st.get('last_files')}"
              f", {st.get('last_duration')} с")
    if st.get("last_error"):
        print(f"  ошибка:         {st['last_error']}")
    if pid and st.get("next_run_ts"):
        print(f"Следующая:        {fmt_ts(st['next_run_ts'])} "
              f"(через {human_delta(st['next_run_ts'] - now)})")
    print(f"Итого:            успешно {st.get('total_ok', 0)}, ошибок {st.get('total_failed', 0)}, "
          f"подряд ошибок {st.get('consecutive_failures', 0)}")
    return EXIT_OK


def cmd_check(args):
    """Для Nagios/Icinga/Zabbix/cron: одна строка + код возврата 0/1/2/3."""
    cfg = load(args)
    st = read_status(cfg)
    if not st:
        print("UNKNOWN: файл статуса отсутствует или нечитаем")
        return EXIT_UNKNOWN
    if not daemon_pid(st):
        print("CRITICAL: демон backupd не запущен")
        return EXIT_CRIT
    now = time.time()
    last = st.get("last_success_ts")
    grace = max(300, cfg.interval // 10)
    perf = (f"| ok={st.get('total_ok', 0)} failed={st.get('total_failed', 0)} "
            f"age={int(now - last) if last else -1}s size={st.get('last_size', 0)}B")
    if st.get("last_status") == "failed":
        print(f"CRITICAL: последняя копия не удалась: {st.get('last_error')} {perf}")
        return EXIT_CRIT
    if last is None:
        if now - st.get("started_ts", now) > cfg.interval + grace:
            print(f"CRITICAL: за {human_delta(now - st['started_ts'])} не создано ни одной копии {perf}")
            return EXIT_CRIT
        print(f"WARNING: пока нет ни одной успешной копии {perf}")
        return EXIT_WARN
    age = now - last
    if age > 2 * cfg.interval + grace:
        print(f"CRITICAL: последней успешной копии {human_delta(age)} {perf}")
        return EXIT_CRIT
    if age > cfg.interval + grace:
        print(f"WARNING: копия просрочена, последней {human_delta(age)} {perf}")
        return EXIT_WARN
    if st.get("last_status") == "warning":
        print(f"WARNING: последняя копия с предупреждениями ({st.get('last_warnings')}) {perf}")
        return EXIT_WARN
    print(f"OK: последняя копия {human_delta(age)} назад, {human_size(st.get('last_size'))} {perf}")
    return EXIT_OK


def cmd_list(args):
    cfg = load(args)
    rx = re.compile(rf"^{re.escape(cfg.prefix)}_\d{{8}}_\d{{6}}(\.tar\.gz)?$")
    try:
        names = sorted(n for n in os.listdir(cfg.destination) if rx.match(n))
    except OSError as e:
        print(f"Не удалось прочитать {cfg.destination}: {e}", file=sys.stderr)
        return EXIT_UNKNOWN
    for n in names:
        p = os.path.join(cfg.destination, n)
        if os.path.isdir(p):
            size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(p) for f in fs
                       if not os.path.islink(os.path.join(r, f)))
        else:
            size = os.path.getsize(p)
        print(f"{n:45} {human_size(size):>10}")
    print(f"Всего копий: {len(names)}")
    return EXIT_OK


def _signal(args, sig, what):
    cfg = load(args)
    st = read_status(cfg)
    pid = daemon_pid(st)
    if not pid:
        print("Демон не запущен", file=sys.stderr)
        return EXIT_CRIT
    before = st.get("last_run_ts")
    try:
        os.kill(pid, sig)
    except PermissionError:
        print("Недостаточно прав (нужен root или владелец процесса)", file=sys.stderr)
        return EXIT_UNKNOWN
    print(f"{what}: сигнал отправлен процессу {pid}")
    if getattr(args, "wait", False):
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            time.sleep(1)
            s = read_status(cfg)
            if s.get("last_run_ts") != before and s.get("state") == "idle":
                print(f"Готово: {s.get('last_status')}")
                return EXIT_OK if s.get("last_status") in ("ok", "warning") else EXIT_CRIT
        print("Время ожидания истекло", file=sys.stderr)
        return EXIT_WARN
    return EXIT_OK


def cmd_config(args):
    cfg = load(args)
    for k, v in vars(cfg).items():
        print(f"{k:14} = {', '.join(v) if isinstance(v, list) else v}")
    return EXIT_OK


def set_option(path: str, section: str, key: str, value: str) -> None:
    """Меняет параметр в INI-файле, сохраняя комментарии и порядок строк."""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    new_line = f"{key} = {value}\n"
    cur, replaced, last_in_sec = None, False, None
    key_rx = re.compile(rf"^\s*{re.escape(key)}\s*[=:]", re.IGNORECASE)
    for i, line in enumerate(lines):
        m = re.match(r"^\s*\[(.+?)\]\s*$", line)
        if m:
            cur = m.group(1)
            if cur == section:
                last_in_sec = i
            continue
        if cur == section:
            if line.strip() and not line.lstrip().startswith(("#", ";")):
                last_in_sec = i
                if key_rx.match(line):
                    lines[i] = new_line
                    replaced = True
                    break
    if not replaced:
        if last_in_sec is None:
            lines += [f"\n[{section}]\n", new_line]
        else:
            lines.insert(last_in_sec + 1, new_line)
    # сначала проверяем во временном файле, и только потом подменяем боевой
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".backupd-conf-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(lines)
        load_config(tmp, check_perms=False)
        shutil.copymode(path, tmp)
        try:
            st = os.stat(path)
            os.chown(tmp, st.st_uid, st.st_gid)
        except PermissionError:
            pass
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def cmd_set(args):
    sec, _, key = args.key.rpartition(".")
    if not sec:   # короткая запись: «interval» вместо «backup.interval»
        sec = next((s for s, keys in KNOWN_KEYS.items() if key in keys), "")
    if key not in KNOWN_KEYS.get(sec, ()):
        print("Неизвестный параметр. Допустимые: " +
              ", ".join(f"{s}.{k}" for s, ks in KNOWN_KEYS.items() for k in ks), file=sys.stderr)
        return EXIT_UNKNOWN
    try:
        set_option(args.config, sec, key, args.value)
    except (ConfigError, OSError) as e:
        print(f"Не изменено: {e}", file=sys.stderr)
        return EXIT_UNKNOWN
    print(f"{sec}.{key} = {args.value}")
    if key == "destination":
        print("ВНИМАНИЕ: обновите ReadWritePaths в unit-файле backupd.service "
              "и выполните: systemctl daemon-reload && systemctl restart backupd")
    elif key in ("pidfile", "statusfile", "nice"):
        print("Параметр вступит в силу после перезапуска: backupctl restart")
        return EXIT_OK
    st = read_status(load(args))
    if daemon_pid(st):
        return _signal(args, signal.SIGHUP, "Применение настроек")
    return EXIT_OK


def cmd_logs(args):
    return subprocess.call(["journalctl", "-t", "backupd", "-u", UNIT, "-n", str(args.lines), "--no-pager"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Управление демоном backupd",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("-c", "--config", default=os.environ.get("BACKUPD_CONFIG", backupd.DEFAULT_CONFIG))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("check")
    sub.add_parser("list")
    sub.add_parser("config")
    p = sub.add_parser("backup-now")
    p.add_argument("--wait", action="store_true", help="дождаться завершения копирования")
    p.add_argument("--timeout", type=int, default=600)
    sub.add_parser("reload")
    p = sub.add_parser("set")
    p.add_argument("key", help="например backup.interval или interval")
    p.add_argument("value")
    p = sub.add_parser("logs")
    p.add_argument("-n", "--lines", type=int, default=50)
    for name in ("start", "stop", "restart", "enable", "disable"):
        sub.add_parser(name)
    args = ap.parse_args(argv)

    if args.cmd in ("start", "stop", "restart", "enable", "disable"):
        return subprocess.call(["systemctl", args.cmd, UNIT])
    handlers = {
        "status": cmd_status, "check": cmd_check, "list": cmd_list, "config": cmd_config,
        "set": cmd_set, "logs": cmd_logs,
        "backup-now": lambda a: _signal(a, signal.SIGUSR1, "Запуск копирования"),
        "reload": lambda a: _signal(a, signal.SIGHUP, "Перечитывание конфигурации"),
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
