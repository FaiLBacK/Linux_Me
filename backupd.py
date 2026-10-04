#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backupd — демон регулярного резервного копирования каталога.

Читает INI-конфигурацию, по расписанию (интервал) делает копию исходного
каталога в каталог назначения с временной меткой в имени, пишет в syslog,
хранит статус в JSON-файле и управляется сигналами:

    SIGTERM / SIGINT  — корректно остановиться (текущая копия прерывается,
                        временные файлы удаляются)
    SIGHUP            — перечитать конфигурацию
    SIGUSR1           — сделать резервную копию немедленно

Запуск: backupd.py --foreground (под systemd) или без флага (классический
демон с двойным fork). Только стандартная библиотека Python 3.8+.
"""
from __future__ import annotations

import argparse
import configparser
import fcntl
import fnmatch
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import stat
import sys
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

__version__ = "1.0.0"

DEFAULT_CONFIG = "/etc/backupd/backupd.conf"
MIN_INTERVAL = 10       # секунд; защита от «забивания» системы
RETRY_DELAY = 300       # через сколько повторить после неудачи (не больше интервала)
STARTUP_DELAY = 5       # пауза после старта до первой проверки расписания

log = logging.getLogger("backupd")

# Допустимые ключи конфигурации (используются также backupctl)
KNOWN_KEYS = {
    "backup": ("source", "destination", "interval", "mode", "prefix",
               "keep", "exclude", "verify", "compresslevel"),
    "daemon": ("pidfile", "statusfile", "log_level", "nice"),
}


class ConfigError(Exception):
    """Ошибка в конфигурации."""


class BackupError(Exception):
    """Ошибка при создании резервной копии."""


class Interrupted(Exception):
    """Копирование прервано сигналом остановки."""


# --------------------------------------------------------------------------
# Конфигурация
# --------------------------------------------------------------------------
@dataclass
class Config:
    source: str
    destination: str
    interval: int
    mode: str = "archive"
    prefix: str = "backup"
    keep: int = 7
    exclude: list = field(default_factory=list)
    verify: bool = True
    compresslevel: int = 6
    pidfile: str = "/run/backupd/backupd.pid"
    statusfile: str = "/var/lib/backupd/status.json"
    log_level: str = "INFO"
    nice: int = 10


def parse_interval(value) -> int:
    """'90' -> 90 с, '30m', '6h', '1d', '2w' -> секунды."""
    m = re.fullmatch(r"\s*(\d+)\s*([smhdw]?)\s*", str(value), re.IGNORECASE)
    if not m:
        raise ConfigError(f"неверный интервал: {value!r} (примеры: 90, 30m, 6h, 1d)")
    mult = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    return int(m.group(1)) * mult[m.group(2).lower()]


def _int(name: str, value: str, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except ValueError:
        raise ConfigError(f"параметр {name} должен быть целым числом, получено {value!r}")
    if not lo <= n <= hi:
        raise ConfigError(f"параметр {name} должен быть в диапазоне {lo}..{hi}")
    return n


def _bool(name: str, value: str) -> bool:
    v = value.strip().lower()
    if v in ("1", "yes", "true", "on"):
        return True
    if v in ("0", "no", "false", "off"):
        return False
    raise ConfigError(f"параметр {name} должен быть yes/no, получено {value!r}")


def _check_perms(path: str) -> None:
    """Конфиг не должен быть доступен на запись посторонним (демон часто root)."""
    st = os.stat(path)
    if st.st_mode & stat.S_IWOTH:
        raise ConfigError(f"{path} доступен на запись всем — небезопасно (chmod 600)")
    if st.st_uid not in (0, os.geteuid()):
        raise ConfigError(f"{path} принадлежит постороннему пользователю (uid={st.st_uid})")
    if st.st_mode & stat.S_IWGRP:
        log.warning("%s доступен на запись группе — рекомендуется chmod 600", path)


def load_config(path: str, check_perms: bool = True) -> Config:
    if not os.path.isfile(path):
        raise ConfigError(f"конфигурационный файл не найден: {path}")
    if check_perms:
        _check_perms(path)

    cp = configparser.ConfigParser(interpolation=None,
                                   inline_comment_prefixes=("#", ";"))
    try:
        with open(path, encoding="utf-8") as f:
            cp.read_file(f)
    except (OSError, configparser.Error, UnicodeDecodeError) as e:
        raise ConfigError(f"не удалось прочитать {path}: {e}")

    def opt(sec: str, key: str, default=None) -> str:
        if cp.has_option(sec, key):
            return cp.get(sec, key).strip()
        if default is None:
            raise ConfigError(f"в секции [{sec}] не задан обязательный параметр {key}")
        return default

    source = opt("backup", "source")
    destination = opt("backup", "destination")
    for name, val in (("source", source), ("destination", destination)):
        if not os.path.isabs(val):
            raise ConfigError(f"{name} должен быть абсолютным путём: {val!r}")
    source, destination = os.path.normpath(source), os.path.normpath(destination)
    if not os.path.isdir(source):
        raise ConfigError(f"исходный каталог не существует: {source}")
    if os.path.realpath(source) == os.path.realpath(destination):
        raise ConfigError("source и destination не могут совпадать")

    interval = parse_interval(opt("backup", "interval"))
    if interval < MIN_INTERVAL:
        raise ConfigError(f"interval не может быть меньше {MIN_INTERVAL} с")

    mode = opt("backup", "mode", "archive").lower()
    if mode not in ("archive", "copy"):
        raise ConfigError("mode должен быть archive или copy")

    prefix = opt("backup", "prefix", "backup")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", prefix):
        raise ConfigError("prefix может содержать только буквы, цифры и . _ -")

    level = opt("daemon", "log_level", "INFO").upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError("log_level: DEBUG, INFO, WARNING или ERROR")

    pidfile = opt("daemon", "pidfile", Config.pidfile)
    statusfile = opt("daemon", "statusfile", Config.statusfile)
    for name, val in (("pidfile", pidfile), ("statusfile", statusfile)):
        if not os.path.isabs(val):
            raise ConfigError(f"{name} должен быть абсолютным путём")

    return Config(
        source=source,
        destination=destination,
        interval=interval,
        mode=mode,
        prefix=prefix,
        keep=_int("keep", opt("backup", "keep", "7"), 0, 100000),
        exclude=[p.strip() for p in opt("backup", "exclude", "").split(",") if p.strip()],
        verify=_bool("verify", opt("backup", "verify", "yes")),
        compresslevel=_int("compresslevel", opt("backup", "compresslevel", "6"), 1, 9),
        pidfile=pidfile,
        statusfile=statusfile,
        log_level=level,
        nice=_int("nice", opt("daemon", "nice", "10"), 0, 19),
    )


# --------------------------------------------------------------------------
# Резервное копирование
# --------------------------------------------------------------------------
def _excluded(path: str, src: str, patterns, skip) -> bool:
    if skip and (path == skip or path.startswith(skip + os.sep)):
        return True
    rel = os.path.relpath(path, src)
    name = os.path.basename(path)
    return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p) for p in patterns)


def _iter_tree(src, patterns, skip, stop, errors):
    """Обходит дерево без перехода по симлинкам; проверяет флаг остановки."""
    def onerror(e):
        errors.append(e)
        log.warning("не удалось прочитать каталог: %s", e)

    yield src
    for root, dirs, files in os.walk(src, onerror=onerror):
        if stop.is_set():
            raise Interrupted()
        dirs[:] = sorted(d for d in dirs
                         if not _excluded(os.path.join(root, d), src, patterns, skip))
        yield from (os.path.join(root, d) for d in dirs)
        for f in sorted(files):
            p = os.path.join(root, f)
            if not _excluded(p, src, patterns, skip):
                yield p


def _final_name(dest: str, cfg: Config) -> str:
    """Имя с временной меткой; при коллизии (две копии за секунду) ждём."""
    while True:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{cfg.prefix}_{stamp}" + (".tar.gz" if cfg.mode == "archive" else "")
        if not os.path.exists(os.path.join(dest, name)):
            return os.path.join(dest, name)
        time.sleep(1)


def _fsync_path(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_archive(cfg, src, dest, final, skip, stop):
    base = os.path.basename(src) or "root"
    errors, entries, files = [], 0, 0
    fd, tmp = tempfile.mkstemp(dir=dest, prefix=".tmp-", suffix=".tar.gz")  # 0600
    try:
        with os.fdopen(fd, "wb") as raw, \
                tarfile.open(fileobj=raw, mode="w:gz", compresslevel=cfg.compresslevel) as tar:
            for p in _iter_tree(src, cfg.exclude, skip, stop, errors):
                arc = base if p == src else os.path.join(base, os.path.relpath(p, src))
                try:
                    tar.add(p, arcname=arc, recursive=False)
                except OSError as e:          # файл исчез/нет прав — пропускаем
                    errors.append(e)
                    log.warning("пропущен %s: %s", p, e)
                    continue
                entries += 1
                if not os.path.isdir(p) or os.path.islink(p):
                    files += 1
        if cfg.verify:
            with tarfile.open(tmp, "r:gz") as t:       # читает поток целиком → проверяется CRC
                found = sum(1 for _ in t)
            if found != entries:
                raise BackupError(f"проверка архива не пройдена: {found} из {entries} записей")
        _fsync_path(tmp)
        os.replace(tmp, final)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return os.path.getsize(final), files, len(errors)


def _write_copy(cfg, src, dest, final, skip, stop):
    errors, files, size = [], 0, 0
    tmp = tempfile.mkdtemp(dir=dest, prefix=".tmp-")  # 0700
    try:
        for p in _iter_tree(src, cfg.exclude, skip, stop, errors):
            target = os.path.join(tmp, os.path.relpath(p, src))
            try:
                if os.path.islink(p):
                    os.symlink(os.readlink(p), target)
                elif os.path.isdir(p):
                    os.makedirs(target, exist_ok=True)
                    continue
                else:
                    shutil.copy2(p, target)
                    size += os.path.getsize(target)
                files += 1
            except OSError as e:
                errors.append(e)
                log.warning("пропущен %s: %s", p, e)
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return size, files, len(errors)


def apply_retention(cfg: Config) -> int:
    """Удаляет самые старые копии, оставляя cfg.keep. Трогает только наши имена."""
    if cfg.keep <= 0:
        return 0
    rx = re.compile(rf"^{re.escape(cfg.prefix)}_\d{{8}}_\d{{6}}(\.tar\.gz)?$")
    items = sorted(n for n in os.listdir(cfg.destination) if rx.match(n))
    removed = 0
    for name in items[:-cfg.keep]:
        path = os.path.join(cfg.destination, name)
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
            removed += 1
            log.info("удалена устаревшая копия: %s", name)
        except OSError as e:
            log.error("не удалось удалить %s: %s", name, e)
    return removed


def run_backup(cfg: Config, stop: threading.Event | None = None) -> dict:
    """Делает одну резервную копию. Возвращает словарь с результатом."""
    stop = stop or threading.Event()
    t0 = time.monotonic()
    src = os.path.realpath(cfg.source)
    if not os.path.isdir(src):
        raise BackupError(f"исходный каталог недоступен: {cfg.source}")
    os.makedirs(cfg.destination, mode=0o700, exist_ok=True)
    dest = os.path.realpath(cfg.destination)
    if os.stat(dest).st_mode & 0o077:
        log.warning("каталог назначения %s доступен посторонним (рекомендуется chmod 700)", dest)
    # если каталог назначения лежит внутри источника — не копируем его в себя
    skip = dest if dest.startswith(src + os.sep) else None

    final = _final_name(dest, cfg)
    writer = _write_archive if cfg.mode == "archive" else _write_copy
    size, files, warnings = writer(cfg, src, dest, final, skip, stop)

    removed = 0
    try:
        removed = apply_retention(cfg)
    except OSError as e:
        log.error("ротация копий не удалась: %s", e)
    return {"path": final, "size": size, "files": files, "warnings": warnings,
            "removed": removed, "duration": round(time.monotonic() - t0, 2)}


# --------------------------------------------------------------------------
# Статус (для мониторинга)
# --------------------------------------------------------------------------
class Status:
    def __init__(self, path: str):
        self.path = path
        self.data: dict = {}

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def update(self, **kw) -> None:
        self.data.update(kw)
        try:
            d = os.path.dirname(self.path)
            if not os.path.isdir(d):
                os.makedirs(d)
                os.chmod(d, 0o755)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.chmod(tmp, 0o644)   # в статусе нет секретов — читать может мониторинг
            os.replace(tmp, self.path)
        except OSError as e:
            log.error("не удалось записать статус %s: %s", self.path, e)


# --------------------------------------------------------------------------
# Демон
# --------------------------------------------------------------------------
class Daemon:
    def __init__(self, cfg: Config, config_path: str):
        self.cfg = cfg
        self.config_path = config_path
        self.status = Status(cfg.statusfile)
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.reload_req = False
        self.now_req = False
        self._pid_fd = None

    # --- сигналы (в обработчиках только флаги: это безопасно) ---
    def _on_signal(self, signum, _frame):
        if signum in (signal.SIGTERM, signal.SIGINT):
            self.stop.set()
        elif signum == signal.SIGHUP:
            self.reload_req = True
        elif signum == signal.SIGUSR1:
            self.now_req = True
        self.wake.set()

    def _install_signals(self):
        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGUSR1):
            signal.signal(s, self._on_signal)
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    # --- pid-файл с блокировкой: второй экземпляр не запустится ---
    def _lock_pidfile(self):
        path = self.cfg.pidfile
        os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise SystemExit(f"backupd уже запущен (pid-файл {path} заблокирован)")
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._pid_fd = fd

    def _unlock_pidfile(self):
        if self._pid_fd is not None:
            try:
                os.unlink(self.cfg.pidfile)
            except OSError:
                pass
            os.close(self._pid_fd)
            self._pid_fd = None

    # --- планирование ---
    def _initial_next_run(self) -> float:
        now = time.time()
        last = self.status.data.get("last_success_ts")
        return max(last + self.cfg.interval, now + STARTUP_DELAY) if last else now + STARTUP_DELAY

    def _reschedule(self) -> float:
        base = self.status.data.get("last_success_ts") or time.time()
        return max(base + self.cfg.interval, time.time() + 1)

    def _reload(self) -> bool:
        try:
            new = load_config(self.config_path)
        except ConfigError as e:
            log.error("перечитать конфигурацию не удалось, остаётся прежняя: %s", e)
            return False
        for name in ("pidfile", "statusfile", "nice"):
            if getattr(new, name) != getattr(self.cfg, name):
                log.warning("параметр %s изменится только после перезапуска демона", name)
                setattr(new, name, getattr(self.cfg, name))
        self.cfg = new
        log.setLevel(new.log_level)
        self.status.update(interval=new.interval, source=new.source, destination=new.destination)
        log.info("конфигурация перечитана (интервал=%dс, режим=%s, keep=%d)",
                 new.interval, new.mode, new.keep)
        return True

    def _do_backup(self) -> bool:
        t = time.time()
        self.status.update(state="running", last_run_ts=t)
        log.info("начало резервного копирования: %s -> %s (%s)",
                 self.cfg.source, self.cfg.destination, self.cfg.mode)
        d = self.status.data
        try:
            r = run_backup(self.cfg, self.stop)
        except Interrupted:
            log.warning("резервное копирование прервано остановкой демона")
            self.status.update(last_status="interrupted")
            return False
        except Exception as e:   # любая ошибка не должна убивать демон
            log.error("резервное копирование не удалось: %s", e, exc_info=not isinstance(e, BackupError))
            self.status.update(last_status="failed", last_error=str(e),
                               total_failed=d.get("total_failed", 0) + 1,
                               consecutive_failures=d.get("consecutive_failures", 0) + 1)
            return False
        st = "ok" if r["warnings"] == 0 else "warning"
        log.info("копия готова: %s (файлов=%d, размер=%d Б, предупреждений=%d, %.2f с, удалено старых=%d)",
                 r["path"], r["files"], r["size"], r["warnings"], r["duration"], r["removed"])
        self.status.update(last_status=st, last_error=None, last_success_ts=time.time(),
                           last_archive=r["path"], last_size=r["size"], last_files=r["files"],
                           last_warnings=r["warnings"], last_duration=r["duration"],
                           total_ok=d.get("total_ok", 0) + 1, consecutive_failures=0)
        return True

    def run(self) -> int:
        self._lock_pidfile()
        self._install_signals()
        try:
            self.status.load()
            self.status.update(pid=os.getpid(), version=__version__, state="idle",
                               started_ts=time.time(), interval=self.cfg.interval,
                               source=self.cfg.source, destination=self.cfg.destination)
            log.info("демон запущен (pid=%d, источник=%s, назначение=%s, интервал=%dс)",
                     os.getpid(), self.cfg.source, self.cfg.destination, self.cfg.interval)
            next_run = self._initial_next_run()
            while not self.stop.is_set():
                self.status.update(state="idle", next_run_ts=next_run)
                self.wake.wait(max(0.0, next_run - time.time()))
                self.wake.clear()
                if self.stop.is_set():
                    break
                if self.reload_req:
                    self.reload_req = False
                    if self._reload():
                        next_run = self._reschedule()
                if self.now_req or time.time() >= next_run - 0.5:
                    if self.now_req:
                        log.info("получен запрос на немедленное копирование")
                    self.now_req = False
                    ok = self._do_backup()
                    if self.stop.is_set():
                        break
                    delay = self.cfg.interval if ok else min(self.cfg.interval, RETRY_DELAY)
                    next_run = time.time() + delay
            return 0
        finally:
            self.status.update(state="stopped", next_run_ts=None)
            log.info("демон остановлен")
            self._unlock_pidfile()


# --------------------------------------------------------------------------
# Запуск
# --------------------------------------------------------------------------
def daemonize() -> None:
    """Классический двойной fork: отсоединяемся от терминала и сессии."""
    if os.fork():
        os._exit(0)
    os.setsid()
    if os.fork():
        os._exit(0)
    os.chdir("/")
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)


def setup_logging(level: str, to_stderr: bool) -> None:
    log.handlers.clear()
    log.setLevel(level)
    log.propagate = False
    handler = None
    try:
        handler = logging.handlers.SysLogHandler(
            address="/dev/log", facility=logging.handlers.SysLogHandler.LOG_DAEMON)
        handler.ident = f"backupd[{os.getpid()}]: "
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        log.addHandler(handler)
    except OSError:
        to_stderr = True   # нет /dev/log (контейнер, тест) — пишем в stderr
    if to_stderr:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(asctime)s backupd[%(process)d] %(levelname)s %(message)s"))
        log.addHandler(h)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Демон регулярного резервного копирования")
    ap.add_argument("-c", "--config", default=os.environ.get("BACKUPD_CONFIG", DEFAULT_CONFIG),
                    help=f"файл конфигурации (по умолчанию {DEFAULT_CONFIG})")
    ap.add_argument("-f", "--foreground", action="store_true",
                    help="не демонизироваться (для systemd и отладки)")
    ap.add_argument("--check-config", action="store_true", help="проверить конфигурацию и выйти")
    ap.add_argument("--once", action="store_true", help="сделать одну копию и выйти")
    ap.add_argument("-V", "--version", action="version", version=f"backupd {__version__}")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"backupd: ошибка конфигурации: {e}", file=sys.stderr)
        return 78   # EX_CONFIG
    if args.check_config:
        print("конфигурация корректна")
        return 0

    os.umask(0o077)                    # всё, что создаёт демон, закрыто для посторонних
    try:
        os.nice(cfg.nice)              # низкий приоритет, чтобы не мешать системе
    except OSError:
        pass

    if args.once:
        setup_logging(cfg.log_level, True)
        try:
            r = run_backup(cfg)
        except Exception as e:
            log.error("резервное копирование не удалось: %s", e)
            return 1
        log.info("копия готова: %s (файлов=%d, %.2f с)", r["path"], r["files"], r["duration"])
        return 0

    if not args.foreground:
        daemonize()
    setup_logging(cfg.log_level, args.foreground and sys.stderr.isatty())
    return Daemon(cfg, args.config).run()


if __name__ == "__main__":
    sys.exit(main())
