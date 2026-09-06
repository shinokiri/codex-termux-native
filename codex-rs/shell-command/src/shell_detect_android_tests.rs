use std::fs;
use std::os::unix::fs::PermissionsExt;

use pretty_assertions::assert_eq;

use super::android_user_shell_path;

#[test]
fn android_shell_selection_requires_an_existing_executable() -> anyhow::Result<()> {
    let directory = tempfile::tempdir()?;
    let shell = directory.path().join("bash");
    assert_eq!(android_user_shell_path(Some(shell.clone().into())), None);

    fs::write(&shell, b"#!/system/bin/sh\n")?;
    fs::set_permissions(&shell, fs::Permissions::from_mode(0o600))?;
    assert_eq!(android_user_shell_path(Some(shell.clone().into())), None);

    fs::set_permissions(&shell, fs::Permissions::from_mode(0o700))?;
    assert_eq!(
        android_user_shell_path(Some(shell.clone().into())),
        Some(shell)
    );
    Ok(())
}

#[test]
fn android_shell_selection_rejects_unsupported_and_relative_paths() -> anyhow::Result<()> {
    let directory = tempfile::tempdir()?;
    let program = directory.path().join("python");
    fs::write(&program, b"#!/system/bin/sh\n")?;
    fs::set_permissions(&program, fs::Permissions::from_mode(0o700))?;

    assert_eq!(android_user_shell_path(Some(program.into())), None);
    assert_eq!(android_user_shell_path(Some("bash".into())), None);
    assert_eq!(android_user_shell_path(None), None);
    Ok(())
}
