#!/usr/bin/env python3
"""Build a native Android candidate using official Codex and upstream V8 sources."""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tomllib

ROOT = Path(__file__).resolve().parents[2]
TARGET = "aarch64-linux-android"
API = "29"
RUST = "1.95.0"
NDK = "28.2.13676358"
V8_VERSION = "150.4.0"
V8_COMMIT = "5c15a6995c9bb4bacd3e341b59fff32c909c80bf"
# These two git revisions are recorded in the matching upstream V8 DEPS.
ANDROID_DEPS = {
    "android_platform": (
        "https://chromium.googlesource.com/chromium/src/third_party/android_platform.git",
        "e3919359f2387399042d31401817db4a02d756ec",
    ),
    "catapult": (
        "https://chromium.googlesource.com/catapult.git",
        "2852bb7e91e4995502ffb72b7ed21412ee157914",
    ),
}


def run(args, *, cwd=ROOT, env=None):
    print("+", *map(str, args), flush=True)
    subprocess.run(list(map(str, args)), cwd=cwd, env=env, check=True)


def output(args, *, cwd=ROOT):
    return subprocess.check_output(list(map(str, args)), cwd=cwd, text=True).strip()


def checkout(path, remote, revision):
    if not (path / ".git").exists():
        path.mkdir(parents=True, exist_ok=True)
        run(["git", "init", path])
        run(["git", "remote", "add", "origin", remote], cwd=path)
        run(["git", "fetch", "--depth=1", "origin", revision], cwd=path)
        run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=path)
    actual = output(["git", "rev-parse", "HEAD"], cwd=path)
    if actual != revision:
        raise RuntimeError(f"Unexpected source revision at {path}: {actual}")


def link(path, destination):
    if path.is_symlink() and path.resolve() == destination.resolve():
        return
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"Refusing to replace existing path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(destination, target_is_directory=True)


def prepare(args):
    lock = tomllib.loads((ROOT / "codex-rs/Cargo.lock").read_text())
    versions = {p["version"] for p in lock["package"] if p["name"] == "v8"}
    if versions != {V8_VERSION}:
        raise RuntimeError(f"Review the V8 source pin for Cargo versions {versions}")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    # Catch missing Bionic interfaces before fetching and compiling V8.
    abi_probe = args.work_dir / "android-abi.c"
    abi_probe.write_text(
        "#include <pty.h>\n#include <sys/file.h>\n#include <netdb.h>\n"
        "int main(void) { int m, s; struct addrinfo *r; "
        "int e = openpty(&m, &s, 0, 0, 0); "
        "e |= flock(m, LOCK_EX | LOCK_NB); "
        'return e | getaddrinfo("localhost", "443", 0, &r); }\n'
    )
    run(
        [
            args.toolchain / "bin" / f"{TARGET}{API}-clang",
            "-Werror",
            abi_probe,
            "-o",
            args.work_dir / "android-abi",
        ]
    )
    checkout(args.source, "https://github.com/denoland/rusty_v8.git", V8_COMMIT)
    run(
        [
            "git",
            "submodule",
            "update",
            "--init",
            "--recursive",
            "--depth=1",
            "--jobs=4",
        ],
        cwd=args.source,
    )
    deps = (args.source / "v8/DEPS").read_text()
    for name, (remote, revision) in ANDROID_DEPS.items():
        if revision not in deps:
            raise RuntimeError(
                f"Android dependency pin absent from upstream DEPS: {name}"
            )
        checkout(args.source / "third_party" / name, remote, revision)
    link(args.source / "third_party/android_ndk", args.ndk)
    link(args.source / "third_party/android_toolchain/ndk", args.ndk)
    link(args.source / "third_party/android_sdk/public", args.sdk)
    gclient = args.source / "build/config/gclient_args.gni"
    content = gclient.read_text() if gclient.exists() else ""
    if "android_ndk_version" not in content:
        gclient.write_text(content + '\nandroid_ndk_version = "r28"\n')
    patch = ROOT / "patches/termux-rusty-v8-bindgen.patch"
    run(["git", "apply", "--check", patch], cwd=args.source)
    run(["git", "apply", patch], cwd=args.source)
    run(
        ["python3", "build/linux/sysroot_scripts/install-sysroot.py", "--arch=amd64"],
        cwd=args.source,
    )


def build_env(args):
    env = os.environ.copy()
    compiler = args.toolchain / "bin" / f"{TARGET}{API}-clang"
    env.update(
        {
            "PATH": str(args.toolchain / "bin") + os.pathsep + env["PATH"],
            "CARGO_NET_GIT_FETCH_WITH_CLI": "true",
            "CARGO_BUILD_JOBS": str(args.jobs),
            "CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER": str(compiler),
            "CC_aarch64_linux_android": str(compiler),
            "CXX_aarch64_linux_android": str(compiler) + "++",
            "AR_aarch64_linux_android": str(args.toolchain / "bin/llvm-ar"),
            "RANLIB_aarch64_linux_android": str(args.toolchain / "bin/llvm-ranlib"),
            "ANDROID_NDK_HOME": str(args.ndk),
            "ANDROID_NDK_ROOT": str(args.ndk),
            "LIBLZMA_NO_PKG_CONFIG": "1",
            "OPENSSL_NO_PKG_CONFIG": "1",
            "PKG_CONFIG_ALLOW_CROSS": "1",
        }
    )
    return env


def build_v8(args):
    env = build_env(args)
    resource = output(["clang-21", "-print-resource-dir"])
    sysroot = args.toolchain / "sysroot"
    # Use the driver matching bindgen's libclang and the same Android C headers.
    # Fail on unsupported libc++ builtins before spending hours compiling V8.
    probe = args.work_dir / "android-cxx.cc"
    probe.write_text(
        "#include <algorithm>\n#include <bit>\n#include <cstdint>\n"
        "#include <memory>\n#include <new>\nint main() { return 0; }\n"
    )
    run(
        [
            "clang-21",
            "-fsyntax-only",
            "-x",
            "c++",
            "-std=c++20",
            "-nostdinc++",
            f"--target={TARGET}{API}",
            f"--sysroot={sysroot}",
            "-D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_NONE",
            "-D_LIBCPP_HARDENING_MODE_DEFAULT=_LIBCPP_HARDENING_MODE_NONE",
            "-isystembuildtools/third_party/libc++",
            "-isystemthird_party/libc++/src/include",
            "-isystemthird_party/libc++abi/src/include",
            f"-isystem{sysroot}/usr/include",
            f"-isystem{sysroot}/usr/include/{TARGET}",
            f"-isystem{resource}/include",
            probe,
        ],
        cwd=args.source,
    )
    env.update(
        {
            "V8_FROM_SOURCE": "1",
            "CARGO_TARGET_DIR": str(args.work_dir / "v8-target"),
            "LIBCLANG_PATH": "/usr/lib/llvm-21/lib",
            "CODEX_V8_NDK_TOOLCHAIN": str(args.toolchain),
            "CODEX_V8_ANDROID_API": API,
            "CODEX_V8_CLANG_RESOURCE_DIR": resource,
            "GN_ARGS": f"android_ndk_api_level={API} symbol_level=0",
        }
    )
    # Do not pass target-header overrides into V8's separate host-tool builds.
    if any(name.startswith("BINDGEN_EXTRA_CLANG_ARGS") for name in env):
        raise RuntimeError("Remove global bindgen overrides before this source build")
    run(
        [
            "cargo",
            f"+{RUST}",
            "build",
            "--locked",
            "--release",
            "--target",
            TARGET,
            "--features",
            "v8_enable_sandbox",
        ],
        cwd=args.source,
        env=env,
    )
    gn = args.work_dir / "v8-target" / TARGET / "release/gn_out"
    for flag in ("v8_enable_sandbox", "v8_enable_pointer_compression"):
        if not re.search(rf"\b{flag}\s*=\s*true\b", (gn / "args.gn").read_text()):
            raise RuntimeError(f"V8 was built without required flag: {flag}")
    if (
        not (gn / "obj/librusty_v8.a").is_file()
        or not (gn / "src_binding.rs").is_file()
    ):
        raise RuntimeError("Matching V8 archive and binding outputs are missing")
    (gn / "v8-build.json").write_text(json.dumps(v8_fingerprint(gn), indent=2) + "\n")


def v8_fingerprint(gn):
    return {
        "v8_commit": V8_COMMIT,
        "target": TARGET,
        "api": API,
        "ndk": NDK,
        "rust": RUST,
        "recipe_sha256": digest(Path(__file__)),
        "patch_sha256": digest(ROOT / "patches/termux-rusty-v8-bindgen.patch"),
        "outputs": {
            name: digest(gn / name)
            for name in ("obj/librusty_v8.a", "src_binding.rs", "args.gn")
        },
    }


def verify_v8(args):
    gn = args.work_dir / "v8-target" / TARGET / "release/gn_out"
    if json.loads((gn / "v8-build.json").read_text()) != v8_fingerprint(gn):
        raise RuntimeError("V8 cache inputs or paired outputs differ from this recipe")
    for flag in ("v8_enable_sandbox", "v8_enable_pointer_compression"):
        if not re.search(rf"\b{flag}\s*=\s*true\b", (gn / "args.gn").read_text()):
            raise RuntimeError(f"V8 is missing required flag: {flag}")
    print("Verified matching V8 archive, binding, configuration and source recipe.")


def build_codex(args):
    verify_v8(args)
    gn = args.work_dir / "v8-target" / TARGET / "release/gn_out"
    env = build_env(args)
    env.update(
        {
            "CARGO_TARGET_DIR": str(args.work_dir / "codex-target"),
            "RUSTY_V8_ARCHIVE": str(gn / "obj/librusty_v8.a"),
            "RUSTY_V8_SRC_BINDING_PATH": str(gn / "src_binding.rs"),
            "CARGO_PROFILE_RELEASE_DEBUG": "0",
            "CARGO_PROFILE_RELEASE_STRIP": "symbols",
            "CARGO_PROFILE_RELEASE_LTO": "false",
            "CARGO_TARGET_AARCH64_LINUX_ANDROID_RUSTFLAGS": "-C link-arg=-Wl,-rpath,$ORIGIN/../lib -C link-arg=-Wl,-z,max-page-size=16384",
        }
    )
    run(
        [
            "cargo",
            f"+{RUST}",
            "build",
            "--locked",
            "--release",
            "--target",
            TARGET,
            "-p",
            "codex-cli",
            "-p",
            "codex-code-mode-host",
            "-p",
            "codex-http-client",
            "--bins",
            "--example",
            "termux_probe",
        ],
        cwd=ROOT / "codex-rs",
        env=env,
    )


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def package(args):
    verify_v8(args)
    stage = args.work_dir / "package/codex-termux-native"
    (stage / "bin").mkdir(parents=True, exist_ok=True)
    (stage / "lib").mkdir(exist_ok=True)
    release = args.work_dir / "codex-target" / TARGET / "release"
    readelf = args.toolchain / "bin/llvm-readelf"
    for name in ("codex", "codex-code-mode-host", "examples/termux_probe"):
        binary = release / name
        description = output(
            [readelf, "--file-header", "--program-headers", "--dynamic", binary]
        )
        if "AArch64" not in description or "/system/bin/linker64" not in description:
            raise RuntimeError(f"Not an Android ARM64 executable: {binary}")
        if "$ORIGIN/../lib" not in description:
            raise RuntimeError(f"Missing relative library search path: {binary}")
        needed = re.findall(r"\(NEEDED\).*?\[(.*?)\]", description)
        supported = {
            "libc.so",
            "libm.so",
            "libdl.so",
            "liblog.so",
            "libandroid.so",
            "libz.so",
            "libc++_shared.so",
        }
        if set(needed) - supported:
            raise RuntimeError(f"Unpackaged native dependency in {binary}: {needed}")
        shutil.copy2(binary, stage / "bin" / binary.name)
    shutil.copy2(
        args.toolchain / "sysroot/usr/lib/aarch64-linux-android/libc++_shared.so",
        stage / "lib/libc++_shared.so",
    )
    shutil.copy2(ROOT / "scripts/termux_smoke.py", stage / "smoke.py")
    licenses = stage / "licenses"
    licenses.mkdir(exist_ok=True)
    shutil.copy2(ROOT / "LICENSE", licenses / "codex.txt")
    shutil.copy2(args.source / "LICENSE", licenses / "rusty-v8.txt")
    shutil.copy2(args.source / "v8/LICENSE", licenses / "v8.txt")
    for name in ("NOTICE", "NOTICE.toolchain"):
        if (args.ndk / name).is_file():
            shutil.copy2(args.ndk / name, licenses / f"ndk-{name}.txt")
    gn = args.work_dir / "v8-target" / TARGET / "release/gn_out"
    provenance = {
        "codex_commit": output(["git", "rev-parse", "HEAD"]),
        "target": TARGET,
        "minimum_android_api": int(API),
        "ndk": NDK,
        "rustc": output(["rustc", f"+{RUST}", "--version"]),
        "v8_version": V8_VERSION,
        "v8_commit": V8_COMMIT,
        "v8_submodules": output(
            ["git", "submodule", "status", "--recursive"], cwd=args.source
        ),
        "android_dependencies": ANDROID_DEPS,
        "v8_gn_args": (gn / "args.gn").read_text(),
        "v8_binding_patch_sha256": digest(
            ROOT / "patches/termux-rusty-v8-bindgen.patch"
        ),
        "v8_archive_sha256": digest(gn / "obj/librusty_v8.a"),
        "v8_binding_sha256": digest(gn / "src_binding.rs"),
        "device_validated": False,
        "files": {
            str(p.relative_to(stage)): digest(p)
            for p in sorted(stage.rglob("*"))
            if p.is_file()
        },
    }
    (stage / "BUILD-INFO.json").write_text(json.dumps(provenance, indent=2) + "\n")
    archive = args.work_dir / "codex-termux-native-android-arm64.tar.gz"
    with (
        archive.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed,
    ):
        with tarfile.open(fileobj=compressed, mode="w") as tar:
            for path in sorted(stage.rglob("*")):
                info = tar.gettarinfo(path, arcname=str(path.relative_to(stage.parent)))
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ""
                if path.is_file():
                    with path.open("rb") as contents:
                        tar.addfile(info, contents)
                else:
                    tar.addfile(info)
    archive.with_suffix(archive.suffix + ".sha256").write_text(
        f"{digest(archive)}  {archive.name}\n"
    )
    print(f"Candidate: {archive}; Android runtime validation is still required.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage", choices=["prepare", "v8", "verify-v8", "codex", "package"]
    )
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--ndk", type=Path, required=True)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args()
    args.work_dir = args.work_dir.resolve()
    args.ndk = args.ndk.resolve()
    args.sdk = args.sdk.resolve()
    if args.jobs < 1 or NDK not in (args.ndk / "source.properties").read_text():
        parser.error(f"Use a positive job count and Android NDK {NDK}")
    args.source = args.work_dir / "rusty-v8"
    args.toolchain = args.ndk / "toolchains/llvm/prebuilt/linux-x86_64"
    {
        "prepare": prepare,
        "v8": build_v8,
        "verify-v8": verify_v8,
        "codex": build_codex,
        "package": package,
    }[args.stage](args)


if __name__ == "__main__":
    main()
