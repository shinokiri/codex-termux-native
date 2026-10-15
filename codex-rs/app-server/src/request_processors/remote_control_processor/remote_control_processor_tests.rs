use super::*;
use crate::error_code::INTERNAL_ERROR_CODE;
use crate::error_code::INVALID_REQUEST_ERROR_CODE;
use pretty_assertions::assert_eq;

#[tokio::test]
async fn daemon_preference_preserves_other_settings() -> anyhow::Result<()> {
    let dir = tempfile::tempdir()?;
    let path = dir.path().join("daemon").join("settings.json");
    let mut processor = RemoteControlRequestProcessor::new(None);
    processor.daemon_settings_file = Some(path.clone());
    processor.persist_daemon_preference(true).await?;
    let settings: Value = serde_json::from_slice(&fs::read(&path)?)?;
    assert_eq!(settings, serde_json::json!({"remoteControlEnabled": true}));

    let mut settings = serde_json::json!({
        "remoteControlEnabled": true,
        "shutdownGraceSeconds": 3,
        "updater": {"autoUpdateEnabled": false}
    });
    fs::write(&path, serde_json::to_vec(&settings)?)?;
    processor.persist_daemon_preference(false).await?;
    settings["remoteControlEnabled"] = Value::Bool(false);
    assert_eq!(serde_json::from_slice::<Value>(&fs::read(path)?)?, settings);
    Ok(())
}

#[tokio::test]
async fn daemon_preference_yields_to_lifecycle_lock() -> anyhow::Result<()> {
    let dir = tempfile::tempdir()?;
    let path = dir.path().join("settings.json");
    let original = br#"{"remoteControlEnabled":false,"shutdownGraceSeconds":3}"#;
    fs::write(&path, original)?;
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(dir.path().join("daemon.lock"))?;
    codex_utils_file_lock::try_lock(&lock)?;
    let mut processor = RemoteControlRequestProcessor::new(None);
    processor.daemon_settings_file = Some(path.clone());
    let result = tokio::time::timeout(
        std::time::Duration::from_secs(5),
        processor.persist_daemon_preference(true),
    )
    .await;
    drop(lock);
    result.expect("preference persistence must not wait for the lifecycle lock")?;
    assert_eq!(fs::read(&path)?, original.to_vec());

    processor.persist_daemon_preference(true).await?;
    assert_eq!(
        serde_json::from_slice::<Value>(&fs::read(path)?)?,
        serde_json::json!({"remoteControlEnabled": true, "shutdownGraceSeconds": 3})
    );
    Ok(())
}

#[tokio::test]
async fn pairing_start_returns_internal_error_when_remote_control_is_unavailable() {
    let err = RemoteControlRequestProcessor::new(/*remote_control_handle*/ None)
        .pairing_start(
            RemoteControlPairingStartParams::default(),
            /*app_server_client_name*/ None,
        )
        .await
        .expect_err("missing remote control should fail pairing");

    assert_eq!(
        err,
        JSONRPCErrorError {
            code: INTERNAL_ERROR_CODE,
            data: None,
            message: "remote control is unavailable for this app-server".to_string(),
        }
    );
}

#[tokio::test]
async fn pairing_status_returns_internal_error_when_remote_control_is_unavailable() {
    let err = RemoteControlRequestProcessor::new(/*remote_control_handle*/ None)
        .pairing_status(RemoteControlPairingStatusParams {
            pairing_code: Some("pairing-code".to_string()),
            manual_pairing_code: None,
        })
        .await
        .expect_err("missing remote control should fail pairing status");

    assert_eq!(
        err,
        JSONRPCErrorError {
            code: INTERNAL_ERROR_CODE,
            data: None,
            message: "remote control is unavailable for this app-server".to_string(),
        }
    );
}

#[test]
fn pairing_status_rejects_missing_pairing_codes() {
    assert_eq!(
        validate_pairing_status_params(&RemoteControlPairingStatusParams {
            pairing_code: None,
            manual_pairing_code: None,
        }),
        Err(JSONRPCErrorError {
            code: INVALID_REQUEST_ERROR_CODE,
            data: None,
            message: "remoteControl/pairing/status requires pairingCode or manualPairingCode"
                .to_string(),
        })
    );
}

#[test]
fn pairing_status_rejects_conflicting_pairing_codes() {
    assert_eq!(
        validate_pairing_status_params(&RemoteControlPairingStatusParams {
            pairing_code: Some("pairing-code".to_string()),
            manual_pairing_code: Some("ABCD-EFGH".to_string()),
        }),
        Err(JSONRPCErrorError {
            code: INVALID_REQUEST_ERROR_CODE,
            data: None,
            message:
                "remoteControl/pairing/status accepts either pairingCode or manualPairingCode, not both"
                    .to_string(),
        })
    );
}

#[test]
fn pairing_start_maps_invalid_input_to_invalid_request() {
    assert_eq!(
        map_pairing_start_error(io::Error::new(
            io::ErrorKind::InvalidInput,
            "remote control pairing is unavailable",
        )),
        JSONRPCErrorError {
            code: INVALID_REQUEST_ERROR_CODE,
            data: None,
            message: "remote control pairing is unavailable".to_string(),
        }
    );
}

#[test]
fn pairing_start_maps_backend_failures_to_internal_error() {
    assert_eq!(
        map_pairing_start_error(io::Error::other("remote control pairing failed")),
        JSONRPCErrorError {
            code: INTERNAL_ERROR_CODE,
            data: None,
            message: "remote control pairing failed".to_string(),
        }
    );
}

#[test]
fn client_management_maps_user_actionable_errors_to_invalid_request() {
    for kind in [
        io::ErrorKind::InvalidInput,
        io::ErrorKind::NotFound,
        io::ErrorKind::PermissionDenied,
        io::ErrorKind::WouldBlock,
    ] {
        assert_eq!(
            map_client_management_error(io::Error::new(kind, "client management unavailable")),
            JSONRPCErrorError {
                code: INVALID_REQUEST_ERROR_CODE,
                data: None,
                message: "client management unavailable".to_string(),
            }
        );
    }
}

#[test]
fn client_management_maps_backend_failures_to_internal_error() {
    assert_eq!(
        map_client_management_error(io::Error::other("client management failed")),
        JSONRPCErrorError {
            code: INTERNAL_ERROR_CODE,
            data: None,
            message: "client management failed".to_string(),
        }
    );
}
