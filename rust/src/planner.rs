use std::path::Path;
use crate::budget::ResourceBudgetEstimator;
use crate::models::{ExecutionKind, Inspection, Mode, Plan, PublishedPort, Settings};
use crate::storage::DockerStorageProbe;

pub struct Planner;

impl Planner {
    pub fn build(settings: &Settings, inspection: Inspection) -> Plan {
        let mode = if settings.mode != Mode::Auto {
            settings.mode
        } else {
            inspection.detected_mode
        };

        let framework = inspection.entry.framework.as_str();

        // Execution kind determination
        let execution_kind = match framework {
            "fastapi" | "flask" | "streamlit" | "gradio" => ExecutionKind::Web,
            _ => {
                if inspection.entry.needs_stdin {
                    ExecutionKind::Interactive
                } else {
                    ExecutionKind::Batch
                }
            }
        };

        // Port determination
        let port = settings.port.or_else(|| match framework {
            "fastapi" => Some(8000),
            "flask" => Some(5000),
            "streamlit" => Some(8501),
            "gradio" => Some(7860),
            _ => None,
        });

        // Start command formulation
        let mut inferred_pkgs = Vec::new();
        let entry_file = &inspection.entry.rel_path;
        let module_stem = Path::new(entry_file)
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("app");

        let command = match framework {
            "fastapi" => {
                let app_var = inspection.entry.app_object_name.as_deref().unwrap_or("app");
                inferred_pkgs.push("fastapi".to_string());
                inferred_pkgs.push("uvicorn".to_string());
                vec![
                    "python".to_string(),
                    "-m".to_string(),
                    "uvicorn".to_string(),
                    format!("{}:{}", module_stem, app_var),
                    "--app-dir".to_string(),
                    ".".to_string(),
                    "--host".to_string(),
                    "0.0.0.0".to_string(),
                    "--port".to_string(),
                    port.unwrap_or(8000).to_string(),
                ]
            }
            "flask" => {
                inferred_pkgs.push("flask".to_string());
                vec!["python".to_string(), entry_file.clone()]
            }
            "streamlit" => {
                inferred_pkgs.push("streamlit".to_string());
                vec![
                    "streamlit".to_string(),
                    "run".to_string(),
                    entry_file.clone(),
                    "--server.address".to_string(),
                    "0.0.0.0".to_string(),
                    "--server.port".to_string(),
                    port.unwrap_or(8501).to_string(),
                ]
            }
            _ => vec!["python".to_string(), entry_file.clone()],
        };

        // Persistent dirs discovery
        let mut persistent_dirs = Vec::new();
        for dir_name in ["artifacts", "models", "data", "logs", "checkpoints"] {
            if inspection.root.join(dir_name).is_dir() {
                persistent_dirs.push(dir_name.to_string());
            }
        }

        // Published ports
        let mut published_ports = Vec::new();
        if let Some(p) = port {
            published_ports.push(PublishedPort {
                service: "app".to_string(),
                container: p,
                host: p,
            });
        }

        // Additional services based on mode
        let mut services = vec!["app".to_string()];
        if mode == Mode::Mlops {
            services.push("mlflow".to_string());
            published_ports.push(PublishedPort {
                service: "mlflow".to_string(),
                container: 5000,
                host: 5000,
            });
        }

        // Health path
        let health_path = match execution_kind {
            ExecutionKind::Web => Some("/health".to_string()),
            _ => None,
        };

        // Resource budget evaluation
        let storage_status = DockerStorageProbe::probe(settings.docker_context.as_deref());
        let required_slice: Vec<String> = inspection.entry.required_imports.iter().cloned().collect();
        let optional_slice: Vec<String> = inspection.entry.optional_imports.iter().cloned().collect();

        let budget = ResourceBudgetEstimator::estimate(
            "python:3.12-slim-bookworm",
            &required_slice,
            &inspection.root,
            &storage_status,
            &optional_slice,
        );

        let mut warnings = inspection.warnings.clone();
        if execution_kind == ExecutionKind::Interactive {
            warnings.push("Interactive CLI selected: run with 'run YES' on a real terminal, not detached 'up'.".to_string());
        }

        Plan {
            inspection,
            mode,
            port,
            gpu: settings.gpu,
            command,
            services,
            warnings,
            published_ports,
            inferred_packages: inferred_pkgs,
            execution_kind,
            health_path,
            python_image: "python:3.12-slim-bookworm".to_string(),
            persistent_dirs,
            resource_budget: Some(budget),
        }
    }
}

