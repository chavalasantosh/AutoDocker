use std::collections::HashSet;
use std::fs;
use std::path::Path;
use walkdir::WalkDir;
use crate::ast_visitor::DependencyAstVisitor;
use crate::models::{Candidate, Inspection, Mode, MAX_PYTHON_FILE_BYTES, MAX_VISITED_FILES};

pub struct Inspector;

impl Inspector {
    pub fn inspect(root: &Path, explicit_entry: Option<&str>) -> Result<Inspection, String> {
        let canonical_root = root.canonicalize().map_err(|e| format!("Invalid root path: {}", e))?;

        let mut py_files = Vec::new();
        let ignore_dirs: HashSet<&'static str> = [
            ".git", ".opsforge", "__pycache__", ".venv", "venv", ".idea", ".vscode", "node_modules", ".cache",
        ]
        .into_iter()
        .collect();

        for entry in WalkDir::new(&canonical_root).into_iter().filter_entry(|e| {
            if let Some(name) = e.file_name().to_str() {
                !ignore_dirs.contains(name)
            } else {
                true
            }
        }) {
            if py_files.len() >= MAX_VISITED_FILES {
                break;
            }
            if let Ok(entry) = entry {
                if entry.file_type().is_file() {
                    let p = entry.path();
                    if p.extension().and_then(|ext| ext.to_str()) == Some("py") {
                        py_files.push(p.to_path_buf());
                    }
                }
            }
        }

        if py_files.is_empty() {
            return Err("No Python files found in project".to_string());
        }

        let mut candidates = Vec::new();
        let mut best_candidate: Option<Candidate> = None;
        let mut highest_score = 0;

        for py_path in &py_files {
            if let Ok(meta) = fs::metadata(py_path) {
                if meta.len() > MAX_PYTHON_FILE_BYTES {
                    continue;
                }
            }
            let source = match fs::read_to_string(py_path) {
                Ok(s) => s,
                Err(_) => continue,
            };

            let mut visitor = DependencyAstVisitor::new();
            visitor.parse_source(&source);

            let rel_path = py_path
                .strip_prefix(&canonical_root)
                .unwrap_or(py_path)
                .to_string_lossy()
                .replace('\\', "/");

            let file_name = py_path.file_name().and_then(|f| f.to_str()).unwrap_or("");

            let mut score = 10;
            if file_name == "main.py" || file_name == "app.py" || file_name == "server.py" {
                score += 40;
            } else if file_name == "cli.py" || file_name == "run.py" {
                score += 30;
            }

            if !rel_path.contains('/') {
                score += 20; // Root level preference
            }

            if visitor.has_app_object {
                score += 30;
            }

            if source.contains("__name__") && source.contains("__main__") {
                score += 25;
            }

            let framework = visitor
                .detected_framework
                .clone()
                .unwrap_or_else(|| "python".to_string());

            let candidate = Candidate {
                path: py_path.clone(),
                rel_path: rel_path.clone(),
                score,
                framework,
                imports: visitor.all_runtime_imports(),
                required_imports: visitor.required_imports,
                optional_imports: visitor.optional_imports,
                needs_stdin: visitor.needs_stdin,
                has_app_object: visitor.has_app_object,
                app_object_name: visitor.app_object_name,
            };

            if let Some(exp) = explicit_entry {
                if rel_path == exp || file_name == exp {
                    best_candidate = Some(candidate.clone());
                    highest_score = u32::MAX;
                }
            } else if score > highest_score {
                highest_score = score;
                best_candidate = Some(candidate.clone());
            }

            candidates.push(candidate);
        }

        let entry = best_candidate.ok_or_else(|| "Failed to determine suitable entrypoint".to_string())?;

        // Manifest check
        let mut dependency_manifest = None;
        for mf in ["requirements.txt", "pyproject.toml", "setup.py", "Pipfile"] {
            if canonical_root.join(mf).is_file() {
                dependency_manifest = Some(mf.to_string());
                break;
            }
        }

        // Framework to mode mapping
        let detected_mode = match entry.framework.as_str() {
            "fastapi" | "flask" => Mode::Web,
            "streamlit" | "gradio" => Mode::Web,
            _ => {
                if entry.imports.contains("mlflow") || entry.imports.contains("sklearn") || entry.imports.contains("torch") {
                    Mode::Mlops
                } else if entry.imports.contains("ollama") || entry.imports.contains("openai") || entry.imports.contains("langchain") {
                    Mode::Genai
                } else {
                    Mode::Base
                }
            }
        };

        let mut warnings = Vec::new();
        if dependency_manifest.is_none() {
            warnings.push("No requirements.txt or pyproject.toml at project root. Review dependency handling before building.".to_string());
        }

        Ok(Inspection {
            root: canonical_root,
            entry,
            dependency: dependency_manifest,
            detected_mode,
            candidates,
            python_count: py_files.len(),
            warnings,
        })
    }
}

