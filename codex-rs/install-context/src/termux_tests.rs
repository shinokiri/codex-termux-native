use super::*;
use pretty_assertions::assert_eq;

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
