use super::ResponsesStreamRequest;
use super::log_retry;
use crate::session::tests::make_session_and_context;
use codex_protocol::error::CodexErr;
use std::time::Duration;
use tracing_test::internal::MockWriter;

#[tokio::test]
async fn sampling_retry_logs_stream_error_context() {
    let (_session, turn_context) = make_session_and_context().await;
    let buffer: &'static std::sync::Mutex<Vec<u8>> =
        Box::leak(Box::new(std::sync::Mutex::new(Vec::new())));
    let subscriber = tracing_subscriber::fmt()
        .with_ansi(false)
        .with_max_level(tracing::Level::WARN)
        .with_writer(MockWriter::new(buffer))
        .finish();
    let _subscriber_guard = tracing::subscriber::set_default(subscriber);

    log_retry(
        ResponsesStreamRequest::Sampling,
        &turn_context,
        &CodexErr::Stream("websocket closed by server before response.completed".to_string()),
        /*retries*/ 2,
        /*max_retries*/ 5,
        Duration::from_secs(1),
    );

    let logs = String::from_utf8(
        buffer
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone(),
    )
    .expect("retry log should be valid utf-8");
    assert!(logs.contains("stream disconnected - retrying sampling request"));
    assert!(logs.contains(&format!("turn_id={}", turn_context.sub_id)));
    assert!(logs.contains("retries=2"));
    assert!(logs.contains("max_retries=5"));
    assert!(logs.contains(
        "sampling_error=stream disconnected before completion: websocket closed by server before response.completed"
    ));
}

#[test_case::test_case(true, ResponsesStreamRequest::Sampling, CodexErr::Stream("closed".into()), true; "disconnect waits")]
#[test_case::test_case(true, ResponsesStreamRequest::Sampling, CodexErr::RequestTimeout, true; "connect timeout waits")]
#[test_case::test_case(false, ResponsesStreamRequest::Sampling, CodexErr::Stream("closed".into()), false; "disabled feature falls back")]
#[test_case::test_case(true, ResponsesStreamRequest::RemoteCompactionV2, CodexErr::Stream("closed".into()), false; "compaction stays bounded")]
#[tokio::test]
async fn websocket_waiting_respects_retry_policy(
    unbounded: bool,
    request: ResponsesStreamRequest,
    error: CodexErr,
    websocket_enabled: bool,
) {
    let (session, turn_context, _events) =
        crate::session::tests::make_session_and_context_with_auth_and_config_and_rx(
            codex_login::CodexAuth::from_api_key("test-key"),
            Vec::new(),
            |config| {
                config.model_provider.supports_websockets = true;
                if unbounded {
                    config
                        .features
                        .enable(codex_features::Feature::UnboundedConnectionRetries);
                } else {
                    config
                        .features
                        .disable(codex_features::Feature::UnboundedConnectionRetries);
                }
            },
        )
        .await;
    let mut client_session = session.services.model_client.new_session();
    tokio::time::pause();
    super::handle_retryable_response_stream_error(
        &mut super::ResponsesStreamRetryState::default(),
        /*max_retries*/ 0,
        error,
        &mut client_session,
        &session,
        &turn_context,
        request,
    )
    .await
    .expect("retry or fallback should succeed");
    pretty_assertions::assert_eq!(
        session.services.model_client.responses_websocket_enabled(),
        websocket_enabled
    );
}
