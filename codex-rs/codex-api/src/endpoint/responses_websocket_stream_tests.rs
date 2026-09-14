use super::IdleDeadline;
use super::WsStream;
use anyhow::Result;
use codex_http_client::HttpClientFactory;
use codex_http_client::OutboundProxyPolicy;
use codex_websocket_client::WebSocketConnector;
use futures::SinkExt;
use futures::StreamExt;
use pretty_assertions::assert_eq;
use std::time::Duration;
use std::time::SystemTime;
use tokio::net::TcpListener;
use tokio::net::TcpStream;
use tokio::time::Instant;
use tokio::time::timeout;
use tokio_tungstenite::WebSocketStream;
use tokio_tungstenite::accept_async;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;

async fn pair(idle_timeout: Option<Duration>) -> Result<(WsStream, WebSocketStream<TcpStream>)> {
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
    Ok((WsStream::new(client?.0, idle_timeout), server?))
}

#[tokio::test]
async fn idle_transport_expires_without_waiting_for_a_server_ping() -> Result<()> {
    let (mut client, mut server) = pair(Some(Duration::from_millis(100))).await?;
    assert!(timeout(Duration::from_secs(3), client.next()).await?.is_none());
    assert!(client.is_closed());
    let closed = timeout(Duration::from_secs(3), server.next()).await?;
    assert!(closed.is_none() || closed.is_some_and(|message| message.is_err()));
    Ok(())
}

#[tokio::test]
async fn active_response_keeps_pong_replies_after_the_idle_timeout() -> Result<()> {
    let (mut client, mut server) = pair(Some(Duration::from_millis(100))).await?;
    let request = Message::Text("response.create".into());
    client.send(request.clone()).await?;
    assert_eq!(server.next().await.transpose()?, Some(request));
    tokio::time::sleep(Duration::from_millis(200)).await;
    server.send(Message::Ping(vec![1, 2, 3, 4].into())).await?;
    assert_eq!(
        timeout(Duration::from_secs(3), server.next()).await?.transpose()?,
        Some(Message::Pong(vec![1, 2, 3, 4].into()))
    );
    assert!(!client.is_closed());
    client.mark_idle().await?;
    assert!(timeout(Duration::from_secs(3), client.next()).await?.is_none());
    assert!(client.is_closed());
    Ok(())
}

#[tokio::test]
async fn a_new_request_cancels_idle_expiry_until_it_completes() -> Result<()> {
    let (mut client, mut server) = pair(Some(Duration::from_millis(200))).await?;
    let first = Message::Text("first request".into());
    client.send(first.clone()).await?;
    assert_eq!(server.next().await.transpose()?, Some(first));
    client.mark_idle().await?;
    let second = Message::Text("second request".into());
    client.send(second.clone()).await?;
    assert_eq!(server.next().await.transpose()?, Some(second));
    tokio::time::sleep(Duration::from_millis(300)).await;
    let response = Message::Text("second response".into());
    server.send(response.clone()).await?;
    assert_eq!(
        timeout(Duration::from_secs(3), client.next()).await?.transpose()?,
        Some(response)
    );
    assert!(!client.is_closed());
    client.mark_idle().await?;
    assert!(timeout(Duration::from_secs(3), client.next()).await?.is_none());
    Ok(())
}

#[tokio::test]
async fn an_unlimited_transport_still_answers_idle_pings() -> Result<()> {
    let (client, mut server) = pair(None).await?;
    client.mark_idle().await?;
    tokio::time::sleep(Duration::from_millis(200)).await;
    server.send(Message::Ping(vec![5, 6].into())).await?;
    assert_eq!(
        timeout(Duration::from_secs(3), server.next()).await?.transpose()?,
        Some(Message::Pong(vec![5, 6].into()))
    );
    assert!(!client.is_closed());
    Ok(())
}

#[test]
fn suspend_time_expires_idle_even_when_the_monotonic_clock_did_not_advance() {
    let deadline = IdleDeadline {
        monotonic: Instant::now(),
        wall: SystemTime::now() - Duration::from_secs(30),
        timeout: Duration::from_secs(10),
    };
    assert_eq!(deadline.remaining(), Duration::ZERO);
}

#[test]
fn a_backwards_wall_clock_does_not_postpone_monotonic_expiry() {
    let deadline = IdleDeadline {
        monotonic: Instant::now() - Duration::from_secs(30),
        wall: SystemTime::now() + Duration::from_secs(30),
        timeout: Duration::from_secs(10),
    };
    assert_eq!(deadline.remaining(), Duration::ZERO);
}
