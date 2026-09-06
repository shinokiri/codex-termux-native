//! Advisory file locks with an Android implementation.
//!
//! Rust 1.95's `File` locking methods return `Unsupported` on Android, although
//! Bionic supplies `flock`. Use that interface on Android and retain the standard
//! library implementation on other platforms. Errors never grant a lock.
//!
//! On Android, locks belong to the open file description. Duplicated descriptors
//! share a lock; an explicit unlock or closing the last duplicate releases it.
//! Keep the file alive for the critical section. As with `File` locks, callers
//! must coordinate any deletion of lock files to avoid locking different inodes.

#[cfg(any(target_os = "android", all(test, target_os = "linux")))]
mod android;

#[cfg(target_os = "android")]
pub use android::lock;
#[cfg(target_os = "android")]
pub use android::lock_shared;
#[cfg(target_os = "android")]
pub use android::try_lock;
#[cfg(target_os = "android")]
pub use android::try_lock_shared;
#[cfg(target_os = "android")]
pub use android::unlock;

#[cfg(not(target_os = "android"))]
mod portable;

#[cfg(not(target_os = "android"))]
pub use portable::lock;
#[cfg(not(target_os = "android"))]
pub use portable::lock_shared;
#[cfg(not(target_os = "android"))]
pub use portable::try_lock;
#[cfg(not(target_os = "android"))]
pub use portable::try_lock_shared;
#[cfg(not(target_os = "android"))]
pub use portable::unlock;

#[cfg(all(test, any(target_os = "android", target_os = "linux")))]
#[path = "file_lock_tests.rs"]
mod tests;
