//! The native Termux distribution's published update channel.

use semver::Version;
use serde::Deserialize;

use crate::InstallContext;

pub const LATEST_RELEASE_URL: &str =
    "https://api.github.com/repos/shinokiri/codex-termux-native/releases/latest";
pub const INSTALL_COMMAND: &str = "curl -fsSL https://github.com/shinokiri/codex-termux-native/releases/latest/download/install.sh | CODEX_NON_INTERACTIVE=1 sh";
pub const VERSION_FILENAME: &str = "version-termux.json";

#[derive(Deserialize)]
struct PackageBuildInfo {
    release_version: Option<String>,
}

/// Read the local packaging revision without changing Codex's runtime version.
pub fn installed_version(context: &InstallContext, cli_version: &str) -> String {
    context
        .package_layout
        .as_ref()
        .and_then(|layout| std::fs::read_to_string(layout.package_dir.join("BUILD-INFO.json")).ok())
        .and_then(|contents| serde_json::from_str::<PackageBuildInfo>(&contents).ok())
        .and_then(|info| info.release_version)
        .unwrap_or_else(|| cli_version.to_owned())
}

/// Accept an update only after its installer, archive and checksums are published.
pub fn version_from_release(tag: &str, assets: &[String]) -> Option<String> {
    let version = tag.strip_prefix("termux-v")?;
    let parsed = Version::parse(version).ok()?;
    if !parsed.pre.is_empty()
        || parsed
            .build
            .as_str()
            .strip_prefix("termux.")?
            .parse::<u64>()
            .ok()?
            == 0
    {
        return None;
    }
    [
        "codex-package-aarch64-linux-android.tar.gz",
        "codex-package_SHA256SUMS",
        "install.sh",
    ]
    .iter()
    .all(|required| assets.iter().any(|asset| asset == required))
    .then(|| version.to_owned())
}

/// Compare both the official version and our numeric packaging revision.
pub fn is_newer(latest: &str, current: &str) -> Option<bool> {
    let latest = version_key(latest)?;
    let current = if current.starts_with("main.") || current.contains("+termux.g") {
        // Allow a development candidate to move to the published release channel.
        (0, 0, 0, 0)
    } else {
        version_key(current)?
    };
    Some(latest > current)
}

fn version_key(value: &str) -> Option<(u64, u64, u64, u64)> {
    let version = Version::parse(value.trim()).ok()?;
    if !version.pre.is_empty() {
        return None;
    }
    let revision = if version.build.is_empty() {
        0
    } else {
        version
            .build
            .as_str()
            .strip_prefix("termux.")?
            .parse()
            .ok()?
    };
    Some((version.major, version.minor, version.patch, revision))
}

#[cfg(test)]
#[path = "termux_tests.rs"]
mod tests;
