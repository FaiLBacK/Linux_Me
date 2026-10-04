#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты backupd. Запуск: python3 -m unittest -v test_backupd"""
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import backupctl  # noqa: E402
import backupd  # noqa: E402
from backupd import Config, ConfigError, Interrupted, load_config, parse_interval, run_backup  # noqa: E402


def make_tree(root):
    os.makedirs(os.path.join(root, "sub", "deep"))
    for rel, text in {"a.txt": "alpha", "sub/b.txt": "beta", "sub/deep/c.txt": "gamma",
                      "skip.tmp": "junk", "sub/x.swp": "junk"}.items():
        with open(os.path.join(root, rel), "w") as f:
            f.write(text)
    os.symlink("a.txt", os.path.join(root, "link"))


def write_conf(path, **kw):
    params = {"interval": "1h", "mode": "archive", "keep": "7", "exclude": "*.tmp, *.swp",
              **kw}
    body = "[backup]\n" + "".join(f"{k} = {v}\n" for k, v in params.items())
    if "daemon" in kw:
        pass
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, 0o600)


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name
        self.src = os.path.join(self.tmp, "data")
        self.dst = os.path.join(self.tmp, "backups")
        os.makedirs(self.src)
        make_tree(self.src)
        self.conf = os.path.join(self.tmp, "backupd.conf")

    def tearDown(self):
        self._td.cleanup()

    def cfg(self, **kw):
        return Config(source=self.src, destination=self.dst, interval=3600,
                      exclude=["*.tmp", "*.swp"], **kw)


class TestConfig(Base):
    def test_parse_interval(self):
        self.assertEqual(parse_interval("90"), 90)
        self.assertEqual(parse_interval("30m"), 1800)
        self.assertEqual(parse_interval("6h"), 21600)
        self.assertEqual(parse_interval("1D"), 86400)
        for bad in ("", "abc", "-5", "1.5h", "10x"):
            with self.assertRaises(ConfigError):
                parse_interval(bad)

    def test_valid_config(self):
        write_conf(self.conf, source=self.src, destination=self.dst)
        cfg = load_config(self.conf)
        self.assertEqual(cfg.interval, 3600)
        self.assertEqual(cfg.exclude, ["*.tmp", "*.swp"])

    def test_invalid_configs(self):
        cases = [
            dict(source="relative/path", destination=self.dst),
            dict(source=self.tmp + "/missing", destination=self.dst),
            dict(source=self.src, destination=self.src),
            dict(source=self.src, destination=self.dst, interval="2s"),
            dict(source=self.src, destination=self.dst, mode="zip"),
            dict(source=self.src, destination=self.dst, prefix="bad/name"),
            dict(source=self.src, destination=self.dst, keep="-1"),
        ]
        for kw in cases:
            write_conf(self.conf, **kw)
            with self.assertRaises(ConfigError, msg=str(kw)):
                load_config(self.conf)

    def test_missing_required(self):
        write_conf(self.conf, source=self.src)
        with self.assertRaises(ConfigError):
            load_config(self.conf)

    def test_world_writable_rejected(self):
        write_conf(self.conf, source=self.src, destination=self.dst)
        os.chmod(self.conf, 0o666)
        with self.assertRaises(ConfigError):
            load_config(self.conf)


class TestBackup(Base):
    def test_archive(self):
        r = run_backup(self.cfg())
        name = os.path.basename(r["path"])
        self.assertRegex(name, r"^backup_\d{8}_\d{6}\.tar\.gz$")
        self.assertEqual(stat.S_IMODE(os.stat(r["path"]).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.dst).st_mode), 0o700)
        with tarfile.open(r["path"]) as t:
            names = set(t.getnames())
            self.assertIn("data/a.txt", names)
            self.assertIn("data/sub/deep/c.txt", names)
            self.assertNotIn("data/skip.tmp", names)      # exclude
            self.assertNotIn("data/sub/x.swp", names)
            self.assertTrue(t.getmember("data/link").issym())  # симлинк сохранён как ссылка
        self.assertEqual(r["files"], 4)  # a, b, c, link
        self.assertEqual([n for n in os.listdir(self.dst) if n.startswith(".tmp")], [])

    def test_copy_mode(self):
        r = run_backup(self.cfg(mode="copy"))
        self.assertTrue(os.path.isdir(r["path"]))
        with open(os.path.join(r["path"], "sub", "deep", "c.txt")) as f:
            self.assertEqual(f.read(), "gamma")
        self.assertFalse(os.path.exists(os.path.join(r["path"], "skip.tmp")))
        self.assertTrue(os.path.islink(os.path.join(r["path"], "link")))

    def test_destination_inside_source_is_not_recursed(self):
        inner = os.path.join(self.src, "backups_here")
        cfg = Config(source=self.src, destination=inner, interval=3600)
        r1 = run_backup(cfg)
        time.sleep(1.1)
        r2 = run_backup(cfg)
        with tarfile.open(r2["path"]) as t:
            self.assertFalse([n for n in t.getnames() if "backups_here" in n])
        self.assertNotEqual(r1["path"], r2["path"])

    def test_retention(self):
        cfg = self.cfg(keep=2)
        os.makedirs(self.dst, mode=0o700)
        for stamp in ("20200101_000001", "20200101_000002", "20200101_000003", "20200101_000004"):
            open(os.path.join(self.dst, f"backup_{stamp}.tar.gz"), "w").close()
        foreign = os.path.join(self.dst, "important.tar.gz")      # чужой файл не трогаем
        open(foreign, "w").close()
        r = run_backup(cfg)
        left = sorted(os.listdir(self.dst))
        self.assertEqual(len([n for n in left if n.startswith("backup_")]), 2)
        self.assertIn("important.tar.gz", left)
        self.assertEqual(r["removed"], 3)

    def test_interrupted_leaves_no_garbage(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(Interrupted):
            run_backup(self.cfg(), stop)
        self.assertEqual(os.listdir(self.dst), [])

    def test_unreadable_file_gives_warning_not_failure(self):
        if os.geteuid() == 0:
            self.skipTest("root читает любые файлы")
        bad = os.path.join(self.src, "secret.txt")
        open(bad, "w").write("x")
        os.chmod(bad, 0)
        r = run_backup(self.cfg())
        self.assertGreaterEqual(r["warnings"], 1)
        self.assertTrue(os.path.exists(r["path"]))


class TestCtl(Base):
    def test_set_option_preserves_comments_and_validates(self):
        with open(self.conf, "w") as f:
            f.write(f"# комментарий\n[backup]\nsource = {self.src}\ndestination = {self.dst}\n"
                    f"interval = 1h  # раз в час\n\n[daemon]\nnice = 5\n")
        os.chmod(self.conf, 0o600)
        backupctl.set_option(self.conf, "backup", "interval", "30m")
        backupctl.set_option(self.conf, "backup", "keep", "3")       # новый ключ
        backupctl.set_option(self.conf, "daemon", "log_level", "DEBUG")
        text = open(self.conf).read()
        self.assertIn("# комментарий", text)
        cfg = load_config(self.conf)
        self.assertEqual((cfg.interval, cfg.keep, cfg.log_level, cfg.nice), (1800, 3, "DEBUG", 5))
        before = open(self.conf).read()
        with self.assertRaises(ConfigError):                         # некорректное значение
            backupctl.set_option(self.conf, "backup", "interval", "never")
        self.assertEqual(open(self.conf).read(), before)             # файл не испорчен
        self.assertEqual(stat.S_IMODE(os.stat(self.conf).st_mode), 0o600)
        self.assertEqual([n for n in os.listdir(self.tmp) if n.startswith(".backupd-conf")], [])


class TestDaemonIntegration(Base):
    def test_lifecycle(self):
        pidfile = os.path.join(self.tmp, "run", "backupd.pid")
        statusfile = os.path.join(self.tmp, "state", "status.json")
        with open(self.conf, "w") as f:
            f.write(f"[backup]\nsource = {self.src}\ndestination = {self.dst}\ninterval = 1h\nkeep = 5\n"
                    f"[daemon]\npidfile = {pidfile}\nstatusfile = {statusfile}\nlog_level = DEBUG\n")
        os.chmod(self.conf, 0o600)

        def status():
            try:
                return json.load(open(statusfile))
            except (OSError, ValueError):
                return {}

        def wait_for(cond, timeout=20):
            end = time.time() + timeout
            while time.time() < end:
                if cond(status()):
                    return True
                time.sleep(0.2)
            return False

        proc = subprocess.Popen([sys.executable, os.path.join(HERE, "backupd.py"), "-f", "-c", self.conf],
                                stderr=subprocess.PIPE, text=True)
        try:
            # 1. первая копия по расписанию (через STARTUP_DELAY)
            self.assertTrue(wait_for(lambda s: s.get("total_ok", 0) >= 1), "нет первой копии")
            self.assertEqual(open(pidfile).read().strip(), str(proc.pid))

            # 2. второй экземпляр не запускается
            dup = subprocess.run([sys.executable, os.path.join(HERE, "backupd.py"), "-f", "-c", self.conf],
                                 capture_output=True, text=True, timeout=10)
            self.assertNotEqual(dup.returncode, 0)

            # 3. SIGUSR1 — немедленная копия
            time.sleep(1.1)
            proc.send_signal(signal.SIGUSR1)
            self.assertTrue(wait_for(lambda s: s.get("total_ok", 0) >= 2), "SIGUSR1 не сработал")

            # 4. мониторинг видит здоровый демон
            args = backupctl.argparse.Namespace(config=self.conf)
            self.assertEqual(backupctl.cmd_check(args), 0)

            # 5. SIGHUP с битым конфигом — прежние настройки сохраняются
            backupctl.set_option(self.conf, "backup", "interval", "2h")
            proc.send_signal(signal.SIGHUP)
            self.assertTrue(wait_for(lambda s: s.get("interval") == 7200), "SIGHUP не применил настройки")
            with open(self.conf, "a") as f:
                f.write("garbage line without equals\n")
            proc.send_signal(signal.SIGHUP)
            time.sleep(1)
            self.assertIsNone(proc.poll(), "демон упал от плохого конфига")
            self.assertEqual(status().get("interval"), 7200)
        finally:
            # 6. корректная остановка по SIGTERM
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                self.fail("демон не остановился по SIGTERM")
        err = proc.stderr.read() if proc.stderr else ""
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(status().get("state"), "stopped")
        self.assertFalse(os.path.exists(pidfile))
        self.assertEqual(len([n for n in os.listdir(self.dst) if n.startswith("backup_")]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
