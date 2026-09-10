use anyhow::Result;
use predicates::str::contains;
#[cfg(debug_assertions)]
use std::path::Path;
use tempfile::TempDir;

#[cfg(debug_assertions)]
fn codex_command(codex_home: &Path) -> Result<assert_cmd::Command> {
    let mut cmd = assert_cmd::Command::new(codex_utils_cargo_bin::cargo_bin("codex")?);
    cmd.env("CODEX_HOME", codex_home);
    Ok(cmd)
}

#[cfg(debug_assertions)]
#[tokio::test]
async fn update_does_not_start_interactive_prompt() -> Result<()> {
    let codex_home = TempDir::new()?;

    codex_command(codex_home.path())?
        .arg("update")
        .assert()
        .failure()
        .stderr(contains("`codex update` is not available in debug builds"));

    Ok(())
}

#[cfg(all(unix, not(debug_assertions)))]
#[test]
fn update_runs_standalone_installer_and_reports_its_exit_status() -> Result<()> {
    use pretty_assertions::assert_eq;
    use std::os::unix::fs::PermissionsExt;

    let root = TempDir::new()?;
    let codex_home = root.path().join("codex-home");
    let release = codex_home.join("packages/standalone/releases/update-test");
    std::fs::create_dir_all(&release)?;
    let executable = release.join("codex");
    let built = codex_utils_cargo_bin::cargo_bin("codex")?;
    // current_exe must resolve inside the standalone installation, not through
    // a symlink back to Cargo's target directory.
    std::fs::hard_link(&built, &executable)
        .or_else(|_| std::fs::copy(&built, &executable).map(|_| ()))?;

    let bin = root.path().join("bin");
    std::fs::create_dir(&bin)?;
    let curl = bin.join("curl");
    let shell = if cfg!(target_os = "android") {
        "/system/bin/sh"
    } else {
        "/bin/sh"
    };
    std::fs::write(
        &curl,
        format!(
            "#!{shell}\nprintf '%s\\n' \"$@\" > \"$CODEX_UPDATE_TEST_LOG\"\n\
             cat <<'INSTALLER'\n\
             test \"$CODEX_NON_INTERACTIVE\" = 1 || exit 42\n\
             printf 'installer invoked\\n' >> \"$CODEX_UPDATE_TEST_LOG\"\n\
             exit \"$CODEX_UPDATE_TEST_EXIT\"\n\
             INSTALLER\n"
        ),
    )?;
    std::fs::set_permissions(&curl, std::fs::Permissions::from_mode(0o755))?;
    let path = std::env::join_paths(std::iter::once(bin).chain(std::env::split_paths(
        &std::env::var_os("PATH").unwrap_or_default(),
    )))?;
    let log = root.path().join("installer.log");
    let url = if cfg!(target_os = "android") {
        "https://github.com/shinokiri/codex-termux-native/releases/latest/download/install.sh"
    } else {
        "https://chatgpt.com/codex/install.sh"
    };
    for status in [0, 17] {
        let mut command = assert_cmd::Command::new(&executable);
        command
            .env("CODEX_HOME", &codex_home)
            .env("PATH", &path)
            .env("CODEX_UPDATE_TEST_LOG", &log)
            .env("CODEX_UPDATE_TEST_EXIT", status.to_string())
            .env_remove("CODEX_MANAGED_BY_NPM")
            .env_remove("CODEX_MANAGED_BY_BUN")
            .env_remove("CODEX_MANAGED_BY_PNPM")
            .env_remove("CODEX_MANAGED_BY_VITE_PLUS")
            .timeout(std::time::Duration::from_secs(/*secs*/ 15));
        let result = command.arg("update").assert();
        if status == 0 {
            result.success().stdout(contains("Update ran successfully"));
        } else {
            result.failure().stderr(contains("failed with status"));
        }
        assert_eq!(
            std::fs::read_to_string(&log)?,
            format!("-fsSL\n{url}\ninstaller invoked\n")
        );
    }
    Ok(())
}

#[cfg(not(target_os = "android"))]
#[test]
fn update_prune_requires_termux() -> Result<()> {
    let codex_home = TempDir::new()?;
    assert_cmd::Command::new(codex_utils_cargo_bin::cargo_bin("codex")?)
        .env("CODEX_HOME", codex_home.path())
        .args(["update", "--prune"])
        .assert()
        .failure()
        .stderr(contains(
            "--prune is supported only by Termux installations",
        ));
    Ok(())
}
