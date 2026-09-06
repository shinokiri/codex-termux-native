use std::fs::File;
use std::fs::TryLockError;
use std::io;

/// Wait for an exclusive lock on `file`.
pub fn lock(file: &File) -> io::Result<()> {
    file.lock()
}

/// Wait for a shared lock on `file`.
pub fn lock_shared(file: &File) -> io::Result<()> {
    file.lock_shared()
}

/// Acquire an exclusive lock, returning `WouldBlock` on contention.
pub fn try_lock(file: &File) -> Result<(), TryLockError> {
    file.try_lock()
}

/// Acquire a shared lock, returning `WouldBlock` on contention.
pub fn try_lock_shared(file: &File) -> Result<(), TryLockError> {
    file.try_lock_shared()
}

/// Release the lock on `file`.
pub fn unlock(file: &File) -> io::Result<()> {
    file.unlock()
}
