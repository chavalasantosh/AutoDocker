use std::collections::HashSet;
use std::path::Path;
use walkdir::WalkDir;
use crate::models::{DockerStorageStatus, ResourceBudget, StorageVerdict};

pub struct ResourceBudgetEstimator;

impl ResourceBudgetEstimator {
    pub fn estimate_context_bytes(root: &Path, max_files: usize) -> u64 {
        let mut total: u64 = 0;
        let mut count: usize = 0;
        let ignore_dirs: HashSet<&'static str> = [
            ".git", ".opsforge", "__pycache__", ".venv", "venv", ".idea", ".vscode", "node_modules", ".cache",
        ]
        .into_iter()
        .collect();

        for entry in WalkDir::new(root).into_iter().filter_entry(|e| {
            if let Some(name) = e.file_name().to_str() {
                !ignore_dirs.contains(name)
            } else {
                true
            }
        }) {
            if count >= max_files {
                break;
            }
            if let Ok(entry) = entry {
                if entry.file_type().is_file() {
                    if let Ok(meta) = entry.metadata() {
                        total += meta.len();
                        count += 1;
                    }
                }
            }
        }
        total.min(500 * 1024 * 1024)
    }

    pub fn estimate(
        base_image: &str,
        packages: &[String],
        root: &Path,
        storage_status: &DockerStorageStatus,
        optional_packages: &[String],
    ) -> ResourceBudget {
        let img_lower = base_image.to_lowercase();
        let base_bytes: u64 = if img_lower.contains("slim") {
            160 * 1024 * 1024
        } else if img_lower.contains("alpine") {
            60 * 1024 * 1024
        } else if img_lower.contains("cuda") {
            3500 * 1024 * 1024
        } else {
            950 * 1024 * 1024
        };

        let mut heavy_pkgs: Vec<String> = Vec::new();
        let mut dl_sum: u64 = 0;
        let mut ext_sum: u64 = 0;

        for pkg in packages {
            let (dl, ext, is_heavy) = get_package_footprint(pkg);
            dl_sum += dl;
            ext_sum += ext;
            if is_heavy && !heavy_pkgs.contains(pkg) {
                heavy_pkgs.push(pkg.clone());
            }
        }

        let mut opt_ext_sum: u64 = 0;
        let mut opt_heavy_pkgs: Vec<String> = Vec::new();
        for pkg in optional_packages {
            let (_, ext, is_heavy) = get_package_footprint(pkg);
            opt_ext_sum += ext;
            if is_heavy && !opt_heavy_pkgs.contains(pkg) {
                opt_heavy_pkgs.push(pkg.clone());
            }
        }

        let context_bytes = Self::estimate_context_bytes(root, 1000);
        let min_bytes = base_bytes + ext_sum + context_bytes;
        let max_bytes = base_bytes + ((dl_sum + ext_sum) as f64 * 1.35) as u64 + context_bytes + (250 * 1024 * 1024);

        let mut explanations: Vec<String> = Vec::new();
        let mut recommendations: Vec<String> = Vec::new();

        if !heavy_pkgs.is_empty() {
            explanations.push(format!("Heavy ML/computation packages detected: {}", heavy_pkgs.join(", ")));
        }

        if !optional_packages.is_empty() {
            let opt_gib = opt_ext_sum as f64 / (1024.0 * 1024.0 * 1024.0);
            explanations.push(format!(
                "Optional candidates in guarded blocks omitted from base build ({}); if included, would add ~{:.1} GiB.",
                optional_packages.join(", "),
                opt_gib
            ));
        }

        let verdict;
        let confidence;

        if let Some(free) = storage_status.daemon_free_bytes {
            if free < min_bytes {
                verdict = StorageVerdict::Blocked;
                confidence = "HIGH".to_string();
                let free_gib = free as f64 / (1024.0 * 1024.0 * 1024.0);
                let min_gib = min_bytes as f64 / (1024.0 * 1024.0 * 1024.0);
                explanations.push(format!(
                    "Estimated build requires at least {:.1} GiB, but only {:.1} GiB free on Docker storage.",
                    min_gib, free_gib
                ));
                recommendations.push(format!(
                    "Free up at least {:.1} GiB before building container images.",
                    (min_bytes.saturating_sub(free)) as f64 / (1024.0 * 1024.0 * 1024.0) + 1.0
                ));
            } else if free < max_bytes {
                verdict = StorageVerdict::Warning;
                confidence = "HIGH".to_string();
                explanations.push("Free storage is near the estimated upper build boundary.".to_string());
            } else {
                verdict = StorageVerdict::Sufficient;
                confidence = if storage_status.assessment_status == "CONFIRMED" { "HIGH" } else { "MEDIUM" }.to_string();
            }
        } else {
            verdict = StorageVerdict::Unknown;
            confidence = if storage_status.is_vm_isolated || storage_status.is_remote_context { "LOW" } else { "MEDIUM" }.to_string();
            explanations.push(format!("Docker storage capacity could not be queried directly ({})", storage_status.details));
            recommendations.push(format!(
                "Ensure at least {:.1} GiB free space on your Docker storage before building.",
                max_bytes as f64 / (1024.0 * 1024.0 * 1024.0)
            ));
        }

        ResourceBudget {
            base_image: base_image.to_string(),
            base_image_bytes_est: base_bytes,
            packages_bytes_min: ext_sum,
            packages_bytes_max: ext_sum + dl_sum,
            build_headroom_bytes: 1024 * 1024 * 1024,
            context_bytes,
            total_bytes_min: min_bytes,
            total_bytes_max: max_bytes,
            confidence,
            verdict,
            heavy_packages: heavy_pkgs,
            optional_candidates: optional_packages.to_vec(),
            explanations,
            recommendations,
        }
    }
}

fn get_package_footprint(name: &str) -> (u64, u64, bool) {
    let lower = name.to_lowercase();
    match lower.as_str() {
        "torch" | "pytorch" => (850 * 1024 * 1024, 2200 * 1024 * 1024, true),
        "tensorflow" => (550 * 1024 * 1024, 1500 * 1024 * 1024, true),
        "catboost" => (120 * 1024 * 1024, 380 * 1024 * 1024, true),
        "lightgbm" => (80 * 1024 * 1024, 250 * 1024 * 1024, true),
        "xgboost" => (100 * 1024 * 1024, 320 * 1024 * 1024, true),
        "scipy" => (45 * 1024 * 1024, 160 * 1024 * 1024, true),
        "pandas" => (35 * 1024 * 1024, 120 * 1024 * 1024, false),
        "numpy" => (20 * 1024 * 1024, 70 * 1024 * 1024, false),
        "transformers" => (150 * 1024 * 1024, 450 * 1024 * 1024, true),
        "mlflow" => (60 * 1024 * 1024, 180 * 1024 * 1024, true),
        "fastapi" => (5 * 1024 * 1024, 15 * 1024 * 1024, false),
        "uvicorn" => (3 * 1024 * 1024, 10 * 1024 * 1024, false),
        "flask" => (4 * 1024 * 1024, 12 * 1024 * 1024, false),
        _ => (10 * 1024 * 1024, 30 * 1024 * 1024, false),
    }
}

