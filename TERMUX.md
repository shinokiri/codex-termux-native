# Native Termux port

This branch starts from official `openai/codex` commit
`ac192cd7937b0d73edc6dffe009940ae53782dd4`. It implements Android adaptations
independently. No third-party Codex fork patches or release binaries are used.

## Status

This is a development branch, not an installable or device-validated release.

| Area | Implementation | Validation still required |
| --- | --- | --- |
| File locks | Android `flock`, standard library elsewhere; 20 migrated call sites | Rust tests, Android compilation and device filesystem tests |
| OpenSSL | Android-only vendored build feature | Full cross compilation; this does not configure certificate roots |
| DNS | Target Android/Bionic so system resolution can follow Android networking | Native binary DNS and HTTPS tests with the user's TUN |
| TLS certificates | Official CA policy retained pending dependency audit | HTTPS and WSS default roots, custom roots, invalid-root rejection |
| V8 / code mode | Official dependency remains exactly `v8 = 150.4.0` | Build matching upstream sources for Android with sandbox enabled |
| PTY and child tools | Official implementation retained pending ABI checks | Interactive shell, resize, interrupt, MCP subprocess environment |

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
