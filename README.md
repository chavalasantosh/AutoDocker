# Auto Docker 3.0

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-brightgreen.svg)](pyproject.toml)
[![Release](https://img.shields.io/badge/Release-3.0.0--rc8-orange.svg)](RELEASE_NOTES.md)
[![Dependencies](https://img.shields.io/badge/Dependencies-Zero%20(Stdlib%20Only)-success.svg)](requirements.txt)
[![Tests](https://img.shields.io/badge/Tests-339%20Passed%20(100%25)-brightgreen.svg)](TEST_REPORT.md)

> **Autonomous, zero-dependency container infrastructure generator, resource cleaner, and deployment orchestrator in a single Python executable.**

**Auto Docker** transforms any Python, ML, GenAI, or multi-service codebase into hardened, reproducible Docker and Docker Compose environments in seconds. It requires **zero third-party pip dependencies**, never touches or overwrites your source code, enforces strict loopback network security, and provides an intelligent **SMART Mode** alongside an interactive terminal workbench and 3-step Auto-Pilot wizard.

---

## Highlights

- **Single Master Executable (`auto_docker.py`)**: Zero external pip packages needed at runtime — runs out of the box via `python auto_docker.py`.
- **SMART Mode (`auto_docker.py smart`)**: One-command intelligent deployment combining project detection, dependency intelligence, preflight verification, deployment planning, and truthful runtime health checks with zero Docker jargon.
- **Auto-Pilot Quick-Start Wizard**: 3-step guided deployment (`Detect -> Configure -> Safe Deploy`) for zero-friction containerization.
- **Dependency Intelligence 2.0**:
  - **Standard Library Immunity**: 300+ built-in modules (`json`, `sqlite3`, `pathlib`, `logging`, `dataclasses`, `asyncio`, etc.) are never misclassified as missing pip dependencies.
  - **Curated PyPI Alias Resolution**: 120+ top-level imports mapped to canonical package distributions (`cv2` $\to$ `opencv-python`, `PIL` $\to$ `Pillow`, `yaml` $\to$ `PyYAML`, `dotenv` $\to$ `python-dotenv`, `bs4` $\to$ `beautifulsoup4`, `psycopg2` $\to$ `psycopg2-binary`, `fitz` $\to$ `PyMuPDF`, etc.).
  - **Multi-Manifest Discovery**: Statically discovers and parses `requirements.txt`, `pyproject.toml`, `setup.cfg`, `setup.py`, `Pipfile`, and Conda `environment.yml`.
- **Dedicated Docker Resource Cleaner**:
  - Discovery and ownership tagging across containers, images, networks, and volumes.
  - 3 Scoped Modes: `TEMPORARY_TEST`, `PROJECT`, and `ORPHAN` recovery.
  - Zero `docker system prune`: strictly protects unrelated containers and preserves named volumes by default.
- **Enterprise Scalability & Observability**:
  - Horizontal scaling (`deploy.replicas`) with automated non-colliding host port ranges (`127.0.0.1:8000-8002:8000`).
  - CPU and memory resource limits (`deploy.resources.limits`).
  - Automated Prometheus (`v3.0.1`) and Grafana (`11.4.0`) auxiliary service generation with automated `/metrics` scraping and cryptographically strong credentials.
- **MLOps Persistent State Mounts**:
  - Automatic detection and volume mounting for persistent directories (`artifacts/`, `models/`, `runs/`, `checkpoints/`, `data/`, `datasets/`, `storage/`, `outputs/`, `logs/`, `weights/`, `db/`).
- **Strict Security Gate & Redaction**:
  - Default loopback binding (`127.0.0.1`) prevents accidental public network exposure.
  - Non-root runtime user (`app:app`).
  - Cryptographically bound `DeploymentDigest` and session-local HMAC credential redaction.
  - Critical risk detection (privileged containers, Docker socket mounts, 0.0.0.0 database ports) requiring explicit `--allow-insecure` authorization.

---

## Quickstart

### 1. SMART Mode (Recommended for Beginners)
Launch intelligent one-command deployment on any project folder:

```bash
python auto_docker.py smart
# or specify the project directly
python auto_docker.py smart /path/to/project
```

SMART Mode automatically:
1. **Understands your project**: Detects framework, entrypoint, dependencies, ports, and storage with clear `CONFIRMED` / `INFERRED` / `BLOCKED` evidence.
2. **Prepares deployment safely**: Generates `.opsforge/Dockerfile` and `compose.yaml` (or reuses existing Compose infrastructure without overwriting).
3. **One Approval**: Stages immutable deployment review and requires explicit confirmation.
4. **Verifies Runtime**: Distinguishes verified healthy services from unverified containers without healthchecks.

### 2. Interactive Terminal Workbench
Run with no arguments to launch the full developer workbench:

```bash
python auto_docker.py
```

### 3. Auto-Pilot 3-Step Wizard
Launch the guided Quick-Start Wizard directly on any project:

```bash
python auto_docker.py wizard
# or
python auto_docker.py quick /path/to/project
```

- **Step 1: Detect** — Scans project structure, identifies entrypoint, detects framework (FastAPI, Flask, Streamlit, Gradio, Python CLI/Batch), and discovers persistent directories.
- **Step 2: Configure** — Quick toggles for workload profile, port, hardware acceleration (GPU), replicas, resource limits, dependencies (`STRICT` / `INFER`), and custom execution command override.
- **Step 3: Safe Deploy** — Preflight safety review, unmanifested dependency checks, and staged execution (`Deploy Now`, `Dry-Run`, or `Generate Only`).

### 3. Non-Interactive CLI Automation

```bash
# Inspect project and print deployment plan
python auto_docker.py inspect /path/to/project

# Generate hardened container infrastructure (.opsforge/)
python auto_docker.py generate /path/to/project

# Validate generated configuration offline
python auto_docker.py check /path/to/project

# Stage deployment with preflight review
python auto_docker.py preflight /path/to/project

# Build and start services (requires explicit --yes in scripts)
python auto_docker.py build /path/to/project --yes
python auto_docker.py up /path/to/project --yes

# Verify container health and TCP/HTTP readiness
python auto_docker.py verify /path/to/project

# Clean temporary test resources safely
python auto_docker.py clean --clean-test --yes
```

---

## File Generation Contract

Auto Docker follows a strict **non-destructive generation contract**:

```text
my_project/
├── main.py
├── requirements.txt
└── .opsforge/                          <-- All generated files live exclusively here!
    ├── Dockerfile                      <-- Multi-stage, non-root builder & runtime
    ├── compose.yaml                    <-- Hardened service definitions & volumes
    ├── Dockerfile.dockerignore         <-- Build context exclusions
    ├── manifest.json                   <-- Cryptographic hashes and runtime metadata
    └── INSTRUCTIONS.txt                <-- Human-readable verification instructions
```

- **Zero Root Overwrites**: Auto Docker **never** overwrites root `Dockerfile` or `compose.yaml` files.
- **Automatic Backups**: Whenever files are re-generated, previous configurations are safely archived into `.opsforge-backup-<timestamp>/` with one-click rollback support (`recover`).
- **Existing Stack Preservation**: When existing Docker Compose infrastructure is detected, Auto Docker acts as a non-destructive validator and deployment orchestrator, preserving your files byte-for-byte.

---

## Workload Profiles

Configure workloads to automatically recommend auxiliary microservices:

| Mode | Target Use Case | Auxiliary Services Included |
|:---|:---|:---|
| `auto` | Automatic heuristic detection based on imports | Inferred from code imports |
| `base` | Standard web services & batch scripts | Core application only |
| `mlops` | Machine learning training & model serving | `mlflow` (tracking server) |
| `llmops` | Large Language Model serving & workflows | `ollama` (GPU/CPU inference) |
| `genaiops` | Full GenAI stack with caching & metrics | `ollama`, `redis`, `prometheus`, `grafana` |

---

## Dedicated Docker Resource Cleaner

Auto Docker includes a purpose-built resource cleaner that prevents resource accumulation:

```bash
# Safe dry-run scan (read-only discovery)
python auto_docker.py clean

# Remove disposable test containers, images, and networks
python auto_docker.py clean --clean-test --yes

# Remove all resources belonging to a specific project
python auto_docker.py clean --project /path/to/project --yes

# Detect and clean orphaned containers from crashed runs
python auto_docker.py clean --orphans --yes
```

### Safety Guarantees
- **No System Prune**: Never executes unrestricted `docker system prune` or `docker network prune`.
- **Volume Protection**: Named volumes are preserved by default (`--preserve-volumes`), preventing data loss.
- **Ownership Tagging**: Distinguishes project-owned, test-tagged, and unmanaged host containers.

---

## Quality Release Gates

Auto Docker is developed under strict release criteria:

| Gate | Verification Area | Status | Evidence |
|:---:|:---|:---:|:---|
| **G1** | Baseline Regression Suite | **PASS** | 228 legacy baseline tests verified |
| **G2** | Large-Project Discovery | **PASS** | 1,050+ Python files traversed cleanly without scan limits |
| **G3** | Environment & Secrets Readiness | **PASS** | Multi-file Compose validation, precedence, session HMAC redaction |
| **G4** | Existing Infrastructure Protection | **PASS** | Existing Compose files preserved 100% byte-for-byte |
| **G5** | Deterministic Deployment Planning | **PASS** | Graph-based topological sort with reproducible JSON export |
| **G6** | Security Gate & Risk Enforcement | **PASS** | Privileged containers, socket mounts, and public DB ports blocked |
| **G7** | Host Docker Engine Status | **HONEST PROBE** | Probed via `DockerHostProbe` (`BLOCKED` when Docker CLI is absent) |
| **G8** | Application Readiness Verification | **PASS** | Offline service role and dependency graph validated across 330 tests |
| **G9** | Failure Recovery & Resource Cleanup | **PASS** | State machine lifecycle and scoped `ResourceCleaner` verified |
| **G10** | Single-File Executable Distribution | **PASS** | `auto_docker.py` remains 100% self-contained standard library |
| **G11** | Guided Quick-Start Wizard & MLOps Persistence | **PASS** | 3-step wizard and persistent volume mounts for models/data |
| **G12** | Scalability & Observability Orchestration | **PASS** | Multi-replica port ranges, limits, Prometheus & Grafana services |
| **G13** | Release Blocker Remediation | **PASS** | Non-zero cleaner exit, slug discovery, active container protection |
| **G14** | Dependency Intelligence 2.0 | **PASS** | Stdlib immunity, 120+ PyPI aliases, multi-manifest discovery |

---

## Running Automated Tests

Auto Docker includes a comprehensive self-test suite built into the executable:

```bash
python auto_docker.py self-test
```

All **330 automated tests** execute in under 15 seconds with zero external dependencies.

---

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
Copyright 2026 Chavala Santosh.
