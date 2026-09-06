# Native Termux port

This branch starts from official `openai/codex` commit
`ac192cd7937b0d73edc6dffe009940ae53782dd4`. It implements Android adaptations
independently. No third-party Codex fork patches or release binaries are used.

## Status

This is a development branch, not an installable or device-validated release.

| Area | Implementation | Validation still required |
| --- | --- | --- |
| File locks | Android `flock`, standard library elsewhere; 20 migrated call sites | Device filesystem tests; 5 Linux semantic tests and Android API compilation passed |
| OpenSSL | Android-only vendored build feature | Full cross compilation; this does not configure certificate roots |
| DNS | Target Android/Bionic so system resolution can follow Android networking | Native binary DNS and HTTPS tests with the user's TUN |
| TLS certificates | Both locked `openssl-probe` versions recognize Termux's CA bundle | Device HTTPS and WSS roots; custom-root rejection tests in CI |
| V8 / code mode | Exact `v8 = 150.4.0` source build workflow, Android binding-header patch | Successful source build, native sandbox and code-mode execution |
| PTY and child tools | Android provides `openpty` since API 23; no replacement added | Link verification, interactive shell, resize, interrupt, MCP subprocess environment |

The new file-lock crate preserves contention and real I/O errors. Unsupported
filesystems do not silently receive permission to enter critical sections.
Closing the last descriptor or terminating a process releases the kernel lock.
There is no directory-lock fallback or stale-lock cleanup protocol.

Linux tests exercise both the standard-library backend and the actual Android
`flock` implementation, including cross-process interoperability. A separate
Android target check catches conditional-compilation errors. Neither establishes
that every Android filesystem supports locking.

## Development checks

The `Termux native checks` workflow runs on pushes to `termux/**`. It uses
read-only repository permissions, pinned action revisions, no account credentials
for Codex, and three-day retention for small correction patches. It does not
publish packages or releases.

```sh
just test --locked -p codex-utils-file-lock
# Run in codex-rs:
cargo check --locked -p codex-utils-file-lock --tests --target aarch64-linux-android
# Run from the repository:
just fix -p codex-utils-file-lock --locked
just fmt
just bazel-lock-update
```

## Reference audit

`DioNanos/codex-termux` is a source of compatibility questions, not our code base.
Its documentation identifies browser login, certificate discovery, PTYs,
dynamic linking, subprocess environments and V8 as areas to investigate.
Its README and patch inventory disagree on whether a TLS-root adaptation is
still present; source and runtime evidence take precedence over those claims.

Model-instruction fallbacks, custom update servers, ignored locking failures and
stubbed operations are not prerequisites for an Android port. Each adaptation
must have an identified upstream limitation and retain real error reporting.

## Android candidate build

`Termux Android source build` uses Ubuntu 26.04, Rust 1.95.0, NDK
28.2.13676358 and Android API 29 (Android 10 or newer, ARM64). The SDK inputs
match the pinned Chromium build files: platform 37.0 and build-tools 37.0.0.
This SDK choice does not raise the runtime API floor to 37.

V8 starts at upstream `denoland/rusty_v8` commit
`5c15a6995c9bb4bacd3e341b59fff32c909c80bf`; git submodules and the additional
Android repositories use exact commits. Our small patch gives V8's final
bindgen invocation Android target headers. It does not redirect the x64 host
tools to Android headers. Pointer compression and the V8 sandbox remain enabled.

The candidate contains `codex`, `codex-code-mode-host`, a network probe and the
NDK C++ shared library. ELF checks reject Linux executables and unexpected
shared-library dependencies. Relative library lookup avoids a launcher that
rewrites process-wide proxy or dynamic-library environment variables. The
development build strips symbols and disables release LTO to limit build cost.

The build uploads only the compressed candidate and checksum, retained for
three days. Sources and intermediate builds are not uploaded. `BUILD-INFO.json`
records source revisions, V8 configuration, hashes and the unverified device
status. A successful compile does not establish working phone behavior.

The bundled `termux_probe` checks system DNS, HTTPS, and the TLS configuration
used for WebSocket clients without an explicit HTTP proxy. It uses no account
credentials. A full WebSocket upgrade, login, sessions, code mode and subprocess
execution still require separate device checks. Normal CA verification stays
enabled; the port does not hard-code a DNS service or disable TLS verification.
