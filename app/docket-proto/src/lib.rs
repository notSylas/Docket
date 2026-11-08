//! Wire types shared by `docketd` and `docket-app`.
//!
//! Two protocols live here:
//! * `rpc`: JSON-RPC 2.0, one JSON object per line, between `docketd` and the
//!   Python worker (`docket worker`).
//! * `api`: the local HTTP/JSON API served by `docketd` on a Unix socket.
//!
//! `client` is a tiny blocking HTTP-over-Unix-socket client used by the GUI
//! and by tests, so neither needs an HTTP client dependency.

pub mod api;
pub mod client;
pub mod rpc;
