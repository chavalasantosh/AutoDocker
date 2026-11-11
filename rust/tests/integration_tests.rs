use std::collections::{HashMap, HashSet};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};

use autodocker::{
    Candidate, DockerStorageStatus, ExecutionKind, Inspection, Mode, OutputManager, Plan,
    PublishedPort, Renderer, ResourceBudgetEstimator, StorageVerdict,
};

#[test]
fn test_fastapi_web_rendering_and_healthcheck() {
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
        resource_budget: None,
    };
    let files = Renderer::files(&plan);
    let compose = files.get("compose.yaml").expect("compose.yaml missing");
    assert!(compose.contains("healthcheck:"));
    assert!(compose.contains("socket.create_connection"));
    assert!(compose.contains("\"127.0.0.1:8000:8000\""));
}

#[test]
fn test_interactive_cli_rendering_tty() {
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
        resource_budget: None,
    };
    let files = Renderer::files(&plan);
    let compose = files.get("compose.yaml").expect("compose.yaml missing");
    assert!(compose.contains("stdin_open: true"));
    assert!(compose.contains("tty: true"));
}

#[test]
fn test_output_manager_manifest_and_validation() {
    let tmp = env::temp_dir().join("autodocker_rust_test_om_crate");
    let _ = fs::remove_dir_all(&tmp);
    fs::create_dir_all(&tmp).unwrap();
    let om = OutputManager::new(&tmp);
    let mut files = HashMap::new();
    files.insert("Dockerfile".to_string(), "FROM python:3.12-slim\n".to_string());
    files.insert("compose.yaml".to_string(), "services: {}\n".to_string());
    files.insert("manifest.json".to_string(), "{\"generator\": \"Auto Docker\"}\n".to_string());
    om.write(&files).unwrap();
    let (valid, _) = om.validate();
    let _ = fs::remove_dir_all(&tmp);
    assert!(valid);
}

#[test]
fn test_budget_estimator_omits_optional_packages() {
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
    assert_eq!(b1.total_bytes_min, b2.total_bytes_min);
    assert_eq!(b1.verdict, StorageVerdict::Sufficient);
}

#[test]
fn test_storage_deficit_verdict_blocked() {
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
    assert_eq!(budget.verdict, StorageVerdict::Blocked);
}

#[test]
fn test_vm_isolated_storage_verdict_unknown() {
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
    assert_eq!(budget.verdict, StorageVerdict::Unknown);
}
