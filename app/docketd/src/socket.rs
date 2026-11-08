//! Creating the listening socket safely: private directory, no stale file,
//! mode 0600, and never a second daemon on the same path.

use std::fs;
use std::io;
use std::os::unix::fs::{DirBuilderExt, FileTypeExt, MetadataExt, PermissionsExt};
use std::os::unix::net::UnixStream;
use std::path::Path;
use tokio::net::UnixListener;

fn current_uid() -> io::Result<u32> {
    Ok(fs::metadata("/proc/self")?.uid())
}

/// Create `dir` with mode 0700 (and parents as needed). If it exists it must be
/// a real directory (not a symlink) owned by this user; its mode is tightened.
pub fn ensure_private_dir(dir: &Path) -> io::Result<()> {
    if let Some(parent) = dir.parent() {
        if !parent.as_os_str().is_empty() && !parent.exists() {
            fs::DirBuilder::new()
                .recursive(true)
                .mode(0o700)
                .create(parent)?;
        }
    }
    match fs::DirBuilder::new().mode(0o700).create(dir) {
        Ok(()) => {}
        Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {}
        Err(e) => return Err(e),
    }
    let meta = fs::symlink_metadata(dir)?;
    if !meta.is_dir() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("{} is not a directory", dir.display()),
        ));
    }
    if meta.uid() != current_uid()? {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            format!("{} is owned by another user", dir.display()),
        ));
    }
    if meta.mode() & 0o777 != 0o700 {
        fs::set_permissions(dir, fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

/// Remove a leftover socket file, but refuse if a daemon still answers on it
/// or the path is not a socket.
pub fn clean_stale(path: &Path) -> io::Result<()> {
    let meta = match fs::symlink_metadata(path) {
        Ok(m) => m,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(e),
    };
    if !meta.file_type().is_socket() {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            format!("{} exists and is not a socket", path.display()),
        ));
    }
    match UnixStream::connect(path) {
        Ok(_) => Err(io::Error::new(
            io::ErrorKind::AddrInUse,
            "another docketd is already running",
        )),
        Err(_) => fs::remove_file(path),
    }
}

/// Bind the listener. `default_location` applies the strict private-directory
/// rule to the socket's parent; otherwise the parent is only created if absent.
pub fn bind(path: &Path, default_location: bool) -> io::Result<UnixListener> {
    let parent = path
        .parent()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "socket path has no parent"))?;
    if default_location {
        ensure_private_dir(parent)?;
    } else if !parent.exists() {
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(parent)?;
    }
    clean_stale(path)?;
    let listener = UnixListener::bind(path)?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    Ok(listener)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn bind_sets_modes_and_replaces_stale_socket() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join("rt").join("docket");
        let sock = dir.join("docketd.sock");
        // loosen to prove tightening
        fs::create_dir_all(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o755)).unwrap();
        let l = bind(&sock, true).unwrap();
        assert_eq!(fs::metadata(&dir).unwrap().mode() & 0o777, 0o700);
        assert_eq!(fs::metadata(&sock).unwrap().mode() & 0o777, 0o600);
        // a live daemon blocks a second bind
        assert_eq!(
            bind(&sock, true).unwrap_err().kind(),
            io::ErrorKind::AddrInUse
        );
        // stale file (listener gone) is cleaned
        drop(l);
        let l2 = bind(&sock, true).unwrap();
        drop(l2);
    }

    #[test]
    fn refuses_non_socket_and_symlinked_dir() {
        let tmp = tempfile::tempdir().unwrap();
        let file = tmp.path().join("x.sock");
        fs::write(&file, b"hi").unwrap();
        assert!(clean_stale(&file).is_err());
        assert!(file.exists(), "must not delete a regular file");
        let real = tmp.path().join("real");
        fs::create_dir(&real).unwrap();
        let link = tmp.path().join("link");
        std::os::unix::fs::symlink(&real, &link).unwrap();
        assert!(ensure_private_dir(&link).is_err());
    }
}
