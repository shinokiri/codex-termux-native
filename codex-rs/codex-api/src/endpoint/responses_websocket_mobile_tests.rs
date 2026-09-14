use super::ResponsesWebsocketConnection;
use super::WsStream;
use crate::common::ResponseCreateWsRequest;
use crate::common::ResponseEvent;
use crate::common::ResponsesWsRequest;
use crate::endpoint::responses::ResponsesEndpoint;
use anyhow::Result;
use codex_http_client::HttpClientFactory;
use codex_http_client::OutboundProxyPolicy;
use codex_websocket_client::WebSocketConnector;
use futures::SinkExt;
use futures::StreamExt;
use pretty_assertions::assert_eq;
use serde_json::Value;
use serde_json::json;
use std::time::Duration;
use tokio::net::TcpListener;
use tokio::time::timeout;
use tokio_tungstenite::accept_async;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;

#[tokio::test]
async fn completed_requests_reuse_the_transport_then_automatically_expire() -> Result<()> {
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let request = format!("ws://{}/responses", listener.local_addr()?).into_client_request()?;
    let factory = HttpClientFactory::new(OutboundProxyPolicy::ReqwestDefault);
    let connector = WebSocketConnector::new(&factory)?;
    let (client, server) = tokio::join!(
        connector.connect_loopback_direct(request, WebSocketConfig::default()),
        async {
            let (socket, _) = listener.accept().await?;
            Ok::<_, anyhow::Error>(accept_async(socket).await?)
        }
    );
    let connection = ResponsesWebsocketConnection::new(
        WsStream::new(client?.0, Some(Duration::from_millis(200))),
        Duration::from_secs(3),
        /*server_reasoning_included*/ false,
        /*server_model*/ None,
        /*telemetry*/ None,
        ResponsesEndpoint::Responses,
    );
    let mut server = server?;
    let peer = tokio::spawn(async move {
        for id in ["resp-1", "resp-2"] {
            let message = timeout(Duration::from_secs(3), server.next())
                .await?
                .expect("missing request")?;
            let payload: Value = serde_json::from_str(message.to_text()?)?;
            assert_eq!(payload["type"], "response.create");
            // A response may take longer than the between-request idle limit.
            tokio::time::sleep(Duration::from_millis(300)).await;
            server.send(Message::Ping(vec![9, 8].into())).await?;
            assert_eq!(
                timeout(Duration::from_secs(3), server.next())
                    .await?
                    .transpose()?,
                Some(Message::Pong(vec![9, 8].into()))
            );
            server
                .send(Message::Text(
                    json!({
                        "type": "response.completed",
                        "response": {"id": id, "output": []}
                    })
                    .to_string()
                    .into(),
                ))
                .await?;
        }
        let closed = timeout(Duration::from_secs(3), server.next()).await?;
        assert!(closed.is_none() || closed.is_some_and(|message| message.is_err()));
        Ok::<_, anyhow::Error>(())
    });
    for (index, id) in ["resp-1", "resp-2"].into_iter().enumerate() {
        assert!(!connection.is_closed().await);
        let request = ResponsesWsRequest::ResponseCreate(ResponseCreateWsRequest {
            model: "gpt-test",
            instructions: "",
            previous_response_id: None,
            input: &[],
            tools: None,
            tool_choice: "auto",
            parallel_tool_calls: true,
            reasoning: None,
            store: false,
            stream: true,
            stream_options: None,
            include: &[],
            service_tier: None,
            prompt_cache_key: None,
            text: None,
            generate: None,
            client_metadata: None,
            access_programs: None,
        });
        let mut events = connection
            .stream_request(request, index != 0, /*turn_state*/ None)
            .await?;
        let mut completed = None;
        while let Some(event) = timeout(Duration::from_secs(3), events.next()).await? {
            if let ResponseEvent::Completed { response_id, .. } = event? {
                completed = Some(response_id);
            }
        }
        assert_eq!(completed.as_deref(), Some(id));
    }
    peer.await??;
    assert!(connection.is_closed().await);
    Ok(())
}
