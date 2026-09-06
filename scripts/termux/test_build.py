"""Exercise retries and package contents without compiling V8 or using the NDK."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.termux import build


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.args = SimpleNamespace(
            work_dir=root / "work",
            source=root / "work/rusty-v8",
            toolchain=root / "ndk/toolchain",
            ndk=root / "ndk",
            sdk=root / "sdk",
        )
        source = self.args.source
        (source / "v8").mkdir(parents=True)
        (source / "build/config").mkdir(parents=True)
        (source / "v8/DEPS").write_text("")
        (source / "build.rs").write_text("original binding headers\n")
        subprocess.run(["git", "init", "-q", source], check=True)
        (root / "codex-rs").mkdir()
        (root / "codex-rs/Cargo.lock").write_text(
            f'[[package]]\nname = "v8"\nversion = "{build.V8_VERSION}"\n'
        )
        (root / "patches").mkdir()
        (root / "patches/termux-rusty-v8-bindgen.patch").write_text(
            "diff --git a/build.rs b/build.rs\n"
            "--- a/build.rs\n+++ b/build.rs\n@@ -1 +1 @@\n"
            "-original binding headers\n+Android binding headers\n"
        )
        # Use real git apply operations; skip compiler, network and SDK work.
        run = build.run

        def prepare_command(args, **kwargs):
            if list(args[:2]) == ["git", "apply"]:
                run(args, **kwargs)

        for replacement in (
            patch.object(build, "ROOT", root),
            patch.object(build, "ANDROID_DEPS", {}),
            patch.object(build, "checkout"),
            patch.object(build, "run", side_effect=prepare_command),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.enterContext(replacement)

    def test_prepare_can_resume_with_the_same_patch_already_applied(self):
        build.prepare(self.args)
        build.prepare(self.args)
        self.assertEqual(
            (self.args.source / "build.rs").read_text(), "Android binding headers\n"
        )

    def test_prepare_reports_a_conflict_without_overwriting_source(self):
        source = self.args.source / "build.rs"
        source.write_text("different local headers\n")
        with self.assertRaises(subprocess.CalledProcessError):
            build.prepare(self.args)
        self.assertEqual(source.read_text(), "different local headers\n")


class CompilerBuiltinsTest(unittest.TestCase):
    def test_codex_env_links_ndk_compiler_builtins(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            toolchain = root / "toolchain"
            archive = toolchain / "lib/libclang_rt.builtins-aarch64-android.a"
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b"compiler builtins")
            args = SimpleNamespace(
                work_dir=root / "work",
                toolchain=toolchain,
                ndk=root / "ndk",
                jobs=4,
            )

            def tool_output(command, **_kwargs):
                if command[-1] == "--print-libgcc-file-name":
                    return str(archive)
                if Path(command[0]).name == "llvm-nm":
                    return "0000000000000000 T __clear_cache"
                raise AssertionError(f"Unexpected tool command: {command}")

            with (
                patch.object(build, "output", side_effect=tool_output),
                patch.object(
                    build,
                    "build_identity",
                    return_value={
                        "codex_commit": "codex-source-revision",
                        "source_dirty": False,
                        "cli_version": "main.upstream+termux.revision",
                    },
                ),
            ):
                env = build.codex_env(args)

            self.assertEqual(
                env["CARGO_TARGET_AARCH64_LINUX_ANDROID_RUSTFLAGS"].split()[-2:],
                ["-C", f"link-arg={archive}"],
            )


class PackageTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.args = SimpleNamespace(
            work_dir=root / "work",
            source=root / "work/rusty-v8",
            toolchain=root / "ndk/toolchain",
            ndk=root / "ndk",
        )
        self.needs_libcxx = True
        for relative, content in {
            "work/codex-build.json": json.dumps(
                {
                    "codex_commit": "compiled-source-revision",
                    "source_dirty": False,
                    "package_version": "0.153.4+termux.g0123456789ab",
                    "cli_version": "0.153.4+termux.g0123456789ab",
                }
            ).encode(),
            "LICENSE": b"Codex license",
            "scripts/termux_smoke.py": b"smoke check",
            "scripts/install/install.sh": b"#!/bin/sh\nexit 0\n",
            "patches/termux-rusty-v8-bindgen.patch": b"binding patch",
            "work/rusty-v8/LICENSE": b"rusty-v8 license",
            "work/rusty-v8/v8/LICENSE": b"V8 license",
            "ndk/NOTICE": b"NDK notice",
            "ndk/toolchain/sysroot/usr/lib/aarch64-linux-android/libc++_shared.so": b"C++ runtime",
        }.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        release = self.args.work_dir / "codex-target" / build.TARGET / "release"
        for name in ("codex", "codex-code-mode-host", "examples/termux_probe"):
            binary = release / name
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_bytes(name.encode())
            binary.chmod(0o755)
        gn = self.args.work_dir / "v8-target" / build.TARGET / "release/gn_out"
        (gn / "obj").mkdir(parents=True)
        for name in ("args.gn", "src_binding.rs", "obj/librusty_v8.a"):
            (gn / name).write_text(name)
        self.enterContext(patch.object(build, "ROOT", root))
        (gn / "v8-build.json").write_text(json.dumps(build.v8_fingerprint(gn)))
        self.enterContext(patch.object(build, "output", side_effect=self.tool_output))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.archive = self.args.work_dir / "codex-termux-native-android-arm64.tar.gz"
        self.stage = self.args.work_dir / "package/codex-termux-native"

    def tool_output(self, args, **kwargs):
        if Path(args[0]).name == "llvm-readelf":
            # Packaging tests use synthetic metadata, not device-validation claims.
            description = "AArch64 /system/bin/linker64 $ORIGIN/../lib\n"
            if self.needs_libcxx and Path(args[-1]).name == "codex-code-mode-host":
                description += "(NEEDED) Shared library: [libc++_shared.so]\n"
            return description + "(NEEDED) Shared library: [libc.so]\n"
        if args[:2] == ["git", "rev-parse"]:
            return "codex-source-revision"
        if args[:2] == ["git", "submodule"]:
            return "v8-submodule-revision"
        if args[0] == "rustc":
            return "rustc test toolchain"
        raise AssertionError(f"Unexpected tool command: {args}")

    def test_repackaging_removes_stale_files_and_reproduces_the_same_archive(self):
        build.package(self.args)
        original = self.archive.read_bytes()
        (self.stage / "bin/obsolete-tool").write_bytes(b"old build")
        build.package(self.args)
        with tarfile.open(self.archive) as archive:
            self.assertNotIn(
                "codex-termux-native/bin/obsolete-tool", archive.getnames()
            )
        self.assertEqual(self.archive.read_bytes(), original)
        manifest = json.loads((self.stage / "BUILD-INFO.json").read_text())
        files = {
            str(path.relative_to(self.stage)): build.digest(path)
            for path in self.stage.rglob("*")
            if path.is_file() and path.name != "BUILD-INFO.json"
        }
        self.assertEqual(manifest["files"], files)
        # Repackaging must describe the compiled input, not the current checkout.
        self.assertEqual(manifest["codex_commit"], "compiled-source-revision")
        package_manifest = json.loads((self.stage / "codex-package.json").read_text())
        self.assertEqual(package_manifest["version"], manifest["package_version"])

    def test_bundle_libcxx_only_when_an_executable_needs_it(self):
        for needed in (True, False):
            with self.subTest(needed=needed):
                self.needs_libcxx = needed
                build.package(self.args)
                with tarfile.open(self.archive) as archive:
                    self.assertEqual(
                        "codex-termux-native/lib/libc++_shared.so"
                        in archive.getnames(),
                        needed,
                    )

    def test_release_archive_matches_the_standalone_installer_layout(self):
        identity_path = self.args.work_dir / "codex-build.json"
        identity = json.loads(identity_path.read_text())
        for field in ("package_version", "cli_version", "release_version"):
            identity[field] = "0.153.4+termux.1"
        identity_path.write_text(json.dumps(identity))
        build.package(self.args)
        archive_path = self.args.work_dir / f"codex-package-{build.TARGET}.tar.gz"
        with tarfile.open(archive_path) as archive:
            self.assertIn("bin/codex", archive.getnames())
            self.assertIn("bin/codex-code-mode-host", archive.getnames())
            self.assertIn("codex-package.json", archive.getnames())
            self.assertNotIn("codex-termux-native/bin/codex", archive.getnames())
        self.assertEqual(
            (self.args.work_dir / "codex-package_SHA256SUMS").read_text(),
            f"{build.digest(archive_path)}  {archive_path.name}\n",
        )
        self.assertEqual(
            (self.args.work_dir / "install.sh").read_bytes(), b"#!/bin/sh\nexit 0\n"
        )


if __name__ == "__main__":
    unittest.main()
