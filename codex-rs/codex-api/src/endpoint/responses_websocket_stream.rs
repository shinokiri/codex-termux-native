//! Owns the transport pump and releases mobile connections between requests.

use codex_websocket_client::WebSocketConnection;
use futures::SinkExt;
use futures::StreamExt;
use std::future::pending;
use std::sync::Arc;
use std::sync::atomic::AtomicBool;
use std::sync::atomic::Ordering;
use std::time::Duration;
use std::time::SystemTime;
use tokio::sync::mpsc;
use tokio::sync::oneshot;
use tokio::time::Instant;
use tokio_tungstenite::tungstenite::Error as WsError;
use tokio_tungstenite::tungstenite::Message;

struct IdleDeadline {
    monotonic: Instant,
    wall: SystemTime,
    timeout: Duration,
}

impl IdleDeadline {
    fn new(timeout: Duration) -> Self {
        Self {
            monotonic: Instant::now(),
            wall: SystemTime::now(),
            timeout,
        }
    }

    fn remaining(&self) -> Duration {
        // Android's monotonic clock excludes suspend. Check wall time as well
        // when a packet resumes us, so a heartbeat cannot preserve the socket
        // for another full timeout after the phone has already been asleep.
        let elapsed = self
            .monotonic
            .elapsed()
            .max(self.wall.elapsed().unwrap_or_default());
        self.timeout.saturating_sub(elapsed)
    }
}

pub(super) struct WsStream {
    tx_command: mpsc::Sender<WsCommand>,
    rx_message: mpsc::UnboundedReceiver<Result<Message, WsError>>,
    pump_task: tokio::task::JoinHandle<()>,
    idle_expired: Arc<AtomicBool>,
}

enum WsCommand {
    Send {
        message: Message,
        tx_result: oneshot::Sender<Result<(), WsError>>,
    },
    MarkIdle {
        tx_result: oneshot::Sender<Result<(), WsError>>,
    },
}

impl WsStream {
    pub(super) fn new(inner: WebSocketConnection, idle_timeout: Option<Duration>) -> Self {
        let (tx_command, mut rx_command) = mpsc::channel::<WsCommand>(32);
        let (tx_message, rx_message) = mpsc::unbounded_channel::<Result<Message, WsError>>();
        let idle_expired = Arc::new(AtomicBool::new(false));
        let pump_idle_expired = Arc::clone(&idle_expired);
        let pump_task = tokio::spawn(async move {
            let mut inner = inner;
            let mut idle = idle_timeout.map(IdleDeadline::new);
            loop {
                let remaining = idle.as_ref().map(IdleDeadline::remaining);
                tokio::select! {
                    biased;
                    command = rx_command.recv() => {
                        let Some(command) = command else {
                            break;
                        };
                        match command {
                            WsCommand::Send { message, tx_result } => {
                                idle = None;
                                let result = inner.send(message).await;
                                let should_break = result.is_err();
                                let _ = tx_result.send(result);
                                if should_break {
                                    break;
                                }
                            }
                            WsCommand::MarkIdle { tx_result } => {
                                idle = idle_timeout.map(IdleDeadline::new);
                                let _ = tx_result.send(Ok(()));
                            }
                        }
                    }
                    () = async {
                        match remaining {
                            Some(duration) => tokio::time::sleep(duration).await,
                            None => pending().await,
                        }
                    } => {
                        pump_idle_expired.store(true, Ordering::Release);
                        break;
                    }
                    message = inner.next() => {
                        if idle.as_ref().is_some_and(|deadline| deadline.remaining().is_zero()) {
                            pump_idle_expired.store(true, Ordering::Release);
                            break;
                        }
                        let Some(message) = message else {
                            break;
                        };
                        match message {
                            Ok(Message::Ping(payload)) => {
                                if let Err(err) = inner.send(Message::Pong(payload)).await {
                                    let _ = tx_message.send(Err(err));
                                    break;
                                }
                            }
                            Ok(Message::Pong(_)) => {}
                            Ok(message @ (Message::Text(_)
                            | Message::Binary(_)
                            | Message::Close(_)
                            | Message::Frame(_))) => {
                                let is_close = matches!(message, Message::Close(_));
                                if tx_message.send(Ok(message)).is_err() || is_close {
                                    break;
                                }
                            }
                            Err(err) => {
                                let _ = tx_message.send(Err(err));
                                break;
                            }
                        }
                    }
                }
            }
        });
        Self {
            tx_command,
            rx_message,
            pump_task,
            idle_expired,
        }
    }

    pub(super) fn idle_expired(&self) -> bool {
        // Only proactive idle expiry changes the caller's connection reuse decision.
        // Peer closure and stream errors retain the existing response/retry path.
        self.idle_expired.load(Ordering::Acquire)
    }

    async fn request(
        &self,
        make_command: impl FnOnce(oneshot::Sender<Result<(), WsError>>) -> WsCommand,
    ) -> Result<(), WsError> {
        let (tx_result, rx_result) = oneshot::channel();
        if self.tx_command.send(make_command(tx_result)).await.is_err() {
            return Err(WsError::ConnectionClosed);
        }
        rx_result.await.unwrap_or(Err(WsError::ConnectionClosed))
    }

    pub(super) async fn mark_idle(&self) -> Result<(), WsError> {
        self.request(|tx_result| WsCommand::MarkIdle { tx_result })
            .await
    }

    pub(super) async fn send(&self, message: Message) -> Result<(), WsError> {
        self.request(|tx_result| WsCommand::Send { message, tx_result })
            .await
    }

    pub(super) async fn next(&mut self) -> Option<Result<Message, WsError>> {
        self.rx_message.recv().await
    }
}

impl Drop for WsStream {
    fn drop(&mut self) {
        self.pump_task.abort();
    }
}

#[cfg(test)]
#[path = "responses_websocket_stream_tests.rs"]
mod tests;
