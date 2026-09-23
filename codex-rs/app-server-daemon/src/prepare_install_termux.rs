//! Keep the Termux packaging revision when seeding a daemon from its CLI package.

use std::path::Path;

use anyhow::Context;
use anyhow::Result;
use serde::Deserialize;

#[derive(Deserialize)]
struct BuildInfo {
    release_version: Option<String>,
}

pub(super) fn release_version(source: &Path, cli_version: &str) -> Result<Option<String>> {
    let contents = match std::fs::read(source.join("BUILD-INFO.json")) {
        Ok(contents) => contents,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error).context("failed to read Termux package provenance"),
    };
    let info: BuildInfo =
        serde_json::from_slice(&contents).context("invalid Termux package provenance")?;
    let Some(version) = info.release_version else {
        return Ok(None);
    };
    let prefix = format!("{cli_version}+termux.");
    let revision = version.strip_prefix(&prefix);
    anyhow::ensure!(
        super::stable_version(cli_version).is_some()
            && revision.is_some_and(|revision| {
                revision.bytes().all(|byte| byte.is_ascii_digit())
                    && revision.parse::<u64>().is_ok_and(|revision| revision > 0)
            }),
        "Termux package revision does not match its CLI version"
    );
    Ok(Some(version))
}

#[cfg(test)]
#[path = "prepare_install_termux_tests.rs"]
mod tests;
