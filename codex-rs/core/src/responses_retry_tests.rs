use super::ResponsesStreamRequest;
use super::ResponsesStreamRetryState;
use super::handle_response_stream_error;
use super::log_retry;
use crate::realtime_history::RealtimeHistoryState;
use crate::session::step_context::StepContext;
use crate::session::tests::make_session_and_context;
use codex_http_client::RetryAfter;
use codex_protocol::error::CodexErr;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::Mutex;
use tokio::time::Instant;
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

/// Time spent reporting a retry must count toward the server's original deadline.
#[tokio::test]
#[expect(
    clippy::await_holding_invalid_type,
    reason = "test holds the event-delivery lock to delay notification while virtual time advances"
)]
async fn stream_retry_preserves_deadline_across_delayed_notification() {
    let (mut session, turn_context) = make_session_and_context().await;
    let step_context = StepContext::for_test(Arc::new(turn_context));
    session.realtime_history = Some(Mutex::new(RealtimeHistoryState::default()));
    let mut client_session = session.services.model_client.new_session();
    // The second retry emits a notification even when release builds hide the first.
    let mut retry_state = ResponsesStreamRetryState {
        retries: 1,
        ..Default::default()
    };

    tokio::time::pause();
    let advice = RetryAfter::from_delay(Duration::from_secs(10)).expect("retry deadline");
    tokio::time::advance(Duration::from_secs(4)).await;

    // Hold the event-delivery lock so reporting the error consumes part of the deadline.
    let history_guard = session.realtime_history.as_ref().unwrap().lock().await;
    let retry = handle_response_stream_error(
        &mut retry_state,
        /*max_retries*/ 2,
        CodexErr::InternalServerError.with_retry_after(advice),
        &mut client_session,
        &session,
        &step_context,
        ResponsesStreamRequest::Sampling,
    );
    tokio::pin!(retry);
    assert!(futures::poll!(&mut retry).is_pending());
    tokio::time::advance(Duration::from_secs(4)).await;
    drop(history_guard);

    assert!(futures::poll!(&mut retry).is_pending());
    tokio::time::advance(Duration::from_secs(1)).await;
    assert!(futures::poll!(&mut retry).is_pending());
    retry.await.expect("retry should be allowed");
    // Tokio rounds timer deadlines up to the next millisecond.
    let resumed_at = Instant::now();
    assert!(
        (advice.deadline()..=advice.deadline() + Duration::from_millis(1)).contains(&resumed_at),
        "retry resumed at {resumed_at:?}, expected {advice:?}"
    );
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
    super::handle_response_stream_error(
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

#[tokio::test]
async fn websocket_waiting_honors_server_advice_without_resetting_backoff() {
    let (session, turn_context, _events) =
        crate::session::tests::make_session_and_context_with_auth_and_config_and_rx(
            codex_login::CodexAuth::from_api_key("test-key"),
            Vec::new(),
            |config| {
                config.model_provider.supports_websockets = true;
                config
                    .features
                    .enable(codex_features::Feature::UnboundedConnectionRetries);
            },
        )
        .await;
    let mut client_session = session.services.model_client.new_session();
    let mut retry_state = super::ResponsesStreamRetryState::default();
    tokio::time::pause();
    for (error, expected_delay) in [
        (
            CodexErr::Stream("closed".into()).with_retry_after(
                RetryAfter::from_delay(Duration::from_millis(20)).expect("retry deadline"),
            ),
            Duration::from_millis(20),
        ),
        (
            CodexErr::Stream("closed again".into()),
            Duration::from_secs(10),
        ),
    ] {
        let before = tokio::time::Instant::now();
        super::handle_response_stream_error(
            &mut retry_state,
            /*max_retries*/ 0,
            error,
            &mut client_session,
            &session,
            &turn_context,
            ResponsesStreamRequest::Sampling,
        )
        .await
        .expect("retry should succeed");
        // Tokio timers round deadlines to their millisecond tick, even with paused time.
        let elapsed = before.elapsed();
        assert!(
            elapsed >= expected_delay && elapsed <= expected_delay + Duration::from_millis(1),
            "waited {elapsed:?}, expected {expected_delay:?} within timer resolution",
        );
        assert!(session.services.model_client.responses_websocket_enabled());
    }
}

#[tokio::test]
#[expect(
    clippy::await_holding_invalid_type,
    reason = "test holds the event-delivery lock to delay notification while virtual time advances"
)]
async fn websocket_waiting_preserves_deadline_across_delayed_notification() {
    let (mut session, turn_context, _events) =
        crate::session::tests::make_session_and_context_with_auth_and_config_and_rx(
            codex_login::CodexAuth::from_api_key("test-key"),
            Vec::new(),
            |config| {
                config.model_provider.supports_websockets = true;
                config
                    .features
                    .enable(codex_features::Feature::UnboundedConnectionRetries);
            },
        )
        .await;
    std::sync::Arc::get_mut(&mut session)
        .expect("unique test session")
        .realtime_history = Some(Mutex::new(RealtimeHistoryState::default()));
    let mut client_session = session.services.model_client.new_session();
    let mut retry_state = ResponsesStreamRetryState::default();

    tokio::time::pause();
    let advice = RetryAfter::from_delay(Duration::from_secs(10)).expect("retry deadline");
    tokio::time::advance(Duration::from_secs(4)).await;
    let history_guard = session.realtime_history.as_ref().unwrap().lock().await;
    let retry = handle_response_stream_error(
        &mut retry_state,
        /*max_retries*/ 0,
        CodexErr::Stream("closed".into()).with_retry_after(advice),
        &mut client_session,
        &session,
        &turn_context,
        ResponsesStreamRequest::Sampling,
    );
    tokio::pin!(retry);
    assert!(futures::poll!(&mut retry).is_pending());
    tokio::time::advance(Duration::from_secs(4)).await;
    drop(history_guard);

    assert!(futures::poll!(&mut retry).is_pending());
    tokio::time::advance(Duration::from_secs(1)).await;
    assert!(futures::poll!(&mut retry).is_pending());
    retry.await.expect("retry should be allowed");
    let resumed_at = Instant::now();
    assert!(
        (advice.deadline()..=advice.deadline() + Duration::from_millis(1)).contains(&resumed_at),
        "retry resumed at {resumed_at:?}, expected {advice:?}"
    );
    assert!(session.services.model_client.responses_websocket_enabled());
}
