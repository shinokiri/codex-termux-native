use super::HTTP_FALLBACK_COOLDOWN;
use super::WebsocketFallback;
use std::time::Duration;

#[tokio::test(start_paused = true)]
async fn fallback_expires_only_when_a_request_checks_the_cooldown() {
    let fallback = WebsocketFallback::default();
    assert!(fallback.activate());
    tokio::time::advance(HTTP_FALLBACK_COOLDOWN - Duration::from_secs(1)).await;
    assert!(!fallback.try_expire());
    assert!(fallback.is_active());

    // Another failed HTTP request must not postpone the next WebSocket attempt.
    assert!(!fallback.activate());
    tokio::time::advance(Duration::from_secs(1)).await;
    assert!(fallback.is_active());
    assert!(fallback.try_expire());
    assert!(!fallback.is_active());

    // If the next WebSocket attempt fails, give HTTP another full cooldown.
    assert!(fallback.activate());
    assert!(!fallback.try_expire());
    tokio::time::advance(HTTP_FALLBACK_COOLDOWN).await;
    assert!(fallback.try_expire());
}
