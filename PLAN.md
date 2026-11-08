# Auto Docker 3.0 RC7 — Development Master Plan

## 1. Baseline Audit & Review Status

- **Starting Application Version**: `3.0.0-rc6` -> Upgraded to `3.0.0-rc7`
- **File Architecture**: Single-file Python executable `auto_docker.py` (11,800+ lines, ~610 KB).
- **Python Syntax Status**: Valid Python 3.10+ (compiled with `python -m py_compile auto_docker.py`, AST parsed clean).
- **Automated Test Results**:
  - Baseline RC5: 228 tests run, 222 passed, 6 platform-skipped on Windows.
  - RC6 Milestone: 240 tests run, 234 passed, 6 platform-skipped.
  - RC6 Correction Pass 1: 250 tests run, 244 passed, 6 platform-skipped.
  - RC6 Correction Pass 2: 261 tests run, 255 passed, 6 platform-skipped.
  - RC6 Final Stabilization (Pass 3): 278 tests run, 272 passed, 6 platform-skipped.
  - RC6 Live Acceptance Test Hardening (Pass 4): 287 tests run, 281 passed, 6 platform-skipped.
  - RC6 Final Live-Test Safety Corrections (Pass 5): 297 tests run, 291 passed, 6 platform-skipped.
  - RC6 Dedicated Resource Cleaner & P0/P1 Hardening (Pass 6): 307 tests run, 301 passed, 6 platform-skipped.
  - RC6 Auto-Pilot Guided Quick-Start Wizard & MLOps Persistence (Pass 7): 312 tests run, 306 passed, 6 platform-skipped on Windows, 0 failed, 0 errors.
  - RC6 Scalability & Observability Orchestration (Pass 8): 315 tests run, 309 passed, 6 platform-skipped on Windows, 0 failed, 0 errors.
  - RC6 Release Blocker Remediation (Pass 9): 322 tests run, 316 passed, 6 platform-skipped on Windows, 0 failed, 0 errors (all 322 pass on Linux).
  - RC7 Reliability & Usability Stabilization (Pass 10): 330 tests run, 324 passed, 6 platform-skipped on Windows, 0 failed, 0 errors (all 330 pass on Linux).
- **Docker Environment Check**:
  - `docker` executable is not installed or available on the current Windows host PATH.
  - Accurately probed via `DockerHostProbe` (`CLI missing`).
  - Offline inspections, plan generation, security audits, and state transitions work without Docker. Docker builds/startups are honestly labeled as `BLOCKED` on this host.
- **Existing CLI Commands & Behaviors**:
  - Actions supported: `console`, `wizard`, `quick`, `autopilot`, `doctor`, `existing`, `native`, `readiness`, `preflight`, `inspect`, `preview`, `generate`, `check`, `ports`, `verify`, `diagnose`, `audit`, `dryrun`, `build`, `up`, `run`, `job`, `down`, `ps`, `logs`, `recover`, `unlock`, `lock`, `contexts`, `self-test`, `integration-test`, `ai-integration-test`, `live-test`, `smoke`, `cleaner`, `clean`.
  - Added in RC6: `native plan` action (console & CLI), `--json` export support, `--allow-insecure` security gate override flag.
  - Added in Usability Pass: Interactive Project Browser (Option 2) with pagination and parent folder navigation; Persistent Recent Projects (Option 3); Guided Settings Customizer (Option 6); Safe Preflight Review Staging (Option 10); `DockerHostProbe` diagnostics.
  - Added in Final Stabilization & Hardening (Passes 3-5):
    - Priority 1: Multiline paste rejection (`10\nYES` in single turn executes 0 Docker calls; requires distinct turn).
    - Priority 2: Cryptographically bound, content-aware `DeploymentDigest` (command, entrypoint, overrides, session-local HMAC for `.env`).
    - Priority 3: Separation of static validation from container runtime state (`last_validation` tracking, preserving `VERIFYING`/`HEALTHY` during `ready` and `check`, syntax error preservation).
    - Priority 4: Compose & Dockerfile file browser selection in `target_for` without treating as Python entrypoint.
    - Priority 5: `--native-action verify` CLI choice with aligned exit codes (0/1/2).
    - Priority 6: Beginner 10-step journey acceptance test without hidden commands.
    - Priority 7: `live-test` command with safety checks, disk verification, loopback-only port binding (`127.0.0.1:{port}:8080`), and 7-phase lifecycle execution.
  - Added in Pass 6 (Dedicated Resource Cleaner & P0/P1 Resolution):
    - Dedicated `ResourceCleaner`: Read-only discovery and ownership tagging (`CleanableResource`) for containers, images, networks, and volumes.
    - 3 Cleanup Modes: `TEMPORARY_TEST` (disposable test resources), `PROJECT` (project-owned resources), and `ORPHAN` (leftovers from crashed runs).
    - Volume Preservation: Named volumes protected by default (`preserve_volumes=True`); data loss prevented.
    - Zero System Prune: Strict safety prevents executing unrestricted `docker system prune` or deleting untagged/shared host resources.
    - Container PID 1 Signal Handling: Explicit `SIGTERM`/`SIGINT` handlers in test `server.py` prevent 10s shutdown timeout hangs.
    - Linux Remote Context Resolution: Accurately differentiates nonexistent/remote contexts from daemon inspection errors.
    - Console & CLI Access: Interactive Workbench Option 12 and CLI action `cleaner`/`clean`.
  - Added in Pass 7 (Auto-Pilot Guided Quick-Start Wizard & MLOps Persistence):
    - Auto-Pilot Wizard: Option `[A]` in Workbench menu and direct CLI invocation (`auto_docker.py wizard`, `quick`, `autopilot`) providing a guided 3-step deployment walk-through (`Detect -> Configure -> Safe Deploy`).
    - Top-Level AST Scope Analysis: Differentiates module-level web applications (`app = FastAPI()`) from nested CLI functions containing local web services (`enterprise_automl_v3_2.py`), preventing false FastAPI classification and uvicorn crashes on CLI projects.
    - MLOps Persistent State & Volume Mounts: Automatic detection of persistent directories (`artifacts/`, `models/`, `runs/`, `checkpoints/`, `data/`, `registry/`) and automatic host-to-container volume bindings (`../artifacts:/app/artifacts`).
- **Existing File Generation Contract**:
  - Generates exclusively in `<PROJECT_ROOT>/.opsforge/`.
  - Artifacts: `Dockerfile`, `compose.yaml`, `.dockerignore`, `manifest.json`.
  - Backup: `.opsforge-backup-<timestamp>` created on update.
  - Safety: Never overwrites existing root `Dockerfile` or `compose.yaml`.

---

## 2. Milestone Implementation & Correction Verification Status

| Milestone / Stage | Title | Implementation Highlights | Status |
|:---|:---|:---|:---:|
| **RC6-M1** | Intelligent Project Discovery | Bounded traversal, circular symlink immunity, bypasses 240-file limit when existing Compose infrastructure exists; artifact scoring; nested path root suggestions without silent redirection. | **COMPLETED** |
| **RC6-M2** | Existing Infrastructure Analysis | Static Compose service parser; multi-file Compose ordering (`compose.yaml` + `compose.override.yaml`); automatic service role classification; preserves existing infrastructure 100% byte-for-byte. | **COMPLETED** |
| **RC6-M3 / Stage 1A** | Environment & Secrets Validation | Multi-file Compose variable inspection evaluates all selected Compose files uncoupled from service counts; respects Docker Compose variable precedence (`os.environ` > `.env` > default `${VAR:-default}`); handles `${VAR:?}` vs `${VAR?}`; never displays secret values. | **COMPLETED** |
| **RC6-M4** | Deployment Planning Engine | Immutable typed `DeploymentPlan` model; graph-based dependency analysis; topological phase ordering (4 phases); `native plan` command in Console & CLI; deterministic `--json` export. | **COMPLETED** |
| **RC6-M5 / Stage 1C** | Host & Security Gate Enforcement | Surfaces critical security flags for privileged containers, Docker socket mounts, and public 0.0.0.0 database ports; enforces strict security gate blocking mutating operations unless `--allow-insecure` is explicitly authorized; override expires on project switch. | **COMPLETED** |
| **RC6-M6 / Priority 1** | Safe Deployment Lifecycle & Transitions | `DeploymentOrchestrator` corrected for sequential lifecycle scenarios; runtime state preserved during `ready` and `check`; Compose syntax errors preserved without masking. | **COMPLETED** |
| **Priority 1 (Pass 3)** | Multiline Approval Bypass Prevention | Single turn delivering `10\nYES` executes ZERO mutating Docker commands; requires a distinct user input turn after the review screen is presented; single central reader preserved; one-use tokens invalidated by cancellation or project switch. | **COMPLETED** |
| **Priority 2 (Pass 3)** | Comprehensive Content-Aware Fingerprint | `DeploymentDigest` computes SHA-256 over selected Compose files, overrides, Dockerfiles, command/entrypoint settings, and session-local HMAC-SHA256 (`_SESSION_HMAC_KEY`) of `.env` files. Changes to `command:` or `.env` invalidate staged approval. | **COMPLETED** |
| **Priority 3 (Pass 3)** | Validation vs. Container Runtime Separation | `last_validation` property introduced; running `native ready` or `native check` on running containers preserves runtime state (`VERIFYING`, `HEALTHY`, `DEGRADED`); failed check preserves primary syntax error directly. | **COMPLETED** |
| **Priority 4 (Pass 3)** | Compose & Dockerfile Browser Selection | `target_for` recognizes Compose files and Dockerfiles, selecting the parent folder as project root with `pinned=None`; displays existing-infrastructure analysis and plan; explains unrelated extensions (`.txt`, etc.). | **COMPLETED** |
| **Priority 5 (Pass 3)** | CLI `--native-action verify` Alignment | Added `verify` to `--native-action` choices in `main()`; returns exit code 0 (healthy / unknown running), 1 (degraded / unhealthy), 2 (unavailable / error). | **COMPLETED** |
| **Priority 6 (Pass 3)** | Real Beginner Guided Workflow Acceptance Test | Acceptance test covering the 10-step beginner journey without hidden commands; menu banner clearly displays cwd as default suggestion `(Current terminal folder; not yet selected as project)`. | **COMPLETED** |
| **Priority 7 (Pass 3)** | Live Docker Acceptance Test Procedure | Implemented `LiveAcceptanceTest.run()` with `live-test` CLI command; checks host connectivity, disk space (>= 2.0 GiB), labels resources (`com.auto_docker.test`), and runs full 7-step lifecycle (Build -> Start -> Verify -> Stop -> Restart -> Verify -> Cleanup). | **COMPLETED** |
| **Pass 4 (Live-Test Hardening)** | Syntactic & Semantic Live Test Overhaul | Replaced broken inline Python command with clean standalone `server.py`; fixed YAML plain scalar colon issues; added real `Dockerfile` with custom image build (`autodocker-live-test:{run_id}`); project-scoped cleanup with verification; explicit download & disk disclosure (~20 MB download / ~60 MB disk); remote context rejection; and top-level Python 3.10+ compatibility guard. | **COMPLETED** |
| **Pass 5 (Safety Corrections)** | Network Loopback, Authoritative Cleanup & Endpoint Verification | Restricted Compose mapping strictly to `127.0.0.1:{port}:8080`; made cleanup verification authoritative (exit code inspection, stopped container detection via `ps -a -q`, authoritative image absence verification, and inspection failure tracking); and verified effective Docker endpoint before mutations, detecting and rejecting implicit remote contexts (`docker context show` / `inspect`) and remote `DOCKER_HOST`. | **COMPLETED** |
| **Pass 6 (P0/P1 Hardening & Resource Cleaner)** | Dedicated Resource Cleaner & Lifecycle Fixes | Built dedicated `ResourceCleaner` supporting automatic test cleanup, project cleanup, and orphan recovery; volume protection by default; strict protection of unrelated containers/images; container PID 1 SIGTERM/SIGINT signal handling; and Linux remote-context inspection error classification. | **COMPLETED** |
| **Pass 7 (Auto-Pilot Wizard & MLOps Persistence)** | Guided 3-Step Wizard & MLOps Engine Optimization | Added interactive 3-step Auto-Pilot Wizard (`Detect -> Configure -> Safe Deploy`); top-level AST scope verification to prevent false web-service classifications for CLI tools like `enterprise_automl_v3_2.py`; automatic discovery and persistent volume mounts for `artifacts/`, `models/`, and checkpoint directories. | **COMPLETED** |
| **Pass 8 (Scalability & Observability)** | Horizontal Scaling, Resource Limits & Metrics Monitoring | Horizontal container scaling (`deploy.replicas`) and CPU/memory resource limits (`deploy.resources.limits`) in `compose.yaml`; first-class Prometheus (`prom/prometheus:v3.0.1`) and Grafana (`grafana/grafana:11.4.0`) orchestration with automated `prometheus.yml` scrape configuration targeting `app:<port>/metrics`; CLI flags (`--replicas`, `--cpus`, `--memory`, `--services`), console commands (`replicas`, `resources`), and Auto-Pilot Wizard Step 2 option 7 integration; coupled with Enterprise AutoML serving concurrency (`--workers`, `--backlog`, `--timeout-keep-alive`) and Prometheus `/metrics` exposition. | **COMPLETED** |
| **Pass 9 (Release Blocker Remediation)** | Critical Safety, Multi-Worker Serving & Discovery Alignment | Fixed AutoML multi-worker serving (`uvicorn.run` module factory with `factory=True`); guarded cleaner CLI against false zero-resource exit 0 when Docker is missing (returns exit code 2); implemented Compose port ranges (`127.0.0.1:8000-8002:8000`) for `replicas > 1`; added `get_project_identifiers` to match generated `opsforge-{slug}-{fingerprint}` stacks; eliminated unsafe volume `_` heuristic splitting; protected running test containers from orphan classification; randomized Grafana passwords; ensured Prometheus self-scraping (`localhost:9090`) and avoided non-web application scrape 404s; resolved AutoML auth metrics counter leak; expanded `IMPORT_PACKAGES` (joblib, scipy, optuna, lightgbm, seaborn, matplotlib); and enforced Wizard Step 3 blocking with resolution options for unmanifested dependencies. | **COMPLETED** |
| **Pass 10 (RC7 Stabilization)** | Dependency Intelligence 2.0 & Deployment Reliability Engine | Standard library immunity (`STDLIB_MODULES` with 300+ modules) prevents misclassifying built-in modules as missing; expanded `IMPORT_PACKAGES` to 120+ curated distributions including top-level import aliases (`cv2` -> `opencv-python`, `PIL` -> `Pillow`, `yaml` -> `PyYAML`, `dotenv` -> `python-dotenv`, `bs4` -> `beautifulsoup4`, `psycopg2` -> `psycopg2-binary`, `fitz` -> `PyMuPDF`, `skimage` -> `scikit-image`, `jwt` -> `PyJWT`, `docx` -> `python-docx`, `dateutil` -> `python-dateutil`, etc.); multi-manifest discovery and parsing (`requirements.txt`, `pyproject.toml`, `setup.cfg`, `setup.py`, `Pipfile`, `environment.yml`); multi-manifest Dockerfile generation; deployment verification reliability differentiating healthy healthchecks from unverified running containers; Auto-Pilot Wizard Step 2 Option [9] for custom execution commands; and expanded persistent storage auto-detection (`datasets`, `storage`, `outputs`, `output`, `logs`, `weights`, `db`, `database`). | **COMPLETED** |

---

## 3. Quality Release Gates Summary

| Gate | Requirement | Status | Evidence / Notes |
|:---:|:---|:---:|:---|
| **G1** | Existing regression suite passes | **PASS** | 228 baseline tests verified (222 passed, 6 platform-skipped on Windows) |
| **G2** | Large-project discovery passes | **PASS** | 1,050+ Python files traversed cleanly; NVIDIA Auto Ontology verified |
| **G3** | Environment readiness checks pass | **PASS** | Evaluates all selected Compose files; respects precedence; session HMAC for secrets |
| **G4** | Existing infrastructure remains untouched | **PASS** | Zero file mutations on user repositories; existing Compose files byte-identical |
| **G5** | Deployment plan is deterministic | **PASS** | `DeploymentPlan` generated with topological sort and repeatable JSON export |
| **G6** | Security and resource checks produce correct classifications | **PASS** | Privileged containers, socket mounts, and 0.0.0.0 DB ports correctly rated `CRITICAL`; security gate enforced; override resets on switch |
| **G7** | Real Docker builds and startup pass on a supported host | **BLOCKED** | Docker Engine is not installed on the test host environment |
| **G8** | Application-specific readiness tests pass | **OFFLINE_VERIFIED / RUNTIME_BLOCKED** | Service role classification and dependency graph verified offline (330 tests); live runtime execution blocked by host Docker absence |
| **G9** | Failure recovery and cleanup are verified | **OFFLINE_VERIFIED / RUNTIME_BLOCKED** | State machine lifecycle and `ResourceCleaner` verified across all sequential scenarios; live container rollback blocked by host Docker absence |
| **G10** | Single-file distribution is reproducible | **PASS** | `auto_docker.py` remains 100% self-contained in a single executable script; zero nested input calls |
| **G11** | Guided Quick-Start Wizard & MLOps State Persistence | **PASS** | 3-step interactive Auto-Pilot wizard (`Detect -> Configure -> Safe Deploy`), MLOps volume mounting for `artifacts/` and `models/`, accurate AST scope check |
| **G12** | Scalability and Observability Orchestration | **PASS** | Container replicas & resource limits in Compose; Prometheus and Grafana service generation with automated scraping; Enterprise AutoML workers and OpenMetrics `/metrics` endpoint |
| **G13** | Release Blocker Remediation & Safe Discovery Alignment | **PASS** | Multi-worker Uvicorn serving; non-zero cleaner exit when Docker is missing; Compose host port ranges; stack slug discovery; active test container protection |
| **G14** | Dependency Intelligence 2.0 & Deployment Reliability Engine | **PASS** | Standard library immunity; 120+ package and alias distribution mappings; multi-manifest discovery (setup.cfg, setup.py, Pipfile, environment.yml); unverified container differentiation; Wizard custom execution command Option [9]; expanded persistent storage |
