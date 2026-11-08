//! Minimal blocking HTTP/1.1 client over a Unix domain socket.
//!
//! Enough for `docketd`'s local API: sends `Connection: close`, understands
//! `Content-Length`, chunked and read-until-close bodies, and exposes the body
//! as a `Read` so server-sent events can be consumed as they arrive.

use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::Duration;

pub struct HttpResponse {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Body,
}

impl HttpResponse {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }

    /// Read the whole body as text.
    pub fn text(mut self) -> io::Result<String> {
        let mut s = String::new();
        self.body.read_to_string(&mut s)?;
        Ok(s)
    }
}

enum Mode {
    Length(u64),
    Chunked { left: u64, done: bool },
    UntilClose,
}

pub struct Body {
    inner: BufReader<UnixStream>,
    mode: Mode,
}

impl Read for Body {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        match &mut self.mode {
            Mode::Length(left) => {
                if *left == 0 {
                    return Ok(0);
                }
                let max = (*left).min(buf.len() as u64) as usize;
                let n = self.inner.read(&mut buf[..max])?;
                *left -= n as u64;
                Ok(n)
            }
            Mode::UntilClose => self.inner.read(buf),
            Mode::Chunked { left, done } => {
                if *done {
                    return Ok(0);
                }
                if *left == 0 {
                    let mut line = String::new();
                    self.inner.read_line(&mut line)?;
                    let size = line.trim().split(';').next().unwrap_or("");
                    let size = u64::from_str_radix(size, 16).map_err(|_| {
                        io::Error::new(io::ErrorKind::InvalidData, "bad chunk size")
                    })?;
                    if size == 0 {
                        *done = true;
                        return Ok(0);
                    }
                    *left = size;
                }
                let max = (*left).min(buf.len() as u64) as usize;
                let n = self.inner.read(&mut buf[..max])?;
                if n == 0 {
                    return Err(io::ErrorKind::UnexpectedEof.into());
                }
                *left -= n as u64;
                if *left == 0 {
                    let mut crlf = String::new();
                    self.inner.read_line(&mut crlf)?;
                }
                Ok(n)
            }
        }
    }
}

/// Send one request and return once the response headers have been read.
pub fn request(
    socket: &Path,
    method: &str,
    path: &str,
    headers: &[(&str, &str)],
    body: Option<&[u8]>,
    read_timeout: Option<Duration>,
) -> io::Result<HttpResponse> {
    let mut stream = UnixStream::connect(socket)?;
    stream.set_read_timeout(read_timeout)?;
    stream.set_write_timeout(Some(Duration::from_secs(10)))?;
    let mut head = format!("{method} {path} HTTP/1.1\r\nHost: docketd\r\nConnection: close\r\n");
    for (k, v) in headers {
        head.push_str(&format!("{k}: {v}\r\n"));
    }
    if let Some(b) = body {
        head.push_str(&format!("Content-Length: {}\r\n", b.len()));
    }
    head.push_str("\r\n");
    stream.write_all(head.as_bytes())?;
    if let Some(b) = body {
        stream.write_all(b)?;
    }
    stream.flush()?;

    let mut reader = BufReader::new(stream);
    let mut status_line = String::new();
    reader.read_line(&mut status_line)?;
    let status = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse::<u16>().ok())
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "bad status line"))?;
    let mut headers = Vec::new();
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line)? == 0 {
            return Err(io::ErrorKind::UnexpectedEof.into());
        }
        let line = line.trim_end();
        if line.is_empty() {
            break;
        }
        if let Some((k, v)) = line.split_once(':') {
            headers.push((k.trim().to_string(), v.trim().to_string()));
        }
    }
    let find = |n: &str| {
        headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(n))
            .map(|(_, v)| v.clone())
    };
    let mode = if find("transfer-encoding").is_some_and(|v| v.eq_ignore_ascii_case("chunked")) {
        Mode::Chunked {
            left: 0,
            done: false,
        }
    } else if let Some(n) = find("content-length").and_then(|v| v.parse().ok()) {
        Mode::Length(n)
    } else {
        Mode::UntilClose
    };
    Ok(HttpResponse {
        status,
        headers,
        body: Body {
            inner: reader,
            mode,
        },
    })
}

pub fn get(socket: &Path, path: &str, timeout: Duration) -> io::Result<HttpResponse> {
    request(socket, "GET", path, &[], None, Some(timeout))
}

pub fn post_json(
    socket: &Path,
    path: &str,
    json: &str,
    accept: Option<&str>,
    timeout: Duration,
) -> io::Result<HttpResponse> {
    let mut h = vec![("Content-Type", "application/json")];
    if let Some(a) = accept {
        h.push(("Accept", a));
    }
    request(
        socket,
        "POST",
        path,
        &h,
        Some(json.as_bytes()),
        Some(timeout),
    )
}

/// One server-sent event.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SseEvent {
    pub event: String,
    pub data: String,
}

/// Iterate SSE events from a response body until it ends.
pub fn sse_events<R: Read>(body: R) -> impl Iterator<Item = io::Result<SseEvent>> {
    let mut lines = BufReader::new(body).lines();
    let mut event = String::new();
    let mut data = String::new();
    std::iter::from_fn(move || loop {
        match lines.next() {
            None => return None,
            Some(Err(e)) => return Some(Err(e)),
            Some(Ok(line)) => {
                if line.is_empty() {
                    if !data.is_empty() || !event.is_empty() {
                        let ev = SseEvent {
                            event: if event.is_empty() {
                                "message".into()
                            } else {
                                std::mem::take(&mut event)
                            },
                            data: std::mem::take(&mut data),
                        };
                        return Some(Ok(ev));
                    }
                } else if let Some(v) = line.strip_prefix("event:") {
                    event = v.trim().to_string();
                } else if let Some(v) = line.strip_prefix("data:") {
                    if !data.is_empty() {
                        data.push('\n');
                    }
                    data.push_str(v.strip_prefix(' ').unwrap_or(v));
                }
            }
        }
    })
}

/// `$XDG_RUNTIME_DIR/docket/docketd.sock`, the daemon's default socket.
pub fn default_socket_path() -> Result<PathBuf, String> {
    let dir = std::env::var_os("XDG_RUNTIME_DIR")
        .filter(|v| !v.is_empty())
        .ok_or("XDG_RUNTIME_DIR is not set; set DOCKET_SOCKET to choose a socket path")?;
    Ok(PathBuf::from(dir).join("docket").join("docketd.sock"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sse_parser_groups_events() {
        let raw = "event: phase\ndata: {\"phase\":\"reading\"}\n\n: keepalive\n\nevent: result\ndata: a\ndata: b\n\n";
        let evs: Vec<_> = sse_events(raw.as_bytes()).map(Result::unwrap).collect();
        assert_eq!(evs.len(), 2);
        assert_eq!(evs[0].event, "phase");
        assert_eq!(evs[1].data, "a\nb");
    }
}
