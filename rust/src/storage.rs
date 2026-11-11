use std::process::Command;
use std::sync::Mutex;
use crate::models::DockerStorageStatus;

static MOCK_STATUS: Mutex<Option<DockerStorageStatus>> = Mutex::new(None);

pub struct DockerStorageProbe;

impl DockerStorageProbe {
    pub fn set_mock(status: Option<DockerStorageStatus>) {
        let mut mock = MOCK_STATUS.lock().unwrap();
        *mock = status;
    }

    pub fn probe(context: Option<&str>) -> DockerStorageStatus {
        if let Some(ref mock) = *MOCK_STATUS.lock().unwrap() {
            return mock.clone();
        }

        let docker_cmd = if cfg!(windows) { "docker.exe" } else { "docker" };

        // Test if docker CLI exists
        let cli_check = Command::new(docker_cmd).arg("--version").output();
        if cli_check.is_err() {
            return DockerStorageStatus {
                engine_accessible: false,
                docker_root_dir: None,
                storage_driver: None,
                daemon_free_bytes: None,
                daemon_total_bytes: None,
                reclaimable_build_cache_bytes: 0,
                reclaimable_images_bytes: 0,
                is_remote_context: false,
                is_vm_isolated: false,
                assessment_status: "UNKNOWN".to_string(),
                details: "Docker CLI not found on host PATH".to_string(),
            };
        }

        let mut base_args = Vec::new();
        let mut is_remote = false;
        if let Some(ctx) = context {
            base_args.extend(["--context", ctx]);
            // Check context endpoint
            if let Ok(ctx_out) = Command::new(docker_cmd)
                .args(["context", "inspect", ctx, "--format", "{{json .Endpoints.docker.Host}}"])
                .output()
            {
                if ctx_out.status.success() {
                    let s = String::from_utf8_lossy(&ctx_out.stdout).to_lowercase();
                    if s.contains("tcp://") || s.contains("ssh://") || s.contains("http://") || s.contains("https://") {
                        is_remote = true;
                    }
                }
            }
        }

        if is_remote {
            return DockerStorageStatus {
                engine_accessible: true,
                docker_root_dir: None,
                storage_driver: None,
                daemon_free_bytes: None,
                daemon_total_bytes: None,
                reclaimable_build_cache_bytes: 0,
                reclaimable_images_bytes: 0,
                is_remote_context: true,
                is_vm_isolated: false,
                assessment_status: "UNKNOWN".to_string(),
                details: format!("Remote Docker context '{:?}'; storage resides on remote daemon", context),
            };
        }

        // Run docker info
        let mut info_cmd = Command::new(docker_cmd);
        for arg in &base_args {
            info_cmd.arg(arg);
        }
        info_cmd.args(["info", "--format", "{{.DockerRootDir}}|{{.Driver}}"]);

        let (root_dir, driver) = match info_cmd.output() {
            Ok(output) if output.status.success() => {
                let stdout = String::from_utf8_lossy(&output.stdout);
                let parts: Vec<&str> = stdout.trim().split('|').collect();
                let r = parts.first().map(|s| s.trim().to_string());
                let d = parts.get(1).map(|s| s.trim().to_string());
                (r, d)
            }
            Ok(output) => {
                let err = String::from_utf8_lossy(&output.stderr);
                return DockerStorageStatus {
                    engine_accessible: false,
                    docker_root_dir: None,
                    storage_driver: None,
                    daemon_free_bytes: None,
                    daemon_total_bytes: None,
                    reclaimable_build_cache_bytes: 0,
                    reclaimable_images_bytes: 0,
                    is_remote_context: false,
                    is_vm_isolated: false,
                    assessment_status: "UNKNOWN".to_string(),
                    details: format!("Docker daemon unreachable: {}", err.trim()),
                };
            }
            Err(e) => {
                return DockerStorageStatus {
                    engine_accessible: false,
                    docker_root_dir: None,
                    storage_driver: None,
                    daemon_free_bytes: None,
                    daemon_total_bytes: None,
                    reclaimable_build_cache_bytes: 0,
                    reclaimable_images_bytes: 0,
                    is_remote_context: false,
                    is_vm_isolated: false,
                    assessment_status: "UNKNOWN".to_string(),
                    details: format!("Failed to query Docker daemon: {}", e),
                };
            }
        };

        // Storage isolation logic for non-Linux hosts (Windows, macOS running Docker Desktop VM)
        let mut is_vm = false;
        let mut assessment = "UNKNOWN".to_string();
        let free_bytes: Option<u64> = None;
        let total_bytes: Option<u64> = None;
        let detail_msg;

        if let Some(ref dir) = root_dir {
            if !cfg!(target_os = "linux") && dir.starts_with('/') {
                is_vm = true;
                assessment = "UNKNOWN".to_string();
                detail_msg = format!("Docker daemon storage root ({}) is isolated inside VM/WSL2; host disk space does not reflect daemon space.", dir);
            } else {
                detail_msg = format!("Docker daemon storage root ({}) checked", dir);
            }
        } else {
            detail_msg = "Docker root directory was not reported by daemon".to_string();
        }

        DockerStorageStatus {
            engine_accessible: true,
            docker_root_dir: root_dir,
            storage_driver: driver,
            daemon_free_bytes: free_bytes,
            daemon_total_bytes: total_bytes,
            reclaimable_build_cache_bytes: 0,
            reclaimable_images_bytes: 0,
            is_remote_context: false,
            is_vm_isolated: is_vm,
            assessment_status: assessment,
            details: detail_msg,
        }
    }
}

