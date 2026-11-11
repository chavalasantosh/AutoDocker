use std::path::{Path, PathBuf};
use std::process::Command;
use crate::models::OUTPUT_DIR;

#[derive(Debug, Clone)]
pub struct DockerHostStatus {
    pub cli_installed: bool,
    pub cli_path: Option<String>,
    pub compose_available: bool,
    pub compose_version: Option<String>,
    pub engine_reachable: bool,
    pub server_version: Option<String>,
    pub error_detail: Option<String>,
}

pub struct DockerHostProbe;

impl DockerHostProbe {
    pub fn probe(context: Option<&str>) -> DockerHostStatus {
        let docker_cmd = if cfg!(windows) { "docker.exe" } else { "docker" };

        let mut base_args = Vec::new();
        if let Some(ctx) = context {
            base_args.extend(["--context", ctx]);
        }

        // 1. Docker CLI
        let cli_check = Command::new(docker_cmd).arg("--version").output();
        if cli_check.is_err() {
            return DockerHostStatus {
                cli_installed: false,
                cli_path: None,
                compose_available: false,
                compose_version: None,
                engine_reachable: false,
                server_version: None,
                error_detail: Some("Docker CLI not found on host PATH".to_string()),
            };
        }

        // 2. Compose
        let mut compose_cmd = Command::new(docker_cmd);
        for arg in &base_args {
            compose_cmd.arg(arg);
        }
        compose_cmd.args(["compose", "version"]);

        let (compose_avail, compose_ver) = match compose_cmd.output() {
            Ok(out) if out.status.success() => {
                let v = String::from_utf8_lossy(&out.stdout).trim().to_string();
                (true, Some(v))
            }
            _ => (false, None),
        };

        // 3. Engine
        let mut info_cmd = Command::new(docker_cmd);
        for arg in &base_args {
            info_cmd.arg(arg);
        }
        info_cmd.args(["info", "--format", "{{.ServerVersion}}"]);

        let (engine_ok, srv_ver, err_detail) = match info_cmd.output() {
            Ok(out) if out.status.success() => {
                let v = String::from_utf8_lossy(&out.stdout).trim().to_string();
                (true, Some(v), None)
            }
            Ok(out) => {
                let err = String::from_utf8_lossy(&out.stderr).trim().to_string();
                (false, None, Some(err))
            }
            Err(e) => (false, None, Some(e.to_string())),
        };

        DockerHostStatus {
            cli_installed: true,
            cli_path: Some(docker_cmd.to_string()),
            compose_available: compose_avail,
            compose_version: compose_ver,
            engine_reachable: engine_ok,
            server_version: srv_ver,
            error_detail: err_detail,
        }
    }
}

pub struct DockerAdapter {
    root: PathBuf,
    context: Option<String>,
}

impl DockerAdapter {
    pub fn new(root: &Path, context: Option<String>) -> Self {
        Self {
            root: root.to_path_buf(),
            context,
        }
    }

    pub fn compose_file(&self) -> PathBuf {
        self.root.join(OUTPUT_DIR).join("compose.yaml")
    }

    pub fn preflight(&self) -> Result<(), String> {
        let probe = DockerHostProbe::probe(self.context.as_deref());
        if !probe.cli_installed {
            return Err("Docker CLI not found on host PATH. Install Docker to build/run containers.".to_string());
        }
        if !probe.compose_available {
            return Err("Docker Compose plugin is not installed or available.".to_string());
        }
        if !probe.engine_reachable {
            return Err(format!(
                "Docker Engine is unreachable: {}",
                probe.error_detail.unwrap_or_else(|| "daemon not running".to_string())
            ));
        }
        if !self.compose_file().is_file() {
            return Err("No generated Compose file found. Run 'generate' first.".to_string());
        }
        Ok(())
    }

    pub fn run_action(&self, action: &str) -> Result<(), String> {
        let docker_cmd = if cfg!(windows) { "docker.exe" } else { "docker" };
        let mut cmd = Command::new(docker_cmd);

        if let Some(ref ctx) = self.context {
            cmd.args(["--context", ctx]);
        }

        cmd.arg("compose");
        cmd.args(["-f", self.compose_file().to_str().unwrap()]);

        match action {
            "build" => {
                cmd.arg("build");
            }
            "up" => {
                cmd.args(["up", "-d"]);
            }
            "run" => {
                cmd.args(["run", "--rm", "app"]);
            }
            "down" => {
                cmd.arg("down");
            }
            "check" => {
                cmd.args(["config", "-q"]);
            }
            _ => return Err(format!("Unknown Docker action: {}", action)),
        }

        cmd.current_dir(&self.root);

        let status = cmd
            .status()
            .map_err(|e| format!("Failed to invoke Docker CLI: {}", e))?;

        if status.success() {
            Ok(())
        } else {
            Err(format!("Docker {} failed with exit code: {:?}", action, status.code()))
        }
    }
}

