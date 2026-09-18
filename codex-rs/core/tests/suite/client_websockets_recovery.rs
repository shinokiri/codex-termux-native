use super::message_item;
use super::prompt_with_input;
use super::stream_until_complete_with_model_info;
use super::websocket_harness_with_provider_options_and_auth;
use codex_login::CodexAuth;
use codex_model_provider_info::ModelProviderInfo;
use core_test_support::responses::ev_completed;
use core_test_support::responses::ev_response_created;
use core_test_support::responses::mount_response_sequence;
use core_test_support::responses::sse;
use core_test_support::responses::sse_response;
use core_test_support::responses::start_mock_server;
use core_test_support::responses::start_websocket_server;
use core_test_support::skip_if_no_network;
use pretty_assertions::assert_eq;
use serde_json::json;
use std::sync::Arc;
use std::sync::Mutex;
use std::time::Duration;
use tokio::net::TcpListener;
use tokio::net::TcpStream;
use wiremock::Mock;
use wiremock::ResponseTemplate;
use wiremock::matchers::method;

#[test_case::test_case("account_id"; "same_account")]
#[test_case::test_case("second-account"; "changed_account")]
#[tokio::test]
async fn websocket_cooldown_preserves_routing_only_for_the_same_account(account_id: &str) {
    skip_if_no_network!();
    let http_server = start_mock_server().await;
    Mock::given(method("GET"))
        .respond_with(ResponseTemplate::new(426))
        .mount(&http_server)
        .await;
    let http_responses = mount_response_sequence(
        &http_server,
        vec![
            sse_response(sse(vec![
                ev_response_created("resp-1"),
                ev_completed("resp-1"),
            ]))
            .insert_header("x-codex-turn-state", "first-account-state"),
        ],
    )
    .await;
    let server = start_websocket_server(vec![
        (2..=3)
            .map(|index| {
                let id = format!("resp-{index}");
                vec![
                    ev_response_created(&id),
                    json!({
                        "type": "response.metadata",
                        "headers": {"x-codex-turn-state": "resumed-state"},
                    }),
                    ev_completed(&id),
                ]
            })
            .collect(),
    ])
    .await;
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base_url = format!("http://{}/v1", listener.local_addr().unwrap());
    let destination = Arc::new(Mutex::new(http_server.address().to_string()));
    let proxy_destination = Arc::clone(&destination);
    let proxy = tokio::spawn(async move {
        while let Ok((mut downstream, _)) = listener.accept().await {
            let destination = proxy_destination.lock().unwrap().clone();
            tokio::spawn(async move {
                let mut upstream = TcpStream::connect(destination).await?;
                tokio::io::copy_bidirectional(&mut downstream, &mut upstream).await
            });
        }
    });
    let harness = websocket_harness_with_provider_options_and_auth(
        ModelProviderInfo::create_openai_provider(Some(base_url)),
        /*runtime_metrics_enabled*/ false,
        /*concurrent_reasoning_summaries_enabled*/ false,
        /*enabled_features*/ &[],
        Some(CodexAuth::create_dummy_chatgpt_auth_for_testing()),
    )
    .await;
    let mut client_session = harness.client.new_session();
    let mut input = vec![message_item("request 1")];
    stream_until_complete_with_model_info(
        &mut client_session,
        &harness,
        &prompt_with_input(input.clone()),
        &harness.model_info,
        "resp-1",
    )
    .await;
    assert_eq!(http_responses.requests().len(), 1);
    assert!(server.handshakes().is_empty());
    *destination.lock().unwrap() = server
        .uri()
        .strip_prefix("ws://")
        .unwrap()
        .to_string();

    if account_id == "second-account" {
        let mut tokens = harness
            .auth_manager
            .auth_cached()
            .unwrap()
            .get_token_data()
            .unwrap();
        tokens.id_token.raw_jwt = "e30.e30.signature".into();
        tokens.account_id = Some(account_id.into());
        tokens.access_token = "second-account-token".into();
        std::fs::write(
            harness.codex_home.path().join("auth.json"),
            serde_json::to_vec(&json!({
                "auth_mode": "chatgpt",
                "tokens": tokens,
                "last_refresh": chrono::Utc::now(),
            }))
            .unwrap(),
        )
        .unwrap();
        harness.auth_manager.reload().await;
    }
    // Expire only between completed requests; perform network I/O with time resumed.
    tokio::time::pause();
    tokio::time::advance(Duration::from_secs(/*secs*/ 31)).await;
    tokio::time::resume();
    for index in 2..=3 {
        input.push(message_item(&format!("request {index}")));
        stream_until_complete_with_model_info(
            &mut client_session,
            &harness,
            &prompt_with_input(input.clone()),
            &harness.model_info,
            &format!("resp-{index}"),
        )
        .await;
    }
    let handshakes = server.handshakes();
    assert_eq!(handshakes.len(), 1);
    assert_eq!(
        handshakes[0].header("chatgpt-account-id"),
        Some(account_id.into())
    );
    let expected_routing = if account_id == "second-account" {
        [json!(null), json!("resumed-state")]
    } else {
        [json!("first-account-state"), json!("first-account-state")]
    };
    let connections = server.connections();
    assert_eq!(
        connections[0]
            .iter()
            .map(|request| {
                let body = request.body_json();
                (
                    body["previous_response_id"].clone(),
                    body["input"].as_array().unwrap().len(),
                    body["client_metadata"]["x-codex-turn-state"].clone(),
                )
            })
            .collect::<Vec<_>>(),
        vec![
            (json!(null), 2, expected_routing[0].clone()),
            (json!("resp-2"), 1, expected_routing[1].clone()),
        ],
    );
    assert_eq!(http_responses.requests().len(), 1);
    proxy.abort();
    server.shutdown().await;
}
