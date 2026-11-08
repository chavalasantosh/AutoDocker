# Auto Docker 3.0 RC6 — Release Notes

## Version Overview
- **Release**: Auto Docker 3.0 RC6 (Final Stabilization Release)
- **Architecture**: Single-file Python standard-library automation (`auto_docker.py`)
- **Key Focus**: Production-grade analysis, validation, planning, and deployment reliability for real-world multi-service repositories (modeled after NVIDIA Auto Ontology and Enterprise AutoML/MLOps engines), with beginner-friendly terminal navigation, 3-step Auto-Pilot quick-start wizard, MLOps persistent state mounts, robust approval fingerprinting, multiline paste safety, deterministic state machine lifecycle, and dedicated Docker Resource Cleaner.

---

## What's New in 3.0 RC6

### 1. Intelligent Project Discovery (Milestone RC6-M1)
- **High-Capacity Discovery**: Bypasses the 240-Python-file limit when existing Compose infrastructure is present. Evaluated successfully against repositories with over 1,000 Python files.
- **Artifact Scoring & Hierarchy Detection**: Automatically detects and scores `Dockerfile`, `compose.yaml`, `docker-compose.yml`, `pyproject.toml`, `requirements.txt`, `uv.lock`, `package.json`, and Helm charts.
- **Nested Path Root Suggestions**: When a nested directory (e.g., `dev_tools/` or `backend/src/utils/`) is selected, Auto Docker suggests the authoritative parent project root with explicit score evidence without silently redirecting the user's target.
- **Circular Symlink Immunity**: Tracks resolved directory realpaths to prevent recursion and stack overflow in complex repository structures.

### 2. Existing Infrastructure Analysis (Milestone RC6-M2)
- **Multi-File Compose Support**: Detects and respects ordered Compose files (e.g., `compose.yaml` paired with `compose.override.yaml`).
- **Service Role Classification**: Automatically classifies services into functional roles:
  - `database` (PostgreSQL, MySQL, Redis, MongoDB)
  - `migration` (One-shot migration tools, Alembic, Flyway, restart="no")
  - `backend` (FastAPI, Flask, Django, application servers)
  - `frontend` (React, Vite, Node, Nginx)
  - `worker` (Ingestion, Celery, queue consumers)
  - `admin` (pgAdmin, Adminer, dashboards)
  - `ai` (Ollama, MLflow, vLLM, Triton)
- **Non-Destructive Integrity Guarantee**: Never generates a generic single-container Dockerfile over an existing Compose stack.

### 3. Environment & Secrets Validation Subsystem (Milestone RC6-M3 & Stage 1A)
- **Multi-File Compose Evaluation**: Evaluates required and optional interpolation variables across all selected Compose files (e.g., base and overrides) independently of service count.
- **Environment Variable Precedence**: Strictly enforces Docker Compose precedence:
  1. Host shell environment (`os.environ`)
  2. Project `.env` file
  3. Interpolation fallback defaults (`${VAR:-default}` or `${VAR-default}`)
- **Interpolation Syntax Handling**: Correctly handles `${VAR:?}` (must be set and non-empty), `${VAR?}` (must be set), `${VAR:-default}` (uses default), and un-defaulted optional `${VAR}` (emits INFO note without blocking).
- **Classified Findings Engine**: Reports pre-deployment issues with standardized severity: `BLOCKER`, `WARNING`, `INFO`, `UNKNOWN`.
- **Absolute Redaction**: Under no circumstances are resolved secret values printed in terminal output, logs, or JSON reports.

### 4. Typed Deployment Planning Engine (Milestone RC6-M4)
- **`DeploymentPlan` Model**: Immutable typed plan containing services, dependencies, published ports, volume bindings, and environmental prerequisites.
- **Topological Phase Scheduling**: Automatically groups services into 4 ordered execution phases:
  - Phase 1: Storage / Databases
  - Phase 2: Migrations / Initialization
  - Phase 3: Core Application & AI Services
  - Phase 4: Frontend & Administration
- **`native plan` Command**: Added to both the interactive console and CLI (`auto_docker.py native <PATH> --native-action plan`), with machine-readable `--json` export support.

### 5. Host & Security Gate Enforcement (Milestone RC6-M5 & Stage 1C)
- **Local vs. Remote Context Distinction**: Accurately recognizes when a Docker context points to a remote host; does not conflate local disk usage with remote daemon storage.
- **Strict Security Gate**: Detects `CRITICAL` risk configurations (privileged containers, Docker socket mounts `/var/run/docker.sock`, and public `0.0.0.0` database port publication).
- **Security Gate Blocking**: Prevents mutating operations (`build`, `up`) on `CRITICAL` risk stacks unless explicitly authorized via `--allow-insecure` (CLI) or `insecure on` (Workbench).
- **Zero Cross-Project Leakage**: The `allow_insecure` override automatically resets to `False` on any project switch.

### 6. Corrected Deployment Lifecycle & State Machine (Priority 1 & Pass 3)
- **Deterministic State Machine Transitions**:
  - `native build` transitions to `VALIDATED`.
  - `native up` transitions `VALIDATED -> APPROVED -> STARTING -> VERIFYING`.
  - `native down` transitions `VERIFYING / HEALTHY / DEGRADED -> APPROVED -> ANALYZED`.
  - `native verify` transitions `VERIFYING / VALIDATED -> HEALTHY / DEGRADED`, or remains in `VERIFYING` on unknown health.
- **Separation of Operation Validation from Container Runtime State**:
  - Running read-only checks (`native ready`, `native check`) while containers are running preserves container runtime state (`VERIFYING`, `HEALTHY`, `DEGRADED`) and records static outcome in `last_validation`.
  - A failing Compose check while running preserves the primary syntax error directly without masking it behind an illegal state transition error.
  - Unapproved mutating commands do not corrupt running container state into `CANCELLED`.

### 7. Multiline Paste Protection & Approval Turn Isolation (Pass 3 Priority 1)
- **Pasted Input Block Protection**: Pasting `10\nYES` in a single input event stages deployment but **never** approves it (0 mutating Docker commands executed).
- **Distinct Input Turn Requirement**: Approvals require an explicit user input event delivered after the review screen is presented.
- **One-Use Token Invalidation**: Approval tokens are strictly single-use and are automatically invalidated upon cancellation (`cancel`) or project switch (`project`).
- **Single Central Reader**: All input flows through `Console.loop()` and `Console.feed_input()`; zero nested `input()` calls.

### 8. Comprehensive Content-Aware Deployment Fingerprint (Pass 3 Priority 2)
- **Cryptographic Digest Binding**: `DeploymentDigest.compute()` hashes:
  - Selected Compose files and ordered overrides (content hashes, detecting symlinks and missing files).
  - Referenced Dockerfiles (content hashes, detecting symlinks and missing files).
  - Service commands (`command:`) and entrypoints (`entrypoint:`).
  - Session-local HMAC-SHA256 (`_SESSION_HMAC_KEY`) of `.env` files (protecting secret values without plain text or reusable unkeyed hashes).
  - Deployment action, project root, Docker context, and security policy (`allow_insecure`).
- Any post-staging modification (including editing only `command:` in `compose.yaml`) invalidates the approval token.

### 9. Compose File Selection in the File Browser (Pass 3 Priority 4)
- **Direct Compose & Dockerfile Selection**: Selecting `compose.yaml`, `docker-compose.yml`, or `Dockerfile` in the interactive browser (Option 2) or via `target_for()` sets the parent folder as project root with `pinned=None`.
- **Infrastructure Recognition**: Preserves existing infrastructure byte-for-byte, does not treat Compose file as a Python entrypoint, and displays the existing stack analysis and deployment plan.
- **Actionable File Error Messages**: Selecting an unrelated extension (`.txt`, `.json`, etc.) clearly explains valid choices.

### 10. CLI Alignment & Exit Codes (Pass 3 Priority 5)
- Added `verify` to argparse `--native-action` choices in `main()`.
- Standardized exit codes:
  - `0`: Healthy / unknown running (`VERIFYING`).
  - `1`: Unhealthy / degraded container detected.
  - `2`: Docker unavailable / syntax error / system exception.

### 11. Beginner Guided Journey Acceptance Testing (Pass 3 Priority 6)
- Workbench menu clearly marks the current working directory as default suggestion: `(Current terminal folder; not yet selected as project)`.
- Verified 10-step beginner journey without hidden commands: Open -> Browse -> Select Compose -> Analyze -> Readiness -> Settings -> Preview -> Plan -> Stage -> Explicit Approve.

### 12. Live Docker Acceptance Test Overhaul & Hardening (Pass 4)
- **YAML Syntax Fix**: Replaced inline commands containing unquoted colon-space patterns with a structured `build:` context and dedicated Dockerfile, guaranteeing valid YAML syntax across all Compose specifications.
- **Python HTTP Server Syntax Fix**: Replaced fragile inline semicolons (`python -c "...; class H: ..."`) with a standalone `server.py` test server returning `b'LIVE_TEST_OK'`.
- **Real Image Build Testing**: Live test creates a real temporary `Dockerfile` with `COPY server.py .` and builds custom image `autodocker-live-test:{run_id}`, verifying that image build, container startup, HTTP response, restart, and post-restart verification all succeed against real Docker infrastructure.
- **Project Namespace & Resource Labeling**: Isolates the live test into a unique project namespace (`-p autodocker_live_{run_id}` and `name: autodocker_live_{run_id}`). All resources are tagged with `com.auto_docker.test="true"` and `com.auto_docker.run_id="{run_id}"`.
- **Transparent Authorization & Resource Disclosure**: Live test requires explicit `--yes` flag or interactive confirmation. It clearly discloses expected resource usage upfront:
  - Base image: `python:3.11-alpine` (~20 MB download, ~60 MB uncompressed)
  - Temporary project directory: in OS temp space, removed after completion
  - Temporary ports: random ephemeral loopback port (`127.0.0.1:0`)
- **Host Free Disk Verification**: Checks available disk space on the host ($\ge 2.0\text{ GiB}$) before initiating any build; reports `UNKNOWN` when remote or uninspectable.
- **Top-Level Python Compatibility Guard**: Added `check_python_compatibility()` enforcing Python 3.10+. If run under an older interpreter (e.g., Python 3.8.20 on a Linux server), it displays actionable, user-friendly instructions on how to invoke with Python 3.10+ without modifying the system Python.

### 13. Live-Test Safety Corrections (Pass 5)
- **Strict Loopback-Only Port Binding (`127.0.0.1:{port}:8080`)**: Changed generated Compose port mapping from bare `"{port}:8080"` to `"127.0.0.1:{port}:8080"`, eliminating any accidental external exposure on public or private host network interfaces.
- **Effective Docker Endpoint Verification**:
  - Accurately determines effective endpoint before any mutations, accounting for explicit `--docker-context`, `DOCKER_CONTEXT`, and `DOCKER_HOST` environment variables, as well as the active context from `docker context show`.
  - Inspects endpoint with `docker context inspect` to verify local transport (`unix://`, `npipe://`, `127.0.0.1`, `localhost`).
  - Detects and rejects both explicit and implicit remote contexts (`ssh://`, `tcp://<remote>`) where localhost HTTP verification cannot be guaranteed.

### 14. Dedicated Docker Resource Cleaner Subsystem & Hardening (Pass 6)
- **Production-Grade `ResourceCleaner`**:
  - **Read-Only Discovery**: `ResourceCleaner.scan` discovers containers, images, networks, and volumes without performing any mutating operations.
  - **Ownership Classification**: Tags each discovered resource with `is_test`, `project`, and `is_orphan` using metadata labels (`com.auto_docker.test`, `com.docker.compose.project`, `com.auto_docker.run_id`), image tags (`autodocker-live-test:*`), and network naming patterns.
  - **3 Purpose-Built Cleanup Modes**:
    1. `TEMPORARY_TEST`: Automates safe teardown of disposable test resources.
    2. `PROJECT`: Stops and removes resources belonging to a selected project upon explicit confirmation.
    3. `ORPHAN`: Identifies and purges leftover resources from crashed or interrupted Auto Docker runs.
  - **Volume Protection by Default**: Preserves named volumes and application data by default (`preserve_volumes=True`). Destructive volume removal requires explicit opt-in (`--include-volumes`).
  - **Zero System Prune Guarantee**: Strict safety policy prevents unrestricted `docker system prune` or deletion of unowned/shared host resources.
  - **Multi-Stage Cleanup Execution**: Executes `compose down -v --remove-orphans -t 5` with multi-stage fallback (force-stop, `rm -f -v`, `network rm`, `rmi -f`).
  - **Authoritative Re-Scan Verification**: Re-queries the Docker daemon after removal to guarantee 0 lingering leftovers.
- **Container PID 1 Signal Handling**: Test `server.py` implements explicit `signal.signal` handlers for `SIGTERM` and `SIGINT`, ensuring container PID 1 terminates within milliseconds rather than hanging for 10-second stop timeouts.
- **Linux Remote Context Error Classification**: Accurately differentiates nonexistent/unsupported remote contexts (`Remote Docker context '<name>' is unsupported for live-test`) from daemon inspection failures (`BLOCKED: Unable to inspect Docker context '<name>'`).
- **Interactive UI & CLI Access**:
  - Workbench Option `[12] Docker Resource Cleaner & Recovery` with full interactive sub-menu.
  - CLI actions `cleaner` and `clean` supporting `--scan`, `--preview`, `--clean-test`, `--clean-project`, `--recover-orphans`, `--include-volumes`, and `--yes`.

### 15. Auto-Pilot Guided Quick-Start Wizard & MLOps Persistence (Pass 7)
- **3-Step Interactive Auto-Pilot Wizard (`Detect -> Configure -> Safe Deploy`)**:
  - Prominently positioned at the top of the Workbench menu as Option `[A]` and accessible via CLI (`auto_docker.py wizard`, `quick`, `autopilot`).
  - **Step 1 (Detect)**: Fast, non-destructive static analysis identifying workload type, primary entrypoint, framework, execution style (interactive TTY vs background service), persistent storage paths, auxiliary services (e.g. MLflow), and existing Compose/Dockerfile assets. A single `[ENTER]` accepts the detection findings and advances to Step 2.
  - **Step 2 (Configure)**: Streamlined toggles 1-7 for Workload Profile, Execution Style (with safe fallback to standard web port 8000), Persistent Data preservation, Auxiliary Services, Port override, Hardware Acceleration (NVIDIA GPU), and Scale & Limits (Replicas). A single `[ENTER]` accepts all recommendations and advances to Step 3.
  - **Step 3 (Safe Deploy)**: Preflight safety review displaying verified target, base image, services, scale & limits, loopback port bindings, persistent mounts, security rating, and readiness status. Offers choices `[1] Deploy & Run Now`, `[2] Dry-Run Only`, `[3] Generate Files Only`, `[0] Return to Menu`.
- **Top-Level AST Scope Analysis**:
  - Only module-level assignments to `app` treat an application as a standalone FastAPI or Flask web service.
  - CLI or batch scripts containing embedded inference functions (such as `enterprise_automl_v3_2.py`, where `app = FastAPI(...)` is defined inside `def serve_cmd(args):`) are accurately classified as Python interactive CLI applications (`["python", "enterprise_automl_v3_2.py"]`) rather than causing runtime crashes via `python -m uvicorn`.
- **MLOps Persistent State & Volume Mounts**:
  - Automatically identifies persistent storage folders (`artifacts/`, `models/`, `runs/`, `checkpoints/`, `data/`, `registry/`).
  - Configures container volume mounts in `compose.yaml` (e.g. `../artifacts:/app/artifacts`) so that trained models, checkpoints, logs, and experiment runs persist across container restarts.

### 16. Scalability & Observability Orchestration (Pass 8)
- **Container Scalability & Limits in Auto Docker**:
  - **Horizontal Replicas**: Generates `deploy.replicas: <COUNT>` in `compose.yaml` under `services.app`.
  - **Resource Limits**: Generates `deploy.resources.limits.cpus: "<CPUS>"` and `deploy.resources.limits.memory: "<MEM>"` in `compose.yaml`.
  - **CLI Flags**: Added `--replicas <INT>` (1-64), `--cpus <STR>` (e.g. `1.5`, `2`), and `--memory <STR>` (e.g. `512M`, `2G`) with strict validation.
  - **Interactive Workbench**: Option 6 includes `[9] Scale (Replicas)` and `[10] Resource Limits`; console commands `replicas <COUNT>` and `resources cpus=X memory=Y`.
  - **Auto-Pilot Wizard Integration**: Step 2 option 7 toggles/cycles replicas (1 -> 2 -> 3 -> 1); Step 3 includes scale and limit summary in preflight review.
- **First-Class Observability Orchestration (Prometheus & Grafana)**:
  - **Prometheus Service**: Orchestrates `prom/prometheus:v3.0.1` on localhost `127.0.0.1:9090` with persistent named volume `prometheus-data`.
  - **Automated Scrape Configuration**: Automatically generates `.opsforge/prometheus.yml` configured to scrape the application's `/metrics` endpoint (`http://app:<port>/metrics`) every 15 seconds.
  - **Grafana Service**: Orchestrates `grafana/grafana:11.4.0` on localhost `127.0.0.1:3000` with named volume `grafana-data`, disabled self-registration, and default admin credentials.
  - **Dependency Decoupling**: Application startup is not gated by observability services; Prometheus depends on `app` (for web workloads); Grafana depends on `prometheus`.
  - **Service Role Classification**: `classify_service_role` classifies Prometheus, Grafana, Datadog, OpenTelemetry, Jaeger, Tempo, and Loki as `'observability'`.
- **Enterprise AutoML Scalability & Observability**:
  - **Serving Scalability**: Added `--workers` (horizontal multi-process), `--backlog` (connection queue depth), and `--timeout-keep-alive` to `enterprise_automl_v3_2.py serve`.
  - **OpenMetrics / Prometheus Exposition**: Implemented thread-safe `MetricsCollector` in FastAPI serving app exposing `/metrics` in standard Prometheus text format (`text/plain; version=0.0.4; charset=utf-8`), tracking prediction counts, latency histograms (SLO buckets 5ms-5s), batch sizes, active requests, cache hits/misses, and loaded models.
  - **Structured JSON & Probes**: Exposes `/metrics/json` for monitoring dashboards, `/healthz` for liveness, and `/readyz` for readiness validation.

### 17. Release Blocker Remediation & Safe Discovery Alignment (Pass 9)
- **AutoML Multi-Worker Serving Architecture**:
  - Resolved Uvicorn multi-worker crash (`ValueError: You must pass the application as an import string to use 'workers'`) when `--workers > 1`.
  - Implemented `create_app(args=None)` application factory function.
  - Passes module import string `f"{Path(__file__).stem}:create_app"` with `factory=True` to `uvicorn.run()` when `workers > 1`, serializing configuration via `AUTOML_SERVE_CONFIG` environment variable.
  - Ensures clean worker process initialization across both Linux (fork/spawn) and Windows (spawn) multiprocessing backends.
- **Strict Host & Daemon Cleaner Guarding**:
  - In `auto_docker.py clean`, explicitly probes host Docker availability via `DockerHostProbe.probe()`.
  - If Docker CLI is missing or Docker Engine is unreachable, prints an informative error to stderr and exits with exit code 2, eliminating false "0 resources to clean" reports on hosts without Docker.
- **Port Range Multi-Replica Scaling**:
  - When `plan.replicas > 1` (e.g. 3) and `plan.port` is specified, Auto Docker generates valid Docker Compose port range bindings: `127.0.0.1:{host}-{host + replicas - 1}:{container}` (e.g. `127.0.0.1:8000-8002:8000`).
  - Resolves host port binding collision on `docker compose up`.
- **Authoritative Compose Project Slug Resolution**:
  - Implemented `ResourceCleaner.get_project_identifiers(project_path)` resolving folder name, lowercase slug, and exact generated Compose stack names (`opsforge-{slug}-{fingerprint}`).
  - Resolves previous cleaner blindness where project containers with `opsforge-...` project labels were missed during targeted project cleanups.
- **Elimination of Unsafe Heuristic Volume Splitting**:
  - Removed dangerous `vol_name.split("_", 1)[0]` fallback on unlabeled volumes.
  - Unlabeled host volumes (e.g. `postgres_data`) are never attributed to arbitrary user projects.
- **Orphan Isolation & Active Container Protection**:
  - Disentangled test container status: active running test containers (`status="running"`) are never classified as orphans (`is_orphan = is_test and not is_running`), protecting active test runs from orphan purge operations.
- **Cryptographically Strong Grafana Credentials**:
  - Replaced hardcoded default password `"admin"` in `compose.yaml` with cryptographically secure random token `secrets.token_urlsafe(16)` (or `GF_SECURITY_ADMIN_PASSWORD` env override), prominently documented in `.opsforge/INSTRUCTIONS.txt`.
- **Refined Prometheus Self-Scraping**:
  - Guaranteed `localhost:9090` self-scrape configuration in generated `prometheus.yml`.
  - Only scrapes application `/metrics` when workload is web-based and has a published port, eliminating continuous 404 scrape errors on batch scripts or non-web applications.
- **AutoML Authentication Metrics Leak Fix**:
  - Moved `require_auth` enforcement inside `try ... finally` block, guaranteeing `metrics.active_requests` counter decrements on 401 Unauthorized responses and accurately records `'unauthorized'` request outcomes.
- **Dependency Preflight in Auto-Pilot Wizard & Console**:
  - Expanded `IMPORT_PACKAGES` to include `joblib`, `scipy`, `optuna`, `lightgbm`, `seaborn`, `matplotlib`.
  - Auto-Pilot Wizard Step 2 provides Option `[8] Dependencies: STRICT / INFER` to toggle unpinned package installation.
  - Wizard Step 3 displays `Readiness Status: RESOLUTION REQUIRED` and blocks Option `[1] Deploy & Run Now` when unmanifested packages are detected in strict mode, offering clear resolution pathways.

---

## Backward Compatibility
- Maintains 100% backward compatibility with all previous CLI flags and interactive console commands.
- Preserves output folder convention `<PROJECT>/.opsforge/` for generated stacks.
- All 228 legacy baseline tests continue to pass (total 322 automated tests: 316 pass, 6 platform-skip on Windows; all 322 pass on Linux).
