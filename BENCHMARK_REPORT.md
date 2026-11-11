# Auto Docker RC11 — Public Acceptance Benchmark Report

> Generated automatically on 2026-11-11 15:01:51 UTC
> Version: `v3.0.0-rc10.1` | Platform: `win32` (Python `3.13.14`)

## 1. Executive Summary

The Auto Docker RC11 Acceptance Benchmark Suite evaluates real-world developer workloads across 6 critical operational vectors. Unlike simple Dockerfile templates, Auto Docker performs resource-aware, explainable containerization with proactive safety gates.

| Scenario | Description | Verdict | Latency (ms) | Exit Code | Operational Impact |
| :--- | :--- | :---: | :---: | :---: | :--- |
| `SCN-01` | Interactive Python CLI Workflow | **PASS** | 41.7 | `0` | Detected input()/stdin; generated interactive terminal container without detached hang risk. |
| `SCN-02` | FastAPI Production Microservice | **PASS** | 19.4 | `0` | Inferred ASGI framework; auto-injected uvicorn, exposed port 8000 with loopback health probe. |
| `SCN-03` | Enterprise AutoML / MLOps Workload | **PASS** | 1406.4 | `0` | Distinguished required dependencies from 4 heavy optional ML libraries; estimated minimal build at 0.5 GiB (avoided runaway layer crash). |
| `SCN-04` | Existing Multi-Service Compose Adoption | **PASS** | 11.5 | `0` | Adopted pre-existing 3-tier stack (nginx + postgres + redis) without modifying or overwriting developer files. |
| `SCN-05` | Broken Application Diagnosis & Recovery | **PASS** | 12.7 | `0` | Detected recoverable deployment state with actionable rollback suggestion. |
| `SCN-06` | Storage Deficit Preflight & Build Blocker | **PASS** | 68.3 | `1` | Halted build before invocation; preflight returned 1 and build halted with 2, preventing Docker daemon disk exhaustion. |

**Total Benchmark Suite Latency**: `1560.1 ms` across all 6 end-to-end scenarios.
**Overall Suite Status**: `100% PASS (6/6)`.

---

## 2. Detailed Scenario Analysis

### SCN-01: Interactive Python CLI Workflow
- **Result**: `PASS`
- **Execution Kind**: `interactive`
- **Storage Verdict**: `SUFFICIENT`
- **Duration**: `41.7 ms`
- **Required Packages**: `(none)`
- **Optional Packages**: `(none)`
- **Developer Impact**: Detected input()/stdin; generated interactive terminal container without detached hang risk.

```json
{
  "entrypoint": "calculator_cli.py",
  "command": "python calculator_cli.py",
  "tty_configured": "True"
}
```

### SCN-02: FastAPI Production Microservice
- **Result**: `PASS`
- **Execution Kind**: `web`
- **Storage Verdict**: `SUFFICIENT`
- **Duration**: `19.4 ms`
- **Required Packages**: `fastapi, uvicorn`
- **Optional Packages**: `(none)`
- **Developer Impact**: Inferred ASGI framework; auto-injected uvicorn, exposed port 8000 with loopback health probe.

```json
{
  "entrypoint": "server.py",
  "framework": "fastapi",
  "command": "python -m uvicorn server:app --app-dir . --host 0.0.0.0 --port 8000",
  "healthcheck_present": "True"
}
```

### SCN-03: Enterprise AutoML / MLOps Workload
- **Result**: `PASS`
- **Execution Kind**: `interactive`
- **Storage Verdict**: `SUFFICIENT`
- **Duration**: `1406.4 ms`
- **Required Packages**: `__future__, argparse, collections, csv, datetime, fastapi, hashlib, hmac, http, importlib, io, joblib, json, math, numpy, os, pandas, pathlib, re, scipy, shutil, signal, sklearn, sqlite3, subprocess, sys, tempfile, threading, time, traceback, urllib, uuid, warnings`
- **Optional Packages**: `catboost, kafka, lightgbm, mlflow, psutil, serve_api, starlette, statsmodels, uvicorn, xgboost`
- **Developer Impact**: Distinguished required dependencies from 4 heavy optional ML libraries; estimated minimal build at 0.5 GiB (avoided runaway layer crash).

```json
{
  "real_workload_file": "True",
  "required_count": "33",
  "optional_count": "10",
  "min_build_gib": "0.52",
  "max_build_gib": "1.02"
}
```

### SCN-04: Existing Multi-Service Compose Adoption
- **Result**: `PASS`
- **Execution Kind**: `multi-service`
- **Storage Verdict**: `SUFFICIENT`
- **Duration**: `11.5 ms`
- **Required Packages**: `(none)`
- **Optional Packages**: `(none)`
- **Developer Impact**: Adopted pre-existing 3-tier stack (nginx + postgres + redis) without modifying or overwriting developer files.

```json
{
  "compose_preserved": "True",
  "services_detected": "web, postgres, redis",
  "inspection_output_chars": "737"
}
```

### SCN-05: Broken Application Diagnosis & Recovery
- **Result**: `PASS`
- **Execution Kind**: `recovery`
- **Storage Verdict**: `SUFFICIENT`
- **Duration**: `12.7 ms`
- **Required Packages**: `(none)`
- **Optional Packages**: `(none)`
- **Developer Impact**: Detected recoverable deployment state with actionable rollback suggestion.

```json
{
  "diagnose_findings": "Recoverable backup found: .opsforge-backup-20261110T120000Z"
}
```

### SCN-06: Storage Deficit Preflight & Build Blocker
- **Result**: `PASS`
- **Execution Kind**: `preflight-gate`
- **Storage Verdict**: `BLOCKED`
- **Duration**: `68.3 ms`
- **Required Packages**: `torch`
- **Optional Packages**: `(none)`
- **Developer Impact**: Halted build before invocation; preflight returned 1 and build halted with 2, preventing Docker daemon disk exhaustion.

```json
{
  "preflight_exit_code": "1",
  "build_exit_code": "2",
  "docker_run_called": "False"
}
```

---

## 3. Comparison: Auto Docker vs. Standard Tooling

| Capability | Plain `docker init` | Cloud Native Buildpacks | Auto Docker 3.0 |
| :--- | :---: | :---: | :---: |
| **Standard Library Only (Zero Dependencies)** | N/A (Go binary) | N/A (CLI tools) | **Yes (Pure Python standard library)** |
| **Interactive CLI Terminal Attachment** | No (Generates detached web) | No (Assumes server) | **Yes (Detects stdin/input and configures TTY)** |
| **Storage Deficit Preflight Blocker** | No | No (Runs until disk fills) | **Yes (Halts with code 2 before Docker invoke)** |
| **Dependency Ast Intelligence (Guarded vs Required)** | No (Naive requirements.txt) | No | **Yes (Distinguishes try/except & feature flags)** |
| **Non-Destructive Existing Compose Adoption** | Warns / clobbers | Replaces build | **Yes (Preserves and orchestrates existing files)** |
| **Automated Crash Diagnosis & Safe Rollback** | None | None | **Yes (Dedicated recovery coordinator & backup)** |
