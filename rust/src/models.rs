use std::collections::HashSet;
use std::path::PathBuf;
use serde::{Deserialize, Serialize};

pub const VERSION: &str = "3.0.0-rc10.1";
pub const OUTPUT_DIR: &str = ".opsforge";
pub const MAX_VISITED_FILES: usize = 3000;
pub const MAX_PYTHON_FILE_BYTES: u64 = 2 * 1024 * 1024; // 2 MB

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Mode {
    Auto,
    Base,
    Web,
    Mlops,
    Llmops,
    Genai,
    Batch,
    Interactive,
}

impl Mode {
    pub fn as_str(&self) -> &'static str {
        match self {
            Mode::Auto => "auto",
            Mode::Base => "base",
            Mode::Web => "web",
            Mode::Mlops => "mlops",
            Mode::Llmops => "llmops",
            Mode::Genai => "genai",
            Mode::Batch => "batch",
            Mode::Interactive => "interactive",
        }
    }

    pub fn from_str_loose(s: &str) -> Self {
        match s.to_lowercase().as_str() {
            "web" => Mode::Web,
            "mlops" => Mode::Mlops,
            "llmops" => Mode::Llmops,
            "genai" => Mode::Genai,
            "batch" => Mode::Batch,
            "interactive" => Mode::Interactive,
            "base" => Mode::Base,
            _ => Mode::Auto,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ExecutionKind {
    Web,
    Batch,
    Interactive,
}

impl ExecutionKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            ExecutionKind::Web => "web",
            ExecutionKind::Batch => "batch",
            ExecutionKind::Interactive => "interactive",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum StorageVerdict {
    Sufficient,
    Warning,
    Blocked,
    Unknown,
}

impl StorageVerdict {
    pub fn as_str(&self) -> &'static str {
        match self {
            StorageVerdict::Sufficient => "SUFFICIENT",
            StorageVerdict::Warning => "WARNING",
            StorageVerdict::Blocked => "BLOCKED",
            StorageVerdict::Unknown => "UNKNOWN",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Candidate {
    pub path: PathBuf,
    pub rel_path: String,
    pub score: u32,
    pub framework: String,
    pub imports: HashSet<String>,
    pub required_imports: HashSet<String>,
    pub optional_imports: HashSet<String>,
    pub needs_stdin: bool,
    pub has_app_object: bool,
    pub app_object_name: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Inspection {
    pub root: PathBuf,
    pub entry: Candidate,
    pub dependency: Option<String>,
    pub detected_mode: Mode,
    pub candidates: Vec<Candidate>,
    pub python_count: usize,
    pub warnings: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PublishedPort {
    pub service: String,
    pub container: u16,
    pub host: u16,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DockerStorageStatus {
    pub engine_accessible: bool,
    pub docker_root_dir: Option<String>,
    pub storage_driver: Option<String>,
    pub daemon_free_bytes: Option<u64>,
    pub daemon_total_bytes: Option<u64>,
    pub reclaimable_build_cache_bytes: u64,
    pub reclaimable_images_bytes: u64,
    pub is_remote_context: bool,
    pub is_vm_isolated: bool,
    pub assessment_status: String, // "CONFIRMED", "UNKNOWN", "BLOCKED"
    pub details: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResourceBudget {
    pub base_image: String,
    pub base_image_bytes_est: u64,
    pub packages_bytes_min: u64,
    pub packages_bytes_max: u64,
    pub build_headroom_bytes: u64,
    pub context_bytes: u64,
    pub total_bytes_min: u64,
    pub total_bytes_max: u64,
    pub confidence: String, // "HIGH", "MEDIUM", "LOW"
    pub verdict: StorageVerdict,
    pub heavy_packages: Vec<String>,
    pub optional_candidates: Vec<String>,
    pub explanations: Vec<String>,
    pub recommendations: Vec<String>,
}

impl ResourceBudget {
    pub fn min_gib(&self) -> f64 {
        self.total_bytes_min as f64 / (1024.0 * 1024.0 * 1024.0)
    }

    pub fn max_gib(&self) -> f64 {
        self.total_bytes_max as f64 / (1024.0 * 1024.0 * 1024.0)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Plan {
    pub inspection: Inspection,
    pub mode: Mode,
    pub port: Option<u16>,
    pub gpu: bool,
    pub command: Vec<String>,
    pub services: Vec<String>,
    pub warnings: Vec<String>,
    pub published_ports: Vec<PublishedPort>,
    pub inferred_packages: Vec<String>,
    pub execution_kind: ExecutionKind,
    pub health_path: Option<String>,
    pub python_image: String,
    pub persistent_dirs: Vec<String>,
    pub resource_budget: Option<ResourceBudget>,
}

#[derive(Debug, Clone)]
pub struct Settings {
    pub target_root: PathBuf,
    pub explicit_entry: Option<String>,
    pub mode: Mode,
    pub port: Option<u16>,
    pub gpu: bool,
    pub dependency_mode: String,
    pub docker_context: Option<String>,
    pub yes: bool,
}

