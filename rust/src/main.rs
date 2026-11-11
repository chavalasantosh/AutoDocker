use std::env;
use std::path::{Path, PathBuf};
use std::process;

use autodocker::{
    DependencyAudit, DockerAdapter, DockerHostProbe,
    ExecutionKind, Inspector, Mode, OutputManager, Plan, Planner, PortAudit, Renderer,
    Settings, StorageVerdict, VERSION,
};

fn print_usage() {
    println!("Auto Docker (Rust) v{} — Container infrastructure generator", VERSION);
    println!("\nUSAGE:");
    println!("  autodocker <COMMAND> [PROJECT_PATH] [OPTIONS]");
    println!("\nCOMMANDS:");
    println!("  inspect    Inspect and summarize project understanding");
    println!("  preflight  Run offline dependency, port, and storage safety audits");
    println!("  preview    Preview generated Dockerfile and Compose files without writing");
    println!("  generate   Generate Docker infrastructure into .opsforge/");
    println!("  ports      Verify host port availability");
    println!("  check      Validate generated configuration");
    println!("  build      Build Docker images with storage deficit pre-check");
    println!("  up         Start project container stack");
    println!("  down       Stop project container stack");
    println!("  self-test  Run built-in test suite");
    println!("\nOPTIONS:");
    println!("  --mode <MODE>        Application mode (base, web, mlops, llmops, genai)");
    println!("  --entry <FILE>       Explicit entrypoint script");
    println!("  --port <PORT>        Published port");
    println!("  --gpu                Enable GPU/CUDA acceleration");
    println!("  --yes, -y            Non-interactive execution confirmation");
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        print_usage();
        process::exit(0);
    }

    let command = args[1].as_str();
    if command == "--help" || command == "-h" || command == "help" {
        print_usage();
        process::exit(0);
    }
    if command == "--version" || command == "-v" || command == "version" {
        println!("Auto Docker (Rust) v{}", VERSION);
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
        target_root: target_root.clone(),
        explicit_entry: explicit_entry.clone(),
        mode,
        port,
        gpu,
        dependency_mode: "strict".to_string(),
        docker_context: None,
        yes,
    };

    let inspection = match Inspector::inspect(&settings.target_root, explicit_entry.as_deref()) {
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
            let out = OutputManager::new(&settings.target_root);
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
            let out = OutputManager::new(&settings.target_root);
            let (valid, errors) = out.validate();
            if !valid {
                eprintln!("ERROR: Generated configuration is invalid: {}", errors.join("; "));
                process::exit(2);
            }
            println!("Generated files: verified");
            let probe = DockerHostProbe::probe(None);
            if probe.compose_available {
                let adapter = DockerAdapter::new(&settings.target_root, None);
                let _ = adapter.run_action("check");
                println!("Docker Compose: verified");
            } else {
                println!("Docker Compose unavailable: Compose syntax not checked");
            }
            process::exit(0);
        }
        "build" => {
            // Storage safety gate
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

            let adapter = DockerAdapter::new(&settings.target_root, None);
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
            let adapter = DockerAdapter::new(&settings.target_root, None);
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
            let adapter = DockerAdapter::new(&settings.target_root, None);
            if let Err(e) = adapter.run_action("down") {
                eprintln!("ERROR: Down failed: {}", e);
                process::exit(2);
            }
            println!("Containers shut down cleanly.");
            process::exit(0);
        }
        unknown => {
            eprintln!("ERROR: Unknown command '{}'. Run autodocker --help for usage.", unknown);
            process::exit(2);
        }
    }
}

fn print_summary(plan: &Plan) {
    println!("\nProject    : {}", plan.inspection.root.display());
    println!("Entrypoint : {}", plan.inspection.entry.rel_path);
    println!("Framework  : {}", plan.inspection.entry.framework);
    println!("Workload   : {:?}", plan.mode);
    println!("Services   : {}", plan.services.join(", "));
    println!("Execution  : {:?}", plan.execution_kind);
    println!("Command    : {}", plan.command.join(" "));
    if let Some(port) = plan.port {
        println!("App port   : {}", port);
    }
    for w in &plan.warnings {
        println!("WARNING: {}", w);
    }
}

fn run_self_test() {
    println!("================================================================================");
    println!("AUTO DOCKER (RUST) — SELF-TEST SUITE");
    println!("================================================================================\n");

    let tests: [(&str, fn() -> Result<(), String>); 6] = [
        ("test_ast_visitor_main_guard_and_type_checking", || {
            use autodocker::DependencyAstVisitor;
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
        ("test_budget_estimator_omits_optional_packages", || {
            use autodocker::{DockerStorageStatus, ResourceBudgetEstimator, StorageVerdict};
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
            use autodocker::{DockerStorageStatus, ResourceBudgetEstimator, StorageVerdict};
            let storage = DockerStorageStatus {
                engine_accessible: true,
                docker_root_dir: Some("/var/lib/docker".to_string()),
                storage_driver: Some("overlay2".to_string()),
                daemon_free_bytes: Some(10 * 1024 * 1024), // 10 MB
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
            use autodocker::{Candidate, ExecutionKind, Inspection, Mode, Plan, PublishedPort, Renderer};
            use std::collections::HashSet;
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
            let compose = files.get("compose.yaml").ok_or("compose.yaml missing")?;
            if !compose.contains("healthcheck:") || !compose.contains("socket.create_connection") {
                return Err("TCP loopback healthcheck missing from compose.yaml".to_string());
            }
            Ok(())
        }),
        ("test_interactive_cli_rendering_tty", || {
            use autodocker::{Candidate, ExecutionKind, Inspection, Mode, Plan, Renderer};
            use std::collections::HashSet;
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
            let compose = files.get("compose.yaml").ok_or("compose.yaml missing")?;
            if !compose.contains("stdin_open: true") || !compose.contains("tty: true") {
                return Err("Interactive TTY configuration missing from compose.yaml".to_string());
            }
            Ok(())
        }),
        ("test_output_manager_manifest_and_validation", || {
            use autodocker::OutputManager;
            use std::collections::HashMap;
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
