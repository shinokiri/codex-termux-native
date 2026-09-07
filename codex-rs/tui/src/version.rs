/// The current Codex CLI version as embedded at compile time.
pub const CODEX_CLI_VERSION: &str = env!("CARGO_PKG_VERSION");

pub(crate) fn display_version(version: &str) -> &str {
    version
        .split_once("+termux.")
        .map_or(version, |(upstream, _)| upstream)
}
