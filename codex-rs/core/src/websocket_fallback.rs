//! Session-scoped cooldown after a Responses WebSocket transport fallback.

use std::sync::Mutex;
use std::time::Duration;
use tokio::time::Instant;

const HTTP_FALLBACK_COOLDOWN: Duration = Duration::from_secs(30);

#[derive(Debug, Default)]
pub(crate) struct WebsocketFallback {
    retry_at: Mutex<Option<Instant>>,
}

impl WebsocketFallback {
    pub(crate) fn is_active(&self) -> bool {
        self.retry_at
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .is_some()
    }

    /// Repeated failures during fallback must not extend the cooldown or reset retries.
    pub(crate) fn activate(&self) -> bool {
        let mut retry_at = self
            .retry_at
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if retry_at.is_some() {
            return false;
        }
        *retry_at = Some(Instant::now() + HTTP_FALLBACK_COOLDOWN);
        true
    }

    /// Re-enable WebSockets only at a request boundary, never while an HTTP stream is running.
    pub(crate) fn try_expire(&self) -> bool {
        let mut retry_at = self
            .retry_at
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if retry_at.is_some_and(|deadline| Instant::now() >= deadline) {
            *retry_at = None;
            return true;
        }
        false
    }
}

#[cfg(test)]
#[path = "websocket_fallback_tests.rs"]
mod tests;
