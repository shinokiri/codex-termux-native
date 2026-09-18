"""Exercise pruning with real processes holding old release resources."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.install.test_install_sh import run_installer_in, write_executable
from scripts.termux.test_installer import release_fixture


@unittest.skipUnless(
    Path("/proc/self/exe").exists() and shutil.which("cc"),
    "Requires Linux/Android procfs and a native C compiler",
)
class PruneLiveProcessesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="codex-prune-native-")
        cls.addClassCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        source = directory / "fixture.c"
        cls.fixture = directory / "fixture"
        source.write_text(r"""
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>
int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--version") == 0) {
        puts("codex-cli 0.153.4");
        return 0;
    }
    const char *command = getenv("TERMUX_TEST_INSTALL_COMMAND");
    if (command) {
        int result = system(command);
        return result >= 0 && WIFEXITED(result) ? WEXITSTATUS(result) : 99;
    }
    if (argc == 3 && strcmp(argv[1], "--map") == 0) {
        int fd = open(argv[2], O_RDONLY);
        if (fd < 0 || mmap(NULL, 4096, PROT_READ, MAP_PRIVATE, fd, 0) == MAP_FAILED)
            return 98;
        close(fd);
    }
    puts("READY");
    fflush(stdout);
    for (;;) pause();
}
""")
        subprocess.run(["cc", str(source), "-o", str(cls.fixture)], check=True)

    def install(self, root, revision, **options):
        result, requests = run_installer_in(
            root,
            "latest",
            platform="android",
            **release_fixture(root, revision),
            **options,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return (root / "codex-home/packages/standalone/current").resolve(), result

    def prune(self, root):
        result, _ = run_installer_in(
            root, "latest", platform="android", installer_args=("--prune",)
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def holder(self, program, *args, cwd=None):
        process = subprocess.Popen(
            [str(program), *map(str, args)], cwd=cwd, stdout=subprocess.PIPE, text=True
        )

        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            process.stdout.close()

        self.addCleanup(cleanup)
        self.assertEqual(process.stdout.readline(), "READY\n")
        return process

    def test_prune_keeps_live_daemon_and_host_and_removes_after_exit(self):
        for binary in ("codex", "codex-code-mode-host"):
            with (
                self.subTest(binary=binary),
                tempfile.TemporaryDirectory(prefix="termux live prune ") as temporary,
            ):
                root = Path(temporary)
                old, _ = self.install(root, 1)
                unused, _ = self.install(root, 2)
                current, _ = self.install(root, 3)
                program = old / "bin" / binary
                shutil.copy2(self.fixture, program)
                process = self.holder(program, "app-server")
                result = self.prune(root)
                self.assertIn(f"in use by PID {process.pid}", result.stdout)
                self.assertTrue((old / "bin/codex-code-mode-host").is_file())
                self.assertFalse(unused.exists())
                self.assertIsNone(process.poll())
                process.terminate()
                process.wait(timeout=5)
                self.prune(root)
                self.assertEqual(list(current.parent.iterdir()), [current])

    def test_update_and_prune_keeps_live_daemon(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old, _ = self.install(root, 1)
            shutil.copy2(self.fixture, old / "bin/codex")
            process = self.holder(old / "bin/codex", "app-server")
            current, result = self.install(
                root, 2, installer_args=("--prune-after-install",)
            )
            self.assertIn(f"in use by PID {process.pid}", result.stdout)
            self.assertTrue((old / "bin/codex-code-mode-host").is_file())
            self.assertNotEqual(current, old)

    def test_only_the_actual_updater_ancestor_is_exempt(self):
        scenarios = (
            (("update", "--prune"), False),
            (("-c", "label=update", "update", "--prune"), False),
            (("agents",), True),
            (("exec", "update"), True),
        )
        original_run = subprocess.run
        for arguments, keep in scenarios:
            with (
                self.subTest(arguments=arguments),
                tempfile.TemporaryDirectory(prefix="termux updater ") as temporary,
            ):
                root = Path(temporary)
                old, _ = self.install(root, 1)
                program = old / "bin/codex"
                shutil.copy2(self.fixture, program)

                def through_ancestor(command, **options):
                    env = dict(options["env"])
                    env["TERMUX_TEST_INSTALL_COMMAND"] = shlex.join(command)
                    return original_run(
                        [str(program), *arguments], **{**options, "env": env}
                    )

                with patch(
                    "scripts.install.test_install_sh.subprocess.run",
                    side_effect=through_ancestor,
                ):
                    current, _ = self.install(
                        root, 2, installer_args=("--prune-after-install",)
                    )
                self.assertEqual(old.exists(), keep)
                self.assertTrue((current / "bin/codex-code-mode-host").is_file())

    def test_unrelated_update_process_is_kept(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old, _ = self.install(root, 1)
            self.install(root, 2)
            shutil.copy2(self.fixture, old / "bin/codex")
            process = self.holder(old / "bin/codex", "update", "--prune")
            result = self.prune(root)
            self.assertIn(f"in use by PID {process.pid}", result.stdout)
            self.assertTrue(old.exists())

    def test_working_directory_and_mapped_resource_keep_package(self):
        for resource in ("cwd", "mapping"):
            with (
                self.subTest(resource=resource),
                tempfile.TemporaryDirectory(prefix="termux resource ") as temporary,
            ):
                root = Path(temporary)
                old, _ = self.install(root, 1)
                self.install(root, 2)
                if resource == "cwd":
                    process = self.holder(self.fixture, cwd=old)
                else:
                    mapped = old / "lib/mapped resource"
                    mapped.write_bytes(b"x" * 4096)
                    process = self.holder(self.fixture, "--map", mapped)
                result = self.prune(root)
                self.assertIn(f"in use by PID {process.pid}", result.stdout)
                self.assertTrue(old.exists())

    def test_uninspectable_processes_keep_packages(self):
        for failure in ("ps", "exe", "cwd", "maps"):
            with (
                self.subTest(failure=failure),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                old, _ = self.install(root, 1)
                self.install(root, 2)
                if failure == "ps":
                    write_executable(root / "bin/ps", "#!/bin/sh\nexit 1\n")
                elif failure in ("exe", "cwd"):
                    command = shlex.quote(shutil.which("readlink"))
                    write_executable(
                        root / "bin/readlink",
                        f'#!/bin/sh\ncase "$1" in /proc/{os.getpid()}/{failure}) exit 1;; esac\n'
                        f'exec {command} "$@"\n',
                    )
                else:
                    command = shlex.quote(shutil.which("grep"))
                    write_executable(
                        root / "bin/grep",
                        f'#!/bin/sh\ncase "$*" in *"/proc/{os.getpid()}/maps") exit 2;; esac\n'
                        f'exec {command} "$@"\n',
                    )
                result = self.prune(root)
                self.assertTrue(old.exists())
                self.assertIn("Could not inspect all running processes", result.stderr)


if __name__ == "__main__":
    unittest.main()
