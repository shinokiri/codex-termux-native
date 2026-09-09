# Native Termux port

Development of this port started from official `openai/codex` commit
`ac192cd7937b0d73edc6dffe009940ae53782dd4`. It implements Android adaptations
independently. No third-party Codex fork patches or release binaries are used.

## Version identity and upstream updates

Runtime version fields match the exact official Cargo version. A build of
`rust-v0.153.4` reports `0.153.4` in the CLI, TUI, package manifest and client
build information. The existing `STABLE_GIT_COMMIT` stamp identifies the official
upstream commit. Development main keeps its upstream `0.0.0` placeholder.

The actual fork source commit, dirty state and packaging revision remain in
`BUILD-INFO.json`. Release tags and installation directories use an internal key
such as `0.153.4+termux.2` so an adaptation fix can update the same official
version. The updater reads that key from the local build metadata; it is not
used as the runtime version. Existing installations with the older version
suffix can upgrade normally.

`scripts/termux/upstream.json` records the integrated official commit and the
latest stable release reviewed. Update the commit only after integrating its
source; `latest_release_seen` is a review checkpoint, not a claim that the release
is incorporated. Check both official channels without compiling anything:

```sh
python3 scripts/termux/check_upstream.py
```

`Termux upstream status` remains a manual diagnostic. The `Termux release
updates` workflow implements the release pipeline:

1. Read the latest published, non-prerelease official release. An unchanged
   release/revision does not start another build.
2. Check out its exact official tag and apply only our Android delta, measured
   from the recorded upstream baseline. This avoids merging unreleased main
   features into a package bearing a stable release number. Publish the prepared
   source to `termux/releases/<version>` without rewriting any branch.
3. Run formatting, installer/update and selected CLI/compatibility regressions,
   plus the real Android build and ELF/package checks against that source commit.
   Reuse V8 when its inputs match. Cache hits fetch only the pinned license
   sources, skipping the remaining V8 compiler dependencies. A changed locked V8
   version still requires reviewing the source pin and binding patch.
4. Create a draft GitHub release, upload the package, checksums and installer,
   verify their digests, then publish it. Only a newer official version or
   packaging revision becomes latest; a late older build cannot move the channel
   backwards. The publisher re-reads latest after uploading the assets. A draft
   retry updates its source identity before uploading replacement assets. An
   existing tag must already match the validated source. Failures leave the
   previous published release available to clients.

The source branches keep the controller's reviewed workflow files unchanged;
the built-in CI token cannot write workflows. Their product source is the exact
official tag plus our Android delta, but their Git parent is the controller
commit so the push does not import upstream workflow history. Each job checks
out the immutable prepared source. `BUILD-INFO.json` records that source commit
and the official tag's commit; the Actions run's head identifies the controller.

GitHub does not send an upstream repository's `release` event to a fork.
The workflow therefore checks published releases every five minutes and also
accepts an `upstream-release` repository dispatch. The scheduled trigger runs
from `termux/native`, now the fork's default branch. GitHub may delay scheduled
runs. The workflow uses the repository's built-in GitHub token; no
OpenAI API key or separate hosting service is needed.

Each attempted official version/revision has a small `termux-build-*` tag so a
failed build does not repeat every five minutes. `Retry failed Termux releases`
responds to a failed or timed-out release run with at most two failed-job reruns,
with a one-minute pause before each retry. GitHub retains successful jobs and
their artifacts, so retrying publication does not compile Android again. A
retried prepare job can resume past its original attempt tag. Cancelled runs
are not retried; a third failed attempt stays failed for maintenance.
A failed source application retains conflict diagnostics. Fix the adaptation
and use the release workflow's `retry` input; an existing prepared source branch
is reused on retry. To publish a new Termux fix for the same official version,
increase the `revision` input. Published release assets are not replaced.
Pushing a change to the release controller also retries the current attempt;
unchanged scheduled checks do not. The release concurrency group queues pending
runs with GitHub's `queue: max`, so a scheduled check does not replace a pending
repair. Up to 100 runs may wait; GitHub rejects additional runs when full.
Source preparation aligns versions for explicit and implicit workspace members
and runs `just bazel-lock-update` before committing. Retries preserve source
fixes already on the prepared branch while refreshing these generated locks.
Unchanged release checks finish before installing build tools.

The release controller checks formatting before starting native and CLI regression
compilation. The CLI regression job disables test-profile debug assertions to compile the
release-only update paths, while leaving optimization off. Update-popup tests
run separately with the same build targets so an empty selection fails without
rebuilding a different feature selection. The CLI entry-point test launches a
standalone-layout binary with a local fake downloader, executes the returned
installer, and checks both success and failure without downloading an update.
The native update-command regression also covers empty and partial failed
downloads: neither executes the installer, and both retain curl's exit status.
The existing daemon tests run in the same job.

## Client update experience

The Android daemon updater uses the same native release channel as `codex update`
and resolves `sh` through Termux's `PATH`. Its installer is non-interactive.
This does not change PID detection or enable remote control automatically.

Android builds check this repository's completed releases, display the existing
update prompt, and run the native installer through `codex update`. The version
comparison includes the upstream version and the local packaging revision,
while visible version numbers use the upstream version. Update state
uses `version-termux.json`, so an old official-channel cache cannot announce a
package before its Termux build exists. This retains the upstream startup check
and cache behavior: the background check refreshes metadata for a later startup;
it is not a server push to an already-running session.

The shell installer is the official installer with Android target, release-source
and package-layout adaptations. It verifies downloads and switches the complete
CLI/code-mode-host package together. Failed downloads leave the current version
selected. Sessions and authentication files are not replaced. Ripgrep continues
to come from Termux rather than a bundled Linux binary.
`codex update` first downloads the entire installer into its shell process and
executes it only if curl succeeds. It preserves both download and installer
failure statuses without adding retries or temporary script files.

The first installation of an update-capable release uses:

```sh
curl -fsSL https://github.com/shinokiri/codex-termux-native/releases/latest/download/install.sh | sh
# Subsequent updates:
codex update
```

Older manually unpacked candidates need that initial installation once. The
installer prints any required PATH setup instructions.

To reclaim old installed packages, close all Codex processes and then use the
installer's explicit cleanup operation:

```sh
codex_installer=$(curl -fsSL https://github.com/shinokiri/codex-termux-native/releases/latest/download/install.sh) &&
  printf '%s\n' "$codex_installer" | sh -s -- --prune
```

`--prune` keeps only the package selected by `current`, with no rollback version.
It removes older package and legacy platform-npm installs, along with temporary
files left by interrupted installations. It validates `current` before cleanup
and leaves unrecognized directories alone. It uses the existing installer lock
and does not download a version, change PATH, or remove sessions, authentication
or configuration. Normal installation and
`codex update` retain older packages until this explicit operation, because an
already-running CLI may still need its matching code-mode host. Do not launch
another Codex process during cleanup.

## Status

The first published package, based on official `rust-v0.153.4`, passed the
[release workflow](https://github.com/shinokiri/codex-termux-native/actions/runs/34053785175):
Android CLI/code-mode host/probe linking, ELF and archive checks, selected
CLI/session/update regressions and native compatibility tests. The user has
reported successful installation and ordinary use on Termux. This is not a
record of completing every device smoke scenario.

Packaging revision 2 was published on 2026-09-07 from source
`628e04690892926ae683d00c279df3b91f3864af`, with runtime version `0.153.4`.
Its [build and regression gates](https://github.com/shinokiri/codex-termux-native/actions/runs/34119609682)
all passed. A missing-tag API lookup initially blocked publication; the
[publication recovery](https://github.com/shinokiri/codex-termux-native/actions/runs/34128172122)
used the same verified artifact after testing the controller fix, without
rebuilding Android or V8. All three published asset digests matched the artifact.

| Area | Implementation | Validation still required |
| --- | --- | --- |
| File locks | Android `flock`, standard library elsewhere; 20 migrated call sites | Device filesystem tests; 5 Linux semantic tests and Android API compilation passed |
| OpenSSL | Android-only vendored build feature; Android CLI and network-probe linking passed | Device handshakes; this does not configure certificate roots |
| DNS | Target Android/Bionic so system resolution can follow Android networking | Native binary DNS and HTTPS tests with the user's TUN |
| TLS certificates | Both locked `openssl-probe` versions recognize Termux's CA bundle; nested TLS errors retain their classification | Device HTTPS and WSS roots; 10 existing CA integration tests passed |
| V8 / code mode | Exact `v8 = 150.4.0` Android source compilation, paired cache verification and code-mode host linking passed; Android binding-header patch; NDK compiler builtins selected for the final link | V8 sandbox and code-mode execution on device |
| PTY | Android provides `openpty` since API 23; no replacement added | NDK link probe and Rust Android API checks passed; device shell, resize and interrupt pending |
| Shell and local MCP tools | Android shell discovery uses validated `$SHELL`; snapshot v2 resolves `env` through `PATH`; stdio MCP children inherit Termux execution variables | Targeted shell tests and Android build passed; broader device subprocess checks remain |
| Credential storage | Reject keyring's entry-local mock save so automatic mode uses the existing file fallback | Real mock-backend regression and existing MCP fallback checks added; device login pending |
| Process sandbox | Upstream has no Android process-sandbox backend | Permission and approval behavior on device; executor requests that require a sandbox are unsupported |

The experimental `shell_snapshot_v2` feature is disabled by default in this
upstream revision. Its environment capture now finds `env` through `PATH`,
supporting Termux's prefix while bypassing same-named shell functions. A shell
regression covers a prefix containing spaces and NUL-delimited multiline values.

Codex's OS process sandbox is separate from V8's internal sandbox. The upstream
platform selector returns no process-sandbox backend on Android. Local direct
launches can consequently use `SandboxType::None`; a workspace or read-only
profile alone does not establish OS enforcement. The exec-server path instead
rejects explicit sandbox requests with `sandbox intent cannot be enforced on
this executor`. This affects sandboxed snapshot-v2 launches when that feature
is enabled. This branch retains upstream permission defaults and does not add
an Android process-sandbox backend.

The new file-lock crate preserves contention and real I/O errors. Unsupported
filesystems do not silently receive permission to enter critical sections.
Closing the last descriptor or terminating a process releases the kernel lock.
There is no directory-lock fallback or stale-lock cleanup protocol.

Linux tests exercise both the standard-library backend and the actual Android
`flock` implementation, including cross-process interoperability. A separate
Android target check catches conditional-compilation errors. Neither establishes
that every Android filesystem supports locking.

## Responses WebSocket recovery

This fork keeps the provider's fast stream retry budget. With the existing
`unbounded_connection_retries` feature enabled (the default), an interactive
sampling request that still encounters WebSocket stream failures or connection
timeouts waits and retries WebSockets at 5, 10, 20, 40 and then at most 60 second
intervals. This extends the existing wait-for-network behavior; disabling that
feature restores bounded retries. Terminal API errors retain their existing
handling, and internal sessions and remote compaction retain their bounded policy.

When HTTP fallback does activate, including an HTTP 426 handshake response, it
lasts for a 30 second cooldown. The next streaming request then tries WebSockets
again with fresh connection and incremental-response state, retaining the full
conversation and the turn's routing token. Active HTTP responses finish normally;
there is no background probe or automatic request while the conversation is idle.
A failed recovery can start a new cooldown. Logs record both fallback and the
subsequent WebSocket recovery attempt. This is a preference, not a guarantee that
HTTPS is never used.

The regression tests cover repeated disconnects beyond the fast retry budget,
both fallback entry paths, cooldown across turns, successful WebSocket recovery,
and preservation of replies received over HTTP. They use local mock endpoints
and no model credentials.

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

The Android source-build workflow builds and links the CLI, code-mode host
and network probe once. The native-check workflow keeps the focused behavior
tests and Android API checks, without duplicating that CLI build. Formatting
must pass before the release controller starts its Android build.

CLI, exec and TUI changes also run selected exec, session-resume and update
regressions on Linux through `just test`.

```sh
just test --locked -p codex-utils-file-lock -p codex-http-client -p codex-shell-command -p codex-keyring-store
# Run in codex-rs:
cargo check --locked -p codex-utils-file-lock -p codex-utils-pty -p codex-keyring-store --tests --target aarch64-linux-android
# Run from the repository:
just fix -p codex-utils-file-lock -p codex-http-client -p codex-shell-command -p codex-keyring-store --locked --examples
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

The locked `keyring` 3.6.3 defaults to an in-memory mock on Android. A save
reports success but the next entry cannot retrieve it, suppressing the existing
file fallback in automatic credential-storage mode. The port rejects saves to
that actual mock backend; real keyring backends keep their existing behavior.
Reads and deletion still report a missing entry so fallback files remain
readable and removable. This is credential persistence, not a new storage format
or an additional encryption layer. Explicit keyring-only mode reports that a
persistent keyring backend is unavailable.

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
optional smoke script. Rust's Android link command uses `-nodefaultlibs`, while
V8's ARM64 instruction-cache flush calls compiler-rt's `__clear_cache`. The
build therefore asks the target NDK Clang driver for its compiler builtins
archive, verifies that symbol is present, and links the archive statically. This
uses the pinned NDK runtime and adds no device shared-library dependency.
The NDK C++ shared library is included only if an
executable's ELF dependencies require it. ELF checks reject Linux executables and unexpected
shared-library dependencies. Relative library lookup avoids a launcher that
rewrites process-wide proxy or dynamic-library environment variables. The
build strips symbols and retains upstream's cross-crate ThinLTO release setting.
Packaging logs the size of each executable so release size and build time can be
compared with the previous locally optimized build. Device startup time and
memory usage still require measurements on Android.
Only the two delivered binaries and the probe are selected for compilation;
unrelated CLI binaries such as `logs_client` are not built. V8 builds use Cargo's
verbose mode to expose Ninja progress, including completed and running tasks
and elapsed seconds. The public Linux runner uses four compile jobs for V8 and
Codex and prints its CPU, memory and free disk resources before the build.
The upgraded workflow has its own concurrency group so it can run alongside
the initial bootstrap; later pushes to this branch still share one queue.
Changing job counts or progress logging does not invalidate the V8 cache because
the compiler configuration is unchanged. Preparation can be rerun with the same
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
