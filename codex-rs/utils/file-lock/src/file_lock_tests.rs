use std::fs::File;
use std::fs::OpenOptions;
use std::fs::TryLockError;
use std::io;
use std::io::Read;
use std::os::unix::process::ExitStatusExt;
use std::process::Child;
use std::process::Command;
use std::process::Stdio;
use std::sync::mpsc;
use std::thread;
use std::time::Duration;
use std::time::Instant;

use pretty_assertions::assert_eq;

type TestResult = Result<(), Box<dyn std::error::Error>>;

#[derive(Clone, Copy, Debug)]
struct LockApi {
    name: &'static str,
    lock: fn(&File) -> io::Result<()>,
    lock_shared: fn(&File) -> io::Result<()>,
    try_lock: fn(&File) -> Result<(), TryLockError>,
    try_lock_shared: fn(&File) -> Result<(), TryLockError>,
    unlock: fn(&File) -> io::Result<()>,
}

const PUBLIC: LockApi = LockApi {
    name: "public",
    lock: super::lock,
    lock_shared: super::lock_shared,
    try_lock: super::try_lock,
    try_lock_shared: super::try_lock_shared,
    unlock: super::unlock,
};

const ANDROID: LockApi = LockApi {
    name: "android",
    lock: super::android::lock,
    lock_shared: super::android::lock_shared,
    try_lock: super::android::try_lock,
    try_lock_shared: super::android::try_lock_shared,
    unlock: super::android::unlock,
};

#[cfg(target_os = "linux")]
const APIS: &[LockApi] = &[PUBLIC, ANDROID];
#[cfg(target_os = "android")]
const APIS: &[LockApi] = &[PUBLIC];

#[test]
fn exclusive_lock_blocks_separate_readers_and_writers() -> TestResult {
    for &api in APIS {
        let file = tempfile::NamedTempFile::new()?;
        let contender = file.reopen()?;
        (api.lock)(file.as_file())?;
        assert!(
            matches!((api.try_lock)(&contender), Err(TryLockError::WouldBlock)),
            "{api:?}"
        );
        assert!(
            matches!((api.try_lock_shared)(&contender), Err(TryLockError::WouldBlock)),
            "{api:?}"
        );
        (api.unlock)(file.as_file())?;
        (api.try_lock)(&contender)?;
    }
    Ok(())
}

#[test]
fn shared_readers_block_writers_until_the_last_reader_closes() -> TestResult {
    for &api in APIS {
        let path = tempfile::NamedTempFile::new()?;
        let first = path.reopen()?;
        let second = path.reopen()?;
        let writer = path.reopen()?;
        (api.lock_shared)(&first)?;
        (api.try_lock_shared)(&second)?;
        assert!(
            matches!((api.try_lock)(&writer), Err(TryLockError::WouldBlock)),
            "{api:?}"
        );
        drop(first);
        assert!(
            matches!((api.try_lock)(&writer), Err(TryLockError::WouldBlock)),
            "{api:?}"
        );
        drop(second);
        (api.try_lock)(&writer)?;
    }
    Ok(())
}

#[test]
fn duplicated_descriptor_keeps_the_lock_alive() -> TestResult {
    for &api in APIS {
        let path = tempfile::NamedTempFile::new()?;
        let owner = path.reopen()?;
        let contender = path.reopen()?;
        (api.lock)(&owner)?;
        let duplicate = owner.try_clone()?;
        drop(owner);
        assert!(
            matches!((api.try_lock)(&contender), Err(TryLockError::WouldBlock)),
            "{api:?}"
        );
        drop(duplicate);
        (api.try_lock)(&contender)?;
    }
    Ok(())
}

#[test]
fn blocking_acquisition_resumes_after_unlock() -> TestResult {
    for &api in APIS {
        let path = tempfile::NamedTempFile::new()?;
        let contender = path.reopen()?;
        (api.lock)(path.as_file())?;
        let (started_tx, started_rx) = mpsc::channel();
        let (acquired_tx, acquired_rx) = mpsc::channel();
        let waiter = thread::spawn(move || {
            let _ = started_tx.send(());
            let _ = acquired_tx.send((api.lock)(&contender));
        });
        started_rx.recv_timeout(Duration::from_secs(5))?;
        let waiting = acquired_rx.recv_timeout(Duration::from_millis(100));
        // Release before asserting, so a failing test cannot strand the waiter.
        (api.unlock)(path.as_file())?;
        assert!(
            matches!(waiting, Err(mpsc::RecvTimeoutError::Timeout)),
            "{api:?}"
        );
        acquired_rx.recv_timeout(Duration::from_secs(5))??;
        assert!(waiter.join().is_ok(), "{api:?}");
    }
    Ok(())
}

struct ChildGuard(Child);

impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

#[test]
fn killed_process_releases_its_lock_without_cleanup() -> TestResult {
    for &api in APIS {
        let directory = tempfile::tempdir()?;
        let path = directory.path().join("owner.lock");
        let ready = directory.path().join("ready");
        let mut child = ChildGuard(
            Command::new(std::env::current_exe()?)
                .args([
                    "--exact",
                    "tests::child_holds_lock",
                    "--ignored",
                    "--nocapture",
                ])
                .env("CODEX_FILE_LOCK_TEST_PATH", &path)
                .env("CODEX_FILE_LOCK_TEST_BACKEND", api.name)
                .stdin(Stdio::piped())
                .stdout(Stdio::null())
                .spawn()?,
        );
        let deadline = Instant::now() + Duration::from_secs(5);
        while !ready.exists() {
            assert!(
                child.0.try_wait()?.is_none(),
                "lock holder exited early: {api:?}"
            );
            assert!(
                Instant::now() < deadline,
                "lock holder did not become ready: {api:?}"
            );
            thread::sleep(Duration::from_millis(10));
        }
        let contender = OpenOptions::new().read(true).write(true).open(&path)?;
        // The parent always uses the public API, also checking interoperability
        // between Rust's Linux locks and the Android implementation.
        assert!(
            matches!(super::try_lock(&contender), Err(TryLockError::WouldBlock)),
            "{api:?}"
        );
        child.0.kill()?;
        assert_eq!(child.0.wait()?.signal(), Some(libc::SIGKILL));
        super::try_lock(&contender)?;
    }
    Ok(())
}

#[test]
#[ignore = "spawned by killed_process_releases_its_lock_without_cleanup"]
fn child_holds_lock() -> TestResult {
    let path = std::path::PathBuf::from(std::env::var("CODEX_FILE_LOCK_TEST_PATH")?);
    let api = match std::env::var("CODEX_FILE_LOCK_TEST_BACKEND")?.as_str() {
        "public" => PUBLIC,
        "android" => ANDROID,
        name => return Err(format!("unknown lock implementation: {name}").into()),
    };
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(&path)?;
    (api.lock)(&file)?;
    std::fs::write(path.with_file_name("ready"), b"locked")?;
    let mut input = [0];
    io::stdin().read_exact(&mut input)?;
    Ok(())
}
