use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Deserialize)]
pub struct Request {
    pub op: String,
    #[serde(default)]
    pub transaction_id: String,
    #[serde(default)]
    pub config_b64: String,
    #[serde(default)]
    pub config_sha256: String,
    #[serde(default)]
    pub providers: Vec<ProviderFile>,
    #[serde(default)]
    pub runtime_mode: String,
    #[serde(default)]
    pub fast_toggle_ready: bool,
    #[serde(default)]
    pub system_proxy_bypass_domains: Vec<String>,
    #[serde(default)]
    pub enabled: bool,
    #[serde(default)]
    pub provider_name: String,
}

#[derive(Debug, Deserialize)]
pub struct ProviderFile {
    pub name: String,
    pub content_b64: String,
    pub sha256: String,
    #[serde(default)]
    pub modified_at: u64,
}

#[derive(Debug, Serialize)]
pub struct Response {
    pub ok: bool,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data: Option<Value>,
}

impl Response {
    pub fn success(message: impl Into<String>, data: Value) -> Self {
        Self {
            ok: true,
            message: message.into(),
            data: Some(data),
        }
    }

    pub fn error(message: impl Into<String>) -> Self {
        Self {
            ok: false,
            message: message.into(),
            data: None,
        }
    }
}
