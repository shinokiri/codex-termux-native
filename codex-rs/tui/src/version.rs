/// The current Codex CLI version as embedded at compile time.
#[cfg(not(test))]
pub const CODEX_CLI_VERSION: &str = env!("CARGO_PKG_VERSION");

// Keep rendered fixtures independent of the release version, including padding.
// Production builds and CLI version checks still use the real package version.
#[cfg(test)]
pub const CODEX_CLI_VERSION: &str = "0.0.0";

pub(crate) fn display_version(version: &str) -> &str {
    version
        .split_once("+termux.")
        .map_or(version, |(upstream, _)| upstream)
}
