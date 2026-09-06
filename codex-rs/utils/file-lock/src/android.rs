use std::fs::File;
use std::fs::TryLockError;
use std::io;
use std::os::fd::AsRawFd;

/// Wait for an exclusive lock on `file`, retrying interrupted system calls.
pub fn lock(file: &File) -> io::Result<()> {
    flock(file, libc::LOCK_EX)
}

/// Wait for a shared lock on `file`, retrying interrupted system calls.
pub fn lock_shared(file: &File) -> io::Result<()> {
    flock(file, libc::LOCK_SH)
}

/// Acquire an exclusive lock, returning `WouldBlock` on contention.
pub fn try_lock(file: &File) -> Result<(), TryLockError> {
    try_flock(file, libc::LOCK_EX)
}

/// Acquire a shared lock, returning `WouldBlock` on contention.
pub fn try_lock_shared(file: &File) -> Result<(), TryLockError> {
    try_flock(file, libc::LOCK_SH)
}

/// Release the lock shared by `file` and any of its duplicated descriptors.
pub fn unlock(file: &File) -> io::Result<()> {
    flock(file, libc::LOCK_UN)
}

fn try_flock(file: &File, operation: libc::c_int) -> Result<(), TryLockError> {
    flock(file, operation | libc::LOCK_NB).map_err(|error| {
        if error.kind() == io::ErrorKind::WouldBlock {
            TryLockError::WouldBlock
        } else {
            TryLockError::Error(error)
        }
    })
}

fn flock(file: &File, operation: libc::c_int) -> io::Result<()> {
    loop {
        // SAFETY: `file` owns a live descriptor for the duration of this call.
        // The only callers supply flock operations from libc; no pointer crosses
        // the FFI boundary. Closing the descriptor is left to File's ownership.
        if unsafe { libc::flock(file.as_raw_fd(), operation) } == 0 {
            return Ok(());
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
}
