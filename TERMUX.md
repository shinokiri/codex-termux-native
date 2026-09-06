# Native Termux port

This branch starts from official `openai/codex` commit
`ac192cd7937b0d73edc6dffe009940ae53782dd4`. It implements Android adaptations
independently. No third-party Codex fork patches or release binaries are used.

## Status

This is a development branch, not an installable or device-validated release.

[Focused CI at `9360a962`](https://github.com/shinokiri/codex-termux-native/actions/runs/34027445362)
passed all 266 selected tests (plus one intentionally skipped subprocess
fixture), Android lock/PTY API compilation, targeted Clippy, formatting and the
Bazel lock check. The full Android source build is a separate gate.

| Area | Implementation | Validation still required |
| --- | --- | --- |
| File locks | Android `flock`, standard library elsewhere; 20 migrated call sites | Device filesystem tests; 5 Linux semantic tests and Android API compilation passed |
| OpenSSL | Android-only vendored build feature | Full cross compilation; this does not configure certificate roots |
| DNS | Target Android/Bionic so system resolution can follow Android networking | Native binary DNS and HTTPS tests with the user's TUN |
| TLS certificates | Both locked `openssl-probe` versions recognize Termux's CA bundle; nested TLS errors retain their classification | Device HTTPS and WSS roots; 10 existing CA integration tests passed |
| V8 / code mode | Exact `v8 = 150.4.0` source build workflow, Android binding-header patch | Successful source build, native sandbox and code-mode execution |
| PTY | Android provides `openpty` since API 23; no replacement added | NDK link probe and Rust Android API checks passed; device shell, resize and interrupt pending |
| Shell and local MCP tools | Android shell discovery uses validated `$SHELL`; stdio MCP children inherit Termux execution variables | Targeted shell tests passed; full Android build and subprocess execution pending |

The new file-lock crate preserves contention and real I/O errors. Unsupported
filesystems do not silently receive permission to enter critical sections.
Closing the last descriptor or terminating a process releases the kernel lock.
There is no directory-lock fallback or stale-lock cleanup protocol.

Linux tests exercise both the standard-library backend and the actual Android
`flock` implementation, including cross-process interoperability. A separate
Android target check catches conditional-compilation errors. Neither establishes
that every Android filesystem supports locking.

## Development checks

The `Termux native checks` workflow runs on relevant pushes to `termux/**`; build
recipe changes use the separate Android source-build workflow. It uses
read-only repository permissions, pinned action revisions, no account credentials
for Codex, and three-day retention for small correction patches. It does not
publish packages or releases.

`Termux formatting and build-script checks` independently runs the build-script
tests and the complete `just fmt` command on code changes, including build recipe
edits. It installs the formatter tools on its own runner and does not wait for
the Android source build. The worktree must remain unchanged after formatting.

The native-check workflow also runs `build.py check-cli` with the pinned Android
NDK and the candidate's release configuration. It checks the CLI and network
probe independently of V8: only the separate code-mode host links that library.
This catches Android compiler errors in the CLI's dependencies while V8 builds;
final linking, code-mode host execution and device networking remain separate checks.

```sh
just test --locked -p codex-utils-file-lock -p codex-http-client -p codex-shell-command
# Run in codex-rs:
cargo check --locked -p codex-utils-file-lock -p codex-utils-pty --tests --target aarch64-linux-android
# Run from the repository:
just fix -p codex-utils-file-lock -p codex-http-client -p codex-shell-command --locked --examples
just fmt
just bazel-lock-update
# Build-script regressions (Python 3.11+, no V8 compilation or NDK required):
python3 -m unittest discover -s scripts/termux -p 'test_*.py' -v
```

Build-script tests cover repeated preparation with an already-applied patch,
real patch conflicts, clean and reproducible repackaging, and inclusion of the
C++ shared library only when a packaged executable needs it. The Android build
workflow runs these checks before installing tools or compiling sources.

The HTTP tests include both successful custom-CA handshakes and rejection of
malformed or empty CA files. A real certificate-error test exposed a nested
`io::Error` classification bug, which this branch fixes without changing trust.
Five Native-TLS-to-Rustls fallback tests also failed on the unmodified official
baseline. Diagnostic execution showed custom-CA configuration was being
introduced between the outer shell and test process. The check workflow now
scrubs those CA variables at the test-process boundary; this does not affect
Codex's runtime environment or custom-CA support. All five fallback tests then
passed. Temporary diagnostic instrumentation was confined to the baseline
worktree and has been removed from the check workflow.

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
optional smoke script. The NDK C++ shared library is included only if an
executable's ELF dependencies require it. ELF checks reject Linux executables and unexpected
shared-library dependencies. Relative library lookup avoids a launcher that
rewrites process-wide proxy or dynamic-library environment variables. The
development build strips symbols and disables release LTO to limit build cost.
Only the two delivered binaries and the probe are selected for compilation;
unrelated CLI binaries such as `logs_client` are not built. V8 builds use Cargo's
verbose mode to expose Ninja progress. Preparation can be rerun with the same
patch, and repackaging replaces staged copies without deleting compiler outputs.

The build uploads the compressed candidate and checksum, retained for three
days. Its separate Actions cache keeps only this repository's compiled V8
archive, matching Rust binding, GN configuration and checksum manifest. Cache
keys match V8, Rust and NDK versions, target/API, GN settings, the binding patch
and a V8 build revision. Packaging-only edits do not invalidate this cache;
changes to V8 build logic outside those inputs must bump `V8_BUILD_REVISION`.
Required V8 flags are checked when V8 is built, and paired output hashes are
checked once before Codex consumes them. Saving this pair before compiling
Codex lets subsequent Rust fixes reuse it even if Codex compilation fails.
Source trees and other build intermediates are not uploaded. `BUILD-INFO.json`
records source revisions, V8 configuration, hashes and the unverified device
status. A successful compile does not establish working phone behavior.

The bundled `termux_probe` checks system DNS, HTTPS, and the TLS configuration
used for WebSocket clients without an explicit HTTP proxy. It uses no account
credentials. A full WebSocket upgrade, login, sessions, code mode and subprocess
execution still require separate device checks. Normal CA verification stays
enabled; the port does not hard-code a DNS service or disable TLS verification.

## Device smoke check

Once a candidate exists, the optional Python 3.10+ script runs its CLI and actual
code-mode host in a temporary directory, without an account or model calls:

```sh
python scripts/termux_smoke.py /path/to/codex-termux-native
# Additionally exercise Android DNS and verified TLS through the current TUN:
python scripts/termux_smoke.py /path/to/codex-termux-native --network
```

The script checks JavaScript execution, a Promise, a stored value across two
cells and clean host shutdown. The script itself still needs validation with
the candidate. Python is only a dependency of this optional check, not the CLI.
This does not test the interactive UI, MCP servers, account login or session
resume, and does not install the package or alter conversation archives.
