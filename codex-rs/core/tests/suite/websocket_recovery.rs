use anyhow::Result;
use codex_core::TurnInputRequest;
use codex_features::Feature;
use codex_protocol::protocol::EventMsg;
use codex_protocol::user_input::UserInput;
use core_test_support::responses;
use core_test_support::responses::ev_assistant_message;
use core_test_support::responses::ev_completed;
use core_test_support::responses::ev_response_created;
use core_test_support::responses::mount_sse_sequence;
use core_test_support::responses::sse;
use core_test_support::skip_if_no_network;
use core_test_support::test_codex::test_codex;
use core_test_support::wait_for_event;
use pretty_assertions::assert_eq;
use std::sync::Arc;
use std::sync::Mutex;
use std::time::Duration;
use tokio::net::TcpListener;
use tokio::net::TcpStream;
use wiremock::Mock;
use wiremock::ResponseTemplate;
use wiremock::matchers::method;
use wiremock::matchers::path_regex;

#[tokio::test]
async fn websocket_disconnects_keep_reconnecting_after_fast_retries() -> Result<()> {
    skip_if_no_network!(Ok(()));
    let server = responses::start_websocket_server(vec![
        vec![
            vec![ev_response_created("warm"), ev_completed("warm")],
            vec![], // Disconnect after receiving the first sampling request.
        ],
        vec![vec![]], // Exhaust the one fast retry.
        vec![vec![ev_response_created("recovered"), ev_completed("recovered")]],
    ])
    .await;
    let test = test_codex()
        .with_config(|config| {
            config.features.enable(Feature::UnboundedConnectionRetries);
            config.model_provider.stream_max_retries = Some(1);
            config.model_provider.request_max_retries = Some(0);
        })
        .build_with_websocket_server(&server)
        .await?;
    test.codex
        .start_or_steer_turn(TurnInputRequest::user_input(vec![UserInput::Text {
            text: "recover this request".into(),
            text_elements: Vec::new(),
        }]))
        .await?;
    let mut retry_messages = Vec::new();
    let mut fallback_warnings = Vec::new();
    loop {
        match wait_for_event(&test.codex, |_| true).await {
            EventMsg::StreamError(event) => retry_messages.push(event.message),
            EventMsg::Warning(event) => fallback_warnings.push(event.message),
            EventMsg::Error(event) => panic!("unexpected error: {}", event.message),
            EventMsg::TurnComplete(event) => {
                assert!(event.error.is_none());
                break;
            }
            _ => {}
        }
    }
    assert_eq!(
        retry_messages.last().map(String::as_str),
        Some("Reconnecting... waiting for network")
    );
    assert!(fallback_warnings.is_empty());
    assert_eq!(server.handshakes().len(), 3);
    server.shutdown().await;
    Ok(())
}

#[test_case::test_case(426; "upgrade rejected")]
#[test_case::test_case(503; "handshake retries exhausted")]
#[tokio::test]
async fn websocket_recovers_after_http_cooldown(status: u16) -> Result<()> {
    skip_if_no_network!(Ok(()));
    let http_server = responses::start_mock_server().await;
    Mock::given(method("GET"))
        .and(path_regex(".*/responses$"))
        .respond_with(ResponseTemplate::new(status))
        .mount(&http_server)
        .await;
    let http_responses = mount_sse_sequence(
        &http_server,
        vec![
            sse(vec![
                ev_response_created("http-1"),
                ev_assistant_message("message-1", "first reply"),
                ev_completed("http-1"),
            ]),
            sse(vec![
                ev_response_created("http-2"),
                ev_assistant_message("message-2", "second reply"),
                ev_completed("http-2"),
            ]),
        ],
    )
    .await;
    let websocket_server = responses::start_websocket_server(vec![vec![vec![
        ev_response_created("ws-3"),
        ev_completed("ws-3"),
    ]]])
    .await;

    // Keep the provider URL fixed while its endpoint recovers from an outage.
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let base_url = format!("http://{}/v1", listener.local_addr()?);
    let upstream = Arc::new(Mutex::new(http_server.address().to_string()));
    let proxy_upstream = Arc::clone(&upstream);
    let proxy = tokio::spawn(async move {
        while let Ok((mut downstream, _)) = listener.accept().await {
            let destination = proxy_upstream.lock().unwrap().clone();
            tokio::spawn(async move {
                let mut upstream = TcpStream::connect(destination).await?;
                tokio::io::copy_bidirectional(&mut downstream, &mut upstream).await
            });
        }
    });
    let test = test_codex()
        .with_config(move |config| {
            config.model_provider.base_url = Some(base_url);
            config.model_provider.supports_websockets = true;
            config.model_provider.stream_max_retries = Some(0);
            config.model_provider.request_max_retries = Some(0);
        })
        .build_with_auto_env(&http_server)
        .await?;
    test.submit_turn("first").await?;
    test.submit_turn("second").await?;
    assert_eq!(http_responses.requests().len(), 2);
    assert!(websocket_server.handshakes().is_empty());

    *upstream.lock().unwrap() = websocket_server
        .uri()
        .strip_prefix("ws://")
        .unwrap()
        .to_string();
    // Advance only between completed turns; network I/O runs with the clock resumed.
    tokio::time::pause();
    tokio::time::advance(Duration::from_secs(31)).await;
    tokio::time::resume();
    test.submit_turn("third").await?;

    assert_eq!(http_responses.requests().len(), 2);
    assert_eq!(websocket_server.handshakes().len(), 1);
    let requests = websocket_server.single_connection();
    assert_eq!(requests.len(), 1);
    let request = requests[0].body_json();
    assert!(request.get("previous_response_id").is_none());
    let input = request["input"].as_array().unwrap();
    let assistant_texts: Vec<_> = input
        .iter()
        .filter(|item| item["role"] == "assistant")
        .flat_map(|item| item["content"].as_array().unwrap())
        .filter_map(|content| content["text"].as_str())
        .collect();
    assert_eq!(assistant_texts, vec!["first reply", "second reply"]);

    proxy.abort();
    websocket_server.shutdown().await;
    Ok(())
}
