use super::*;
use pretty_assertions::assert_eq;

#[cfg(unix)]
#[test]
fn installer_download_must_finish_before_execution() {
    use std::os::unix::fs::PermissionsExt;
    use std::process::Command;

    let root = tempfile::tempdir().expect("downloader fixture");
    let curl = root.path().join("curl");
    let shell = if cfg!(target_os = "android") {
        "/system/bin/sh"
    } else {
        "/bin/sh"
    };
    std::fs::write(
        &curl,
        format!(
            "#!{shell}\n\
             if [ \"$CODEX_TEST_DOWNLOAD_BODY\" = 1 ]; then\n\
             cat <<'INSTALLER'\n\
             test \"$CODEX_NON_INTERACTIVE\" = 1 || exit 42\n\
             printf 'installer invoked\\n'\n\
             for arg do printf 'argument: %s\\n' \"$arg\"; done\n\
             exit \"$CODEX_TEST_INSTALL_STATUS\"\n\
             INSTALLER\n\
             fi\n\
             exit \"$CODEX_TEST_DOWNLOAD_STATUS\"\n"
        ),
    )
    .expect("fake curl");
    std::fs::set_permissions(&curl, std::fs::Permissions::from_mode(0o755))
        .expect("executable downloader");
    let current_path = std::env::var_os("PATH").unwrap_or_default();
    let path = std::env::join_paths(
        std::iter::once(root.path().to_owned()).chain(std::env::split_paths(&current_path)),
    )
    .expect("fixture PATH");
    for (download_status, body, install_status, args, expected_status, expected_output) in [
        (0, 1, 0, &[][..], 0, "installer invoked\n"),
        (0, 1, 17, &[][..], 17, "installer invoked\n"),
        (
            0,
            1,
            0,
            &["--prune-after-install"][..],
            0,
            "installer invoked\nargument: --prune-after-install\n",
        ),
        (35, 0, 0, &[][..], 35, ""),
        // A failed transfer must not execute even a runnable partial script.
        (35, 1, 0, &["--prune-after-install"][..], 35, ""),
    ] {
        let output = Command::new("sh")
            .args(["-c", INSTALL_COMMAND, "codex-update"])
            .args(args)
            .env("PATH", &path)
            .env("CODEX_TEST_DOWNLOAD_STATUS", download_status.to_string())
            .env("CODEX_TEST_DOWNLOAD_BODY", body.to_string())
            .env("CODEX_TEST_INSTALL_STATUS", install_status.to_string())
            .output()
            .expect("run native update command");
        assert_eq!(
            (output.status.code(), output.stdout, output.stderr),
            (
                Some(expected_status),
                expected_output.as_bytes().to_vec(),
                Vec::<u8>::new(),
            )
        );
    }
}

#[test]
fn local_revision_orders_updates_without_changing_the_runtime_manifest() {
    let root = tempfile::tempdir().expect("package directory");
    let bin = root.path().join("bin");
    std::fs::create_dir(&bin).expect("bin directory");
    let executable = bin.join("codex");
    std::fs::write(&executable, "").expect("executable");
    std::fs::write(
        root.path().join("codex-package.json"),
        r#"{"version":"0.153.4"}"#,
    )
    .expect("runtime manifest");
    std::fs::write(
        root.path().join("BUILD-INFO.json"),
        r#"{"release_version":"0.153.4+termux.2"}"#,
    )
    .expect("local build metadata");
    let context = InstallContext::from_exe(
        /*is_macos*/ false,
        /*current_exe*/ Some(&executable),
        /*method_override*/ None,
    );
    let version = installed_version(&context, "0.153.4");
    assert_eq!(version, "0.153.4+termux.2");
    assert_eq!(
        context.package_manifest().expect("manifest").version,
        Version::new(0, 153, 4)
    );
    assert_eq!(is_newer("0.153.4+termux.2", &version), Some(false));
    assert_eq!(is_newer("0.153.4+termux.3", &version), Some(true));
}

#[test]
fn unpublished_package_does_not_offer_an_update() {
    let mut assets = vec![
        "install.sh".to_owned(),
        "codex-package_SHA256SUMS".to_owned(),
    ];
    assert_eq!(
        version_from_release("termux-v0.153.4+termux.1", &assets),
        None
    );
    assets.push("codex-package-aarch64-linux-android.tar.gz".to_owned());
    assert_eq!(
        version_from_release("termux-v0.153.4+termux.1", &assets),
        Some("0.153.4+termux.1".to_owned())
    );
    assert_eq!(version_from_release("rust-v0.153.4", &assets), None);
}

#[test]
fn upstream_and_termux_revisions_both_order_updates() {
    for (latest, current, expected) in [
        ("0.153.4+termux.2", "0.153.4+termux.1", Some(true)),
        ("0.153.4+termux.2", "0.153.4+termux.2", Some(false)),
        ("0.153.5+termux.1", "0.153.4+termux.9", Some(true)),
        ("0.153.4+termux.9", "0.153.5+termux.1", Some(false)),
        ("0.153.4+termux.1", "main.abcdef+termux.g123456", Some(true)),
        ("0.153.4+termux.1", "0.0.0", Some(true)),
        ("0.154.0-alpha.1", "0.153.4+termux.1", None),
        ("invalid", "0.153.4+termux.1", None),
    ] {
        assert_eq!(is_newer(latest, current), expected);
    }
}
