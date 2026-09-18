use super::message_item;
use super::prompt_with_input;
use super::stream_until_complete_with_model_info;
use super::websocket_harness_for_codex_backend;
use core_test_support::responses::ev_completed;
use core_test_support::responses::ev_response_created;
use core_test_support::responses::start_websocket_server;
use core_test_support::skip_if_no_network;
use pretty_assertions::assert_eq;
use serde_json::json;
use std::time::Duration;

#[test_case::test_case("account_id"; "same_account")]
#[test_case::test_case("second-account"; "changed_account")]
#[tokio::test]
async fn websocket_cooldown_preserves_routing_only_for_the_same_account(account_id: &str) {
    skip_if_no_network!();
    let server = start_websocket_server(
        [(1..=3, "first-account-state"), (2..=3, "resumed-state")]
            .into_iter()
            .map(|(responses, turn_state)| {
                responses
                    .map(|index| {
                        let id = format!("resp-{index}");
                        vec![
                            ev_response_created(&id),
                            json!({
                                "type": "response.metadata",
                                "headers": {"x-codex-turn-state": turn_state},
                            }),
                            ev_completed(&id),
                        ]
                    })
                    .collect()
            })
            .collect(),
    )
    .await;
    let harness = websocket_harness_for_codex_backend(&server).await;
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
    assert!(
        harness
            .client
            .force_http_fallback(&harness.session_telemetry, &harness.model_info)
    );

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
    assert_eq!(handshakes.len(), 2);
    assert_eq!(
        handshakes[1].header("chatgpt-account-id"),
        Some(account_id.into())
    );
    let expected_routing = if account_id == "second-account" {
        [json!(null), json!("resumed-state")]
    } else {
        [json!("first-account-state"), json!("first-account-state")]
    };
    let connections = server.connections();
    assert_eq!(
        connections[1]
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
    server.shutdown().await;
}
