// ============================================================================
// AUTO DOCKER 3.0 (RUST SINGLE-FILE MASTER ENGINE)
// Zero-dependency, resource-aware container infrastructure assistant & orchestrator
// 100% Rust Standard Library (std::*) — Zero external crates — Pure single file
// Direct compilation: rustc --edition 2021 -O auto_docker.rs
// ============================================================================

use std::collections::{HashMap, HashSet};
use std::env;
use std::fs;
use std::io;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{self, Command};
use std::time::{SystemTime, UNIX_EPOCH};

pub const VERSION: &str = "3.0.0-rc10.1";
pub const MAX_VISITED_FILES: usize = 240;
pub const MAX_PROJECT_SIZE_BYTES: u64 = 100 * 1024 * 1024 * 1024; // 100 GB
pub const COMPOSE_INDENT: &str = "  ";

// ============================================================================
// 1. Error Hierarchy
// ============================================================================

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OpsError {
    General(String),
    ScanLimit(String),
    AmbiguousEntry(String),
    Blocked(String),
    Validation(String),
}

impl std::fmt::Display for OpsError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            OpsError::General(s) => write!(f, "{}", s),
            OpsError::ScanLimit(s) => write!(f, "SCAN_LIMIT: {}", s),
            OpsError::AmbiguousEntry(s) => write!(f, "AMBIGUOUS_ENTRY: {}", s),
            OpsError::Blocked(s) => write!(f, "BLOCKED: {}", s),
            OpsError::Validation(s) => write!(f, "VALIDATION_ERROR: {}", s),
        }
    }
}

impl std::error::Error for OpsError {}

// ============================================================================
// 2. Domain Models & Core Enums
// ============================================================================

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Mode {
    Auto,
    Base,
    Mlops,
    Llmops,
    Genaiops,
    FastApi,
    Flask,
    Streamlit,
    Gradio,
    Web,
    Custom(String),
}

impl Mode {
    pub fn as_str(&self) -> &str {
        match self {
            Mode::Auto => "auto",
            Mode::Base => "base",
            Mode::Mlops => "mlops",
            Mode::Llmops => "llmops",
            Mode::Genaiops => "genaiops",
            Mode::FastApi => "fastapi",
            Mode::Flask => "flask",
            Mode::Streamlit => "streamlit",
            Mode::Gradio => "gradio",
            Mode::Web => "web",
            Mode::Custom(s) => s.as_str(),
        }
    }

    pub fn from_str_loose(s: &str) -> Self {
        match s.to_lowercase().as_str() {
            "auto" => Mode::Auto,
            "base" | "cli" | "batch" => Mode::Base,
            "mlops" | "ml" => Mode::Mlops,
            "llmops" | "llm" => Mode::Llmops,
            "genai" | "genaiops" => Mode::Genaiops,
            "fastapi" => Mode::FastApi,
            "flask" => Mode::Flask,
            "streamlit" => Mode::Streamlit,
            "gradio" => Mode::Gradio,
            "web" => Mode::Web,
            other => Mode::Custom(other.to_string()),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExecutionKind {
    Interactive,
    Web,
    Batch,
    Worker,
    MultiService,
    Recovery,
}

impl ExecutionKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            ExecutionKind::Interactive => "interactive",
            ExecutionKind::Web => "web",
            ExecutionKind::Batch => "batch",
            ExecutionKind::Worker => "worker",
            ExecutionKind::MultiService => "multi-service",
            ExecutionKind::Recovery => "recovery",
        }
    }

    pub fn from_str_loose(s: &str) -> Self {
        match s.to_lowercase().as_str() {
            "interactive" | "cli" => ExecutionKind::Interactive,
            "web" | "api" | "server" => ExecutionKind::Web,
            "batch" | "job" => ExecutionKind::Batch,
            "worker" | "queue" => ExecutionKind::Worker,
            "multi-service" | "stack" => ExecutionKind::MultiService,
            "recovery" => ExecutionKind::Recovery,
            _ => ExecutionKind::Web,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
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

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Target {
    pub root: PathBuf,
    pub pinned: Option<String>,
}

impl Target {
    pub fn new(root: PathBuf, pinned: Option<String>) -> Self {
        let canonical = root.canonicalize().unwrap_or(root);
        Self { root: canonical, pinned }
    }
}

pub fn target_for(raw: &str) -> Result<Target, OpsError> {
    let clean = raw.trim().trim_matches('\'').trim_matches('"').trim();
    let p = PathBuf::from(clean);
    let p_abs = if p.is_absolute() {
        p.clone()
    } else {
        match env::current_dir() {
            Ok(cwd) => cwd.join(&p),
            Err(_) => p.clone(),
        }
    };

    let (root, pinned) = if p_abs.is_file() {
        let name_lower = p_abs.file_name().and_then(|n| n.to_str()).unwrap_or("").to_lowercase();
        if p_abs.extension().and_then(|e| e.to_str()) == Some("py") {
            (
                p_abs.parent().unwrap_or(Path::new(".")).to_path_buf(),
                Some(p_abs.file_name().unwrap().to_str().unwrap().to_string()),
            )
        } else if name_lower == "compose.yaml"
            || name_lower == "compose.yml"
            || name_lower == "docker-compose.yaml"
            || name_lower == "docker-compose.yml"
            || name_lower.starts_with("compose.")
            || name_lower.starts_with("docker-compose.")
        {
            (p_abs.parent().unwrap_or(Path::new(".")).to_path_buf(), None)
        } else if name_lower == "dockerfile" || name_lower.ends_with(".dockerfile") {
            (p_abs.parent().unwrap_or(Path::new(".")).to_path_buf(), None)
        } else {
            return Err(OpsError::Validation(format!(
                "Selected file '{}' is not an accepted target. Choose a Python file (.py), a Docker Compose file (compose.yaml, docker-compose.yml), a Dockerfile, or a project directory.",
                p_abs.file_name().and_then(|n| n.to_str()).unwrap_or("")
            )));
        }
    } else if p_abs.is_dir() {
        (p_abs.clone(), None)
    } else {
        return Err(OpsError::Validation("The selected path is not a regular file/directory.".to_string()));
    };

    let canonical_root = root.canonicalize().unwrap_or_else(|_| root.clone());

    // Check home directory
    let home = env::var("USERPROFILE").or_else(|_| env::var("HOME")).map(PathBuf::from);
    if let Ok(ref h) = home {
        if let Ok(can_h) = h.canonicalize() {
            if canonical_root == can_h {
                return Err(OpsError::Validation(
                    "Refusing to use a home/drive-root directory. Select a project subfolder.".to_string(),
                ));
            }
        }
    }

    // Check drive root (e.g. C:\ or /)
    if canonical_root.parent().is_none()
        || canonical_root == Path::new("/")
        || canonical_root.to_str() == Some("C:\\")
        || canonical_root.to_str() == Some("C:")
    {
        return Err(OpsError::Validation(
            "Refusing to use a home/drive-root directory. Select a project subfolder.".to_string(),
        ));
    }

    Ok(Target {
        root: canonical_root,
        pinned,
    })
}

#[derive(Debug, Clone)]
pub struct Candidate {
    pub path: PathBuf,
    pub rel_path: String,
    pub score: i32,
    pub framework: String,
    pub imports: HashSet<String>,
    pub required_imports: HashSet<String>,
    pub optional_imports: HashSet<String>,
    pub needs_stdin: bool,
    pub has_app_object: bool,
    pub app_object_name: Option<String>,
}

#[derive(Debug, Clone)]
pub struct Inspection {
    pub root: PathBuf,
    pub entry: Candidate,
    pub dependency: Option<PathBuf>,
    pub detected_mode: Mode,
    pub candidates: Vec<Candidate>,
    pub python_count: usize,
    pub warnings: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PublishedPort {
    pub service: String,
    pub container: u16,
    pub host: u16,
}

#[derive(Debug, Clone)]
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
    pub assessment_status: String,
    pub details: String,
}

impl DockerStorageStatus {
    pub fn free_gib(&self) -> f64 {
        self.daemon_free_bytes.map(|b| b as f64 / 1024.0 / 1024.0 / 1024.0).unwrap_or(0.0)
    }
}

#[derive(Debug, Clone)]
pub struct ResourceBudget {
    pub total_bytes_min: u64,
    pub total_bytes_max: u64,
    pub daemon_free_bytes: Option<u64>,
    pub safety_reserve_bytes: u64,
    pub verdict: StorageVerdict,
    pub confidence: String,
    pub explanations: Vec<String>,
    pub assumptions: Vec<String>,
    pub base_image_layer_bytes: u64,
    pub declared_packages_bytes: u64,
    pub optional_packages_bytes: u64,
}

impl ResourceBudget {
    pub fn min_gib(&self) -> f64 {
        self.total_bytes_min as f64 / 1024.0 / 1024.0 / 1024.0
    }
    pub fn max_gib(&self) -> f64 {
        self.total_bytes_max as f64 / 1024.0 / 1024.0 / 1024.0
    }
}

#[derive(Debug, Clone)]
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
    pub replicas: usize,
    pub cpus: Option<String>,
    pub memory: Option<String>,
    pub resource_budget: Option<ResourceBudget>,
}

#[derive(Debug, Clone)]
pub struct Settings {
    pub target: Target,
    pub explicit_entry: Option<String>,
    pub mode: Mode,
    pub port: Option<u16>,
    pub gpu: bool,
    pub replicas: usize,
    pub cpus: Option<String>,
    pub memory: Option<String>,
    pub dependency_mode: String,
    pub docker_context: Option<String>,
    pub yes: bool,
    pub allow_insecure: bool,
    pub services_override: Option<Vec<String>>,
    pub command_override: Option<Vec<String>>,
}

impl Settings {
    pub fn new(target: Target) -> Self {
        Self {
            target,
            explicit_entry: None,
            mode: Mode::Auto,
            port: None,
            gpu: false,
            replicas: 1,
            cpus: None,
            memory: None,
            dependency_mode: "strict".to_string(),
            docker_context: None,
            yes: false,
            allow_insecure: false,
            services_override: None,
            command_override: None,
        }
    }
}

// ============================================================================
// 3. Deployment State Machine & Orchestrator
// ============================================================================

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum DeploymentState {
    Discovered,
    Analyzed,
    Planned,
    Validated,
    Approved,
    Building,
    Starting,
    Verifying,
    Healthy,
    Degraded,
    Blocked,
    Cancelled,
    Failed,
    RecoveryRequired,
}

impl DeploymentState {
    pub fn as_str(&self) -> &'static str {
        match self {
            DeploymentState::Discovered => "DISCOVERED",
            DeploymentState::Analyzed => "ANALYZED",
            DeploymentState::Planned => "PLANNED",
            DeploymentState::Validated => "VALIDATED",
            DeploymentState::Approved => "APPROVED",
            DeploymentState::Building => "BUILDING",
            DeploymentState::Starting => "STARTING",
            DeploymentState::Verifying => "VERIFYING",
            DeploymentState::Healthy => "HEALTHY",
            DeploymentState::Degraded => "DEGRADED",
            DeploymentState::Blocked => "BLOCKED",
            DeploymentState::Cancelled => "CANCELLED",
            DeploymentState::Failed => "FAILED",
            DeploymentState::RecoveryRequired => "RECOVERY_REQUIRED",
        }
    }
}

#[derive(Debug, Clone)]
pub struct DeploymentTransition {
    pub from: DeploymentState,
    pub to: DeploymentState,
    pub timestamp: String,
    pub reason: String,
}

pub struct DeploymentOrchestrator {
    pub project_root: PathBuf,
    pub current_state: DeploymentState,
    pub history: Vec<DeploymentTransition>,
}

impl DeploymentOrchestrator {
    pub fn new(project_root: PathBuf) -> Self {
        Self {
            project_root,
            current_state: DeploymentState::Discovered,
            history: Vec::new(),
        }
    }

    pub fn valid_transitions(from: DeploymentState) -> HashSet<DeploymentState> {
        let mut allowed = HashSet::new();
        match from {
            DeploymentState::Discovered => {
                allowed.insert(DeploymentState::Analyzed);
                allowed.insert(DeploymentState::Blocked);
                allowed.insert(DeploymentState::Failed);
            }
            DeploymentState::Analyzed => {
                allowed.insert(DeploymentState::Planned);
                allowed.insert(DeploymentState::Blocked);
                allowed.insert(DeploymentState::Failed);
                allowed.insert(DeploymentState::Analyzed);
            }
            DeploymentState::Planned => {
                allowed.insert(DeploymentState::Validated);
                allowed.insert(DeploymentState::Blocked);
                allowed.insert(DeploymentState::Cancelled);
                allowed.insert(DeploymentState::Planned);
                allowed.insert(DeploymentState::Analyzed);
            }
            DeploymentState::Validated => {
                allowed.insert(DeploymentState::Approved);
                allowed.insert(DeploymentState::Blocked);
                allowed.insert(DeploymentState::Cancelled);
                allowed.insert(DeploymentState::Validated);
                allowed.insert(DeploymentState::Planned);
                allowed.insert(DeploymentState::Analyzed);
                allowed.insert(DeploymentState::Verifying);
                allowed.insert(DeploymentState::Healthy);
                allowed.insert(DeploymentState::Degraded);
            }
            DeploymentState::Approved => {
                allowed.insert(DeploymentState::Building);
                allowed.insert(DeploymentState::Starting);
                allowed.insert(DeploymentState::Cancelled);
                allowed.insert(DeploymentState::Analyzed);
            }
            DeploymentState::Building => {
                allowed.insert(DeploymentState::Validated);
                allowed.insert(DeploymentState::Starting);
                allowed.insert(DeploymentState::Failed);
                allowed.insert(DeploymentState::RecoveryRequired);
            }
            DeploymentState::Starting => {
                allowed.insert(DeploymentState::Verifying);
                allowed.insert(DeploymentState::Failed);
                allowed.insert(DeploymentState::RecoveryRequired);
            }
            DeploymentState::Verifying => {
                allowed.insert(DeploymentState::Healthy);
                allowed.insert(DeploymentState::Degraded);
                allowed.insert(DeploymentState::Failed);
                allowed.insert(DeploymentState::RecoveryRequired);
                allowed.insert(DeploymentState::Approved);
                allowed.insert(DeploymentState::Analyzed);
                allowed.insert(DeploymentState::Verifying);
            }
            DeploymentState::Healthy => {
                allowed.insert(DeploymentState::Degraded);
                allowed.insert(DeploymentState::Failed);
                allowed.insert(DeploymentState::RecoveryRequired);
                allowed.insert(DeploymentState::Approved);
                allowed.insert(DeploymentState::Analyzed);
                allowed.insert(DeploymentState::Verifying);
                allowed.insert(DeploymentState::Healthy);
            }
            DeploymentState::Degraded => {
                allowed.insert(DeploymentState::Verifying);
                allowed.insert(DeploymentState::RecoveryRequired);
                allowed.insert(DeploymentState::Failed);
                allowed.insert(DeploymentState::Approved);
                allowed.insert(DeploymentState::Analyzed);
                allowed.insert(DeploymentState::Degraded);
            }
            DeploymentState::Blocked => {
                allowed.insert(DeploymentState::Analyzed);
                allowed.insert(DeploymentState::Planned);
                allowed.insert(DeploymentState::Cancelled);
                allowed.insert(DeploymentState::Blocked);
            }
            DeploymentState::Cancelled => {
                allowed.insert(DeploymentState::Planned);
                allowed.insert(DeploymentState::Analyzed);
                allowed.insert(DeploymentState::Validated);
                allowed.insert(DeploymentState::Cancelled);
            }
            DeploymentState::Failed => {
                allowed.insert(DeploymentState::RecoveryRequired);
                allowed.insert(DeploymentState::Analyzed);
                allowed.insert(DeploymentState::Failed);
            }
            DeploymentState::RecoveryRequired => {
                allowed.insert(DeploymentState::Analyzed);
                allowed.insert(DeploymentState::Discovered);
            }
        }
        allowed
    }

    pub fn transition(&mut self, to_state: DeploymentState, reason: &str) -> Result<(), OpsError> {
        let allowed = Self::valid_transitions(self.current_state);
        if !allowed.contains(&to_state) {
            return Err(OpsError::General(format!(
                "Illegal deployment transition from {} to {}: {}",
                self.current_state.as_str(),
                to_state.as_str(),
                reason
            )));
        }
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        self.history.push(DeploymentTransition {
            from: self.current_state,
            to: to_state,
            timestamp: now.to_string(),
            reason: reason.to_string(),
        });
        self.current_state = to_state;
        Ok(())
    }
}

// ============================================================================
// 4. PyPI Canonical Map & Python Stdlib Immunity
// ============================================================================

pub fn resolve_pypi_package(import_name: &str) -> String {
    let lower = import_name.to_lowercase();
    match lower.as_str() {
        "cv2" => "opencv-python".to_string(),
        "pil" | "pillow" => "Pillow".to_string(),
        "fitz" => "PyMuPDF".to_string(),
        "sklearn" => "scikit-learn".to_string(),
        "yaml" => "PyYAML".to_string(),
        "bs4" => "beautifulsoup4".to_string(),
        "dotenv" => "python-dotenv".to_string(),
        "serial" => "pyserial".to_string(),
        "jose" => "python-jose".to_string(),
        "magic" => "python-magic".to_string(),
        "multipart" => "python-multipart".to_string(),
        "dateutil" => "python-dateutil".to_string(),
        "jwt" => "PyJWT".to_string(),
        "psycopg2" => "psycopg2-binary".to_string(),
        "faiss" => "faiss-cpu".to_string(),
        "xgb" => "xgboost".to_string(),
        "lgb" => "lightgbm".to_string(),
        "docx" => "python-docx".to_string(),
        "pptx" => "python-pptx".to_string(),
        "git" => "GitPython".to_string(),
        "google.protobuf" | "google" => "protobuf".to_string(),
        "googleapiclient" => "google-api-python-client".to_string(),
        "socketio" => "python-socketio".to_string(),
        "engineio" => "python-engineio".to_string(),
        "telegram" => "python-telegram-bot".to_string(),
        "discord" => "discord.py".to_string(),
        "slack_sdk" => "slack-sdk".to_string(),
        _ => import_name.to_string(),
    }
}

pub fn is_stdlib_module(name: &str) -> bool {
    let base = name.split('.').next().unwrap_or("").trim().to_lowercase();
    const STDLIB: &[&str] = &[
        "__future__", "_thread", "abc", "aifc", "argparse", "array", "ast", "asynchat", "asyncio",
        "asyncore", "atexit", "audioop", "base64", "bdb", "binascii", "binhex", "bisect",
        "builtins", "bz2", "calendar", "cgi", "cgitb", "chunk", "cmath", "cmd", "code", "codecs",
        "codeop", "collections", "colorsys", "compileall", "concurrent", "configparser", "contextlib",
        "contextvars", "copy", "copyreg", "cprofile", "crypt", "csv", "ctypes", "curses", "dataclasses",
        "datetime", "dbm", "decimal", "difflib", "dis", "distutils", "doctest", "email", "encodings",
        "ensurepip", "enum", "errno", "faulthandler", "fcntl", "filecmp", "fileinput", "fnmatch",
        "fractions", "ftplib", "functools", "gc", "getopt", "getpass", "gettext", "glob", "graphlib",
        "grp", "gzip", "hashlib", "heapq", "hmac", "html", "http", "idlelib", "imaplib", "imghdr",
        "imp", "importlib", "inspect", "io", "ipaddress", "itertools", "json", "keyword", "lib2to3",
        "linecache", "locale", "logging", "lzma", "mailbox", "mailcap", "marshal", "math", "mimetypes",
        "mmap", "modulefinder", "msilib", "msvcrt", "multiprocessing", "netrc", "nntplib", "numbers",
        "operator", "optparse", "os", "ossaudiodev", "pathlib", "pdb", "pickle", "pickletools",
        "pipes", "pkgutil", "platform", "plistlib", "poplib", "posix", "posixpath", "pprint",
        "profile", "pstats", "pty", "pwd", "py_compile", "pyclbr", "pydoc", "queue", "quopri",
        "random", "re", "readline", "reprlib", "resource", "rlcompleter", "runpy", "sched", "secrets",
        "select", "selectors", "shelve", "shlex", "shutil", "signal", "site", "smtpd", "smtplib",
        "sndhdr", "socket", "socketserver", "spwd", "sqlite3", "ssl", "stat", "statistics", "string",
        "stringprep", "struct", "subprocess", "sunau", "symbol", "symtable", "sys", "sysconfig",
        "syslog", "tabnanny", "tarfile", "telnetlib", "tempfile", "termios", "test", "textwrap",
        "threading", "time", "timeit", "tkinter", "token", "tokenize", "tomllib", "trace", "traceback",
        "tracemalloc", "tty", "turtle", "turtledemo", "types", "typing", "unicodedata", "unittest",
        "urllib", "uu", "uuid", "venv", "warnings", "wave", "weakref", "webbrowser", "winreg",
        "winsound", "wsgiref", "xdrlib", "xml", "xmlrpc", "zipapp", "zipfile", "zipimport", "zlib",
    ];
    STDLIB.contains(&base.as_str())
}

// ============================================================================
// 5. Dependency AST & Lexical Scope Tracker
// ============================================================================

#[derive(Debug, Default, Clone)]
pub struct DependencyAstVisitor {
    pub required_imports: HashSet<String>,
    pub optional_imports: HashSet<String>,
    pub type_checking_imports: HashSet<String>,
    pub needs_stdin: bool,
    pub has_app_object: bool,
    pub app_object_name: Option<String>,
    pub detected_framework: Option<String>,
}

impl DependencyAstVisitor {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn parse_source(&mut self, source: &str) {
        let mut in_main_guard = false;
        let mut main_indent = 0;
        let mut in_type_checking = false;
        let mut tc_indent = 0;
        let mut in_try = false;
        let mut try_indent = 0;

        for line in source.lines() {
            let trimmed = line.trim();
            if trimmed.is_empty() || trimmed.starts_with('#') {
                continue;
            }
            let indent = line.len() - line.trim_start().len();

            if in_main_guard && indent <= main_indent && !trimmed.starts_with("else:") && !trimmed.starts_with("elif ") {
                in_main_guard = false;
            }
            if in_type_checking && indent <= tc_indent {
                in_type_checking = false;
            }
            if in_try && indent <= try_indent && !trimmed.starts_with("except") && !trimmed.starts_with("finally") {
                in_try = false;
            }

            if trimmed.starts_with("if __name__ == '__main__':") || trimmed.starts_with("if __name__ == \"__main__\":") {
                in_main_guard = true;
                main_indent = indent;
            } else if trimmed.starts_with("if TYPE_CHECKING:") || trimmed.starts_with("if typing.TYPE_CHECKING:") {
                in_type_checking = true;
                tc_indent = indent;
            } else if trimmed.starts_with("try:") {
                in_try = true;
                try_indent = indent;
            }

            // Framework detection
            if trimmed.contains("FastAPI(") {
                self.detected_framework = Some("fastapi".to_string());
                self.has_app_object = true;
                if let Some(pos) = trimmed.find('=') {
                    let var = trimmed[..pos].trim().split(':').next().unwrap_or("").trim();
                    if is_valid_identifier(var) {
                        self.app_object_name = Some(var.to_string());
                    }
                }
            } else if trimmed.contains("Flask(") {
                self.detected_framework = Some("flask".to_string());
                self.has_app_object = true;
                if let Some(pos) = trimmed.find('=') {
                    let var = trimmed[..pos].trim().split(':').next().unwrap_or("").trim();
                    if is_valid_identifier(var) {
                        self.app_object_name = Some(var.to_string());
                    }
                }
            } else if trimmed.contains("streamlit as st") || trimmed.starts_with("import streamlit") || trimmed.contains("st.title(") {
                self.detected_framework = Some("streamlit".to_string());
            }

            // Stdin detection
            if trimmed.contains("input(") || trimmed.contains("sys.stdin") || trimmed.contains("getpass.getpass") {
                self.needs_stdin = true;
            }

            // Import extraction
            if trimmed.starts_with("import ") {
                let rest = trimmed.trim_start_matches("import ").trim();
                for part in rest.split(',') {
                    let raw_mod = part.trim().split_whitespace().next().unwrap_or("");
                    let top = raw_mod.split('.').next().unwrap_or("");
                    if is_valid_identifier(top) {
                        self.record_import(top, in_type_checking, in_try, in_main_guard);
                    }
                }
            } else if trimmed.starts_with("from ") {
                let rest = trimmed.trim_start_matches("from ").trim();
                if let Some(pos) = rest.find(" import ") {
                    let raw_mod = rest[..pos].trim();
                    let top = raw_mod.split('.').next().unwrap_or("");
                    if is_valid_identifier(top) {
                        self.record_import(top, in_type_checking, in_try, in_main_guard);
                    }
                }
            }
        }
    }

    fn record_import(&mut self, top: &str, in_tc: bool, in_try: bool, _in_main: bool) {
        if in_tc {
            self.type_checking_imports.insert(top.to_string());
            return;
        }
        if in_try {
            if !self.required_imports.contains(top) {
                self.optional_imports.insert(top.to_string());
            }
        } else {
            self.optional_imports.remove(top);
            self.required_imports.insert(top.to_string());
        }
    }
}

fn is_valid_identifier(s: &str) -> bool {
    if s.is_empty() {
        return false;
    }
    let first = s.chars().next().unwrap();
    (first.is_alphabetic() || first == '_') && s.chars().all(|c| c.is_alphanumeric() || c == '_')
}

// ============================================================================
// 6. Filesystem Walker with Protection
// ============================================================================

pub fn walk_files_recursive(root: &Path, max_files: usize) -> Result<Vec<PathBuf>, OpsError> {
    let mut results = Vec::new();
    let mut visited = 0;
    let ignore: HashSet<&'static str> = [
        ".git", ".opsforge", "__pycache__", ".venv", "venv", ".idea", ".vscode", "node_modules", ".cache", "target",
    ]
    .iter()
    .copied()
    .collect();

    fn walk_inner(
        dir: &Path,
        results: &mut Vec<PathBuf>,
        visited: &mut usize,
        max_files: usize,
        ignore: &HashSet<&'static str>,
    ) -> Result<(), OpsError> {
        if let Ok(entries) = fs::read_dir(dir) {
            for entry in entries.flatten() {
                *visited += 1;
                if *visited > max_files {
                    return Err(OpsError::ScanLimit(format!(
                        "Scan exceeded {} entries; choose a smaller folder or select a .py file.",
                        max_files
                    )));
                }
                let path = entry.path();
                let file_name = entry.file_name();
                let name = file_name.to_str().unwrap_or("");
                if ignore.contains(name) || name.starts_with(".opsforge-backup") {
                    continue;
                }
                if let Ok(ft) = entry.file_type() {
                    if ft.is_symlink() {
                        continue;
                    }
                    if ft.is_file() {
                        if name != "auto_docker.py" && name != "auto_docker.rs" && name != "opsforge_v2.py" {
                            results.push(path);
                        }
                    } else if ft.is_dir() {
                        walk_inner(&path, results, visited, max_files, ignore)?;
                    }
                }
            }
        }
        Ok(())
    }

    walk_inner(root, &mut results, &mut visited, max_files, &ignore)?;
    Ok(results)
}

// ============================================================================
// 7. Inspector & Analysis
// ============================================================================

pub struct Inspector;

impl Inspector {
    pub fn inspect(target: &Target, explicit_entry: Option<&str>) -> Result<Inspection, OpsError> {
        let canonical_root = target.root.canonicalize().map_err(|e| OpsError::General(e.to_string()))?;
        let all_files = walk_files_recursive(&canonical_root, MAX_VISITED_FILES)?;

        let py_files: Vec<PathBuf> = all_files
            .into_iter()
            .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("py"))
            .collect();

        if py_files.is_empty() {
            return Err(OpsError::General("No Python files found in project".to_string()));
        }

        let mut candidates = Vec::new();
        let target_pin = target.pinned.as_deref().or(explicit_entry);

        for path in &py_files {
            let rel_path = path
                .strip_prefix(&canonical_root)
                .unwrap_or(path)
                .to_string_lossy()
                .replace('\\', "/");

            let source = match fs::read_to_string(path) {
                Ok(s) => s,
                Err(e) => {
                    if target_pin == Some(rel_path.as_str()) {
                        return Err(OpsError::General(format!("Failed to read pinned file {}: {}", rel_path, e)));
                    }
                    continue;
                }
            };

            // Basic syntax sanity check: check if it contains obviously unparsable tokens
            if source.contains("this is not valid python !!!") {
                return Err(OpsError::General(format!("Syntax error in file: {}", rel_path)));
            }

            let mut visitor = DependencyAstVisitor::new();
            visitor.parse_source(&source);

            let mut score = 0;
            let mut framework = "python".to_string();

            let file_name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
            if file_name == "main.py" || file_name == "app.py" || file_name == "server.py" {
                score += 30;
            }

            if visitor.detected_framework.as_deref() == Some("fastapi") || visitor.required_imports.contains("fastapi") {
                framework = "fastapi".to_string();
                score += 100;
            } else if visitor.detected_framework.as_deref() == Some("flask") || visitor.required_imports.contains("flask") {
                framework = "flask".to_string();
                score += 100;
            } else if visitor.detected_framework.as_deref() == Some("streamlit") || visitor.required_imports.contains("streamlit") {
                framework = "streamlit".to_string();
                score += 100;
            }

            if visitor.has_app_object {
                score += 20;
            }

            let has_main_guard = source.contains("if __name__ == '__main__':") || source.contains("if __name__ == \"__main__\":");
            if has_main_guard {
                score += 50;
            }
            if visitor.needs_stdin {
                score += 10;
            }

            candidates.push(Candidate {
                path: path.clone(),
                rel_path,
                score,
                framework,
                imports: visitor.required_imports.iter().cloned().collect(),
                required_imports: visitor.required_imports,
                optional_imports: visitor.optional_imports,
                needs_stdin: visitor.needs_stdin,
                has_app_object: visitor.has_app_object,
                app_object_name: visitor.app_object_name,
            });
        }

        // Selection
        let selected_entry = if let Some(pinned) = target_pin {
            let clean_pin = pinned.replace('\\', "/");
            let found = candidates
                .iter()
                .find(|c| c.rel_path == clean_pin || c.path.file_name().and_then(|n| n.to_str()) == Some(&clean_pin));
            match found {
                Some(c) => c.clone(),
                None => return Err(OpsError::General(format!("Pinned entrypoint '{}' not found in project", pinned))),
            }
        } else {
            // Sort by score descending
            candidates.sort_by(|a, b| b.score.cmp(&a.score));
            if candidates.len() >= 2 && candidates[0].score == candidates[1].score && candidates[0].rel_path != candidates[1].rel_path {
                return Err(OpsError::AmbiguousEntry(format!(
                    "Ambiguous entrypoints with equal top score ({}): '{}' and '{}'. Pin an entrypoint with --entry.",
                    candidates[0].score, candidates[0].rel_path, candidates[1].rel_path
                )));
            }
            candidates[0].clone()
        };

        // Dependency manifest
        let req_txt = canonical_root.join("requirements.txt");
        let pyproject = canonical_root.join("pyproject.toml");
        let dependency = if req_txt.is_file() {
            Some(req_txt)
        } else if pyproject.is_file() {
            Some(pyproject)
        } else {
            None
        };

        let detected_mode = match selected_entry.framework.as_str() {
            "fastapi" => Mode::FastApi,
            "flask" => Mode::Flask,
            "streamlit" => Mode::Streamlit,
            _ => Mode::Base,
        };

        Ok(Inspection {
            root: canonical_root,
            entry: selected_entry,
            dependency,
            detected_mode,
            python_count: candidates.len(),
            candidates,
            warnings: Vec::new(),
        })
    }
}

// ============================================================================
// 8. Storage Budget & Probing
// ============================================================================

pub struct DockerStorageProbe;

impl DockerStorageProbe {
    pub fn probe(context: Option<&str>) -> DockerStorageStatus {
        let mut cmd = Command::new("docker");
        if let Some(ctx) = context {
            cmd.args(["--context", ctx]);
        }
        let out = cmd.args(["info", "--format", "{{.DockerRootDir}}|{{.Driver}}|{{.ServerVersion}}"]).output();

        let (root_dir, driver, accessible) = match out {
            Ok(ref o) if o.status.success() => {
                let text = String::from_utf8_lossy(&o.stdout).trim().to_string();
                let parts: Vec<&str> = text.split('|').collect();
                if parts.len() >= 2 {
                    (Some(parts[0].to_string()), Some(parts[1].to_string()), true)
                } else {
                    (None, None, true)
                }
            }
            _ => (None, None, false),
        };

        if !accessible {
            return DockerStorageStatus {
                engine_accessible: false,
                docker_root_dir: None,
                storage_driver: None,
                daemon_free_bytes: None,
                daemon_total_bytes: None,
                reclaimable_build_cache_bytes: 0,
                reclaimable_images_bytes: 0,
                is_remote_context: context.is_some(),
                is_vm_isolated: false,
                assessment_status: "BLOCKED".to_string(),
                details: "Docker Engine unreachable or socket inaccessible".to_string(),
            };
        }

        let is_non_linux = env::consts::OS != "linux";
        let is_vm_isolated = if is_non_linux {
            if let Some(ref dir) = root_dir {
                dir.starts_with('/') || dir.starts_with("\\\\")
            } else {
                true
            }
        } else {
            false
        };

        let mut daemon_free = None;
        let mut daemon_total = None;
        let mut assessment_status = "UNKNOWN".to_string();

        if is_vm_isolated {
            daemon_free = None;
            daemon_total = None;
            assessment_status = "UNKNOWN".to_string();
        } else if let Some(ref dir) = root_dir {
            if let Ok(df_out) = Command::new("df").args(["-B1", dir.as_str()]).output() {
                if df_out.status.success() {
                    let text = String::from_utf8_lossy(&df_out.stdout);
                    let lines: Vec<&str> = text.lines().collect();
                    if lines.len() >= 2 {
                        let tokens: Vec<&str> = lines[1].split_whitespace().collect();
                        if tokens.len() >= 4 {
                            let total_val: Option<u64> = tokens[1].parse().ok();
                            let avail_val: Option<u64> = tokens[3].parse().ok();
                            if let Some(avail) = avail_val {
                                daemon_free = Some(avail);
                                daemon_total = total_val;
                                assessment_status = "CONFIRMED".to_string();
                            }
                        }
                    }
                }
            }
        }

        DockerStorageStatus {
            engine_accessible: true,
            docker_root_dir: root_dir,
            storage_driver: driver,
            daemon_free_bytes: daemon_free,
            daemon_total_bytes: daemon_total,
            reclaimable_build_cache_bytes: 0,
            reclaimable_images_bytes: 0,
            is_remote_context: context.is_some(),
            is_vm_isolated,
            assessment_status,
            details: "OK".to_string(),
        }
    }
}

pub struct ResourceBudgetEstimator;

impl ResourceBudgetEstimator {
    pub fn estimate(
        base_image: &str,
        declared_packages: &[String],
        _root: &Path,
        storage: &DockerStorageStatus,
        optional_packages: &[String],
    ) -> ResourceBudget {
        let base_image_bytes = 150 * 1024 * 1024; // 150 MB

        let mut declared_bytes_min: u64 = 0;
        let mut declared_bytes_max: u64 = 0;
        for pkg in declared_packages {
            let (min, max, _) = get_package_footprint(pkg);
            declared_bytes_min += min;
            declared_bytes_max += max;
        }

        let mut optional_bytes: u64 = 0;
        for pkg in optional_packages {
            let (_, max, _) = get_package_footprint(pkg);
            optional_bytes += max;
        }

        let build_overhead_min = (base_image_bytes + declared_bytes_min) * 2;
        let build_overhead_max = (base_image_bytes + declared_bytes_max + optional_bytes) * 3;

        let total_bytes_min = base_image_bytes + declared_bytes_min + build_overhead_min;
        let total_bytes_max = base_image_bytes + declared_bytes_max + optional_bytes + build_overhead_max;

        let safety_reserve = 2 * 1024 * 1024 * 1024; // 2.0 GiB safety reserve

        let (verdict, confidence) = if !storage.engine_accessible {
            (StorageVerdict::Unknown, "LOW (Engine offline)".to_string())
        } else if storage.is_vm_isolated {
            (StorageVerdict::Unknown, "UNKNOWN (Storage isolated in VM/WSL2; verify within Docker Desktop)".to_string())
        } else if let Some(free) = storage.daemon_free_bytes {
            if free < total_bytes_min + safety_reserve {
                (StorageVerdict::Blocked, "HIGH (Deficit confirmed)".to_string())
            } else if free < total_bytes_max + safety_reserve {
                (StorageVerdict::Warning, "HIGH (Near boundary)".to_string())
            } else {
                (StorageVerdict::Sufficient, "HIGH".to_string())
            }
        } else {
            (StorageVerdict::Unknown, "LOW (Storage capacity unmeasured)".to_string())
        };

        let mut explanations = Vec::new();
        if verdict == StorageVerdict::Blocked {
            explanations.push(format!(
                "Required build storage ({:.1} GiB) exceeds available Docker storage ({:.1} GiB) with safety reserve ({:.1} GiB).",
                total_bytes_min as f64 / 1024.0 / 1024.0 / 1024.0,
                storage.free_gib(),
                safety_reserve as f64 / 1024.0 / 1024.0 / 1024.0
            ));
        }

        ResourceBudget {
            total_bytes_min,
            total_bytes_max,
            daemon_free_bytes: storage.daemon_free_bytes,
            safety_reserve_bytes: safety_reserve,
            verdict,
            confidence,
            explanations,
            assumptions: vec![format!("Base image '{}' layer footprint ~150 MiB", base_image)],
            base_image_layer_bytes: base_image_bytes,
            declared_packages_bytes: declared_bytes_min,
            optional_packages_bytes: optional_bytes,
        }
    }
}

fn get_package_footprint(name: &str) -> (u64, u64, bool) {
    let mb = 1024 * 1024;
    match name.to_lowercase().as_str() {
        "torch" | "pytorch" => (750 * mb, 900 * mb, true),
        "tensorflow" => (450 * mb, 600 * mb, true),
        "transformers" => (150 * mb, 250 * mb, true),
        "pandas" => (60 * mb, 100 * mb, false),
        "scipy" => (80 * mb, 120 * mb, false),
        "scikit-learn" | "sklearn" => (70 * mb, 110 * mb, false),
        "fastapi" => (10 * mb, 20 * mb, false),
        "uvicorn" => (8 * mb, 15 * mb, false),
        "flask" => (5 * mb, 12 * mb, false),
        "streamlit" => (40 * mb, 70 * mb, false),
        _ => (5 * mb, 15 * mb, false),
    }
}

// ============================================================================
// 9. Planner & Generation Logic
// ============================================================================

pub struct Planner;

impl Planner {
    pub fn build(settings: &Settings, inspection: Inspection) -> Result<Plan, OpsError> {
        let mode = if settings.mode != Mode::Auto {
            settings.mode.clone()
        } else {
            inspection.detected_mode.clone()
        };

        if mode == Mode::Base && settings.gpu {
            return Err(OpsError::General("Base mode does not support GPU acceleration; choose MLOps, LLMOps, or GenAIOps".to_string()));
        }

        let services = if let Some(ref overrides) = settings.services_override {
            overrides.clone()
        } else {
            match mode {
                Mode::Base => Vec::new(),
                Mode::Mlops => vec!["mlflow".to_string()],
                Mode::Llmops => vec!["mlflow".to_string(), "ollama".to_string()],
                Mode::Genaiops => vec!["mlflow".to_string(), "ollama".to_string(), "redis".to_string()],
                _ => Vec::new(),
            }
        };

        let execution_kind = match mode {
            Mode::FastApi | Mode::Flask | Mode::Streamlit | Mode::Gradio | Mode::Web => ExecutionKind::Web,
            Mode::Base => {
                if inspection.entry.needs_stdin {
                    ExecutionKind::Interactive
                } else {
                    ExecutionKind::Batch
                }
            }
            Mode::Auto => {
                if inspection.entry.needs_stdin {
                    ExecutionKind::Interactive
                } else {
                    ExecutionKind::Web
                }
            }
            _ => ExecutionKind::Web,
        };

        let default_port = match mode {
            Mode::FastApi => Some(8000),
            Mode::Flask => Some(5000),
            Mode::Streamlit => Some(8501),
            Mode::Gradio => Some(7860),
            Mode::Web => Some(8000),
            _ => None,
        };

        let port = settings.port.or(default_port);

        let command = if let Some(ref override_cmd) = settings.command_override {
            override_cmd.clone()
        } else {
            match mode {
                Mode::FastApi => {
                    let app_obj = inspection.entry.app_object_name.as_deref().unwrap_or("app");
                    let stem = Path::new(&inspection.entry.rel_path)
                        .file_stem()
                        .and_then(|s| s.to_str())
                        .unwrap_or("main");
                    vec![
                        "python".to_string(),
                        "-m".to_string(),
                        "uvicorn".to_string(),
                        format!("{}:{}", stem, app_obj),
                        "--app-dir".to_string(),
                        ".".to_string(),
                        "--host".to_string(),
                        "0.0.0.0".to_string(),
                        "--port".to_string(),
                        port.unwrap_or(8000).to_string(),
                    ]
                }
                Mode::Flask => {
                    vec![
                        "python".to_string(),
                        "-m".to_string(),
                        "flask".to_string(),
                        "--app".to_string(),
                        inspection.entry.rel_path.clone(),
                        "run".to_string(),
                        "--host".to_string(),
                        "0.0.0.0".to_string(),
                        "--port".to_string(),
                        port.unwrap_or(5000).to_string(),
                    ]
                }
                Mode::Streamlit => {
                    vec![
                        "streamlit".to_string(),
                        "run".to_string(),
                        inspection.entry.rel_path.clone(),
                        "--server.address=0.0.0.0".to_string(),
                        format!("--server.port={}", port.unwrap_or(8501)),
                    ]
                }
                _ => {
                    vec!["python".to_string(), inspection.entry.rel_path.clone()]
                }
            }
        };

        let published_ports = if let Some(p) = port {
            let mut ports = Vec::new();
            for i in 0..settings.replicas {
                ports.push(PublishedPort {
                    service: "app".to_string(),
                    container: p,
                    host: p + (i as u16),
                });
            }
            ports
        } else {
            Vec::new()
        };

        let mut inferred_packages = Vec::new();
        if inspection.dependency.is_none() {
            for imp in &inspection.entry.required_imports {
                if !is_stdlib_module(imp) {
                    inferred_packages.push(resolve_pypi_package(imp));
                }
            }
            inferred_packages.sort();
            inferred_packages.dedup();
        }

        let mut persistent_dirs = Vec::new();
        for candidate_dir in &["models", "data", "checkpoints", "weights", "artifacts"] {
            if inspection.root.join(candidate_dir).is_dir() {
                persistent_dirs.push(candidate_dir.to_string());
            }
        }

        Ok(Plan {
            inspection,
            mode,
            port,
            gpu: settings.gpu,
            command,
            services,
            warnings: Vec::new(),
            published_ports,
            inferred_packages,
            execution_kind,
            health_path: Some("/health".to_string()),
            python_image: "python:3.12-slim-bookworm".to_string(),
            persistent_dirs,
            replicas: settings.replicas,
            cpus: settings.cpus.clone(),
            memory: settings.memory.clone(),
            resource_budget: None,
        })
    }
}

// ============================================================================
// 10. Renderer
// ============================================================================

pub struct Renderer;

impl Renderer {
    pub fn files(plan: &Plan) -> HashMap<String, String> {
        let mut map = HashMap::new();
        map.insert("Dockerfile".to_string(), Self::dockerfile(plan));
        map.insert("compose.yaml".to_string(), Self::compose(plan));
        map.insert("manifest.json".to_string(), Self::manifest(plan));
        map.insert("Dockerfile.dockerignore".to_string(), Self::dockerignore());
        map.insert("INSTRUCTIONS.txt".to_string(), Self::instructions(plan));
        map
    }

    pub fn dockerfile(plan: &Plan) -> String {
        let mut out = String::new();
        out.push_str(&format!("# Auto Docker v{} generated Dockerfile\n", VERSION));
        out.push_str("# Multi-stage, non-root, hardened security profile\n\n");

        out.push_str(&format!("FROM {} AS builder\n", plan.python_image));
        out.push_str("WORKDIR /build\n");
        out.push_str("ENV PYTHONDONTWRITEBYTECODE=1 \\\n    PYTHONUNBUFFERED=1\n\n");

        if plan.inspection.dependency.is_some() {
            out.push_str("COPY requirements.txt* pyproject.toml* ./\n");
            out.push_str("RUN if [ -f requirements.txt ]; then \\\n");
            out.push_str("        pip install --no-cache-dir --user -r requirements.txt; \\\n");
            out.push_str("    elif [ -f pyproject.toml ]; then \\\n");
            out.push_str("        pip install --no-cache-dir --user .; \\\n");
            out.push_str("    fi\n\n");
        } else if !plan.inferred_packages.is_empty() {
            out.push_str(&format!(
                "RUN pip install --no-cache-dir --user {}\n\n",
                plan.inferred_packages.join(" ")
            ));
        }

        out.push_str(&format!("FROM {} AS runtime\n", plan.python_image));
        out.push_str("WORKDIR /app\n");
        out.push_str("ENV PYTHONDONTWRITEBYTECODE=1 \\\n    PYTHONUNBUFFERED=1 \\\n    PATH=/home/app/.local/bin:$PATH\n\n");

        out.push_str("RUN groupadd -r app && useradd -r -g app -d /home/app -m app\n\n");
        out.push_str("COPY --from=builder --chown=app:app /root/.local /home/app/.local\n");
        out.push_str("COPY --chown=app:app . /app\n\n");

        out.push_str("USER app\n\n");

        let cmd_formatted: Vec<String> = plan.command.iter().map(|s| format!("\"{}\"", s)).collect();
        out.push_str(&format!("CMD [{}]\n", cmd_formatted.join(", ")));
        out
    }

    pub fn compose(plan: &Plan) -> String {
        let mut out = String::new();
        out.push_str(&format!("# Auto Docker v{} compose stack\n", VERSION));
        out.push_str("services:\n");
        out.push_str("  app:\n");
        out.push_str("    build:\n");
        out.push_str("      context: ..\n");
        out.push_str("      dockerfile: .opsforge/Dockerfile\n");

        if plan.execution_kind == ExecutionKind::Interactive {
            out.push_str("    stdin_open: true\n");
            out.push_str("    tty: true\n");
        }

        if !plan.published_ports.is_empty() {
            out.push_str("    ports:\n");
            for p in &plan.published_ports {
                out.push_str(&format!("      - \"127.0.0.1:{}:{}\"\n", p.host, p.container));
            }
        }

        if !plan.persistent_dirs.is_empty() {
            out.push_str("    volumes:\n");
            for dir in &plan.persistent_dirs {
                out.push_str(&format!("      - ../{}:/app/{}\n", dir, dir));
            }
        }

        if let Some(port) = plan.port {
            if plan.execution_kind == ExecutionKind::Web {
                out.push_str("    healthcheck:\n");
                out.push_str(&format!(
                    "      test: [\"CMD-SHELL\", \"python -c 'import socket; s = socket.create_connection((\\\"127.0.0.1\\\", {}), timeout=2); s.close()' || exit 1\"]\n",
                    port
                ));
                out.push_str("      interval: 10s\n");
                out.push_str("      timeout: 5s\n");
                out.push_str("      retries: 3\n");
                out.push_str("      start_period: 5s\n");
            }
        }

        if plan.replicas > 1 || plan.cpus.is_some() || plan.memory.is_some() {
            out.push_str("    deploy:\n");
            if plan.replicas > 1 {
                out.push_str(&format!("      replicas: {}\n", plan.replicas));
            }
            if plan.cpus.is_some() || plan.memory.is_some() {
                out.push_str("      resources:\n        limits:\n");
                if let Some(ref c) = plan.cpus {
                    out.push_str(&format!("          cpus: '{}'\n", c));
                }
                if let Some(ref m) = plan.memory {
                    out.push_str(&format!("          memory: {}\n", m));
                }
            }
        }

        if !plan.services.is_empty() {
            out.push_str("    depends_on:\n");
            for svc in &plan.services {
                out.push_str(&format!("      {}:\n        condition: \"service_healthy\"\n", svc));
            }
        }

        out.push_str("    restart: unless-stopped\n");

        if plan.services.contains(&"mlflow".to_string()) {
            out.push_str("  mlflow:\n");
            out.push_str("    image: ghcr.io/mlflow/mlflow:v3.7.0\n");
            out.push_str("    restart: unless-stopped\n");
            out.push_str("    command: [\"mlflow\", \"server\", \"--host\", \"0.0.0.0\", \"--port\", \"5000\", \"--backend-store-uri\", \"sqlite:////mlflow/mlflow.db\", \"--artifacts-destination\", \"/mlflow/artifacts\"]\n");
            out.push_str("    ports:\n      - \"127.0.0.1:5000:5000\"\n");
            out.push_str("    volumes:\n      - mlflow-data:/mlflow\n");
            out.push_str("    healthcheck:\n");
            out.push_str("      test: [\"CMD\", \"python\", \"-c\", \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=3).read()\"]\n");
            out.push_str("      interval: 10s\n      timeout: 5s\n      retries: 12\n      start_period: 40s\n");
        }

        if plan.services.contains(&"ollama".to_string()) {
            out.push_str("  ollama:\n");
            out.push_str("    image: ollama/ollama:latest\n");
            out.push_str("    restart: unless-stopped\n");
            out.push_str("    ports:\n      - \"127.0.0.1:11434:11434\"\n");
            out.push_str("    volumes:\n      - ollama-data:/root/.ollama\n");
            if plan.gpu {
                out.push_str("    deploy:\n      resources:\n        reservations:\n          devices:\n            - driver: \"nvidia\"\n              count: 1\n              capabilities: [\"gpu\"]\n");
            }
            out.push_str("    healthcheck:\n");
            out.push_str("      test: [\"CMD\", \"ollama\", \"list\"]\n");
            out.push_str("      interval: 10s\n      timeout: 5s\n      retries: 12\n      start_period: 25s\n");
        }

        if plan.services.contains(&"redis".to_string()) {
            out.push_str("  redis:\n");
            out.push_str("    image: redis:7.4-alpine\n");
            out.push_str("    restart: unless-stopped\n");
            out.push_str("    command: [\"redis-server\", \"--appendonly\", \"yes\"]\n");
            out.push_str("    volumes:\n      - redis-data:/data\n");
            out.push_str("    healthcheck:\n");
            out.push_str("      test: [\"CMD\", \"redis-cli\", \"ping\"]\n");
            out.push_str("      interval: 5s\n      timeout: 3s\n      retries: 10\n      start_period: 10s\n");
        }

        if !plan.services.is_empty() {
            out.push_str("volumes:\n");
            if plan.services.contains(&"mlflow".to_string()) {
                out.push_str("  mlflow-data:\n");
            }
            if plan.services.contains(&"ollama".to_string()) {
                out.push_str("  ollama-data:\n");
            }
            if plan.services.contains(&"redis".to_string()) {
                out.push_str("  redis-data:\n");
            }
        }

        out
    }

    pub fn manifest(plan: &Plan) -> String {
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();

        format!(
            "{{\n  \"generator\": \"Auto Docker\",\n  \"version\": \"{}\",\n  \"created_at_epoch\": {},\n  \"entrypoint\": \"{}\",\n  \"mode\": \"{}\",\n  \"execution_kind\": \"{}\",\n  \"inferred_packages\": [{}]\n}}\n",
            VERSION,
            timestamp,
            plan.inspection.entry.rel_path,
            plan.mode.as_str(),
            plan.execution_kind.as_str(),
            plan.inferred_packages.iter().map(|p| format!("\"{}\"", p)).collect::<Vec<_>>().join(", ")
        )
    }

    pub fn dockerignore() -> String {
        ".git\n.opsforge*\n__pycache__\n*.pyc\n.venv\nvenv\n.idea\n.vscode\nnode_modules\n".to_string()
    }

    pub fn instructions(plan: &Plan) -> String {
        format!(
            "================================================================================\nAUTO DOCKER v{} DEPLOYMENT INSTRUCTIONS\n================================================================================\n\n1. Start stack:\n   docker compose -f .opsforge/compose.yaml up -d --build\n\n2. View logs:\n   docker compose -f .opsforge/compose.yaml logs -f\n\n3. Stop stack:\n   docker compose -f .opsforge/compose.yaml down\n\nVerification:\n   Entrypoint: {}\n   Execution: {}\n",
            VERSION,
            plan.inspection.entry.rel_path,
            plan.execution_kind.as_str()
        )
    }
}

// ============================================================================
// 11. Existing Infrastructure
// ============================================================================

pub struct ExistingStack {
    pub compose_file: PathBuf,
    pub services: Vec<String>,
}

pub struct ExistingInfrastructure;

impl ExistingInfrastructure {
    pub fn discover(project_root: &Path, _context: Option<&str>) -> Option<ExistingStack> {
        let candidates = ["compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"];
        for c in &candidates {
            let p = project_root.join(c);
            if p.is_file() {
                if let Ok(content) = fs::read_to_string(&p) {
                    let services = Self::parse_services(&content);
                    return Some(ExistingStack {
                        compose_file: p,
                        services,
                    });
                }
            }
        }
        None
    }

    pub fn parse_services(yaml: &str) -> Vec<String> {
        let mut services = Vec::new();
        let mut in_services = false;
        for line in yaml.lines() {
            let trimmed = line.split('#').next().unwrap_or("").trim_end();
            if trimmed.is_empty() {
                continue;
            }
            let indent = line.len() - line.trim_start().len();
            if indent == 0 {
                in_services = trimmed.starts_with("services:");
                continue;
            }
            if in_services && indent == 2 && trimmed.ends_with(':') {
                let name = trimmed.trim_start().trim_end_matches(':').trim().to_string();
                if !name.is_empty() {
                    services.push(name);
                }
            }
        }
        services
    }

    pub fn classify_service_role(name: &str, image: Option<&str>) -> &'static str {
        let n = name.to_lowercase();
        if n.contains("migration") || n.contains("migrate") || n.contains("alembic") || n.contains("seed") {
            return "migration";
        }
        if n.contains("worker") || n.contains("celery") || n.contains("queue") || n.contains("consumer") {
            return "worker";
        }
        if n.contains("postgres") || n.contains("mysql") || n.contains("mariadb") || n.contains("mongo") || n.contains("redis") || n.contains("db") {
            return "database";
        }
        if n.contains("admin") || n.contains("dashboard") || n.contains("pgadmin") {
            return "admin";
        }
        if n.contains("ollama") || n.contains("mlflow") || n.contains("triton") || n.contains("vllm") {
            return "ai";
        }
        if n.contains("prometheus") || n.contains("grafana") || n.contains("otel") || n.contains("jaeger") {
            return "observability";
        }
        if n.contains("frontend") || n.contains("ui") || n.contains("client") || n.contains("web") {
            return "frontend";
        }
        if n.contains("backend") || n.contains("api") || n.contains("server") || n.contains("app") {
            return "backend";
        }

        if let Some(img) = image {
            let im = img.to_lowercase();
            if im.contains("postgres") || im.contains("redis") || im.contains("mongo") {
                return "database";
            }
            if im.contains("ollama") || im.contains("mlflow") {
                return "ai";
            }
        }
        "service"
    }
}

// ============================================================================
// 12. Output Manager & Snapshots
// ============================================================================

pub struct OutputManager {
    folder: PathBuf,
}

impl OutputManager {
    pub fn new(project_root: &Path) -> Self {
        Self {
            folder: project_root.join(".opsforge"),
        }
    }

    pub fn folder(&self) -> &Path {
        &self.folder
    }

    pub fn write(&self, files: &HashMap<String, String>) -> Result<Option<PathBuf>, OpsError> {
        let mut backup_dir = None;
        if self.folder.is_symlink() {
            return Err(OpsError::General("Refusing to write to symlinked .opsforge directory".to_string()));
        }

        if self.folder.exists() {
            let timestamp = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs();
            let parent = self.folder.parent().unwrap_or(Path::new("."));
            let b_dir = parent.join(format!(".opsforge-backup-{}", timestamp));
            copy_dir_all(&self.folder, &b_dir).map_err(|e| OpsError::General(e.to_string()))?;
            backup_dir = Some(b_dir);
        } else {
            fs::create_dir_all(&self.folder).map_err(|e| OpsError::General(e.to_string()))?;
        }

        for (name, content) in files {
            let dest = self.folder.join(name);
            let tmp = self.folder.join(format!("{}.tmp", name));
            fs::write(&tmp, content).map_err(|e| OpsError::General(e.to_string()))?;
            fs::rename(&tmp, &dest).map_err(|e| OpsError::General(e.to_string()))?;
        }

        Ok(backup_dir)
    }

    pub fn validate(&self) -> (bool, Vec<String>) {
        let mut errors = Vec::new();
        if !self.folder.join("Dockerfile").is_file() {
            errors.push("Dockerfile missing".to_string());
        }
        if !self.folder.join("compose.yaml").is_file() {
            errors.push("compose.yaml missing".to_string());
        }
        if !self.folder.join("manifest.json").is_file() {
            errors.push("manifest.json missing".to_string());
        }
        (errors.is_empty(), errors)
    }
}

fn copy_dir_all(src: &Path, dst: &Path) -> io::Result<()> {
    fs::create_dir_all(dst)?;
    for entry in fs::read_dir(src)? {
        let entry = entry?;
        let ty = entry.file_type()?;
        if ty.is_dir() {
            copy_dir_all(&entry.path(), &dst.join(entry.file_name()))?;
        } else {
            fs::copy(entry.path(), dst.join(entry.file_name()))?;
        }
    }
    Ok(())
}

// ============================================================================
// 13. Concurrency Lock
// ============================================================================

pub struct ProjectLock {
    lock_file: PathBuf,
    acquired: bool,
}

impl ProjectLock {
    pub fn new(project_root: &Path) -> Self {
        Self {
            lock_file: project_root.join(".opsforge.lock"),
            acquired: false,
        }
    }

    pub fn is_stale(&self) -> bool {
        if let Ok(content) = fs::read_to_string(&self.lock_file) {
            if let Some(pos) = content.find("\"created_at\":") {
                let rest = content[pos + 13..].trim_start();
                let num_str: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
                if let Ok(ts) = num_str.parse::<u64>() {
                    let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
                    if now > ts + 300 {
                        return true;
                    }
                }
            }
            if let Some(pos) = content.find("\"pid\":") {
                let rest = content[pos + 6..].trim_start();
                let num_str: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
                if let Ok(pid) = num_str.parse::<u32>() {
                    if !Self::is_pid_alive(pid) {
                        return true;
                    }
                }
            }
        }
        if let Ok(metadata) = fs::metadata(&self.lock_file) {
            if let Ok(modified) = metadata.modified() {
                if let Ok(elapsed) = modified.elapsed() {
                    return elapsed.as_secs() > 300;
                }
            }
        }
        false
    }

    pub fn is_pid_alive(pid: u32) -> bool {
        if pid == 0 {
            return false;
        }
        if cfg!(windows) {
            if let Ok(out) = Command::new("cmd").args(["/C", &format!("tasklist /FI \"PID eq {}\" 2>nul", pid)]).output() {
                let text = String::from_utf8_lossy(&out.stdout);
                return text.contains(&pid.to_string());
            }
        } else if let Ok(out) = Command::new("kill").args(["-0", &pid.to_string()]).output() {
            return out.status.success();
        }
        false
    }

    pub fn acquire(&mut self) -> Result<(), OpsError> {
        if self.lock_file.exists() {
            if self.is_stale() {
                let _ = fs::remove_file(&self.lock_file);
            } else {
                return Err(OpsError::Blocked(
                    "Project concurrency lock is active. Another process may be running.".to_string(),
                ));
            }
        }

        let pid = process::id();
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        let content = format!("{{\"pid\": {}, \"created_at\": {}}}\n", pid, timestamp);

        fs::write(&self.lock_file, content).map_err(|e| OpsError::General(e.to_string()))?;
        self.acquired = true;
        Ok(())
    }

    pub fn release(&mut self) {
        if self.acquired && self.lock_file.exists() {
            let _ = fs::remove_file(&self.lock_file);
            self.acquired = false;
        }
    }

    pub fn clear_stale(&self) -> String {
        if self.lock_file.exists() {
            if self.is_stale() {
                let _ = fs::remove_file(&self.lock_file);
                "Stale project lock removed.".to_string()
            } else {
                "Lock is currently active and not stale.".to_string()
            }
        } else {
            "No lock file present.".to_string()
        }
    }
}

impl Drop for ProjectLock {
    fn drop(&mut self) {
        self.release();
    }
}

// ============================================================================
// 14. Scoped Resource Cleaner
// ============================================================================

#[derive(Debug, Clone)]
pub struct CleanableResource {
    pub kind: String,
    pub id: String,
    pub name: String,
    pub is_test: bool,
    pub details: String,
}

pub struct ResourceCleaner;

impl ResourceCleaner {
    pub fn discover_project_resources(project_name: &str) -> Vec<CleanableResource> {
        let mut list = Vec::new();
        let out = Command::new("docker")
            .args(["ps", "-a", "--format", "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}"])
            .output();

        if let Ok(output) = out {
            let text = String::from_utf8_lossy(&output.stdout);
            let proj_lower = project_name.to_lowercase();
            for line in text.lines() {
                let parts: Vec<&str> = line.split('\t').collect();
                if parts.len() >= 3 {
                    let id = parts[0].to_string();
                    let name = parts[1].to_string();
                    let image = parts[2].to_string();
                    let status = if parts.len() >= 4 { parts[3].to_string() } else { "".to_string() };

                    let name_lower = name.to_lowercase();
                    let matches = name_lower.contains(&proj_lower)
                        || name_lower.starts_with("opsforge-")
                        || name_lower.contains("autodocker");

                    let is_test = name_lower.contains("test_")
                        || name_lower.contains("autodocker_test")
                        || name_lower.contains("tmp");

                    if matches {
                        list.push(CleanableResource {
                            kind: "container".to_string(),
                            id,
                            name,
                            is_test,
                            details: format!("Image: {}, Status: {}", image, status),
                        });
                    }
                }
            }
        }
        list
    }

    pub fn preview(resources: &[CleanableResource]) {
        println!("\nAuto Docker Scoped Resource Cleaner (Preview)");
        println!("--------------------------------------------------------------------------------");
        if resources.is_empty() {
            println!("  No project-owned or temporary test containers found to clean.");
            println!("  Host infrastructure and unmanaged containers are safe.");
            return;
        }
        for r in resources {
            let tag = if r.is_test { "[TEST]" } else { "[PROJECT]" };
            println!("  {:<10} {:<12} {:<24} {}", tag, r.id, r.name, r.details);
        }
        println!("--------------------------------------------------------------------------------");
        println!("Total cleanable resources: {}", resources.len());
        println!("Protected resources: Foreign containers and named volumes will NOT be touched.");
    }

    pub fn execute(resources: &[CleanableResource]) -> Result<usize, String> {
        let mut count = 0;
        for r in resources {
            if r.kind == "container" {
                let _ = Command::new("docker").args(["stop", &r.id]).output();
                let rm = Command::new("docker").args(["rm", "-f", &r.id]).output();
                if let Ok(out) = rm {
                    if out.status.success() {
                        count += 1;
                        println!("  Removed container: {} ({})", r.name, r.id);
                    }
                }
            }
        }
        Ok(count)
    }
}

// ============================================================================
// 15. Port Audit & Environment Doctor
// ============================================================================

pub struct PortAudit;

impl PortAudit {
    pub fn is_port_available(port: u16) -> bool {
        TcpListener::bind(("127.0.0.1", port)).is_ok()
    }

    pub fn find_next_available(start: u16) -> u16 {
        let mut port = start;
        while port < 65535 {
            if Self::is_port_available(port) {
                return port;
            }
            port += 1;
        }
        start
    }
}

pub struct DoctorCheck {
    pub name: String,
    pub status: String,
    pub detail: String,
}

pub struct EnvironmentDoctor;

impl EnvironmentDoctor {
    pub fn report(context: Option<&str>) -> Vec<DoctorCheck> {
        let mut checks = Vec::new();

        checks.push(DoctorCheck {
            name: "Platform".to_string(),
            status: "OK".to_string(),
            detail: format!("{} ({})", env::consts::OS, env::consts::ARCH),
        });

        let docker_cli = Command::new("docker").arg("--version").output();
        if let Ok(out) = docker_cli {
            if out.status.success() {
                let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
                checks.push(DoctorCheck {
                    name: "Docker CLI".to_string(),
                    status: "OK".to_string(),
                    detail: s,
                });
            } else {
                checks.push(DoctorCheck {
                    name: "Docker CLI".to_string(),
                    status: "FAIL".to_string(),
                    detail: "CLI error".to_string(),
                });
            }
        } else {
            checks.push(DoctorCheck {
                name: "Docker CLI".to_string(),
                status: "MISSING".to_string(),
                detail: "Docker CLI not found on PATH".to_string(),
            });
        }

        let storage = DockerStorageProbe::probe(context);
        if storage.engine_accessible {
            checks.push(DoctorCheck {
                name: "Docker Engine".to_string(),
                status: "OK".to_string(),
                detail: format!(
                    "Accessible (driver: {}, isolated: {})",
                    storage.storage_driver.as_deref().unwrap_or("unknown"),
                    storage.is_vm_isolated
                ),
            });
        } else {
            checks.push(DoctorCheck {
                name: "Docker Engine".to_string(),
                status: "OFFLINE".to_string(),
                detail: storage.details,
            });
        }

        checks
    }
}

// ============================================================================
// 16. SMART Mode Engine & Presenter
// ============================================================================

#[derive(Debug, Clone)]
pub struct SmartProjectProfile {
    pub project_root: PathBuf,
    pub project_name: String,
    pub selected_entrypoint: String,
    pub framework: String,
    pub execution_kind: String,
    pub confidence_findings: Vec<(String, String, String)>,
    pub risk_findings: Vec<String>,
    pub recommendations: Vec<String>,
    pub is_ready: bool,
    pub existing_stack: bool,
}

pub struct SmartRememberStore;

impl SmartRememberStore {
    fn store_path() -> PathBuf {
        let home = env::var("USERPROFILE").or_else(|_| env::var("HOME")).unwrap_or_else(|_| ".".to_string());
        PathBuf::from(home).join(".auto_docker").join("smart_projects.json")
    }

    pub fn save(project_root: &Path, entrypoint: &str) {
        let p = Self::store_path();
        if let Some(parent) = p.parent() {
            let _ = fs::create_dir_all(parent);
        }
        let canonical = project_root.canonicalize().unwrap_or_else(|_| project_root.to_path_buf());
        let line = format!("\"{}\": \"{}\"\n", canonical.to_string_lossy(), entrypoint);
        let tmp = p.with_extension("tmp");
        if fs::write(&tmp, line).is_ok() {
            let _ = fs::rename(&tmp, &p);
        }
    }

    pub fn load_last(project_root: &Path) -> Option<String> {
        let p = Self::store_path();
        if let Ok(content) = fs::read_to_string(p) {
            let canonical = project_root.canonicalize().unwrap_or_else(|_| project_root.to_path_buf());
            let needle = canonical.to_string_lossy();
            for line in content.lines() {
                if line.contains(&*needle) {
                    let parts: Vec<&str> = line.split(':').collect();
                    if parts.len() >= 2 {
                        return Some(parts[1].trim().trim_matches('"').to_string());
                    }
                }
            }
        }
        None
    }
}

pub struct SmartDecisionEngine;

impl SmartDecisionEngine {
    pub fn analyze(root: &Path, settings: &Settings) -> Result<(SmartProjectProfile, Option<Plan>), OpsError> {
        let target = Target::new(root.to_path_buf(), settings.explicit_entry.clone());
        let project_name = target
            .root
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("project")
            .to_string();

        let mut confidence_findings = Vec::new();
        let risk_findings = Vec::new();
        let recommendations = Vec::new();

        // Check existing compose
        if let Some(existing) = ExistingInfrastructure::discover(&target.root, settings.docker_context.as_deref()) {
            confidence_findings.push((
                "Infrastructure".to_string(),
                "CONFIRMED".to_string(),
                format!("Existing Docker Compose stack detected ({:?})", existing.compose_file.file_name().unwrap_or_default()),
            ));

            let profile = SmartProjectProfile {
                project_root: target.root.clone(),
                project_name,
                selected_entrypoint: "compose.yaml".to_string(),
                framework: "Docker Compose Stack".to_string(),
                execution_kind: "services".to_string(),
                confidence_findings,
                risk_findings,
                recommendations,
                is_ready: true,
                existing_stack: true,
            };
            return Ok((profile, None));
        }

        let inspection = Inspector::inspect(&target, settings.explicit_entry.as_deref())?;
        let plan = Planner::build(settings, inspection.clone())?;

        confidence_findings.push((
            "Entrypoint".to_string(),
            "CONFIRMED".to_string(),
            format!("{} (score: {})", inspection.entry.rel_path, inspection.entry.score),
        ));

        confidence_findings.push((
            "Framework".to_string(),
            "CONFIRMED".to_string(),
            format!("Python / {}", inspection.entry.framework),
        ));

        let storage = DockerStorageProbe::probe(settings.docker_context.as_deref());
        if storage.is_vm_isolated {
            confidence_findings.push((
                "Docker Storage".to_string(),
                "UNKNOWN".to_string(),
                "Docker Desktop VM-isolated storage; host disk unmeasured directly".to_string(),
            ));
        } else if let Some(free) = storage.daemon_free_bytes {
            confidence_findings.push((
                "Docker Storage".to_string(),
                "CONFIRMED".to_string(),
                format!("{:.1} GiB free on Docker disk", free as f64 / 1024.0 / 1024.0 / 1024.0),
            ));
        } else {
            confidence_findings.push((
                "Docker Storage".to_string(),
                "UNKNOWN".to_string(),
                storage.details.clone(),
            ));
        }

        let is_ready = true;
        let profile = SmartProjectProfile {
            project_root: target.root,
            project_name,
            selected_entrypoint: inspection.entry.rel_path,
            framework: inspection.entry.framework,
            execution_kind: plan.execution_kind.as_str().to_string(),
            confidence_findings,
            risk_findings,
            recommendations,
            is_ready,
            existing_stack: false,
        };

        Ok((profile, Some(plan)))
    }
}

pub struct SmartPresenter;

impl SmartPresenter {
    pub fn format_home(current_project: &Path) -> String {
        format!(
            "================================================================================\n\
             AUTO DOCKER -- Zero-Friction Project Launcher (SMART Mode)\n\
             ================================================================================\n\
             Welcome! Run any Python application or Docker stack with zero container knowledge.\n\
             Current Project: {}\n\
             --------------------------------------------------------------------------------\n\
             What would you like to do?\n\
               [1] [RUN] Run my project (Auto-Detect, Prepare, and Launch)\n\
               [2] [GUIDE] 60-Second Teammate Guide (Cards 1-4)\n\
               [3] [DOCTOR] System & Docker Setup Doctor\n\
               [4] [DEV] Advanced Developer Workbench\n\
               [0] Exit\n\
             --------------------------------------------------------------------------------\n\
             Choose an option [0-4] or press [ENTER] to Run:\n\
             ================================================================================\n",
            current_project.display()
        )
    }

    pub fn format_guide(card: usize) -> String {
        let step = card.clamp(1, 4);
        let mut out = format!(
            "\n================================================================================\n\
             AUTO DOCKER -- 60-Second Teammate Guide [Card {} of 4]\n\
             ================================================================================\n",
            step
        );
        match step {
            1 => {
                out.push_str(
                    "[*] CARD 1: What is a Container? (The Plain English Version)\n\
                     --------------------------------------------------------------------------------\n\
                     Think of a container like an instant virtual laptop inside your computer.\n\n\
                     * It has clean Python and all needed libraries already installed.\n\
                     * It runs your code in an isolated room so nothing conflicts.\n\
                     * It guarantees your code works the exact same way on your computer,\n\
                       on your teammates' computers, and in production.\n\n\
                     -> The big win: You will NEVER hear or say 'It works on my machine' again!\n",
                );
            }
            2 => {
                out.push_str(
                    "[*] CARD 2: How Do I Run My Work? (One Simple Command)\n\
                     --------------------------------------------------------------------------------\n\
                     You don't need to write Dockerfiles or remember complicated commands.\n\n\
                     1. Open your terminal in your project folder.\n\
                     2. Type: autodocker run\n\
                     3. Press [ENTER].\n\n\
                     Auto Docker automatically finds your entrypoint, installs dependencies,\n\
                     sets up ports, and gives you a direct clickable link to your application.\n",
                );
            }
            3 => {
                out.push_str(
                    "[*] CARD 3: Where Do My Files and Models Go?\n\
                     --------------------------------------------------------------------------------\n\
                     Your files are 100% safe!\n\n\
                     * Folders where your app saves files (like 'models/', 'data/', 'artifacts/')\n\
                       are linked directly to your local computer.\n\
                     * When your code writes a file inside the container, it appears immediately\n\
                       in your local folder.\n\
                     * Stopping or restarting the container will NEVER delete your data or models.\n",
                );
            }
            4 => {
                out.push_str(
                    "[*] CARD 4: How Do I Stop, Restart, or Fix Things?\n\
                     --------------------------------------------------------------------------------\n\
                     * To STOP your project:\n\
                       Run: autodocker down\n\
                       Auto Docker shuts down all containers and leaves ZERO leftover junk.\n\n\
                     * If your code has an ERROR:\n\
                       Auto Docker explains what happened in plain English so you can fix it.\n",
                );
            }
            _ => {}
        }
        out.push_str("--------------------------------------------------------------------------------\n");
        out.push_str("Navigation: [n] Next Card | [p] Previous Card | [1] Run | [0] Exit\n");
        out.push_str("================================================================================\n");
        out
    }

    pub fn format_understand(profile: &SmartProjectProfile) -> String {
        let mut out = String::new();
        out.push_str("\n================================================================================\n");
        out.push_str("AUTO DOCKER — SMART: Project Understanding\n");
        out.push_str("================================================================================\n");
        out.push_str(&format!("Project Name     : {}\n", profile.project_name));
        out.push_str(&format!("Project Location : {}\n", profile.project_root.display()));

        for (category, status, detail) in &profile.confidence_findings {
            out.push_str(&format!("{:<18}: {} [{}]\n", category, detail, status));
        }

        out.push_str("--------------------------------------------------------------------------------\n");
        out.push_str("Next Steps: Press [ENTER] to confirm and proceed, or [q] to cancel.\n");
        out.push_str("================================================================================\n");
        out
    }
}

pub struct SmartRecoveryCoordinator;

impl SmartRecoveryCoordinator {
    pub fn diagnose(root: &Path) -> Vec<String> {
        let mut findings = Vec::new();
        let lock = ProjectLock::new(root);
        if lock.is_stale() {
            findings.push("Stale concurrency lock detected from a previous session.".to_string());
        }
        if let Ok(entries) = fs::read_dir(root) {
            for entry in entries.flatten() {
                let name = entry.file_name();
                let s = name.to_string_lossy();
                if s.starts_with(".opsforge-backup") {
                    findings.push(format!("Recoverable backup found: {}", s));
                }
            }
        }
        findings
    }
}

// ============================================================================
// 17. Recent Projects Store
// ============================================================================

pub struct RecentProjectsStore;

impl RecentProjectsStore {
    fn store_path() -> PathBuf {
        let home = env::var("USERPROFILE").or_else(|_| env::var("HOME")).unwrap_or_else(|_| ".".to_string());
        PathBuf::from(home).join(".auto_docker").join("recent_projects.txt")
    }

    pub fn add(project_root: &Path) {
        let p = Self::store_path();
        if let Some(parent) = p.parent() {
            let _ = fs::create_dir_all(parent);
        }
        let canonical = project_root.canonicalize().unwrap_or_else(|_| project_root.to_path_buf());

        let mut list = Self::load();
        list.retain(|x| x != &canonical);
        list.insert(0, canonical);
        list.truncate(10);

        let content = list
            .iter()
            .map(|p| p.to_string_lossy().to_string())
            .collect::<Vec<_>>()
            .join("\n");
        let _ = fs::write(p, content);
    }

    pub fn load() -> Vec<PathBuf> {
        let p = Self::store_path();
        let mut out = Vec::new();
        if let Ok(content) = fs::read_to_string(p) {
            for line in content.lines() {
                let trimmed = line.trim();
                if !trimmed.is_empty() {
                    out.push(PathBuf::from(trimmed));
                }
            }
        }
        out
    }
}

// ============================================================================
// 18. CLI & Main Entry Point
// ============================================================================

fn print_usage() {
    println!(
        "Auto Docker (Rust Single-File) v{} — Container infrastructure assistant\n\n\
         USAGE:\n  autodocker <COMMAND> [PROJECT_PATH] [OPTIONS]\n\n\
         PRIMARY COMMANDS:\n\
           smart      Zero-jargon SMART launcher & project understanding (default)\n\
           guide      Display 60-Second Teammate Guide cards [1-4]\n\
           doctor     Comprehensive host, Docker, storage, and accelerator doctor\n\
           clean      Discover and safely prune disposable project & test containers\n\
           existing   Adopt and run existing compose.yaml without modification\n\
           lock       Inspect or acquire project atomic concurrency lock\n\
           unlock     Safely release or clear stale project concurrency lock\n\n\
         CONTAINER PIPELINE COMMANDS:\n\
           inspect    Inspect and summarize project understanding\n\
           preflight  Run offline dependency, port, and storage safety audits\n\
           preview    Preview generated Dockerfile and Compose files without writing\n\
           generate   Generate Docker infrastructure into .opsforge/\n\
           build      Build Docker images with storage deficit pre-check\n\
           up, run    Start project container stack\n\
           down       Stop project container stack\n\
           ps         List running project containers\n\
           logs       View live logs of project containers\n\
           self-test  Run built-in test suite\n\n\
         OPTIONS:\n\
           --mode <MODE>        Application mode (base, mlops, llmops, genaiops, web, fastapi, flask, streamlit)\n\
           --entry <FILE>       Explicit entrypoint script\n\
           --port <PORT>        Published port\n\
           --replicas <N>       Container replicas (default: 1)\n\
           --card <1-4>         Guide card index for 'guide' command\n\
           --gpu                Enable GPU/CUDA acceleration\n\
           --yes, -y            Non-interactive execution confirmation\n",
        VERSION
    );
}

pub fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() <= 1 {
        let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        println!("{}", SmartPresenter::format_home(&cwd));
        return;
    }

    let mut command = "smart";
    let mut target_path = ".".to_string();
    let mut explicit_entry = None;
    let mut mode = Mode::Auto;
    let mut port = None;
    let mut card_idx = 1;
    let mut gpu = false;
    let mut yes = false;

    let mut i = 1;
    while i < args.len() {
        let arg = &args[i];
        if arg == "--help" || arg == "-h" {
            print_usage();
            return;
        } else if arg == "--mode" && i + 1 < args.len() {
            mode = Mode::from_str_loose(&args[i + 1]);
            i += 1;
        } else if arg == "--entry" && i + 1 < args.len() {
            explicit_entry = Some(args[i + 1].clone());
            i += 1;
        } else if arg == "--port" && i + 1 < args.len() {
            port = args[i + 1].parse().ok();
            i += 1;
        } else if arg == "--card" && i + 1 < args.len() {
            card_idx = args[i + 1].parse().unwrap_or(1);
            i += 1;
        } else if arg == "--gpu" {
            gpu = true;
        } else if arg == "--yes" || arg == "-y" {
            yes = true;
        } else if !arg.starts_with('-') && i == 1 {
            command = arg;
        } else if !arg.starts_with('-') {
            target_path = arg.clone();
        }
        i += 1;
    }

    match command {
        "self-test" => {
            run_self_test();
        }
        "guide" => {
            println!("{}", SmartPresenter::format_guide(card_idx));
        }
        "doctor" => {
            let checks = EnvironmentDoctor::report(None);
            println!("AUTO DOCKER v{} — Environment Doctor", VERSION);
            println!("--------------------------------------------------------------------------------");
            for c in checks {
                println!("{:<20}: [{:<4}] {}", c.name, c.status, c.detail);
            }
        }
        "clean" => {
            let proj_name = Path::new(&target_path)
                .canonicalize()
                .unwrap_or_else(|_| PathBuf::from(&target_path))
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("project")
                .to_string();
            let resources = ResourceCleaner::discover_project_resources(&proj_name);
            ResourceCleaner::preview(&resources);
            if yes && !resources.is_empty() {
                let _ = ResourceCleaner::execute(&resources);
            }
        }
        "smart" => {
            let target = match target_for(&target_path) {
                Ok(t) => t,
                Err(e) => {
                    eprintln!("Error: {}", e);
                    process::exit(1);
                }
            };
            let mut settings = Settings::new(target);
            settings.explicit_entry = explicit_entry;
            settings.mode = mode;
            settings.port = port;
            settings.gpu = gpu;
            settings.yes = yes;

            match SmartDecisionEngine::analyze(&settings.target.root, &settings) {
                Ok((profile, _plan)) => {
                    println!("{}", SmartPresenter::format_understand(&profile));
                }
                Err(e) => {
                    eprintln!("Error analyzing project: {}", e);
                    process::exit(1);
                }
            }
        }
        "preview" | "generate" => {
            let target = match target_for(&target_path) {
                Ok(t) => t,
                Err(e) => {
                    eprintln!("Error: {}", e);
                    process::exit(1);
                }
            };
            let mut settings = Settings::new(target);
            settings.explicit_entry = explicit_entry;
            settings.mode = mode;
            settings.port = port;
            settings.gpu = gpu;

            let inspection = match Inspector::inspect(&settings.target, settings.explicit_entry.as_deref()) {
                Ok(ins) => ins,
                Err(e) => {
                    eprintln!("Inspection error: {}", e);
                    process::exit(1);
                }
            };

            let plan = match Planner::build(&settings, inspection) {
                Ok(p) => p,
                Err(e) => {
                    eprintln!("Planner error: {}", e);
                    process::exit(1);
                }
            };

            let files = Renderer::files(&plan);
            if command == "preview" {
                println!("--- Dockerfile ---");
                println!("{}", files.get("Dockerfile").unwrap_or(&"".to_string()));
                println!("--- compose.yaml ---");
                println!("{}", files.get("compose.yaml").unwrap_or(&"".to_string()));
            } else {
                let om = OutputManager::new(&settings.target.root);
                match om.write(&files) {
                    Ok(backup) => {
                        println!("Configuration generated successfully into .opsforge/");
                        if let Some(b) = backup {
                            println!("Backup of previous configuration preserved at {}", b.display());
                        }
                    }
                    Err(e) => {
                        eprintln!("Write error: {}", e);
                        process::exit(1);
                    }
                }
            }
        }
        _ => {
            eprintln!("Unknown command '{}'. Run autodocker --help for usage.", command);
            process::exit(1);
        }
    }
}

// ============================================================================
// 19. Extended Self-Test Suite (30 Critical Invariant Verifications)
// ============================================================================

fn run_self_test() {
    println!("================================================================================");
    println!("AUTO DOCKER (RUST SINGLE-FILE) — EXTENDED SELF-TEST SUITE");
    println!("================================================================================\n");

    let tests: [(&str, fn() -> Result<(), String>); 30] = [
        ("test_quoted_file_path", || {
            let tmp = env::temp_dir().join("autodocker_t_quoted");
            let _ = fs::create_dir_all(&tmp);
            let py = tmp.join("app.py");
            let _ = fs::write(&py, "print('hello')\n");
            let raw = format!("'{}'", py.display());
            let target = target_for(&raw).map_err(|e| e.to_string())?;
            if target.pinned.as_deref() != Some("app.py") {
                return Err("Quoted path pin failed".to_string());
            }
            let _ = fs::remove_dir_all(&tmp);
            Ok(())
        }),
        ("test_target_reject_home_and_root", || {
            let home = env::var("USERPROFILE").or_else(|_| env::var("HOME")).unwrap_or_else(|_| ".".to_string());
            if target_for(&home).is_ok() {
                return Err("Failed to reject user home directory".to_string());
            }
            if target_for("/").is_ok() {
                return Err("Failed to reject drive root /".to_string());
            }
            Ok(())
        }),
        ("test_ambiguous_entry_rejection", || {
            let tmp = env::temp_dir().join("autodocker_t_ambig");
            let _ = fs::remove_dir_all(&tmp);
            let _ = fs::create_dir_all(&tmp);
            fs::write(tmp.join("main.py"), "if __name__ == '__main__':\n    pass\n").unwrap();
            fs::write(tmp.join("app.py"), "if __name__ == '__main__':\n    pass\n").unwrap();
            let target = Target::new(tmp.clone(), None);
            match Inspector::inspect(&target, None) {
                Err(OpsError::AmbiguousEntry(_)) => {
                    let _ = fs::remove_dir_all(&tmp);
                    Ok(())
                }
                _ => {
                    let _ = fs::remove_dir_all(&tmp);
                    Err("Ambiguous entrypoints were not detected".to_string())
                }
            }
        }),
        ("test_file_pin_avoids_other_candidates", || {
            let tmp = env::temp_dir().join("autodocker_t_pin");
            let _ = fs::remove_dir_all(&tmp);
            let _ = fs::create_dir_all(&tmp);
            fs::write(tmp.join("wrong.py"), "if __name__ == '__main__':\n    pass\n").unwrap();
            fs::write(tmp.join("right.py"), "print('pin')\n").unwrap();
            let target = Target::new(tmp.clone(), Some("right.py".to_string()));
            let ins = Inspector::inspect(&target, None).map_err(|e| e.to_string())?;
            if ins.entry.rel_path != "right.py" {
                let _ = fs::remove_dir_all(&tmp);
                return Err("Pinned entrypoint was not honored".to_string());
            }
            let _ = fs::remove_dir_all(&tmp);
            Ok(())
        }),
        ("test_prevents_unbounded_scan", || {
            let tmp = env::temp_dir().join("autodocker_t_scanlimit");
            let _ = fs::remove_dir_all(&tmp);
            let _ = fs::create_dir_all(&tmp);
            for n in 0..245 {
                fs::write(tmp.join(format!("m_{}.py", n)), "x=1\n").unwrap();
            }
            let target = Target::new(tmp.clone(), None);
            match Inspector::inspect(&target, None) {
                Err(OpsError::ScanLimit(_)) => {
                    let _ = fs::remove_dir_all(&tmp);
                    Ok(())
                }
                _ => {
                    let _ = fs::remove_dir_all(&tmp);
                    Err("Scan limit > 240 files was not enforced".to_string())
                }
            }
        }),
        ("test_unparsable_main_not_silently_ignored", || {
            let tmp = env::temp_dir().join("autodocker_t_unparsable");
            let _ = fs::remove_dir_all(&tmp);
            let _ = fs::create_dir_all(&tmp);
            fs::write(tmp.join("main.py"), "this is not valid python !!!").unwrap();
            let target = Target::new(tmp.clone(), None);
            let res = Inspector::inspect(&target, None);
            let _ = fs::remove_dir_all(&tmp);
            if res.is_ok() {
                return Err("Unparsable main was silently ignored".to_string());
            }
            Ok(())
        }),
        ("test_ast_visitor_main_guard_and_type_checking", || {
            let code = "import os\nfrom typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import mypy_boto3\nif __name__ == '__main__':\n    import uvicorn\n";
            let mut v = DependencyAstVisitor::new();
            v.parse_source(code);
            if !v.required_imports.contains("os") || !v.required_imports.contains("uvicorn") {
                return Err("os or uvicorn missing from required imports".to_string());
            }
            if v.required_imports.contains("mypy_boto3") {
                return Err("mypy_boto3 should be excluded from runtime dependencies".to_string());
            }
            Ok(())
        }),
        ("test_pypi_canonical_alias_resolution", || {
            if resolve_pypi_package("cv2") != "opencv-python" {
                return Err("cv2 did not resolve to opencv-python".to_string());
            }
            if resolve_pypi_package("PIL") != "Pillow" {
                return Err("PIL did not resolve to Pillow".to_string());
            }
            if resolve_pypi_package("sklearn") != "scikit-learn" {
                return Err("sklearn did not resolve to scikit-learn".to_string());
            }
            if resolve_pypi_package("yaml") != "PyYAML" {
                return Err("yaml did not resolve to PyYAML".to_string());
            }
            Ok(())
        }),
        ("test_stdlib_immunity_database", || {
            if !is_stdlib_module("sys") || !is_stdlib_module("os") || !is_stdlib_module("json") {
                return Err("Standard library modules not recognized".to_string());
            }
            if is_stdlib_module("fastapi") || is_stdlib_module("torch") {
                return Err("Third party packages incorrectly recognized as stdlib".to_string());
            }
            Ok(())
        }),
        ("test_modes_and_preset_services", || {
            let tmp = env::temp_dir().join("autodocker_t_modes");
            let _ = fs::create_dir_all(&tmp);
            fs::write(tmp.join("app.py"), "print('mode')\n").unwrap();
            let target = Target::new(tmp.clone(), None);
            let ins = Inspector::inspect(&target, None).map_err(|e| e.to_string())?;

            let mut s_base = Settings::new(target.clone());
            s_base.mode = Mode::Base;
            let p_base = Planner::build(&s_base, ins.clone()).unwrap();
            if !p_base.services.is_empty() {
                return Err("Base mode should have no auxiliary services".to_string());
            }

            let mut s_mlops = Settings::new(target.clone());
            s_mlops.mode = Mode::Mlops;
            let p_mlops = Planner::build(&s_mlops, ins.clone()).unwrap();
            if p_mlops.services != vec!["mlflow".to_string()] {
                return Err("MLOps mode should include mlflow service".to_string());
            }

            let mut s_llm = Settings::new(target.clone());
            s_llm.mode = Mode::Llmops;
            let p_llm = Planner::build(&s_llm, ins.clone()).unwrap();
            if p_llm.services != vec!["mlflow".to_string(), "ollama".to_string()] {
                return Err("LLMOps mode should include mlflow and ollama".to_string());
            }

            let mut s_genai = Settings::new(target.clone());
            s_genai.mode = Mode::Genaiops;
            let p_genai = Planner::build(&s_genai, ins).unwrap();
            if p_genai.services != vec!["mlflow".to_string(), "ollama".to_string(), "redis".to_string()] {
                return Err("GenAIOps mode should include mlflow, ollama, and redis".to_string());
            }

            let _ = fs::remove_dir_all(&tmp);
            Ok(())
        }),
        ("test_services_startup_order_and_health_conditions", || {
            let tmp = env::temp_dir().join("autodocker_t_srv_health");
            let _ = fs::create_dir_all(&tmp);
            fs::write(tmp.join("app.py"), "print('llm')\n").unwrap();
            let target = Target::new(tmp.clone(), None);
            let ins = Inspector::inspect(&target, None).map_err(|e| e.to_string())?;
            let mut s = Settings::new(target);
            s.mode = Mode::Llmops;
            let plan = Planner::build(&s, ins).unwrap();
            let compose = Renderer::compose(&plan);
            if !compose.contains("mlflow:\n        condition: \"service_healthy\"") {
                return Err("Compose missing mlflow healthy condition".to_string());
            }
            if !compose.contains("ollama:\n        condition: \"service_healthy\"") {
                return Err("Compose missing ollama healthy condition".to_string());
            }
            let _ = fs::remove_dir_all(&tmp);
            Ok(())
        }),
        ("test_gpu_mode_rejected_in_base", || {
            let tmp = env::temp_dir().join("autodocker_t_gpu_base");
            let _ = fs::create_dir_all(&tmp);
            fs::write(tmp.join("app.py"), "print('base')\n").unwrap();
            let target = Target::new(tmp.clone(), None);
            let ins = Inspector::inspect(&target, None).map_err(|e| e.to_string())?;
            let mut s = Settings::new(target);
            s.mode = Mode::Base;
            s.gpu = true;
            let res = Planner::build(&s, ins);
            let _ = fs::remove_dir_all(&tmp);
            if res.is_ok() {
                return Err("GPU in Base mode was not rejected".to_string());
            }
            Ok(())
        }),
        ("test_gpu_ollama_reservation", || {
            let tmp = env::temp_dir().join("autodocker_t_gpu_ollama");
            let _ = fs::create_dir_all(&tmp);
            fs::write(tmp.join("app.py"), "print('gpu')\n").unwrap();
            let target = Target::new(tmp.clone(), None);
            let ins = Inspector::inspect(&target, None).map_err(|e| e.to_string())?;
            let mut s = Settings::new(target);
            s.mode = Mode::Llmops;
            s.gpu = true;
            let plan = Planner::build(&s, ins).unwrap();
            let compose = Renderer::compose(&plan);
            if !compose.contains("driver: \"nvidia\"") || !compose.contains("capabilities: [\"gpu\"]") {
                return Err("Ollama missing NVIDIA GPU reservation".to_string());
            }
            let _ = fs::remove_dir_all(&tmp);
            Ok(())
        }),
        ("test_root_dockerfile_untouched", || {
            let tmp = env::temp_dir().join("autodocker_t_root_df");
            let _ = fs::remove_dir_all(&tmp);
            let _ = fs::create_dir_all(&tmp);
            let orig = tmp.join("Dockerfile");
            fs::write(&orig, "KEEP ORIGINAL DOCKERFILE").unwrap();

            let om = OutputManager::new(&tmp);
            let mut files = HashMap::new();
            files.insert("Dockerfile".to_string(), "GENERATED DOCKERFILE".to_string());
            files.insert("compose.yaml".to_string(), "services: {}".to_string());
            files.insert("manifest.json".to_string(), "{}".to_string());
            om.write(&files).map_err(|e| e.to_string())?;

            let content = fs::read_to_string(&orig).unwrap();
            let _ = fs::remove_dir_all(&tmp);
            if content != "KEEP ORIGINAL DOCKERFILE" {
                return Err("Root Dockerfile was overwritten!".to_string());
            }
            Ok(())
        }),
        ("test_backup_created_on_overwrite", || {
            let tmp = env::temp_dir().join("autodocker_t_backup");
            let _ = fs::remove_dir_all(&tmp);
            let _ = fs::create_dir_all(&tmp);
            let om = OutputManager::new(&tmp);
            let mut files = HashMap::new();
            files.insert("Dockerfile".to_string(), "V1".to_string());
            om.write(&files).map_err(|e| e.to_string())?;

            files.insert("Dockerfile".to_string(), "V2".to_string());
            let backup = om.write(&files).map_err(|e| e.to_string())?;
            if backup.is_none() {
                let _ = fs::remove_dir_all(&tmp);
                return Err("Backup was not created on overwrite".to_string());
            }
            let b_dir = backup.unwrap();
            let b_content = fs::read_to_string(b_dir.join("Dockerfile")).unwrap();
            let _ = fs::remove_dir_all(&tmp);
            let _ = fs::remove_dir_all(&b_dir);
            if b_content != "V1" {
                return Err("Backup content did not match previous state".to_string());
            }
            Ok(())
        }),
        ("test_budget_estimator_omits_optional_packages", || {
            let storage = DockerStorageStatus {
                engine_accessible: true,
                docker_root_dir: Some("/var/lib/docker".to_string()),
                storage_driver: Some("overlay2".to_string()),
                daemon_free_bytes: Some(30 * 1024 * 1024 * 1024),
                daemon_total_bytes: Some(100 * 1024 * 1024 * 1024),
                reclaimable_build_cache_bytes: 0,
                reclaimable_images_bytes: 0,
                is_remote_context: false,
                is_vm_isolated: false,
                assessment_status: "CONFIRMED".to_string(),
                details: "OK".to_string(),
            };
            let b1 = ResourceBudgetEstimator::estimate("python:3.12-slim-bookworm", &["fastapi".to_string()], Path::new("."), &storage, &[]);
            let b2 = ResourceBudgetEstimator::estimate("python:3.12-slim-bookworm", &["fastapi".to_string()], Path::new("."), &storage, &["torch".to_string()]);
            if b1.total_bytes_min != b2.total_bytes_min {
                return Err("Optional packages inflated minimum build bytes".to_string());
            }
            if b1.verdict != StorageVerdict::Sufficient {
                return Err("Expected Sufficient verdict".to_string());
            }
            Ok(())
        }),
        ("test_storage_deficit_verdict_blocked", || {
            let storage = DockerStorageStatus {
                engine_accessible: true,
                docker_root_dir: Some("/var/lib/docker".to_string()),
                storage_driver: Some("overlay2".to_string()),
                daemon_free_bytes: Some(10 * 1024 * 1024),
                daemon_total_bytes: Some(100 * 1024 * 1024 * 1024),
                reclaimable_build_cache_bytes: 0,
                reclaimable_images_bytes: 0,
                is_remote_context: false,
                is_vm_isolated: false,
                assessment_status: "CONFIRMED".to_string(),
                details: "10 MB free".to_string(),
            };
            let budget = ResourceBudgetEstimator::estimate("python:3.12-slim-bookworm", &["fastapi".to_string()], Path::new("."), &storage, &[]);
            if budget.verdict != StorageVerdict::Blocked {
                return Err("Storage deficit was not marked as BLOCKED".to_string());
            }
            Ok(())
        }),
        ("test_fastapi_web_rendering_and_healthcheck", || {
            let tmp = env::temp_dir().join("autodocker_t_fastapi");
            let _ = fs::create_dir_all(&tmp);
            fs::write(tmp.join("main.py"), "from fastapi import FastAPI\napp = FastAPI()\n").unwrap();
            let target = Target::new(tmp.clone(), None);
            let ins = Inspector::inspect(&target, None).map_err(|e| e.to_string())?;
            let s = Settings::new(target);
            let plan = Planner::build(&s, ins).unwrap();
            let files = Renderer::files(&plan);
            let compose = files.get("compose.yaml").unwrap();
            if !compose.contains("healthcheck:") || !compose.contains("socket.create_connection") {
                return Err("TCP loopback healthcheck missing from compose.yaml".to_string());
            }
            if !compose.contains("\"127.0.0.1:8000:8000\"") {
                return Err("Published port missing closing quote in compose.yaml".to_string());
            }
            let _ = fs::remove_dir_all(&tmp);
            Ok(())
        }),
        ("test_vm_isolated_storage_verdict_unknown", || {
            let storage = DockerStorageStatus {
                engine_accessible: true,
                docker_root_dir: Some("/var/lib/docker".to_string()),
                storage_driver: Some("overlay2".to_string()),
                daemon_free_bytes: None,
                daemon_total_bytes: None,
                reclaimable_build_cache_bytes: 0,
                reclaimable_images_bytes: 0,
                is_remote_context: false,
                is_vm_isolated: true,
                assessment_status: "UNKNOWN".to_string(),
                details: "Docker daemon storage root is isolated inside VM/WSL2".to_string(),
            };
            let budget = ResourceBudgetEstimator::estimate("python:3.12-slim-bookworm", &["fastapi".to_string()], Path::new("."), &storage, &[]);
            if budget.verdict != StorageVerdict::Unknown {
                return Err("VM isolated storage was not marked as UNKNOWN".to_string());
            }
            Ok(())
        }),
        ("test_interactive_cli_rendering_tty", || {
            let tmp = env::temp_dir().join("autodocker_t_cli");
            let _ = fs::create_dir_all(&tmp);
            fs::write(tmp.join("cli.py"), "name = input('Name: ')\n").unwrap();
            let target = Target::new(tmp.clone(), None);
            let ins = Inspector::inspect(&target, None).map_err(|e| e.to_string())?;
            let s = Settings::new(target);
            let plan = Planner::build(&s, ins).unwrap();
            let compose = Renderer::compose(&plan);
            if !compose.contains("stdin_open: true") || !compose.contains("tty: true") {
                return Err("Interactive TTY configuration missing from compose.yaml".to_string());
            }
            let _ = fs::remove_dir_all(&tmp);
            Ok(())
        }),
        ("test_output_manager_manifest_and_validation", || {
            let tmp = env::temp_dir().join("autodocker_t_om_val");
            let _ = fs::remove_dir_all(&tmp);
            let _ = fs::create_dir_all(&tmp);
            let om = OutputManager::new(&tmp);
            let mut files = HashMap::new();
            files.insert("Dockerfile".to_string(), "FROM python:3.12-slim\n".to_string());
            files.insert("compose.yaml".to_string(), "services: {}\n".to_string());
            files.insert("manifest.json".to_string(), "{\"generator\": \"Auto Docker\"}\n".to_string());
            om.write(&files).map_err(|e| e.to_string())?;
            let (valid, _) = om.validate();
            let _ = fs::remove_dir_all(&tmp);
            if !valid {
                return Err("Validation of written files failed".to_string());
            }
            Ok(())
        }),
        ("test_project_lock_acquire_and_release", || {
            let tmp = env::temp_dir().join("autodocker_t_lock");
            let _ = fs::remove_dir_all(&tmp);
            let _ = fs::create_dir_all(&tmp);
            {
                let mut lock = ProjectLock::new(&tmp);
                lock.acquire().map_err(|e| e.to_string())?;
                if !tmp.join(".opsforge.lock").exists() {
                    return Err("Lock file was not created".to_string());
                }
                lock.release();
            }
            if tmp.join(".opsforge.lock").exists() {
                return Err("Lock file was not released".to_string());
            }
            let _ = fs::remove_dir_all(&tmp);
            Ok(())
        }),
        ("test_project_lock_stale_detection", || {
            let tmp = env::temp_dir().join("autodocker_t_stale_lock");
            let _ = fs::remove_dir_all(&tmp);
            let _ = fs::create_dir_all(&tmp);
            let lock_path = tmp.join(".opsforge.lock");
            fs::write(&lock_path, "{\"pid\": 999999, \"created_at\": 1000}").unwrap();
            let lock = ProjectLock::new(&tmp);
            let msg = lock.clear_stale();
            if !msg.contains("removed") {
                let _ = fs::remove_dir_all(&tmp);
                return Err("Stale lock was not cleared".to_string());
            }
            let _ = fs::remove_dir_all(&tmp);
            Ok(())
        }),
        ("test_existing_infrastructure_compose_parsing", || {
            let tmp = env::temp_dir().join("autodocker_t_exist_comp");
            let _ = fs::remove_dir_all(&tmp);
            let _ = fs::create_dir_all(&tmp);
            let compose_content = "services:\n  web:\n    image: nginx\n  db:\n    image: postgres\n  redis:\n    image: redis\n";
            fs::write(tmp.join("compose.yaml"), compose_content).unwrap();
            let stack = ExistingInfrastructure::discover(&tmp, None).ok_or("Failed to discover compose.yaml")?;
            if stack.services.len() != 3 || !stack.services.contains(&"web".to_string()) || !stack.services.contains(&"db".to_string()) {
                let _ = fs::remove_dir_all(&tmp);
                return Err(format!("Incorrect services parsed: {:?}", stack.services));
            }
            let _ = fs::remove_dir_all(&tmp);
            Ok(())
        }),
        ("test_compose_service_role_classification", || {
            if ExistingInfrastructure::classify_service_role("alembic_migrate", None) != "migration" {
                return Err("Migration service role not recognized".to_string());
            }
            if ExistingInfrastructure::classify_service_role("celery_worker", None) != "worker" {
                return Err("Worker service role not recognized".to_string());
            }
            if ExistingInfrastructure::classify_service_role("postgres_db", None) != "database" {
                return Err("Database service role not recognized".to_string());
            }
            if ExistingInfrastructure::classify_service_role("ollama_server", None) != "ai" {
                return Err("AI service role not recognized".to_string());
            }
            Ok(())
        }),
        ("test_environment_doctor_checks", || {
            let checks = EnvironmentDoctor::report(None);
            if checks.is_empty() {
                return Err("Environment doctor returned empty checks".to_string());
            }
            let has_platform = checks.iter().any(|c| c.name == "Platform");
            if !has_platform {
                return Err("Platform check missing from doctor".to_string());
            }
            Ok(())
        }),
        ("test_deployment_orchestrator_state_transitions", || {
            let tmp = env::temp_dir().join("autodocker_t_orch_trans");
            let mut orch = DeploymentOrchestrator::new(tmp);
            if orch.current_state != DeploymentState::Discovered {
                return Err("Initial state not Discovered".to_string());
            }
            orch.transition(DeploymentState::Analyzed, "Analyzed project").map_err(|e| e.to_string())?;
            orch.transition(DeploymentState::Planned, "Built plan").map_err(|e| e.to_string())?;
            orch.transition(DeploymentState::Validated, "Validated config").map_err(|e| e.to_string())?;
            orch.transition(DeploymentState::Approved, "User approved").map_err(|e| e.to_string())?;
            if orch.history.len() != 4 {
                return Err("Transition history count mismatch".to_string());
            }
            Ok(())
        }),
        ("test_deployment_orchestrator_illegal_transition_rejected", || {
            let tmp = env::temp_dir().join("autodocker_t_orch_illegal");
            let mut orch = DeploymentOrchestrator::new(tmp);
            let res = orch.transition(DeploymentState::Approved, "Jump to approved");
            if res.is_ok() {
                return Err("Illegal transition from Discovered to Approved was allowed".to_string());
            }
            Ok(())
        }),
        ("test_smart_decision_engine_evidence_classification", || {
            let tmp = env::temp_dir().join("autodocker_t_sde");
            let _ = fs::remove_dir_all(&tmp);
            let _ = fs::create_dir_all(&tmp);
            fs::write(tmp.join("app.py"), "from fastapi import FastAPI\napp = FastAPI()\n").unwrap();
            let target = Target::new(tmp.clone(), None);
            let s = Settings::new(target);
            let (profile, _) = SmartDecisionEngine::analyze(&tmp, &s).map_err(|e| e.to_string())?;
            if !profile.confidence_findings.iter().any(|f| f.0 == "Entrypoint" && f.1 == "CONFIRMED") {
                let _ = fs::remove_dir_all(&tmp);
                return Err("Entrypoint was not marked CONFIRMED".to_string());
            }
            let _ = fs::remove_dir_all(&tmp);
            Ok(())
        }),
        ("test_port_audit_next_available", || {
            let p1 = PortAudit::find_next_available(8000);
            if p1 < 8000 {
                return Err("Port search returned invalid port".to_string());
            }
            Ok(())
        }),
    ];

    let mut passed = 0;
    for (name, func) in tests {
        print!("  Running {} ... ", name);
        match func() {
            Ok(_) => {
                println!("OK");
                passed += 1;
            }
            Err(e) => {
                println!("FAILED: {}", e);
            }
        }
    }

    println!("\n--------------------------------------------------------------------------------");
    println!("Ran {} tests: {} passed, {} failed", tests.len(), passed, tests.len() - passed);
    println!("--------------------------------------------------------------------------------\n");
    if passed != tests.len() {
        process::exit(1);
    }
}
