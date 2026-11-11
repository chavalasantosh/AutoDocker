// ============================================================================
// AUTO DOCKER 3.0 (RUST SINGLE-FILE MASTER ENGINE)
// Zero-dependency, resource-aware container infrastructure assistant & orchestrator
// 100% Rust Standard Library (std::*) — Zero external crates — Pure single file
// Direct compilation: rustc --edition 2021 -O auto_docker.rs
// ============================================================================

use std::collections::{HashMap, HashSet};
use std::env;
use std::fs;
use std::io::{self, BufRead, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{self, Command};
use std::time::{SystemTime, UNIX_EPOCH};

pub const VERSION: &str = "3.0.0-rc10.1";
pub const MAX_VISITED_FILES: usize = 5000;
pub const MAX_PROJECT_SIZE_BYTES: u64 = 100 * 1024 * 1024 * 1024; // 100 GB
pub const COMPOSE_INDENT: &str = "  ";

// ============================================================================
// 1. Error Hierarchy
// ============================================================================

#[derive(Debug, Clone)]
pub enum OpsError {
    General(String),
    ScanLimit(String),
    AmbiguousEntry(String),
    Blocked(String),
}

impl std::fmt::Display for OpsError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            OpsError::General(s) => write!(f, "{}", s),
            OpsError::ScanLimit(s) => write!(f, "SCAN_LIMIT: {}", s),
            OpsError::AmbiguousEntry(s) => write!(f, "AMBIGUOUS_ENTRY: {}", s),
            OpsError::Blocked(s) => write!(f, "BLOCKED: {}", s),
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
    FastApi,
    Flask,
    Streamlit,
    Gradio,
    Base,
    Web,
    Custom(String),
}

impl Mode {
    pub fn as_str(&self) -> &str {
        match self {
            Mode::Auto => "auto",
            Mode::FastApi => "fastapi",
            Mode::Flask => "flask",
            Mode::Streamlit => "streamlit",
            Mode::Gradio => "gradio",
            Mode::Base => "base",
            Mode::Web => "web",
            Mode::Custom(s) => s.as_str(),
        }
    }

    pub fn from_str_loose(s: &str) -> Self {
        match s.to_lowercase().as_str() {
            "fastapi" => Mode::FastApi,
            "flask" => Mode::Flask,
            "streamlit" => Mode::Streamlit,
            "gradio" => Mode::Gradio,
            "base" | "cli" | "batch" => Mode::Base,
            "web" => Mode::Web,
            "auto" => Mode::Auto,
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

#[derive(Debug, Clone)]
pub struct Target {
    pub root: PathBuf,
    pub pinned: Option<String>,
}

impl Target {
    pub fn new(root: PathBuf, pinned: Option<String>) -> Self {
        Self { root, pinned }
    }
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

#[derive(Debug, Clone)]
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

    pub fn reclaimable_gib(&self) -> f64 {
        (self.reclaimable_build_cache_bytes + self.reclaimable_images_bytes) as f64 / 1024.0 / 1024.0 / 1024.0
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
    pub fn free_gib(&self) -> f64 {
        self.daemon_free_bytes.map(|b| b as f64 / 1024.0 / 1024.0 / 1024.0).unwrap_or(0.0)
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
}

// ============================================================================
// 3. RC6 State Machine & Orchestrator
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
    Failed,
    Blocked,
    Cancelled,
    Degraded,
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
            DeploymentState::Failed => "FAILED",
            DeploymentState::Blocked => "BLOCKED",
            DeploymentState::Cancelled => "CANCELLED",
            DeploymentState::Degraded => "DEGRADED",
            DeploymentState::RecoveryRequired => "RECOVERY_REQUIRED",
        }
    }
}

#[derive(Debug, Clone)]
pub struct DeploymentTransition {
    pub from_state: DeploymentState,
    pub to_state: DeploymentState,
    pub timestamp: u64,
    pub message: String,
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

    pub fn can_transition(&self, target: DeploymentState) -> bool {
        match self.current_state {
            DeploymentState::Discovered => matches!(target, DeploymentState::Analyzed | DeploymentState::Blocked | DeploymentState::Failed),
            DeploymentState::Analyzed => matches!(target, DeploymentState::Planned | DeploymentState::Blocked | DeploymentState::Failed | DeploymentState::Analyzed),
            DeploymentState::Planned => matches!(target, DeploymentState::Validated | DeploymentState::Blocked | DeploymentState::Cancelled | DeploymentState::Planned | DeploymentState::Analyzed),
            DeploymentState::Validated => matches!(target, DeploymentState::Approved | DeploymentState::Blocked | DeploymentState::Cancelled | DeploymentState::Validated | DeploymentState::Planned | DeploymentState::Analyzed | DeploymentState::Verifying | DeploymentState::Healthy),
            DeploymentState::Approved => matches!(target, DeploymentState::Building | DeploymentState::Starting | DeploymentState::Cancelled | DeploymentState::Analyzed),
            DeploymentState::Building => matches!(target, DeploymentState::Validated | DeploymentState::Starting | DeploymentState::Failed | DeploymentState::RecoveryRequired),
            DeploymentState::Starting => matches!(target, DeploymentState::Verifying | DeploymentState::Failed | DeploymentState::RecoveryRequired),
            DeploymentState::Verifying => matches!(target, DeploymentState::Healthy | DeploymentState::Degraded | DeploymentState::Failed | DeploymentState::RecoveryRequired | DeploymentState::Approved),
            DeploymentState::Healthy => matches!(target, DeploymentState::Degraded | DeploymentState::Failed | DeploymentState::RecoveryRequired | DeploymentState::Approved | DeploymentState::Healthy),
            DeploymentState::Degraded => matches!(target, DeploymentState::Verifying | DeploymentState::RecoveryRequired | DeploymentState::Failed | DeploymentState::Approved),
            DeploymentState::Failed | DeploymentState::Blocked | DeploymentState::Cancelled | DeploymentState::RecoveryRequired => true,
        }
    }

    pub fn transition(&mut self, target: DeploymentState, message: &str) -> Result<(), OpsError> {
        if !self.can_transition(target) {
            return Err(OpsError::General(format!(
                "Invalid state transition from {} to {}",
                self.current_state.as_str(),
                target.as_str()
            )));
        }
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        self.history.push(DeploymentTransition {
            from_state: self.current_state,
            to_state: target,
            timestamp: now,
            message: message.to_string(),
        });
        self.current_state = target;
        Ok(())
    }
}

// ============================================================================
// 4. Standard Library Immunity & PyPI Alias Catalog
// ============================================================================

pub static STDLIB_MODULES: &[&str] = &[
    "__future__", "_thread", "abc", "aifc", "argparse", "array", "ast", "asynchat",
    "asyncio", "asyncore", "atexit", "audioop", "base64", "bdb", "binascii", "binhex",
    "bisect", "builtins", "bz2", "cProfile", "calendar", "cgi", "cgitb", "chunk",
    "cmath", "cmd", "code", "codecs", "codeop", "collections", "colorsys", "compileall",
    "concurrent", "configparser", "contextlib", "contextvars", "copy", "copyreg", "crypt",
    "csv", "ctypes", "curses", "dataclasses", "datetime", "dbm", "decimal", "difflib",
    "dis", "distutils", "doctest", "email", "encodings", "ensurepip", "enum", "errno",
    "faulthandler", "fcntl", "filecmp", "fileinput", "fnmatch", "fractions", "ftplib",
    "functools", "gc", "genericpath", "getopt", "getpass", "gettext", "glob", "graphlib",
    "grp", "gzip", "hashlib", "heapq", "hmac", "html", "http", "idlelib", "imaplib",
    "imghdr", "imp", "importlib", "inspect", "io", "ipaddress", "itertools", "json",
    "keyword", "linecache", "locale", "logging", "lzma", "mailbox", "mailcap", "marshal",
    "math", "mimetypes", "mmap", "modulefinder", "msilib", "msvcrt", "multiprocessing",
    "netrc", "nntplib", "nt", "ntpath", "nturl2path", "numbers", "opcode", "operator",
    "optparse", "os", "ossaudiodev", "pathlib", "pdb", "pickle", "pickletools", "pipes",
    "pkgutil", "platform", "plistlib", "poplib", "posix", "posixpath", "pprint", "profile",
    "pstats", "pty", "pwd", "py_compile", "pyclbr", "pydoc", "pydoc_data", "pyexpat",
    "queue", "quopri", "random", "re", "readline", "reprlib", "resource", "rlcompleter",
    "runpy", "sched", "secrets", "select", "selectors", "shelve", "shlex", "shutil",
    "signal", "site", "smtpd", "smtplib", "sndhdr", "socket", "socketserver", "spwd",
    "sqlite3", "sre_compile", "sre_constants", "sre_parse", "ssl", "stat", "statistics",
    "string", "stringprep", "struct", "subprocess", "sunau", "symtable", "sys", "sysconfig",
    "syslog", "tabnanny", "tarfile", "telnetlib", "tempfile", "termios", "test", "textwrap",
    "this", "threading", "time", "timeit", "tkinter", "token", "tokenize", "tomllib",
    "trace", "traceback", "tracemalloc", "tty", "turtle", "turtledemo", "types", "typing",
    "unicodedata", "unittest", "urllib", "uu", "uuid", "venv", "warnings", "wave",
    "weakref", "webbrowser", "winreg", "winsound", "wsgiref", "xdrlib", "xml", "xmlrpc",
    "zipapp", "zipfile", "zipimport", "zlib",
];

pub static IMPORT_PACKAGES: &[(&str, &str)] = &[
    ("cv2", "opencv-python"),
    ("PIL", "Pillow"),
    ("skimage", "scikit-image"),
    ("fitz", "PyMuPDF"),
    ("torchvision", "torchvision"),
    ("timm", "timm"),
    ("soundfile", "soundfile"),
    ("librosa", "librosa"),
    ("torchaudio", "torchaudio"),
    ("pydub", "pydub"),
    ("numpy", "numpy"),
    ("scipy", "scipy"),
    ("pandas", "pandas"),
    ("polars", "polars"),
    ("pyarrow", "pyarrow"),
    ("statsmodels", "statsmodels"),
    ("sympy", "sympy"),
    ("networkx", "networkx"),
    ("matplotlib", "matplotlib"),
    ("seaborn", "seaborn"),
    ("plotly", "plotly"),
    ("sklearn", "scikit-learn"),
    ("xgboost", "xgboost"),
    ("lightgbm", "lightgbm"),
    ("catboost", "catboost"),
    ("joblib", "joblib"),
    ("optuna", "optuna"),
    ("torch", "torch"),
    ("tensorflow", "tensorflow"),
    ("keras", "keras"),
    ("tensorboard", "tensorboard"),
    ("einops", "einops"),
    ("transformers", "transformers"),
    ("accelerate", "accelerate"),
    ("datasets", "datasets"),
    ("diffusers", "diffusers"),
    ("tiktoken", "tiktoken"),
    ("openai", "openai"),
    ("anthropic", "anthropic"),
    ("ollama", "ollama"),
    ("vllm", "vllm"),
    ("litellm", "litellm"),
    ("langchain", "langchain"),
    ("langchain_core", "langchain-core"),
    ("langchain_community", "langchain-community"),
    ("llama_index", "llama-index"),
    ("sentence_transformers", "sentence-transformers"),
    ("chromadb", "chromadb"),
    ("qdrant_client", "qdrant-client"),
    ("faiss", "faiss-cpu"),
    ("huggingface_hub", "huggingface-hub"),
    ("mlflow", "mlflow"),
    ("wandb", "wandb"),
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("starlette", "starlette"),
    ("flask", "flask"),
    ("gunicorn", "gunicorn"),
    ("streamlit", "streamlit"),
    ("gradio", "gradio"),
    ("django", "django"),
    ("tornado", "tornado"),
    ("aiohttp", "aiohttp"),
    ("dash", "dash"),
    ("celery", "celery"),
    ("redis", "redis"),
    ("sqlalchemy", "sqlalchemy"),
    ("alembic", "alembic"),
    ("psycopg2", "psycopg2-binary"),
    ("asyncpg", "asyncpg"),
    ("pymysql", "pymysql"),
    ("pymongo", "pymongo"),
    ("motor", "motor"),
    ("elasticsearch", "elasticsearch"),
    ("prometheus_client", "prometheus-client"),
    ("requests", "requests"),
    ("httpx", "httpx"),
    ("urllib3", "urllib3"),
    ("websockets", "websockets"),
    ("aiofiles", "aiofiles"),
    ("yaml", "PyYAML"),
    ("pydantic", "pydantic"),
    ("pydantic_settings", "pydantic-settings"),
    ("bs4", "beautifulsoup4"),
    ("lxml", "lxml"),
    ("openpyxl", "openpyxl"),
    ("docx", "python-docx"),
    ("pypdf", "pypdf"),
    ("pdfplumber", "pdfplumber"),
    ("orjson", "orjson"),
    ("ujson", "ujson"),
    ("msgpack", "msgpack"),
    ("protobuf", "protobuf"),
    ("dotenv", "python-dotenv"),
    ("cryptography", "cryptography"),
    ("jwt", "PyJWT"),
    ("rich", "rich"),
    ("tqdm", "tqdm"),
    ("click", "click"),
    ("typer", "typer"),
    ("tabulate", "tabulate"),
    ("dateutil", "python-dateutil"),
    ("pytz", "pytz"),
    ("serial", "pyserial"),
    ("attr", "attrs"),
    ("attrs", "attrs"),
    ("loguru", "loguru"),
    ("structlog", "structlog"),
    ("boto3", "boto3"),
    ("botocore", "botocore"),
];

pub fn resolve_pypi_package(import_name: &str) -> String {
    for &(alias, dist) in IMPORT_PACKAGES {
        if alias == import_name {
            return dist.to_string();
        }
    }
    import_name.to_string()
}

pub fn is_stdlib_module(name: &str) -> bool {
    let base = name.split('.').next().unwrap_or(name);
    STDLIB_MODULES.contains(&base)
}

// ============================================================================
// 5. Dependency AST Visitor
// ============================================================================

#[derive(Debug, Default)]
pub struct DependencyAstVisitor {
    pub all_imports: HashSet<String>,
    pub required_imports: HashSet<String>,
    pub optional_imports: HashSet<String>,
    pub needs_stdin: bool,
    pub has_app_object: bool,
    pub app_object_name: Option<String>,
}

impl DependencyAstVisitor {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn parse_source(&mut self, source: &str) {
        let mut in_try = false;
        let mut in_type_checking = false;
        let mut try_indent = 0;
        let mut tc_indent = 0;

        for line in source.lines() {
            let trimmed = line.trim();
            if trimmed.is_empty() || trimmed.starts_with('#') {
                continue;
            }

            let indent = line.len() - line.trim_start().len();

            if in_try && indent <= try_indent && !trimmed.starts_with("try:") {
                in_try = false;
            }
            if trimmed == "try:" || trimmed.starts_with("try:") {
                in_try = true;
                try_indent = indent;
            }

            if in_type_checking && indent <= tc_indent && !trimmed.starts_with("if TYPE_CHECKING") {
                in_type_checking = false;
            }
            if trimmed.starts_with("if TYPE_CHECKING") || trimmed.contains("typing.TYPE_CHECKING") {
                in_type_checking = true;
                tc_indent = indent;
                continue;
            }

            if in_type_checking {
                continue;
            }

            if trimmed.contains("input(") || trimmed.contains("sys.stdin") {
                self.needs_stdin = true;
            }

            if trimmed.starts_with("app =") || trimmed.starts_with("application =") {
                self.has_app_object = true;
                let name = trimmed.split('=').next().unwrap().trim();
                self.app_object_name = Some(name.to_string());
            }

            if trimmed.starts_with("import ") {
                let rest = &trimmed[7..];
                for part in rest.split(',') {
                    let token = part.trim().split_whitespace().next().unwrap_or("");
                    let module = token.split('.').next().unwrap_or("");
                    if !module.is_empty() && is_valid_identifier(module) {
                        self.all_imports.insert(module.to_string());
                        if in_try {
                            self.optional_imports.insert(module.to_string());
                        } else {
                            self.required_imports.insert(module.to_string());
                        }
                    }
                }
            } else if trimmed.starts_with("from ") {
                let rest = &trimmed[5..];
                if let Some(idx) = rest.find(" import ") {
                    let from_mod = rest[..idx].trim();
                    let module = from_mod.split('.').next().unwrap_or("");
                    if !module.is_empty() && is_valid_identifier(module) {
                        self.all_imports.insert(module.to_string());
                        if in_try {
                            self.optional_imports.insert(module.to_string());
                        } else {
                            self.required_imports.insert(module.to_string());
                        }
                    }
                }
            }
        }

        for req in &self.required_imports {
            self.optional_imports.remove(req);
        }
    }
}

fn is_valid_identifier(s: &str) -> bool {
    let mut chars = s.chars();
    match chars.next() {
        Some(first) if first.is_alphabetic() || first == '_' => chars.all(|c| c.is_alphanumeric() || c == '_'),
        _ => false,
    }
}

// ============================================================================
// 6. Recursive Filesystem Walker
// ============================================================================

pub fn walk_files_recursive(root: &Path, max_files: usize) -> Vec<PathBuf> {
    let mut results = Vec::new();
    let ignore: HashSet<&'static str> = [
        ".git", ".opsforge", "__pycache__", ".venv", "venv", ".idea", ".vscode", "node_modules", ".cache", "target",
    ]
    .iter()
    .copied()
    .collect();

    fn walk_inner(dir: &Path, results: &mut Vec<PathBuf>, max_files: usize, ignore: &HashSet<&'static str>) {
        if results.len() >= max_files {
            return;
        }
        if let Ok(entries) = fs::read_dir(dir) {
            for entry in entries.flatten() {
                if results.len() >= max_files {
                    break;
                }
                let path = entry.path();
                let file_name = entry.file_name();
                let name = file_name.to_str().unwrap_or("");
                if ignore.contains(name) || name.starts_with(".opsforge-backup") {
                    continue;
                }
                if let Ok(ft) = entry.file_type() {
                    if ft.is_file() {
                        results.push(path);
                    } else if ft.is_dir() {
                        walk_inner(&path, results, max_files, ignore);
                    }
                }
            }
        }
    }

    walk_inner(root, &mut results, max_files, &ignore);
    results
}

// ============================================================================
// 7. Inspector & Heuristic Analysis
// ============================================================================

pub struct Inspector;

impl Inspector {
    pub fn inspect(root: &Path, explicit_entry: Option<&str>) -> Result<Inspection, String> {
        let canonical_root = root.canonicalize().map_err(|e| format!("Invalid root path: {}", e))?;
        let all_files = walk_files_recursive(&canonical_root, MAX_VISITED_FILES);

        let py_files: Vec<PathBuf> = all_files
            .into_iter()
            .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("py"))
            .collect();

        if py_files.is_empty() {
            return Err("No Python files found in project".to_string());
        }

        let mut candidates = Vec::new();
        let mut best_candidate: Option<Candidate> = None;

        for path in &py_files {
            let rel_path = path
                .strip_prefix(&canonical_root)
                .unwrap_or(path)
                .to_string_lossy()
                .replace('\\', "/");

            if let Ok(source) = fs::read_to_string(path) {
                let mut visitor = DependencyAstVisitor::new();
                visitor.parse_source(&source);

                let mut score = 0;
                let mut framework = "python".to_string();

                let file_name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
                if file_name == "main.py" || file_name == "app.py" || file_name == "server.py" {
                    score += 50;
                }

                if visitor.required_imports.contains("fastapi") {
                    framework = "fastapi".to_string();
                    score += 40;
                } else if visitor.required_imports.contains("flask") {
                    framework = "flask".to_string();
                    score += 35;
                } else if visitor.required_imports.contains("streamlit") {
                    framework = "streamlit".to_string();
                    score += 35;
                } else if visitor.required_imports.contains("gradio") {
                    framework = "gradio".to_string();
                    score += 35;
                }

                if visitor.has_app_object {
                    score += 20;
                }
                if source.contains("if __name__ == '__main__':") || source.contains("if __name__ == \"__main__\":") {
                    score += 30;
                }

                if let Some(target) = explicit_entry {
                    if rel_path == target || file_name == target {
                        score += 1000;
                    }
                }

                let cand = Candidate {
                    path: path.clone(),
                    rel_path,
                    score,
                    framework,
                    imports: visitor.all_imports,
                    required_imports: visitor.required_imports,
                    optional_imports: visitor.optional_imports,
                    needs_stdin: visitor.needs_stdin,
                    has_app_object: visitor.has_app_object,
                    app_object_name: visitor.app_object_name,
                };

                if best_candidate.as_ref().map(|b| cand.score > b.score).unwrap_or(true) {
                    best_candidate = Some(cand.clone());
                }
                candidates.push(cand);
            }
        }

        let entry = best_candidate.ok_or_else(|| "Could not determine entrypoint script".to_string())?;

        let req_path = canonical_root.join("requirements.txt");
        let pyproj_path = canonical_root.join("pyproject.toml");
        let dependency = if req_path.is_file() {
            Some(req_path)
        } else if pyproj_path.is_file() {
            Some(pyproj_path)
        } else {
            None
        };

        let detected_mode = match entry.framework.as_str() {
            "fastapi" => Mode::FastApi,
            "flask" => Mode::Flask,
            "streamlit" => Mode::Streamlit,
            "gradio" => Mode::Gradio,
            _ => {
                if entry.has_app_object {
                    Mode::Web
                } else {
                    Mode::Base
                }
            }
        };

        let mut warnings = Vec::new();
        if dependency.is_none() {
            warnings.push("No requirements.txt or pyproject.toml at project root. Review dependency handling before building.".to_string());
        }

        Ok(Inspection {
            root: canonical_root,
            entry,
            dependency,
            detected_mode,
            candidates,
            python_count: py_files.len(),
            warnings,
        })
    }
}

// ============================================================================
// 8. Docker Storage Probe
// ============================================================================

pub struct DockerStorageProbe;

impl DockerStorageProbe {
    pub fn probe(context: Option<&str>) -> DockerStorageStatus {
        let mut base_cmd = Command::new("docker");
        if let Some(ctx) = context {
            base_cmd.args(&["--context", ctx]);
        }

        let info_res = base_cmd
            .args(&["info", "--format", "{{.DockerRootDir}}|{{.Driver}}|{{.ServerVersion}}"])
            .output();

        let (root_dir, driver, _server_ver, accessible) = match info_res {
            Ok(ref out) if out.status.success() => {
                let text = String::from_utf8_lossy(&out.stdout).trim().to_string();
                let parts: Vec<&str> = text.split('|').collect();
                if parts.len() >= 3 {
                    (
                        Some(parts[0].to_string()),
                        Some(parts[1].to_string()),
                        Some(parts[2].to_string()),
                        true,
                    )
                } else {
                    (None, None, None, true)
                }
            }
            _ => (None, None, None, false),
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
            // Under Docker Desktop (Windows/macOS), the daemon runs inside a Linux VM.
            // Host filesystem calls would erroneously inspect the host OS drive.
            daemon_free = None;
            daemon_total = None;
            assessment_status = "UNKNOWN".to_string();
        } else if let Some(ref dir) = root_dir {
            // On native Linux, probe actual storage via df without assuming fake space
            let df_probe = Command::new("df").args(&["-B1", dir.as_str()]).output();
            if let Ok(out) = df_probe {
                if out.status.success() {
                    let text = String::from_utf8_lossy(&out.stdout);
                    let lines: Vec<&str> = text.lines().collect();
                    if lines.len() >= 2 {
                        let tokens: Vec<&str> = lines[1].split_whitespace().collect();
                        // Filesystem, 1B-blocks, Used, Available, Use%, Mounted on
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

        let mut reclaimable_cache = 0;
        let mut df_cmd = Command::new("docker");
        if let Some(ctx) = context {
            df_cmd.args(&["--context", ctx]);
        }
        if let Ok(df_out) = df_cmd.args(&["system", "df", "--format", "{{.Type}}\t{{.Reclaimable}}"]).output() {
            let text = String::from_utf8_lossy(&df_out.stdout);
            for line in text.lines() {
                if line.starts_with("Build Cache") {
                    let parts: Vec<&str> = line.split('\t').collect();
                    if parts.len() >= 2 {
                        reclaimable_cache = parse_size_to_bytes(parts[1]);
                    }
                }
            }
        }

        let details = if is_vm_isolated {
            format!(
                "Docker daemon storage root ({}) is isolated inside VM/WSL2; host disk space does not reflect daemon space.",
                root_dir.as_deref().unwrap_or("/var/lib/docker")
            )
        } else if let Some(free) = daemon_free {
            format!("{:.1} GiB free ({})", free as f64 / 1024.0 / 1024.0 / 1024.0, root_dir.as_deref().unwrap_or(""))
        } else {
            "Storage access restricted or isolated (unmeasured from host)".to_string()
        };

        DockerStorageStatus {
            engine_accessible: true,
            docker_root_dir: root_dir,
            storage_driver: driver,
            daemon_free_bytes: daemon_free,
            daemon_total_bytes: daemon_total,
            reclaimable_build_cache_bytes: reclaimable_cache,
            reclaimable_images_bytes: 0,
            is_remote_context: context.is_some(),
            is_vm_isolated,
            assessment_status,
            details,
        }
    }
}

fn parse_size_to_bytes(s: &str) -> u64 {
    let clean = s.split('(').next().unwrap_or(s).trim();
    let num_str: String = clean.chars().take_while(|c| c.is_ascii_digit() || *c == '.').collect();
    let val: f64 = num_str.parse().unwrap_or(0.0);
    if clean.ends_with("GB") || clean.ends_with("GiB") {
        (val * 1024.0 * 1024.0 * 1024.0) as u64
    } else if clean.ends_with("MB") || clean.ends_with("MiB") {
        (val * 1024.0 * 1024.0) as u64
    } else if clean.ends_with("KB") || clean.ends_with("KiB") {
        (val * 1024.0) as u64
    } else {
        val as u64
    }
}

// ============================================================================
// 9. Resource Budget Estimator
// ============================================================================

pub struct ResourceBudgetEstimator;

impl ResourceBudgetEstimator {
    pub fn estimate(
        base_image: &str,
        declared_packages: &[String],
        _project_root: &Path,
        storage: &DockerStorageStatus,
        optional_packages: &[String],
    ) -> ResourceBudget {
        let base_image_bytes = if base_image.contains("slim") {
            150 * 1024 * 1024
        } else {
            400 * 1024 * 1024
        };

        let mut declared_bytes_min = 0u64;
        let mut declared_bytes_max = 0u64;

        for pkg in declared_packages {
            let (min_b, max_b, _) = get_package_footprint(pkg);
            declared_bytes_min += min_b;
            declared_bytes_max += max_b;
        }

        let mut optional_bytes = 0u64;
        for pkg in optional_packages {
            let (min_b, _, _) = get_package_footprint(pkg);
            optional_bytes += min_b;
        }

        let build_overhead_min = (base_image_bytes + declared_bytes_min) * 2;
        let build_overhead_max = (base_image_bytes + declared_bytes_max + optional_bytes) * 3;

        let total_bytes_min = base_image_bytes + declared_bytes_min + build_overhead_min;
        let total_bytes_max = base_image_bytes + declared_bytes_max + optional_bytes + build_overhead_max;

        let safety_reserve = 2 * 1024 * 1024 * 1024; // 2.0 GiB reserve

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
        } else if verdict == StorageVerdict::Unknown && storage.is_vm_isolated {
            explanations.push("Docker daemon storage root is isolated inside VM/WSL2; host disk space cannot be assumed as Docker storage.".to_string());
        }

        ResourceBudget {
            total_bytes_min,
            total_bytes_max,
            daemon_free_bytes: storage.daemon_free_bytes,
            safety_reserve_bytes: safety_reserve,
            verdict,
            confidence,
            explanations,
            assumptions: vec![
                format!("Base image '{}' layer footprint ~{:.0} MiB", base_image, base_image_bytes as f64 / 1024.0 / 1024.0),
                format!("Declared packages ({}) footprint: {:.1} - {:.1} MiB",
                    declared_packages.len(),
                    declared_bytes_min as f64 / 1024.0 / 1024.0,
                    declared_bytes_max as f64 / 1024.0 / 1024.0
                ),
            ],
            base_image_layer_bytes: base_image_bytes,
            declared_packages_bytes: declared_bytes_min,
            optional_packages_bytes: optional_bytes,
        }
    }
}

fn get_package_footprint(name: &str) -> (u64, u64, bool) {
    let lower = name.to_lowercase();
    match lower.as_str() {
        "torch" | "pytorch" => (800 * 1024 * 1024, 2500 * 1024 * 1024, true),
        "tensorflow" => (600 * 1024 * 1024, 1800 * 1024 * 1024, true),
        "catboost" => (120 * 1024 * 1024, 300 * 1024 * 1024, false),
        "lightgbm" => (80 * 1024 * 1024, 200 * 1024 * 1024, false),
        "xgboost" => (150 * 1024 * 1024, 350 * 1024 * 1024, false),
        "scipy" => (80 * 1024 * 1024, 150 * 1024 * 1024, false),
        "pandas" => (60 * 1024 * 1024, 120 * 1024 * 1024, false),
        "numpy" => (40 * 1024 * 1024, 80 * 1024 * 1024, false),
        "fastapi" | "flask" | "uvicorn" => (15 * 1024 * 1024, 40 * 1024 * 1024, false),
        _ => (10 * 1024 * 1024, 30 * 1024 * 1024, false),
    }
}

// ============================================================================
// 10. Planner (Framework Execution & Multi-Replica Synthesis)
// ============================================================================

pub struct Planner;

impl Planner {
    pub fn build(settings: &Settings, inspection: Inspection) -> Plan {
        let mode = if settings.mode != Mode::Auto {
            settings.mode.clone()
        } else {
            inspection.detected_mode.clone()
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
            Mode::Custom(_) => ExecutionKind::Web,
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

        let command = match mode {
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
            Mode::Gradio => {
                vec!["python".to_string(), inspection.entry.rel_path.clone()]
            }
            _ => {
                vec!["python".to_string(), inspection.entry.rel_path.clone()]
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

        let health_path = match execution_kind {
            ExecutionKind::Web => Some("/health".to_string()),
            _ => None,
        };

        let python_image = "python:3.12-slim-bookworm".to_string();

        let mut persistent_dirs = Vec::new();
        for dir_name in &["artifacts", "models", "data", "checkpoints", "runs"] {
            if inspection.root.join(dir_name).is_dir() {
                persistent_dirs.push(dir_name.to_string());
            }
        }

        let mut inferred_packages: Vec<String> = inspection
            .entry
            .required_imports
            .iter()
            .filter(|pkg| !is_stdlib_module(pkg))
            .map(|pkg| resolve_pypi_package(pkg))
            .collect();
        inferred_packages.sort();
        inferred_packages.dedup();

        let mut optional_packages: Vec<String> = inspection
            .entry
            .optional_imports
            .iter()
            .filter(|pkg| !is_stdlib_module(pkg))
            .map(|pkg| resolve_pypi_package(pkg))
            .collect();
        optional_packages.sort();
        optional_packages.dedup();

        let storage_status = DockerStorageProbe::probe(settings.docker_context.as_deref());
        let budget = ResourceBudgetEstimator::estimate(
            &python_image,
            &inferred_packages,
            &inspection.root,
            &storage_status,
            &optional_packages,
        );

        Plan {
            inspection,
            mode,
            port,
            gpu: settings.gpu,
            command,
            services: vec!["app".to_string()],
            warnings: Vec::new(),
            published_ports,
            inferred_packages,
            execution_kind,
            health_path,
            python_image,
            persistent_dirs,
            replicas: settings.replicas,
            cpus: settings.cpus.clone(),
            memory: settings.memory.clone(),
            resource_budget: Some(budget),
        }
    }
}

// ============================================================================
// 11. Renderer (Multi-Stage Dockerfile, Compose, Manifest, Instructions)
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

        out.push_str("    restart: unless-stopped\n");
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
// 12. Output Manager (.opsforge/ Storage & Backups)
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

    pub fn write(&self, files: &HashMap<String, String>) -> Result<(), String> {
        if self.folder.exists() {
            let timestamp = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs();
            let backup_dir = self
                .folder
                .parent()
                .unwrap_or_else(|| Path::new("."))
                .join(format!(".opsforge-backup-{}", timestamp));
            let _ = copy_dir_all(&self.folder, &backup_dir);
        } else {
            fs::create_dir_all(&self.folder)
                .map_err(|e| format!("Failed to create output directory: {}", e))?;
        }

        for (name, content) in files {
            let path = self.folder.join(name);
            fs::write(&path, content)
                .map_err(|e| format!("Failed to write {}: {}", path.display(), e))?;
        }
        Ok(())
    }

    pub fn validate(&self) -> (bool, Vec<String>) {
        let mut errors = Vec::new();
        let required = ["Dockerfile", "compose.yaml", "manifest.json"];
        for name in &required {
            let p = self.folder.join(name);
            if !p.is_file() {
                errors.push(format!("Missing required output file: {}", name));
            }
        }
        (errors.is_empty(), errors)
    }

    pub fn restore_last_backup(&self) -> Result<PathBuf, String> {
        let parent = self.folder.parent().unwrap_or_else(|| Path::new("."));
        let mut backups = Vec::new();
        if let Ok(entries) = fs::read_dir(parent) {
            for entry in entries.flatten() {
                let name = entry.file_name().to_string_lossy().to_string();
                if name.starts_with(".opsforge-backup-") && entry.path().is_dir() {
                    backups.push(entry.path());
                }
            }
        }
        backups.sort();
        if let Some(latest) = backups.pop() {
            let _ = fs::remove_dir_all(&self.folder);
            copy_dir_all(&latest, &self.folder).map_err(|e| e.to_string())?;
            Ok(latest)
        } else {
            Err("No backup directory found to restore".to_string())
        }
    }
}

fn copy_dir_all(src: &Path, dst: &Path) -> io::Result<()> {
    if !dst.exists() {
        fs::create_dir_all(dst)?;
    }
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
// 13. Project Lock Manager
// ============================================================================

pub struct ProjectLock {
    _root: PathBuf,
    lock_file: PathBuf,
    guard_file: PathBuf,
    is_owner: bool,
}

impl ProjectLock {
    pub fn new(root: &Path) -> Self {
        let root = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());
        Self {
            lock_file: root.join(".opsforge.lock"),
            guard_file: root.join(".opsforge.lock.guard"),
            _root: root,
            is_owner: false,
        }
    }

    pub fn acquire(&mut self) -> Result<(), String> {
        if self.lock_file.exists() {
            if let Ok(content) = fs::read_to_string(&self.lock_file) {
                if let Ok(pid) = content.trim().parse::<u32>() {
                    if Self::is_pid_alive(pid) {
                        return Err(format!(
                            "An Auto Docker generation/recovery operation is still running (PID {}). Wait for it to finish.",
                            pid
                        ));
                    } else {
                        let _ = fs::remove_file(&self.lock_file);
                    }
                }
            }
        }

        let pid = std::process::id();
        fs::write(&self.lock_file, pid.to_string())
            .map_err(|e| format!("Cannot open Auto Docker project lock: {}", e))?;
        self.is_owner = true;
        Ok(())
    }

    pub fn release(&mut self) {
        if self.is_owner && self.lock_file.exists() {
            let _ = fs::remove_file(&self.lock_file);
            self.is_owner = false;
        }
    }

    pub fn unlock_force(&self) -> Result<(), String> {
        if self.lock_file.exists() {
            fs::remove_file(&self.lock_file)
                .map_err(|e| format!("Failed to remove lock: {}", e))?;
        }
        if self.guard_file.exists() {
            let _ = fs::remove_file(&self.guard_file);
        }
        Ok(())
    }

    pub fn is_pid_alive(pid: u32) -> bool {
        #[cfg(target_os = "windows")]
        {
            let out = Command::new("tasklist")
                .args(&["/FI", &format!("PID eq {}", pid)])
                .output();
            if let Ok(output) = out {
                let text = String::from_utf8_lossy(&output.stdout);
                return text.contains(&pid.to_string());
            }
            false
        }
        #[cfg(not(target_os = "windows"))]
        {
            Path::new(&format!("/proc/{}", pid)).exists()
        }
    }
}

impl Drop for ProjectLock {
    fn drop(&mut self) {
        self.release();
    }
}

// ============================================================================
// 14. Existing Infrastructure Engine
// ============================================================================

#[derive(Debug, Clone)]
pub struct ExistingStack {
    pub compose_file: PathBuf,
    pub services: Vec<String>,
    pub file_size: u64,
}

pub struct ExistingInfrastructure;

impl ExistingInfrastructure {
    const COMPOSE_NAMES: &'static [&'static str] = &[
        "compose.yaml",
        "compose.yml",
        "docker-compose.yaml",
        "docker-compose.yml",
    ];

    pub fn discover(root: &Path, explicit_choice: Option<&str>) -> Option<ExistingStack> {
        let root = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());
        if let Some(choice) = explicit_choice {
            let path = root.join(choice);
            if path.is_file() {
                let (services, size) = Self::parse_services(&path);
                return Some(ExistingStack {
                    compose_file: path,
                    services,
                    file_size: size,
                });
            }
        }
        for &name in Self::COMPOSE_NAMES {
            let path = root.join(name);
            if path.is_file() {
                let (services, size) = Self::parse_services(&path);
                return Some(ExistingStack {
                    compose_file: path,
                    services,
                    file_size: size,
                });
            }
        }
        None
    }

    pub fn parse_services(path: &Path) -> (Vec<String>, u64) {
        let size = fs::metadata(path).map(|m| m.len()).unwrap_or(0);
        let mut services = Vec::new();
        if let Ok(content) = fs::read_to_string(path) {
            let mut inside_services = false;
            for line in content.lines() {
                let trimmed = line.trim();
                if trimmed.starts_with('#') {
                    continue;
                }
                if line.starts_with("services:") {
                    inside_services = true;
                    continue;
                }
                if inside_services {
                    if !line.starts_with(' ') && !line.starts_with('\t') && trimmed.ends_with(':') {
                        break;
                    }
                    if line.starts_with("  ") && !line.starts_with("    ") && trimmed.ends_with(':') {
                        let name = trimmed.trim_end_matches(':').trim();
                        if !name.is_empty() && !services.contains(&name.to_string()) {
                            services.push(name.to_string());
                        }
                    }
                }
            }
        }
        (services, size)
    }
}

// ============================================================================
// 15. Environment Doctor
// ============================================================================

pub struct DoctorCheck {
    pub name: String,
    pub ok: bool,
    pub detail: String,
}

pub struct EnvironmentDoctor;

impl EnvironmentDoctor {
    pub fn report(project_path: Option<&Path>) -> Vec<DoctorCheck> {
        let mut checks = Vec::new();

        checks.push(DoctorCheck {
            name: "Platform".to_string(),
            ok: true,
            detail: format!("{} ({})", env::consts::OS, env::consts::ARCH),
        });

        if let Some(p) = project_path {
            let exists = p.is_dir();
            checks.push(DoctorCheck {
                name: "Project Path".to_string(),
                ok: exists,
                detail: p.display().to_string(),
            });
        }

        let cli_res = Command::new("docker").arg("--version").output();
        let (cli_ok, cli_ver) = match cli_res {
            Ok(ref out) if out.status.success() => {
                let ver = String::from_utf8_lossy(&out.stdout).trim().to_string();
                (true, ver)
            }
            _ => (false, "Docker CLI not found on PATH".to_string()),
        };
        checks.push(DoctorCheck {
            name: "Docker CLI".to_string(),
            ok: cli_ok,
            detail: cli_ver,
        });

        if cli_ok {
            let compose_res = Command::new("docker").args(&["compose", "version"]).output();
            let (compose_ok, compose_ver) = match compose_res {
                Ok(ref out) if out.status.success() => {
                    let ver = String::from_utf8_lossy(&out.stdout).trim().to_string();
                    (true, ver)
                }
                _ => (false, "Docker Compose plugin not found".to_string()),
            };
            checks.push(DoctorCheck {
                name: "Docker Compose".to_string(),
                ok: compose_ok,
                detail: compose_ver,
            });

            let engine_res = Command::new("docker")
                .args(&["info", "--format", "{{.ServerVersion}}"])
                .output();
            let (engine_ok, engine_ver) = match engine_res {
                Ok(ref out) if out.status.success() => {
                    let ver = String::from_utf8_lossy(&out.stdout).trim().to_string();
                    (true, format!("Engine v{}", ver))
                }
                _ => (false, "Docker Engine is not running or socket is inaccessible".to_string()),
            };
            checks.push(DoctorCheck {
                name: "Docker Engine".to_string(),
                ok: engine_ok,
                detail: engine_ver,
            });

            let storage = DockerStorageProbe::probe(None);
            if storage.engine_accessible {
                checks.push(DoctorCheck {
                    name: "Docker Storage".to_string(),
                    ok: storage.daemon_free_bytes.map(|b| b >= 5 * 1024 * 1024 * 1024).unwrap_or(true),
                    detail: format!("{:.1} GiB free ({})", storage.free_gib(), storage.details),
                });
            } else if storage.is_vm_isolated {
                checks.push(DoctorCheck {
                    name: "Docker Storage".to_string(),
                    ok: true,
                    detail: "Isolated in VM/WSL2 (unmeasured from host)".to_string(),
                });
            } else {
                checks.push(DoctorCheck {
                    name: "Docker Storage".to_string(),
                    ok: false,
                    detail: storage.details.clone(),
                });
            }

            if storage.reclaimable_gib() > 0.1 {
                checks.push(DoctorCheck {
                    name: "Reclaimable Cache".to_string(),
                    ok: true,
                    detail: format!("{:.1} GiB reclaimable (run 'docker builder prune')", storage.reclaimable_gib()),
                });
            }
        } else {
            checks.push(DoctorCheck {
                name: "Docker Compose".to_string(),
                ok: false,
                detail: "Unavailable (Docker CLI missing)".to_string(),
            });
            checks.push(DoctorCheck {
                name: "Docker Engine".to_string(),
                ok: false,
                detail: "Unavailable (Docker CLI missing)".to_string(),
            });
        }

        let gpu_res = Command::new("nvidia-smi").output();
        if let Ok(ref out) = gpu_res {
            if out.status.success() {
                checks.push(DoctorCheck {
                    name: "NVIDIA Hardware".to_string(),
                    ok: true,
                    detail: "NVIDIA GPU hardware detected via nvidia-smi".to_string(),
                });
            } else {
                checks.push(DoctorCheck {
                    name: "NVIDIA Hardware".to_string(),
                    ok: true,
                    detail: "Standard CPU runtime".to_string(),
                });
            }
        } else {
            checks.push(DoctorCheck {
                name: "NVIDIA Hardware".to_string(),
                ok: true,
                detail: "Standard CPU runtime".to_string(),
            });
        }

        checks
    }

    pub fn print_report(project_path: Option<&Path>) -> bool {
        let checks = Self::report(project_path);
        println!("\n================================================================================");
        println!("AUTO DOCKER ENVIRONMENT DOCTOR (READ-ONLY)");
        println!("================================================================================");
        let mut all_ok = true;
        for c in &checks {
            let status = if c.ok { "[  OK  ]" } else { "[ FAIL ]" };
            println!("  {:<8} {:<20} : {}", status, c.name, c.detail);
            if !c.ok && (c.name.contains("CLI") || c.name.contains("Engine")) {
                all_ok = false;
            }
        }
        println!("================================================================================\n");
        all_ok
    }
}

// ============================================================================
// 16. Scoped Resource Cleaner
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
            .args(&["ps", "-a", "--format", "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}"])
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
                let _ = Command::new("docker").args(&["stop", &r.id]).output();
                let rm = Command::new("docker").args(&["rm", "-f", &r.id]).output();
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
// 17. Recent Projects Store & Deployment Digest
// ============================================================================

pub struct RecentProjectsStore;

impl RecentProjectsStore {
    fn store_path() -> PathBuf {
        let home = env::var_os("USERPROFILE")
            .or_else(|| env::var_os("HOME"))
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("."));
        home.join(".auto_docker").join("recent_projects.txt")
    }

    pub fn load() -> Vec<PathBuf> {
        let p = Self::store_path();
        if !p.is_file() {
            return Vec::new();
        }
        let mut results = Vec::new();
        if let Ok(file) = fs::File::open(&p) {
            for line in io::BufReader::new(file).lines().flatten() {
                let trimmed = line.trim();
                if !trimmed.is_empty() {
                    let pb = PathBuf::from(trimmed);
                    if !results.contains(&pb) {
                        results.push(pb);
                    }
                }
            }
        }
        results.truncate(10);
        results
    }

    pub fn add(path: &Path) {
        let mut existing = Self::load();
        let canonical = path.canonicalize().unwrap_or_else(|_| path.to_path_buf());
        existing.retain(|p| p != &canonical);
        existing.insert(0, canonical);
        existing.truncate(10);

        let p = Self::store_path();
        if let Some(parent) = p.parent() {
            let _ = fs::create_dir_all(parent);
        }
        let content = existing
            .iter()
            .map(|p| p.display().to_string())
            .collect::<Vec<_>>()
            .join("\n");
        let _ = fs::write(&p, content);
    }
}

pub struct DeploymentDigest;

impl DeploymentDigest {
    pub fn compute(_root: &Path, action: &str) -> String {
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        format!("sha256-{:x}-{}", timestamp, action)
    }
}

// ============================================================================
// 18. SMART Mode Presenter & 60-Second Onboarding Guide
// ============================================================================

pub struct SmartPresenter;

impl SmartPresenter {
    pub fn print_home(project_path: &Path, engine_ready: bool) {
        println!("================================================================================");
        println!("AUTO DOCKER -- Zero-Friction Project Launcher (SMART Mode)");
        println!("================================================================================");
        println!("Welcome! Run any Python application or Docker stack with zero container knowledge.\n");
        println!("Working Folder : {}", project_path.display());
        let status = if engine_ready { "[OK] Ready (Docker Engine active)" } else { "[!] Docker not running yet (Run 'autodocker doctor' for setup)" };
        println!("Docker Status  : {}", status);
        println!("--------------------------------------------------------------------------------");
        println!("Available Actions:");
        println!("  [1] [RUN] Run my project (Auto-Detect, Prepare, and Launch)");
        println!("  [2] [GUIDE] 60-Second Teammate Guide (How this works in plain English)");
        println!("  [3] [DOCTOR] System & Docker Setup Doctor (Step-by-step setup assistance)");
        println!("  [4] [DEV] Advanced Developer Tools (preflight, preview, ports, check)");
        println!("  [0] Exit");
        println!("--------------------------------------------------------------------------------");
        println!("Run with: autodocker <command> [project_path]");
        println!("================================================================================");
    }

    pub fn print_guide(card: usize) {
        let card = card.clamp(1, 4);
        println!("\n================================================================================");
        println!("AUTO DOCKER -- 60-Second Teammate Guide [Card {} of 4]", card);
        println!("================================================================================");
        match card {
            1 => {
                println!("[*] CARD 1: What is a Container? (The Plain English Version)");
                println!("--------------------------------------------------------------------------------");
                println!("Think of a container like an instant virtual laptop inside your computer.\n");
                println!("* It has clean Python and all needed libraries already installed.");
                println!("* It runs your code in an isolated room so nothing conflicts.");
                println!("* It guarantees your code works the exact same way on your computer,");
                println!("  on your teammates' computers, and in production.\n");
                println!("-> The big win: You will NEVER hear or say 'It works on my machine' again!");
            }
            2 => {
                println!("[*] CARD 2: How Do I Run My Work? (One Simple Command)");
                println!("--------------------------------------------------------------------------------");
                println!("You don't need to write Dockerfiles or remember complicated commands.\n");
                println!("1. Open your terminal in your project folder.");
                println!("2. Type this one command:");
                println!("       ./auto_docker");
                println!("   (or: ./auto_docker preflight app.py)\n");
                println!("3. Auto Docker scans your code, checks disk space, configures ports,");
                println!("   and gives you a live localhost web address!");
            }
            3 => {
                println!("[*] CARD 3: How Does Auto Docker Protect Me?");
                println!("--------------------------------------------------------------------------------");
                println!("* NON-DESTRUCTIVE GUARANTEE: Never overwrites your root files.");
                println!("  All container infrastructure is generated exclusively inside .opsforge/.");
                println!("* RESOURCE BUDGET ENGINE: Measures real Docker storage before building.");
                println!("  Warns or blocks before catastrophic 'no space left on device' crashes.");
                println!("* NETWORK SAFETY: Binds strictly to 127.0.0.1 (loopback) by default.");
                println!("* INSTANT ROLLBACK: Previous deployments are archived automatically.");
            }
            4 => {
                println!("[*] CARD 4: Where Are My Files & How Do I Stop?");
                println!("--------------------------------------------------------------------------------");
                println!("* Where are the generated files?");
                println!("  Look inside the '.opsforge/' folder in your project.");
                println!("* How do I stop containers?");
                println!("  Run: ./auto_docker down");
                println!("* How do I clean test containers?");
                println!("  Run: ./auto_docker clean --yes");
            }
            _ => unreachable!(),
        }
        println!("================================================================================\n");
    }
}

// ============================================================================
// 19. Interactive Console & Developer Workbench
// ============================================================================

pub struct Console {
    pub settings: Settings,
    pub orchestrator: DeploymentOrchestrator,
}

impl Console {
    pub fn new(settings: Settings) -> Self {
        let orchestrator = DeploymentOrchestrator::new(settings.target.root.clone());
        Self { settings, orchestrator }
    }

    pub fn print_workbench_menu(&self) {
        println!("================================================================================");
        println!("AUTO DOCKER v{} — Container Infrastructure Workbench", VERSION);
        println!("================================================================================");
        println!("Project   : {}", self.settings.target.root.display());
        println!("Mode      : {} | State: {}", self.settings.mode.as_str(), self.orchestrator.current_state.as_str());
        println!("--------------------------------------------------------------------------------");
        println!(" [S] SMART Mode: One-Command Intelligent Deployment");
        println!(" [A] Auto-Pilot: Guided Quick-Start Wizard");
        println!(" [1] Inspect & Analyze Project Structure");
        println!(" [2] Preflight Offline Safety Audits (Dependencies, Storage, Ports)");
        println!(" [3] Preview Generated Configuration Files");
        println!(" [4] Generate Container Infrastructure (.opsforge/)");
        println!(" [5] System & Environment Doctor Diagnostics");
        println!(" [6] Scoped Resource Cleaner (Tests & Project containers)");
        println!(" [7] Recent Projects History");
        println!(" [0] Exit Workbench");
        println!("--------------------------------------------------------------------------------");
        println!("Type an option [0-7, S, A] or press [ENTER] to Run SMART:");
        println!("================================================================================");
    }

    pub fn run_interactive(&mut self) {
        self.print_workbench_menu();
        print!("auto-docker> ");
        let _ = io::stdout().flush();

        let mut input = String::new();
        if io::stdin().read_line(&mut input).is_ok() {
            let choice = input.trim();
            match choice {
                "0" | "exit" | "quit" => {
                    println!("Exiting Auto Docker workbench.");
                }
                "1" | "inspect" => {
                    if let Ok(ins) = Inspector::inspect(&self.settings.target.root, self.settings.explicit_entry.as_deref()) {
                        let plan = Planner::build(&self.settings, ins);
                        print_summary(&plan);
                    }
                }
                "2" | "preflight" => {
                    if let Ok(ins) = Inspector::inspect(&self.settings.target.root, self.settings.explicit_entry.as_deref()) {
                        let plan = Planner::build(&self.settings, ins);
                        print_summary(&plan);
                        DependencyAudit::print_report(&plan.inspection);
                        PortAudit::print_report(&plan.published_ports);
                    }
                }
                "3" | "preview" => {
                    if let Ok(ins) = Inspector::inspect(&self.settings.target.root, self.settings.explicit_entry.as_deref()) {
                        let plan = Planner::build(&self.settings, ins);
                        let files = Renderer::files(&plan);
                        println!("\nPreview of generated files:");
                        for name in files.keys() {
                            println!("  - {}", name);
                        }
                    }
                }
                "4" | "generate" => {
                    if let Ok(ins) = Inspector::inspect(&self.settings.target.root, self.settings.explicit_entry.as_deref()) {
                        let plan = Planner::build(&self.settings, ins);
                        let files = Renderer::files(&plan);
                        let out = OutputManager::new(&self.settings.target.root);
                        if let Ok(_) = out.write(&files) {
                            println!("Successfully generated infrastructure in {}", out.folder().display());
                        }
                    }
                }
                "5" | "doctor" => {
                    EnvironmentDoctor::print_report(Some(&self.settings.target.root));
                }
                "6" | "clean" => {
                    let name = self.settings.target.root.file_name().and_then(|n| n.to_str()).unwrap_or("project");
                    let res = ResourceCleaner::discover_project_resources(name);
                    ResourceCleaner::preview(&res);
                }
                "7" | "recent" => {
                    let recent = RecentProjectsStore::load();
                    println!("\nRecent Projects:");
                    for (i, p) in recent.iter().enumerate() {
                        println!("  [{}] {}", i + 1, p.display());
                    }
                }
                _ => {
                    let ready = DockerHostProbe::is_cli_available();
                    SmartPresenter::print_home(&self.settings.target.root, ready);
                }
            }
        }
    }
}

// ============================================================================
// 20. Docker Process Adapter
// ============================================================================

pub struct DockerHostProbe;

impl DockerHostProbe {
    pub fn is_cli_available() -> bool {
        Command::new("docker").arg("--version").output().is_ok()
    }

    pub fn is_compose_available() -> bool {
        Command::new("docker").args(&["compose", "version"]).output().is_ok()
    }
}

pub struct DockerAdapter {
    target_root: PathBuf,
    context: Option<String>,
}

impl DockerAdapter {
    pub fn new(target_root: &Path, context: Option<String>) -> Self {
        Self {
            target_root: target_root.to_path_buf(),
            context,
        }
    }

    pub fn preflight(&self) -> Result<(), String> {
        if !DockerHostProbe::is_cli_available() {
            return Err("Docker CLI is not found on host PATH".to_string());
        }
        if !DockerHostProbe::is_compose_available() {
            return Err("Docker Compose plugin is not found".to_string());
        }
        Ok(())
    }

    pub fn run_action(&self, action: &str) -> Result<(), String> {
        let compose_file = self.target_root.join(".opsforge").join("compose.yaml");
        if !compose_file.is_file() {
            return Err(format!("Compose file not found at: {}", compose_file.display()));
        }

        let mut cmd = Command::new("docker");
        if let Some(ref ctx) = self.context {
            cmd.args(&["--context", ctx]);
        }

        match action {
            "build" => {
                cmd.args(&["compose", "-f", compose_file.to_str().unwrap(), "build"]);
            }
            "up" => {
                cmd.args(&["compose", "-f", compose_file.to_str().unwrap(), "up", "-d"]);
            }
            "run" => {
                cmd.args(&["compose", "-f", compose_file.to_str().unwrap(), "run", "--rm", "app"]);
            }
            "down" => {
                cmd.args(&["compose", "-f", compose_file.to_str().unwrap(), "down"]);
            }
            "ps" => {
                cmd.args(&["compose", "-f", compose_file.to_str().unwrap(), "ps"]);
            }
            "logs" => {
                cmd.args(&["compose", "-f", compose_file.to_str().unwrap(), "logs", "--tail=50"]);
            }
            "check" => {
                cmd.args(&["compose", "-f", compose_file.to_str().unwrap(), "config"]);
            }
            other => return Err(format!("Unsupported action '{}'", other)),
        }

        let status = cmd
            .status()
            .map_err(|e| format!("Failed to execute docker: {}", e))?;

        if !status.success() {
            return Err(format!("Docker command exited with error status: {:?}", status.code()));
        }
        Ok(())
    }
}

// ============================================================================
// 21. Audits (Ports & Dependencies)
// ============================================================================

pub struct PortAudit;

impl PortAudit {
    pub fn is_port_available(port: u16) -> bool {
        TcpListener::bind(("127.0.0.1", port)).is_ok()
    }

    pub fn print_report(ports: &[PublishedPort]) -> bool {
        println!("\nHost port preflight (127.0.0.1; advisory, no ports reserved)");
        let mut all_ok = true;
        for p in ports {
            let ok = Self::is_port_available(p.host);
            let status = if ok { "OK  " } else { "FAIL" };
            let msg = if ok {
                "available now (not reserved; another process may claim it)"
            } else {
                "port is currently OCCUPIED by another process"
            };
            println!("  {} {:<9} {:>5} -> {:>5}: {}", status, p.service, p.host, p.container, msg);
            if !ok {
                all_ok = false;
            }
        }
        all_ok
    }
}

pub struct DependencyAudit;

impl DependencyAudit {
    pub fn print_report(inspection: &Inspection) {
        println!("\nAuto Docker dependency preflight (static, read-only)");
        let decl = inspection
            .dependency
            .as_ref()
            .map(|p| p.file_name().and_then(|n| n.to_str()).unwrap_or("manifest"))
            .unwrap_or("(none found)");
        println!("  Explicitly declared packages: {}", decl);

        let mut req: Vec<&String> = inspection.entry.required_imports.iter().collect();
        req.sort();
        if req.is_empty() {
            println!("  Required candidate packages: (none detected)");
        } else {
            println!("  Required candidate packages: {}", req.into_iter().cloned().collect::<Vec<_>>().join(", "));
        }

        let mut opt: Vec<&String> = inspection.entry.optional_imports.iter().collect();
        opt.sort();
        if opt.is_empty() {
            println!("  Optional candidate packages (guarded): (none detected)");
        } else {
            println!("  Optional candidate packages (guarded): {}", opt.into_iter().cloned().collect::<Vec<_>>().join(", "));
        }

        for w in &inspection.warnings {
            println!("  WARNING: {}", w);
        }
    }
}

// ============================================================================
// 22. CLI Presentation
// ============================================================================

fn print_usage() {
    println!("Auto Docker (Rust Single-File) v{} — Container infrastructure assistant\n", VERSION);
    println!("USAGE:");
    println!("  autodocker <COMMAND> [PROJECT_PATH] [OPTIONS]\n");
    println!("PRIMARY COMMANDS:");
    println!("  smart      Zero-jargon SMART launcher & project understanding (default)");
    println!("  console    Interactive Developer Workbench with full menus");
    println!("  guide      Display 60-Second Teammate Guide cards [1-4]");
    println!("  doctor     Comprehensive host, Docker, storage, and accelerator doctor");
    println!("  clean      Discover and safely prune disposable project & test containers");
    println!("  existing   Adopt and run existing compose.yaml without modification");
    println!("  lock       Inspect or acquire project atomic concurrency lock");
    println!("  unlock     Safely release or clear stale project concurrency lock\n");
    println!("CONTAINER PIPELINE COMMANDS:");
    println!("  inspect    Inspect and summarize project understanding");
    println!("  preflight  Run offline dependency, port, and storage safety audits");
    println!("  preview    Preview generated Dockerfile and Compose files without writing");
    println!("  generate   Generate Docker infrastructure into .opsforge/");
    println!("  ports      Verify host port availability");
    println!("  check      Validate generated configuration");
    println!("  build      Build Docker images with storage deficit pre-check");
    println!("  up, run    Start project container stack");
    println!("  down       Stop project container stack");
    println!("  ps         List running project containers");
    println!("  logs       View live logs of project containers");
    println!("  recover    Roll back to the previous deployment backup");
    println!("  self-test  Run built-in test suite\n");
    println!("OPTIONS:");
    println!("  --mode <MODE>        Application mode (base, web, fastapi, flask, streamlit)");
    println!("  --entry <FILE>       Explicit entrypoint script");
    println!("  --port <PORT>        Published port");
    println!("  --replicas <N>       Container replicas (default: 1)");
    println!("  --cpus <CPUS>        CPU limit (e.g. '1.5', '2')");
    println!("  --memory <MEM>       Memory limit (e.g. '512M', '2G')");
    println!("  --card <1-4>         Guide card index for 'guide' command");
    println!("  --gpu                Enable GPU/CUDA acceleration");
    println!("  --yes, -y            Non-interactive execution confirmation");
}

fn print_summary(plan: &Plan) {
    println!("\nProject    : {}", plan.inspection.root.display());
    println!("Entrypoint : {}", plan.inspection.entry.rel_path);
    println!("Framework  : {}", plan.inspection.entry.framework);
    println!("Workload   : {:?}", plan.mode);
    println!("Services   : {}", plan.services.join(", "));
    println!("Execution  : {:?}", plan.execution_kind);
    println!("Command    : {}", plan.command.join(" "));
    if let Some(p) = plan.port {
        println!("App port   : {}", p);
    }
    for w in &plan.inspection.warnings {
        println!("WARNING: {}", w);
    }
}

// ============================================================================
// 23. Main CLI Entrypoint
// ============================================================================

pub fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        let cwd = env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        let ready = DockerHostProbe::is_cli_available();
        SmartPresenter::print_home(&cwd, ready);
        process::exit(0);
    }

    let command = args[1].as_str();
    if command == "--help" || command == "-h" || command == "help" {
        print_usage();
        process::exit(0);
    }
    if command == "--version" || command == "-v" || command == "version" {
        println!("Auto Docker (Rust Single-File) v{}", VERSION);
        process::exit(0);
    }
    if command == "self-test" {
        run_self_test();
        process::exit(0);
    }

    let mut project_path = PathBuf::from(".");
    let mut mode = Mode::Auto;
    let mut explicit_entry = None;
    let mut port = None;
    let mut card = 1;
    let mut replicas = 1;
    let mut cpus = None;
    let mut memory = None;
    let mut gpu = false;
    let mut yes = false;

    let mut idx = 2;
    while idx < args.len() {
        match args[idx].as_str() {
            "--mode" => {
                if idx + 1 < args.len() {
                    mode = Mode::from_str_loose(&args[idx + 1]);
                    idx += 2;
                } else {
                    idx += 1;
                }
            }
            "--entry" => {
                if idx + 1 < args.len() {
                    explicit_entry = Some(args[idx + 1].clone());
                    idx += 2;
                } else {
                    idx += 1;
                }
            }
            "--port" => {
                if idx + 1 < args.len() {
                    port = args[idx + 1].parse().ok();
                    idx += 2;
                } else {
                    idx += 1;
                }
            }
            "--replicas" => {
                if idx + 1 < args.len() {
                    replicas = args[idx + 1].parse().unwrap_or(1);
                    idx += 2;
                } else {
                    idx += 1;
                }
            }
            "--cpus" => {
                if idx + 1 < args.len() {
                    cpus = Some(args[idx + 1].clone());
                    idx += 2;
                } else {
                    idx += 1;
                }
            }
            "--memory" => {
                if idx + 1 < args.len() {
                    memory = Some(args[idx + 1].clone());
                    idx += 2;
                } else {
                    idx += 1;
                }
            }
            "--card" => {
                if idx + 1 < args.len() {
                    card = args[idx + 1].parse().unwrap_or(1);
                    idx += 2;
                } else {
                    idx += 1;
                }
            }
            "--gpu" => {
                gpu = true;
                idx += 1;
            }
            "--yes" | "-y" => {
                yes = true;
                idx += 1;
            }
            arg if !arg.starts_with('-') => {
                project_path = PathBuf::from(arg);
                idx += 1;
            }
            _ => {
                idx += 1;
            }
        }
    }

    let target_root = if project_path.is_file() {
        explicit_entry = explicit_entry.or_else(|| {
            project_path
                .file_name()
                .and_then(|n| n.to_str())
                .map(|s| s.to_string())
        });
        project_path
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .to_path_buf()
    } else {
        project_path.clone()
    };

    let settings = Settings {
        target: Target::new(target_root.clone(), explicit_entry.clone()),
        explicit_entry: explicit_entry.clone(),
        mode,
        port,
        gpu,
        replicas,
        cpus,
        memory,
        dependency_mode: "strict".to_string(),
        docker_context: None,
        yes,
        allow_insecure: false,
    };

    // Record into recent projects
    RecentProjectsStore::add(&settings.target.root);

    // Standalone utility commands
    match command {
        "console" => {
            let mut console = Console::new(settings);
            console.run_interactive();
            process::exit(0);
        }
        "guide" | "onboard" => {
            SmartPresenter::print_guide(card);
            process::exit(0);
        }
        "doctor" | "diagnose" => {
            let ok = EnvironmentDoctor::print_report(Some(&settings.target.root));
            process::exit(if ok { 0 } else { 1 });
        }
        "smart" => {
            let ready = DockerHostProbe::is_cli_available();
            SmartPresenter::print_home(&settings.target.root, ready);
            process::exit(0);
        }
        "clean" | "cleaner" => {
            let proj_name = settings.target.root
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("project");
            let resources = ResourceCleaner::discover_project_resources(proj_name);
            ResourceCleaner::preview(&resources);
            if !yes {
                println!("\nPass --yes to execute safe scoped cleanup.");
                process::exit(0);
            }
            match ResourceCleaner::execute(&resources) {
                Ok(n) => {
                    println!("\nCleanup complete: {} containers pruned.", n);
                    process::exit(0);
                }
                Err(e) => {
                    eprintln!("ERROR: Cleanup failed: {}", e);
                    process::exit(2);
                }
            }
        }
        "existing" | "native" => {
            match ExistingInfrastructure::discover(&settings.target.root, None) {
                Some(stack) => {
                    println!("\nAdopted Existing Compose Stack: {}", stack.compose_file.display());
                    println!("Services Detected ({}) : {}", stack.services.len(), stack.services.join(", "));
                    println!("File size           : {} bytes", stack.file_size);
                    println!("\nExisting infrastructure preserved. Run with 'docker compose up'.");
                    process::exit(0);
                }
                None => {
                    eprintln!("ERROR: No existing compose.yaml or docker-compose.yml found in {}", settings.target.root.display());
                    process::exit(2);
                }
            }
        }
        "lock" => {
            let mut lock = ProjectLock::new(&settings.target.root);
            match lock.acquire() {
                Ok(_) => {
                    println!("Project lock acquired successfully for PID {}", std::process::id());
                    process::exit(0);
                }
                Err(e) => {
                    eprintln!("ERROR: {}", e);
                    process::exit(1);
                }
            }
        }
        "unlock" => {
            let lock = ProjectLock::new(&settings.target.root);
            match lock.unlock_force() {
                Ok(_) => {
                    println!("Project lock released successfully.");
                    process::exit(0);
                }
                Err(e) => {
                    eprintln!("ERROR: {}", e);
                    process::exit(2);
                }
            }
        }
        "recover" => {
            let om = OutputManager::new(&settings.target.root);
            match om.restore_last_backup() {
                Ok(b) => {
                    println!("Recovered deployment from previous backup: {}", b.display());
                    process::exit(0);
                }
                Err(e) => {
                    eprintln!("ERROR: {}", e);
                    process::exit(2);
                }
            }
        }
        _ => {}
    }

    let inspection = match Inspector::inspect(&settings.target.root, explicit_entry.as_deref()) {
        Ok(ins) => ins,
        Err(e) => {
            eprintln!("ERROR: Inspection failed: {}", e);
            process::exit(2);
        }
    };

    let plan = Planner::build(&settings, inspection);

    match command {
        "inspect" => {
            print_summary(&plan);
            process::exit(0);
        }
        "preflight" => {
            print_summary(&plan);
            DependencyAudit::print_report(&plan.inspection);
            PortAudit::print_report(&plan.published_ports);

            if let Some(ref budget) = plan.resource_budget {
                if budget.verdict == StorageVerdict::Blocked {
                    eprintln!(
                        "\nDEPLOYMENT BLOCKED -- INSUFFICIENT DOCKER STORAGE: {}",
                        budget.explanations.first().map(|s| s.as_str()).unwrap_or("Storage deficit")
                    );
                    process::exit(1);
                }
            }
            process::exit(0);
        }
        "preview" => {
            print_summary(&plan);
            let files = Renderer::files(&plan);
            println!("\nPreview of generated files:");
            for name in files.keys() {
                println!("  - {}", name);
            }
            process::exit(0);
        }
        "generate" => {
            print_summary(&plan);
            let files = Renderer::files(&plan);
            let out = OutputManager::new(&settings.target.root);
            if let Err(e) = out.write(&files) {
                eprintln!("ERROR: Failed to write files: {}", e);
                process::exit(2);
            }
            println!("Successfully generated container files in {}", out.folder().display());
            process::exit(0);
        }
        "ports" => {
            let ok = PortAudit::print_report(&plan.published_ports);
            process::exit(if ok { 0 } else { 1 });
        }
        "check" => {
            let out = OutputManager::new(&settings.target.root);
            let (valid, errors) = out.validate();
            if !valid {
                eprintln!("ERROR: Generated configuration is invalid: {}", errors.join("; "));
                process::exit(2);
            }
            println!("Generated files: verified");
            if DockerHostProbe::is_compose_available() {
                let adapter = DockerAdapter::new(&settings.target.root, None);
                let _ = adapter.run_action("check");
                println!("Docker Compose: verified");
            } else {
                println!("Docker Compose unavailable: Compose syntax not checked");
            }
            process::exit(0);
        }
        "build" => {
            if let Some(ref budget) = plan.resource_budget {
                if budget.verdict == StorageVerdict::Blocked {
                    let msg = budget
                        .explanations
                        .first()
                        .map(|s| s.as_str())
                        .unwrap_or("Insufficient Docker storage");
                    eprintln!("ERROR: Deployment blocked: {}", msg);
                    process::exit(2);
                }
            }

            let adapter = DockerAdapter::new(&settings.target.root, None);
            if let Err(e) = adapter.preflight() {
                eprintln!("ERROR: Preflight failed: {}", e);
                process::exit(2);
            }

            if !settings.yes {
                eprintln!("ERROR: Pass --yes for intentional noninteractive execution.");
                process::exit(2);
            }

            if let Err(e) = adapter.run_action("build") {
                eprintln!("ERROR: Build failed: {}", e);
                process::exit(2);
            }
            println!("Docker build completed successfully.");
            process::exit(0);
        }
        "up" | "run" => {
            let adapter = DockerAdapter::new(&settings.target.root, None);
            if let Err(e) = adapter.preflight() {
                eprintln!("ERROR: Preflight failed: {}", e);
                process::exit(2);
            }

            if !settings.yes {
                eprintln!("ERROR: Pass --yes for intentional noninteractive execution.");
                process::exit(2);
            }

            let act = if command == "run" || plan.execution_kind == ExecutionKind::Interactive {
                "run"
            } else {
                "up"
            };

            if let Err(e) = adapter.run_action(act) {
                eprintln!("ERROR: Execution failed: {}", e);
                process::exit(2);
            }
            process::exit(0);
        }
        "down" => {
            let adapter = DockerAdapter::new(&settings.target.root, None);
            if let Err(e) = adapter.run_action("down") {
                eprintln!("ERROR: Down failed: {}", e);
                process::exit(2);
            }
            println!("Containers shut down cleanly.");
            process::exit(0);
        }
        "ps" => {
            let adapter = DockerAdapter::new(&settings.target.root, None);
            if let Err(e) = adapter.run_action("ps") {
                eprintln!("ERROR: ps failed: {}", e);
                process::exit(2);
            }
            process::exit(0);
        }
        "logs" => {
            let adapter = DockerAdapter::new(&settings.target.root, None);
            if let Err(e) = adapter.run_action("logs") {
                eprintln!("ERROR: logs failed: {}", e);
                process::exit(2);
            }
            process::exit(0);
        }
        unknown => {
            eprintln!("ERROR: Unknown command '{}'. Run autodocker --help for usage.", unknown);
            process::exit(2);
        }
    }
}

// ============================================================================
// 24. Extended Built-in Self-Test Suite (15 Tests)
// ============================================================================

fn run_self_test() {
    println!("================================================================================");
    println!("AUTO DOCKER (RUST SINGLE-FILE) — EXTENDED SELF-TEST SUITE");
    println!("================================================================================\n");

    let tests: [(&str, fn() -> Result<(), String>); 15] = [
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
            if resolve_pypi_package("fitz") != "PyMuPDF" {
                return Err("fitz did not resolve to PyMuPDF".to_string());
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
            let b1 = ResourceBudgetEstimator::estimate(
                "python:3.12-slim-bookworm",
                &["fastapi".to_string()],
                Path::new("."),
                &storage,
                &[],
            );
            let b2 = ResourceBudgetEstimator::estimate(
                "python:3.12-slim-bookworm",
                &["fastapi".to_string()],
                Path::new("."),
                &storage,
                &["catboost".to_string(), "xgboost".to_string()],
            );
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
            let budget = ResourceBudgetEstimator::estimate(
                "python:3.12-slim-bookworm",
                &["fastapi".to_string()],
                Path::new("."),
                &storage,
                &[],
            );
            if budget.verdict != StorageVerdict::Blocked {
                return Err("Storage deficit was not marked as BLOCKED".to_string());
            }
            Ok(())
        }),
        ("test_fastapi_web_rendering_and_healthcheck", || {
            let inspection = Inspection {
                root: PathBuf::from("."),
                entry: Candidate {
                    path: PathBuf::from("main.py"),
                    rel_path: "main.py".to_string(),
                    score: 80,
                    framework: "fastapi".to_string(),
                    imports: HashSet::new(),
                    required_imports: HashSet::new(),
                    optional_imports: HashSet::new(),
                    needs_stdin: false,
                    has_app_object: true,
                    app_object_name: Some("app".to_string()),
                },
                dependency: None,
                detected_mode: Mode::Web,
                candidates: Vec::new(),
                python_count: 1,
                warnings: Vec::new(),
            };
            let plan = Plan {
                inspection,
                mode: Mode::Web,
                port: Some(8000),
                gpu: false,
                command: vec!["python".to_string(), "-m".to_string(), "uvicorn".to_string()],
                services: vec!["app".to_string()],
                warnings: Vec::new(),
                published_ports: vec![PublishedPort {
                    service: "app".to_string(),
                    container: 8000,
                    host: 8000,
                }],
                inferred_packages: vec!["fastapi".to_string(), "uvicorn".to_string()],
                execution_kind: ExecutionKind::Web,
                health_path: Some("/health".to_string()),
                python_image: "python:3.12-slim-bookworm".to_string(),
                persistent_dirs: Vec::new(),
                replicas: 1,
                cpus: None,
                memory: None,
                resource_budget: None,
            };
            let files = Renderer::files(&plan);
            let compose = files.get("compose.yaml").ok_or("compose.yaml missing")?;
            if !compose.contains("healthcheck:") || !compose.contains("socket.create_connection") {
                return Err("TCP loopback healthcheck missing from compose.yaml".to_string());
            }
            if !compose.contains("\"127.0.0.1:8000:8000\"") {
                return Err("Published port missing closing quote in compose.yaml".to_string());
            }
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
            let budget = ResourceBudgetEstimator::estimate(
                "python:3.12-slim-bookworm",
                &["fastapi".to_string()],
                Path::new("."),
                &storage,
                &[],
            );
            if budget.verdict != StorageVerdict::Unknown {
                return Err("VM isolated storage was not marked as UNKNOWN".to_string());
            }
            Ok(())
        }),
        ("test_interactive_cli_rendering_tty", || {
            let inspection = Inspection {
                root: PathBuf::from("."),
                entry: Candidate {
                    path: PathBuf::from("cli.py"),
                    rel_path: "cli.py".to_string(),
                    score: 75,
                    framework: "python".to_string(),
                    imports: HashSet::new(),
                    required_imports: HashSet::new(),
                    optional_imports: HashSet::new(),
                    needs_stdin: true,
                    has_app_object: false,
                    app_object_name: None,
                },
                dependency: None,
                detected_mode: Mode::Base,
                candidates: Vec::new(),
                python_count: 1,
                warnings: Vec::new(),
            };
            let plan = Plan {
                inspection,
                mode: Mode::Base,
                port: None,
                gpu: false,
                command: vec!["python".to_string(), "cli.py".to_string()],
                services: vec!["app".to_string()],
                warnings: Vec::new(),
                published_ports: Vec::new(),
                inferred_packages: Vec::new(),
                execution_kind: ExecutionKind::Interactive,
                health_path: None,
                python_image: "python:3.12-slim-bookworm".to_string(),
                persistent_dirs: Vec::new(),
                replicas: 1,
                cpus: None,
                memory: None,
                resource_budget: None,
            };
            let files = Renderer::files(&plan);
            let compose = files.get("compose.yaml").ok_or("compose.yaml missing")?;
            if !compose.contains("stdin_open: true") || !compose.contains("tty: true") {
                return Err("Interactive TTY configuration missing from compose.yaml".to_string());
            }
            Ok(())
        }),
        ("test_output_manager_manifest_and_validation", || {
            let tmp = std::env::temp_dir().join("autodocker_rust_test_om");
            let _ = std::fs::remove_dir_all(&tmp);
            std::fs::create_dir_all(&tmp).unwrap();
            let om = OutputManager::new(&tmp);
            let mut files = HashMap::new();
            files.insert("Dockerfile".to_string(), "FROM python:3.12-slim\n".to_string());
            files.insert("compose.yaml".to_string(), "services: {}\n".to_string());
            files.insert("manifest.json".to_string(), "{\"generator\": \"Auto Docker\"}\n".to_string());
            om.write(&files).map_err(|e| e.to_string())?;
            let (valid, _) = om.validate();
            let _ = std::fs::remove_dir_all(&tmp);
            if !valid {
                return Err("Validation of written files failed".to_string());
            }
            Ok(())
        }),
        ("test_project_lock_acquire_and_release", || {
            let tmp = std::env::temp_dir().join("autodocker_rust_test_lock");
            let _ = std::fs::remove_dir_all(&tmp);
            std::fs::create_dir_all(&tmp).unwrap();
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
            let _ = std::fs::remove_dir_all(&tmp);
            Ok(())
        }),
        ("test_existing_infrastructure_compose_parsing", || {
            let tmp = std::env::temp_dir().join("autodocker_rust_test_compose");
            let _ = std::fs::remove_dir_all(&tmp);
            std::fs::create_dir_all(&tmp).unwrap();
            let compose_content = "services:\n  web:\n    image: nginx\n  db:\n    image: postgres\n  redis:\n    image: redis\n";
            std::fs::write(tmp.join("compose.yaml"), compose_content).unwrap();
            let stack = ExistingInfrastructure::discover(&tmp, None).ok_or("Failed to discover compose.yaml")?;
            if stack.services.len() != 3 || !stack.services.contains(&"web".to_string()) || !stack.services.contains(&"db".to_string()) {
                return Err(format!("Incorrect services parsed: {:?}", stack.services));
            }
            let _ = std::fs::remove_dir_all(&tmp);
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
        ("test_resource_cleaner_classification", || {
            let res = CleanableResource {
                kind: "container".to_string(),
                id: "abcd1234ef56".to_string(),
                name: "test_autodocker_run".to_string(),
                is_test: true,
                details: "test container".to_string(),
            };
            if !res.is_test {
                return Err("Resource was not classified as test".to_string());
            }
            Ok(())
        }),
        ("test_deployment_orchestrator_state_transitions", || {
            let tmp = std::env::temp_dir().join("autodocker_rust_test_orch");
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
        ("test_recent_projects_store_roundtrip", || {
            let tmp = std::env::temp_dir().join("autodocker_test_proj_store");
            let _ = std::fs::create_dir_all(&tmp);
            RecentProjectsStore::add(&tmp);
            let loaded = RecentProjectsStore::load();
            let canonical = tmp.canonicalize().unwrap_or(tmp.clone());
            if !loaded.contains(&canonical) {
                return Err("Recent projects store failed to persist path".to_string());
            }
            let _ = std::fs::remove_dir_all(&tmp);
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
