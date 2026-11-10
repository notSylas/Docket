//! JSON-RPC 2.0 framing for the worker (line-delimited).

use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const VERSION: &str = "2.0";

pub const PARSE_ERROR: i64 = -32700;
pub const INVALID_REQUEST: i64 = -32600;
pub const METHOD_NOT_FOUND: i64 = -32601;
pub const INVALID_PARAMS: i64 = -32602;
pub const SERVER_ERROR: i64 = -32000;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Request {
    pub jsonrpc: String,
    pub id: u64,
    pub method: String,
    #[serde(default, skip_serializing_if = "Value::is_null")]
    pub params: Value,
}

impl Request {
    pub fn new(id: u64, method: &str, params: Value) -> Self {
        Self {
            jsonrpc: VERSION.into(),
            id,
            method: method.into(),
            params,
        }
    }

    /// The request as one newline-terminated line.
    pub fn to_line(&self) -> String {
        // Serializing plain data cannot fail.
        let mut s = serde_json::to_string(self).unwrap_or_default();
        s.push('\n');
        s
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RpcError {
    pub code: i64,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub data: Option<Value>,
}

impl RpcError {
    pub fn kind(&self) -> Option<&str> {
        self.data.as_ref()?.get("kind")?.as_str()
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Response {
    pub jsonrpc: String,
    pub id: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<RpcError>,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Incoming {
    Response(Response),
    Notification { method: String, params: Value },
}

/// Why a worker output line is not protocol.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Noise {
    Empty,
    NotJson,
    NotProtocol,
}

/// Classify one line from the worker's stdout.
pub fn parse_line(line: &str) -> Result<Incoming, Noise> {
    let line = line.trim();
    if line.is_empty() {
        return Err(Noise::Empty);
    }
    let v: Value = serde_json::from_str(line).map_err(|_| Noise::NotJson)?;
    let obj = v.as_object().ok_or(Noise::NotProtocol)?;
    if obj.get("jsonrpc").and_then(Value::as_str) != Some(VERSION) {
        return Err(Noise::NotProtocol);
    }
    if obj.contains_key("result") || obj.contains_key("error") {
        return serde_json::from_value(v)
            .map(Incoming::Response)
            .map_err(|_| Noise::NotProtocol);
    }
    match obj.get("method").and_then(Value::as_str) {
        Some(m) if !obj.contains_key("id") => Ok(Incoming::Notification {
            method: m.to_string(),
            params: obj.get("params").cloned().unwrap_or(Value::Null),
        }),
        _ => Err(Noise::NotProtocol),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn request_round_trip() {
        let r = Request::new(7, "query.ask", json!({"question": "q"}));
        let line = r.to_line();
        assert!(line.ends_with('\n') && line.matches('\n').count() == 1);
        let back: Request = serde_json::from_str(&line).unwrap();
        assert_eq!(back, r);
    }

    #[test]
    fn request_without_params_omits_field() {
        let line = Request::new(1, "ping", Value::Null).to_line();
        assert!(!line.contains("params"));
    }

    #[test]
    fn classifies_lines() {
        assert!(matches!(
            parse_line(r#"{"jsonrpc":"2.0","id":1,"result":{"pong":true}}"#),
            Ok(Incoming::Response(_))
        ));
        let e = parse_line(
            r#"{"jsonrpc":"2.0","id":2,"error":{"code":-32000,"message":"x","data":{"kind":"K"}}}"#,
        );
        match e {
            Ok(Incoming::Response(r)) => assert_eq!(r.error.unwrap().kind(), Some("K")),
            other => panic!("{other:?}"),
        }
        assert!(matches!(
            parse_line(r#"{"jsonrpc":"2.0","method":"progress","params":{"phase":"reading"}}"#),
            Ok(Incoming::Notification { .. })
        ));
        assert_eq!(parse_line(""), Err(Noise::Empty));
        assert_eq!(parse_line("loading model..."), Err(Noise::NotJson));
        assert_eq!(parse_line("{\"hello\":1}"), Err(Noise::NotProtocol));
        assert_eq!(parse_line("[1,2]"), Err(Noise::NotProtocol));
        assert_eq!(
            parse_line("{\"jsonrpc\":\"2.0\",\"id\":1"),
            Err(Noise::NotJson)
        );
    }
}
