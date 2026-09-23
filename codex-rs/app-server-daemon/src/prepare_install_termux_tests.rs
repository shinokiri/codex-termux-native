use super::release_version;
use pretty_assertions::assert_eq;

#[test]
fn retains_the_published_revision_when_seeding_a_daemon() {
    let package = tempfile::TempDir::new().unwrap();
    std::fs::write(
        package.path().join("BUILD-INFO.json"),
        r#"{"release_version":"0.156.1+termux.11"}"#,
    )
    .unwrap();
    assert_eq!(
        release_version(package.path(), "0.156.1").unwrap(),
        Some("0.156.1+termux.11".to_string())
    );
}

#[test]
fn development_packages_stay_pinned() {
    let package = tempfile::TempDir::new().unwrap();
    assert_eq!(release_version(package.path(), "0.156.1").unwrap(), None);
    std::fs::write(
        package.path().join("BUILD-INFO.json"),
        r#"{"release_version":null}"#,
    )
    .unwrap();
    assert_eq!(release_version(package.path(), "0.156.1").unwrap(), None);
}

#[test]
fn refuses_mismatched_or_invalid_release_provenance() {
    let package = tempfile::TempDir::new().unwrap();
    for version in [
        "0.155.1+termux.11",
        "0.156.1+termux.0",
        "0.156.1+termux.",
        "0.156.1+termux.-1",
        "0.156.1+termux.1.2",
        "0.156.1",
        "0.156.1-alpha.1+termux.11",
    ] {
        std::fs::write(
            package.path().join("BUILD-INFO.json"),
            serde_json::json!({"release_version": version}).to_string(),
        )
        .unwrap();
        assert!(release_version(package.path(), "0.156.1").is_err());
    }
}

#[test]
fn refuses_unreadable_or_malformed_provenance() {
    let package = tempfile::TempDir::new().unwrap();
    let info = package.path().join("BUILD-INFO.json");
    std::fs::write(&info, b"not json").unwrap();
    assert!(release_version(package.path(), "0.156.1").is_err());
    std::fs::remove_file(&info).unwrap();
    std::fs::create_dir(&info).unwrap();
    assert!(release_version(package.path(), "0.156.1").is_err());
}
