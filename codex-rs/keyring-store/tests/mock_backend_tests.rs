use codex_keyring_store::DefaultKeyringStore;
use codex_keyring_store::KeyringStore;
use keyring::Entry;
use keyring::Error as KeyringError;

#[test]
fn mock_backend_cannot_report_a_persistent_save() -> Result<(), Box<dyn std::error::Error>> {
    keyring::set_default_credential_builder(keyring::mock::default_credential_builder());
    let service = "codex-keyring-store-test";
    let account = "mock-backend";

    // The dependency reports success even though a fresh entry loses the value.
    Entry::new(service, account)?.set_password("test-value")?;
    assert!(matches!(
        Entry::new(service, account)?.get_password(),
        Err(KeyringError::NoEntry)
    ));

    let store = DefaultKeyringStore;
    let error = store.save(service, account, "test-value").unwrap_err();
    assert!(matches!(error.into_error(), KeyringError::NoStorageAccess(_)));

    // Absence stays nonfatal so callers can read and delete their fallback file.
    assert!(store.load(service, account)?.is_none());
    assert!(!store.delete(service, account)?);
    Ok(())
}
