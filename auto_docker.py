#!/usr/bin/env python3
"""Auto Docker 3.0 RC6 — single-file, standard-library Docker infrastructure generator.

Run:      python auto_docker.py
Automate: python auto_docker.py generate '/path/to/app.py' --mode llmops --yes
Test:     python auto_docker.py self-test

Interactive commands: help, project PATH, existing, native ACTION, mode NAME, kind auto|web|batch|interactive, services NAME,...,
    entry FILE, port N|auto, gpu on|off, doctor, inspect, preview [full],
    generate, check, build, up, down,
    ps, logs, verify, ports, hostport, status, lock, recover, unlock, back, quit.

Output stays in PROJECT/.opsforge/ for backward compatibility and never overwrites PROJECT/Dockerfile.
Docker and network access are not required to inspect/preview/generate.
"""
from __future__ import annotations

import argparse
import ast
import concurrent.futures
from contextlib import contextmanager
import dataclasses
import datetime as dt
import hashlib
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import stat
import socket
import subprocess
import sys
import tempfile
from collections import deque
import time
import urllib.request
import urllib.error
import select
import platform
from enum import Enum
from typing import Any, Iterator

VERSION = "3.0.0-rc7"
LOG = logging.getLogger("auto_docker")
OUTPUT = ".opsforge"
MAX_VISITED = 3000
MAX_DEPTH = 5
MAX_PYTHON = 240
MAX_PY_SIZE = 512 * 1024
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".nox", "site-packages", "dist", "build", ".next",
    ".idea", ".vscode", ".auto-docker", OUTPUT,
})
SKIP_FILES = frozenset({"setup.py", "conftest.py", "opsforge.py", "auto_docker.py"})
MODES = ("auto", "base", "mlops", "llmops", "genaiops")
SERVICE_ORDER = ("mlflow", "ollama", "redis", "prometheus", "grafana")
EXECUTION_KINDS = ("auto", "web", "batch", "interactive")
MIN_PYTHON_VERSION = (3, 10)


def check_python_compatibility(version_info: tuple[int, ...] | None = None) -> tuple[bool, str]:
    ver = version_info if version_info is not None else sys.version_info[:3]
    if ver < MIN_PYTHON_VERSION:
        ver_str = ".".join(str(x) for x in ver)
        msg = (
            f"ERROR: Auto Docker requires Python 3.10+ (detected Python {ver_str}).\n"
            "Your system may have a compatible Python interpreter already installed.\n\n"
            "How to run with an existing compatible interpreter:\n"
            "  1. Discover installed interpreters:  which python3.10 python3.11 python3.12 python3.13\n"
            "  2. Or invoke directly:              python3.11 auto_docker.py <command>\n"
            "  3. Or use an active virtualenv:      source /path/to/py311_venv/bin/activate && python auto_docker.py"
        )
        return False, msg
    ver_str = ".".join(str(x) for x in ver)
    return True, f"Python {ver_str} is compatible (>= 3.10 required)."


class JsonLogFormatter(logging.Formatter):
    """Structured logs without emitting source text or environment contents."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "time": dt.datetime.fromtimestamp(record.created, dt.timezone.utc).isoformat(),
            "severity": record.levelname,
            "component": record.name,
            "event": record.getMessage(),
        }, ensure_ascii=False)


class OpsError(Exception):
    """User-facing, recoverable application error."""


class ScanLimit(OpsError):
    pass


class AmbiguousEntry(OpsError):
    pass


class Mode(str, Enum):
    AUTO = "auto"
    BASE = "base"
    MLOPS = "mlops"
    LLMOPS = "llmops"
    GENAI = "genaiops"


@dataclasses.dataclass(frozen=True)
class Target:
    root: Path
    pinned: str | None = None


@dataclasses.dataclass(frozen=True)
class Candidate:
    path: str
    score: int
    imports: frozenset[str]
    main_guard: bool
    framework: str
    app_object: bool
    needs_stdin: bool = False


@dataclasses.dataclass(frozen=True)
class Inspection:
    root: Path
    entry: Candidate
    dependency: str | None
    detected_mode: Mode
    candidates: tuple[Candidate, ...]
    python_count: int
    warnings: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Settings:
    target: Target
    mode: Mode = Mode.AUTO
    entry: str | None = None
    port: int | None = None  # None means inferred from framework
    gpu: bool = False
    services_override: tuple[str, ...] | None = None
    host_ports: tuple[tuple[str, int], ...] = ()
    dependency_mode: str = "strict"  # strict (default) or explicitly approved infer
    docker_context: str | None = None  # optional named Docker context; never changes CLI default
    execution_kind: str = "auto"  # auto, web, batch, interactive
    command_override: tuple[str, ...] | None = None  # exec form, never a shell
    health_path: str | None = None  # opt-in HTTP readiness check; otherwise TCP
    python_image: str | None = None  # supply a reviewed tag/digest
    service_images: tuple[tuple[str, str], ...] = ()  # optional explicit image digests
    persistent_dirs: tuple[str, ...] = ()
    replicas: int = 1
    cpus: str | None = None
    memory: str | None = None


@dataclasses.dataclass(frozen=True)
class PublishedPort:
    service: str
    host: int
    container: int


@dataclasses.dataclass(frozen=True)
class Plan:
    inspection: Inspection
    mode: Mode
    port: int | None
    gpu: bool
    command: tuple[str, ...]
    services: tuple[str, ...]
    warnings: tuple[str, ...]
    published_ports: tuple[PublishedPort, ...] = ()
    inferred_packages: tuple[str, ...] = ()
    execution_kind: str = "batch"
    health_path: str | None = None
    python_image: str = 'python:3.12-slim-bookworm'
    service_images: tuple[tuple[str, str], ...] = ()
    persistent_dirs: tuple[str, ...] = ()
    replicas: int = 1
    cpus: str | None = None
    memory: str | None = None


# ---------- RC6 Typed Deployment Planning and Orchestration Models ----------

class DeploymentState(str, Enum):
    DISCOVERED = "DISCOVERED"
    ANALYZED = "ANALYZED"
    PLANNED = "PLANNED"
    VALIDATED = "VALIDATED"
    APPROVED = "APPROVED"
    BUILDING = "BUILDING"
    STARTING = "STARTING"
    VERIFYING = "VERIFYING"
    HEALTHY = "HEALTHY"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    DEGRADED = "DEGRADED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


@dataclasses.dataclass(frozen=True)
class ReadinessFinding:
    level: str  # BLOCKER, WARNING, INFO, UNKNOWN
    category: str
    message: str
    key: str | None = None


@dataclasses.dataclass(frozen=True)
class DiscoveredService:
    name: str
    role: str  # database, migration, backend, frontend, ai, worker, admin, service
    image: str | None = None
    build_context: str | None = None
    dockerfile: str | None = None
    ports: tuple[str, ...] = ()
    volumes: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    environment_keys: tuple[str, ...] = ()
    env_files: tuple[str, ...] = ()
    profiles: tuple[str, ...] = ()
    restart: str | None = None
    privileged: bool = False
    network_mode: str | None = None
    command: str | None = None
    entrypoint: str | None = None


@dataclasses.dataclass(frozen=True)
class DeploymentPlan:
    project_root: Path
    infrastructure_mode: str  # "existing" or "generated"
    compose_files: tuple[str, ...]
    profiles: tuple[str, ...]
    docker_context: str | None
    services: tuple[DiscoveredService, ...]
    dependencies_graph: dict[str, tuple[str, ...]]
    topological_order: tuple[str, ...]
    startup_phases: tuple[tuple[int, str, tuple[str, ...]], ...]
    published_ports: tuple[PublishedPort, ...]
    volumes: tuple[str, ...]
    environment_prerequisites: tuple[str, ...]
    gpu_required: bool
    storage_estimate_gib: float
    risk_classification: str  # LOW, MEDIUM, HIGH, CRITICAL
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    notes: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return len(self.blockers) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "project_root": str(self.project_root),
            "infrastructure_mode": self.infrastructure_mode,
            "compose_files": list(self.compose_files),
            "profiles": list(self.profiles),
            "docker_context": self.docker_context or "default",
            "services": [
                {
                    "name": s.name,
                    "role": s.role,
                    "image": s.image,
                    "build_context": s.build_context,
                    "dockerfile": s.dockerfile,
                    "command": s.command,
                    "entrypoint": s.entrypoint,
                    "ports": list(s.ports),
                    "volumes": list(s.volumes),
                    "depends_on": list(s.depends_on),
                    "environment_keys": list(s.environment_keys),
                    "env_files": list(s.env_files),
                    "profiles": list(s.profiles),
                    "restart": s.restart,
                    "privileged": s.privileged,
                    "network_mode": s.network_mode,
                }
                for s in self.services
            ],
            "dependencies_graph": {k: list(v) for k, v in self.dependencies_graph.items()},
            "topological_order": list(self.topological_order),
            "startup_phases": [
                {"phase": num, "name": name, "services": list(svcs)}
                for num, name, svcs in self.startup_phases
            ],
            "published_ports": [
                {"service": p.service, "host": p.host, "container": p.container}
                for p in self.published_ports
            ],
            "volumes": list(self.volumes),
            "environment_prerequisites": list(self.environment_prerequisites),
            "gpu_required": self.gpu_required,
            "storage_estimate_gib": round(self.storage_estimate_gib, 1),
            "risk_classification": self.risk_classification,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "notes": list(self.notes),
            "ready": self.ready,
        }


@dataclasses.dataclass(frozen=True)
class DeploymentTransition:
    from_state: DeploymentState
    to_state: DeploymentState
    timestamp: str
    message: str


class DeploymentOrchestrator:
    """Explicit state machine managing safe, verified deployment transitions."""
    VALID_TRANSITIONS = {
        DeploymentState.DISCOVERED: {DeploymentState.ANALYZED, DeploymentState.BLOCKED, DeploymentState.FAILED},
        DeploymentState.ANALYZED: {DeploymentState.PLANNED, DeploymentState.BLOCKED, DeploymentState.FAILED, DeploymentState.ANALYZED},
        DeploymentState.PLANNED: {DeploymentState.VALIDATED, DeploymentState.BLOCKED, DeploymentState.CANCELLED, DeploymentState.PLANNED, DeploymentState.ANALYZED},
        DeploymentState.VALIDATED: {DeploymentState.APPROVED, DeploymentState.BLOCKED, DeploymentState.CANCELLED, DeploymentState.VALIDATED, DeploymentState.PLANNED, DeploymentState.ANALYZED, DeploymentState.VERIFYING, DeploymentState.HEALTHY, DeploymentState.DEGRADED},
        DeploymentState.APPROVED: {DeploymentState.BUILDING, DeploymentState.STARTING, DeploymentState.CANCELLED, DeploymentState.ANALYZED},
        DeploymentState.BUILDING: {DeploymentState.VALIDATED, DeploymentState.STARTING, DeploymentState.FAILED, DeploymentState.RECOVERY_REQUIRED},
        DeploymentState.STARTING: {DeploymentState.VERIFYING, DeploymentState.FAILED, DeploymentState.RECOVERY_REQUIRED},
        DeploymentState.VERIFYING: {DeploymentState.HEALTHY, DeploymentState.DEGRADED, DeploymentState.FAILED, DeploymentState.RECOVERY_REQUIRED, DeploymentState.APPROVED, DeploymentState.ANALYZED, DeploymentState.VERIFYING},
        DeploymentState.HEALTHY: {DeploymentState.DEGRADED, DeploymentState.FAILED, DeploymentState.RECOVERY_REQUIRED, DeploymentState.APPROVED, DeploymentState.ANALYZED, DeploymentState.VERIFYING, DeploymentState.HEALTHY},
        DeploymentState.DEGRADED: {DeploymentState.VERIFYING, DeploymentState.RECOVERY_REQUIRED, DeploymentState.FAILED, DeploymentState.APPROVED, DeploymentState.ANALYZED, DeploymentState.DEGRADED},
        DeploymentState.BLOCKED: {DeploymentState.ANALYZED, DeploymentState.PLANNED, DeploymentState.CANCELLED, DeploymentState.BLOCKED},
        DeploymentState.CANCELLED: {DeploymentState.PLANNED, DeploymentState.ANALYZED, DeploymentState.VALIDATED, DeploymentState.CANCELLED},
        DeploymentState.FAILED: {DeploymentState.RECOVERY_REQUIRED, DeploymentState.ANALYZED, DeploymentState.FAILED},
        DeploymentState.RECOVERY_REQUIRED: {DeploymentState.ANALYZED, DeploymentState.DISCOVERED},
    }

    def __init__(self, root: Path, initial: DeploymentState = DeploymentState.DISCOVERED):
        self.root = root
        self.state = initial
        self.last_validation: DeploymentState | None = None
        self.history: list[DeploymentTransition] = []

    def transition(self, to_state: DeploymentState, reason: str = "") -> None:
        allowed = self.VALID_TRANSITIONS.get(self.state, set())
        if to_state not in allowed:
            raise OpsError(f"Illegal deployment transition from {self.state.value} to {to_state.value}: {reason}")
        trans = DeploymentTransition(self.state, to_state, dt.datetime.now(dt.timezone.utc).isoformat(), reason)
        self.history.append(trans)
        self.state = to_state


def classify_service_role(name: str, image: str | None, build: str | None, restart: str | None) -> str:
    n = name.lower()
    if any(k in n for k in ('migration', 'migrate', 'alembic', 'flyway', 'db-init', 'seed')) or restart == 'no':
        return 'migration'
    if any(k in n for k in ('worker', 'ingestion', 'celery', 'consumer', 'beat', 'queue')):
        return 'worker'
    if any(k in n for k in ('postgres', 'mysql', 'mariadb', 'mongo', 'cockroach', 'redis', 'elasticsearch', 'opensearch', 'db')):
        return 'database'
    if any(k in n for k in ('pgadmin', 'adminer', 'phpmyadmin', 'redis-commander', 'dashboard')):
        return 'admin'
    if any(k in n for k in ('ollama', 'mlflow', 'triton', 'vllm', 'tgi', 'model-runner')):
        return 'ai'
    if any(k in n for k in ('prometheus', 'grafana', 'datadog', 'otel', 'jaeger', 'tempo', 'loki')):
        return 'observability'
    if any(k in n for k in ('frontend', 'client', 'ui', 'web-ui', 'vite', 'next', 'react', 'nginx')):
        return 'frontend'
    if any(k in n for k in ('backend', 'api', 'server', 'app', 'fastapi', 'flask', 'django')):
        return 'backend'

    combined = f"{image or ''} {build or ''}".lower()
    if any(k in combined for k in ('migration', 'migrate', 'alembic', 'flyway', 'db-init', 'seed')):
        return 'migration'
    if any(k in combined for k in ('postgres', 'mysql', 'mariadb', 'mongo', 'cockroach', 'redis', 'elasticsearch', 'opensearch')):
        return 'database'
    if any(k in combined for k in ('pgadmin', 'adminer', 'phpmyadmin', 'redis-commander', 'dashboard')):
        return 'admin'
    if any(k in combined for k in ('ollama', 'mlflow', 'triton', 'vllm', 'tgi', 'model-runner')):
        return 'ai'
    if any(k in combined for k in ('prometheus', 'grafana', 'datadog', 'otel', 'jaeger', 'tempo', 'loki')):
        return 'observability'
    if any(k in combined for k in ('frontend', 'client', 'ui', 'web-ui', 'vite', 'next', 'react', 'nginx')):
        return 'frontend'
    if any(k in combined for k in ('backend', 'api', 'server', 'app', 'fastapi', 'flask', 'django')):
        return 'backend'
    if any(k in combined for k in ('worker', 'ingestion', 'celery', 'consumer', 'beat', 'queue')):
        return 'worker'
    return 'service'


def parse_compose_services_static(data: str) -> dict[str, dict[str, Any]]:
    services: dict[str, dict[str, Any]] = {}
    current_service: dict[str, Any] | None = None
    current_section: str | None = None
    in_services = False

    for raw_line in data.splitlines():
        line = raw_line.split('#', 1)[0].rstrip()
        if not line:
            continue
        indent = len(line) - len(line.lstrip())
        trimmed = line.strip()

        if indent == 0:
            if trimmed.startswith('services:'):
                in_services = True
            else:
                in_services = False
            current_service = None
            continue

        if not in_services:
            continue

        if indent == 2 and trimmed.endswith(':'):
            svc_name = trimmed[:-1].strip('\'"')
            current_service = {
                'name': svc_name,
                'image': None,
                'build': None,
                'dockerfile': None,
                'command': None,
                'entrypoint': None,
                'ports': [],
                'volumes': [],
                'depends_on': [],
                'env_file': [],
                'environment': [],
                'restart': None,
                'profiles': [],
                'privileged': False,
                'network_mode': None,
            }
            services[svc_name] = current_service
            current_section = None
            continue

        if not current_service:
            continue

        if indent == 4:
            if trimmed.endswith(':'):
                current_section = trimmed[:-1].strip('\'"')
            elif ':' in trimmed:
                k, _, v = trimmed.partition(':')
                k = k.strip('\'"')
                v = v.strip().strip('\'"')
                current_section = None
                if k == 'image':
                    current_service['image'] = v
                elif k == 'build':
                    current_service['build'] = v
                elif k == 'command':
                    current_service['command'] = v
                elif k == 'entrypoint':
                    current_service['entrypoint'] = v
                elif k == 'restart':
                    current_service['restart'] = v
                elif k == 'network_mode':
                    current_service['network_mode'] = v
                elif k == 'privileged':
                    current_service['privileged'] = (v.lower() == 'true')
                elif k == 'env_file':
                    current_service['env_file'].append(v)
            continue

        if indent >= 6 and current_section:
            if trimmed.startswith('-'):
                val = trimmed[1:].strip().strip('\'"')
                if current_section == 'command':
                    if current_service['command'] is None:
                        current_service['command'] = val
                    else:
                        current_service['command'] += " " + val
                elif current_section == 'entrypoint':
                    if current_service['entrypoint'] is None:
                        current_service['entrypoint'] = val
                    else:
                        current_service['entrypoint'] += " " + val
                elif current_section == 'ports':
                    current_service['ports'].append(val)
                elif current_section == 'volumes':
                    current_service['volumes'].append(val)
                elif current_section == 'depends_on':
                    current_service['depends_on'].append(val)
                elif current_section == 'env_file':
                    current_service['env_file'].append(val)
                elif current_section == 'environment':
                    current_service['environment'].append(val)
                elif current_section == 'profiles':
                    current_service['profiles'].append(val)
            elif ':' in trimmed and current_section == 'depends_on' and indent == 6:
                dep_name = trimmed.partition(':')[0].strip('\'"')
                current_service['depends_on'].append(dep_name)
            elif ':' in trimmed and current_section == 'environment':
                k, _, _ = trimmed.partition(':')
                current_service['environment'].append(k.strip('\'"'))
            elif ':' in trimmed and current_section == 'build':
                k, _, v = trimmed.partition(':')
                k = k.strip('\'"')
                v = v.strip().strip('\'"')
                if k == 'context':
                    current_service['build'] = v
                elif k == 'dockerfile':
                    current_service['dockerfile'] = v

    return services


def topological_sort_services(services: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    in_degree = {s: 0 for s in services}
    dependents: dict[str, list[str]] = {s: [] for s in services}
    for s, deps in services.items():
        for d in deps:
            if d in in_degree:
                in_degree[s] += 1
                dependents[d].append(s)
    queue = deque(sorted([s for s, deg in in_degree.items() if deg == 0]))
    ordered: list[str] = []
    while queue:
        s = queue.popleft()
        ordered.append(s)
        for dep in sorted(dependents.get(s, [])):
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)
    cycles: list[str] = []
    if len(ordered) < len(services):
        unresolved = sorted([s for s, deg in in_degree.items() if deg > 0])
        cycles.append(f"Dependency cycle detected among services: {', '.join(unresolved)}")
    return ordered, cycles


def parse_services(text: str) -> tuple[str, ...] | None:
    """Parse optional services into canonical deterministic order; no shell expansion."""
    value = text.strip().lower()
    if value == "auto":
        return None
    if value in ("none", "off"):
        return ()
    names = [word.strip() for word in value.split(",")]
    if not names or any(not name or name not in SERVICE_ORDER for name in names):
        raise OpsError(f"Services: {','.join(SERVICE_ORDER)} (comma-separated), 'none', or 'auto'.")
    if len(names) != len(set(names)):
        raise OpsError("Each service may be selected only once.")
    return tuple(name for name in SERVICE_ORDER if name in names)


# ---------- Paths and bounded, concurrent static inspection ----------

def clean_path(raw: str, base: Path | None = None) -> Path:
    value = raw.strip()
    # Terminal file managers often paste a single fully quoted path.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    if not value:
        raise OpsError("Project path is empty.")
    value = os.path.expandvars(os.path.expanduser(value))
    path = Path(value)
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    try:
        return path.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise OpsError(f"Path not found: {path}") from exc


def target_for(raw: str, base: Path | None = None) -> Target:
    path = clean_path(raw, base)
    if path.is_file():
        name_lower = path.name.lower()
        if path.suffix.lower() == ".py":
            root, pinned = path.parent, path.name
        elif (name_lower in {"compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"}
              or name_lower.startswith("compose.")
              or name_lower.startswith("docker-compose.")):
            root, pinned = path.parent, None
        elif name_lower == "dockerfile" or name_lower.endswith(".dockerfile"):
            root, pinned = path.parent, None
        else:
            raise OpsError(
                f"Selected file '{path.name}' is not an accepted target. "
                "Choose a Python file (.py), a Docker Compose file (compose.yaml, docker-compose.yml), "
                "a Dockerfile, or a project directory."
            )
    elif path.is_dir():
        root, pinned = path, None
    else:
        raise OpsError("The selected path is not a regular file/directory.")
    if root == Path.home().resolve() or root == Path(root.anchor):
        raise OpsError("Refusing to use a home/drive-root directory. Select a project subfolder.")
    return Target(root, pinned)


def _collect_python(root: Path) -> list[Path]:
    queue: deque[tuple[Path, int]] = deque([(root, 0)])
    found: list[Path] = []
    visited = 0
    while queue:
        directory, depth = queue.popleft()
        try:
            with os.scandir(directory) as entries:
                batch = []
                for entry in entries:
                    visited += 1
                    if visited > MAX_VISITED:
                        raise ScanLimit(f"Scan exceeded {MAX_VISITED} entries; choose a smaller folder or select a .py file.")
                    batch.append(entry)
        except OSError as exc:
            raise OpsError(f"Cannot read {directory}: {exc}") from exc
        for e in sorted(batch, key=lambda x: x.name.casefold()):
            try:
                if e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    if depth < MAX_DEPTH and e.name not in SKIP_DIRS and not e.name.startswith("."):
                        queue.append((Path(e.path), depth + 1))
                elif e.is_file(follow_symlinks=False) and e.name.endswith(".py"):
                    if (e.name not in SKIP_FILES and not e.name.lower().startswith(("opsforge_", "auto_docker_"))
                            and Path(e.path).resolve() != Path(__file__).resolve()
                            and not e.name.startswith("test_")):
                        rel = Path(e.path).relative_to(root)
                        if "tests" not in rel.parts and "test" not in rel.parts:
                            found.append(Path(e.path))
                            if len(found) > MAX_PYTHON:
                                raise ScanLimit(f"More than {MAX_PYTHON} Python files; select a specific .py file.")
            except OSError:
                continue
    return found


def _check_source(path: Path, root: Path) -> Candidate:
    rel = path.relative_to(root).as_posix()
    if path.stat().st_size > MAX_PY_SIZE:
        raise OpsError(f"Python file too large to inspect safely: {rel}")
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=rel)
    except (SyntaxError, UnicodeError) as exc:
        raise OpsError(f"Cannot parse {rel}: {exc}") from exc
    imports: set[str] = set()
    main_guard = False
    app_object = False
    top_level_app = False
    for top_node in tree.body:
        if isinstance(top_node, (ast.Assign, ast.AnnAssign)):
            targets = top_node.targets if isinstance(top_node, ast.Assign) else [top_node.target]
            if any(isinstance(t, ast.Name) and t.id == "app" for t in targets):
                top_level_app = True
                break
    needs_stdin = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # Conservative static signal: direct input(), getpass.getpass(),
            # or sys.stdin.readline()/read(). Never execute inspected project code.
            func = node.func
            if isinstance(func, ast.Name) and func.id == "input":
                needs_stdin = True
            if (isinstance(func, ast.Attribute) and func.attr == "getpass"
                    and isinstance(func.value, ast.Name) and func.value.id == "getpass"):
                needs_stdin = True
            if (isinstance(func, ast.Attribute) and func.attr in ("read", "readline", "readlines")
                    and isinstance(func.value, ast.Attribute) and func.value.attr == "stdin"
                    and isinstance(func.value.value, ast.Name) and func.value.value.id == "sys"):
                needs_stdin = True
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "app" for t in targets):
                app_object = True
        elif isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
            # Inspect AST, never execute user Python code.
            t = node.test
            if isinstance(t.left, ast.Name) and t.left.id == "__name__" and any(
                isinstance(c, ast.Constant) and c.value == "__main__" for c in t.comparators
            ):
                main_guard = True
    framework = "python"
    if "streamlit" in imports:
        framework = "streamlit"
    elif "fastapi" in imports and top_level_app:
        framework = "fastapi"
    elif "flask" in imports and top_level_app:
        framework = "flask"
    elif "gradio" in imports:
        framework = "gradio"
    name = path.name.lower()
    score = (45 if path.parent == root else max(0, 30 - 5 * (len(path.relative_to(root).parts) - 1)))
    score += {"main.py": 30, "app.py": 30, "server.py": 27, "run.py": 25, "manage.py": 10}.get(name, 0)
    score += 35 if main_guard else 0
    score += 25 if framework != "python" else 0
    score += 10 if (app_object and top_level_app) else 0
    if name.startswith("_") or "util" in name or "config" in name:
        score -= 35
    return Candidate(rel, score, frozenset(imports), main_guard, framework, top_level_app, needs_stdin)


def _deps(root: Path) -> str | None:
    # 1. Standard requirements.txt at root
    if (root / "requirements.txt").is_file():
        return "requirements.txt"
    # 2. Modern pyproject.toml
    p = root / "pyproject.toml"
    if p.is_file():
        text = p.read_text(encoding="utf-8", errors="replace")[:200_000]
        if re.search(r"(?m)^\s*\[(project|build-system|tool\.poetry)\]\s*$", text):
            return "pyproject.toml"
    # 3. Setup configuration files
    if (root / "setup.cfg").is_file():
        return "setup.cfg"
    if (root / "setup.py").is_file():
        return "setup.py"
    # 4. Pipfile
    if (root / "Pipfile").is_file():
        return "Pipfile"
    # 5. Conda environment manifests
    if (root / "environment.yml").is_file():
        return "environment.yml"
    if (root / "environment.yaml").is_file():
        return "environment.yaml"
    # 6. Specialized requirements files
    if (root / "requirements-dev.txt").is_file():
        return "requirements-dev.txt"
    req_dir = root / "requirements"
    if req_dir.is_dir():
        for candidate in ("base.txt", "prod.txt", "production.txt", "common.txt", "requirements.txt", "dev.txt"):
            if (req_dir / candidate).is_file():
                return f"requirements/{candidate}"
        txts = sorted(f.name for f in req_dir.glob("*.txt") if f.is_file())
        if txts:
            return f"requirements/{txts[0]}"
    return None


# ---------- Read-only Python dependency preflight ----------
# Standard library immunity: stdlib modules must NEVER be classified as missing
STDLIB_MODULES: frozenset[str] = getattr(sys, "stdlib_module_names", frozenset({
    "abc", "aifc", "antigravity", "argparse", "array", "ast", "asynchat", "asyncio", "asyncore",
    "atexit", "audioop", "base64", "bdb", "binascii", "binhex", "bisect", "builtins", "bz2",
    "cProfile", "calendar", "cgi", "cgitb", "chunk", "cmath", "cmd", "code", "codecs",
    "codeop", "collections", "colorsys", "compileall", "concurrent", "configparser", "contextlib",
    "contextvars", "copy", "copyreg", "crypt", "csv", "ctypes", "curses", "dataclasses",
    "datetime", "dbm", "decimal", "difflib", "dis", "distutils", "doctest", "email",
    "encodings", "ensurepip", "enum", "errno", "faulthandler", "fcntl", "filecmp", "fileinput",
    "fnmatch", "fractions", "ftplib", "functools", "gc", "genericpath", "getopt", "getpass",
    "gettext", "glob", "graphlib", "grp", "gzip", "hashlib", "heapq", "hmac", "html",
    "http", "idlelib", "imaplib", "imghdr", "imp", "importlib", "inspect", "io",
    "ipaddress", "itertools", "json", "keyword", "linecache", "locale", "logging", "lzma",
    "mailbox", "mailcap", "marshal", "math", "mimetypes", "mmap", "modulefinder", "msilib",
    "msvcrt", "multiprocessing", "netrc", "nntplib", "nt", "ntpath", "nturl2path", "numbers",
    "opcode", "operator", "optparse", "os", "ossaudiodev", "pathlib", "pdb", "pickle",
    "pickletools", "pipes", "pkgutil", "platform", "plistlib", "poplib", "posix", "posixpath",
    "pprint", "profile", "pstats", "pty", "pwd", "py_compile", "pyclbr", "pydoc", "pydoc_data",
    "pyexpat", "queue", "quopri", "random", "re", "readline", "reprlib", "resource", "rlcompleter",
    "runpy", "sched", "secrets", "select", "selectors", "shelve", "shlex", "shutil", "signal",
    "site", "smtpd", "smtplib", "sndhdr", "socket", "socketserver", "spwd", "sqlite3",
    "sre_compile", "sre_constants", "sre_parse", "ssl", "stat", "statistics", "string",
    "stringprep", "struct", "subprocess", "sunau", "symtable", "sys", "sysconfig", "syslog",
    "tabnanny", "tarfile", "telnetlib", "tempfile", "termios", "test", "textwrap", "this",
    "threading", "time", "timeit", "tkinter", "token", "tokenize", "tomllib", "trace",
    "traceback", "tracemalloc", "tty", "turtle", "turtledemo", "types", "typing", "unicodedata",
    "unittest", "urllib", "uu", "uuid", "venv", "warnings", "wave", "weakref", "webbrowser",
    "winreg", "winsound", "wsgiref", "xdrlib", "xml", "xmlrpc", "zipapp", "zipfile", "zipimport",
    "zlib", "_thread"
}))

# Known import-to-distribution mappings (120+ curated packages and canonical PyPI aliases):
# Top-level import alias is mapped to its installable PyPI distribution name.
IMPORT_PACKAGES: dict[str, str] = {
    # Vision & Image Processing (including package aliases)
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "skimage": "scikit-image",
    "fitz": "PyMuPDF",
    "torchvision": "torchvision",
    "timm": "timm",
    # Audio & Signal Processing
    "soundfile": "soundfile",
    "librosa": "librosa",
    "torchaudio": "torchaudio",
    "pydub": "pydub",
    # Scientific & Numerical Computing
    "numpy": "numpy",
    "scipy": "scipy",
    "pandas": "pandas",
    "polars": "polars",
    "pyarrow": "pyarrow",
    "statsmodels": "statsmodels",
    "sympy": "sympy",
    "networkx": "networkx",
    # Data Visualization
    "matplotlib": "matplotlib",
    "seaborn": "seaborn",
    "plotly": "plotly",
    # Classical Machine Learning
    "sklearn": "scikit-learn",
    "xgboost": "xgboost",
    "lightgbm": "lightgbm",
    "catboost": "catboost",
    "joblib": "joblib",
    "optuna": "optuna",
    # Deep Learning Frameworks
    "torch": "torch",
    "tensorflow": "tensorflow",
    "keras": "keras",
    "tensorboard": "tensorboard",
    "einops": "einops",
    # LLM, NLP & Generative AI
    "transformers": "transformers",
    "accelerate": "accelerate",
    "datasets": "datasets",
    "diffusers": "diffusers",
    "tiktoken": "tiktoken",
    "openai": "openai",
    "anthropic": "anthropic",
    "ollama": "ollama",
    "vllm": "vllm",
    "litellm": "litellm",
    "langchain": "langchain",
    "langchain_core": "langchain-core",
    "langchain_community": "langchain-community",
    "llama_index": "llama-index",
    "sentence_transformers": "sentence-transformers",
    "chromadb": "chromadb",
    "qdrant_client": "qdrant-client",
    "faiss": "faiss-cpu",
    "huggingface_hub": "huggingface-hub",
    # MLOps & Experiment Tracking
    "mlflow": "mlflow",
    "wandb": "wandb",
    # Web Frameworks & APIs
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "starlette": "starlette",
    "flask": "flask",
    "gunicorn": "gunicorn",
    "streamlit": "streamlit",
    "gradio": "gradio",
    "django": "django",
    "tornado": "tornado",
    "aiohttp": "aiohttp",
    "dash": "dash",
    "celery": "celery",
    # Databases & Caching
    "redis": "redis",
    "sqlalchemy": "sqlalchemy",
    "alembic": "alembic",
    "psycopg2": "psycopg2-binary",
    "asyncpg": "asyncpg",
    "pymysql": "pymysql",
    "pymongo": "pymongo",
    "motor": "motor",
    "elasticsearch": "elasticsearch",
    # Observability & Metrics
    "prometheus_client": "prometheus-client",
    # Networking & HTTP Clients
    "requests": "requests",
    "httpx": "httpx",
    "urllib3": "urllib3",
    "websockets": "websockets",
    "aiofiles": "aiofiles",
    # Serialization, Parsing & Documents
    "yaml": "PyYAML",
    "pydantic": "pydantic",
    "pydantic_settings": "pydantic-settings",
    "bs4": "beautifulsoup4",
    "lxml": "lxml",
    "openpyxl": "openpyxl",
    "docx": "python-docx",
    "pypdf": "pypdf",
    "pdfplumber": "pdfplumber",
    "orjson": "orjson",
    "ujson": "ujson",
    "msgpack": "msgpack",
    "protobuf": "protobuf",
    # Security, Auth & Environment
    "dotenv": "python-dotenv",
    "cryptography": "cryptography",
    "jwt": "PyJWT",
    # CLI & Terminal Utilities
    "rich": "rich",
    "tqdm": "tqdm",
    "click": "click",
    "typer": "typer",
    "tabulate": "tabulate",
    # Common Helpers & Cloud SDKs
    "dateutil": "python-dateutil",
    "pytz": "pytz",
    "serial": "pyserial",
    "attr": "attrs",
    "attrs": "attrs",
    "loguru": "loguru",
    "structlog": "structlog",
    "boto3": "boto3",
    "botocore": "botocore",
}


def _distribution_key(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclasses.dataclass(frozen=True)
class DependencyReport:
    observed: tuple[str, ...]
    declared: tuple[str, ...]
    possible_missing: tuple[str, ...]
    warnings: tuple[str, ...]


class DependencyAudit:
    """Static, offline audit. Does not import the project or run pip.

    A missing declaration is only a warning, never conclusive evidence of an
    un-installable project: packages can be optional or provided transitively.
    """

    @staticmethod
    def _requirements(root: Path, manifest: str = "requirements.txt") -> tuple[set[str], list[str]]:
        names: set[str] = set()
        issues: list[str] = []
        pending = [root / manifest]
        seen: set[Path] = set()
        while pending:
            file = pending.pop()
            try:
                real = file.resolve(strict=True)
                real.relative_to(root.resolve())
                if not real.is_file() or file.is_symlink():
                    raise ValueError("not a regular in-project file")
            except (FileNotFoundError, OSError, ValueError) as exc:
                issues.append(f"Cannot safely read requirements include {file.name}: {exc}")
                continue
            if real in seen:
                continue
            seen.add(real)
            if len(seen) > 16:
                issues.append("More than 16 requirements files; audit stopped.")
                break
            try:
                if real.stat().st_size > 256_000:
                    issues.append(f"Requirements file too large to audit: {file.name}")
                    continue
                text = real.read_text(encoding="utf-8-sig")
            except (UnicodeError, OSError) as exc:
                issues.append(f"Cannot decode {file.name}: {exc}")
                continue
            for raw in text.splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                include = re.match(r"^(?:-r\s*|--requirement(?:=|\s+))(.+)$", line)
                if include:
                    inner = include.group(1).strip().strip('"\'')
                    pending.append(real.parent / inner)
                    continue
                if line.startswith("-") or line.startswith("git+") or line.startswith("."):
                    continue  # Pip flags and editable/VCS installs aren't statically resolvable.
                match = re.match(r"^([a-zA-Z][a-zA-Z0-9_.-]*)\s*(?:\[([^\]]+)\])?", line)
                if match:
                    names.add(_distribution_key(match.group(1)))
                    if match.group(1).lower() == "fastapi" and match.group(2) and "standard" in match.group(2).split(','):
                        names.add("uvicorn")  # fastapi[standard] bundles a Uvicorn dependency.
        return names, issues

    @staticmethod
    def _pyproject(root: Path) -> tuple[set[str], list[str]]:
        path = root / "pyproject.toml"
        if path.is_symlink():
            return set(), ["Refusing to audit symlinked pyproject.toml"]
        try:
            import tomllib  # Python 3.11+
            if path.stat().st_size > 256_000:
                return set(), ["pyproject.toml too large to audit."]
            with path.open("rb") as f:
                cfg = tomllib.load(f)
            project = cfg.get("project", {})
            project = project if isinstance(project, dict) else {}
            poetry = cfg.get("tool", {}).get("poetry", {}) if isinstance(cfg.get("tool"), dict) else {}
            poetry = poetry if isinstance(poetry, dict) else {}
            declared: set[str] = set()
            declarations = project.get("dependencies", [])
            if isinstance(declarations, list):
                for val in declarations:
                    if isinstance(val, str):
                        match = re.match(r"^\s*([a-zA-Z][a-zA-Z0-9_.-]*)", val)
                        if match:
                            declared.add(_distribution_key(match.group(1)))
                            if match.group(1).lower() == "fastapi" and "[standard]" in val.lower():
                                declared.add("uvicorn")
            opt_deps = project.get("optional-dependencies", {})
            if isinstance(opt_deps, dict):
                for _, extra_list in opt_deps.items():
                    if isinstance(extra_list, list):
                        for val in extra_list:
                            if isinstance(val, str):
                                m = re.match(r"^\s*([a-zA-Z][a-zA-Z0-9_.-]*)", val)
                                if m:
                                    declared.add(_distribution_key(m.group(1)))
            poetry_deps = poetry.get("dependencies", {})
            if isinstance(poetry_deps, dict):
                declared.update(_distribution_key(k) for k in poetry_deps if isinstance(k, str))
            issues = []
            if isinstance(project.get("dynamic"), list) and "dependencies" in project["dynamic"]:
                issues.append("Project dependencies are dynamic; static audit may miss them.")
            return declared, issues
        except ImportError:
            # Python 3.10 fallback regex parser
            declared_fallback: set[str] = set()
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                for m in re.finditer(r"(?:dependencies|requires)\s*=\s*\[(.*?)\]", text, re.DOTALL):
                    for item in re.finditer(r"""['"]([a-zA-Z][a-zA-Z0-9_.-]*)""", m.group(1)):
                        declared_fallback.add(_distribution_key(item.group(1)))
                in_deps = False
                for raw in text.splitlines():
                    line = raw.strip()
                    if re.search(r"^\s*\[(?:project\.dependencies|tool\.poetry\.dependencies)\]", line):
                        in_deps = True
                        continue
                    if in_deps:
                        if line.startswith("["):
                            in_deps = False
                            continue
                        m = re.match(r"^[\"']?([a-zA-Z][a-zA-Z0-9_.-]*)", line)
                        if m and m.group(1).lower() != "python":
                            declared_fallback.add(_distribution_key(m.group(1)))
                return declared_fallback, ["tomllib not available (Python < 3.11); pyproject.toml parsed with best-effort fallback"]
            except Exception as exc:
                return set(), [f"Unable to parse pyproject.toml: {exc}"]
        except (OSError, ValueError) as exc:
            return set(), [f"Unable to parse pyproject.toml: {exc}"]

    @staticmethod
    def _setup_cfg_or_py(root: Path, manifest: str = "setup.cfg") -> tuple[set[str], list[str]]:
        path = root / manifest
        if path.is_symlink():
            return set(), [f"Refusing to audit symlinked {manifest}"]
        try:
            if not path.is_file():
                return set(), []
            if path.stat().st_size > 256_000:
                return set(), [f"{manifest} too large to audit."]
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except (OSError, UnicodeError) as exc:
            return set(), [f"Cannot read {manifest}: {exc}"]

        names: set[str] = set()
        issues: list[str] = []
        if manifest.endswith(".cfg"):
            try:
                import configparser
                parser = configparser.ConfigParser()
                parser.read_string(text)
                if parser.has_section("options"):
                    opt = parser.get("options", "install_requires", fallback="")
                    for line in opt.splitlines():
                        match = re.match(r"^\s*([a-zA-Z][a-zA-Z0-9_.-]*)", line)
                        if match:
                            names.add(_distribution_key(match.group(1)))
                if parser.has_section("options.extras_require"):
                    for _, extra_val in parser.items("options.extras_require"):
                        for line in extra_val.splitlines():
                            match = re.match(r"^\s*([a-zA-Z][a-zA-Z0-9_.-]*)", line)
                            if match:
                                names.add(_distribution_key(match.group(1)))
            except Exception as exc:
                issues.append(f"Failed to parse setup.cfg: {exc}")
        else:
            try:
                tree = ast.parse(text)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call):
                        for kw in node.keywords:
                            if kw.arg in ("install_requires", "requires"):
                                if isinstance(kw.value, (ast.List, ast.Tuple, ast.Set)):
                                    for elt in kw.value.elts:
                                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                            match = re.match(r"^\s*([a-zA-Z][a-zA-Z0-9_.-]*)", elt.value)
                                            if match:
                                                names.add(_distribution_key(match.group(1)))
            except Exception:
                pass
            if not names:
                for match in re.finditer(r"['\"]([a-zA-Z][a-zA-Z0-9_.-]*)(?:[><=~^!].*?)?['\"]", text):
                    val = match.group(1)
                    if _distribution_key(val) in IMPORT_PACKAGES.values() or val.lower() in IMPORT_PACKAGES:
                        names.add(_distribution_key(val))
        return names, issues

    @staticmethod
    def _pipfile(root: Path) -> tuple[set[str], list[str]]:
        path = root / "Pipfile"
        if path.is_symlink():
            return set(), ["Refusing to audit symlinked Pipfile"]
        try:
            if not path.is_file():
                return set(), []
            if path.stat().st_size > 256_000:
                return set(), ["Pipfile too large to audit."]
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except (OSError, UnicodeError) as exc:
            return set(), [f"Cannot read Pipfile: {exc}"]
        names: set[str] = set()
        in_packages = False
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("["):
                in_packages = line.lower() in ("[packages]", "[dev-packages]")
                continue
            if in_packages and line and not line.startswith("#"):
                match = re.match(r"^\s*([a-zA-Z0-9_.-]+)\s*=", line)
                if match:
                    names.add(_distribution_key(match.group(1)))
        return names, []

    @staticmethod
    def _environment_yml(root: Path, manifest: str = "environment.yml") -> tuple[set[str], list[str]]:
        path = root / manifest
        if path.is_symlink():
            return set(), [f"Refusing to audit symlinked {manifest}"]
        try:
            if not path.is_file():
                return set(), []
            if path.stat().st_size > 256_000:
                return set(), [f"{manifest} too large to audit."]
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except (OSError, UnicodeError) as exc:
            return set(), [f"Cannot read {manifest}: {exc}"]
        names: set[str] = set()
        in_deps = False
        for raw in text.splitlines():
            stripped = raw.strip()
            if re.match(r"^dependencies\s*:", stripped):
                in_deps = True
                continue
            if in_deps:
                if re.match(r"^[a-zA-Z0-9_-]+\s*:", stripped) and not stripped.startswith("-"):
                    in_deps = False
                    continue
                match = re.match(r"^\s*-\s*([a-zA-Z][a-zA-Z0-9_.-]*)", stripped)
                if match:
                    pkg = match.group(1).lower()
                    if pkg not in ("python", "pip"):
                        names.add(_distribution_key(match.group(1)))
        return names, []

    @staticmethod
    def _reachable_imports(inspection: Inspection) -> tuple[set[str], list[str]]:
        """BFS through directly reachable local .py modules, without importing them.

        Resolves static local imports only; dynamic imports, optional extras, and
        installed-package versions remain outside the scope of static inference.
        """
        root = inspection.root.resolve()
        first = root / inspection.entry.path
        queue: deque[Path] = deque([first])
        seen: set[Path] = set()
        imports: set[str] = set()
        warnings: list[str] = []
        MAX_REACHABLE = 64

        def local_candidates(spec: str, search_roots: tuple[Path, ...]) -> list[Path]:
            parts = tuple(part for part in spec.split('.') if part)
            if not parts or any(not part.isidentifier() for part in parts):
                return []
            result: list[Path] = []
            for base in search_roots:
                candidate = base.joinpath(*parts)
                result.extend([candidate.with_suffix('.py'), candidate / '__init__.py'])
            return result

        while queue:
            current = queue.popleft()
            try:
                path = current.resolve(strict=True)
                path.relative_to(root)
                if not path.is_file() or current.is_symlink() or path.stat().st_size > MAX_PY_SIZE:
                    continue
            except (OSError, ValueError):
                continue
            if path in seen:
                continue
            if len(seen) >= MAX_REACHABLE:
                warnings.append(f"Local dependency scan hit {MAX_REACHABLE} files; report may be incomplete.")
                break
            seen.add(path)
            try:
                tree = ast.parse(path.read_text(encoding='utf-8-sig'))
            except (OSError, UnicodeError, SyntaxError) as exc:
                warnings.append(f"Could not statically inspect local module {path.relative_to(root)}: {type(exc).__name__}")
                continue
            targets: list[Path] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.add(alias.name.split('.')[0])
                        targets.extend(local_candidates(alias.name, (root, first.parent)))
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        base = path.parent
                        for _ in range(node.level - 1):
                            base = base.parent
                        search_roots = (base,)
                    else:
                        search_roots = (root, first.parent)
                    module = node.module or ''
                    if module:
                        if not node.level:
                            imports.add(module.split('.')[0])
                        targets.extend(local_candidates(module, search_roots))
                    for alias in node.names:
                        if alias.name == '*':
                            continue
                        targets.extend(local_candidates(f"{module}.{alias.name}".strip('.'), search_roots))
            # A local module that also shares a name with a third-party package
            # is ambiguous: retain the warning rather than claiming certainty.
            for candidate in sorted(set(targets)):
                if candidate.is_file() and not candidate.is_symlink():
                    queue.append(candidate)
        return imports, warnings

    def report(self, inspection: Inspection) -> DependencyReport:
        imports, reach_warnings = self._reachable_imports(inspection)
        # Exclude standard library modules from observed imports (standard library immunity)
        all_raw_imports = (inspection.entry.imports | imports) - STDLIB_MODULES
        observed = {IMPORT_PACKAGES[m] for m in all_raw_imports if m in IMPORT_PACKAGES}
        if inspection.entry.framework == "fastapi":
            observed.add("uvicorn")  # Required by generated launch command.
        warnings: list[str] = list(reach_warnings)
        declared: set[str] = set()

        # Audit primary dependency manifest
        dep = inspection.dependency
        if dep:
            if dep.endswith(".txt"):
                found, issues = self._requirements(inspection.root, manifest=dep)
                declared.update(found)
                warnings.extend(issues)
            elif dep == "pyproject.toml":
                found, issues = self._pyproject(inspection.root)
                declared.update(found)
                warnings.extend(issues)
            elif dep in ("setup.cfg", "setup.py"):
                found, issues = self._setup_cfg_or_py(inspection.root, manifest=dep)
                declared.update(found)
                warnings.extend(issues)
            elif dep == "Pipfile":
                found, issues = self._pipfile(inspection.root)
                declared.update(found)
                warnings.extend(issues)
            elif dep in ("environment.yml", "environment.yaml"):
                found, issues = self._environment_yml(inspection.root, manifest=dep)
                declared.update(found)
                warnings.extend(issues)

        # Also inspect any secondary or co-existing manifests to prevent false undeclared warnings
        for secondary in ("requirements.txt", "pyproject.toml", "setup.cfg", "setup.py", "Pipfile", "environment.yml"):
            if secondary != dep and (inspection.root / secondary).is_file():
                if secondary.endswith(".txt"):
                    f_sec, _ = self._requirements(inspection.root, manifest=secondary)
                    declared.update(f_sec)
                elif secondary == "pyproject.toml":
                    f_sec, _ = self._pyproject(inspection.root)
                    declared.update(f_sec)
                elif secondary in ("setup.cfg", "setup.py"):
                    f_sec, _ = self._setup_cfg_or_py(inspection.root, manifest=secondary)
                    declared.update(f_sec)
                elif secondary == "Pipfile":
                    f_sec, _ = self._pipfile(inspection.root)
                    declared.update(f_sec)
                elif secondary in ("environment.yml", "environment.yaml"):
                    f_sec, _ = self._environment_yml(inspection.root, manifest=secondary)
                    declared.update(f_sec)

        if not dep and observed:
            warnings.append("No requirements.txt, pyproject.toml, setup.cfg, setup.py, or Pipfile: strict mode installs no third-party imports; 'deps infer' is an explicit opt-in.")
        missing = sorted(name for name in observed if _distribution_key(name) not in declared)
        if missing and dep:
            warnings.append("Possibly undeclared imports (may be provided through extras or transitively): " + ", ".join(missing))
        return DependencyReport(tuple(sorted(observed)), tuple(sorted(declared)), tuple(missing), tuple(warnings))

    @staticmethod
    def print_report(report: DependencyReport) -> None:
        print("\nAuto Docker dependency preflight (static, read-only)")
        print("  Known third-party packages: " + (", ".join(report.observed) or "(none identified)"))
        print("  Explicitly declared packages: " + (", ".join(report.declared) or "(none found)"))
        for warning in report.warnings:
            print("  WARNING: " + warning)
        print("  Imports are inspected without executing your app. Review the project's dependency manifest before building.")


class Inspector:
    def inspect(self, target: Target, entry: str | None = None) -> Inspection:
        root = target.root
        selected = entry or target.pinned
        if selected:
            selected = selected.replace("\\", "/").strip().strip('"\'')
            p = root / selected
            resolved = p.resolve(strict=True)
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise OpsError("Entrypoint must be inside the selected project.") from exc
            if not resolved.is_file() or resolved.suffix != ".py":
                raise OpsError("Entrypoint must point to a .py file.")
            paths = [resolved]
        else:
            has_compose = bool(ExistingInfrastructure.discover(root).compose_files)
            if has_compose:
                try:
                    paths = _collect_python(root)
                except ScanLimit:
                    # When Compose infrastructure exists, do not fail on 240-file limit.
                    # Gather top-level and standard service entrypoints.
                    paths = []
                    for name in ('app.py', 'main.py', 'server.py', 'run.py',
                                 'backend/app.py', 'backend/main.py', 'backend/server.py',
                                 'src/app.py', 'src/main.py', 'api/main.py'):
                        candidate_p = root / name
                        if candidate_p.is_file():
                            paths.append(candidate_p)
                    if not paths:
                        paths = [p for p in root.glob('*.py') if p.is_file() and not p.name.startswith(('test_', 'setup.py'))]
                    if not paths:
                        # Fallback to any python files in root subdirectories up to 20
                        for p in root.glob('*/*.py'):
                            if not p.name.startswith(('test_', 'setup.py')) and len(paths) < 20:
                                paths.append(p)
            else:
                paths = _collect_python(root)
        if not paths:
            raise OpsError(f"No application Python files found under {root}.")
        workers = min(4, max(1, os.cpu_count() or 1), len(paths))
        # Read-only tasks run concurrently; sorting yields stable results.
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_check_source, path, root) for path in paths]
            parsed: list[Candidate] = []
            failures: list[str] = []
            for f in futures:
                try:
                    parsed.append(f.result())
                except (OpsError, OSError) as exc:
                    failures.append(str(exc))
        if not parsed:
            raise OpsError("No parsable Python entrypoints. " + "; ".join(failures[:3]))
        if not selected and any(re.search(r"(?:^|[/\s])(?:app|main|server|run)\.py:", problem) for problem in failures):
            raise OpsError("A likely application entrypoint failed to parse; choose an exact .py file after fixing its syntax. "
                           + "; ".join(failures[:2]))
        ranked = tuple(sorted(parsed, key=lambda c: (-c.score, c.path)))
        if not selected and len(ranked) > 1 and ranked[0].score - ranked[1].score < 20:
            names = ", ".join(f"{c.path} ({c.score})" for c in ranked[:5])
            raise AmbiguousEntry(f"Multiple possible entrypoints: {names}. Use: entry RELATIVE_FILE.py")
        chosen = ranked[0]
        imports = chosen.imports
        mode = Mode.BASE
        if imports & {"langchain", "langchain_core", "llama_index", "ollama", "vllm", "litellm", "openai", "anthropic"}:
            mode = Mode.LLMOPS
        elif imports & {"mlflow", "sklearn", "torch", "tensorflow", "xgboost"}:
            mode = Mode.MLOPS
        warnings = list(failures[:4])
        dependency = _deps(root)
        if dependency is None:
            warnings.append("No requirements.txt or pyproject.toml at project root. Review dependency handling before building.")
        if chosen.framework == "fastapi" and dependency == "requirements.txt":
            req_text = (root / "requirements.txt").read_text(encoding="utf-8", errors="replace").lower()
            if "uvicorn" not in req_text:
                warnings.append("FastAPI requires uvicorn in requirements.txt for the generated launch command.")
        LOG.info("project inspection complete: %d Python files; framework=%s", len(paths), chosen.framework)
        return Inspection(root, chosen, dependency, mode, ranked[:8], len(paths), tuple(warnings))


# ---------- Workload planning ----------

class Planner:
    def build(self, settings: Settings, inspection: Inspection) -> Plan:
        mode = inspection.detected_mode if settings.mode == Mode.AUTO else settings.mode
        # Auto mode suggests a profile, but never conceals service creation in preview.
        preset_services: tuple[str, ...] = {
            Mode.BASE: (),
            Mode.MLOPS: ("mlflow",),
            Mode.LLMOPS: ("mlflow", "ollama"),
            Mode.GENAI: ("mlflow", "ollama", "redis"),
        }[mode]
        services = preset_services if settings.services_override is None else settings.services_override
        if any(name not in SERVICE_ORDER for name in services):
            raise OpsError("Unsupported optional service in configuration.")
        f = inspection.entry.framework
        port = settings.port if settings.port is not None else {"fastapi": 8000, "flask": 5000, "streamlit": 8501, "gradio": 7860}.get(f)
        if port is not None and not (1024 <= port <= 65535):
            raise OpsError("App port must be between 1024 and 65535.")
        if settings.execution_kind not in EXECUTION_KINDS:
            raise OpsError("Execution kind must be auto, web, batch, or interactive.")
        kind = settings.execution_kind
        if kind == "auto":
            kind = ("web" if f != "python" else
                    "interactive" if inspection.entry.needs_stdin else "batch")
        if f != "python" and kind != "web":
            raise OpsError("Web-framework entrypoints must use 'kind web'.")
        if kind == "web" and port is None:
            raise OpsError("Web applications need a port; set 'port 8080' or --port 8080.")
        entry = inspection.entry.path
        # --app-dir lets Uvicorn import a selected file under a directory with
        # spaces, dashes, or no __init__.py, without executing it during inspection.
        entry_path = Path(entry)
        app_directory = entry_path.parent.as_posix()
        if f == "fastapi":
            command = ("python", "-m", "uvicorn", f"{entry_path.stem}:app", "--app-dir", app_directory,
                       "--host", "0.0.0.0", "--port", str(port))
        elif f == "flask":
            command = ("python", "-m", "flask", "--app", f"{entry}:app", "run",
                       "--host", "0.0.0.0", "--port", str(port))
        elif f == "streamlit":
            command = ("python", "-m", "streamlit", "run", entry, "--server.address=0.0.0.0", f"--server.port={port}")
        else:
            command = ("python", entry)
        if settings.command_override is not None:
            override = settings.command_override
            if (not override or len(override) > 32 or
                    any(not isinstance(item, str) or not item or len(item) > 512 or
                        any(x in item for x in ('\x00', '\r', '\n')) for item in override)):
                raise OpsError('Custom command must be 1-32 nonempty exec-form arguments')
            command = tuple(override)
        image = settings.python_image or 'python:3.12-slim-bookworm'
        if (len(image) > 240 or not re.fullmatch(r'[A-Za-z0-9_./:@-]+', image) or
                '@' in image and not re.search(r'@sha256:[a-f0-9]{64}$', image)):
            raise OpsError('Python image must be a valid image reference (optionally @sha256:<64-hex-digest>)')
        overrides = settings.service_images
        if (len(overrides) != len({key for key, _ in overrides}) or
                any(key not in SERVICE_ORDER or key not in services or
                    len(value) > 240 or not re.fullmatch(r'[A-Za-z0-9_./:@-]+', value) or
                    ('@' in value and not re.search(r'@sha256:[a-f0-9]{64}$', value))
                    for key, value in overrides)):
            raise OpsError('Service image overrides must reference enabled services and valid image names/digests')
        path = settings.health_path
        if path is not None:
            if kind != 'web' or not re.fullmatch(r'/[A-Za-z0-9_./?&=%-]{0,199}', path) or path.startswith('//'):
                raise OpsError('HTTP health path must be a short absolute path for a web application')
        warnings = list(inspection.warnings)
        if settings.command_override:
            warnings.append('Custom exec-form command is in use; ensure required packages and files exist.')
        if kind == "interactive":
            warnings.append("Interactive CLI selected: run with 'run YES' on a real terminal, not detached 'up'.")
        elif kind == "batch":
            warnings.append("Batch job selected: prefer 'job YES' for foreground exit-code checking.")
        if settings.execution_kind == "auto" and inspection.entry.needs_stdin:
            warnings.append("Static inspection found input()/stdin; review with 'kind interactive|batch' if inaccurate.")
        if settings.dependency_mode not in ("strict", "infer"):
            raise OpsError("Dependency policy must be 'strict' or 'infer'.")
        inferred_packages: tuple[str, ...] = ()
        if settings.dependency_mode == "infer":
            if inspection.dependency is not None:
                raise OpsError("'deps infer' works only without a dependency manifest. "
                               "Edit requirements.txt / pyproject.toml instead, or select 'deps strict'.")
            audit = DependencyAudit().report(inspection)
            inferred_packages = audit.possible_missing
            if not inferred_packages:
                warnings.append("No known third-party distributions were inferred. This does NOT prove dependencies are complete.")
            else:
                warnings.append("INFERENCE OPT-IN: pip will install unpinned third-party packages: "
                                + ", ".join(inferred_packages)
                                + ". Resolve and pin reviewed versions for reproducible builds.")
                warnings.append("Static imports cannot prove which dependencies, versions, or system libraries your app needs.")
        if settings.services_override is not None:
            warnings.append("Optional services manually selected; workload profile does not add other services.")
        if f == "gradio":
            warnings.append("Gradio is configured through GRADIO_SERVER_NAME/PORT; app code overriding them may need adjustment.")
        if settings.gpu and "ollama" not in services:
            raise OpsError("GPU option currently applies to the Ollama service (LLMOps / GenAI Ops).")
        if settings.gpu:
            warnings.append("GPU mode needs NVIDIA GPU drivers and NVIDIA Container Toolkit on the Docker host.")
        if "ollama" in services:
            warnings.append("Ollama starts with no downloaded models. Pull an approved model separately before inference.")
        if services:
            warnings.append("Compose health checks gate initial startup; applications should still retry after later service outages.")
        warnings.append("Infrastructure is a local-development baseline, not a production-hardened deployment.")
        ports: list[PublishedPort] = []
        used: set[int] = set()
        specs = ([('app', port)] if port is not None else []) + [
            (name, container) for name, container in (
                ('mlflow', 5000),
                ('ollama', 11434),
                ('prometheus', 9090),
                ('grafana', 3000),
            ) if name in services
        ]
        overrides = dict(settings.host_ports)
        if len(overrides) != len(settings.host_ports):
            raise OpsError("Duplicate host port override for one service.")
        missing = sorted(set(overrides) - {name for name, _ in specs})
        if missing:
            raise OpsError("Cannot publish ports for disabled or portless service(s): " + ", ".join(missing))
        explicit = list(overrides.values())
        if len(explicit) != len(set(explicit)):
            raise OpsError('Two explicit host port overrides cannot use the same port.')
        for service, container in specs:
            if service in overrides:
                host = overrides[service]
                if not isinstance(host, int) or not (1024 <= host <= 65535):
                    raise OpsError("Host ports must be integers between 1024 and 65535.")
                if host in used:
                    raise OpsError(f"Host port {host} is already assigned to a service in this plan.")
                used.add(host)
            else:
                # Reserve explicitly chosen host ports before allocating defaults,
                # regardless of the order services appear in Compose.
                host = _choose_port(container, used | set(explicit))
                used.add(host)
            ports.append(PublishedPort(service, host, container))
        persistent_dirs = settings.persistent_dirs
        if not persistent_dirs:
            auto_dirs = [d for d in ("artifacts", "models", "runs", "checkpoints", "data", "datasets", "registry", "storage", "uploads", "outputs", "output", "logs", "weights", "db", "database") if (inspection.root / d).is_dir()]
            persistent_dirs = tuple(auto_dirs)
        return Plan(inspection, mode, port, settings.gpu, command, services, tuple(warnings), tuple(ports), inferred_packages, kind, path, image, tuple(sorted(settings.service_images)), persistent_dirs=persistent_dirs, replicas=settings.replicas, cpus=settings.cpus, memory=settings.memory)


# ---------- Deterministic YAML and Dockerfile generation ----------

def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def yaml_dump(value: Any, depth: int = 0) -> str:
    pad = " " * depth
    lines: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if not re.fullmatch(r"[A-Za-z_][\w-]*", str(k)):
                raise OpsError(f"Unsafe YAML key: {k}")
            if isinstance(v, (dict, list)):
                lines.append(f"{pad}{k}:")
                lines.append(yaml_dump(v, depth + 2))
            else:
                lines.append(f"{pad}{k}: {_yaml_scalar(v)}")
    elif isinstance(value, list):
        for v in value:
            if isinstance(v, (dict, list)):
                lines.append(f"{pad}-")
                lines.append(yaml_dump(v, depth + 2))
            else:
                lines.append(f"{pad}- {_yaml_scalar(v)}")
    else:
        lines.append(f"{pad}{_yaml_scalar(value)}")
    return "\n".join(lines)


def _choose_port(preferred: int, occupied: set[int]) -> int:
    port = preferred
    while port in occupied:
        port += 1
        if port > 65535:
            raise OpsError("No free port mapping candidate.")
    occupied.add(port)
    return port


class Renderer:
    def files(self, plan: Plan) -> dict[str, str]:
        i = plan.inspection
        base = plan.python_image
        service_images = dict(plan.service_images)
        requirements = i.dependency
        if requirements and requirements.endswith(".txt"):
            # Local -r includes and editable installs must remain available for pip.
            setup = ("COPY . /build\n"
                     f"RUN /opt/venv/bin/python -m pip install --no-cache-dir -r {requirements}")
        elif requirements in ("pyproject.toml", "setup.py", "setup.cfg"):
            setup = ("COPY . /build\n"
                     "RUN /opt/venv/bin/python -m pip install --no-cache-dir .")
        elif requirements == "Pipfile":
            setup = ("COPY . /build\n"
                     "RUN /opt/venv/bin/python -m pip install --no-cache-dir pipenv && "
                     "/opt/venv/bin/pipenv requirements > /tmp/req.txt && "
                     "/opt/venv/bin/python -m pip install --no-cache-dir -r /tmp/req.txt")
        elif requirements in ("environment.yml", "environment.yaml"):
            setup = ("COPY . /build\n"
                     "RUN /opt/venv/bin/python -m pip install --no-cache-dir pyyaml && "
                     "/opt/venv/bin/python -c \"import yaml, subprocess, sys; data=yaml.safe_load(open('" + requirements + "')); "
                     "deps=[d for d in data.get('dependencies', []) if isinstance(d, str) and not d.startswith('python')]; "
                     "pip_deps=[p for d in data.get('dependencies', []) if isinstance(d, dict) for p in d.get('pip', [])]; "
                     "pkgs=deps+pip_deps; subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-cache-dir'] + pkgs, check=True) if pkgs else None\"")
        elif plan.inferred_packages:
            # The names are from a fixed import-to-distribution allowlist, never shell input.
            if not all(re.fullmatch(r"[a-zA-Z0-9_.-]+", pkg) for pkg in plan.inferred_packages):
                raise OpsError("Unsafe inferred dependency name.")
            setup = ("# Explicitly approved, unpinned inferred packages; use a real manifest for production.\n"
                     "RUN /opt/venv/bin/python -m pip install --no-cache-dir "
                     + " ".join(plan.inferred_packages))
        else:
            setup = "# No dependency manifest detected; no external Python packages installed."
        expose = f"EXPOSE {plan.port}\n" if plan.port else ""
        dockerfile = (
            f"# syntax=docker/dockerfile:1\n"
            f"ARG PYTHON_IMAGE={base}\n"
            "FROM ${PYTHON_IMAGE} AS builder\n"
            "ENV PIP_DISABLE_PIP_VERSION_CHECK=1\n"
            "WORKDIR /build\n"
            "RUN python -m venv /opt/venv\n"
            f"{setup}\n\n"
            "FROM ${PYTHON_IMAGE} AS runtime\n"
            "ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app PATH=/opt/venv/bin:$PATH\n"
            "WORKDIR /app\n"
            "RUN groupadd --system app && useradd --system --gid app --create-home --home-dir /home/app app && chown app:app /app\n"
            "ENV HOME=/home/app\n"
            "COPY --from=builder /opt/venv /opt/venv\n"
            "COPY --chown=app:app . /app\n"
            "USER app\n"
            f"{expose}"
            f"CMD {json.dumps(list(plan.command), ensure_ascii=False)}\n"
        )
        slug = re.sub(r"[^a-z0-9-]", "-", i.root.name.lower()).strip("-")[:24] or "project"
        fingerprint = hashlib.sha256(str(i.root).encode()).hexdigest()[:7]
        composition: dict[str, Any] = {"name": f"opsforge-{slug}-{fingerprint}", "services": {}}
        app: dict[str, Any] = {
            "build": {"context": "..", "dockerfile": ".opsforge/Dockerfile"},
            # Batch scripts may finish successfully; do not restart them endlessly.
            "restart": "unless-stopped" if plan.execution_kind == "web" else "no",
            "init": True,
            "security_opt": ["no-new-privileges:true"],
            "cap_drop": ["ALL"],
            "stop_grace_period": "30s",
            "environment": {"PYTHONUNBUFFERED": "1"},
        }
        if plan.persistent_dirs:
            app["volumes"] = [f"../{d}:/app/{d}" for d in plan.persistent_dirs]
        if plan.execution_kind == "interactive":
            # Compose run attaches foreground stdin/TTY; detached up is disabled
            # by the CLI for this application contract.
            app["stdin_open"] = True
            app["tty"] = True
        published = {p.service: p for p in plan.published_ports}
        if i.entry.framework == "gradio":
            app["environment"]["GRADIO_SERVER_NAME"] = "0.0.0.0"
            app["environment"]["GRADIO_SERVER_PORT"] = str(plan.port)
        if plan.port:
            mapping = published['app']
            if plan.replicas and plan.replicas > 1:
                end_host = mapping.host + plan.replicas - 1
                app["ports"] = [f"127.0.0.1:{mapping.host}-{end_host}:{mapping.container}"]
            else:
                app["ports"] = [f"127.0.0.1:{mapping.host}:{mapping.container}"]
            # Test TCP listener readiness using the Python already installed in
            # the application image. Do not incorrectly assume GET / returns 200.
            # This is a port-level signal, NOT application-specific readiness.
            if plan.execution_kind == "web":
                readiness = (f"import socket; s=socket.create_connection(('127.0.0.1', {plan.port}),timeout=2); s.close()"
                             if plan.health_path is None else
                             "import urllib.request; urllib.request.urlopen(" +
                             repr(f'http://127.0.0.1:{plan.port}{plan.health_path}') + ",timeout=3).read(128)")
                app["healthcheck"] = {
                    "test": ["CMD", "python", "-c", readiness],
                    "interval": "10s", "timeout": "4s", "retries": 12,
                    "start_period": "25s",
                }
        env = app["environment"]
        if "mlflow" in plan.services:
            env["MLFLOW_TRACKING_URI"] = "http://mlflow:5000"
        if "ollama" in plan.services:
            env["OLLAMA_BASE_URL"] = "http://ollama:11434"
        if "redis" in plan.services:
            env["REDIS_URL"] = "redis://redis:6379/0"
        if "prometheus" in plan.services:
            env["PROMETHEUS_MULTIPROC_DIR"] = "/tmp"

        deploy: dict[str, Any] = {}
        if plan.replicas and plan.replicas > 1:
            deploy["replicas"] = plan.replicas
        limits: dict[str, Any] = {}
        if plan.cpus:
            limits["cpus"] = str(plan.cpus)
        if plan.memory:
            limits["memory"] = str(plan.memory)
        if limits:
            deploy["resources"] = {"limits": limits}
        if deploy:
            app["deploy"] = deploy

        backend_deps = [s for s in plan.services if s in ("mlflow", "ollama", "redis")]
        if backend_deps:
            app["depends_on"] = {
                service: {"condition": "service_healthy"}
                for service in backend_deps
            }
        composition["services"]["app"] = app
        volumes: dict[str, None] = {}
        if "mlflow" in plan.services:
            mlport = published['mlflow'].host
            composition["services"]["mlflow"] = {
                "image": service_images.get('mlflow', 'ghcr.io/mlflow/mlflow:v3.7.0'),
                "restart": "unless-stopped",
                "command": ["mlflow", "server", "--host", "0.0.0.0", "--port", "5000",
                            "--backend-store-uri", "sqlite:////mlflow/mlflow.db",
                            "--artifacts-destination", "/mlflow/artifacts",
                            "--allowed-hosts", "mlflow,mlflow:5000,localhost,127.0.0.1,host.docker.internal"],
                "ports": [f"127.0.0.1:{mlport}:5000"],
                "volumes": ["mlflow-data:/mlflow"],
                # The published MLflow image contains Python; no curl installation required.
                "healthcheck": {
                    "test": ["CMD", "python", "-c",
                             "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=3).read()"],
                    "interval": "10s", "timeout": "5s", "retries": 12,
                    "start_period": "40s",
                },
            }
            volumes["mlflow-data"] = None
        if "ollama" in plan.services:
            ollama_port = published['ollama'].host
            ollama: dict[str, Any] = {
                "image": service_images.get('ollama', 'ollama/ollama:latest'),
                "restart": "unless-stopped",
                "ports": [f"127.0.0.1:{ollama_port}:11434"],
                "volumes": ["ollama-data:/root/.ollama"],
                "healthcheck": {
                    "test": ["CMD", "ollama", "list"],
                    "interval": "10s", "timeout": "5s", "retries": 12,
                    "start_period": "25s",
                },
            }
            if plan.gpu:
                ollama["deploy"] = {"resources": {"reservations": {"devices": [
                    {"driver": "nvidia", "count": 1, "capabilities": ["gpu"]}
                ]}}}
            composition["services"]["ollama"] = ollama
            volumes["ollama-data"] = None
        if "redis" in plan.services:
            composition["services"]["redis"] = {
                "image": service_images.get('redis', 'redis:7.4-alpine'),
                "restart": "unless-stopped",
                "command": ["redis-server", "--appendonly", "yes"],
                "volumes": ["redis-data:/data"],
                "healthcheck": {
                    "test": ["CMD", "redis-cli", "ping"],
                    "interval": "5s", "timeout": "3s", "retries": 10,
                    "start_period": "10s",
                },
            }
            volumes["redis-data"] = None
        if "prometheus" in plan.services:
            prom_port = published['prometheus'].host
            prom_svc: dict[str, Any] = {
                "image": service_images.get('prometheus', 'prom/prometheus:v3.0.1'),
                "restart": "unless-stopped",
                "command": [
                    "--config.file=/etc/prometheus/prometheus.yml",
                    "--storage.tsdb.path=/prometheus",
                    "--web.console.libraries=/usr/share/prometheus/console_libraries",
                    "--web.console.templates=/usr/share/prometheus/consoles",
                ],
                "ports": [f"127.0.0.1:{prom_port}:9090"],
                "volumes": [
                    "./prometheus.yml:/etc/prometheus/prometheus.yml:ro",
                    "prometheus-data:/prometheus",
                ],
            }
            if plan.execution_kind == "web":
                prom_svc["depends_on"] = {
                    "app": {"condition": "service_started"},
                }
            composition["services"]["prometheus"] = prom_svc
            volumes["prometheus-data"] = None
        graf_password = os.environ.get("GF_SECURITY_ADMIN_PASSWORD") or secrets.token_urlsafe(16)
        if "grafana" in plan.services:
            grafana_port = published['grafana'].host
            graf_svc: dict[str, Any] = {
                "image": service_images.get('grafana', 'grafana/grafana:11.4.0'),
                "restart": "unless-stopped",
                "environment": {
                    "GF_SECURITY_ADMIN_PASSWORD": graf_password,
                    "GF_USERS_ALLOW_SIGN_UP": "false",
                },
                "ports": [f"127.0.0.1:{grafana_port}:3000"],
                "volumes": ["grafana-data:/var/lib/grafana"],
            }
            if "prometheus" in plan.services:
                graf_svc["depends_on"] = {
                    "prometheus": {"condition": "service_started"},
                }
            elif plan.execution_kind == "web":
                graf_svc["depends_on"] = {
                    "app": {"condition": "service_started"},
                }
            composition["services"]["grafana"] = graf_svc
            volumes["grafana-data"] = None
        if volumes:
            composition["volumes"] = {name: None for name in volumes}
        # Dockerfile-specific ignore is supported next to .opsforge/Dockerfile.
        ignore = """# Scoped to .opsforge/Dockerfile (build context is the project root)
.git
.hg
.svn
**/.git
**/__pycache__
**/*.pyc
**/.venv
**/venv
**/node_modules
**/.pytest_cache
**/.mypy_cache
**/.ruff_cache
**/.env
**/.env.*
.env
.env.*
**/*.pem
**/*.key
**/id_rsa
**/.ssh
**/.aws
**/.npmrc
**/.pypirc
**/.netrc
**/.git-credentials
**/credentials.json
**/secrets.*
**/*.p12
**/*.pfx
# Large ML/LLM weights stay on the host; mount data explicitly if needed.
**/*.gguf
**/*.safetensors
**/*.pt
**/*.pth
**/*.ckpt
**/*.onnx
**/*.bin
**/*.h5
**/*.joblib
**/*.pkl
**/id_ed25519
**/*secret*.yaml
**/*secret*.json
.opsforge
.opsforge-*
.opsforge.lock
.opsforge.lock.guard
opsforge.py
opsforge_*.py
auto_docker.py
auto_docker_*.py
"""
        # A consciously selected entrypoint must not disappear from the build context.
        if i.entry.path in {"opsforge.py", "auto_docker.py"} or i.entry.path.startswith(("opsforge_", "auto_docker_")):
            ignore += "!" + i.entry.path + "\n"
        notes = (
            "Auto Docker 3.0 RC3 — local Docker environment\n"
            f"Entrypoint: {i.entry.path}\nMode: {plan.mode.value}\n"
            f"Framework: {i.entry.framework}\nExecution: {plan.execution_kind}\n"
            f"App command: {json.dumps(list(plan.command))}\n"
            f"Health endpoint: {plan.health_path or '(TCP only)'}\n"
            f"Base image: {plan.python_image}\n\n"
            "From the project directory:\n"
            "  docker compose -f .opsforge/compose.yaml config -q\n"
            "  docker compose -f .opsforge/compose.yaml build\n"
            "  # Select the execution command appropriate for this project below.\n"
            "  docker compose -f .opsforge/compose.yaml ps --all\n"
            "  docker compose -f .opsforge/compose.yaml down\n\n"
            "Auto Docker keeps .opsforge/ output for compatibility with earlier releases.\n"
            "The generator does not install Docker, download LLM models,\n"
            "modify application code, or make the stack production-ready.\n"
            "Only web applications restart indefinitely. Batch jobs should use\n"
            "'job YES'; interactive CLI apps require foreground 'run YES' with a TTY.\n"
            "Environment variables are hints: your app must explicitly use them.\n"
            "Credentials should come from runtime secrets, never baked into images.\n"
            "Large model files (.gguf, .safetensors, .pt, .pth, .ckpt, .onnx)\n"
            "are excluded from image builds to protect against huge contexts.\n"
            "Use an explicit runtime mount or customize the ignore file if needed.\n"
            "Web-app health checks prove that the local TCP port accepts connections,\n"
            "not that an application endpoint or LLM model is ready.\n"
            "Adjust image versions and Python dependencies to suit your application.\n"
            "Optional 'deps infer' installs recognized imports without version pins,\n"
            "and must be reviewed; use requirements.txt or pyproject.toml for reliable builds.\n"
        )
        if plan.published_ports:
            notes += "\nPublished on 127.0.0.1 only:\n"
            for mapping in plan.published_ports:
                notes += f"  {mapping.service}: host {mapping.host} -> container {mapping.container}\n"
            notes += "Run 'ports' in Auto Docker to check for possible host conflicts.\n"
        if plan.execution_kind == 'interactive':
            notes += "\nInteractive CLI: from a host terminal use Auto Docker 'run YES' after build.\n"
            notes += "Or: docker compose -f .opsforge/compose.yaml run --rm app\n"
            notes += "Do not use detached up for interactive chat applications.\n"
        elif plan.execution_kind == 'batch':
            notes += "\nBatch job: use Auto Docker 'job YES' after build.\n"
            notes += "Or: docker compose -f .opsforge/compose.yaml run --rm -T app\n"
            notes += "The process exit code determines job success, not container health.\n"
        else:
            notes += "\nWeb application: docker compose -f .opsforge/compose.yaml up -d\n"
            notes += "Inspect: docker compose -f .opsforge/compose.yaml logs --tail=100\n"
            notes += "Then use Auto Docker 'verify' and test a real HTTP endpoint.\n"
        if "ollama" in plan.services:
            notes += "\nPull a model explicitly, e.g.:\n  docker compose -f .opsforge/compose.yaml exec ollama ollama pull llama3.2\n"
        if "prometheus" in plan.services:
            prom_host = published['prometheus'].host if 'prometheus' in published else 9090
            if plan.execution_kind == "web" and plan.port is not None:
                notes += f"\nObservability (Prometheus): scrapes http://app:{plan.port}/metrics and http://localhost:9090/metrics every 15s.\n"
            else:
                notes += f"\nObservability (Prometheus): scrapes http://localhost:9090/metrics every 15s (application {plan.execution_kind or 'non-web'} metrics endpoint not configured).\n"
            notes += f"Prometheus UI: http://127.0.0.1:{prom_host}\n"
        if "grafana" in plan.services:
            graf_host = published['grafana'].host if 'grafana' in published else 3000
            notes += f"\nObservability (Grafana): UI at http://127.0.0.1:{graf_host} (admin/{graf_password})\n"
        if plan.inferred_packages:
            notes += "\nExplicitly approved inferred Python packages (unpinned): " + ", ".join(plan.inferred_packages) + "\n"
        if plan.warnings:
            notes += "\nWarnings:\n" + "".join(f"- {w}\n" for w in plan.warnings)
        data = {
            "Dockerfile": dockerfile,
            "Dockerfile.dockerignore": ignore,
            "compose.yaml": "# Generated by Auto Docker 3.0 RC3; localhost-only published ports.\n" + yaml_dump(composition) + "\n",
            "INSTRUCTIONS.txt": notes,
        }
        if "prometheus" in plan.services:
            scrape_configs = [
                '  - job_name: "prometheus"\n'
                '    static_configs:\n'
                '      - targets: ["localhost:9090"]\n'
            ]
            if plan.execution_kind == "web" and plan.port is not None:
                scrape_configs.append(
                    '  - job_name: "app"\n'
                    '    metrics_path: "/metrics"\n'
                    '    static_configs:\n'
                    f'      - targets: ["app:{plan.port}"]\n'
                )
            data["prometheus.yml"] = (
                "global:\n"
                "  scrape_interval: 15s\n"
                "  evaluation_interval: 15s\n\n"
                "scrape_configs:\n" + "".join(scrape_configs)
            )
        hashes = {name: hashlib.sha256(content.encode()).hexdigest() for name, content in sorted(data.items())}
        data["manifest.json"] = json.dumps({"generator": "Auto Docker", "schema": 1,
                                               "version": VERSION, "files": hashes,
            "runtime": {"services": ["app", *plan.services],
                        "batch": plan.execution_kind == "batch",
                        "execution_kind": plan.execution_kind,
                        "ports": {p.service: p.host for p in plan.published_ports},
                        "port_mappings": [dataclasses.asdict(p) for p in plan.published_ports],
                        "inferred_packages": list(plan.inferred_packages),
                        "app_command": list(plan.command),
                        "health_path": plan.health_path,
                        "python_image": plan.python_image,
                        "service_images": dict(plan.service_images),
                        "replicas": plan.replicas,
                        "cpus": plan.cpus,
                        "memory": plan.memory}},
                                           indent=2, sort_keys=True) + "\n"
        return data


# ---------- Transactional filesystem and Docker execution ----------

class ProjectLock:
    """Cross-process, process-aware locking with conservative stale recovery.

    The persistent guard file is kernel-locked for the full write transaction.
    It serializes stale-lock recovery with new generators. Never remove a lock
    if process identity or ownership cannot be verified. On POSIX a flock is
    used; on Windows, msvcrt locks one byte. Neither requires pip packages.
    """

    def __init__(self, root: Path):
        self.path = root / ".opsforge.lock"
        self.guard_path = root / ".opsforge.lock.guard"
        self.owner = False
        self.token: str | None = None
        self._guard_context: Any = None
        self._legacy_fd: int | None = None  # OpsForge v1.x persistent regular-file lock

    @contextmanager
    def _exclusive_guard(self) -> Iterator[None]:
        if self.guard_path.is_symlink():
            raise OpsError(f"Refusing symlinked lock guard: {self.guard_path}")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.guard_path, flags, 0o600)
        except OSError as exc:
            raise OpsError(f"Cannot open Auto Docker project lock guard: {exc}") from exc
        locked = False
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OpsError("Refusing non-regular Auto Docker guard file.")
            if os.name == "posix":
                import fcntl
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise OpsError("An Auto Docker generation/recovery operation is still running. Wait for it to finish.") from exc
                locked = True
            elif os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise OpsError("An Auto Docker generation/recovery operation is still running. Wait for it to finish.") from exc
                locked = True
            else:
                raise OpsError("Safe cross-process lock guard is unsupported on this platform.")
            yield
        finally:
            try:
                if locked:
                    if os.name == "posix":
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    else:
                        import msvcrt
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            finally:
                os.close(fd)

    def _acquire_legacy_file(self) -> None:
        """Honor the permanent file locks written by OpsForge v1.x.

        Old releases intentionally LEFT .opsforge.lock as a regular file and
        used kernel flock on that same inode. Do NOT unlink or convert it to
        a directory: either action would permit incompatible concurrent writers.
        """
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags)
        except OSError as exc:
            raise OpsError(f"Cannot open legacy project lock file safely: {exc}") from exc
        acquired = False
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OpsError("Legacy lock path is not an ordinary regular file.")
            current = self.path.lstat()
            if not stat.S_ISREG(current.st_mode) or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise OpsError("Legacy lock path was replaced during inspection; refusing generation.")
            # Old versions wrote no content. The Windows backend may leave a '0'.
            if info.st_size > 1 or (info.st_size == 1 and os.read(fd, 1) != b"0"):
                raise OpsError("Non-empty, unfamiliar legacy lock file; manual inspection required.")
            if os.name == "posix":
                import fcntl
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (OSError, BlockingIOError) as exc:
                    raise OpsError("Legacy OpsForge process is still holding the project lock. Stop that process first.") from exc
            elif os.name == "nt":
                import msvcrt
                if info.st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise OpsError("Legacy OpsForge process still holds the project lock.") from exc
            else:
                raise OpsError("Legacy file locking is unsupported on this operating system.")
            acquired = True
            self._legacy_fd = fd
        finally:
            if not acquired:
                os.close(fd)

    def _release_legacy_file(self) -> None:
        fd = self._legacy_fd
        if fd is None:
            return
        self._legacy_fd = None
        try:
            if os.name == "posix":
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)

    def __enter__(self) -> "ProjectLock":
        self._guard_context = self._exclusive_guard()
        self._guard_context.__enter__()
        try:
            if self.path.is_symlink():
                raise OpsError("Refusing symlinked lock path.")
            if self.path.is_file():
                self._acquire_legacy_file()
                LOG.info("Acquired legacy persistent file lock: %s", self.path)
                print("Auto Docker: using compatible legacy file lock (retained, not deleted).")
                return self
            if self.path.exists():
                try:
                    reason = self.stale_reason()
                except OpsError as exc:
                    raise OpsError(f"Project lock is still present: {self.path}. {exc} "
                                   "Run 'lock' to inspect its owner; a live or unverifiable lock will not be removed.") from exc
                shutil.rmtree(self.path)
                LOG.warning("Recovered verified stale project lock: %s (%s)", self.path, reason)
                print(f"Auto Docker: recovered stale lock ({reason}).")
            try:
                self.path.mkdir(mode=0o700)
            except FileExistsError as exc:
                raise OpsError(f"Another generation has claimed the project lock: {self.path}") from exc
            self.owner = True
            self.token = secrets.token_hex(12)
            owner = {
                "pid": os.getpid(), "host": socket.gethostname(),
                "token": self.token, "version": VERSION,
                "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            proc_start = self._process_start(os.getpid())
            if proc_start:
                owner["proc_start"] = proc_start
            (self.path / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
            return self
        except BaseException:
            if self.owner:
                shutil.rmtree(self.path)
                self.owner = False
            self._release_legacy_file()
            self._guard_context.__exit__(None, None, None)
            self._guard_context = None
            raise

    def __exit__(self, *_: Any) -> None:
        try:
            if self.owner:
                # A manually replaced lock is not ours. Do not delete it.
                owner_file = self.path / "owner.json"
                try:
                    current = json.loads(owner_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    current = {}
                if (not self.path.is_symlink() and not owner_file.is_symlink()
                        and isinstance(current, dict) and current.get("token") == self.token):
                    shutil.rmtree(self.path)
                else:
                    LOG.error("Lock ownership changed; did not remove %s", self.path)
                self.owner = False
        finally:
            try:
                self._release_legacy_file()
            finally:
                if self._guard_context is not None:
                    self._guard_context.__exit__(None, None, None)
                    self._guard_context = None

    @staticmethod
    def _pid_alive(pid: int) -> bool | None:
        if os.name != "posix":
            # Windows has no portable stdlib PID-start probe; conservative.
            return None
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return None

    @staticmethod
    def _process_start(pid: int) -> str | None:
        """Linux boot ID + process start ticks avoids PID-reuse false positives."""
        if not sys.platform.startswith("linux"):
            return None
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            ticks = raw.rsplit(")", 1)[1].split()[19]  # /proc field 22
            boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
            return f"{boot}:{ticks}" if boot and ticks.isdigit() else None
        except (OSError, IndexError, ValueError):
            return None

    def stale_reason(self) -> str:
        """Return a verified safe stale explanation or raise for active/unknown."""
        if self.path.is_file() and not self.path.is_symlink():
            raise OpsError("Legacy persistent file lock does not need unlock; run generate YES. Do not delete it.")
        if self.path.is_symlink() or not self.path.is_dir():
            raise OpsError("No ordinary Auto Docker lock directory found.")
        owner_file = self.path / "owner.json"
        if owner_file.is_symlink() or not owner_file.is_file():
            raise OpsError("Lock ownership file is missing or unsafe; manual inspection required.")
        try:
            owner = json.loads(owner_file.read_text(encoding="utf-8"))
            pid, host = owner.get("pid"), owner.get("host")
        except (OSError, ValueError, AttributeError) as exc:
            raise OpsError("Lock owner cannot be verified; inspect it manually.") from exc
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or not isinstance(host, str) or not host:
            raise OpsError("Lock has no verifiable process/host information (possibly from an older version).")
        if host != socket.gethostname():
            raise OpsError(f"Lock was created on another host ({host}); refuse automatic cleanup.")
        alive = self._pid_alive(pid)
        if alive is False:
            return f"Owner process {pid} on {host} has exited"
        if alive is True:
            recorded = owner.get("proc_start")
            actual = self._process_start(pid)
            if isinstance(recorded, str) and actual and actual != recorded:
                return f"Owner process {pid} was replaced (PID reused)"
            raise OpsError(f"Lock owner process {pid} is still running; cannot unlock.")
        raise OpsError("Cannot safely prove this process exited on this operating system.")

    def status(self) -> str:
        """Read-only explanation intended for the `lock` command."""
        if self.path.is_symlink():
            return f"UNSAFE: lock path is a symlink: {self.path}"
        if not self.path.exists():
            return "No active lock directory. Generation is not blocked by an owner lock."
        if self.path.is_file():
            if self._legacy_fd is not None:
                return "LEGACY FILE: currently held by this Auto Docker instance."
            if os.name not in ("posix", "nt"):
                return "LEGACY FILE: cannot verify kernel-lock availability on this platform."
            try:
                # Probe without unlinking the permanent lock file.
                self._acquire_legacy_file()
            except OpsError as exc:
                return f"LEGACY FILE: unavailable or unsafe ({exc})"
            else:
                return ("LEGACY FILE: kernel lock is available. This is the persistent "
                        "OpsForge v1.x format; generate normally, do not delete this file.")
            finally:
                self._release_legacy_file()
        if not self.path.is_dir():
            return f"UNVERIFIABLE: unexpected lock path type: {self.path}"
        try:
            owner = json.loads((self.path / "owner.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            owner = {}
        if not isinstance(owner, dict):
            owner = {}
        details = (f"Lock: {self.path}\nOwner PID: {owner.get('pid', 'unknown')}\n"
                   f"Owner host: {owner.get('host', 'unknown')}\n"
                   f"Created: {owner.get('created_utc', 'unknown')}")
        try:
            reason = self.stale_reason()
            return details + f"\nStatus: VERIFIED STALE ({reason}); use unlock YES, or retry generate YES for automatic recovery."
        except OpsError as exc:
            return details + f"\nStatus: NOT SAFE TO REMOVE ({exc})"

    def clear_stale(self) -> str:
        if self.path.is_file() and not self.path.is_symlink():
            raise OpsError("Legacy regular-file lock is intentionally persistent; no unlock needed. Run generate YES instead.")
        with self._exclusive_guard():
            reason = self.stale_reason()
            shutil.rmtree(self.path)
            return reason


class OutputManager:
    def __init__(self, root: Path):
        self.root = root
        self.folder = root / OUTPUT

    def state(self, files: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
        self._verify_owned()
        created, changed, same = [], [], []
        for name, text in files.items():
            dest = self.folder / name
            if dest.exists():
                if not dest.is_file() or dest.is_symlink():
                    raise OpsError(f"Refusing non-regular output file: {dest}")
                # A manually edited output might contain binary/invalid UTF-8.
                # Treat it as changed, not as a console-crashing decode error.
                current = dest.read_bytes()
                (same if current == text.encode("utf-8") else changed).append(name)
            else:
                created.append(name)
        return created, changed, same

    @staticmethod
    def _verify_owned_path(folder: Path) -> None:
        if folder.is_symlink():
            raise OpsError(f"Refusing unsafe symlinked output path: {folder}")
        if not folder.exists():
            return
        if not folder.is_dir():
            raise OpsError(f"Refusing unsafe output path: {folder}")
        manifest = folder / "manifest.json"
        if manifest.is_symlink():
            raise OpsError("Refusing symlinked output manifest.")
        try:
            parsed = json.loads(manifest.read_text(encoding="utf-8"))
            if (not isinstance(parsed, dict) or parsed.get("generator") not in {"OpsForge", "Auto Docker"}
                    or parsed.get("schema") != 1):
                raise ValueError("Unrecognized manifest")
        except (OSError, ValueError, TypeError) as exc:
            raise OpsError(f"Existing {folder}/ is not a recognized Auto Docker/legacy output; not overwriting it.") from exc

    def _verify_owned(self) -> None:
        self._verify_owned_path(self.folder)

    def write(self, files: dict[str, str]) -> Path | None:
        if not all(re.fullmatch(r"[\w.-]+", name) and name not in {".", ".."} for name in files):
            raise OpsError("Invalid generated filename.")
        with ProjectLock(self.root):
            self._verify_owned()
            stage = Path(tempfile.mkdtemp(prefix=".opsforge-stage-", dir=self.root))
            backup: Path | None = None
            try:
                for name, body in files.items():
                    dest = stage / name
                    dest.write_text(body, encoding="utf-8", newline="\n")
                    dest.chmod(0o600 if name == "manifest.json" else 0o644)
                if self.folder.exists():
                    # Keep a real recovery copy instead of destroying user edits.
                    while True:
                        suffix = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                        backup = self.root / f".opsforge-backup-{suffix}"
                        if not backup.exists():
                            break
                    self.folder.rename(backup)
                try:
                    stage.rename(self.folder)
                except BaseException:
                    if backup is not None and backup.exists() and not self.folder.exists():
                        backup.rename(self.folder)
                    raise
                LOG.info("generated %d files in isolated output directory", len(files))
                return backup
            finally:
                if stage.exists():
                    shutil.rmtree(stage)

    @classmethod
    def validate_folder(cls, folder: Path) -> tuple[bool, list[str]]:
        cls._verify_owned_path(folder)
        if not folder.exists():
            return False, ["No generated files found. Run generate first."]
        try:
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            expected_files = manifest.get("files")
        except (OSError, ValueError, TypeError) as exc:
            return False, [f"Cannot read output manifest: {exc}"]
        if not isinstance(expected_files, dict):
            return False, ["Invalid manifest: files must be a dictionary."]
        known = {"Dockerfile", "Dockerfile.dockerignore", "compose.yaml", "INSTRUCTIONS.txt"}
        if "prometheus.yml" in expected_files:
            known = known | {"prometheus.yml"}
        errors: list[str] = []
        runtime = manifest.get('runtime')
        if runtime is not None:
            if (not isinstance(runtime, dict) or not isinstance(runtime.get('services'), list)
                    or not all(isinstance(x, str) and x in ('app', *SERVICE_ORDER) for x in runtime['services'])
                    or not isinstance(runtime.get('batch'), bool)
                    or runtime.get('execution_kind', 'batch' if runtime.get('batch') else 'web') not in EXECUTION_KINDS[1:]
                    or not isinstance(runtime.get('ports'), dict)
                    or not all(isinstance(n, str) and n in ('app', *SERVICE_ORDER)
                               and isinstance(v, int) and 1024 <= v <= 65535
                               for n, v in runtime['ports'].items())
                    or not isinstance(runtime.get('port_mappings', []), list)
                    or not isinstance(runtime.get('inferred_packages', []), list)
                    or not all(isinstance(pkg, str) and re.fullmatch(r'[a-zA-Z0-9_.-]+', pkg)
                               for pkg in runtime.get('inferred_packages', []))
                    or not all(isinstance(p, dict) and set(p) == {'service', 'host', 'container'}
                               and p['service'] in ('app', *SERVICE_ORDER)
                               and all(isinstance(p[k], int) and 1024 <= p[k] <= 65535
                                       for k in ('host', 'container'))
                               for p in runtime.get('port_mappings', []))):
                errors.append('Invalid runtime metadata in manifest.')
        if set(expected_files) != known:
            errors.append("Unexpected file set in output manifest.")
        for name, expected in expected_files.items():
            if not isinstance(name, str) or not re.fullmatch(r"[\w.-]+", name) or name in (".", ".."):
                errors.append("Invalid filename in manifest.")
                continue
            if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
                errors.append(f"Invalid SHA256 in manifest for {name}")
                continue
            path = folder / name
            if path.is_symlink() or not path.is_file():
                errors.append(f"Missing/unsafe generated file: {name}")
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != expected:
                errors.append(f"Generated file was changed: {name}")
        return not errors, errors

    def validate(self) -> tuple[bool, list[str]]:
        return self.validate_folder(self.folder)

    def execution_kind(self) -> str:
        """Get the *saved* application contract, not pending console settings."""
        valid, errors = self.validate()
        if not valid:
            raise OpsError("Generated configuration is invalid: " + "; ".join(errors))
        try:
            runtime = json.loads((self.folder / 'manifest.json').read_text(encoding='utf-8'))['runtime']
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise OpsError("Generated runtime metadata is missing; regenerate before executing.") from exc
        return runtime.get('execution_kind', 'batch' if runtime.get('batch') else 'web')

    def runtime_ports(self) -> tuple[PublishedPort, ...]:
        """Read the effective generated ports, not possibly stale console settings."""
        valid, errors = self.validate()
        if not valid:
            raise OpsError('Cannot inspect ports of changed/missing output: ' + '; '.join(errors))
        runtime = json.loads((self.folder / 'manifest.json').read_text(encoding='utf-8')).get('runtime')
        if not isinstance(runtime, dict) or 'port_mappings' not in runtime:
            raise OpsError('Old output has no port mappings; regenerate with this Auto Docker version.')
        return tuple(PublishedPort(**p) for p in runtime['port_mappings'])

    def restore_last_backup(self) -> Path:
        """Recover the latest intact backup only if the live output is absent."""
        with ProjectLock(self.root):
            if self.folder.is_symlink() or self.folder.exists():
                raise OpsError("Current .opsforge/ exists; recovery will not overwrite it.")
            backups = sorted(self.root.glob(".opsforge-backup-*"), reverse=True)
            for old in backups:
                if old.is_symlink() or not old.is_dir():
                    continue
                try:
                    valid, _ = self.validate_folder(old)
                except OpsError:
                    continue
                if valid:
                    old.rename(self.folder)
                    LOG.warning("Recovered previous output from %s", old)
                    return old
            raise OpsError("No intact Auto Docker backup found. No files were changed.")


# ---------- Existing infrastructure: inspect and operate without regeneration ----------

@dataclasses.dataclass(frozen=True)
class ExistingStack:
    """Read-only inventory of a project's own Docker/Compose resources."""

    root: Path
    compose_files: tuple[str, ...]
    dockerfiles: tuple[str, ...]
    services: tuple[str, ...]
    selected_compose: str | None
    has_env_sample: bool
    has_env: bool
    has_helm: bool
    has_uv_lock: bool
    has_pyproject: bool
    has_frontend: bool
    selected_compose_files: tuple[str, ...] = ()
    discovered_services: tuple[DiscoveredService, ...] = ()


class ExistingInfrastructure:
    """Adopt an existing Compose project instead of treating it as a single .py.

    This is an inventory, NOT a YAML parser: service labels are best effort.
    Only Docker Compose itself can validate resolved YAML/anchors/extensions.
    """

    COMPOSE_NAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
    OVERRIDE_NAMES = ("compose.override.yaml", "compose.override.yml", "docker-compose.override.yaml", "docker-compose.override.yml")
    ALL_COMPOSE_NAMES = COMPOSE_NAMES + OVERRIDE_NAMES
    MAX_COMPOSE_SIZE = 2 * 1024 * 1024

    @classmethod
    def _safe_file(cls, root: Path, relative: str) -> Path:
        rel_clean = relative.strip().replace('\\', '/')
        if rel_clean not in cls.ALL_COMPOSE_NAMES and not (rel_clean.endswith(('.yaml', '.yml')) and '/' not in rel_clean):
            raise OpsError("Choose a root Compose file: " + ", ".join(cls.COMPOSE_NAMES))
        path = root / rel_clean
        if path.is_symlink() or not path.is_file():
            raise OpsError(f"Compose file is missing or is a symlink: {relative}")
        if path.stat().st_size > cls.MAX_COMPOSE_SIZE:
            raise OpsError(f"Compose file exceeds size limit ({cls.MAX_COMPOSE_SIZE} bytes)")
        return path

    @staticmethod
    def _service_labels(data: str) -> tuple[str, ...]:
        """Lightweight, non-executing hint; the Compose CLI is authoritative."""
        inside = False
        result: list[str] = []
        top = re.compile(r"^([A-Za-z][A-Za-z0-9_.-]*):(?:\s*(?:#.*)?)?$")
        service = re.compile(r"^  ([A-Za-z0-9_.-]+):(?:\s*(?:#.*)?)?$")
        for line in data.splitlines():
            if not inside:
                if re.match(r"^services:\s*(?:#.*)?$", line):
                    inside = True
                continue
            if top.match(line) and line.strip() and not line.startswith(' '):
                break
            match = service.match(line)
            if match:
                result.append(match.group(1))
        return tuple(dict.fromkeys(result))

    @classmethod
    def discover(cls, root: Path, choice: str | None = None) -> ExistingStack:
        root = root.resolve(strict=True)
        base_names = tuple(name for name in cls.COMPOSE_NAMES
                           if (root / name).is_file() and not (root / name).is_symlink())
        all_names = tuple(name for name in cls.ALL_COMPOSE_NAMES
                          if (root / name).is_file() and not (root / name).is_symlink())
        
        selected_files: list[str] = []
        chosen: str | None = None

        if choice:
            raw_choices = [c.strip() for c in re.split(r'[, ]+', choice) if c.strip()]
            for item in raw_choices:
                if item not in all_names and not (root / item).is_file():
                    raise OpsError("Compose file not present: " + item + ". Found: " + (", ".join(all_names) or "none"))
                selected_files.append(item)
            chosen = selected_files[0] if selected_files else None
        else:
            if len(base_names) == 1:
                base = base_names[0]
                selected_files.append(base)
                chosen = base
                # Check for standard override pair (compose.override.yaml or docker-compose.override.yml)
                stem = base.rsplit('.', 1)[0]
                ext = base.rsplit('.', 1)[1]
                override_candidate = f"{stem}.override.{ext}"
                if (root / override_candidate).is_file() and not (root / override_candidate).is_symlink():
                    selected_files.append(override_candidate)
            elif len(base_names) > 1:
                chosen = None
            else:
                chosen = None

        services_dict: dict[str, dict[str, Any]] = {}
        for fname in selected_files:
            path = cls._safe_file(root, fname)
            try:
                data = path.read_text(encoding='utf-8-sig')
            except (OSError, UnicodeError) as exc:
                raise OpsError(f"Cannot read {fname}: {exc}") from exc
            parsed = parse_compose_services_static(data)
            for sname, sdata in parsed.items():
                if sname not in services_dict:
                    services_dict[sname] = sdata
                else:
                    # Merge override properties
                    existing = services_dict[sname]
                    if sdata['image']:
                        existing['image'] = sdata['image']
                    if sdata['build']:
                        existing['build'] = sdata['build']
                    if sdata['dockerfile']:
                        existing['dockerfile'] = sdata['dockerfile']
                    if sdata.get('command'):
                        existing['command'] = sdata['command']
                    if sdata.get('entrypoint'):
                        existing['entrypoint'] = sdata['entrypoint']
                    if sdata['restart']:
                        existing['restart'] = sdata['restart']
                    if sdata['privileged']:
                        existing['privileged'] = sdata['privileged']
                    if sdata['network_mode']:
                        existing['network_mode'] = sdata['network_mode']
                    existing['ports'].extend(p for p in sdata['ports'] if p not in existing['ports'])
                    existing['volumes'].extend(v for v in sdata['volumes'] if v not in existing['volumes'])
                    existing['depends_on'].extend(d for d in sdata['depends_on'] if d not in existing['depends_on'])
                    existing['env_file'].extend(e for e in sdata['env_file'] if e not in existing['env_file'])
                    existing['environment'].extend(ev for ev in sdata['environment'] if ev not in existing['environment'])

        discovered_services_list: list[DiscoveredService] = []
        for sname, sdata in services_dict.items():
            role = classify_service_role(sname, sdata['image'], sdata['build'], sdata['restart'])
            discovered_services_list.append(DiscoveredService(
                name=sname,
                role=role,
                image=sdata['image'],
                build_context=sdata['build'],
                dockerfile=sdata['dockerfile'],
                ports=tuple(sdata['ports']),
                volumes=tuple(sdata['volumes']),
                depends_on=tuple(sdata['depends_on']),
                environment_keys=tuple(sdata['environment']),
                env_files=tuple(sdata['env_file']),
                profiles=tuple(sdata['profiles']),
                restart=sdata['restart'],
                privileged=sdata['privileged'],
                network_mode=sdata['network_mode'],
                command=sdata.get('command'),
                entrypoint=sdata.get('entrypoint'),
            ))

        service_names = tuple(s.name for s in discovered_services_list)
        dockerfiles = tuple(name for name in ('Dockerfile', 'frontend/Dockerfile',
                                               'backend/Dockerfile', 'docker/Dockerfile')
                            if (root / name).is_file() and not (root / name).is_symlink())

        return ExistingStack(
            root=root,
            compose_files=base_names if base_names else all_names,
            dockerfiles=dockerfiles,
            services=service_names,
            selected_compose=chosen,
            has_env_sample=(root / '.env.example').is_file() or (root / '.env.sample').is_file(),
            has_env=(root / '.env').is_file(),
            has_helm=(root / 'helm').is_dir() or (root / 'Chart.yaml').is_file(),
            has_uv_lock=(root / 'uv.lock').is_file(),
            has_pyproject=(root / 'pyproject.toml').is_file() or any((root / p).is_file() for p in ('backend/pyproject.toml', 'src/pyproject.toml', 'api/pyproject.toml')),
            has_frontend=(root / 'frontend').is_dir() or (root / 'package.json').is_file() or (root / 'frontend' / 'package.json').is_file(),
            selected_compose_files=tuple(selected_files),
            discovered_services=tuple(discovered_services_list),
        )

    @classmethod
    def plan(cls, root: Path, choice: str | None = None, docker_context: str | None = None) -> DeploymentPlan:
        stack = cls.discover(root, choice)
        readiness = NativeReadiness.inspect(root, choice, disk_check=True, docker_context=docker_context)

        # Build dependency graph
        dep_graph: dict[str, tuple[str, ...]] = {}
        for s in stack.discovered_services:
            dep_graph[s.name] = s.depends_on

        # Topological ordering
        topological_order_list, cycle_errors = topological_sort_services({s.name: list(s.depends_on) for s in stack.discovered_services})

        # Group services into startup phases
        phase1: list[str] = []  # Storage / Databases
        phase2: list[str] = []  # Migrations / Initialization
        phase3: list[str] = []  # Core Application & AI Services
        phase4: list[str] = []  # Frontend & Administration

        order_index = {name: i for i, name in enumerate(topological_order_list)}

        for s in stack.discovered_services:
            if s.role == 'database':
                phase1.append(s.name)
            elif s.role == 'migration':
                phase2.append(s.name)
            elif s.role in ('backend', 'worker', 'ai', 'service'):
                phase3.append(s.name)
            elif s.role in ('frontend', 'admin'):
                phase4.append(s.name)
            else:
                phase3.append(s.name)

        phase1.sort(key=lambda x: order_index.get(x, 0))
        phase2.sort(key=lambda x: order_index.get(x, 0))
        phase3.sort(key=lambda x: order_index.get(x, 0))
        phase4.sort(key=lambda x: order_index.get(x, 0))

        phases_list: list[tuple[int, str, tuple[str, ...]]] = []
        p_num = 1
        if phase1:
            phases_list.append((p_num, "Storage / Databases", tuple(phase1)))
            p_num += 1
        if phase2:
            phases_list.append((p_num, "Migrations / Initialization", tuple(phase2)))
            p_num += 1
        if phase3:
            phases_list.append((p_num, "Core Application Services", tuple(phase3)))
            p_num += 1
        if phase4:
            phases_list.append((p_num, "Frontend & Administration", tuple(phase4)))

        # Published ports
        pub_ports: list[PublishedPort] = []
        security_warnings: list[str] = []
        has_critical_security = False
        has_high_security = False

        for s in stack.discovered_services:
            if s.privileged:
                has_critical_security = True
                security_warnings.append(f"Privileged container detected: {s.name} runs with elevated host privileges.")
            for v in s.volumes:
                if 'docker.sock' in v:
                    has_critical_security = True
                    security_warnings.append(f"Docker socket mounted into {s.name}: allows full host container control.")
            if s.network_mode == 'host':
                has_high_security = True
                security_warnings.append(f"Host networking enabled on {s.name}: bypasses container network isolation.")
            for p in s.ports:
                # e.g. "5432:5432" or "127.0.0.1:8000:8000" or "80"
                parts = p.split(':')
                if len(parts) == 3:
                    host_ip, host_port, c_port = parts
                elif len(parts) == 2:
                    host_ip = "0.0.0.0"
                    host_port, c_port = parts
                else:
                    host_ip = "0.0.0.0"
                    host_port, c_port = parts[0], parts[0]
                if host_port.isdecimal() and c_port.isdecimal():
                    pub_ports.append(PublishedPort(s.name, int(host_port), int(c_port)))
                    if s.role == 'database' and host_ip in ('0.0.0.0', ''):
                        has_critical_security = True
                        security_warnings.append(f"Database service '{s.name}' publishes port {host_port} on all network interfaces (0.0.0.0).")

        # Risk classification
        if has_critical_security:
            risk = "CRITICAL"
        elif has_high_security or cycle_errors or not readiness.ready:
            risk = "HIGH"
        elif any(f.level == 'WARNING' for f in readiness.findings) or security_warnings:
            risk = "MEDIUM"
        else:
            risk = "LOW"

        all_blockers = list(readiness.failures)
        if cycle_errors:
            all_blockers.extend(cycle_errors)
        all_warnings = list(readiness.warnings) + security_warnings

        # Estimate local storage
        storage_est = 5.0 + 2.0 * len(stack.discovered_services)

        return DeploymentPlan(
            project_root=root,
            infrastructure_mode="existing",
            compose_files=stack.selected_compose_files,
            profiles=(),
            docker_context=docker_context,
            services=stack.discovered_services,
            dependencies_graph=dep_graph,
            topological_order=tuple(topological_order_list),
            startup_phases=tuple(phases_list),
            published_ports=tuple(pub_ports),
            volumes=tuple(sorted(set(v for s in stack.discovered_services for v in s.volumes))),
            environment_prerequisites=tuple(sorted(set(k for s in stack.discovered_services for k in s.environment_keys))),
            gpu_required=any('nvidia' in (s.image or '').lower() for s in stack.discovered_services),
            storage_estimate_gib=storage_est,
            risk_classification=risk,
            blockers=tuple(all_blockers),
            warnings=tuple(all_warnings),
            notes=readiness.notes,
        )

    @staticmethod
    def show_plan(plan: DeploymentPlan) -> None:
        print(f"\nAuto Docker Deployment Plan (READ ONLY): {plan.project_root}")
        print(f"Infrastructure : {plan.infrastructure_mode.capitalize()} Docker Compose stack")
        print(f"Compose files  : {', '.join(plan.compose_files) or '(none)'}")
        print(f"Docker context : {plan.docker_context or 'CLI default (local)'}")
        print(f"Services ({len(plan.services)}):")
        for s in plan.services:
            deps_str = f", depends_on: {', '.join(s.depends_on)}" if s.depends_on else ""
            ports_str = f", ports: {', '.join(s.ports)}" if s.ports else ""
            target_str = f"image: {s.image}" if s.image else f"build: {s.build_context or '.'}"
            print(f"  - {s.name:12s} [{s.role:9s}] {target_str}{ports_str}{deps_str}")

        print("\nStartup Execution Phases:")
        for num, pname, svcs in plan.startup_phases:
            print(f"  Phase {num} [{pname}]: {', '.join(svcs)}")

        print(f"\nRisk Assessment: {plan.risk_classification}")
        if plan.published_ports:
            print("Published Ports:")
            for p in plan.published_ports:
                print(f"  {p.service}: host {p.host} -> container {p.container}")

        print("\nDeployment Gates:")
        if plan.blockers:
            print(f"  BLOCKERS: {len(plan.blockers)}")
            for b in plan.blockers:
                print(f"    - {b}")
        else:
            print("  BLOCKERS: 0")
        if plan.warnings:
            print(f"  WARNINGS: {len(plan.warnings)}")
            for w in plan.warnings:
                print(f"    - {w}")
        else:
            print("  WARNINGS: 0")
        print(f"  STATUS  : {'READY for explicitly approved deployment' if plan.ready else 'BLOCKED — review blockers above'}")

    @staticmethod
    def show(stack: ExistingStack) -> None:
        print(f"\nExisting deployment inventory (READ ONLY): {stack.root}")
        print('Compose files: ' + (', '.join(stack.compose_files) or '(none)'))
        if len(stack.compose_files) > 1 and not stack.selected_compose:
            print('Multiple Compose files: specify one with native inspect FILE or native check FILE.')
        print('Dockerfiles  : ' + (', '.join(stack.dockerfiles) or '(none)'))
        if stack.discovered_services:
            print('Services (' + str(len(stack.discovered_services)) + '):')
            for s in stack.discovered_services:
                print(f"  - {s.name:12s} [{s.role:9s}]" + (f" image: {s.image}" if s.image else f" build: {s.build_context}"))
        else:
            print('Services    : ' + (', '.join(stack.services) or '(not determined; ask Docker Compose)'))
        print('Helm        : ' + ('found' if stack.has_helm else 'not found'))
        print('Python deps : ' + ('pyproject.toml ' if stack.has_pyproject else '')
              + ('uv.lock' if stack.has_uv_lock else '') if (stack.has_pyproject or stack.has_uv_lock)
              else 'Python deps : (no root manifest detected)')
        print('Frontend    : ' + ('found' if stack.has_frontend else 'not found'))
        print('Environment : ' + ('.env exists (values NOT read)' if stack.has_env
                                  else '.env missing; .env.example exists' if stack.has_env_sample
                                  else 'no .env or sample detected'))
        print('Auto Docker has not modified this repository or inferred a Python entrypoint.')
        if stack.compose_files:
            print("Use 'native plan' for deployment plan, 'native check' for Compose validation, or 'native build YES' after review.")
        print('NOTE: service names and roles above are a static hint; only Docker Compose validates the whole stack.')

    @classmethod
    def apply(cls, root: Path, action: str, *, choice: str | None = None,
              approved: bool = False, context: str | None = None, json_output: bool = False,
              allow_insecure: bool = False, orchestrator: DeploymentOrchestrator | None = None,
              project_name: str | None = None) -> None:
        orch = orchestrator if orchestrator is not None else DeploymentOrchestrator(root)
        if orch.state in {DeploymentState.BLOCKED, DeploymentState.FAILED, DeploymentState.CANCELLED}:
            orch.transition(DeploymentState.ANALYZED, f"Re-analyzing repository for native {action}")
        elif orch.state == DeploymentState.DISCOVERED:
            orch.transition(DeploymentState.ANALYZED, "Discovered existing infrastructure stack")

        stack = cls.discover(root, choice)
        if action in {'inspect', 'status'}:
            cls.show(stack)
            return

        plan = cls.plan(root, choice, docker_context=context)
        if orch.state == DeploymentState.ANALYZED:
            orch.transition(DeploymentState.PLANNED, f"Plan generated with {len(plan.services)} services (risk: {plan.risk_classification})")

        if action == 'plan':
            if json_output:
                print(json.dumps(plan.to_dict(), indent=2))
            else:
                cls.show_plan(plan)
            return
        if action in {'ready', 'readiness'}:
            ready = NativeReadiness.report(root, choice)
            if not ready.ready:
                if orch.state in {DeploymentState.VERIFYING, DeploymentState.HEALTHY, DeploymentState.DEGRADED}:
                    orch.last_validation = DeploymentState.BLOCKED
                    raise OpsError('BLOCKED: Existing repository is not deployment-ready.')
                orch.transition(DeploymentState.BLOCKED, f"Readiness blockers: {', '.join(ready.failures)}")
                raise OpsError('BLOCKED: Existing repository is not deployment-ready.')
            if orch.state in {DeploymentState.VERIFYING, DeploymentState.HEALTHY, DeploymentState.DEGRADED}:
                orch.last_validation = DeploymentState.VALIDATED
                return
            orch.transition(DeploymentState.VALIDATED, "Prerequisites validated")
            return
        if not stack.compose_files:
            orch.transition(DeploymentState.BLOCKED, "No root Compose file found")
            raise OpsError('No root Compose file found. Use a Python entrypoint for generator mode.')
        if not stack.selected_compose:
            orch.transition(DeploymentState.BLOCKED, "Multiple Compose files found; none selected")
            raise OpsError('Multiple Compose files. Specify one: native ' + action + ' FILENAME')
        if action not in {'check', 'build', 'up', 'down', 'ps', 'logs', 'verify'}:
            raise OpsError('native actions: inspect, plan, check, ready, build, up, down, ps, logs, verify')

        # Check approval
        if action in {'build', 'up', 'down'} and not approved:
            if orch.state == DeploymentState.VALIDATED:
                orch.transition(DeploymentState.CANCELLED, f"Approval required: native {action}")
            raise OpsError(f"Approval required: native {action} " +
                           (f"{stack.selected_compose} " if choice else '') + 'YES')

        # If an environment template is provided, don't start potentially
        # misconfigured services without a reviewed .env. Do not read credentials.
        if action in {'build', 'up'} and stack.has_env_sample and not stack.has_env:
            orch.transition(DeploymentState.BLOCKED, "Missing required .env from template")
            raise OpsError('Found .env.example but no .env. Review the project README, '
                           'create .env and supply required credentials before building/starting.')
        if action in {'build', 'up'}:
            ready = NativeReadiness.report(root, choice)
            if not ready.ready:
                orch.transition(DeploymentState.BLOCKED, f"Readiness blockers: {', '.join(ready.failures)}")
                raise OpsError('BLOCKED: Fix native deployment prerequisites before build/up.')
            # Security Gate: critical findings block unless allow_insecure is set
            if plan.risk_classification == 'CRITICAL' and not allow_insecure:
                orch.transition(DeploymentState.BLOCKED, f"Blocked by security gate: {'; '.join(plan.warnings)}")
                raise OpsError('BLOCKED: Critical container security risks detected (' +
                               '; '.join(plan.warnings) +
                               '). Deployment rejected. Use --allow-insecure to override.')

        if orch.state == DeploymentState.PLANNED:
            orch.transition(DeploymentState.VALIDATED, "Prerequisites and security gates validated")
        if action in {'build', 'up', 'down'}:
            orch.transition(DeploymentState.APPROVED, f"Operator approved native {action}")

        docker = shutil.which('docker')
        if not docker:
            orch.transition(DeploymentState.BLOCKED, "Docker CLI unavailable on host")
            if action == 'check':
                raise OpsError('BLOCKED: Docker CLI missing; Compose syntax NOT verified. '
                               'Existing infrastructure was not modified.')
            raise OpsError('Docker CLI unavailable; existing infrastructure was not modified.')
        prefix = [docker]
        context = DockerAdapter.validate_context(context)
        if context:
            prefix += ['--context', context]
        compose = list(prefix) + ['compose']
        if project_name:
            compose.extend(['-p', project_name])
        for cf in (stack.selected_compose_files if stack.selected_compose_files else (stack.selected_compose,)):
            compose.extend(['-f', cf])
        version_ok, detail = EnvironmentDoctor._probe([*prefix, 'compose', 'version'])
        if not version_ok:
            orch.transition(DeploymentState.BLOCKED, "Docker Compose unavailable: " + detail)
            raise OpsError('Docker Compose unavailable: ' + detail)
        if action == 'check':
            command = [*compose, 'config', '--quiet']
        else:
            engine_ok, detail = EnvironmentDoctor._probe([*prefix, 'info', '--format', '{{.ServerVersion}}'])
            if not engine_ok:
                orch.transition(DeploymentState.BLOCKED, "Docker Engine unavailable: " + detail)
                raise OpsError('Docker Engine unavailable: ' + detail)
            suffix = {'build': ['build'], 'up': ['up', '-d', '--no-build'],
                      'down': ['down'], 'ps': ['ps'], 'logs': ['logs', '--tail', '80'],
                      'verify': ['ps', '--all', '--format', 'json']}[action]
            command = [*compose, *suffix]
        # Host port publication depends on repo Compose settings, not the generator.
        if action == 'up':
            print('WARNING: Existing Compose ports may be accessible beyond localhost. '
                  'Review published ports, secrets, and host firewall before starting.')

        # State transition prior to command execution
        if action == 'build':
            orch.transition(DeploymentState.BUILDING, "Executing docker compose build")
        elif action == 'up':
            orch.transition(DeploymentState.STARTING, "Executing docker compose up -d --no-build")

        # Capture output for check and verify to preserve diagnostics
        capture = (action in {'check', 'verify'})
        try:
            result = subprocess.run(command, cwd=root, check=False,
                                    capture_output=capture, text=True if capture else False,
                                    timeout=3600 if action == 'build' else 300)
        except subprocess.TimeoutExpired as exc:
            orch.transition(DeploymentState.FAILED, f"native {action} timed out")
            raise OpsError(f'native {action} timed out; inspect Docker state before retrying.') from exc
        except OSError as exc:
            orch.transition(DeploymentState.FAILED, f"native {action} failed to start: {exc}")
            raise OpsError(f'native {action} failed to start: {exc}') from exc
        if result.returncode:
            if action == 'check':
                err_detail = (result.stderr or '').strip() if isinstance(result.stderr, str) else ''
                if orch.state in {DeploymentState.VERIFYING, DeploymentState.HEALTHY, DeploymentState.DEGRADED}:
                    orch.last_validation = DeploymentState.BLOCKED
                    raise OpsError(f'Compose configuration check failed (exit {result.returncode}): {err_detail or "Compose syntax/interpolation invalid."}')
                orch.transition(DeploymentState.BLOCKED, f"Compose configuration check failed (exit {result.returncode})")
                raise OpsError(f'Compose configuration check failed (exit {result.returncode}): {err_detail or "Compose syntax/interpolation invalid."}')
            orch.transition(DeploymentState.FAILED, f"native {action} failed with exit code {result.returncode}")
            raise OpsError(f'native {action} failed (exit {result.returncode}). '
                           'Review Docker output above; existing files were not overwritten.')

        print(f"Existing Compose project: native {action} succeeded (file: {stack.selected_compose}).")
        if action == 'check':
            orch.last_validation = DeploymentState.VALIDATED
            print('Compose syntax/interpolation: Docker Compose reported OK.')
        elif action == 'build':
            orch.transition(DeploymentState.VALIDATED, "Build completed successfully; images ready for deployment")
        elif action == 'up':
            orch.transition(DeploymentState.VERIFYING, "Containers started; awaiting verification")
            print("Containers were started. State transitioned to VERIFYING. Use 'native verify' or 'native ps' to inspect runtime health.")
        elif action == 'down':
            orch.transition(DeploymentState.ANALYZED, "Containers stopped and removed")
        elif action == 'verify':
            stdout_text = result.stdout if isinstance(result.stdout, str) else ''
            statuses = RuntimeHealth.parse(stdout_text)
            if not statuses:
                orch.transition(DeploymentState.DEGRADED, "No running containers found for stack")
                print("VERIFY NOTICE: No containers currently reported by Docker Compose ps.")
            else:
                has_unhealthy = False
                all_have_healthcheck = True
                all_healthy = True
                print("\nContainer Runtime Verification:")
                for item in statuses:
                    s_name = item.get('Service', item.get('Name', 'unknown'))
                    state = str(item.get('State', '')).lower()
                    health = str(item.get('Health', '')).lower()
                    exit_code = item.get('ExitCode')
                    print(f"  Service: {s_name:<20} State: {state:<12} Health: {health or 'none':<10}")
                    if state == 'exited' and exit_code not in (0, None):
                        has_unhealthy = True
                    if health == 'unhealthy':
                        has_unhealthy = True
                    if not health:
                        all_have_healthcheck = False
                    elif health != 'healthy':
                        all_healthy = False
                
                if has_unhealthy:
                    orch.transition(DeploymentState.DEGRADED, "One or more containers unhealthy or exited with error")
                    print("Status: DEGRADED — Container failures detected.")
                elif all_have_healthcheck and all_healthy:
                    orch.transition(DeploymentState.HEALTHY, "All container healthchecks reported healthy")
                    print("Status: HEALTHY — All verified containers are healthy.")
                else:
                    # Unknown health must remain VERIFYING per user instructions
                    orch.transition(DeploymentState.VERIFYING, "Containers running; application readiness unknown (no explicit healthchecks)")
                    print("Status: VERIFYING — Containers are running, but no explicit container healthchecks confirm readiness.")


# ---------- Existing infrastructure readiness (never prints .env values) ----------

@dataclasses.dataclass(frozen=True)
class ReadinessResult:
    failures: tuple[str, ...]
    warnings: tuple[str, ...]
    notes: tuple[str, ...]
    findings: tuple[ReadinessFinding, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.failures


class NativeReadiness:
    """Static, read-only pre-deployment checks for repository-owned Compose stacks.

    A PASS means offline prerequisites look reasonable, NOT that Docker
    images will build or the deployment is production-safe.
    """

    MAX_ENV_SIZE = 128 * 1024
    SENSITIVE = re.compile(r'(?:PASS(?:WORD)?|SECRET|TOKEN|API[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIAL|AUTH|ACCESS[_-]?KEY)', re.I)
    PLACEHOLDER = re.compile(
        r'^(?:|changeme|change[_-]?me|replace[_-]?me|replace[_-]?this|your[_-].*|'
        r'example.*|placeholder.*|<[^>]*>|\[[^\]]*\]|todo.*|password|secret|'
        r'test[_-]?key|xxx+|dummy.*|insert[_-].*|admin123|root)$', re.I)
    REFERENCE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?])[^}]*)?\}')
    KEY = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=')
    RECOMMENDED_GIB = 10

    @classmethod
    def _env_map(cls, path: Path) -> dict[str, str]:
        if path.is_symlink() or not path.is_file():
            raise OpsError(f'Cannot safely inspect {path.name}: expected a regular file (not symlink).')
        if path.stat().st_size > cls.MAX_ENV_SIZE:
            raise OpsError(f'Cannot inspect {path.name}: file exceeds {cls.MAX_ENV_SIZE} bytes.')
        try:
            text = path.read_text(encoding='utf-8-sig')
        except (UnicodeError, OSError) as exc:
            raise OpsError(f'Cannot read {path.name}: check file permissions/encoding.') from exc
        result: dict[str, str] = {}
        for line in text.splitlines():
            if line.lstrip().startswith('#'):
                continue
            m = cls.KEY.match(line)
            if not m:
                continue
            key = m.group(1)
            value = line[m.end():].strip()
            if len(value) > 1 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            result[key] = value
        return result

    @classmethod
    def inspect(cls, root: Path, choice: str | None = None, *, disk_check: bool = True, docker_context: str | None = None) -> ReadinessResult:
        stack = ExistingInfrastructure.discover(root, choice)
        errors: list[str] = []
        warnings: list[str] = []
        notes: list[str] = ['Read-only scan. No credentials or environment values are displayed.']
        findings: list[ReadinessFinding] = [
            ReadinessFinding(level="INFO", category="security", message="Read-only scan. No credentials or environment values are displayed.")
        ]

        if not stack.compose_files:
            msg = 'No root Compose file found.'
            errors.append(msg)
            findings.append(ReadinessFinding(level="BLOCKER", category="infrastructure", message=msg))
        elif stack.selected_compose is None and len(stack.compose_files) > 1:
            msg = 'Multiple Compose files; select one explicitly.'
            errors.append(msg)
            findings.append(ReadinessFinding(level="BLOCKER", category="infrastructure", message=msg))

        sample = next((root / name for name in ('.env.example', '.env.sample', '.env.template')
                       if (root / name).exists() or (root / name).is_symlink()), None)
        actual = root / '.env'
        sample_keys: dict[str, str] = {}
        live_keys: dict[str, str] = {}

        if sample is not None:
            try:
                sample_keys = cls._env_map(sample)
            except OpsError as exc:
                warnings.append(str(exc))
                findings.append(ReadinessFinding(level="WARNING", category="template", message=str(exc)))

        if actual.is_symlink():
            msg = '.env must not be a symlink.'
            errors.append(msg)
            findings.append(ReadinessFinding(level="BLOCKER", category="security", message=msg))
        elif not actual.exists():
            if sample is not None:
                msg = f'Missing .env; create it from the template and configure secrets before deployment.'
                errors.append(msg)
                findings.append(ReadinessFinding(level="BLOCKER", category="environment", message=msg))
            else:
                msg = 'No root .env (project may rely on another secrets mechanism).'
                notes.append(msg)
                findings.append(ReadinessFinding(level="INFO", category="environment", message=msg))
        elif not actual.is_file():
            msg = '.env is not a regular file.'
            errors.append(msg)
            findings.append(ReadinessFinding(level="BLOCKER", category="environment", message=msg))
        else:
            try:
                live_keys = cls._env_map(actual)
            except OpsError as exc:
                errors.append(str(exc))
                findings.append(ReadinessFinding(level="BLOCKER", category="environment", message=str(exc)))
            if os.name == 'posix':
                try:
                    if actual.stat().st_mode & (stat.S_IRGRP | stat.S_IROTH):
                        msg = '.env is readable by other users; consider chmod 600 .env.'
                        warnings.append(msg)
                        findings.append(ReadinessFinding(level="WARNING", category="permissions", message=msg))
                except OSError:
                    msg = 'Could not inspect .env permissions.'
                    errors.append(msg)
                    findings.append(ReadinessFinding(level="BLOCKER", category="permissions", message=msg))

        if live_keys:
            for key in sorted(set(sample_keys).difference(live_keys)):
                msg = f'Example environment variable not configured: {key}'
                warnings.append(msg)
                findings.append(ReadinessFinding(level="WARNING", category="configuration", message=msg, key=key))
            for key, value in sorted(live_keys.items()):
                if cls.SENSITIVE.search(key):
                    if cls.PLACEHOLDER.fullmatch(value) or (
                        key in sample_keys and value == sample_keys[key] and bool(value)
                    ):
                        msg = f'Example or placeholder credential detected: {key} (value hidden)'
                        errors.append(msg)
                        findings.append(ReadinessFinding(level="BLOCKER", category="security", message=msg, key=key))
                elif value and cls.PLACEHOLDER.fullmatch(value):
                    msg = f'Potential placeholder setting: {key} (value hidden)'
                    warnings.append(msg)
                    findings.append(ReadinessFinding(level="WARNING", category="configuration", message=msg, key=key))

        # Check Compose file references & interpolation across all selected Compose files
        target_compose_files = stack.selected_compose_files if stack.selected_compose_files else ((stack.selected_compose,) if stack.selected_compose else ())
        for comp_name in target_compose_files:
            try:
                contents = ExistingInfrastructure._safe_file(root, comp_name).read_text(encoding='utf-8-sig')
            except (OpsError, OSError, UnicodeError):
                msg = f'Could not safely inspect Compose file: {comp_name}'
                if msg not in errors:
                    errors.append(msg)
                    findings.append(ReadinessFinding(level="BLOCKER", category="compose", message=msg))
                continue

            for match in cls.REFERENCE.finditer(contents):
                key = match.group(1)
                expr = match.group(0)
                is_colon_required = expr.startswith('${' + key + ':?')
                is_required = is_colon_required or expr.startswith('${' + key + '?')
                has_default = expr.startswith('${' + key + ':-') or expr.startswith('${' + key + '-')

                if is_required:
                    # Precedence: shell os.environ > root .env
                    in_shell = key in os.environ
                    in_dotenv = key in live_keys
                    val = os.environ[key] if in_shell else (live_keys[key] if in_dotenv else None)
                    is_set = in_shell or in_dotenv
                    is_non_empty = bool(val)

                    if not is_set or (is_colon_required and not is_non_empty):
                        item = f'Missing required Compose variable: {key}'
                        if item not in errors:
                            errors.append(item)
                            findings.append(ReadinessFinding(level="BLOCKER", category="compose", message=item, key=key))
                elif not has_default and (key not in os.environ and key not in live_keys):
                    item = f"Compose interpolation variable '{key}' is not set (Compose substitutes empty string)"
                    if item not in notes:
                        notes.append(item)
                        findings.append(ReadinessFinding(level="INFO", category="compose", message=item, key=key))

        # Check env_file references from discovered services
        for s in stack.discovered_services:
            for ef in s.env_files:
                ef = ef.strip().strip('\'"')
                if ef and ef != '.env':
                    target_ef = root / ef
                    if not target_ef.is_file():
                        msg = f'Missing referenced env_file: {ef}'
                        if msg not in errors:
                            errors.append(msg)
                            findings.append(ReadinessFinding(level="BLOCKER", category="environment", message=msg))

        if docker_context:
            msg = f"Remote Docker context ({docker_context}): engine host storage and network capacity are remote."
            notes.append(msg)
            findings.append(ReadinessFinding(level="UNKNOWN", category="storage", message="Remote Docker Engine storage space could not be observed."))
        elif disk_check:
            try:
                free = shutil.disk_usage(root).free / 1024**3
                notes.append(f'Available project-filesystem space: {free:.1f} GiB.')
                findings.append(ReadinessFinding(level="INFO", category="storage", message=f"Available project-filesystem space: {free:.1f} GiB."))
                if free < cls.RECOMMENDED_GIB:
                    msg = f'Only {free:.1f} GiB free here; complex multi-service builds may need 10+ GiB on the Docker host.'
                    warnings.append(msg)
                    findings.append(ReadinessFinding(level="WARNING", category="storage", message=msg))
            except OSError:
                msg = 'Could not measure disk space.'
                warnings.append(msg)
                findings.append(ReadinessFinding(level="WARNING", category="storage", message=msg))

        notes.append('Docker Engine, Compose evaluation, network, and live service readiness are separate gates.')
        findings.append(ReadinessFinding(level="INFO", category="engine", message="Docker Engine, Compose evaluation, network, and live service readiness are separate gates."))
        return ReadinessResult(tuple(errors), tuple(warnings), tuple(notes), tuple(findings))

    @classmethod
    def report(cls, root: Path, choice: str | None = None, *, disk_check: bool = True, docker_context: str | None = None) -> ReadinessResult:
        result = cls.inspect(root, choice, disk_check=disk_check, docker_context=docker_context)
        print(f'\nNative deployment readiness (READ ONLY): {root}')
        for issue in result.failures:
            print('BLOCKED: ' + issue)
        for warning in result.warnings:
            print('WARN: ' + warning)
        for note in result.notes:
            print('INFO: ' + note)
        if result.ready:
            print('PASS: Static prerequisites checked; live deployment NOT verified.')
        else:
            print('BLOCKED: Review the issues above before building/starting containers.')
        return result


class ProjectRootDiscovery:
    """Suggest the closest ancestor with Compose or project artifacts; never silently change project."""

    COMPOSE_NAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
    OVERRIDE_NAMES = ("compose.override.yaml", "compose.override.yml", "docker-compose.override.yaml", "docker-compose.override.yml")

    @classmethod
    def score_dir(cls, directory: Path) -> int:
        score = 0
        if not directory.is_dir():
            return 0
        has_compose = any((directory / n).is_file() and not (directory / n).is_symlink()
                          for n in cls.COMPOSE_NAMES + cls.OVERRIDE_NAMES)
        if has_compose:
            score += 100
        has_dockerfile = any((directory / n).is_file() and not (directory / n).is_symlink()
                             for n in ('Dockerfile', 'docker/Dockerfile', 'build/Dockerfile'))
        if has_dockerfile:
            score += 70
        has_py = any((directory / n).is_file() and not (directory / n).is_symlink()
                     for n in ('pyproject.toml', 'requirements.txt', 'uv.lock', 'poetry.lock', 'Pipfile.lock'))
        if has_py:
            score += 60
        has_node = (directory / 'package.json').is_file() and not (directory / 'package.json').is_symlink()
        if has_node:
            score += 50
        if has_py and has_node:
            score += 35
        if (directory / 'helm').is_dir() or (directory / 'Chart.yaml').is_file():
            score += 40
        if (directory / '.git').exists():
            score += 30
        return score

    @classmethod
    def suggest(cls, start: Path, max_levels: int = 6) -> Path | None:
        start = start.resolve(strict=True)
        if start.is_file():
            start = start.parent
        best_candidate: Path | None = None
        best_score = 0
        visited: set[Path] = set()

        for index, candidate in enumerate((start, *start.parents)):
            if index > max_levels or candidate == Path(candidate.anchor) or candidate == Path.home().resolve():
                break
            try:
                real_c = candidate.resolve()
            except OSError:
                break
            if real_c in visited:
                break
            visited.add(real_c)

            score = cls.score_dir(candidate)
            has_compose = any((candidate / n).is_file() and not (candidate / n).is_symlink()
                              for n in cls.COMPOSE_NAMES + cls.OVERRIDE_NAMES)
            if has_compose:
                return candidate
            if score > best_score and score >= 60:
                best_score = score
                best_candidate = candidate
        return best_candidate


@dataclasses.dataclass
class DockerHostStatus:
    cli_installed: bool
    cli_path: str | None
    compose_available: bool
    compose_version: str | None
    engine_reachable: bool
    server_version: str | None
    error_detail: str = ""

    @property
    def summary(self) -> str:
        if not self.cli_installed:
            return "CLI missing"
        if not self.compose_available:
            return "Compose missing"
        if not self.engine_reachable:
            return "Engine unreachable"
        return "Engine active"


class DockerHostProbe:
    """Accurately probes host Docker status distinguishing CLI, Compose, and Engine reachability."""

    @classmethod
    def probe(cls, context: str | None = None, timeout: float = 1.5) -> DockerHostStatus:
        docker_cli = shutil.which("docker")
        if not docker_cli:
            return DockerHostStatus(
                cli_installed=False,
                cli_path=None,
                compose_available=False,
                compose_version=None,
                engine_reachable=False,
                server_version=None,
                error_detail="Docker CLI binary not found on host PATH"
            )

        prefix = [docker_cli]
        if context:
            prefix.extend(["--context", context])

        # Probe Compose
        compose_ok = False
        compose_ver = None
        compose_err = ""
        try:
            res = subprocess.run([*prefix, "compose", "version"],
                                 capture_output=True, text=True, timeout=timeout, check=False)
            if res.returncode == 0:
                compose_ok = True
                compose_ver = res.stdout.strip().splitlines()[0] if res.stdout else "available"
            else:
                compose_err = (res.stderr or res.stdout or "").strip()
        except Exception as exc:
            compose_err = str(exc)

        # Probe Engine reachability
        engine_ok = False
        engine_ver = None
        engine_err = ""
        try:
            res = subprocess.run([*prefix, "info", "--format", "{{.ServerVersion}}"],
                                 capture_output=True, text=True, timeout=timeout, check=False)
            if res.returncode == 0:
                engine_ok = True
                engine_ver = res.stdout.strip()
            else:
                engine_err = (res.stderr or res.stdout or "").strip()
        except Exception as exc:
            engine_err = str(exc)

        err_detail = engine_err or compose_err if not (compose_ok and engine_ok) else ""
        return DockerHostStatus(
            cli_installed=True,
            cli_path=docker_cli,
            compose_available=compose_ok,
            compose_version=compose_ver,
            engine_reachable=engine_ok,
            server_version=engine_ver,
            error_detail=err_detail
        )


class EnvironmentDoctor:
    """Read-only sandbox AND host diagnostics. Never starts Docker or installs anything.

    A sandbox may have no Docker executable even when the host has the CLI.
    Host probes use a fixed, read-only command via flatpak-spawn only when
    Flatpak explicitly exposes that mechanism. Builds never escape the sandbox.
    """

    @staticmethod
    def in_flatpak() -> bool:
        return bool(os.environ.get("FLATPAK_ID")) or Path("/.flatpak-info").is_file()

    @staticmethod
    def _probe(args: list[str]) -> tuple[bool, str]:
        try:
            result = subprocess.run(args, capture_output=True, text=True,
                                    check=False, timeout=8)
        except subprocess.TimeoutExpired:
            return False, "command timed out (8 seconds)"
        except OSError as exc:
            return False, f"cannot execute command ({type(exc).__name__})"
        if result.returncode:
            # Include a bounded, useful error rather than hiding missing Compose
            # or a daemon socket behind a generic 'command returned an error'.
            lines = (result.stderr or result.stdout or "").strip().splitlines()
            detail = next((line.strip() for line in lines if line.strip()), "")
            return False, (detail[:180] or f"command failed (exit {result.returncode})")
        lines = (result.stdout or "").strip().splitlines()
        return True, (lines[0][:180] if lines else "available")

    @classmethod
    def _host_report(cls) -> list[tuple[str, bool, str]]:
        """Inspect host via fixed read-only commands; never infer host usability from CLI alone."""
        runner = shutil.which("flatpak-spawn")
        if runner is None:
            return [("Host access", False, "flatpak-spawn is not available in this sandbox")]
        prefix = [runner, "--host", "sh", "-c"]
        # No untrusted/user-controlled strings are interpolated into the shell.
        exists = "command -v docker >/dev/null 2>&1"
        rows = [
            ("Host Docker CLI", f"{exists} && docker --version"),
            ("Host Compose", f"{exists} && docker compose version"),
            ("Host Engine", f"{exists} && docker info --format '{{{{.ServerVersion}}}}'"),
        ]
        report = []
        for name, command in rows:
            ok, detail = cls._probe([*prefix, command])
            report.append((name, ok, detail if ok or detail else "unavailable"))
        return report

    def report(self, project: Target | None = None,
               docker_context: str | None = None) -> list[tuple[str, bool, str]]:
        rows: list[tuple[str, bool, str]] = [
            ("Python", sys.version_info >= (3, 10), sys.version.split()[0]),
        ]
        sandbox = self.in_flatpak()
        if sandbox:
            rows.append(("Environment", True, "Flatpak sandbox (host environment is separate)"))
        if project:
            rows.append(("Project", project.root.is_dir(), str(project.root)))
            try:
                available = shutil.disk_usage(project.root).free
                disk_label = f"{available / 1024**3:.1f} GiB reported"
                if sandbox:
                    disk_label += " (cloud quota may differ)"
                rows.append(("Free disk", available >= 5 * 1024**3, disk_label))
            except OSError:
                rows.append(("Free disk", False, "Unable to query available space"))
        docker = shutil.which("docker")
        if docker is None:
            rows.extend((name, False, "Docker CLI not found in this environment") for name in
                        ("Docker CLI", "Docker Compose", "Docker Engine"))
        else:
            rows.append(("Docker CLI", *self._probe([docker, "--version"])))
            rows.append(("Docker Compose", *self._probe([docker, "compose", "version"])))
            rows.append(("Docker Engine", *self._probe([docker, "info", "--format", "{{.ServerVersion}}"])))
        if docker_context is not None:
            DockerAdapter.validate_context(docker_context)
            if docker is None:
                rows.append(("Selected context", False,
                             f"{docker_context}: Docker CLI unavailable here; context was not checked"))
            else:
                ok, detail = self._probe([docker, "--context", docker_context,
                                          "info", "--format", "{{.ServerVersion}}"])
                rows.append(("Selected context", ok, f"{docker_context}: {detail}"))
        if sandbox:
            rows.extend(self._host_report())
        return rows

    def print_report(self, project: Target | None = None,
                     docker_context: str | None = None) -> bool:
        rows = self.report(project, docker_context)
        print("\nAuto Docker environment doctor (read-only)")
        for name, ok, detail in rows:
            print(f"  {'OK  ' if ok else 'WARN'} {name:17s} {detail}")
        if self.in_flatpak():
            print("Flatpak and Ubuntu-host capabilities are separate. Host probes are read-only.")
            print("Host Docker CLI alone is NOT enough: Compose and a working Engine are both required.")
            print("Auto Docker never runs build/up on the host through flatpak-spawn automatically.")
        print("Note: Docker is not required to inspect or generate configuration.")
        return all(ok for _, ok, _ in rows)


class PortAudit:
    """Non-destructive loopback TCP availability probe; results are advisory only."""

    @staticmethod
    def check(port: PublishedPort) -> tuple[bool, str]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(('127.0.0.1', port.host))
        except OSError as exc:
            return False, f'BUSY/UNAVAILABLE ({type(exc).__name__}: {exc.strerror or str(exc)})'
        return True, 'available now (not reserved; another process may claim it)'

    @classmethod
    def report(cls, ports: tuple[PublishedPort, ...]) -> list[tuple[PublishedPort, bool, str]]:
        return [(mapping, *cls.check(mapping)) for mapping in ports]

    @classmethod
    def print_report(cls, ports: tuple[PublishedPort, ...]) -> bool:
        print('\nHost port preflight (127.0.0.1; advisory, no ports reserved)')
        if not ports:
            print('  No published ports in this plan.')
        report = cls.report(ports)
        for mapping, ok, detail in report:
            print(f"  {'OK  ' if ok else 'WARN'} {mapping.service:8s} {mapping.host} -> {mapping.container}: {detail}")
        if not all(ok for _, ok, _ in report):
            print('Set a different host port: hostport SERVICE PORT  (e.g. hostport mlflow 5050)')
            print('An existing container from this project may also occupy its own port.')
        return all(ok for _, ok, _ in report)


class RuntimeHealth:
    """Evaluate Compose-reported health including successful one-shot Python jobs."""

    @staticmethod
    def parse(data: str) -> list[dict[str, Any]]:
        content = data.strip()
        if not content:
            return []
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            try:
                parsed = [json.loads(line) for line in content.splitlines() if line.strip()]
            except json.JSONDecodeError as exc:
                raise OpsError('Docker Compose returned invalid JSON service status.') from exc
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
            raise OpsError('Docker Compose status must be JSON objects or a JSON array.')
        if len(parsed) > 1000:
            raise OpsError('Unexpectedly large Docker service status response.')
        return parsed

    @staticmethod
    def evaluate(expected: list[str], batch: bool, statuses: list[dict[str, Any]]) -> tuple[bool, list[str]]:
        outputs = []
        good = True
        for service in expected:
            matching = [item for item in statuses if item.get('Service') == service]
            if not matching:
                outputs.append(f'{service}: MISSING (container not found)')
                good = False
                continue
            for item in matching:
                state = str(item.get('State', '')).lower()
                health = str(item.get('Health', '')).lower()
                exitcode = item.get('ExitCode')
                name = str(item.get('Name', service))[:120]
                if state == 'running' and health in ('', 'healthy'):
                    label = 'RUNNING / healthy' if health == 'healthy' else 'RUNNING (no healthcheck; readiness unknown / unverified)'
                    ok = True
                elif service == 'app' and batch and state == 'exited' and exitcode == 0:
                    label, ok = 'COMPLETED (exit 0)', True
                else:
                    label, ok = f'NOT READY (state={state or "unknown"}, health={health or "n/a"}, exit={exitcode})', False
                good &= ok
                outputs.append(f'{service}: {label} [{name}]')
        return good, outputs


class DockerAdapter:
    """Docker CLI adapter with an optional per-invocation named context.

    No global `docker context use`, Flatpak host escape, shell execution, or
    unattended installation. Remote contexts must already exist on the CLI host.
    """

    @staticmethod
    def validate_context(value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
            raise OpsError("Invalid Docker context name (letters, digits, . _ -; max 64 characters).")
        return value

    def __init__(self, root: Path, docker_context: str | None = None):
        self.root = root
        self.compose_file = root / OUTPUT / "compose.yaml"
        self.context = self.validate_context(docker_context)

    def _docker(self) -> str:
        command = shutil.which("docker")
        if not command:
            detail = ("Docker CLI unavailable inside Flatpak. Run 'doctor' to inspect host Docker; "
                      "remote contexts also require an accessible CLI in this environment."
                      if EnvironmentDoctor.in_flatpak()
                      else "Docker CLI not found. Generation works without Docker; install Docker for build/up.")
            raise OpsError(detail)
        return command

    def _prefix(self) -> list[str]:
        cmd = [self._docker()]
        if self.context:
            cmd.extend(["--context", self.context])
        return cmd

    def _compose_command(self, *args: str) -> list[str]:
        return [*self._prefix(), "compose", "-f", str(self.compose_file), *args]

    def preflight(self) -> None:
        """Require a usable CLI, Compose, Engine, and valid Compose configuration."""
        prefix = self._prefix()
        for label, args in (("Docker Compose", [*prefix, "compose", "version"]),
                            ("Docker Engine", [*prefix, "info", "--format", "{{.ServerVersion}}"]),
                            ("Compose configuration", self._compose_command("config", "-q"))):
            if label == "Compose configuration" and not self.compose_file.is_file():
                raise OpsError("No generated Compose file; run generate first.")
            okay, detail = EnvironmentDoctor._probe(args)
            if not okay:
                raise OpsError(f"{label} unavailable: {detail}. Run 'doctor' or 'check'; no containers were changed.")

    def verify(self) -> tuple[bool, list[str]]:
        """Read Compose states; does not start, stop, or modify containers."""
        if not self.compose_file.is_file():
            raise OpsError('No generated Compose file; run generate first.')
        try:
            manifest = json.loads((self.root / OUTPUT / 'manifest.json').read_text(encoding='utf-8'))
            runtime = manifest.get('runtime')
        except (OSError, ValueError, AttributeError) as exc:
            raise OpsError('Cannot read generated runtime metadata; run check first.') from exc
        if not isinstance(runtime, dict) or not isinstance(runtime.get('services'), list):
            raise OpsError('No runtime metadata: re-generate using this Auto Docker version, then verify.')
        if runtime.get('execution_kind') == 'interactive':
            return False, ["app: INTERACTIVE (use 'run YES' from a real terminal; detached ps cannot verify a CLI conversation)"]
        command = self._compose_command('ps', '--all', '--format', 'json')
        try:
            result = subprocess.run(command, cwd=self.root, capture_output=True, text=True,
                                    check=False, timeout=30)
        except subprocess.TimeoutExpired as exc:
            raise OpsError('Docker status check timed out after 30 seconds.') from exc
        except OSError as exc:
            raise OpsError(f'Docker status unavailable: {exc}') from exc
        if result.returncode:
            detail = (result.stderr or '').strip().splitlines()
            msg = detail[-1][:300] if detail else f'exit code {result.returncode}'
            raise OpsError('Could not read Compose container states: ' + msg)
        if len(result.stdout) > 2_000_000:
            raise OpsError('Docker status output is too large to inspect.')
        return RuntimeHealth.evaluate(runtime['services'], runtime.get('batch') is True,
                                      RuntimeHealth.parse(result.stdout))

    @staticmethod
    def health_hints(lines: list[str]) -> list[str]:
        """Actionable, conservative hints using container state only; no log ingestion."""
        hints: list[str] = []
        content = '\n'.join(lines).lower()
        if 'missing (container not found)' in content:
            hints.append('Missing container: run build, then up; use ps to inspect Compose services.')
        if 'unhealthy' in content or 'health=starting' in content:
            hints.append('Readiness failed: inspect logs and health checks; dependency startup may need more time.')
        if 'exited' in content or 'state=exited' in content:
            hints.append('A container exited: inspect logs, required packages, and the application command.')
        if 'no healthcheck; readiness unknown' in content:
            hints.append('Running does not prove application readiness; test its actual API or task result.')
        if not hints:
            hints.append('Inspect logs for service-specific errors; runtime health is not application correctness.')
        return hints

    def run(self, action: str) -> None:
        if action in {"build", "up", "dryrun", "run", "job"}:
            self.preflight()
        if action in {"run", "job"}:
            # Keep application input attached; a one-off container reports the
            # actual process exit code and is removed on completion.
            if action == 'run' and not (sys.stdin.isatty() and sys.stdout.isatty()):
                raise OpsError("Interactive run needs a real terminal (TTY). Use a host terminal, not a piped command.")
            command = self._compose_command('run', '--rm', *(['-T'] if action == 'job' else []), 'app')
            timeout = None  # foreground application lifetime is user-controlled
        elif action == 'dryrun':
            # The Compose dry-run flag is non-mutating, but requires a real Engine.
            command = [*self._prefix(), 'compose', '--dry-run', '-f', str(self.compose_file),
                       'up', '--build', '-d']
        else:
            args = {
                "check": ["config", "-q"],
                "build": ["build"],
                "up": ["up", "-d"],
                "down": ["down"],
                "ps": ["ps"],
                "logs": ["logs", "--tail=100", "--no-color"],
            }.get(action)
            if args is None:
                raise OpsError(f"Unknown Docker action: {action}")
            command = self._compose_command(*args)
        if action not in {"run", "job"}:
            timeout = {"build": 1800, "up": 1200, "down": 300, "dryrun": 180}.get(action, 90)
        if not self.compose_file.is_file():
            raise OpsError("No generated Compose file; run generate first.")
        LOG.info("Starting Docker action: %s", action)
        try:
            result = subprocess.run(command, cwd=self.root, check=False,
                                    timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise OpsError(f"Docker {action} timed out. Containers may still be running; use 'ps' and 'verify' to inspect before retrying.") from exc
        except OSError as exc:
            raise OpsError(f"Docker unavailable: {exc}") from exc
        if result.returncode:
            hint = ("Review the command output above; use 'diagnose' and 'logs' for troubleshooting."
                    if action in {'build','up'} else "Review the Docker CLI output above.")
            raise OpsError(f"Docker {action} failed with exit code {result.returncode}. {hint}")


class BuildContextSafety:
    """Bounded path-only audit. Never reads project secrets or follows symlinks."""

    SENSITIVE = frozenset({'.env', '.env.local', '.netrc', '.npmrc', '.pypirc',
                            'id_rsa', 'id_ed25519', 'credentials.json'})
    SENSITIVE_EXT = ('.pem', '.key', '.p12', '.pfx')
    LARGE_EXT = ('.gguf', '.safetensors', '.pt', '.pth', '.ckpt', '.onnx',
                 '.bin', '.h5', '.joblib', '.pkl')

    @classmethod
    def scan(cls, root: Path, ignore: str, maximum: int = 3000) -> tuple[bool, list[str]]:
        from fnmatch import fnmatch
        rules = [v.strip() for v in ignore.splitlines()
                 if v.strip() and not v.startswith('#') and not v.startswith('!')]
        findings: list[str] = []
        failures = False
        inspected = 0
        queue = deque([(root, 0)])
        while queue:
            directory, depth = queue.popleft()
            try:
                entries = sorted(directory.iterdir(), key=lambda x: x.name)
            except (OSError, PermissionError):
                findings.append('WARN: Could not read one project directory during context scan')
                continue
            for path in entries:
                inspected += 1
                if inspected > maximum:
                    findings.append('WARN: Build-context scan reached its file limit; not fully audited')
                    return not failures, findings
                if path.is_symlink():
                    # Symlinks outside the build root may be rejected by Docker; do not follow.
                    findings.append('WARN: Project contains symlink: ' + str(path.relative_to(root))[:150])
                    continue
                if path.is_dir():
                    if path.name in SKIP_DIRS or path.name.startswith('.opsforge-'):
                        continue
                    if depth < MAX_DEPTH:
                        queue.append((path, depth + 1))
                    continue
                if not path.is_file():
                    continue
                rel = path.relative_to(root).as_posix()
                filename = path.name.lower()
                is_sensitive = (filename in cls.SENSITIVE or filename.endswith(cls.SENSITIVE_EXT)
                                or filename.startswith('secrets.'))
                is_large = filename.endswith(cls.LARGE_EXT)
                if not (is_sensitive or is_large):
                    try:
                        is_large = path.stat().st_size >= 200 * 1024 * 1024
                    except OSError:
                        pass
                if not (is_sensitive or is_large):
                    continue
                # Conservative match of common Dockerignore rules: name, */name, **/name.
                covered = any(fnmatch(rel, rule) or fnmatch(filename, rule[3:] if rule.startswith('**/') else rule)
                              for rule in rules)
                if is_sensitive and not covered:
                    failures = True
                    findings.append('FAIL: Sensitive-looking file not explicitly ignored: ' + rel[:150])
                elif is_sensitive:
                    findings.append('INFO: Sensitive file excluded by generated ignore rules: ' + rel[:150])
                elif not covered:
                    findings.append('WARN: Large/model-like file may enter Docker build context: ' + rel[:150])
        return not failures, findings


class AppSmoke:
    """Read-only application HTTP probe; local loopback only, no arbitrary URL input."""

    @staticmethod
    def probe(root: Path, path: str = '/', expected: str | None = None) -> tuple[bool, str]:
        valid, problems = OutputManager(root).validate()
        if not valid:
            raise OpsError('Generated configuration is invalid: ' + '; '.join(problems))
        if not (path.startswith('/') and not path.startswith('//') and
                not any(ch in path for ch in ('\r', '\n', '#', '@')) and len(path) <= 200):
            raise OpsError('Smoke path must be a short absolute HTTP path starting with /')
        runtime = json.loads((root / OUTPUT / 'manifest.json').read_text(encoding='utf-8'))['runtime']
        if runtime.get('execution_kind') != 'web':
            raise OpsError('HTTP smoke probe requires a generated web application')
        ports = runtime.get('ports', {})
        port = ports.get('app')
        if not isinstance(port, int) or not 1024 <= port <= 65535:
            raise OpsError('Generated web application has no valid published app port')
        url = f'http://127.0.0.1:{port}{path}'
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'AutoDocker-Probe'})
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
                data = response.read(1024 * 1024 + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return False, f'Application endpoint not ready ({type(exc).__name__})'
        if len(data) > 1024 * 1024:
            return False, 'Response exceeds 1 MiB probe limit'
        if status != 200:
            return False, f'HTTP returned {status}, expected 200'
        if expected is not None and expected not in data.decode('utf-8', errors='replace'):
            return False, 'Expected response marker missing'
        return True, f'HTTP 200 from localhost:{port}{path}'


class OfflineDeploymentAudit:
    """Local pre-deployment checks that work on hosts with no Docker Engine.

    These checks are deliberately conservative, are never presented as a
    substitute for `docker compose config`, and never contact registries.
    """

    @staticmethod
    def evaluate(root: Path) -> tuple[bool, list[str]]:
        manager = OutputManager(root)
        valid, problems = manager.validate()
        if not valid:
            return False, ['FAIL: ' + problem for problem in problems]
        folder = root / OUTPUT
        try:
            manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
            dockerfile = (folder / 'Dockerfile').read_text(encoding='utf-8')
            ignore = (folder / 'Dockerfile.dockerignore').read_text(encoding='utf-8')
            compose = (folder / 'compose.yaml').read_text(encoding='utf-8')
        except (OSError, ValueError, UnicodeError) as exc:
            return False, [f'FAIL: Could not inspect generated files ({type(exc).__name__})']
        runtime = manifest.get('runtime', {})
        if not isinstance(runtime, dict):
            return False, ['FAIL: Invalid runtime manifest metadata']
        failures = []
        warnings = []
        if 'USER app' not in dockerfile:
            failures.append('FAIL: Generated app image lacks the expected non-root USER app')
        if 'CMD [' not in dockerfile:
            failures.append('FAIL: Generated app image lacks exec-form CMD')
        if not all(term in ignore for term in ('.env', '*.gguf', '*.pem')):
            failures.append('FAIL: Build-context ignore rules are incomplete')
        mappings = runtime.get('port_mappings', [])
        if not isinstance(mappings, list):
            failures.append('FAIL: Invalid runtime port mapping metadata')
        else:
            hosts: set[int] = set()
            for item in mappings:
                if not isinstance(item, dict) or not isinstance(item.get('host'), int) or item['host'] in hosts:
                    failures.append('FAIL: Duplicate or invalid published host port')
                    break
                hosts.add(item['host'])
                binding = f'127.0.0.1:{item["host"]}:{item.get("container")}'
                if binding not in compose:
                    failures.append('FAIL: Host port is missing its loopback-only Compose binding')
        if 'ollama/ollama:latest' in compose:
            warnings.append('WARN: Ollama uses a mutable latest tag; pin a reviewed image digest for reproducibility.')
        if 'ghcr.io/mlflow/mlflow:' in compose and 'ghcr.io/mlflow/mlflow@sha256:' not in compose:
            warnings.append('WARN: MLflow image uses a mutable version tag; verify the image digest for release.')
        if 'redis:7.4-alpine' in compose:
            warnings.append('WARN: Redis image uses a mutable version tag; verify the image digest for release.')
        if 'no-new-privileges:true' not in compose or 'cap_drop:' not in compose:
            warnings.append('WARN: Application container lacks optional no-new-privileges/capability-drop hardening.')
        requirements = root / 'requirements.txt'
        if requirements.is_file():
            try:
                unpinned = []
                for line in requirements.read_text(encoding='utf-8').splitlines():
                    line = line.split('#', 1)[0].strip()
                    if (not line or line.startswith(('-', '#'))):
                        continue
                    if not re.match(r'^[A-Za-z0-9_.-]+\s*==\s*[^=<>!~;\s]+', line):
                        unpinned.append(line.split(' ',1)[0][:70])
                if unpinned:
                    warnings.append('WARN: Requirements are not strictly pinned: ' + ', '.join(unpinned[:8]))
            except (OSError, UnicodeError):
                warnings.append('WARN: Could not verify requirements version pins')
        if 'ARG PYTHON_IMAGE=python:' in dockerfile and '@sha256:' not in dockerfile:
            warnings.append('WARN: Python base image uses a mutable tag; pin a verified digest for production.')
        context_ok, context_messages = BuildContextSafety.scan(root, ignore)
        if not context_ok:
            failures.extend(line for line in context_messages if line.startswith('FAIL:'))
        warnings.extend(line for line in context_messages if not line.startswith('FAIL:'))
        selected_images = runtime.get('service_images', {})
        if isinstance(selected_images, dict):
            for service, reference in selected_images.items():
                if '@sha256:' not in str(reference):
                    warnings.append(f'WARN: Selected {service} image is not digest-pinned')
        packages = runtime.get('inferred_packages', [])
        if packages:
            warnings.append('WARN: Inferred dependencies are unpinned: ' + ', '.join(str(p) for p in packages))
        if runtime.get('batch'):
            warnings.append('INFO: Batch applications may exit successfully; prefer job YES for foreground status.')
        if runtime.get('execution_kind') == 'interactive':
            warnings.append('INFO: Interactive apps require a terminal; use run YES, not detached up.')
        return not failures, ['PASS: Generated file hashes and structure are intact', *failures, *warnings,
                              'NOTE: This offline audit does not prove Docker Compose parsing, image builds, or startup.']

    @classmethod
    def print_report(cls, root: Path) -> bool:
        ok, lines = cls.evaluate(root)
        print('\nAuto Docker offline deployment audit (no container engine required)')
        for line in lines:
            print('  ' + line)
        return ok


class RecentProjectsStore:
    """Manages persistent recent project history stored in user's home directory.

    Stores clean path strings only; never credentials, tokens, or environment data.
    """
    STORE_PATH = Path.home() / ".auto_docker" / "recent_projects.json"

    @classmethod
    def load(cls) -> list[Path]:
        try:
            if not cls.STORE_PATH.is_file():
                return []
            data = json.loads(cls.STORE_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return []
            paths: list[Path] = []
            for item in data:
                if isinstance(item, str) and item.strip():
                    p = Path(item).resolve()
                    if p not in paths:
                        paths.append(p)
            return paths[:10]
        except Exception:
            return []

    @classmethod
    def save(cls, paths: list[Path]) -> None:
        try:
            cls.STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
            cleaned: list[str] = []
            for p in paths:
                resolved = str(p.resolve())
                if resolved not in cleaned:
                    cleaned.append(resolved)
            cls.STORE_PATH.write_text(json.dumps(cleaned[:10], indent=2), encoding="utf-8")
        except Exception:
            pass

    @classmethod
    def add(cls, path: Path) -> list[Path]:
        existing = cls.load()
        resolved = path.resolve()
        updated = [resolved] + [p for p in existing if p != resolved]
        cls.save(updated[:10])
        return updated[:10]


# ---------- Deployment Fingerprint: content-bound cryptographic digest ----------

_SESSION_HMAC_KEY = secrets.token_bytes(32)


class DeploymentDigest:
    """Computes a cryptographically bound, content-aware deployment fingerprint."""

    @classmethod
    def compute(
        cls,
        root: Path,
        action: str,
        *,
        choice: str | None = None,
        docker_context: str | None = None,
        allow_insecure: bool = False,
    ) -> str:
        resolved_root = root.resolve()
        stack = ExistingInfrastructure.discover(resolved_root, choice)
        files_data: dict[str, Any] = {}

        # 1. Selected compose files and ordered overrides
        compose_files = stack.selected_compose_files if stack.selected_compose_files else ((stack.selected_compose,) if stack.selected_compose else ())
        for cf in compose_files:
            cf_path = resolved_root / cf
            if cf_path.is_symlink():
                files_data[f"compose:{cf}"] = f"SYMLINK:{cf_path.resolve()}"
            elif cf_path.is_file():
                try:
                    raw = cf_path.read_bytes()
                    files_data[f"compose:{cf}"] = hashlib.sha256(raw).hexdigest()
                except OSError as exc:
                    files_data[f"compose:{cf}"] = f"ERROR:{exc}"
            else:
                files_data[f"compose:{cf}"] = "MISSING"

        # 2. Referenced Dockerfiles
        for df in stack.dockerfiles:
            df_path = resolved_root / df
            if df_path.is_symlink():
                files_data[f"dockerfile:{df}"] = f"SYMLINK:{df_path.resolve()}"
            elif df_path.is_file():
                try:
                    raw = df_path.read_bytes()
                    files_data[f"dockerfile:{df}"] = hashlib.sha256(raw).hexdigest()
                except OSError as exc:
                    files_data[f"dockerfile:{df}"] = f"ERROR:{exc}"
            else:
                files_data[f"dockerfile:{df}"] = "MISSING"

        # 3. Relevant environment input state (.env, .env.example, service env_files)
        # Protected via session-local HMAC-SHA256: secret values never stored in plain text or reusable unkeyed hashes
        env_files_to_check = set()
        if (resolved_root / ".env").exists():
            env_files_to_check.add(".env")
        if (resolved_root / ".env.example").exists():
            env_files_to_check.add(".env.example")
        if (resolved_root / ".env.sample").exists():
            env_files_to_check.add(".env.sample")
        for s in stack.discovered_services:
            for ef in s.env_files:
                env_files_to_check.add(ef)

        for ef in sorted(env_files_to_check):
            ef_path = resolved_root / ef
            if ef_path.is_symlink():
                files_data[f"env:{ef}"] = f"SYMLINK:{ef_path.resolve()}"
            elif ef_path.is_file():
                try:
                    raw = ef_path.read_bytes()
                    files_data[f"env:{ef}"] = hmac.new(_SESSION_HMAC_KEY, raw, hashlib.sha256).hexdigest()
                except OSError as exc:
                    files_data[f"env:{ef}"] = f"ERROR:{exc}"
            else:
                files_data[f"env:{ef}"] = "MISSING"

        # 4. Parsed service definitions (including command, entrypoint, image, etc.)
        services_summary = []
        for s in stack.discovered_services:
            services_summary.append({
                "name": s.name,
                "role": s.role,
                "image": s.image,
                "build_context": s.build_context,
                "dockerfile": s.dockerfile,
                "command": s.command,
                "entrypoint": s.entrypoint,
                "ports": list(s.ports),
                "volumes": list(s.volumes),
                "depends_on": list(s.depends_on),
                "environment_keys": list(s.environment_keys),
                "env_files": list(s.env_files),
                "profiles": list(s.profiles),
                "restart": s.restart,
                "privileged": s.privileged,
                "network_mode": s.network_mode,
            })

        payload = {
            "root": str(resolved_root),
            "action": action,
            "choice": choice or "",
            "docker_context": docker_context or "",
            "allow_insecure": allow_insecure,
            "files": files_data,
            "services": services_summary,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


# ---------- Console: persistent state, no menu/wizard dead ends ----------

@dataclasses.dataclass
class PendingAction:
    """Approval requested without consuming the next console command as input.

    Pending configuration is frozen at the moment of request. Read-only `check`
    commands pasted immediately after `generate` can be deferred until approval.
    """

    name: str
    settings: Settings
    digest: str = ""
    output_state: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None = None
    after: list[str] = dataclasses.field(default_factory=list)
    staged_turn: int = 0
    requires_distinct_turn: bool = False


class Console:
    def __init__(self, settings: Settings | None = None, allow_insecure: bool = False):
        self.settings = settings
        self.history: list[Settings] = []
        self.inspector = Inspector()
        self.planner = Planner()
        self.renderer = Renderer()
        self.pending: PendingAction | None = None
        self.allow_insecure = allow_insecure
        self.recent_projects: list[Path] = RecentProjectsStore.load()
        if settings:
            self.recent_projects = RecentProjectsStore.add(settings.target.root)
        self.ui_mode: str = "menu"
        self.browser_dir: Path = (settings.target.root if settings else Path.cwd()).resolve()
        self.browser_page: int = 0
        self.browser_filter: str = ""
        self.browser_items: list[Path] = []
        self.last_browse_items: list[Path] = []
        self.settings_stage: str = "main"
        self.orchestrator: DeploymentOrchestrator | None = DeploymentOrchestrator(settings.target.root) if settings else None
        self.input_turn: int = 0
        self.wizard_step: int = 1
        self.wizard_persist: bool = True
        self.wizard_awaiting_port: bool = False
        self.wizard_awaiting_command: bool = False

    def print_workbench_menu(self) -> None:
        probe = DockerHostProbe.probe(context=self.settings.docker_context if self.settings else None)
        engine_str = probe.summary
        proj_str = str(self.settings.target.root) if self.settings else f"{Path.cwd()} (Current terminal folder; not yet selected as project — Choose Option 1 or 2)"
        pinned_str = f" [{self.settings.target.pinned}]" if self.settings and self.settings.target.pinned else ""
        mode_str = self.settings.mode.value if self.settings else "auto"
        state_str = self.orchestrator.state.value if self.orchestrator else "DISCOVERED"
        insecure_str = " [ALLOW-INSECURE]" if self.allow_insecure else ""

        print("=" * 80)
        print(f"AUTO DOCKER {VERSION} — Container Infrastructure Workbench")
        print("=" * 80)
        print(f"Project   : {proj_str}{pinned_str}")
        print(f"Mode      : {mode_str} | Docker: {engine_str} | State: {state_str}{insecure_str}")
        print("-" * 80)
        print(" [A] Auto-Pilot: Guided Quick-Start Wizard (Detect -> Configure -> Safe Deploy)")
        print(" [1] Open Project / Quick Select (Paste path or open browser)")
        print(" [2] Interactive Project Browser (Navigate folders, pick entrypoint)")
        print(" [3] Recent Projects History")
        print(" [4] Intelligent Project Analysis (Auto-Detect)")
        print(" [5] Architecture & Readiness Recommendations")
        print(" [6] Customize Settings (Mode, Framework, Ports, GPU)")
        print(" [7] Preview Deployment Plan & Generated Files")
        print(" [8] Generate Container Infrastructure")
        print(" [9] View Deployment Plan (Phased ordering, service roles, risk)")
        print("[10] Review Preflight & Stage Deployment (Build/Start with Explicit Approval)")
        print("[11] System & Environment Doctor Diagnostics")
        print("[12] Docker Resource Cleaner & Recovery (Tests, projects, orphans)")
        print(" [0] Exit Workbench")
        print("-" * 80)
        print("Type [A] for Auto-Pilot Wizard, a number [0-12], or any command.")
        print("=" * 80)

    @staticmethod
    def _file_digest(files: dict[str, str]) -> str:
        # Deterministic fingerprint: plan must not change between request and approval.
        blob = json.dumps(files, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def _request(self, action: PendingAction, immediate: bool = False) -> None:
        self.pending = action
        if immediate:
            self._approve()
        else:
            print("Approval pending. At auto-docker> type YES to proceed, or cancel to abort.")
            print("Other commands do not count as YES; 'check' will wait until generation finishes.")

    def _approve(self) -> None:
        pending = self.pending
        if pending is None:
            print("Nothing is awaiting approval. Type generate to request it.")
            return
        # Consume the authorization only once, even if a subsequent command fails.
        self.pending = None
        if self.settings != pending.settings:
            raise OpsError("Settings changed while awaiting approval; request the action again.")
        root = pending.settings.target.root
        if pending.name == "generate":
            plan = self._plan()
            files = self.renderer.files(plan)
            manager = OutputManager(root)
            current_state = manager.state(files)
            comparable = tuple(tuple(values) for values in current_state)
            if self._file_digest(files) != pending.digest or comparable != pending.output_state:
                raise OpsError("Project/output changed while awaiting approval; run preview and generate again.")
            backup = manager.write(files)
            print(f"Generated: {manager.folder}")
            if backup:
                print(f"Previous configuration backed up: {backup}")
            for follow_up in pending.after:
                self.execute(follow_up)
            return
        if pending.name == "unlock":
            print("Removed stale lock: " + ProjectLock(root).clear_stale())
            return
        if pending.name == "recover":
            print("Recovered from: " + str(OutputManager(root).restore_last_backup()))
            return
        if pending.name in {"build", "up", "run", "job"}:
            valid, errors = OutputManager(root).validate()
            if not valid:
                raise OpsError("Generated files changed or missing: " + "; ".join(errors))
            if pending.name == "up":
                print("Checking planned host ports before start (advisory):")
                if pending.settings.docker_context:
                    print('Remote context: localhost ports are allocated on the Docker host, not checked locally.')
                else:
                    PortAudit.print_report(OutputManager(root).runtime_ports())
            kind = OutputManager(root).execution_kind()
            if pending.name == 'up' and kind == 'interactive':
                raise OpsError("Interactive application cannot start detached. Use run YES in a terminal.")
            if pending.name == 'run' and kind != 'interactive':
                raise OpsError("'run' is for interactive CLI applications. Use job or up for other projects.")
            if pending.name == 'job' and kind != 'batch':
                raise OpsError("'job' requires a batch application (kind batch).")
            DockerAdapter(root, pending.settings.docker_context).run(pending.name)
            if pending.name == "up":
                print("Docker start command completed. Run verify to inspect health and logs for failures.")
            else:
                print("Docker build: command completed")
            return
        if pending.name in {"native_up", "native_build", "native_down"}:
            action = pending.name.removeprefix("native_")
            curr_digest = DeploymentDigest.compute(
                root,
                pending.name,
                docker_context=pending.settings.docker_context,
                allow_insecure=self.allow_insecure,
            )
            if pending.digest and curr_digest != pending.digest:
                raise OpsError("Repository deployment configuration or files changed while awaiting approval; review and run option 10 again.")
            ExistingInfrastructure.apply(root, action, approved=True,
                                         context=pending.settings.docker_context,
                                         allow_insecure=self.allow_insecure,
                                         orchestrator=self.orchestrator)
            return
        raise OpsError("Unrecognized pending operation: " + pending.name)

    def _set(self, **changes: Any) -> None:
        if not self.settings:
            raise OpsError("Select a project first: project /path/to/app.py")
        self.history.append(self.settings)
        self.settings = dataclasses.replace(self.settings, **changes)

    def _inspect(self) -> Inspection:
        if not self.settings:
            raise OpsError("Select a project first: project /path/to/app.py")
        return self.inspector.inspect(self.settings.target, self.settings.entry)

    def _plan(self) -> Plan:
        if not self.settings:
            raise OpsError("Select a project first.")
        return self.planner.build(self.settings, self._inspect())

    @staticmethod
    def _summarize(plan: Plan) -> None:
        i = plan.inspection
        print(f"\nProject    : {i.root}\nEntrypoint : {i.entry.path}\nFramework  : {i.entry.framework}")
        print(f"Dependencies: {i.dependency or '(none)'}\nWorkload   : {plan.mode.value}")
        print(f"Services   : app" + (", " + ", ".join(plan.services) if plan.services else ""))
        print(f"Execution  : {plan.execution_kind}\nApp port   : {plan.port or '(none)'}\nCommand    : {shlex.join(plan.command)}")
        if plan.persistent_dirs:
            print("Persistent : " + ", ".join(f"./{d} -> /app/{d}" for d in plan.persistent_dirs))
        if plan.replicas and plan.replicas > 1:
            print(f"Replicas   : {plan.replicas}")
        if plan.cpus or plan.memory:
            print(f"Limits     : cpus={plan.cpus or 'unlimited'}, memory={plan.memory or 'unlimited'}")
        if plan.inferred_packages:
            print('Inferred pip installs (UNPINNED, opt-in): ' + ', '.join(plan.inferred_packages))
        if plan.published_ports:
            print('Host ports : ' + ', '.join(f'{p.service}={p.host}' for p in plan.published_ports))
        for warning in plan.warnings:
            print(f"WARNING: {warning}")

    def render_browser(self) -> None:
        self.ui_mode = "browser"
        base = self.browser_dir
        print(f"\nBrowsing directory: {base}")
        if self.browser_filter:
            print(f"Filter active: '{self.browser_filter}'")
        try:
            entries = list(base.iterdir())
        except OSError as exc:
            print(f"Could not browse directory {base}: {exc}")
            entries = []

        subdirs = sorted([p for p in entries if p.is_dir() and not p.name.startswith('.') and p.name not in {'__pycache__', 'node_modules', 'venv', '.venv', '.git'}], key=lambda x: x.name.lower())
        py_files = sorted([p for p in entries if p.is_file() and p.name.endswith('.py')], key=lambda x: x.name.lower())
        compose_files = sorted([p for p in entries if p.is_file() and p.name.lower() in {'compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml'}], key=lambda x: x.name.lower())

        if self.browser_filter:
            q = self.browser_filter.lower()
            subdirs = [p for p in subdirs if q in p.name.lower()]
            py_files = [p for p in py_files if q in p.name.lower()]
            compose_files = [p for p in compose_files if q in p.name.lower()]

        items: list[Path] = subdirs + compose_files + py_files
        self.browser_items = items
        self.last_browse_items = items

        page_size = 12
        total_items = len(items)
        total_pages = max(1, (total_items + page_size - 1) // page_size)
        if self.browser_page >= total_pages:
            self.browser_page = max(0, total_pages - 1)

        start = self.browser_page * page_size
        end = min(start + page_size, total_items)
        page_items = items[start:end]

        if not items:
            print("  (No matching subdirectories or Python files found)")
        else:
            for idx, item in enumerate(page_items, start + 1):
                kind = "[DIR] " if item.is_dir() else "[FILE]"
                print(f"  [{idx:2d}] {kind} {item.name}")

        print(f"\nPage {self.browser_page + 1} of {total_pages} (Total items: {total_items})")
        print("Commands: [number] enter/select | [..] parent | [use .] select folder | [n/p] page | [/search] filter | [0] menu")

    def _handle_browser_input(self, line: str) -> bool:
        line = line.strip()
        if not line:
            return True
        cmd = line.lower()
        if cmd in {"0", "b", "back", "menu"}:
            self.ui_mode = "menu"
            self.print_workbench_menu()
            return True
        if cmd in {"quit", "exit", "q"}:
            return False
        if cmd in {"..", "up"}:
            if self.browser_dir.parent != self.browser_dir:
                self.browser_dir = self.browser_dir.parent
                self.browser_page = 0
            self.render_browser()
            return True
        if cmd == "~":
            self.browser_dir = Path.home().resolve()
            self.browser_page = 0
            self.render_browser()
            return True
        if cmd in {"n", "next"}:
            self.browser_page += 1
            self.render_browser()
            return True
        if cmd in {"p", "prev", "previous"}:
            if self.browser_page > 0:
                self.browser_page -= 1
            self.render_browser()
            return True
        if cmd.startswith("/"):
            self.browser_filter = line[1:].strip()
            self.browser_page = 0
            self.render_browser()
            return True
        if cmd == "use .":
            target = self.browser_dir
            self.ui_mode = "menu"
            return self.execute(f"project '{target}'")
        if cmd.startswith("use "):
            target_str = line[4:].strip()
            if target_str.isdigit():
                idx = int(target_str) - 1
                if 0 <= idx < len(self.last_browse_items):
                    target = self.last_browse_items[idx]
                    if target.is_dir():
                        self.ui_mode = "menu"
                        return self.execute(f"project '{target}'")
                    print(f"Item [{target_str}] is a file, not a directory.")
                    return True
                print(f"Invalid index: {target_str}")
                return True
            p = Path(target_str.strip("\"'")).resolve()
            if p.is_dir():
                self.ui_mode = "menu"
                return self.execute(f"project '{p}'")
            print(f"Directory not found: {target_str}")
            return True

        if line.isdigit():
            idx = int(line) - 1
            if 0 <= idx < len(self.last_browse_items):
                target = self.last_browse_items[idx]
                if target.is_dir():
                    self.browser_dir = target
                    self.browser_page = 0
                    self.render_browser()
                    return True
                else:
                    print(f"Opening from browse list: {target}")
                    self.ui_mode = "menu"
                    res = self.execute(f"project '{target}'")
                    if self.settings and not self.settings.target.pinned:
                        stk = ExistingInfrastructure.discover(self.settings.target.root)
                        if stk.compose_files:
                            print("\n[Existing Infrastructure Stack Detected]")
                            self.execute("existing")
                            print("\n[Deployment Plan & Phasing]")
                            self.execute("native plan")
                    return res
            print(f"Invalid index {line}. Type a number from the list.")
            return True

        raw_cand = Path(line.strip("\"'"))
        cand = (self.browser_dir / raw_cand).resolve() if not raw_cand.is_absolute() else raw_cand.resolve()
        if cand.is_dir():
            self.browser_dir = cand
            self.browser_page = 0
            self.render_browser()
            return True
        elif cand.is_file():
            self.ui_mode = "menu"
            res = self.execute(f"project '{cand}'")
            if self.settings and not self.settings.target.pinned:
                stk = ExistingInfrastructure.discover(self.settings.target.root)
                if stk.compose_files:
                    print("\n[Existing Infrastructure Stack Detected]")
                    self.execute("existing")
                    print("\n[Deployment Plan & Phasing]")
                    self.execute("native plan")
            return res

        print(f"Unrecognized browser command: '{line}'. Type a number, '..', 'use .', or '0' for menu.")
        return True

    def render_settings(self) -> None:
        self.ui_mode = "settings"
        if not self.settings:
            print("Select a project first using option 1 or 2.")
            self.ui_mode = "menu"
            self.print_workbench_menu()
            return

        print("\n" + "=" * 80)
        print("AUTO DOCKER — Guided Settings Customizer")
        print("=" * 80)
        print(f"Project Target  : {self.settings.target.root}")
        print(f"Pinned Entry    : {self.settings.target.pinned or '(auto-detect)'}")
        print("-" * 80)
        print(f" [1] Workload Mode    : {self.settings.mode.value} (auto|base|mlops|llmops|genaiops)")
        print(f" [2] Entrypoint File  : {self.settings.entry or '(auto-detect)'}")
        print(f" [3] Container Port   : {self.settings.port or '(framework default)'}")
        print(f" [4] Published Ports  : {dict(self.settings.host_ports) if self.settings.host_ports else '(default)'}")
        print(f" [5] GPU Acceleration : {'ENABLED (Ollama)' if self.settings.gpu else 'OFF'}")
        print(f" [6] Execution Kind   : {self.settings.execution_kind}")
        print(f" [7] Dependencies     : {self.settings.dependency_mode} (strict | infer unpinned)")
        print(f" [8] Security Policy  : {'ALLOW INSECURE' if self.allow_insecure else 'BLOCK CRITICAL'}")
        print(f" [9] Scale (Replicas) : {getattr(self.settings, 'replicas', 1)}")
        print(f"[10] Resource Limits  : CPUs={getattr(self.settings, 'cpus', None) or 'unlimited'}, Memory={getattr(self.settings, 'memory', None) or 'unlimited'}")
        print("-" * 80)
        print("Quick Actions: enter [1-10] to customize, or direct command ('replicas 3', 'resources cpus=2 memory=4G').")
        print("Enter '0', 'b', or 'done' to return to Main Menu.")
        print("=" * 80)

    def _handle_settings_input(self, line: str) -> bool:
        line = line.strip()
        if not line:
            return True
        cmd = line.lower()
        if cmd in {"0", "b", "back", "done", "menu"}:
            self.ui_mode = "menu"
            self.settings_stage = "main"
            self.print_workbench_menu()
            return True
        if cmd in {"quit", "exit", "q"}:
            return False

        if self.settings_stage == "mode":
            mode_map = {"1": "auto", "2": "base", "3": "mlops", "4": "llmops", "5": "genaiops"}
            chosen = mode_map.get(cmd, cmd)
            if chosen in MODES:
                self.execute(f"mode {chosen}")
                self.settings_stage = "main"
                self.render_settings()
                return True
            print(f"Invalid mode: {cmd}. Choose 1-5 or auto/base/mlops/llmops/genaiops.")
            return True
        if self.settings_stage == "entry":
            self.execute(f"entry {line}")
            self.settings_stage = "main"
            self.render_settings()
            return True
        if self.settings_stage == "port":
            self.execute(f"port {line}")
            self.settings_stage = "main"
            self.render_settings()
            return True
        if self.settings_stage == "kind":
            kind_map = {"1": "auto", "2": "web", "3": "batch", "4": "interactive"}
            chosen = kind_map.get(cmd, cmd)
            self.execute(f"kind {chosen}")
            self.settings_stage = "main"
            self.render_settings()
            return True
        if self.settings_stage == "hostport":
            if cmd in {"auto", "reset"}:
                self._set(host_ports=())
                print("Host ports reset to default.")
            else:
                parts = line.split()
                if len(parts) == 2:
                    self.execute(f"hostport {parts[0]} {parts[1]}")
                elif len(parts) == 1 and parts[0].isdigit():
                    self.execute(f"hostport app {parts[0]}")
            self.settings_stage = "main"
            self.render_settings()
            return True

        if cmd == "1":
            print("\nSelect Workload Mode:")
            print("  [1] auto     — Auto-detect based on imports and dependencies")
            print("  [2] base     — Minimal Python application container")
            print("  [3] mlops    — Adds MLflow tracking service")
            print("  [4] llmops   — Adds local Ollama AI model runner")
            print("  [5] genaiops — Full stack (MLflow + Ollama + Redis cache)")
            print("Type number [1-5] or mode name:")
            self.settings_stage = "mode"
            return True
        if cmd == "2":
            print("\nEnter entrypoint file name (e.g. 'app.py') or 'auto' to clear:")
            self.settings_stage = "entry"
            return True
        if cmd == "3":
            print("\nEnter container port (e.g. 8000, 8080) or 'auto' for default:")
            self.settings_stage = "port"
            return True
        if cmd == "4":
            print("\nEnter published host port mapping (e.g. 'hostport app 8080' or 'auto'):")
            self.settings_stage = "hostport"
            return True
        if cmd == "5":
            if self.settings and self.settings.gpu:
                self.execute("gpu off")
            else:
                self.execute("gpu on")
            self.render_settings()
            return True
        if cmd == "6":
            print("\nSelect Execution Kind:")
            print("  [1] auto         — Detect from framework")
            print("  [2] web          — Web service (published port, healthcheck)")
            print("  [3] batch        — Batch job (exits on completion)")
            print("  [4] interactive  — Interactive CLI (attached TTY)")
            print("Type number [1-4] or kind name:")
            self.settings_stage = "kind"
            return True
        if cmd == "7":
            if self.settings and self.settings.dependency_mode == "strict":
                self.execute("deps infer")
            else:
                self.execute("deps strict")
            self.render_settings()
            return True
        if cmd == "8":
            if self.allow_insecure:
                self.execute("insecure off")
            else:
                self.execute("insecure on")
            self.render_settings()
            return True
        if cmd == "9":
            curr = getattr(self.settings, 'replicas', 1)
            next_rep = 2 if curr == 1 else (3 if curr == 2 else (4 if curr == 3 else 1))
            self._set(replicas=next_rep)
            self.render_settings()
            return True
        if cmd == "10":
            curr_mem = getattr(self.settings, 'memory', None)
            cycle = [None, "1G", "2G", "4G"]
            next_mem = cycle[(cycle.index(curr_mem) + 1) % len(cycle)] if curr_mem in cycle else None
            self._set(memory=next_mem)
            self.render_settings()
            return True

        res = self.execute(line)
        self.render_settings()
        return res

    def render_cleaner(self) -> None:
        self.ui_mode = "cleaner"
        probe = DockerHostProbe.probe(context=self.settings.docker_context if self.settings else None)
        engine_str = probe.summary
        proj_str = str(self.settings.target.root.name) if self.settings else "(no project selected)"
        print("\n" + "=" * 80)
        print("AUTO DOCKER — Resource Cleaner & Recovery")
        print("=" * 80)
        print(f"Target Project : {proj_str} | Docker: {engine_str}")
        print("-" * 80)
        print(" [1] Scan Docker Resources (Read-only discovery & ownership tags)")
        print(" [2] Preview Cleanup Plan (Inspect resources before any deletion)")
        print(" [3] Clean Temporary Test Resources (Safely remove verified test artifacts)")
        print(" [4] Clean Selected Project Resources (Stop & remove containers/networks for current project)")
        print(" [5] Recover Orphaned Resources (Detect & remove leftovers from crashed/interrupted runs)")
        print(" [0] Return to Main Menu")
        print("-" * 80)
        print("Type number [0-5] or '0' to return:")
        print("=" * 80)

    def _handle_cleaner_input(self, line: str) -> bool:
        cmd = line.strip().lower()
        if not cmd or cmd in ("0", "q", "quit", "exit", "back", "menu"):
            self.ui_mode = "menu"
            self.print_workbench_menu()
            return True

        prefix = ["docker"]
        if self.settings and self.settings.docker_context:
            prefix.extend(["--context", self.settings.docker_context])

        if cmd == "1":
            print("\nScanning Docker resources (read-only)...")
            resources = ResourceCleaner.scan(prefix=prefix)
            if not resources:
                print("No Docker resources detected (or Docker daemon unreachable).")
            else:
                print(f"Found {len(resources)} Docker resources:")
                for r in resources:
                    owner_tag = "[TEST]" if r.is_test else (f"[PROJECT: {r.project}]" if r.project else "[EXTERNAL/SHARED]")
                    print(f"  {r.kind:10s} : {r.name} (ID: {r.id[:12]}) {owner_tag} — {r.status}")
            print("\nType [0-5] to continue:")
            return True

        if cmd == "2":
            proj_name = self.settings.target.root.name if self.settings else None
            plan = ResourceCleaner.plan_cleanup(mode="project" if proj_name else "test", target_project=proj_name, prefix=prefix)
            print("\n" + ResourceCleaner.format_plan_preview(plan))
            print("\nType [0-5] to continue:")
            return True

        if cmd == "3":
            print("\nPlanning removal of verified temporary test resources...")
            plan = ResourceCleaner.plan_cleanup(mode="test", prefix=prefix, preserve_volumes=False)
            print(ResourceCleaner.format_plan_preview(plan))
            if plan.total_count == 0:
                print("No temporary test resources to clean.")
                return True
            confirm = input("Execute cleanup of temporary test resources? Type YES to proceed: ").strip()
            if confirm == "YES":
                res = ResourceCleaner.execute(plan, prefix=prefix)
                print(f"Result: {res.message}")
            else:
                print("Cleanup cancelled.")
            return True

        if cmd == "4":
            if not self.settings:
                print("Select a project first using option 1 or 2.")
                return True
            proj_name = self.settings.target.root.name
            print(f"\nPlanning removal of project resources for '{proj_name}'...")
            plan = ResourceCleaner.plan_cleanup(mode="project", target_project=proj_name, prefix=prefix, preserve_volumes=True)
            print(ResourceCleaner.format_plan_preview(plan))
            if plan.total_count == 0:
                print(f"No active resources found for project '{proj_name}'.")
                return True
            confirm = input(f"Execute cleanup for project '{proj_name}' (volumes protected)? Type YES: ").strip()
            if confirm == "YES":
                res = ResourceCleaner.execute(plan, prefix=prefix)
                print(f"Result: {res.message}")
            else:
                print("Cleanup cancelled.")
            return True

        if cmd == "5":
            print("\nScanning for orphaned Auto Docker resources...")
            plan = ResourceCleaner.plan_cleanup(mode="orphan", prefix=prefix, preserve_volumes=False)
            print(ResourceCleaner.format_plan_preview(plan))
            if plan.total_count == 0:
                print("No orphaned Auto Docker resources detected.")
                return True
            confirm = input("Recover and delete orphaned resources? Type YES: ").strip()
            if confirm == "YES":
                res = ResourceCleaner.execute(plan, prefix=prefix)
                print(f"Result: {res.message}")
            else:
                print("Orphan recovery cancelled.")
            return True

        print(f"Unrecognized cleaner command: '{cmd}'. Type 0 to return to menu, or 1-5.")
        return True

    def render_wizard(self) -> None:
        self.ui_mode = "wizard"
        if not self.settings:
            cwd = Path.cwd()
            has_py = any(cwd.glob("*.py"))
            has_compose = any((cwd / f).is_file() for f in ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml"))
            if has_py or has_compose:
                self.execute(f"project '{cwd}'")
            else:
                print("\n" + "=" * 80)
                print("AUTO DOCKER — Auto-Pilot Quick-Start Wizard")
                print("=" * 80)
                print("No project is currently selected.")
                print(f"Current terminal folder: {cwd}")
                print("-" * 80)
                print(" [1] Select current terminal folder")
                print(" [2] Browse folders (Interactive Browser)")
                print(" [0] Return to Main Menu")
                print("-" * 80)
                print("Type 1, 2, a project path, or '0' for menu:")
                print("=" * 80)
                return

        try:
            inspection = self._inspect()
            plan = self._plan()
        except OpsError as exc:
            print(f"\nConfiguration error: {exc}")
            self.ui_mode = "menu"
            self.print_workbench_menu()
            return

        if self.wizard_step == 1:
            detected_type = "Generic Python Application"
            if plan.mode == Mode.MLOPS:
                detected_type = "Enterprise AutoML / MLOps Engine"
            elif plan.mode == Mode.LLMOPS:
                detected_type = "LLMOps & Generative AI Application"
            elif plan.mode == Mode.GENAI:
                detected_type = "Full GenAI Application (LLM + Vector DB + Cache)"
            elif inspection.entry.framework != "python":
                detected_type = f"Web API / Dashboard ({inspection.entry.framework.capitalize()})"
            elif inspection.entry.needs_stdin:
                detected_type = "Interactive Terminal CLI / Console Tool"

            existing = ExistingInfrastructure.discover(self.settings.target.root)
            has_existing = bool(existing.compose_files or existing.dockerfiles)

            print("\n" + "=" * 80)
            print("AUTO DOCKER — Auto-Pilot Quick-Start Wizard [Step 1 of 3: Detection]")
            print("=" * 80)
            print(f"Project Target      : {self.settings.target.root}")
            print(f"Primary Entrypoint  : {inspection.entry.path} (Score: {inspection.entry.score})")
            print(f"Detected Workload   : {detected_type}")
            print(f"Framework / Engine  : {inspection.entry.framework}")
            print(f"Execution Style     : {plan.execution_kind.upper()} ({'interactive TTY' if plan.execution_kind == 'interactive' else 'background service'})")
            if plan.persistent_dirs:
                print(f"Persistent Storage  : Found {', '.join(plan.persistent_dirs)} (models & data will be preserved!)")
            else:
                print(f"Persistent Storage  : Standard container storage (no dedicated data folders detected)")
            if plan.services:
                print(f"Auxiliary Services  : {', '.join(plan.services)} (auto-recommended for {plan.mode.value})")
            if has_existing:
                print(f"Existing Stack      : Detected {len(existing.compose_files)} Compose file(s) and {len(existing.dockerfiles)} Dockerfile(s)")
            print("-" * 80)
            print("Press [ENTER] to proceed to Step 2 (Configuration), or type '0' for Main Menu:")
            print("=" * 80)
            return

        if self.wizard_step == 2:
            print("\n" + "=" * 80)
            print("AUTO DOCKER — Auto-Pilot Quick-Start Wizard [Step 2 of 3: Configuration]")
            print("=" * 80)
            print("Review and customize your deployment settings (press Enter to accept all):")
            print("-" * 80)
            print(f" [1] Workload Profile  : {self.settings.mode.value.upper()} (auto | base | mlops | llmops | genaiops)")
            print(f" [2] Execution Style   : {plan.execution_kind.upper()} (interactive | web | batch)")
            persist_str = f"ENABLED ({', '.join(plan.persistent_dirs)})" if plan.persistent_dirs else "DISABLED"
            print(f" [3] Persistent Data   : {persist_str}")
            svc_str = ", ".join(plan.services) if plan.services else "none"
            print(f" [4] Auxiliary Services: {svc_str}")
            print(f" [5] Application Port  : {plan.port or '(no published port)'}")
            print(f" [6] Hardware Accel    : {'NVIDIA GPU ENABLED' if self.settings.gpu else 'CPU Only'}")
            print(f" [7] Scale & Limits    : Replicas={getattr(self.settings, 'replicas', 1)} | CPUs={getattr(self.settings, 'cpus', None) or 'unlimited'} | Mem={getattr(self.settings, 'memory', None) or 'unlimited'}")
            dep_report = DependencyAudit().report(plan.inspection)
            dep_mode = getattr(self.settings, 'dependency_mode', 'strict')
            has_missing = bool(dep_report.possible_missing)
            if plan.inspection.dependency is None and has_missing:
                dep_info = "INFER ENABLED" if dep_mode == "infer" else f"STRICT (Unmanifested: {', '.join(dep_report.possible_missing[:3])}{'...' if len(dep_report.possible_missing) > 3 else ''})"
            else:
                dep_info = plan.inspection.dependency or "STRICT"
            print(f" [8] Dependencies      : {dep_mode.upper()} [{dep_info}]")
            cmd_override_str = " ".join(self.settings.command_override) if self.settings.command_override else f"AUTO ({' '.join(plan.command)})"
            print(f" [9] Execution Command : {cmd_override_str}")
            print("-" * 80)
            print("Quick Actions:")
            print("  Type 1-9 to toggle or edit any setting")
            print("  Press [ENTER] to accept all recommended settings and proceed to Step 3")
            print("  Type '0' or 'back' to return to Main Menu")
            print("=" * 80)
            return

        if self.wizard_step == 3:
            print("\n" + "=" * 80)
            print("AUTO DOCKER — Auto-Pilot Quick-Start Wizard [Step 3 of 3: Safe Deploy]")
            print("=" * 80)
            print("Deployment Plan & Preflight Safety Review:")
            print("-" * 80)
            print(f"Project Target       : {self.settings.target.root}")
            print(f"Base Image           : {plan.python_image}")
            print(f"Services to Launch   : app" + (f", {', '.join(plan.services)}" if plan.services else ""))
            print(f"Execution Command    : {' '.join(plan.command)}")
            if plan.replicas and plan.replicas > 1 or plan.cpus or plan.memory:
                print(f"Scale & Limits       : Replicas={plan.replicas}, CPUs={plan.cpus or 'unlimited'}, Memory={plan.memory or 'unlimited'}")
            if plan.published_ports:
                print(f"Published Host Ports : " + ", ".join(f"{p.service} -> 127.0.0.1:{p.host}" for p in plan.published_ports))
            else:
                print(f"Published Host Ports : (none)")
            if plan.persistent_dirs:
                print(f"Persistent Mounts    : " + ", ".join(f"./{d} -> /app/{d}" for d in plan.persistent_dirs))
            dep_report = DependencyAudit().report(plan.inspection)
            has_unmanifested = (
                plan.inspection.dependency is None
                and bool(dep_report.possible_missing)
                and getattr(self.settings, 'dependency_mode', 'strict') != 'infer'
            )
            print(f"Security Assessment  : LOW RISK (non-root, restricted loopback interfaces)")
            if has_unmanifested:
                missing_str = ", ".join(dep_report.possible_missing)
                print(f"Readiness Status     : RESOLUTION REQUIRED (Unmanifested dependencies detected: {missing_str})")
            else:
                print(f"Readiness Status     : PASS (prerequisites checked)")
            print("-" * 80)
            print("Choose deployment action:")
            print(" [1] Deploy & Run Now (Build container image and start all services)")
            print(" [2] Dry-Run Only (Validate Docker Compose execution without starting)")
            print(" [3] Generate Files Only (Inspect Dockerfile & compose.yaml in .opsforge/)")
            print(" [0] Return to Main Menu")
            print("=" * 80)
            return

    def _handle_wizard_input(self, line: str) -> bool:
        cmd = line.strip().lower()
        if cmd in {"0", "q", "quit", "exit", "back", "menu", "cancel"}:
            self.ui_mode = "menu"
            self.wizard_step = 1
            self.wizard_awaiting_port = False
            self.wizard_awaiting_command = False
            self.print_workbench_menu()
            return True

        if not self.settings:
            if line.strip() == "1":
                self.execute(f"project '{Path.cwd()}'")
            elif line.strip() == "2":
                self.render_browser()
                return True
            else:
                self.execute(f"project '{line.strip()}'")
            if self.settings:
                self.render_wizard()
            return True

        if self.wizard_awaiting_port:
            self.wizard_awaiting_port = False
            if cmd.isdecimal():
                self.execute(f"port {cmd}")
            else:
                print(f"Invalid port: {line}. Kept previous port.")
            self.render_wizard()
            return True

        if self.wizard_awaiting_command:
            self.wizard_awaiting_command = False
            raw = line.strip()
            if raw:
                if raw.lower() == "auto":
                    self._set(command_override=None)
                    print("Execution command reset to auto-detected.")
                else:
                    try:
                        parts = tuple(shlex.split(raw))
                        if not parts:
                            raise ValueError("Empty command")
                        self._set(command_override=parts)
                        print(f"Execution command updated to: {' '.join(parts)}")
                    except Exception as exc:
                        print(f"Invalid command format: {exc}. Kept previous command.")
            self.render_wizard()
            return True

        if self.wizard_step == 1:
            self.wizard_step = 2
            self.render_wizard()
            return True

        if self.wizard_step == 2:
            if not cmd or cmd in {"next", "continue", "c", "ok", "yes", "y"}:
                self.wizard_step = 3
                self.render_wizard()
                return True

            if cmd == "1":
                modes = [Mode.AUTO, Mode.MLOPS, Mode.LLMOPS, Mode.GENAI, Mode.BASE]
                curr = self.settings.mode
                idx = modes.index(curr) if curr in modes else 0
                next_mode = modes[(idx + 1) % len(modes)]
                self.execute(f"mode {next_mode.value}")
                self.render_wizard()
                return True

            if cmd == "2":
                kinds = ["interactive", "web", "batch"]
                curr = self.settings.execution_kind if self.settings.execution_kind in kinds else "interactive"
                next_kind = kinds[(kinds.index(curr) + 1) % len(kinds)]
                new_port = self.settings.port
                if next_kind == "web" and new_port is None:
                    new_port = 8000
                self._set(execution_kind=next_kind, port=new_port)
                self.render_wizard()
                return True

            if cmd == "3":
                if self.settings.persistent_dirs:
                    self._set(persistent_dirs=())
                else:
                    auto_dirs = [d for d in ("artifacts", "models", "runs", "checkpoints", "data", "datasets", "registry", "storage", "uploads", "outputs", "output", "logs", "weights", "db", "database") if (self.settings.target.root / d).is_dir()]
                    self._set(persistent_dirs=tuple(auto_dirs) if auto_dirs else ("artifacts",))
                self.render_wizard()
                return True

            if cmd == "4":
                if self.settings.services_override is not None and len(self.settings.services_override) > 0:
                    self.execute("services none")
                else:
                    self.execute("services mlflow")
                self.render_wizard()
                return True

            if cmd == "5":
                print("\nEnter container port [1024-65535] (or press Enter to keep): ")
                self.wizard_awaiting_port = True
                return True

            if cmd == "6":
                self.execute(f"gpu {'off' if self.settings.gpu else 'on'}")
                self.render_wizard()
                return True

            if cmd == "7":
                curr = getattr(self.settings, 'replicas', 1)
                next_rep = 2 if curr == 1 else (3 if curr == 2 else 1)
                self._set(replicas=next_rep)
                self.render_wizard()
                return True

            if cmd == "8":
                curr = getattr(self.settings, 'dependency_mode', 'strict')
                next_mode = 'infer' if curr != 'infer' else 'strict'
                self.execute(f"deps {next_mode}")
                self.render_wizard()
                return True

            if cmd == "9":
                plan = self._plan()
                print(f"\nCurrent command: {' '.join(plan.command)}")
                print("Enter custom execution command (e.g. 'python server.py --host 0.0.0.0 --port 8000')")
                print("Or enter 'auto' to revert to auto-detected command:")
                self.wizard_awaiting_command = True
                return True

            print(f"Unrecognized choice: '{cmd}'. Press Enter to proceed to Step 3, or choose 1-9.")
            return True

        if self.wizard_step == 3:
            if cmd == "1":
                plan = self._plan()
                dep_report = DependencyAudit().report(plan.inspection)
                has_unmanifested = (
                    plan.inspection.dependency is None
                    and bool(dep_report.possible_missing)
                    and getattr(self.settings, 'dependency_mode', 'strict') != 'infer'
                )
                if has_unmanifested:
                    missing_str = ", ".join(dep_report.possible_missing)
                    print("\n" + "!" * 80)
                    print("DEPLOYMENT BLOCKED — UNMANIFESTED DEPENDENCIES DETECTED")
                    print("!" * 80)
                    print(f"The project imports external packages ({missing_str}) but lacks a requirements.txt or pyproject.toml.")
                    print("Building now would produce a container missing required dependencies.")
                    print("\nResolution options:")
                    print("  1. Add a requirements.txt to your project root.")
                    print("  2. In Wizard Step 2, select Option [8] to enable 'deps infer' (auto-install recognized imports).")
                    print("  3. Or choose Option 3 (Generate Files Only) to inspect configuration without starting.")
                    print("!" * 80)
                    return True
                files = self.renderer.files(plan)
                action = "run" if plan.execution_kind == "interactive" else ("job" if plan.execution_kind == "batch" else "up")
                print("\n" + "=" * 80)
                print("AUTO DOCKER — Auto-Pilot Deployment Staged")
                print("=" * 80)
                print(f"Target Project     : {self.settings.target.root}")
                print(f"Planned Action     : {action.upper()} ({'interactive terminal session' if action == 'run' else 'background containers'})")
                print(f"Output Directory   : {self.settings.target.root / '.opsforge'}")
                print("-" * 80)
                print("Type YES to confirm deployment and start containers.")
                print("Type cancel to abort.")
                print("=" * 80)
                digest = self._file_digest(files)
                self.pending = PendingAction("generate", self.settings, digest=digest,
                                             output_state=tuple(tuple(values) for values in OutputManager(self.settings.target.root).state(files)),
                                             after=(action,), staged_turn=self.input_turn, requires_distinct_turn=True)
                self.ui_mode = "menu"
                self.wizard_step = 1
                return True

            if cmd == "2":
                self.execute("dryrun")
                self.ui_mode = "menu"
                self.wizard_step = 1
                return True

            if cmd == "3":
                self.execute("generate")
                self.ui_mode = "menu"
                self.wizard_step = 1
                return True

            print(f"Unrecognized choice: '{cmd}'. Choose 1 (Deploy), 2 (Dry-Run), 3 (Generate), or 0 (Menu).")
            return True

    def dispatch_input(self, command_line: str) -> bool:
        line = command_line.strip()
        if self.ui_mode == "wizard":
            return self._handle_wizard_input(line)
        if not line:
            return True
        if self.ui_mode == "browser":
            return self._handle_browser_input(line)
        if self.ui_mode == "settings":
            return self._handle_settings_input(line)
        if self.ui_mode == "cleaner":
            return self._handle_cleaner_input(line)
        return self.execute(line)

    def execute(self, line: str) -> bool:
        line = line.strip()
        if not line:
            return True
        name, _, arg = line.partition(" ")
        name = name.lower()
        arg = arg.strip()
        # One input reader only: confirmations are ordinary console commands,
        # never nested input() calls. Pasted multiline commands remain aligned.
        if name in {"yes", "y"}:
            if self.pending is not None and self.pending.requires_distinct_turn:
                if self.input_turn > 0 and self.pending.staged_turn == self.input_turn:
                    print("APPROVAL REJECTED: Deployment approval requires a distinct user input turn after the review screen is displayed.")
                    print("Pasted multiline inputs cannot trigger Docker mutation. Type YES on the next prompt to approve.")
                    return True
            self._approve()
            return True
        if name in {"cancel", "no"}:
            if self.pending is not None:
                print(f"Cancelled pending {self.pending.name}; settings preserved; no action performed.")
                self.pending = None
            else:
                print("Nothing to cancel.")
            return True
        if self.pending is not None:
            if name == "check" and self.pending.name == "generate":
                if "check" not in self.pending.after:
                    self.pending.after.append("check")
                print("Queued 'check' after generation. Type YES or cancel at auto-docker>.")
                return True
            if name not in {"help", "?", "h", "status", "doctor", "inspect", "preview", "preflight", "ports"}:
                print(f"Pending {self.pending.name} cancelled to process '{name}'.")
                self.pending = None
        if name in {"quit", "exit", "q"}:
            return False
        if name in {"menu", "home"}:
            self.print_workbench_menu()
            return True
        if name in {"insecure", "allow-insecure"}:
            if arg.lower() in {"on", "yes", "true", "1"}:
                self.allow_insecure = True
                print("Security override ENABLED: Operations on critical-risk containers are now permitted.")
            elif arg.lower() in {"off", "no", "false", "0"}:
                self.allow_insecure = False
                print("Security override DISABLED: Critical-risk containers will be blocked.")
            else:
                print(f"Allow-Insecure status: {'ENABLED' if self.allow_insecure else 'DISABLED'}")
                print("Use 'insecure on' or 'insecure off' to toggle.")
            return True
        # Numeric Workbench Options [0..11]
        if name == "0":
            return False
        if name == "1":
            if arg:
                return self.execute(f"project {arg}")
            self.ui_mode = "browser"
            self.render_browser()
            return True
        if name == "2":
            if arg and arg.isdigit():
                idx = int(arg) - 1
                if 0 <= idx < len(self.last_browse_items):
                    target = self.last_browse_items[idx]
                    print(f"Opening from browse list: {target}")
                    return self.execute(f"project '{target}'")
                print(f"Invalid index {arg}. Type '2' to view directory contents.")
                return True
            base = Path(arg).resolve() if arg else (self.settings.target.root if self.settings else Path.cwd())
            self.browser_dir = base
            self.browser_page = 0
            self.render_browser()
            return True
        if name == "3":
            self.recent_projects = RecentProjectsStore.load()
            if arg and arg.isdigit():
                idx = int(arg) - 1
                if 0 <= idx < len(self.recent_projects):
                    target = self.recent_projects[idx]
                    print(f"Switching to recent project: {target}")
                    return self.execute(f"project '{target}'")
                print(f"Invalid recent project index {arg}. Type '3' to view list.")
                return True
            print("\nRecent Projects:")
            if not self.recent_projects:
                print("  (No recent projects yet. Use option 1 to open a project.)")
            else:
                for idx, p in enumerate(self.recent_projects, 1):
                    current_mark = " (current)" if self.settings and self.settings.target.root == p else ""
                    print(f"  [{idx}] {p}{current_mark}")
                print("\nType '3 <NUMBER>' to switch to a recent project.")
            return True
        if name == "4":
            if not self.settings:
                print("Select a project first using option 1 or 2.")
                return True
            root = self.settings.target.root
            stack = ExistingInfrastructure.discover(root)
            if stack.compose_files and not self.settings.target.pinned:
                print("\n[Existing Infrastructure Stack Detected]")
                self.execute("existing")
                print("\n[Deployment Plan & Phasing]")
                self.execute("native plan")
            else:
                print("\n[Python Project Analysis]")
                self.execute("inspect")
                print("\n[Dependencies & Build Prerequisites]")
                self.execute("preflight")
            return True
        if name == "5":
            if not self.settings:
                print("Select a project first using option 1 or 2.")
                return True
            root = self.settings.target.root
            stack = ExistingInfrastructure.discover(root)
            if stack.compose_files and not self.settings.target.pinned:
                print("\n[Repository Compose Readiness Check]")
                self.execute("native ready")
            else:
                print("\n[Dependency and Port Readiness]")
                self.execute("preflight")
            return True
        if name == "6":
            if not self.settings:
                print("Select a project first using option 1 or 2.")
                return True
            if not arg:
                self.ui_mode = "settings"
                self.render_settings()
                return True
            subcmd, _, subarg = arg.partition(" ")
            subcmd = subcmd.lower()
            subarg = subarg.strip()
            if subcmd in {"mode", "port", "entry", "kind", "gpu", "hostport", "services", "deps"}:
                return self.execute(f"{subcmd} {subarg}")
            if subcmd in {"insecure", "allow-insecure"}:
                return self.execute(f"insecure {subarg}")
            print(f"Unrecognized setting: {subcmd}. Type '6' to open guided settings.")
            return True
        if name == "7":
            if not self.settings:
                print("Select a project first using option 1 or 2.")
                return True
            root = self.settings.target.root
            stack = ExistingInfrastructure.discover(root)
            if stack.compose_files and not self.settings.target.pinned:
                if arg and arg.lower() == 'json':
                    return self.execute("native plan --json")
                return self.execute("native plan")
            return self.execute(f"preview {arg}".strip())
        if name == "8":
            if not self.settings:
                print("Select a project first using option 1 or 2.")
                return True
            root = self.settings.target.root
            stack = ExistingInfrastructure.discover(root)
            if stack.compose_files and not self.settings.target.pinned:
                print("Existing Compose infrastructure detected. Auto Docker preserves existing repository stacks.")
                print("Use option 9 to inspect the plan, or option 10 to build/start containers.")
                return True
            return self.execute(f"generate {arg}".strip())
        if name == "9":
            if not self.settings:
                print("Select a project first using option 1 or 2.")
                return True
            root = self.settings.target.root
            stack = ExistingInfrastructure.discover(root)
            if stack.compose_files and not self.settings.target.pinned:
                return self.execute("native plan")
            plan = self._plan()
            self._summarize(plan)
            return True
        if name == "10":
            if not self.settings:
                print("Select a project first using option 1 or 2.")
                return True
            probe = DockerHostProbe.probe(context=self.settings.docker_context if self.settings else None)
            if not probe.cli_installed:
                print("\n" + "!" * 80)
                print("DOCKER ENGINE NOT DETECTED ON HOST PATH (CLI missing)")
                print("!" * 80)
                print("Auto Docker validated project structure, dependencies, and configuration offline.")
                print("However, executing container builds or running services requires Docker Engine.")
                print("\nNext Steps:")
                print("  1. Ensure Docker Desktop or Docker Engine is installed and running.")
                print("  2. Add 'docker' to your system PATH.")
                print("  3. Or connect to a remote Docker host via context: 'context <remote-name>'")
                print("!" * 80 + "\n")
                return True
            if not probe.compose_available:
                print(f"\nDocker Compose is unavailable: {probe.error_detail or 'compose plugin missing'}")
                return True
            if not probe.engine_reachable:
                print("\n" + "!" * 80)
                print("DOCKER ENGINE IS NOT RUNNING")
                print("!" * 80)
                print("The Docker CLI was found, but the Docker daemon/engine is not responding:")
                print(f"  {probe.error_detail or 'Connection refused or daemon stopped.'}")
                print("\nNext Steps:")
                print("  1. Start Docker Desktop or run 'sudo systemctl start docker'.")
                print("  2. Verify Docker is running with 'docker info'.")
                print("!" * 80 + "\n")
                return True

            root = self.settings.target.root
            stack = ExistingInfrastructure.discover(root)
            action = arg.lower() if arg in {"build", "down"} else "up"

            if stack.compose_files and not self.settings.target.pinned:
                ready = NativeReadiness.report(root)
                if not ready.ready:
                    print("\nBLOCKED: Native deployment prerequisites failed:")
                    for f in ready.failures:
                        print(f"  - {f}")
                    return True

                plan = ExistingInfrastructure.plan(root, docker_context=self.settings.docker_context)
                if plan.risk_classification == 'CRITICAL' and not self.allow_insecure:
                    print("\n" + "!" * 80)
                    print("SECURITY GATE: DEPLOYMENT BLOCKED")
                    print("!" * 80)
                    print("Critical container security risks detected:")
                    for w in plan.warnings:
                        print(f"  - {w}")
                    print("\nDeployment rejected. To override (at your own risk), type '6 insecure on'.")
                    print("!" * 80 + "\n")
                    return True

                print("\n" + "=" * 80)
                print("PREFLIGHT DEPLOYMENT REVIEW — Existing Repository Infrastructure")
                print("=" * 80)
                print(f"Repository Root    : {root}")
                print(f"Selected Compose   : {stack.selected_compose}")
                print(f"Risk Classification: {plan.risk_classification}")
                print(f"Planned Action     : docker compose {action}" + (" -d --no-build" if action == "up" else ""))
                print(f"Services ({len(plan.services)})     : {', '.join(s.name for s in plan.services)}")
                if plan.published_ports:
                    ports_str = ', '.join(f"{p.service}:{p.host}->{p.container}" for p in plan.published_ports)
                    print(f"Published Ports    : {ports_str}")
                if plan.warnings:
                    print("Warnings:")
                    for w in plan.warnings:
                        print(f"  [!] {w}")
                print("-" * 80)
                pending_act = f"native_{action}"
                digest = DeploymentDigest.compute(
                    root,
                    pending_act,
                    docker_context=self.settings.docker_context,
                    allow_insecure=self.allow_insecure,
                )
                self.pending = PendingAction(
                    pending_act,
                    self.settings,
                    digest=digest,
                    staged_turn=self.input_turn,
                    requires_distinct_turn=True,
                )
                print(f"Plan Fingerprint: {digest[:16]}...{digest[-8:]}")
                print("\n[STAGED FOR OPERATOR APPROVAL]")
                print(f"Selecting option 10 performed ZERO Docker mutations.")
                print(f"To execute 'docker compose {action}', type YES at the prompt.")
                print("To cancel without making changes, type cancel.")
                print("=" * 80)
                return True
            else:
                valid, errors = OutputManager(root).validate()
                if not valid:
                    print("\nGenerated files are missing or incomplete:")
                    for err in errors:
                        print(f"  - {err}")
                    print("Use option 8 (generate) first to create the container files.")
                    return True

                plan = self._plan()
                files = self.renderer.files(plan)
                digest = self._file_digest(files)

                print("\n" + "=" * 80)
                print("PREFLIGHT DEPLOYMENT REVIEW — Generated Container Infrastructure")
                print("=" * 80)
                print(f"Project Target  : {root}")
                print(f"Mode            : {plan.mode.value}")
                print(f"Execution Kind  : {plan.execution_kind}")
                print(f"Planned Action  : {action}")
                print(f"Services        : app" + (f", {', '.join(plan.services)}" if plan.services else ""))
                if plan.published_ports:
                    print(f"Host Ports      : {', '.join(f'{p.service}={p.host}' for p in plan.published_ports)}")
                print("-" * 80)
                self.pending = PendingAction(action, self.settings, digest=digest, staged_turn=self.input_turn, requires_distinct_turn=True)
                print(f"Configuration Digest: {digest[:16]}...{digest[-8:]}")
                print("\n[STAGED FOR OPERATOR APPROVAL]")
                print(f"Selecting option 10 performed ZERO Docker mutations.")
                print(f"To execute '{action}', type YES at the prompt.")
                print("To cancel without making changes, type cancel.")
                print("=" * 80)
                return True
        if name in {"a", "wizard", "quick", "autopilot", "auto-pilot"}:
            self.render_wizard()
            return True
        if name == "11":
            return self.execute("doctor")
        if name in {"12", "cleaner", "clean"}:
            self.render_cleaner()
            return True
        if name in {"help", "?", "h"}:
            print("\nproject PATH    Select project directory or a specific .py file")
            print("mode NAME       auto | base | mlops | llmops | genaiops")
            print("services LIST   mlflow,ollama,redis,prometheus,grafana | none | auto (optional overrides)")
            print("root [use]      Suggest/select a nearby parent Compose project root")
            print("readiness       Read-only native environment and storage prerequisite check")
            print("existing        Read-only inventory of existing Compose/Docker/Helm (no source scan)")
            print("native ACTION   inspect|plan|check|ready|build|up|down|ps|logs [COMPOSE_FILENAME] [YES]")
            print("                Preserves repository's own deployment stack; 'native plan' shows deployment plan")
            print("doctor          Check sandbox AND Flatpak host Docker availability (read-only)")
            print("context NAME|auto   Choose a preconfigured Docker context for runtime commands")
            print("contexts        List configured Docker contexts (read-only)")
            print("dryrun          Docker Compose dry-run of up --build; requires Engine")
            print("diagnose        Inspect runtime health with suggestions (read-only)")
            print("smoke /path     Read-only localhost HTTP response probe for a deployed web app")
            print("audit           Offline inspection of generated deployment files (read-only)")
            print("preflight       Audit imported dependencies and Docker build prerequisites")
            print("deps strict|infer  Default strict; opt-in inferred pip packages if no manifest")
            print("entry FILE      Choose specific .py file inside project; 'entry auto' clears it")
            print("kind TYPE       auto | web | batch | interactive (execution contract)")
            print("run [YES]       Foreground interactive application (TTY required)")
            print("job [YES]       Run a batch job, report exit code; container removed")
            print("cmd COMMAND     Override entrypoint with an exec-form command (cmd auto resets)")
            print("health /path    Set web-app HTTP readiness endpoint; health auto uses TCP")
            print("python-image REF  Override Python base image; python-image auto resets")
            print("image SERVICE REF  Override optional service image (image SERVICE auto resets)")
            print("port N|auto     Set app's internal port, or detect framework default")
            print("hostport NAME N  Set published host port (app/mlflow/ollama/prometheus/grafana); use 'auto' to reset")
            print("replicas COUNT  Set application container replicas in compose.yaml deploy.replicas")
            print("resources [cpus=X] [memory=Y]  Set application CPU and memory limits (or 'resources none')")
            print("ports           Check local host ports for potential conflicts (read-only)")
            print("gpu on|off      NVIDIA GPU reservation for Ollama")
            print("inspect         Analyze Python project without running its code")
            print("preview [full]  Show plan and generated file changes (full displays contents)")
            print("generate [YES]  Prepare files; approve with YES (or generate YES directly)")
            print("YES / cancel    Approve or cancel a pending operation; no nested prompts")
            print("check           Verify output hashes and optionally Docker Compose syntax")
            print("build/up/down   Explicitly build/start/stop containers; build/up need YES")
            print("ps/logs         Show container status and recent logs")
            print("verify          Check live container state and health (read-only)")
            print("recover         Restore last valid backup if .opsforge/ is missing")
            print("lock            Inspect the lock owner and whether automatic recovery is safe")
            print("unlock          Clear a verified stale lock after confirmation")
            print("status          Show current settings; back restores previous settings")
            print("quit            Exit at any time\n")
            return True
        if name == "project":
            if not arg:
                raise OpsError("Usage: project '/path/with spaces/app.py'")
            base = self.settings.target.root if self.settings else Path(__file__).resolve().parent
            new = Settings(target_for(arg, base), mode=self.settings.mode if self.settings else Mode.AUTO,
                           gpu=self.settings.gpu if self.settings else False,
                           services_override=self.settings.services_override if self.settings else None,
                           docker_context=self.settings.docker_context if self.settings else None)
            if self.settings:
                self.history.append(self.settings)
            self.settings = new
            self.allow_insecure = False
            self.pending = None
            self.recent_projects = RecentProjectsStore.add(new.target.root)
            self.browser_dir = new.target.root.resolve()
            self.orchestrator = DeploymentOrchestrator(new.target.root)
            print(f"Selected: {new.target.root}" + (f" | {new.target.pinned}" if new.target.pinned else ""))
            if not new.target.pinned and ExistingInfrastructure.discover(new.target.root).compose_files:
                print("Existing Compose infrastructure found. Use 'existing' or 'native inspect'; generator will not replace it.")
            elif not new.target.pinned:
                suggested = ProjectRootDiscovery.suggest(new.target.root)
                if suggested and suggested != new.target.root:
                    print(f'Found Compose at parent project root: {suggested}')
                    print("Use 'root use' to select it explicitly; nothing was switched automatically.")
            return True
        if name == "back":
            if self.history:
                self.settings = self.history.pop()
                print("Previous settings restored.")
            else:
                print("No previous settings. Type help to see available commands.")
            return True
        if name == "root":
            if not self.settings:
                raise OpsError('Select a project first.')
            suggested = ProjectRootDiscovery.suggest(self.settings.target.root)
            if suggested is None:
                print('No Compose project root found among nearby ancestors.')
            elif arg.lower() == 'use':
                if suggested == self.settings.target.root:
                    print(f'Already at project root: {suggested}')
                else:
                    self.history.append(self.settings)
                    self.settings = dataclasses.replace(self.settings, target=target_for(str(suggested)), entry=None)
                    print(f'Selected project root: {suggested} (existing deployment preserved).')
            elif not arg:
                print(f'Suggested Compose project root: {suggested}; use root use to select it.')
            else:
                raise OpsError('Usage: root [use]')
            return True
        if name in {'readiness', 'ready'}:
            if not self.settings:
                raise OpsError('Select a project first.')
            report = NativeReadiness.report(self.settings.target.root)
            if not report.ready:
                raise OpsError('Native deployment readiness is BLOCKED; no files were changed.')
            return True
        if name == "status":
            print(f"Project: {self.settings.target.root if self.settings else '(not selected)'}")
            if self.settings:
                print(f"Pinned file: {self.settings.target.pinned or '(none)'}")
                custom = self.settings.services_override
                print(f"Mode: {self.settings.mode.value} | Entry: {self.settings.entry or 'auto'} | "
                      f"Port: {self.settings.port or 'auto'} | GPU: {'on' if self.settings.gpu else 'off'}")
                print(f"Docker context: {self.settings.docker_context or 'CLI default'}")
                print(f"Command override: {self.settings.command_override or '(auto)'}")
                print(f"HTTP health path: {self.settings.health_path or '(TCP/default)'}")
                print(f"Python image: {self.settings.python_image or '(default)'}")
                print(f"Service images: {dict(self.settings.service_images) or '(defaults)'}")
                print(f"Dependencies: {self.settings.dependency_mode} "
                      "(inference is opt-in and unpinned)")
                print(f"Optional services: {','.join(custom) if custom else 'none' if custom == () else 'mode default'}")
                print(f"Replicas: {self.settings.replicas} | CPUs: {self.settings.cpus or 'unlimited'} | Memory: {self.settings.memory or 'unlimited'}")
                print('Host ports: ' + (', '.join(f'{k}={v}' for k, v in self.settings.host_ports) or 'auto'))
            return True
        if name == "mode":
            if arg not in MODES:
                raise OpsError("Choose mode: auto, base, mlops, llmops, genaiops")
            self._set(mode=Mode(arg), services_override=None, host_ports=())
            print(f"Mode: {arg} (services reset to mode default)")
            return True
        if name == "context":
            if not arg:
                raise OpsError("Usage: context NAME|auto")
            selected = None if arg == 'auto' else DockerAdapter.validate_context(arg)
            self._set(docker_context=selected)
            print('Docker context: ' + (selected or 'CLI default') + '. CLI default was not modified.')
            if selected:
                print('Remote Docker ports belong to the Engine host, not necessarily this terminal. Use trusted SSH/TLS contexts.')
            return True
        if name == "contexts":
            cli = DockerAdapter(self.settings.target.root if self.settings else Path.cwd())._docker()
            result = subprocess.run([cli, 'context', 'ls'], timeout=15, check=False)
            if result.returncode:
                raise OpsError('Could not list Docker contexts.')
            return True
        if name == "deps":
            if arg not in {"strict", "infer"}:
                raise OpsError("Usage: deps strict|infer  (infer explicitly installs unpinned recognized imports)")
            self._set(dependency_mode=arg)
            print(f"Dependency policy: {arg}" +
                  (". Inferred packages will be visible in preview; confirm generation with YES."
                   if arg == "infer" else ". No dependencies will be guessed or installed."))
            return True
        if name == "services":
            if not arg:
                raise OpsError("Usage: services mlflow,ollama | none | auto")
            custom = parse_services(arg)
            self._set(services_override=custom, host_ports=())
            print("Optional services: " + ("mode default" if custom is None else ", ".join(custom) or "none"))
            return True
        if name in {"existing", "native"}:
            if not self.settings:
                raise OpsError('Select a project directory first: project /path/to/repository')
            args = shlex.split(arg)
            if name == 'existing':
                if args:
                    raise OpsError("Usage: existing (read-only inventory)")
                ExistingInfrastructure.apply(self.settings.target.root, 'inspect',
                                             orchestrator=self.orchestrator)
                return True
            if not args:
                raise OpsError('Usage: native inspect|plan|check|ready|build|up|down|ps|logs|verify [FILE] [YES] [--allow-insecure]')
            action = args.pop(0).lower()
            if action not in {'inspect', 'plan', 'check', 'ready', 'build', 'up', 'down', 'ps', 'logs', 'verify'}:
                raise OpsError('native actions: inspect, plan, check, ready, build, up, down, ps, logs, verify')
            json_output = '--json' in args
            args = [a for a in args if a != '--json']
            allow_insecure = self.allow_insecure or ('--allow-insecure' in args)
            args = [a for a in args if a != '--allow-insecure']
            approval = bool(args and args[-1] == 'YES')
            if approval:
                args.pop()
            if len(args) > 1:
                raise OpsError('Usage: native ' + action + ' [COMPOSE_FILENAME] [YES] [--allow-insecure]')
            choice = args[0] if args else None
            if action in {'build', 'up', 'down'}:
                print('Explicit operation on repository-owned Compose infrastructure; no files are generated.')
            ExistingInfrastructure.apply(self.settings.target.root, action,
                                         choice=choice, approved=approval,
                                         context=self.settings.docker_context,
                                         json_output=json_output,
                                         allow_insecure=allow_insecure,
                                         orchestrator=self.orchestrator)
            return True
        if name == "doctor":
            EnvironmentDoctor().print_report(self.settings.target if self.settings else None,
                                             self.settings.docker_context if self.settings else None)
            return True
        if name == "preflight":
            if (self.settings and not self.settings.target.pinned and
                    ExistingInfrastructure.discover(self.settings.target.root).compose_files):
                ExistingInfrastructure.apply(self.settings.target.root, 'inspect', orchestrator=self.orchestrator)
                NativeReadiness.report(self.settings.target.root)
                print('Native deployment: project-managed dependencies; generator inference is not applied.')
                return True
            plan = self._plan()
            self._summarize(plan)
            report = DependencyAudit().report(plan.inspection)
            if plan.inferred_packages:
                print("Dependency inference enabled for generated image: " + ", ".join(plan.inferred_packages))
                report = dataclasses.replace(report, warnings=tuple(
                    "No manifest: opted-in inferred packages will be installed (unpinned)."
                    if "strict mode installs no third-party" in warning else warning
                    for warning in report.warnings))
            DependencyAudit.print_report(report)
            if self.settings.docker_context:
                print('Remote context selected: local host port probing is not applicable; inspect Docker host ports.')
            else:
                PortAudit.print_report(plan.published_ports)
            return True
        if name == "entry":
            if not arg:
                raise OpsError("Usage: entry filename.py  (or 'entry auto')")
            self._set(entry=None if arg.lower() == "auto" else arg.strip('"\''))
            print(f"Entrypoint override: {self.settings.entry or 'auto'}")
            return True
        if name == "kind":
            if arg not in EXECUTION_KINDS:
                raise OpsError("Usage: kind auto|web|batch|interactive")
            self._set(execution_kind=arg)
            print("Execution contract: " + arg)
            return True
        if name == "port":
            if arg.lower() == "auto":
                self._set(port=None)
            elif arg.isdigit() and 1024 <= int(arg) <= 65535:
                self._set(port=int(arg))
            else:
                raise OpsError("Usage: port 8080  (or 'port auto')")
            return True
        if name in {'hostport', 'host-port'}:
            items = arg.split()
            if len(items) != 2 or items[0] not in ('app', *SERVICE_ORDER):
                raise OpsError(f'Usage: hostport app|{"|".join(SERVICE_ORDER)} 8080|auto')
            service, value = items
            if value != 'auto' and (not value.isdecimal() or not 1024 <= int(value) <= 65535):
                raise OpsError('Host port must be 1024..65535 or auto.')
            if not self.settings:
                raise OpsError('Select a project first.')
            overrides = dict(self.settings.host_ports)
            if value == 'auto':
                overrides.pop(service, None)
            else:
                overrides[service] = int(value)
            self._set(host_ports=tuple(sorted(overrides.items())))
            print('Host port overrides: ' + (str(overrides) if overrides else 'auto'))
            return True
        if name == 'ports':
            plan = self._plan()
            if self.settings.docker_context:
                print('Remote context selected: these ports bind on the Docker Engine host:')
                for port in plan.published_ports:
                    print(f'  {port.service}: 127.0.0.1:{port.host} -> {port.container} (remote host)')
            else:
                PortAudit.print_report(plan.published_ports)
            return True
        if name == 'cmd':
            if not arg:
                raise OpsError('Usage: cmd python -m module --flag; use cmd auto to reset')
            self._set(command_override=None if arg.lower() == 'auto' else tuple(shlex.split(arg)))
            return True
        if name == 'health':
            if not arg:
                raise OpsError('Usage: health /ready or health auto')
            self._set(health_path=None if arg.lower() == 'auto' else arg)
            return True
        if name == 'python-image':
            if not arg:
                raise OpsError('Usage: python-image IMAGE or python-image auto')
            self._set(python_image=None if arg.lower() == 'auto' else arg)
            return True
        if name == 'image':
            items = arg.split(maxsplit=1)
            if len(items) != 2 or items[0] not in SERVICE_ORDER:
                raise OpsError(f'Usage: image {"|".join(SERVICE_ORDER)} IMAGE|auto')
            images = dict(self.settings.service_images)
            if items[1] == 'auto':
                images.pop(items[0], None)
            else:
                images[items[0]] = items[1]
            self._set(service_images=tuple(sorted(images.items())))
            return True
        if name == "gpu":
            if arg not in {"on", "off"}:
                raise OpsError("Usage: gpu on|off")
            self._set(gpu=arg == "on")
            return True
        if name in {"replicas", "replica"}:
            if not arg:
                raise OpsError("Usage: replicas COUNT (e.g. replicas 3)")
            try:
                count = int(arg)
                if count < 1 or count > 64:
                    raise ValueError
            except ValueError:
                raise OpsError("Replicas must be an integer between 1 and 64.")
            self._set(replicas=count)
            print(f"Application replicas set to {count}.")
            return True
        if name in {"resources", "limits"}:
            if not arg:
                curr_c = getattr(self.settings, 'cpus', None) or 'unlimited'
                curr_m = getattr(self.settings, 'memory', None) or 'unlimited'
                print(f"Current limits: cpus={curr_c}, memory={curr_m}")
                print("Usage: resources cpus=1.5 memory=2G  (or 'resources none' to reset)")
                return True
            if arg.strip().lower() in {"none", "off", "reset", "clear"}:
                self._set(cpus=None, memory=None)
                print("Resource limits cleared (unlimited).")
                return True
            cpus = getattr(self.settings, 'cpus', None)
            memory = getattr(self.settings, 'memory', None)
            for part in arg.split():
                if "=" not in part:
                    raise OpsError("Usage: resources cpus=1.5 memory=2G (or 'resources none')")
                k, v = part.split("=", 1)
                k = k.strip().lower()
                v = v.strip()
                if k in ("cpu", "cpus"):
                    if v.lower() in ("none", "auto", "unlimited"):
                        cpus = None
                    else:
                        try:
                            val = float(v)
                            if val <= 0 or val > 256:
                                raise ValueError
                            cpus = str(v)
                        except ValueError:
                            raise OpsError("CPUs limit must be a positive number (e.g. 1.5, 2, 0.5).")
                elif k in ("mem", "memory"):
                    if v.lower() in ("none", "auto", "unlimited"):
                        memory = None
                    else:
                        if not re.fullmatch(r"^[0-9]+(\.[0-9]+)?(b|k|m|g|t|kb|mb|gb|tb|ki|mi|gi|ti)?$", v, re.IGNORECASE):
                            raise OpsError("Memory limit must be a valid size (e.g. 512M, 2G, 4096M).")
                        memory = str(v)
                else:
                    raise OpsError(f"Unknown resource key: {k}. Supported keys: cpus, memory.")
            self._set(cpus=cpus, memory=memory)
            print(f"Resource limits updated: cpus={cpus or 'unlimited'}, memory={memory or 'unlimited'}.")
            return True
        if name in {"inspect", "preview", "generate"}:
            if (self.settings and not self.settings.target.pinned and
                    ExistingInfrastructure.discover(self.settings.target.root).compose_files):
                if name == 'generate':
                    raise OpsError('This repository already owns its Docker/Compose stack. '
                                   "Do not replace it with a generic Python image; use 'native inspect' or 'native check'.")
                ExistingInfrastructure.apply(self.settings.target.root, 'inspect', orchestrator=self.orchestrator)
                return True
            if name == "generate" and arg and arg.upper() != "YES":
                raise OpsError("Usage: generate [YES]. Without YES, approve at the next auto-docker> prompt.")
            plan = self._plan()
            self._summarize(plan)
            if name == "inspect":
                print(f"Candidate files analyzed: {plan.inspection.python_count}")
                return True
            if name == "generate":
                dependency_report = DependencyAudit().report(plan.inspection)
                for warning in dependency_report.warnings:
                    if plan.inferred_packages and "strict mode installs no third-party" in warning:
                        print("DEPENDENCY NOTICE: Opt-in inferred packages will be installed (unpinned).")
                    else:
                        print("DEPENDENCY WARNING: " + warning)
                if dependency_report.possible_missing:
                    if plan.inferred_packages:
                        print("INFERENCE ENABLED: generated Dockerfile will install unpinned: "
                              + ", ".join(plan.inferred_packages))
                        print("Review these packages and use pinned requirements for reproducible builds.")
                    else:
                        print("POSSIBLY MISSING dependencies for Docker build: "
                              + ", ".join(dependency_report.possible_missing))
                        print("No inferred packages will be installed. Use 'deps infer' only after reviewing the plan.")
            files = self.renderer.files(plan)
            manager = OutputManager(plan.inspection.root)
            created, changed, same = manager.state(files)
            print(f"\nFiles at {manager.folder}:")
            for label, seq in (("CREATE", created), ("REPLACE", changed), ("UNCHANGED", same)):
                for filename in seq:
                    print(f"  {label:9s} {filename}")
            if name == "preview":
                if arg.lower() == "full":
                    for filename, content in files.items():
                        print(f"\n{'=' * 16} {filename} {'=' * 16}\n{content}")
                else:
                    print("Run 'preview full' to display generated content, or 'generate' to save.")
                return True
            if not created and not changed:
                print("Output already matches this plan; nothing to write.")
                return True
            if manager.folder.exists():
                manager._verify_owned()
            request = PendingAction("generate", self.settings,
                                    digest=self._file_digest(files),
                                    output_state=tuple(tuple(seq) for seq in (created, changed, same)))
            self._request(request, immediate=arg.upper() == "YES")
            return True
        if name == 'smoke':
            if not self.settings:
                raise OpsError('Select a project first.')
            if self.settings.docker_context:
                raise OpsError('Smoke is local-only; remote Docker host ports may be inaccessible here.')
            good, detail = AppSmoke.probe(self.settings.target.root, arg or '/')
            print(('PASS: ' if good else 'FAIL: ') + detail)
            if not good:
                raise OpsError('Application smoke test failed')
            return True
        if name == 'audit':
            if not self.settings:
                raise OpsError('Select a project first.')
            if not OfflineDeploymentAudit.print_report(self.settings.target.root):
                raise OpsError('Offline audit found problems; fix them before building.')
            return True
        if name == "lock":
            if not self.settings:
                raise OpsError("Select a project first.")
            print(ProjectLock(self.settings.target.root).status())
            return True
        if name in {"recover", "unlock"}:
            if not self.settings:
                raise OpsError("Select a project first.")
            if arg and arg.upper() != "YES":
                raise OpsError(f"Usage: {name} [YES].")
            root = self.settings.target.root
            if name == "unlock":
                print(f"Stale lock candidate: {ProjectLock(root).stale_reason()}")
            else:
                manager = OutputManager(root)
                if manager.folder.is_symlink() or manager.folder.exists():
                    raise OpsError(".opsforge/ already exists; recovery must not overwrite it.")
                # Ensure there really is an intact backup before asking.
                backup_candidates = list(root.glob(".opsforge-backup-*"))
                if not backup_candidates:
                    raise OpsError("No Auto Docker backups were found to recover.")
            self._request(PendingAction(name, self.settings), immediate=arg.upper() == "YES")
            return True
        if name == "check":
            if not self.settings:
                raise OpsError("Select a project first.")
            manager = OutputManager(self.settings.target.root)
            if (not manager.folder.exists() and not self.settings.target.pinned
                    and ExistingInfrastructure.discover(self.settings.target.root).compose_files):
                print('Checking repository-owned Compose file (not .opsforge output).')
                ExistingInfrastructure.apply(self.settings.target.root, 'check',
                                             context=self.settings.docker_context,
                                             orchestrator=self.orchestrator)
                return True
            valid, problems = manager.validate()
            for p in problems:
                print(f"ERROR: {p}")
            if not valid:
                raise OpsError("Generated files did not pass integrity checks.")
            print("Generated file integrity: OK")
            probe = DockerHostProbe.probe(context=self.settings.docker_context)
            if probe.compose_available:
                DockerAdapter(self.settings.target.root, self.settings.docker_context).run("check")
                print("Docker Compose syntax: OK")
            else:
                print("Docker Compose unavailable: Compose syntax check skipped, not passed.")
            return True
        if name in {"build", "up", "run", "job", "down", "ps", "logs", "verify", "dryrun", "diagnose"}:
            if not self.settings:
                raise OpsError("Select a project first.")
            valid, errors = OutputManager(self.settings.target.root).validate()
            if not valid:
                raise OpsError("Generated files changed or missing: " + "; ".join(errors))
            if name in {'verify', 'diagnose'}:
                adapter = DockerAdapter(self.settings.target.root, self.settings.docker_context)
                ok, lines = adapter.verify()
                for line in lines:
                    print(line)
                if name == 'diagnose':
                    for hint in adapter.health_hints(lines):
                        print('SUGGESTION: ' + hint)
                    print('Diagnostic is read-only; it did not collect logs or alter containers.')
                    return True
                if not ok:
                    raise OpsError('Containers not ready. Use diagnose or logs to inspect failures.')
                print('Container states acceptable; check application-specific endpoints separately.')
                return True
            if name in {"up", "build", "run", "job"}:
                if arg and arg.upper() != "YES":
                    raise OpsError(f"Usage: {name} [YES].")
                # Fail immediately when Docker is missing; do not ask users to
                # approve an operation that is impossible in this environment.
                kind = OutputManager(self.settings.target.root).execution_kind()
                if (name == 'up' and kind == 'interactive'):
                    raise OpsError("Interactive CLI cannot be started detached. Use run YES.")
                if name == 'run' and kind != 'interactive':
                    raise OpsError("Run is for interactive CLI; use job for batch and up for web services.")
                if name == 'job' and kind != 'batch':
                    raise OpsError("Job is for batch scripts only; change with kind batch if needed.")
                DockerAdapter(self.settings.target.root, self.settings.docker_context).preflight()
                if name == "up":
                    print('Checking planned host ports before start (advisory):')
                    if self.settings.docker_context:
                        print('Remote context: host ports refer to the Docker Engine machine; local port checks skipped.')
                    else:
                        PortAudit.print_report(OutputManager(self.settings.target.root).runtime_ports())
                    print("Starting containers will publish the planned localhost ports.")
                elif name == "build":
                    print("Building images may download dependencies and consume disk space.")
                else:
                    print("Foreground application execution; dependencies may start, but no ports are published by compose run.")
                self._request(PendingAction(name, self.settings), immediate=arg.upper() == "YES")
                return True
            adapter = DockerAdapter(self.settings.target.root, self.settings.docker_context)
            adapter.run(name)
            if name == 'up':
                print('Docker start command completed. Run verify to inspect health and logs for failures.')
            else:
                print(f"Docker {name}: command completed")
            return True
        raise OpsError(f"Unknown command: {name}. Type help.")

    def feed_input(self, text: str) -> bool:
        """Process a single user input turn containing one or more lines."""
        self.input_turn += 1
        for command_line in text.splitlines():
            if not self.dispatch_input(command_line):
                return False
        return True

    def loop(self) -> None:
        self.print_workbench_menu()
        while True:
            try:
                # One central input reader: zero nested input() calls anywhere in Auto Docker
                prompt = "auto-docker> " if self.ui_mode == "menu" else f"auto-docker [{self.ui_mode}]> "
                received = input(prompt)
                if not self.feed_input(received):
                    return
            except KeyboardInterrupt:
                if self.pending:
                    print(f"\nCancelled pending {self.pending.name}; no action performed.")
                    self.pending = None
                else:
                    if self.ui_mode != "menu":
                        self.ui_mode = "menu"
                        self.print_workbench_menu()
                    else:
                        print("\nCancelled current action; still in Auto Docker. Type quit to exit.")
                continue
            except EOFError:
                print("\nExiting.")
                break
            except OpsError as exc:
                print(f"ERROR: {exc}")
            except (PermissionError, OSError) as exc:
                print(f"Filesystem error (session still active): {exc}")


# ---------- Proof gates: deliberately separate simulated tests from Docker evidence ----------


class ProofEvidence:
    """Minimal machine-readable proof; no environment variables or container logs."""

    def __init__(self, suite: str, output: Path | None = None):
        self.suite = suite
        self.output = output
        self.records: list[dict[str, str]] = []
        self.started = dt.datetime.now(dt.timezone.utc).isoformat()

    def record(self, gate: str, status: str, detail: str) -> None:
        if status not in ("PASS", "FAIL", "BLOCKED"):
            raise OpsError("Invalid proof status")
        # No user code, subprocess output, tokens, or environment details in reports.
        detail = str(detail).replace("\n", " ")[:350]
        self.records.append({"gate": gate, "status": status, "detail": detail})
        print(f"{status}: {gate} — {detail}")

    def save(self) -> None:
        if not self.output:
            return
        document = {"schema": 1, "generator": "Auto Docker", "version": VERSION,
                    "suite": self.suite, "started": self.started,
                    "completed": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "records": self.records,
                    "overall": ("FAIL" if any(x['status'] == 'FAIL' for x in self.records) else
                                "BLOCKED" if any(x['status'] == 'BLOCKED' for x in self.records) else
                                "PASS" if self.records else "BLOCKED")}
        output = self.output.expanduser().absolute()
        if output.is_symlink() or (output.exists() and not output.is_file()):
            raise OpsError("Proof report must be a regular file, not a symlink or directory")
        if not output.parent.is_dir():
            raise OpsError("Proof report parent directory does not exist")
        fd, name = tempfile.mkstemp(prefix='.auto-docker-proof-', dir=output.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(document, stream, sort_keys=True, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(name, 0o600)
            os.replace(name, output)
        finally:
            if os.path.lexists(name):
                os.unlink(name)

    def passed(self) -> bool:
        return bool(self.records) and all(x['status'] == 'PASS' for x in self.records)


def _proof_engine() -> list[str]:
    if EnvironmentDoctor.in_flatpak():
        raise OpsError('Real Docker proof is unavailable in the Flatpak sandbox; run on a Docker-enabled host.')
    adapter = DockerAdapter(Path.cwd())
    prefix = adapter._prefix()
    for label, command in (("Compose", [*prefix, "compose", "version"]),
                           ("Engine", [*prefix, "info", "--format", "{{.ServerVersion}}"] )):
        ok, detail = EnvironmentDoctor._probe(command)
        if not ok:
            raise OpsError(f'{label} unavailable: {detail}')
    # HTTP proof targets are localhost; remote Docker Engine != local machine.
    if os.environ.get('DOCKER_HOST') and not os.environ['DOCKER_HOST'].startswith('unix://'):
        raise OpsError('Real proof requires a local Docker Engine, not DOCKER_HOST over SSH/TCP.')
    result = _proof_command([*prefix, 'context', 'inspect',
                            '--format', '{{.Endpoints.docker.Host}}'], Path.cwd(), timeout=15)
    if result.returncode or not result.stdout.strip().startswith('unix://'):
        raise OpsError('Real proof requires a local Unix-socket Docker context; remote host ports are not local.')
    return prefix


def _proof_command(command: list[str], cwd: Path, *, timeout: int = 300,
                   stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                              input=stdin, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise OpsError(f'Docker proof command timed out after {timeout}s') from exc
    except OSError as exc:
        raise OpsError(f'Docker proof process error: {type(exc).__name__}') from exc


def _proof_ok(command: list[str], cwd: Path, *, timeout: int = 300) -> None:
    result = _proof_command(command, cwd, timeout=timeout)
    if result.returncode:
        # Do not print secrets or raw logs; report process status only.
        raise OpsError(f'Docker command failed (exit {result.returncode}): {command[-2:]}')


def _proof_tty(command: list[str], cwd: Path, *, timeout: float = 120) -> None:
    """Exercise a real pseudo-terminal, not Compose's -T pipe mode (POSIX only)."""
    if os.name != 'posix':
        raise OpsError('Real TTY test needs a POSIX host; cannot certify interactive TTY here.')
    import pty
    master, slave = pty.openpty()
    process: subprocess.Popen[bytes] | None = None
    transcript = bytearray()
    sent = False
    try:
        process = subprocess.Popen(command, cwd=cwd, stdin=slave, stdout=slave,
                                   stderr=slave, start_new_session=True)
        os.close(slave)
        slave = -1
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], 0.4)
            if readable:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                transcript.extend(chunk)
                if len(transcript) > 200_000:
                    raise OpsError('TTY proof output exceeded safe limit')
                if not sent and b'prompt>' in transcript:
                    os.write(master, b'hello\n')
                    sent = True
                if b'AUTO_DOCKER_CHAT_OK:hello' in transcript:
                    break
            if process.poll() is not None and not readable:
                break
        if process.poll() is None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired as exc:
                raise OpsError('TTY proof process did not exit') from exc
        if process.returncode != 0 or not sent or b'AUTO_DOCKER_CHAT_OK:hello' not in transcript:
            raise OpsError('TTY chat proof failed: no successful prompt/response/exit')
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if slave >= 0:
            os.close(slave)
        os.close(master)


def _free_local_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def _http_json(url: str, data: dict[str, Any] | None = None,
               timeout: int = 15) -> dict[str, Any]:
    # URL is constructed only from fixed localhost endpoints, not untrusted user URLs.
    body = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=body,
                                     headers={'Content-Type': 'application/json'} if body else {},
                                     method='POST' if body is not None else 'GET')
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status >= 400:
            raise OpsError(f'HTTP proof failed with status {response.status}')
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise OpsError('HTTP proof response exceeded safe size')
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise OpsError('API response was not a JSON object')
    return result


def _wait_http(url: str, timeout: int = 150) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(url)
            with urllib.request.urlopen(request, timeout=3) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(2)
            continue
        time.sleep(2)
    raise OpsError(f'HTTP service did not become ready within {timeout}s')


def _cleanup_proof(adapter: DockerAdapter, root: Path, *, volumes: bool = True) -> None:
    cmd = adapter._compose_command('down', '--remove-orphans', '--rmi', 'local',
                                   *(['--volumes'] if volumes else []))
    _proof_ok(cmd, root, timeout=180)
    leftover = _proof_command(adapter._compose_command('ps', '-a', '-q'), root, timeout=30)
    if leftover.returncode or leftover.stdout.strip():
        raise OpsError('Cleanup failed to remove every disposable test container')


def integration_test(repeat: int = 1, report: Path | None = None) -> bool:
    """Opt-in real-engine P0 proof: web HTTP, batch exit, true interactive TTY."""
    evidence = ProofEvidence('P0-real-docker', report)
    if repeat not in (1, 2, 3):
        raise OpsError('Proof repeat count must be 1, 2, or 3')
    try:
        try:
            _proof_engine()
        except OpsError as exc:
            evidence.record('docker-engine', 'BLOCKED', str(exc))
            raise
        fixtures = (
            ('web', """from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'AUTO_DOCKER_WEB_OK')
        else:
            self.send_error(404)
HTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
""", 'web'),
            ('batch', "print('AUTO_DOCKER_BATCH_OK')\n", 'batch'),
            ('chat', "print('AUTO_DOCKER_CHAT_OK:' + input('prompt> '))\n", 'interactive'),
        )
        with tempfile.TemporaryDirectory(prefix='auto-docker-real-proof-') as temporary:
            for cycle in range(repeat):
                for name, source, kind in fixtures:
                    gate = f'{cycle+1}/{repeat}:{name}'
                    root = (Path(temporary) / f'cycle-{cycle+1}' / name).resolve()
                    root.mkdir(parents=True)
                    (root / 'app.py').write_text(source, encoding='utf-8')
                    host_port = _free_local_port() if kind == 'web' else None
                    settings = Settings(Target(root, 'app.py'), mode=Mode.BASE,
                                        port=8000 if kind == 'web' else None,
                                        services_override=(),
                                        host_ports=(('app', host_port),) if host_port else (),
                                        execution_kind=kind)
                    plan = Planner().build(settings, Inspector().inspect(settings.target))
                    OutputManager(root).write(Renderer().files(plan))
                    adapter = DockerAdapter(root)
                    failure: str | None = None
                    try:
                        adapter.preflight()
                        _proof_ok(adapter._compose_command('build'), root, timeout=900)
                        if kind == 'web':
                            _proof_ok(adapter._compose_command('up', '-d'), root, timeout=300)
                            url = f'http://127.0.0.1:{host_port}/health'
                            _wait_http(url, timeout=120)
                            with urllib.request.urlopen(url, timeout=5) as response:
                                if response.status != 200 or response.read(128) != b'AUTO_DOCKER_WEB_OK':
                                    raise OpsError('HTTP body/status did not match expected output')
                            deadline = time.monotonic() + 90
                            while True:
                                ok, _ = adapter.verify()
                                if ok:
                                    break
                                if time.monotonic() > deadline:
                                    raise OpsError('Docker Compose did not report a ready web app')
                                time.sleep(2)
                        elif kind == 'batch':
                            result = _proof_command(adapter._compose_command('run', '--rm', '-T', 'app'), root,
                                                    timeout=120)
                            if result.returncode or 'AUTO_DOCKER_BATCH_OK' not in result.stdout:
                                raise OpsError('Batch job exit status or output incorrect')
                        else:
                            _proof_tty(adapter._compose_command('run', '--rm', 'app'), root)
                    except (OpsError, OSError, ValueError) as exc:
                        failure = f'{type(exc).__name__}: {exc}'
                    finally:
                        try:
                            _cleanup_proof(adapter, root)
                        except (OpsError, OSError) as exc:
                            failure = (failure + '; ' if failure else '') + f'cleanup: {exc}'
                    evidence.record(gate, 'FAIL' if failure else 'PASS',
                                    failure or f'real Docker image/build/execution/cleanup verified ({kind})')
        return evidence.passed()
    finally:
        evidence.save()


def ai_integration_test(model: str | None = None, pull: bool = False,
                        report: Path | None = None) -> bool:
    """Opt-in real P1 proof. Does not auto-pull models or remove existing volumes."""
    evidence = ProofEvidence('P1-ai-services', report)
    if model and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}', model):
        raise OpsError('Invalid model identifier')
    if pull and not model:
        raise OpsError('--pull-model requires an explicit --model name')
    try:
        try:
            _proof_engine()
        except OpsError as exc:
            evidence.record('docker-engine', 'BLOCKED', str(exc))
            raise
        with tempfile.TemporaryDirectory(prefix='auto-docker-ai-proof-') as tmp:
            root = Path(tmp).resolve()
            (root / 'app.py').write_text("print('ai proof')\n", encoding='utf-8')
            mlport = _free_local_port()
            olport = _free_local_port()
            while olport == mlport:
                olport = _free_local_port()
            settings = Settings(Target(root, 'app.py'), mode=Mode.GENAI,
                                execution_kind='batch',
                                host_ports=(('mlflow', mlport), ('ollama', olport)))
            plan = Planner().build(settings, Inspector().inspect(settings.target))
            OutputManager(root).write(Renderer().files(plan))
            adapter = DockerAdapter(root)
            failure: str | None = None
            try:
                adapter.preflight()
                _proof_ok(adapter._compose_command('build', 'app'), root, timeout=900)
                _proof_ok(adapter._compose_command('up', '-d', 'mlflow', 'ollama', 'redis'), root, timeout=1200)
                _wait_http(f'http://127.0.0.1:{mlport}/health', timeout=240)
                _wait_http(f'http://127.0.0.1:{olport}/api/tags', timeout=240)
                unique = secrets.token_hex(5)
                experiment_name = 'autodocker-proof-' + unique
                exp = _http_json(f'http://127.0.0.1:{mlport}/api/2.0/mlflow/experiments/create',
                                 {'name': experiment_name})
                exp_id = str(exp.get('experiment_id') or '')
                if not exp_id:
                    raise OpsError('MLflow did not return an experiment ID')
                run = _http_json(f'http://127.0.0.1:{mlport}/api/2.0/mlflow/runs/create',
                                 {'experiment_id': exp_id})
                run_id = run.get('run', {}).get('info', {}).get('run_id')
                if not run_id:
                    raise OpsError('MLflow did not create a run')
                _http_json(f'http://127.0.0.1:{mlport}/api/2.0/mlflow/runs/log-metric',
                           {'run_id': run_id, 'key': 'autodocker_proof', 'value': 1.0,
                            'timestamp': int(time.time() * 1000), 'step': 0})
                evidence.record('mlflow-api', 'PASS', 'experiment, run, and metric REST operations succeeded')
                key = 'autodocker-proof-' + unique
                result = _proof_command(adapter._compose_command('exec', '-T', 'redis', 'redis-cli',
                                                                   'SET', key, 'works'), root, timeout=30)
                if result.returncode or result.stdout.strip() != 'OK':
                    raise OpsError('Redis SET operation failed')
                result = _proof_command(adapter._compose_command('exec', '-T', 'redis', 'redis-cli',
                                                                   'GET', key), root, timeout=30)
                if result.returncode or result.stdout.strip() != 'works':
                    raise OpsError('Redis GET operation failed')
                evidence.record('redis-api', 'PASS', 'SET/GET via service container succeeded')
                # Verify Docker service discovery from the application network with stdlib only.
                script = """import socket,urllib.request
for service,port,path in [('mlflow',5000,'/health'),('ollama',11434,'/api/tags')]:
    with urllib.request.urlopen(f'http://{service}:{port}{path}', timeout=8) as r:
        assert r.status == 200
with socket.create_connection(('redis',6379),timeout=8) as s:
    s.sendall(b'*1\\r\\n$4\\r\\nPING\\r\\n')
    assert b'PONG' in s.recv(128)
print('AUTODOCKER_AI_NETWORK_OK')
"""
                result = _proof_command(adapter._compose_command('run', '--rm', '-T', '--no-deps',
                                                                   'app', 'python', '-c', script),
                                        root, timeout=180)
                if result.returncode or 'AUTODOCKER_AI_NETWORK_OK' not in result.stdout:
                    raise OpsError('Application-to-service DNS/API connectivity failed')
                evidence.record('ai-network', 'PASS', 'app container reached all services by Compose DNS')
                # A real application-container write, not merely a successful DNS/TCP check.
                app_script = """import json,socket,urllib.request
payload=json.dumps({'name':'autodocker-app-proof-""" + unique + """'}).encode()
req=urllib.request.Request('http://mlflow:5000/api/2.0/mlflow/experiments/create',
    data=payload,headers={'Content-Type':'application/json'},method='POST')
with urllib.request.urlopen(req,timeout=15) as response:
    result=json.load(response)
assert result.get('experiment_id')
key='autodocker-app-""" + unique + """'
with socket.create_connection(('redis',6379),timeout=10) as c:
    def send(items):
        wire=b'*'+str(len(items)).encode()+b'\\r\\n'
        for item in items:
            data=item.encode()
            wire+=b'$'+str(len(data)).encode()+b'\\r\\n'+data+b'\\r\\n'
        c.sendall(wire)
    send(['SET',key,'works'])
    assert b'OK' in c.recv(256)
    send(['GET',key])
    assert b'works' in c.recv(256)
print('AUTODOCKER_AI_APP_OPERATIONS_OK')
"""
                result = _proof_command(adapter._compose_command('run', '--rm', '-T', '--no-deps',
                                            'app', 'python', '-c', app_script), root, timeout=180)
                if result.returncode or 'AUTODOCKER_AI_APP_OPERATIONS_OK' not in result.stdout:
                    raise OpsError('App container failed MLflow experiment creation / Redis set-get')
                evidence.record('ai-app-operations', 'PASS', 'application created MLflow experiment and Redis value')
                _proof_ok(adapter._compose_command('restart', 'mlflow', 'redis'), root, timeout=180)
                _wait_http(f'http://127.0.0.1:{mlport}/health', timeout=180)
                stored = _http_json(f'http://127.0.0.1:{mlport}/api/2.0/mlflow/experiments/get?experiment_id={exp_id}')
                if not stored.get('experiment'):
                    raise OpsError('MLflow experiment was not persisted across restart')
                result = _proof_command(adapter._compose_command('exec', '-T', 'redis', 'redis-cli',
                                                                   'GET', key), root, timeout=30)
                if result.returncode or result.stdout.strip() != 'works':
                    raise OpsError('Redis data was not persisted across restart')
                evidence.record('persistence', 'PASS', 'MLflow and Redis retained test data after restart')
                _proof_ok(adapter._compose_command('stop', 'redis'), root, timeout=90)
                # A stopped dependency must not be reported as reachable.
                down_check = _proof_command(adapter._compose_command('run', '--rm', '-T', '--no-deps',
                                                   'app', 'python', '-c',
                                                   'import socket; socket.create_connection(("redis",6379),timeout=2)'
                                                   ), root, timeout=90)
                if down_check.returncode == 0:
                    raise OpsError('Failure-injection test expected Redis to be unavailable')
                _proof_ok(adapter._compose_command('start', 'redis'), root, timeout=90)
                deadline = time.monotonic() + 80
                while True:
                    up_check = _proof_command(adapter._compose_command('exec', '-T', 'redis',
                                                               'redis-cli', 'PING'), root, timeout=20)
                    if up_check.returncode == 0 and up_check.stdout.strip() == 'PONG':
                        break
                    if time.monotonic() > deadline:
                        raise OpsError('Redis did not recover after a controlled restart')
                    time.sleep(2)
                evidence.record('outage-recovery', 'PASS', 'Redis outage detected, service restored')
                tags = _http_json(f'http://127.0.0.1:{olport}/api/tags')
                if not isinstance(tags.get('models'), list):
                    raise OpsError('Ollama tags returned invalid models array')
                evidence.record('ollama-api', 'PASS', 'Ollama /api/tags returned a valid response')
                if model:
                    if pull:
                        # Explicit opt-in; model download may require substantial disk/network usage.
                        _proof_ok(adapter._compose_command('exec', '-T', 'ollama', 'ollama', 'pull', model),
                                  root, timeout=2400)
                    response = _http_json(f'http://127.0.0.1:{olport}/api/generate',
                                          {'model': model, 'prompt': 'Reply with one short word.',
                                           'stream': False}, timeout=240)
                    if not str(response.get('response') or '').strip():
                        raise OpsError('Ollama failed to generate a nonempty model response')
                    # Prove inference from the application network, not only from host localhost.
                    model_script = ("import json,urllib.request; "
                        "body=json.dumps({'model':" + repr(model) +
                        ",'prompt':'Reply with one short word.','stream':False}).encode(); "
                        "req=urllib.request.Request('http://ollama:11434/api/generate', "
                        "data=body,headers={'Content-Type':'application/json'}); "
                        "data=json.load(urllib.request.urlopen(req,timeout=240)); "
                        "assert str(data.get('response','')).strip(); "
                        "print('AUTODOCKER_APP_LLM_OK')")
                    result = _proof_command(adapter._compose_command('run', '--rm', '-T',
                                               '--no-deps', 'app', 'python', '-c', model_script),
                                               root, timeout=300)
                    if result.returncode or 'AUTODOCKER_APP_LLM_OK' not in result.stdout:
                        raise OpsError('Real GenAI request failed from inside the application container')
                    evidence.record('model-inference', 'PASS',
                                    'real model answered on host and from inside app container')
                else:
                    evidence.record('model-inference', 'BLOCKED',
                                    'Provide --model NAME, optionally --pull-model to approve a download')
            except (OpsError, OSError, ValueError, KeyError, urllib.error.URLError) as exc:
                failure = f'{type(exc).__name__}: {exc}'
                evidence.record('ai-execution', 'FAIL', failure)
            finally:
                try:
                    _cleanup_proof(adapter, root, volumes=True)
                except (OpsError, OSError) as exc:
                    evidence.record('ai-cleanup', 'FAIL', str(exc))
                else:
                    evidence.record('ai-cleanup', 'PASS', 'disposable containers and volumes removed')
        return evidence.passed()
    finally:
        evidence.save()


@dataclasses.dataclass(frozen=True)
class CleanableResource:
    """Represents a discovered Docker resource with ownership and classification metadata."""
    kind: str  # 'container' | 'image' | 'network' | 'volume'
    id: str
    name: str
    project: str | None
    is_test: bool
    is_orphan: bool
    status: str
    details: str = ""
    labels: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class CleanupPlan:
    """Explicit, inspectable cleanup plan adhering to strict ownership safety rules."""
    mode: str  # 'test' | 'project' | 'orphan'
    target_project: str | None
    containers: tuple[CleanableResource, ...]
    images: tuple[CleanableResource, ...]
    networks: tuple[CleanableResource, ...]
    volumes: tuple[CleanableResource, ...]
    protected_volumes: tuple[CleanableResource, ...]

    @property
    def total_count(self) -> int:
        return len(self.containers) + len(self.images) + len(self.networks) + len(self.volumes)


@dataclasses.dataclass(frozen=True)
class CleanupResult:
    """Authoritative outcome of an executed cleanup plan."""
    success: bool
    cleaned: tuple[CleanableResource, ...]
    failed: tuple[tuple[CleanableResource, str], ...]
    leftovers: tuple[CleanableResource, ...]
    message: str


class ResourceCleaner:
    """Production-grade Docker Resource Cleaner with read-only discovery,
    ownership classification, preview staging, explicit approval gates,
    and authoritative removal verification.
    """

    @classmethod
    def _run_docker(cls, args: list[str], prefix: list[str] | None = None, timeout: int = 30) -> subprocess.CompletedProcess:
        cmd = list(prefix) if prefix else [shutil.which("docker") or "docker"]
        cmd.extend(args)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)

    @classmethod
    def get_project_identifiers(cls, project_path_or_name: str | Path | None) -> set[str]:
        """Resolves all valid project identifier aliases for a given path or name.
        Includes:
        - Exact directory name (e.g. 'my-app')
        - Lowercase slug (e.g. 'my-app')
        - Auto Docker compose project name: opsforge-{slug}-{fingerprint} (if path exists or can be resolved)
        - Exact name defined in .opsforge/compose.yaml if present
        """
        if not project_path_or_name:
            return set()
        identifiers: set[str] = set()
        raw_str = str(project_path_or_name).strip()
        identifiers.add(raw_str)
        identifiers.add(raw_str.lower())
        try:
            p = Path(raw_str).resolve()
            identifiers.add(p.name)
            identifiers.add(p.name.lower())
            slug = re.sub(r"[^a-z0-9-]", "-", p.name.lower()).strip("-")[:24] or "project"
            fingerprint = hashlib.sha256(str(p).encode()).hexdigest()[:7]
            identifiers.add(f"opsforge-{slug}-{fingerprint}")
            compose_file = p / ".opsforge" / "compose.yaml"
            if compose_file.is_file():
                try:
                    for line in compose_file.read_text(encoding="utf-8", errors="replace").splitlines():
                        if line.startswith("name:"):
                            c_name = line.partition("name:")[2].strip().strip('"').strip("'")
                            if c_name:
                                identifiers.add(c_name)
                            break
                except Exception:
                    pass
        except Exception:
            pass
        return {x for x in identifiers if x}

    @classmethod
    def scan(cls, prefix: list[str] | None = None) -> list[CleanableResource]:
        """Read-only scan of Docker containers, images, networks, and volumes with ownership classification."""
        cli = shutil.which("docker")
        if not cli and not prefix:
            return []
        resources: list[CleanableResource] = []

        # 1. Scan containers
        try:
            res = cls._run_docker(["ps", "-a", "--format", "{{json .}}"], prefix=prefix)
        except Exception as exc:
            raise OpsError(f"Docker container scan error: {exc}")
        if res.returncode != 0:
            err = (res.stderr or "").strip() or f"exit code {res.returncode}"
            raise OpsError(f"Docker container scan failed: {err}")
        if (res.stdout or "").strip():
            for line in (res.stdout or "").strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    c_id = data.get("ID", "")
                    c_names = data.get("Names", "")
                    c_image = data.get("Image", "")
                    c_state = data.get("State", "")
                    c_status = data.get("Status", "")
                    c_labels_raw = data.get("Labels", "")
                    labels: dict[str, str] = {}
                    if isinstance(c_labels_raw, str):
                        for pair in c_labels_raw.split(","):
                            k, sep, v = pair.partition("=")
                            if sep:
                                labels[k.strip()] = v.strip()
                    elif isinstance(c_labels_raw, dict):
                        labels = c_labels_raw

                    proj = labels.get("com.docker.compose.project")
                    run_id = labels.get("com.auto_docker.run_id")
                    is_test = (
                        labels.get("com.auto_docker.test") == "true"
                        or bool(run_id)
                        or c_names.startswith("autodocker_live_")
                        or "autodocker-live-test:" in c_image
                        or (bool(proj) and proj.startswith("autodocker_live_"))
                    )
                    is_running = (c_state.lower() == "running" or "running" in c_status.lower() or "up" in c_status.lower().split())
                    is_orphan = is_test and not is_running

                    resources.append(CleanableResource(
                        kind="container",
                        id=c_id,
                        name=c_names,
                        project=proj,
                        is_test=is_test,
                        is_orphan=is_orphan,
                        status=c_state or c_status,
                        details=f"Image: {c_image}, Status: {c_status}",
                        labels=labels,
                    ))
                except Exception:
                    continue

        # 2. Scan images
        try:
            res = cls._run_docker(["images", "--format", "{{json .}}"], prefix=prefix)
        except Exception as exc:
            raise OpsError(f"Docker image scan error: {exc}")
        if res.returncode != 0:
            err = (res.stderr or "").strip() or f"exit code {res.returncode}"
            raise OpsError(f"Docker image scan failed: {err}")
        if (res.stdout or "").strip():
            for line in (res.stdout or "").strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    img_id = data.get("ID", "")
                    repo = data.get("Repository", "")
                    tag = data.get("Tag", "")
                    created = data.get("CreatedSince", "") or data.get("CreatedAt", "")
                    full_ref = f"{repo}:{tag}" if repo and tag else repo or img_id
                    is_test = (
                        repo == "autodocker-live-test"
                        or full_ref.startswith("autodocker-live-test:")
                    )
                    is_orphan = is_test

                    resources.append(CleanableResource(
                        kind="image",
                        id=img_id,
                        name=full_ref,
                        project=None,
                        is_test=is_test,
                        is_orphan=is_orphan,
                        status="available",
                        details=f"Created: {created}",
                    ))
                except Exception:
                    continue

        # 3. Scan networks
        try:
            res = cls._run_docker(["network", "ls", "--format", "{{json .}}"], prefix=prefix)
        except Exception as exc:
            raise OpsError(f"Docker network scan error: {exc}")
        if res.returncode != 0:
            err = (res.stderr or "").strip() or f"exit code {res.returncode}"
            raise OpsError(f"Docker network scan failed: {err}")
        if (res.stdout or "").strip():
            for line in (res.stdout or "").strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    net_id = data.get("ID", "")
                    net_name = data.get("Name", "")
                    driver = data.get("Driver", "")
                    if net_name in ("bridge", "host", "none"):
                        continue
                    labels_raw = data.get("Labels", "")
                    labels: dict[str, str] = {}
                    if isinstance(labels_raw, str):
                        for pair in labels_raw.split(","):
                            k, sep, v = pair.partition("=")
                            if sep:
                                labels[k.strip()] = v.strip()
                    elif isinstance(labels_raw, dict):
                        labels = labels_raw

                    is_test = net_name.startswith("autodocker_live_") or labels.get("com.auto_docker.test") == "true"
                    proj = labels.get("com.docker.compose.project")
                    if not proj:
                        if net_name.endswith("_default"):
                            proj = net_name.removesuffix("_default")
                    is_orphan = is_test

                    resources.append(CleanableResource(
                        kind="network",
                        id=net_id,
                        name=net_name,
                        project=proj,
                        is_test=is_test,
                        is_orphan=is_orphan,
                        status="active",
                        details=f"Driver: {driver}",
                        labels=labels,
                    ))
                except Exception:
                    continue

        # 4. Scan volumes
        try:
            res = cls._run_docker(["volume", "ls", "--format", "{{json .}}"], prefix=prefix)
        except Exception as exc:
            raise OpsError(f"Docker volume scan error: {exc}")
        if res.returncode != 0:
            err = (res.stderr or "").strip() or f"exit code {res.returncode}"
            raise OpsError(f"Docker volume scan failed: {err}")
        if (res.stdout or "").strip():
            for line in (res.stdout or "").strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    vol_name = data.get("Name", "")
                    driver = data.get("Driver", "")
                    labels_raw = data.get("Labels", "")
                    labels: dict[str, str] = {}
                    if isinstance(labels_raw, str):
                        for pair in labels_raw.split(","):
                            k, sep, v = pair.partition("=")
                            if sep:
                                labels[k.strip()] = v.strip()
                    elif isinstance(labels_raw, dict):
                        labels = labels_raw

                    is_test = vol_name.startswith("autodocker_live_") or labels.get("com.auto_docker.test") == "true"
                    proj = labels.get("com.docker.compose.project")
                    if not proj:
                        if vol_name.startswith("autodocker_live_"):
                            proj = vol_name
                        elif vol_name.startswith("opsforge-") and "_" in vol_name:
                            proj = vol_name.split("_", 1)[0]
                    is_orphan = is_test

                    resources.append(CleanableResource(
                        kind="volume",
                        id=vol_name,
                        name=vol_name,
                        project=proj,
                        is_test=is_test,
                        is_orphan=is_orphan,
                        status="created",
                        details=f"Driver: {driver}",
                        labels=labels,
                    ))
                except Exception:
                    continue

        return resources

    @classmethod
    def plan_cleanup(
        cls,
        mode: str,
        target_project: str | None = None,
        run_id: str | None = None,
        preserve_volumes: bool = True,
        resources: list[CleanableResource] | None = None,
        prefix: list[str] | None = None,
    ) -> CleanupPlan:
        """Constructs an explicit cleanup plan adhering to ownership safety rules."""
        if resources is None:
            resources = cls.scan(prefix=prefix)

        target_ids = cls.get_project_identifiers(target_project) if target_project else set()

        to_clean_containers: list[CleanableResource] = []
        to_clean_images: list[CleanableResource] = []
        to_clean_networks: list[CleanableResource] = []
        to_clean_volumes: list[CleanableResource] = []
        protected_volumes: list[CleanableResource] = []

        for r in resources:
            match = False
            if mode == "test":
                if run_id:
                    match = (
                        r.labels.get("com.auto_docker.run_id") == run_id
                        or (target_ids and (r.project in target_ids or r.project == target_project))
                        or (run_id in r.name)
                    )
                else:
                    match = r.is_test
            elif mode == "project":
                if target_ids:
                    match = (
                        (r.project in target_ids)
                        or any(r.name.startswith(f"{t}-") or r.name.startswith(f"{t}_") or r.name == t for t in target_ids)
                    )
            elif mode == "orphan":
                match = r.is_orphan

            if not match:
                continue

            if r.kind == "container":
                to_clean_containers.append(r)
            elif r.kind == "image":
                to_clean_images.append(r)
            elif r.kind == "network":
                to_clean_networks.append(r)
            elif r.kind == "volume":
                if preserve_volumes and not (mode == "test" and r.is_test):
                    protected_volumes.append(r)
                else:
                    to_clean_volumes.append(r)

        return CleanupPlan(
            mode=mode,
            target_project=target_project,
            containers=tuple(to_clean_containers),
            images=tuple(to_clean_images),
            networks=tuple(to_clean_networks),
            volumes=tuple(to_clean_volumes),
            protected_volumes=tuple(protected_volumes),
        )

    @classmethod
    def format_plan_preview(cls, plan: CleanupPlan) -> str:
        """Generates clear, human-readable preview text for operator review."""
        mode_titles = {
            "test": "Temporary Test Resources",
            "project": f"Project Resources ({plan.target_project or 'Selected Project'})",
            "orphan": "Orphaned Resources & Leftovers",
        }
        lines = [
            "=" * 80,
            "Auto Docker Resource Cleaner — Cleanup Plan Preview",
            "=" * 80,
            f"Mode                 : {mode_titles.get(plan.mode, plan.mode)}",
            f"Containers to remove : {len(plan.containers)}",
            f"Images to remove     : {len(plan.images)}",
            f"Networks to remove   : {len(plan.networks)}",
            f"Volumes to remove    : {len(plan.volumes)}",
            f"Volumes protected    : {len(plan.protected_volumes)} (User data preserved by default)",
            "-" * 80,
        ]
        if plan.total_count == 0:
            lines.append("  (No matching cleanable resources found; system is already clean)")
        else:
            if plan.containers:
                lines.append("Containers to remove:")
                for c in plan.containers:
                    lines.append(f"  - {c.name} (ID: {c.id[:12]}, Status: {c.status})")
            if plan.images:
                lines.append("Images to remove:")
                for img in plan.images:
                    lines.append(f"  - {img.name} (ID: {img.id[:12]})")
            if plan.networks:
                lines.append("Networks to remove:")
                for net in plan.networks:
                    lines.append(f"  - {net.name} (ID: {net.id[:12]})")
            if plan.volumes:
                lines.append("Volumes to remove:")
                for vol in plan.volumes:
                    lines.append(f"  - {vol.name}")
            if plan.protected_volumes:
                lines.append("Protected Volumes (Will NOT be deleted):")
                for vol in plan.protected_volumes:
                    lines.append(f"  [PROTECTED] {vol.name}")
        lines.append("=" * 80)
        return "\n".join(lines)

    @classmethod
    def execute(cls, plan: CleanupPlan, prefix: list[str] | None = None) -> CleanupResult:
        """Executes the cleanup plan authoritatively, verifying actual deletion of every resource."""
        if not (plan.containers or plan.networks or plan.images or plan.volumes):
            return CleanupResult(
                success=True,
                cleaned=(),
                failed=(),
                leftovers=(),
                message="Successfully cleaned 0 resources."
            )

        cleaned: list[CleanableResource] = []
        failed: list[tuple[CleanableResource, str]] = []

        # 1. Stop and remove containers
        for c in plan.containers:
            cls._run_docker(["stop", "-t", "3", c.id], prefix=prefix, timeout=15)
            rm_res = cls._run_docker(["rm", "-f", "-v", c.id], prefix=prefix, timeout=15)
            if rm_res.returncode == 0:
                cleaned.append(c)
            else:
                err = (rm_res.stderr or rm_res.stdout or f"exit code {rm_res.returncode}").strip()
                if "no such container" in err.lower():
                    cleaned.append(c)
                else:
                    failed.append((c, f"container rm failed: {err}"))

        # 2. Remove networks
        for n in plan.networks:
            net_res = cls._run_docker(["network", "rm", n.id], prefix=prefix, timeout=15)
            if net_res.returncode == 0:
                cleaned.append(n)
            else:
                err = (net_res.stderr or net_res.stdout or f"exit code {net_res.returncode}").strip()
                if "no such network" in err.lower() or "not found" in err.lower():
                    cleaned.append(n)
                else:
                    failed.append((n, f"network rm failed: {err}"))

        # 3. Remove images
        for img in plan.images:
            img_res = cls._run_docker(["rmi", "-f", img.name if ":" in img.name else img.id], prefix=prefix, timeout=20)
            if img_res.returncode == 0:
                cleaned.append(img)
            else:
                err = (img_res.stderr or img_res.stdout or f"exit code {img_res.returncode}").strip()
                if "no such image" in err.lower() or "not found" in err.lower():
                    cleaned.append(img)
                else:
                    failed.append((img, f"image rmi failed: {err}"))

        # 4. Remove volumes (if scheduled)
        for vol in plan.volumes:
            vol_res = cls._run_docker(["volume", "rm", "-f", vol.name], prefix=prefix, timeout=15)
            if vol_res.returncode == 0:
                cleaned.append(vol)
            else:
                err = (vol_res.stderr or vol_res.stdout or f"exit code {vol_res.returncode}").strip()
                if "no such volume" in err.lower() or "not found" in err.lower():
                    cleaned.append(vol)
                else:
                    failed.append((vol, f"volume rm failed: {err}"))

        # 5. Authoritatively re-scan to verify that targeted resources are truly gone
        post_scan = cls.scan(prefix=prefix)
        remaining_ids = {r.id for r in post_scan} | {r.name for r in post_scan}
        leftovers: list[CleanableResource] = []

        for item in plan.containers:
            if item.id in remaining_ids or item.name in remaining_ids:
                leftovers.append(item)
        for item in plan.networks:
            if item.id in remaining_ids or item.name in remaining_ids:
                leftovers.append(item)
        for item in plan.images:
            if item.id in remaining_ids or item.name in remaining_ids:
                leftovers.append(item)
            else:
                insp = cls._run_docker(["image", "inspect", item.name], prefix=prefix, timeout=5)
                if insp.returncode == 0:
                    leftovers.append(item)
        for item in plan.volumes:
            if item.name in remaining_ids:
                leftovers.append(item)

        success = (len(failed) == 0 and len(leftovers) == 0)
        summary_msg = (
            f"Successfully cleaned {len(cleaned)} resources; 0 leftovers."
            if success
            else f"Cleanup incomplete: {len(failed)} failed operations, {len(leftovers)} lingering resources."
        )
        return CleanupResult(
            success=success,
            cleaned=tuple(cleaned),
            failed=tuple(failed),
            leftovers=tuple(leftovers),
            message=summary_msg,
        )

    @classmethod
    def clean_test_resources(
        cls,
        run_id: str | None = None,
        project_name: str | None = None,
        prefix: list[str] | None = None,
        root_dir: Path | None = None,
    ) -> tuple[bool, str]:
        """Convenience method for automated test teardown.
        Attempts compose down if compose.yaml exists, then authoritatively
        cleans and verifies removal of all test resources.
        """
        errs: list[str] = []

        # 1. If root_dir and compose.yaml exist, try compose down first
        if root_dir and (root_dir / "compose.yaml").is_file():
            compose_file = str(root_dir / "compose.yaml")
            proj = project_name or (f"autodocker_live_{run_id}" if run_id else None)
            cmd = ["compose"]
            if proj:
                cmd.extend(["-p", proj])
            cmd.extend(["-f", compose_file, "down", "--volumes", "--remove-orphans", "-t", "5"])
            down_res = cls._run_docker(cmd, prefix=prefix, timeout=60)
            if down_res.returncode != 0:
                err_text = (down_res.stderr or down_res.stdout or f"exit code {down_res.returncode}").strip()
                errs.append(f"compose down failed: {err_text}")

        # 2. Plan and execute removal of all matching test resources
        plan = cls.plan_cleanup(mode="test", target_project=project_name, run_id=run_id, preserve_volumes=False, prefix=prefix)
        result = cls.execute(plan, prefix=prefix)

        if errs or not result.success:
            fail_details = [f"{r.kind} {r.name}: {msg}" for r, msg in result.failed]
            leftover_details = [f"{r.kind} {r.name} ({r.id})" for r in result.leftovers]
            all_errs = errs + fail_details
            if leftover_details:
                all_errs.append(f"lingering resources: {', '.join(leftover_details)}")
            return False, "; ".join(all_errs)

        return True, "all project resources cleanly removed and verified"


class LiveAcceptanceTest:
    """Safe, reproducible live Docker acceptance test with full lifecycle execution."""

    @staticmethod
    def _is_local_docker_endpoint(endpoint: str) -> tuple[bool, str]:
        """Evaluates whether a Docker host endpoint string represents a local Docker Engine."""
        clean = endpoint.strip()
        if not clean:
            return True, "default local socket"
        low = clean.lower()
        if low.startswith("ssh://"):
            return False, f"SSH remote daemon ({clean})"
        if low.startswith("unix://") or low.startswith("/"):
            return True, f"local Unix domain socket ({clean})"
        if low.startswith("npipe://") or low.startswith(r"\\.\pipe"):
            return True, f"local Windows named pipe ({clean})"
        if low.startswith("tcp://") or low.startswith("http://") or low.startswith("https://"):
            body = clean.split("://", 1)[1]
            host = body.split(":", 1)[0].strip("[]")
            if host in ("127.0.0.1", "localhost", "::1"):
                return True, f"local loopback endpoint ({clean})"
            return False, f"remote network host ({clean})"
        if ":" in clean and not clean.startswith(("\\", "/")):
            host = clean.split(":", 1)[0].strip("[]")
            if host in ("127.0.0.1", "localhost", "::1"):
                return True, f"local loopback endpoint ({clean})"
            return False, f"remote network host ({clean})"
        return False, f"unrecognized/remote endpoint ({clean})"

    @classmethod
    def verify_docker_endpoint(cls, context: str | None = None, docker_cli: str | None = None) -> tuple[bool, str, str | None]:
        """Determines and verifies the effective Docker endpoint, rejecting remote daemons."""
        # 1. Environment variable override: DOCKER_HOST
        docker_host = os.environ.get("DOCKER_HOST", "").strip()
        if docker_host:
            is_local, reason = cls._is_local_docker_endpoint(docker_host)
            if not is_local:
                return (
                    False,
                    f"Remote Docker Engine detected via DOCKER_HOST={docker_host} ({reason}).\n"
                    "Live acceptance test requires the local Docker daemon because local HTTP readiness\n"
                    "verification (http://127.0.0.1:<port>) directly probes the local network stack.\n"
                    "Unset DOCKER_HOST or configure it to point to a local Docker Engine.",
                    None,
                )

        cli = docker_cli or shutil.which("docker")
        effective_context = context or os.environ.get("DOCKER_CONTEXT")

        if not cli:
            if effective_context and effective_context not in ("default",):
                return (
                    False,
                    f"Remote Docker context '{effective_context}' is unsupported for live-test.\n"
                    "Live acceptance test requires the local Docker daemon because local HTTP readiness\n"
                    "verification (http://127.0.0.1:<port>) directly probes the local network stack.\n"
                    "Run live-test against the local Docker host or omit --docker-context.",
                    effective_context,
                )
            return True, "local engine (docker CLI not found; handled by probe)", effective_context

        # 2. Determine active context if omitted
        if not effective_context:
            try:
                show_res = subprocess.run(
                    [cli, "context", "show"],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                if show_res.returncode == 0 and show_res.stdout.strip():
                    effective_context = show_res.stdout.strip()
                elif show_res.returncode != 0:
                    detail = (show_res.stderr or show_res.stdout or f"exit code {show_res.returncode}").strip()
                    return False, f"BLOCKED: Failed to determine active Docker context ({detail}).", None
            except Exception as exc:
                return False, f"BLOCKED: Failed to determine active Docker context ({exc}).", None

        # 3. Inspect effective context endpoint
        if effective_context:
            try:
                insp_res = subprocess.run(
                    [cli, "context", "inspect", effective_context],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                if insp_res.returncode != 0:
                    detail = (insp_res.stderr or insp_res.stdout or f"exit code {insp_res.returncode}").strip()
                    if effective_context == "default" and ("no such context" in detail.lower() or "not found" in detail.lower()):
                        return True, "default local engine", effective_context
                    if "no such context" in detail.lower() or "not found" in detail.lower():
                        return (
                            False,
                            f"Remote Docker context '{effective_context}' is unsupported for live-test.\n"
                            f"Context cannot be inspected or verified as a local engine ({detail}).\n"
                            "Live acceptance test requires the local Docker daemon because local HTTP readiness\n"
                            "verification (http://127.0.0.1:<port>) directly probes the local network stack.\n"
                            "Run live-test against the local Docker host or omit --docker-context.",
                            effective_context,
                        )
                    return False, f"BLOCKED: Unable to inspect Docker context '{effective_context}' ({detail}).", effective_context
                data = json.loads(insp_res.stdout)
                if isinstance(data, list) and len(data) > 0:
                    endpoints = data[0].get("Endpoints", {})
                    docker_ep = endpoints.get("docker", {})
                    host_val = docker_ep.get("Host", "")
                    if host_val:
                        is_local, reason = cls._is_local_docker_endpoint(host_val)
                        if not is_local:
                            return (
                                False,
                                f"Remote Docker context '{effective_context}' is unsupported for live-test.\n"
                                f"Active endpoint '{host_val}' is a remote daemon ({reason}).\n"
                                "Live acceptance test requires the local Docker daemon because local HTTP readiness\n"
                                "verification (http://127.0.0.1:<port>) directly probes the local network stack.\n"
                                "Switch to a local Docker context (e.g. 'default') or run against the local Docker host.",
                                effective_context,
                            )
            except json.JSONDecodeError as exc:
                return False, f"BLOCKED: Failed to parse Docker context inspect output ({exc}).", effective_context
            except Exception as exc:
                return False, f"BLOCKED: Failed to inspect Docker context '{effective_context}' ({exc}).", effective_context

        return True, "local Docker Engine verified", effective_context

    @classmethod
    def scoped_cleanup(cls, root_dir: Path, project_name: str, run_id: str, prefix: list[str]) -> tuple[bool, str]:
        """Authoritatively and safely removes all resources belonging to this test project."""
        return ResourceCleaner.clean_test_resources(
            run_id=run_id,
            project_name=project_name,
            prefix=prefix,
            root_dir=root_dir,
        )

    @classmethod
    def run(cls, report: Path | None = None, yes: bool = False, context: str | None = None) -> bool:
        # Check Python version compatibility first
        compat, msg = check_python_compatibility()
        if not compat:
            print(msg, file=sys.stderr)
            return False

        # Determine and verify effective Docker endpoint (local engine required)
        endpoint_ok, endpoint_msg, effective_ctx = cls.verify_docker_endpoint(context=context)
        if not endpoint_ok:
            print(endpoint_msg, file=sys.stderr)
            return False

        target_context = effective_ctx or context
        probe = DockerHostProbe.probe(context=target_context)
        if not probe.cli_installed:
            print("BLOCKED: Docker CLI not found on host PATH. Live acceptance test requires Docker Engine.", file=sys.stderr)
            return False
        if not probe.compose_available:
            print(f"BLOCKED: Docker Compose plugin unavailable: {probe.error_detail}", file=sys.stderr)
            return False
        if not probe.engine_reachable:
            print(f"BLOCKED: Docker Engine is not reachable ({probe.error_detail}). Live acceptance test requires a running Docker daemon.", file=sys.stderr)
            return False

        # Disk space check
        storage_str = "UNKNOWN"
        try:
            stat = shutil.disk_usage(Path.cwd())
            free_gib = stat.free / (1024 ** 3)
            storage_str = f"{free_gib:.1f} GiB free"
            if free_gib < 2.0:
                print(f"BLOCKED: Insufficient free disk space ({free_gib:.1f} GiB available; minimum 2.0 GiB required for build/images).", file=sys.stderr)
                return False
        except OSError as exc:
            storage_str = f"UNKNOWN ({exc})"

        port = _free_local_port()
        run_id = secrets.token_hex(4)
        project_name = f"autodocker_live_{run_id}"

        print("=" * 80)
        print(f"AUTO DOCKER — Live Docker Acceptance Test (Run ID: {run_id})")
        print("=" * 80)
        print(f"Docker Host : {probe.summary}")
        print(f"Host Disk   : {storage_str}")
        print(f"Project Name: {project_name} (isolated project namespace)")
        print("-" * 80)
        print("Expected Resources & Operations:")
        print("  - Base Image  : python:3.11-alpine (approx. 20 MB download, 60 MB uncompressed)")
        print(f"  - Custom Image: autodocker-live-test:{run_id} (built from real temporary Dockerfile)")
        print("  - Container   : 1 disposable web container running custom Python HTTP test server")
        print(f"  - Port Mapping: 127.0.0.1:{port} -> 8080 (loopback-only; tested with HTTP GET)")
        print("  - Lifecycle   : Config Validation -> Build -> Start -> Verify -> Stop -> Restart -> Verify -> Scoped Cleanup")
        print("  - Safety      : Scoped exclusively to project resources; zero broad prune or unrelated deletions")
        print("-" * 80)

        if not yes:
            if not sys.stdin.isatty():
                print("ERROR: Explicit confirmation required for live Docker container execution. Pass --yes.", file=sys.stderr)
                return False
            confirm = input("Authorize image build, container execution, and scoped cleanup? Type YES: ").strip()
            if confirm != "YES":
                print("Live acceptance test cancelled by operator; no Docker operations performed.")
                return False

        evidence = ProofEvidence("P0-live-docker-lifecycle", report)
        docker = probe.cli_path or "docker"
        prefix = [docker]
        if context:
            prefix += ["--context", context]

        with tempfile.TemporaryDirectory(prefix=f"auto-docker-live-{run_id}-") as tmpdir:
            root = Path(tmpdir).resolve()

            # 1. Real, clean, deterministic Python HTTP test server with instant SIGTERM/SIGINT shutdown
            server_py_content = (
                "import signal, sys\n"
                "from http.server import HTTPServer, BaseHTTPRequestHandler\n\n"
                "def _exit(*_):\n"
                "    sys.exit(0)\n\n"
                "signal.signal(signal.SIGTERM, _exit)\n"
                "signal.signal(signal.SIGINT, _exit)\n\n"
                "class LiveHandler(BaseHTTPRequestHandler):\n"
                "    def do_GET(self):\n"
                "        self.send_response(200)\n"
                "        self.send_header('Content-Type', 'text/plain')\n"
                "        self.end_headers()\n"
                "        self.wfile.write(b'LIVE_TEST_OK')\n\n"
                "    def log_message(self, format, *args):\n"
                "        pass\n\n"
                "if __name__ == '__main__':\n"
                "    httpd = HTTPServer(('0.0.0.0', 8080), LiveHandler)\n"
                "    httpd.serve_forever()\n"
            )
            (root / "server.py").write_text(server_py_content, encoding="utf-8")

            # 2. Real Dockerfile copying the test application
            dockerfile_content = (
                "FROM python:3.11-alpine\n"
                "WORKDIR /app\n"
                "COPY server.py .\n"
                "EXPOSE 8080\n"
                "CMD [\"python\", \"server.py\"]\n"
            )
            (root / "Dockerfile").write_text(dockerfile_content, encoding="utf-8")

            # 3. Standards-compliant Compose YAML with real build context & loopback-only port mapping
            compose_content = (
                f"name: {project_name}\n"
                "services:\n"
                "  web:\n"
                "    build:\n"
                "      context: .\n"
                "      dockerfile: Dockerfile\n"
                f"    image: autodocker-live-test:{run_id}\n"
                "    ports:\n"
                f'      - "127.0.0.1:{port}:8080"\n'
                "    labels:\n"
                '      com.auto_docker.test: "true"\n'
                f'      com.auto_docker.run_id: "{run_id}"\n'
            )
            (root / "compose.yaml").write_text(compose_content, encoding="utf-8")

            orch = DeploymentOrchestrator(root)

            try:
                # Step 0: Validate Compose syntax using authoritative compose check
                print("\n[Step 0/7] Validating Compose configuration...")
                ExistingInfrastructure.apply(root, "check", context=target_context, orchestrator=orch, project_name=project_name)
                evidence.record("0-config-validation", "PASS", "Compose configuration syntax verified")

                # Step 1: Real custom image build from Dockerfile
                print("\n[Step 1/7] Building custom test image from Dockerfile...")
                ExistingInfrastructure.apply(root, "build", approved=True, context=target_context, orchestrator=orch, project_name=project_name)
                evidence.record("1-build", "PASS", f"Real image build succeeded (autodocker-live-test:{run_id})")

                # Step 2: Start container
                print("\n[Step 2/7] Starting services...")
                ExistingInfrastructure.apply(root, "up", approved=True, context=target_context, orchestrator=orch, project_name=project_name)
                evidence.record("2-start", "PASS", "Container started (VERIFYING)")

                # Step 3: Probe HTTP endpoint and verify container runtime health
                url = f"http://127.0.0.1:{port}"
                print(f"Waiting for HTTP response on {url}...")
                _wait_http(url, timeout=30)
                with urllib.request.urlopen(url, timeout=5) as resp:
                    body = resp.read()
                    if resp.status != 200 or b"LIVE_TEST_OK" not in body:
                        raise OpsError(f"HTTP response check failed: status {resp.status}, body {body!r}")
                print(f"HTTP GET {url} -> 200 OK (marker verified: LIVE_TEST_OK)")

                print("\n[Step 3/7] Verifying container runtime health...")
                ExistingInfrastructure.apply(root, "verify", approved=True, context=target_context, orchestrator=orch, project_name=project_name)
                evidence.record("3-verify", "PASS", "Runtime container verification succeeded (healthy/running)")

                # Step 4: Stop services
                print("\n[Step 4/7] Stopping services...")
                ExistingInfrastructure.apply(root, "down", approved=True, context=target_context, orchestrator=orch, project_name=project_name)
                evidence.record("4-stop", "PASS", "Services stopped (ANALYZED)")

                # Step 5: Restart services
                print("\n[Step 5/7] Restarting services...")
                ExistingInfrastructure.apply(root, "up", approved=True, context=target_context, orchestrator=orch, project_name=project_name)
                evidence.record("5-restart", "PASS", "Services restarted (VERIFYING)")

                # Step 6: Verify after restart
                print("\n[Step 6/7] Verifying after restart...")
                _wait_http(url, timeout=30)
                with urllib.request.urlopen(url, timeout=5) as resp:
                    if resp.status != 200 or b"LIVE_TEST_OK" not in resp.read():
                        raise OpsError("HTTP response check failed after restart")
                ExistingInfrastructure.apply(root, "verify", approved=True, context=target_context, orchestrator=orch, project_name=project_name)
                evidence.record("6-verify-again", "PASS", "Second verification succeeded after restart")

                # Step 7: Scoped cleanup and verification
                print("\n[Step 7/7] Executing scoped cleanup...")
                clean_ok, clean_msg = cls.scoped_cleanup(root, project_name, run_id, prefix)
                if not clean_ok:
                    raise OpsError(f"Cleanup verification failed: {clean_msg}")
                evidence.record("7-cleanup", "PASS", clean_msg)

                print("\n" + "=" * 80)
                print("LIVE ACCEPTANCE TEST: ALL 7 LIFECYCLE PHASES PASSED")
                print("=" * 80)
                return True

            except (Exception, KeyboardInterrupt) as exc:
                print(f"\nLIVE ACCEPTANCE TEST FAILED: {exc}", file=sys.stderr)
                evidence.record("lifecycle-error", "FAIL", str(exc))
                clean_ok, clean_msg = cls.scoped_cleanup(root, project_name, run_id, prefix)
                if not clean_ok:
                    print(f"WARNING: Cleanup after failure had errors: {clean_msg}", file=sys.stderr)
                    evidence.record("cleanup-after-failure", "FAIL", clean_msg)
                return False
            finally:
                evidence.save()


# ---------- Single-file automated regression suite ----------

def self_test() -> bool:
    import io
    import unittest
    from unittest.mock import patch

    class Tests(unittest.TestCase):
        def setUp(self) -> None:
            self.tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self.tmp.cleanup)
            self.root = (Path(self.tmp.name) / "my local llm").resolve()
            self.root.mkdir()
            (self.root / "localllmchat.py").write_text("import ollama\nif __name__ == '__main__':\n print('ok')\n")

        def _safe_symlink(self, link_path: Path, target_path: Path, target_is_directory: bool = False) -> None:
            try:
                link_path.symlink_to(target_path, target_is_directory=target_is_directory)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"Symlinks not supported in this environment: {exc}")

        def config(self, mode: Mode = Mode.BASE, gpu: bool = False) -> tuple[Plan, dict[str, str]]:
            p = Planner().build(Settings(target_for(str(self.root / "localllmchat.py")), mode=mode, gpu=gpu),
                                Inspector().inspect(target_for(str(self.root / "localllmchat.py"))))
            return p, Renderer().files(p)

        def test_quoted_file_path(self):
            t = target_for(f"'{self.root / 'localllmchat.py'}'")
            self.assertEqual(t.pinned, "localllmchat.py")
            self.assertEqual(t.root, self.root)

        def test_reject_home(self):
            with self.assertRaises(OpsError):
                target_for(str(Path.home()))

        def test_file_pin_avoids_other_files(self):
            (self.root / "app.py").write_text("print('wrong')")
            x = Inspector().inspect(target_for(str(self.root / "localllmchat.py")))
            self.assertEqual(x.entry.path, "localllmchat.py")
            self.assertEqual(x.python_count, 1)

        def test_ambiguous_folder(self):
            (self.root / "app.py").write_text("if __name__ == '__main__': pass")
            (self.root / "main.py").write_text("if __name__ == '__main__': pass")
            with self.assertRaises(AmbiguousEntry):
                Inspector().inspect(target_for(str(self.root)))

        def test_prevents_unbounded_scan(self):
            for n in range(240 + 1):
                (self.root / f"module_{n}.py").write_text("x=1")
            with self.assertRaises(ScanLimit):
                Inspector().inspect(target_for(str(self.root)))

        def test_symlinks_not_followed(self):
            foreign = Path(self.tmp.name) / "elsewhere"
            foreign.mkdir()
            (foreign / "app.py").write_text("x=1")
            self._safe_symlink(self.root / "outside", foreign, target_is_directory=True)
            found = _collect_python(self.root)
            self.assertNotIn(foreign / "app.py", found)

        def test_unparsable_main_not_silently_ignored(self):
            (self.root / "main.py").write_text("this is not valid python !!!")
            with self.assertRaises(OpsError):
                Inspector().inspect(target_for(str(self.root)))

        def test_services_startup_order(self):
            _, files = self.config(Mode.LLMOPS)
            self.assertIn('mlflow:\n        condition: "service_healthy"', files["compose.yaml"])
            self.assertIn('ollama:\n        condition: "service_healthy"', files["compose.yaml"])

        def test_modes(self):
            for mode, expected in ((Mode.BASE, ()), (Mode.MLOPS, ("mlflow",)),
                                   (Mode.LLMOPS, ("mlflow", "ollama")),
                                   (Mode.GENAI, ("mlflow", "ollama", "redis"))):
                p, files = self.config(mode)
                self.assertEqual(p.services, expected)
                self.assertIn("services:", files["compose.yaml"])

        def test_gpu(self):
            _, files = self.config(Mode.GENAI, True)
            self.assertIn('"nvidia"', files["compose.yaml"])
            self.assertIn('"gpu"', files["compose.yaml"])

        def test_gpu_rejected_base(self):
            with self.assertRaises(OpsError):
                self.config(Mode.BASE, True)

        def test_stable_hashes(self):
            self.assertEqual(self.config()[1], self.config()[1])

        def test_root_dockerfile_untouched(self):
            (self.root / "Dockerfile").write_text("KEEP ORIGINAL\n")
            _, files = self.config()
            OutputManager(self.root).write(files)
            self.assertEqual((self.root / "Dockerfile").read_text(), "KEEP ORIGINAL\n")

        def test_backup_created(self):
            _, files = self.config()
            out = OutputManager(self.root)
            out.write(files)
            (self.root / OUTPUT / "Dockerfile").write_text("HAND EDIT\n")
            backup = out.write(files)
            self.assertIsNotNone(backup)
            self.assertEqual((backup / "Dockerfile").read_text(), "HAND EDIT\n")

        def test_output_symlink_refused_on_preview(self):
            foreign = Path(self.tmp.name) / "foreign"
            foreign.mkdir()
            self._safe_symlink(self.root / OUTPUT, foreign, target_is_directory=True)
            with self.assertRaises(OpsError):
                OutputManager(self.root).state(self.config()[1])

        def test_generator_skips_own_file(self):
            other = self.root / "opsforge_v2.py"
            other.write_text("print('generator')")
            paths = _collect_python(self.root)
            self.assertNotIn(other, paths)

        def test_forged_output_protected(self):
            (self.root / OUTPUT).mkdir()
            (self.root / OUTPUT / "hello").write_text("mine")
            with self.assertRaises(OpsError):
                OutputManager(self.root).write(self.config()[1])

        def test_lock_collision(self):
            with ProjectLock(self.root):
                with self.assertRaises(OpsError):
                    with ProjectLock(self.root):
                        pass

        def test_hash_integrity(self):
            _, files = self.config()
            manager = OutputManager(self.root)
            manager.write(files)
            self.assertTrue(manager.validate()[0])
            (self.root / OUTPUT / "compose.yaml").write_text("malicious")
            self.assertFalse(manager.validate()[0])

        def test_console_back(self):
            c = Console(Settings(target_for(str(self.root / "localllmchat.py"))))
            c.execute("mode genaiops")
            self.assertEqual(c.settings.mode, Mode.GENAI)
            c.execute("back")
            self.assertEqual(c.settings.mode, Mode.AUTO)

        def test_console_project_switch(self):
            c = Console()
            with patch("sys.stdout", new_callable=io.StringIO):
                c.execute(f"project '{self.root / 'localllmchat.py'}'")
            self.assertEqual(c.settings.target.pinned, "localllmchat.py")

        def test_no_write_if_declined(self):
            c = Console(Settings(target_for(str(self.root / "localllmchat.py"))))
            with patch("builtins.input", return_value="NO"), patch("sys.stdout", new_callable=io.StringIO):
                c.execute("generate")
            self.assertFalse((self.root / OUTPUT).exists())

        def test_confirm_yes_is_top_level_command_no_nested_input(self):
            console = Console(Settings(target_for(str(self.root / "localllmchat.py"))))
            with patch("builtins.input", side_effect=AssertionError("nested input is forbidden")), \
                 patch("sys.stdout", new_callable=io.StringIO) as output:
                console.execute("generate")
                self.assertIsNotNone(console.pending)
                self.assertFalse((self.root / OUTPUT).exists())
                console.execute("YES")
            self.assertIsNone(console.pending)
            self.assertTrue(OutputManager(self.root).validate()[0])
            self.assertIn("Generated:", output.getvalue())

        def test_multiline_paste_check_queues_until_yes(self):
            console = Console()
            commands = [
                f"project '{self.root / 'localllmchat.py'}'", "preflight", "mode genaiops",
                "preview", "generate", "check", "YES", "quit",
            ]
            with patch("builtins.input", side_effect=commands), \
                 patch("sys.stdout", new_callable=io.StringIO) as output:
                console.loop()
            shown = output.getvalue()
            self.assertIn("Queued 'check' after generation", shown)
            self.assertIn("Generated file integrity: OK", shown)
            self.assertNotIn("Cancelled.", shown)
            self.assertTrue(OutputManager(self.root).validate()[0])

        def test_terminal_delivering_one_multiline_string(self):
            console = Console()
            commands = (f"project '{self.root / 'localllmchat.py'}'\n"
                        "mode genaiops\npreview\ngenerate\ncheck\nYES")
            with patch("builtins.input", side_effect=[commands, "quit"]), \
                 patch("sys.stdout", new_callable=io.StringIO) as output:
                console.loop()
            self.assertIn("Queued 'check'", output.getvalue())
            self.assertIn("Generated file integrity: OK", output.getvalue())
            self.assertTrue(OutputManager(self.root).validate()[0])

        def test_no_implicit_yes_from_check(self):
            console = Console(Settings(target_for(str(self.root / "localllmchat.py"))))
            with patch("sys.stdout", new_callable=io.StringIO):
                console.execute("generate")
                console.execute("check")
            self.assertIsNotNone(console.pending)
            self.assertEqual(console.pending.after, ["check"])
            self.assertFalse((self.root / OUTPUT).exists())

        def test_generate_yes_single_command(self):
            console = Console(Settings(target_for(str(self.root / "localllmchat.py"))))
            with patch("builtins.input", side_effect=AssertionError("nested input is forbidden")), \
                 patch("sys.stdout", new_callable=io.StringIO):
                console.execute("generate YES")
            self.assertIsNone(console.pending)
            self.assertTrue(OutputManager(self.root).validate()[0])

        def test_change_setting_cancels_outstanding_generate(self):
            console = Console(Settings(target_for(str(self.root / "localllmchat.py"))))
            with patch("sys.stdout", new_callable=io.StringIO):
                console.execute("generate")
                console.execute("mode llmops")
                console.execute("YES")
            self.assertIsNone(console.pending)
            self.assertEqual(console.settings.mode, Mode.LLMOPS)
            self.assertFalse((self.root / OUTPUT).exists())

        def test_changed_output_before_confirmation_aborts(self):
            console = Console(Settings(target_for(str(self.root / "localllmchat.py"))))
            with patch("sys.stdout", new_callable=io.StringIO):
                console.execute("generate")
                manager = OutputManager(self.root)
                manager.write(self.config()[1])
                with self.assertRaisesRegex(OpsError, "Project/output changed"):
                    console.execute("YES")
            self.assertIsNone(console.pending)

        def test_pending_generation_ctrl_c_never_writes(self):
            console = Console(Settings(target_for(str(self.root / "localllmchat.py"))))
            with patch("builtins.input", side_effect=["generate", KeyboardInterrupt, "quit"]), \
                 patch("sys.stdout", new_callable=io.StringIO) as output:
                console.loop()
            self.assertIsNone(console.pending)
            self.assertIn("Cancelled pending generate", output.getvalue())
            self.assertFalse((self.root / OUTPUT).exists())

        def test_build_confirmation_does_not_consume_nested_input(self):
            console = Console(Settings(target_for(str(self.root / "localllmchat.py"))))
            OutputManager(self.root).write(self.config()[1])
            with patch("builtins.input", side_effect=AssertionError("nested input is forbidden")), \
                 patch("sys.stdout", new_callable=io.StringIO), \
                 patch.object(DockerAdapter, "preflight") as preflight, \
                 patch.object(DockerAdapter, "run") as run:
                console.execute("build")
                run.assert_not_called()
                console.execute("YES")
                run.assert_called_once_with("build")

        def test_missing_docker_is_recoverable(self):
            with patch("shutil.which", return_value=None):
                with self.assertRaises(OpsError):
                    DockerAdapter(self.root).run("build")

        def test_json_logs(self):
            record = logging.LogRecord("opsforge", logging.INFO, __file__, 1, "event %s", ("okay",), None)
            event = json.loads(JsonLogFormatter().format(record))
            self.assertEqual(event["event"], "event okay")
            self.assertEqual(event["severity"], "INFO")

        def test_keyboard_interrupt_stays_in_console(self):
            c = Console(Settings(target_for(str(self.root / "localllmchat.py"))))
            with patch("builtins.input", side_effect=[KeyboardInterrupt, "quit"]), patch("sys.stdout", new_callable=io.StringIO) as output:
                c.loop()
            self.assertIn("still in Auto Docker", output.getvalue())

        def test_requirements(self):
            (self.root / "requirements.txt").write_text("requests==2.32.3\n")
            _, files = self.config()
            self.assertIn("pip install --no-cache-dir -r requirements.txt", files["Dockerfile"])

        def test_fastapi_command(self):
            (self.root / "localllmchat.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
            p, _ = self.config()
            self.assertEqual(p.port, 8000)
            self.assertIn("uvicorn", p.command)

        def test_ordinary_script_does_not_restart_forever(self):
            _, files = self.config(Mode.BASE)
            self.assertIn('restart: "no"', files["compose.yaml"])

        def test_runtime_has_writable_app_directory(self):
            _, files = self.config(Mode.BASE)
            dockerfile = files["Dockerfile"]
            self.assertIn("chown app:app /app", dockerfile)
            self.assertIn("USER app", dockerfile)

        def test_web_service_restarts(self):
            (self.root / "localllmchat.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
            _, files = self.config(Mode.BASE)
            self.assertIn('restart: "unless-stopped"', files["compose.yaml"])

        def test_healthchecks_present(self):
            _, files = self.config(Mode.GENAI)
            y = files["compose.yaml"]
            self.assertEqual(y.count("healthcheck:"), 3)
            for part in ('redis-cli', 'ollama', 'urllib.request'):
                self.assertIn(part, y)

        def test_services_override(self):
            t = target_for(str(self.root / "localllmchat.py"))
            settings = Settings(t, mode=Mode.BASE, services_override=parse_services("redis,ollama"))
            plan = Planner().build(settings, Inspector().inspect(t))
            self.assertEqual(plan.services, ("ollama", "redis"))
            self.assertNotIn("mlflow", Renderer().files(plan)["compose.yaml"])

        def test_services_empty_explicit(self):
            t = target_for(str(self.root / "localllmchat.py"))
            settings = Settings(t, mode=Mode.GENAI, services_override=parse_services("none"))
            plan = Planner().build(settings, Inspector().inspect(t))
            self.assertEqual(plan.services, ())

        def test_services_unknown_rejected(self):
            for value in ("postgres", "redis,", "redis,redis", "", "redis;ollama"):
                with self.assertRaises(OpsError):
                    parse_services(value)

        def test_services_menu_back(self):
            c = Console(Settings(target_for(str(self.root / "localllmchat.py"))))
            with patch("sys.stdout", new_callable=io.StringIO):
                c.execute("services ollama")
                self.assertEqual(c.settings.services_override, ("ollama",))
                c.execute("back")
            self.assertIsNone(c.settings.services_override)

        def test_doctor_missing_docker(self):
            with patch("shutil.which", return_value=None):
                result = EnvironmentDoctor().report()
            self.assertFalse(dict((name, status) for name, status, _ in result)["Docker Engine"])

        def test_doctor_cli_works_without_project(self):
            with patch("shutil.which", return_value=None):
                with patch("sys.stdout", new_callable=io.StringIO) as o:
                    self.assertEqual(main(["doctor"]), 0)
            self.assertIn("Docker CLI not found", o.getvalue())

        def test_ignored_secret_patterns(self):
            _, files = self.config(Mode.BASE)
            self.assertIn(".env.*", files["Dockerfile.dockerignore"])

        def test_fastapi_nested_path_with_spaces(self):
            folder = self.root / "my nested api"
            folder.mkdir()
            script = folder / "app.py"
            script.write_text("from fastapi import FastAPI\napp=FastAPI()\n")
            target = target_for(str(self.root))
            plan = Planner().build(Settings(target, mode=Mode.BASE, entry="my nested api/app.py"),
                                   Inspector().inspect(target, "my nested api/app.py"))
            self.assertEqual(plan.command[3], "app:app")
            self.assertEqual(plan.command[5], "my nested api")

        def test_mode_resets_custom_services(self):
            c = Console(Settings(target_for(str(self.root / "localllmchat.py"))))
            with patch("sys.stdout", new_callable=io.StringIO):
                c.execute("services redis")
                c.execute("mode base")
            self.assertIsNone(c.settings.services_override)

        def test_model_weights_excluded_from_build_context(self):
            _, files = self.config(Mode.LLMOPS)
            patterns = files["Dockerfile.dockerignore"]
            for ext in ("gguf", "safetensors", "pt", "pth", "ckpt", "onnx"):
                self.assertIn("**/*." + ext, patterns)

        def test_backup_can_be_recovered_after_interruption(self):
            _, files = self.config(Mode.BASE)
            manager = OutputManager(self.root)
            manager.write(files)
            backup = manager.write({**files, "INSTRUCTIONS.txt": files["INSTRUCTIONS.txt"] + "changed"})
            self.assertIsNotNone(backup)
            # Simulate losing the new folder after a system crash.
            shutil.rmtree(manager.folder)
            used = manager.restore_last_backup()
            self.assertEqual(used, backup)
            self.assertTrue(manager.validate()[0])

        def test_recovery_never_overwrites_live_output(self):
            manager = OutputManager(self.root)
            manager.write(self.config()[1])
            with self.assertRaisesRegex(OpsError, "will not overwrite"):
                manager.restore_last_backup()

        def test_recovery_rejects_corrupt_backup(self):
            manager = OutputManager(self.root)
            manager.write(self.config()[1])
            backup = manager.write(self.config(Mode.GENAI)[1])
            self.assertIsNotNone(backup)
            (backup / "compose.yaml").write_text("tampered")
            shutil.rmtree(manager.folder)
            with self.assertRaisesRegex(OpsError, "No intact"):
                manager.restore_last_backup()

        def test_active_process_lock_cannot_be_unlocked(self):
            with ProjectLock(self.root):
                with self.assertRaisesRegex(OpsError, "still running"):
                    ProjectLock(self.root).clear_stale()

        def test_verified_stale_lock_can_be_cleared(self):
            if os.name != "posix":
                self.skipTest("POSIX pid probe required")
            lock = ProjectLock(self.root)
            lock.path.mkdir()
            (lock.path / "owner.json").write_text(json.dumps({
                "pid": 999_999_991, "host": socket.gethostname(), "token": "test"
            }))
            with patch.object(ProjectLock, "_pid_alive", return_value=False):
                reason = lock.clear_stale()
            self.assertIn("exited", reason)
            self.assertFalse(lock.path.exists())

        def test_auto_recovers_verified_stale_lock_on_generate(self):
            if os.name != "posix":
                self.skipTest("POSIX process checks required")
            lock = ProjectLock(self.root)
            lock.path.mkdir()
            (lock.path / "owner.json").write_text(json.dumps({
                "pid": 999_999_991, "host": socket.gethostname(), "token": "old",
                "created_utc": "2020-01-01T00:00:00+00:00"
            }))
            with patch.object(ProjectLock, "_pid_alive", return_value=False):
                with patch("sys.stdout", new_callable=io.StringIO) as capture:
                    OutputManager(self.root).write(self.config()[1])
            self.assertIn("recovered stale lock", capture.getvalue())
            self.assertFalse(lock.path.exists())
            self.assertTrue((self.root / OUTPUT / "manifest.json").is_file())
            self.assertTrue(lock.guard_path.is_file())

        def test_legacy_unverified_lock_refuses_generation(self):
            lock = ProjectLock(self.root)
            lock.path.mkdir()
            (lock.path / "owner.json").write_text('{"pid":3}')
            with self.assertRaisesRegex(OpsError, "unverifiable|verifiable|still present"):
                OutputManager(self.root).write(self.config()[1])
            self.assertTrue(lock.path.exists())
            self.assertFalse((self.root / OUTPUT).exists())

        def test_active_lock_refuses_other_generation(self):
            with ProjectLock(self.root):
                with self.assertRaisesRegex(OpsError, "still running"):
                    OutputManager(self.root).write(self.config()[1])
                self.assertTrue((self.root / ".opsforge.lock").exists())
            self.assertFalse((self.root / ".opsforge.lock").exists())

        def test_pid_reuse_checked_using_start_signature(self):
            lock = ProjectLock(self.root)
            lock.path.mkdir()
            (lock.path / "owner.json").write_text(json.dumps({
                "pid": 9999, "host": socket.gethostname(), "proc_start": "previous-process"
            }))
            with patch.object(ProjectLock, "_pid_alive", return_value=True):
                with patch.object(ProjectLock, "_process_start", return_value="new-process"):
                    self.assertIn("replaced", lock.stale_reason())
                with patch.object(ProjectLock, "_process_start", return_value="previous-process"):
                    with self.assertRaisesRegex(OpsError, "still running"):
                        lock.stale_reason()

        def test_legacy_regular_file_lock_accepted_and_retained(self):
            path = self.root / ".opsforge.lock"
            path.touch(mode=0o600)
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                OutputManager(self.root).write(self.config()[1])
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_size, 0 if os.name == "posix" else 1)
            self.assertIn("compatible legacy file lock", out.getvalue())
            self.assertTrue(OutputManager(self.root).validate()[0])

        def test_legacy_file_lock_second_writer_refused(self):
            if os.name != "posix":
                self.skipTest("POSIX flock test")
            path = self.root / ".opsforge.lock"
            path.touch()
            import fcntl
            fd = os.open(path, os.O_RDWR)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with self.assertRaisesRegex(OpsError, "Legacy OpsForge process"):
                    OutputManager(self.root).write(self.config()[1])
                self.assertFalse((self.root / OUTPUT).exists())
                self.assertTrue(path.is_file())
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

        def test_legacy_lock_status_while_owned(self):
            path = self.root / ".opsforge.lock"
            path.touch()
            with ProjectLock(self.root) as lock:
                self.assertIn("currently held", lock.status())
                self.assertIsNotNone(lock._legacy_fd)
            self.assertTrue(path.is_file())

        def test_legacy_lock_status_and_explicit_unlock(self):
            path = self.root / ".opsforge.lock"
            path.touch()
            status = ProjectLock(self.root).status()
            self.assertIn("LEGACY FILE: kernel lock is available", status)
            self.assertTrue(path.is_file())
            with self.assertRaisesRegex(OpsError, "intentionally persistent"):
                ProjectLock(self.root).clear_stale()
            self.assertTrue(path.is_file())

        def test_legacy_lock_symlink_refused(self):
            if os.name == "nt":
                self.skipTest("Windows symlink permissions vary")
            external = self.root / "target.txt"
            external.write_text("do not change")
            (self.root / ".opsforge.lock").symlink_to(external)
            with self.assertRaisesRegex(OpsError, "symlinked lock path"):
                OutputManager(self.root).write(self.config()[1])
            self.assertEqual(external.read_text(), "do not change")

        def test_nonstandard_file_lock_contents_refused(self):
            path = self.root / ".opsforge.lock"
            path.write_text("unexpected data")
            with self.assertRaisesRegex(OpsError, "unfamiliar legacy lock file"):
                OutputManager(self.root).write(self.config()[1])
            self.assertEqual(path.read_text(), "unexpected data")

        def test_legacy_lock_migrates_no_files(self):
            path = self.root / ".opsforge.lock"
            path.touch()
            console = Console(Settings(target_for(str(self.root / "localllmchat.py"))))
            with patch("shutil.which", return_value=None), \
                 patch("sys.stdout", new_callable=io.StringIO) as output:
                console.execute("mode genaiops")
                console.execute("deps infer")
                console.execute("generate YES")
                console.execute("check")
            self.assertIn("Generated file integrity: OK", output.getvalue())
            self.assertTrue(path.is_file())

        def test_lock_status_readonly(self):
            lock = ProjectLock(self.root)
            self.assertIn("No active lock", lock.status())
            lock.path.mkdir()
            (lock.path / "owner.json").write_text(json.dumps({
                "pid": 999_999_991, "host": socket.gethostname(), "created_utc": "old"
            }))
            with patch.object(ProjectLock, "_pid_alive", return_value=False):
                self.assertIn("VERIFIED STALE", lock.status())
            self.assertTrue(lock.path.exists())

        def test_lock_guard_symlink_refused(self):
            if os.name == "nt":
                self.skipTest("Windows symlink permissions vary")
            lock = ProjectLock(self.root)
            external = self.root / "external.txt"
            external.write_text("do not change")
            lock.guard_path.symlink_to(external)
            with self.assertRaisesRegex(OpsError, "symlinked lock guard"):
                with ProjectLock(self.root):
                    pass
            self.assertEqual(external.read_text(), "do not change")

        def test_console_lock_inspection(self):
            console = Console(Settings(target_for(str(self.root / "localllmchat.py"))))
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertTrue(console.execute("lock"))
            self.assertIn("No active lock", out.getvalue())

        def test_preflight_infer_does_not_claim_strict_mode(self):
            (self.root / "localllmchat.py").write_text("import httpx\n")
            console = Console(Settings(target_for(str(self.root / "localllmchat.py")),
                                       dependency_mode="infer"))
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertTrue(console.execute("preflight"))
            self.assertIn("Dependency inference enabled", out.getvalue())
            self.assertNotIn("strict mode installs no third-party", out.getvalue())

        def test_unverifiable_legacy_lock_is_not_deleted(self):
            lock = ProjectLock(self.root)
            lock.path.mkdir()
            (lock.path / "owner.json").write_text('{"pid": 3}')
            with self.assertRaises(OpsError):
                lock.clear_stale()
            self.assertTrue(lock.path.exists())

        def test_binary_edits_are_backed_up_not_crashed(self):
            manager = OutputManager(self.root)
            files = self.config()[1]
            manager.write(files)
            (manager.folder / "Dockerfile").write_bytes(b"\xff\x00corrupt")
            _, changed, _ = manager.state(files)
            self.assertIn("Dockerfile", changed)
            backup = manager.write(files)
            self.assertEqual((backup / "Dockerfile").read_bytes(), b"\xff\x00corrupt")

        def test_compose_topology_all_modes(self):
            try:
                import yaml  # optional test dependency only
            except ImportError:
                self.skipTest("PyYAML unavailable")
            for mode in (Mode.BASE, Mode.MLOPS, Mode.LLMOPS, Mode.GENAI):
                plan, files = self.config(mode)
                parsed = yaml.safe_load(files["compose.yaml"])
                self.assertEqual(set(parsed["services"]), {"app", *plan.services})
                self.assertEqual(parsed["services"]["app"]["build"]["context"], "..")
                self.assertEqual(parsed["services"]["app"]["build"]["dockerfile"], ".opsforge/Dockerfile")
                for service in plan.services:
                    self.assertEqual(parsed["services"]["app"]["depends_on"][service]["condition"],
                                     "service_healthy")

        def test_manifest_with_non_dict_files_is_rejected(self):
            manager = OutputManager(self.root)
            manager.write(self.config()[1])
            manifest = manager.folder / "manifest.json"
            value = json.loads(manifest.read_text())
            value["files"] = []
            manifest.write_text(json.dumps(value))
            ok, errors = manager.validate()
            self.assertFalse(ok)
            self.assertIn("files must be a dictionary", errors[0])

        def test_broken_output_symlink_rejected(self):
            broken = self.root / "gone"
            self._safe_symlink(self.root / OUTPUT, broken, target_is_directory=True)
            with self.assertRaises(OpsError):
                OutputManager(self.root).write(self.config()[1])

        def test_noninteractive_generate_without_explicit_path_refused(self):
            with patch("sys.stderr", new_callable=io.StringIO) as capture:
                self.assertEqual(main(["generate", "--yes"]), 2)
            self.assertIn("explicit project path", capture.getvalue())

        def test_recover_console_no_backup_is_recoverable(self):
            console = Console(Settings(target_for(str(self.root / "localllmchat.py"))))
            with patch("sys.stdout", new_callable=io.StringIO):
                with self.assertRaisesRegex(OpsError, "No Auto Docker backups"):
                    console.execute("recover")
            self.assertFalse((self.root / OUTPUT).exists())

        def test_recover_pending_cancel_does_not_restore(self):
            manager = OutputManager(self.root)
            manager.write(self.config()[1])
            manager.write(self.config(Mode.GENAI)[1])
            shutil.rmtree(manager.folder)
            console = Console(Settings(target_for(str(self.root / "localllmchat.py"))))
            with patch("builtins.input", side_effect=AssertionError("nested input is forbidden")), \
                 patch("sys.stdout", new_callable=io.StringIO):
                console.execute("recover")
                self.assertIsNotNone(console.pending)
                console.execute("cancel")
            self.assertIsNone(console.pending)
            self.assertFalse(manager.folder.exists())

        def test_cli_recover_requires_confirmation(self):
            with patch("sys.stdin.isatty", return_value=False), patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(main(["recover", str(self.root / "localllmchat.py")]), 2)

        def test_yaml_external_if_available(self):
            try:
                import yaml  # optional test-only; not required to run Auto Docker
            except ImportError:
                self.skipTest("PyYAML not installed")
            for mode in (Mode.BASE, Mode.MLOPS, Mode.LLMOPS, Mode.GENAI):
                _, files = self.config(mode)
                obj = yaml.safe_load(files["compose.yaml"])
                self.assertIn("app", obj["services"])

        def test_host_ports_unique_across_services(self):
            plan, files = self.config(Mode.GENAI)
            host_ports = [p.host for p in plan.published_ports]
            self.assertEqual(len(host_ports), len(set(host_ports)))
            self.assertEqual({p.service for p in plan.published_ports}, {'mlflow', 'ollama'})
            self.assertIn('"127.0.0.1:5000:5000"', files['compose.yaml'])

        def test_host_port_override_without_internal_port_change(self):
            target = target_for(str(self.root / 'localllmchat.py'))
            i = Inspector().inspect(target)
            settings = Settings(target, mode=Mode.LLMOPS, host_ports=(('mlflow', 5051), ('ollama', 11500)))
            plan = Planner().build(settings, i)
            content = Renderer().files(plan)['compose.yaml']
            self.assertIn('"127.0.0.1:5051:5000"', content)
            self.assertIn('"127.0.0.1:11500:11434"', content)
            self.assertIn('"--port"', content)

        def test_host_port_duplicate_rejected(self):
            target = target_for(str(self.root / 'localllmchat.py'))
            with self.assertRaisesRegex(OpsError, 'same port'):
                Planner().build(Settings(target, mode=Mode.LLMOPS,
                                         host_ports=(('mlflow', 11500), ('ollama', 11500))),
                                Inspector().inspect(target))

        def test_host_port_for_disabled_service_rejected(self):
            target = target_for(str(self.root / 'localllmchat.py'))
            with self.assertRaisesRegex(OpsError, 'disabled or portless'):
                Planner().build(Settings(target, mode=Mode.BASE, host_ports=(('mlflow', 5500),)),
                                Inspector().inspect(target))

        def test_host_port_non_numeric_rejected_cli(self):
            with patch('sys.stderr', new_callable=io.StringIO) as stderr:
                result = main(['preview', str(self.root / 'localllmchat.py'), '--host-port', 'mlflow=abc'])
            self.assertEqual(result, 2)
            self.assertIn('SERVICE=PORT', stderr.getvalue())

        def test_host_port_console_undo(self):
            console = Console(Settings(target_for(str(self.root / 'localllmchat.py')), mode=Mode.LLMOPS))
            with patch('sys.stdout', new_callable=io.StringIO):
                console.execute('hostport mlflow 5500')
                self.assertEqual(console.settings.host_ports, (('mlflow', 5500),))
                console.execute('back')
                self.assertEqual(console.settings.host_ports, ())
                console.execute('hostport mlflow 5500')
                console.execute('hostport mlflow auto')
            self.assertEqual(console.settings.host_ports, ())

        def test_explicit_ports_take_priority_over_automatic_ports(self):
            target = target_for(str(self.root / 'localllmchat.py'))
            plan = Planner().build(Settings(target, mode=Mode.LLMOPS,
                                            host_ports=(('ollama', 5000),)),
                                   Inspector().inspect(target))
            ports = {p.service: p.host for p in plan.published_ports}
            self.assertEqual(ports['ollama'], 5000)
            self.assertEqual(ports['mlflow'], 5001)

        def test_saved_ports_are_used_after_console_settings_change(self):
            target = target_for(str(self.root / 'localllmchat.py'))
            plan = Planner().build(Settings(target, mode=Mode.LLMOPS,
                                            host_ports=(('mlflow', 5999),)),
                                   Inspector().inspect(target))
            manager = OutputManager(self.root)
            manager.write(Renderer().files(plan))
            with patch('sys.stdout', new_callable=io.StringIO):
                console = Console(Settings(target, mode=Mode.BASE))
                console.execute('mode genaiops')
            actual = {p.service: p.host for p in manager.runtime_ports()}
            self.assertEqual(actual, {'mlflow': 5999, 'ollama': 11434})

        def test_doctor_invalid_project_path_is_clean_error(self):
            with patch('sys.stderr', new_callable=io.StringIO) as stderr:
                result = main(['doctor', str(self.root / 'no-such-project')])
            self.assertEqual(result, 2)
            self.assertIn('Path not found', stderr.getvalue())

        def test_runtime_manifest_port_mappings_round_trip(self):
            plan, files = self.config(Mode.GENAI)
            manager = OutputManager(self.root)
            manager.write(files)
            self.assertEqual(manager.runtime_ports(), plan.published_ports)

        def test_port_busy_detected_without_docker(self):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(('127.0.0.1', 0))
                sock.listen(1)
                port = sock.getsockname()[1]
                ok, info = PortAudit.check(PublishedPort('app', port, 8000))
            self.assertFalse(ok)
            self.assertIn('BUSY', info)

        def test_port_free_detected_without_docker(self):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            ok, _ = PortAudit.check(PublishedPort('app', port, 8000))
            self.assertTrue(ok)

        def test_health_evaluation_live_services(self):
            statuses = [{'Service': 'app', 'State': 'running', 'Health': '', 'Name': 'foo-app-1'},
                        {'Service': 'mlflow', 'State': 'running', 'Health': 'healthy', 'Name': 'foo-mlflow-1'}]
            good, lines = RuntimeHealth.evaluate(['app', 'mlflow'], False, statuses)
            self.assertTrue(good)
            self.assertTrue(any('readiness unknown' in x for x in lines))

        def test_health_evaluation_batch_success(self):
            ok, lines = RuntimeHealth.evaluate(['app'], True,
                         [{'Service': 'app', 'State': 'exited', 'ExitCode': 0}])
            self.assertTrue(ok)
            self.assertIn('COMPLETED', lines[0])

        def test_health_detects_batch_failure_and_missing_services(self):
            ok, lines = RuntimeHealth.evaluate(['app', 'ollama'], True,
                         [{'Service': 'app', 'State': 'exited', 'ExitCode': 1}])
            self.assertFalse(ok)
            self.assertTrue(any('MISSING' in x for x in lines))

        def test_health_detects_starting_as_not_yet_ready(self):
            ok, lines = RuntimeHealth.evaluate(['ollama'], False,
                         [{'Service': 'ollama', 'State': 'running', 'Health': 'starting'}])
            self.assertFalse(ok)
            self.assertIn('NOT READY', lines[0])

        def test_compose_ps_json_lines_and_array(self):
            rows = [{'Service': 'app', 'State': 'running'},
                    {'Service': 'mlflow', 'State': 'running'}]
            data = '\n'.join(json.dumps(row) for row in rows)
            self.assertEqual(RuntimeHealth.parse(data), rows)
            self.assertEqual(RuntimeHealth.parse(json.dumps(rows)), rows)
            with self.assertRaisesRegex(OpsError, 'invalid JSON'):
                RuntimeHealth.parse('{not-json}')

        def test_adapter_verify_with_fake_docker(self):
            plan, files = self.config(Mode.LLMOPS)
            OutputManager(self.root).write(files)
            statuses = [{'Service': service, 'State': 'running',
                         'Health': ('healthy' if service != 'app' else '')}
                        for service in ('app', 'mlflow', 'ollama')]
            fake = subprocess.CompletedProcess(args=[], returncode=0,
                                               stdout='\n'.join(json.dumps(r) for r in statuses), stderr='')
            with patch('shutil.which', return_value='/bin/docker'), patch('subprocess.run', return_value=fake) as run:
                ok, output = DockerAdapter(self.root).verify()
            self.assertTrue(ok)
            self.assertEqual(len(output), 3)
            self.assertIn('--format', run.call_args.args[0])
            self.assertIn('--all', run.call_args.args[0])

        def test_adapter_verify_rejects_no_metadata(self):
            _, files = self.config()
            manager = OutputManager(self.root)
            manager.write(files)
            manifest = manager.folder / 'manifest.json'
            obj = json.loads(manifest.read_text())
            obj.pop('runtime')
            manifest.write_text(json.dumps(obj))
            with self.assertRaisesRegex(OpsError, 're-generate'):
                DockerAdapter(self.root).verify()

        def test_manifest_rejects_invalid_runtime_ports(self):
            _, files = self.config()
            manager = OutputManager(self.root)
            manager.write(files)
            manifest = manager.folder / 'manifest.json'
            data = json.loads(manifest.read_text())
            data['runtime']['ports'] = {'app': 'injection'}
            manifest.write_text(json.dumps(data))
            valid, problems = manager.validate()
            self.assertFalse(valid)
            self.assertTrue(any('runtime metadata' in item for item in problems))

        def test_generated_mlfow_health_endpoint(self):
            _, files = self.config(Mode.MLOPS)
            self.assertIn('5000/health', files['compose.yaml'])


        def test_brand_and_version(self):
            self.assertEqual(VERSION, "3.0.0-rc7")
            _, files = self.config()
            self.assertEqual(json.loads(files["manifest.json"])["generator"], "Auto Docker")
            self.assertIn("Auto Docker", files["INSTRUCTIONS.txt"])

        def test_old_manifest_remains_compatible(self):
            _, files = self.config()
            legacy = json.loads(files["manifest.json"])
            legacy["generator"] = "OpsForge"
            files["manifest.json"] = json.dumps(legacy)
            manager = OutputManager(self.root)
            manager.write(files)
            self.assertTrue(manager.validate()[0])
            self.assertEqual(manager.state(self.config()[1])[1], ["manifest.json"])

        def test_generator_excludes_renamed_script(self):
            (self.root / "auto_docker.py").write_text("print('generator')")
            (self.root / "auto_docker_helpers.py").write_text("print('generator')")
            self.assertEqual(_collect_python(self.root), [self.root / "localllmchat.py"])

        def test_gradio_application_has_reachable_host_port(self):
            script = self.root / "chat.py"
            script.write_text("import gradio as gr\napp = gr.Interface(lambda x:x, 'text','text')\napp.launch()\n")
            target = target_for(str(script))
            plan = Planner().build(Settings(target, mode=Mode.BASE), Inspector().inspect(target))
            self.assertEqual(plan.port, 7860)
            compose = Renderer().files(plan)["compose.yaml"]
            self.assertIn('GRADIO_SERVER_NAME: "0.0.0.0"', compose)
            self.assertIn('GRADIO_SERVER_PORT: "7860"', compose)
            self.assertIn('"127.0.0.1:7860:7860"', compose)
            self.assertIn('restart: "unless-stopped"', compose)

        def test_nested_python_module_has_import_path(self):
            _, files = self.config()
            self.assertIn("PYTHONPATH=/app", files["Dockerfile"])

        def test_dependency_audit_warns_no_manifest(self):
            report = DependencyAudit().report(self.config()[0].inspection)
            self.assertIn("ollama", report.observed)
            self.assertIn("ollama", report.possible_missing)
            self.assertTrue(any("No requirements.txt" in x for x in report.warnings))

        def test_dependency_audit_recognizes_req_pin(self):
            (self.root / "requirements.txt").write_text("ollama>=0.3\n")
            report = DependencyAudit().report(self.config()[0].inspection)
            self.assertEqual(report.possible_missing, ())
            self.assertIn("ollama", report.declared)

        def test_dependency_audit_recursive_requirements(self):
            (self.root / "requirements.txt").write_text("-r requirements/base.txt\n")
            folder = self.root / "requirements"
            folder.mkdir()
            (folder / "base.txt").write_text("ollama==0.3.0\n")
            report = DependencyAudit().report(self.config()[0].inspection)
            self.assertNotIn("ollama", report.possible_missing)

        def test_dependency_audit_rejects_requirements_path_escape(self):
            (self.root / "requirements.txt").write_text("-r ../../outside.txt\n")
            report = DependencyAudit().report(self.config()[0].inspection)
            self.assertTrue(any("Cannot safely read requirements" in x for x in report.warnings))

        def test_deps_strict_does_not_install_guessed_packages(self):
            (self.root / "localllmchat.py").write_text("import httpx\n")
            target = target_for(str(self.root / "localllmchat.py"))
            plan = Planner().build(Settings(target), Inspector().inspect(target))
            generated = Renderer().files(plan)
            self.assertEqual(plan.inferred_packages, ())
            self.assertNotIn("pip install --no-cache-dir httpx", generated["Dockerfile"])
            self.assertFalse((self.root / 'requirements.txt').exists())

        def test_deps_infer_installs_known_httpx_in_generated_dockerfile_only(self):
            (self.root / "localllmchat.py").write_text("import httpx\n")
            target = target_for(str(self.root / "localllmchat.py"))
            plan = Planner().build(Settings(target, dependency_mode='infer'), Inspector().inspect(target))
            self.assertEqual(plan.inferred_packages, ('httpx',))
            generated = Renderer().files(plan)
            self.assertIn('RUN /opt/venv/bin/python -m pip install --no-cache-dir httpx', generated['Dockerfile'])
            self.assertIn('unpinned', generated['INSTRUCTIONS.txt'].lower())
            self.assertFalse((self.root / 'requirements.txt').exists())
            self.assertTrue(any('unpin' in x.lower() for x in plan.warnings))

        def test_deps_infer_deterministic_for_multiple_known_imports(self):
            (self.root / 'localllmchat.py').write_text('import redis\nimport httpx\n')
            target = target_for(str(self.root / 'localllmchat.py'))
            settings = Settings(target, dependency_mode='infer')
            p1 = Planner().build(settings, Inspector().inspect(target))
            p2 = Planner().build(settings, Inspector().inspect(target))
            self.assertEqual(p1.inferred_packages, ('httpx', 'redis'))
            self.assertEqual(Renderer().files(p1), Renderer().files(p2))

        def test_deps_infer_works_for_fastapi_without_manifest(self):
            app = self.root / 'api.py'
            app.write_text('from fastapi import FastAPI\napp=FastAPI()\n')
            target = target_for(str(app))
            plan = Planner().build(Settings(target, dependency_mode='infer'), Inspector().inspect(target))
            self.assertEqual(plan.inferred_packages, ('fastapi', 'uvicorn'))
            self.assertIn('pip install --no-cache-dir fastapi uvicorn', Renderer().files(plan)['Dockerfile'])

        def test_deps_infer_rejects_manifest_and_preserves_file(self):
            req = self.root / 'requirements.txt'
            req.write_text('ollama==0.4.0\n')
            target = target_for(str(self.root / 'localllmchat.py'))
            with self.assertRaisesRegex(OpsError, 'manifest'):
                Planner().build(Settings(target, dependency_mode='infer'), Inspector().inspect(target))
            self.assertEqual(req.read_text(), 'ollama==0.4.0\n')

        def test_deps_unknown_policy_rejected(self):
            target = target_for(str(self.root / 'localllmchat.py'))
            with self.assertRaisesRegex(OpsError, 'policy'):
                Planner().build(Settings(target, dependency_mode='everything'), Inspector().inspect(target))

        def test_deps_console_change_undo_and_status(self):
            target = target_for(str(self.root / 'localllmchat.py'))
            c = Console(Settings(target))
            with patch('sys.stdout', new_callable=io.StringIO) as out:
                c.execute('deps infer')
                c.execute('status')
            self.assertEqual(c.settings.dependency_mode, 'infer')
            self.assertIn('Dependencies: infer', out.getvalue())
            c.execute('back')
            self.assertEqual(c.settings.dependency_mode, 'strict')

        def test_deps_cli_inference_and_integrity(self):
            (self.root / 'localllmchat.py').write_text('import httpx\n')
            with patch('sys.stdout', new_callable=io.StringIO) as out:
                result = main(['generate', str(self.root / 'localllmchat.py'), '--mode', 'base',
                               '--deps', 'infer', '--yes'])
            self.assertEqual(result, 0)
            self.assertIn('httpx', out.getvalue())
            self.assertTrue(OutputManager(self.root).validate()[0])
            self.assertIn('pip install --no-cache-dir httpx', (self.root / OUTPUT / 'Dockerfile').read_text())
            self.assertFalse((self.root / 'requirements.txt').exists())
            meta = json.loads((self.root / OUTPUT / 'manifest.json').read_text())
            self.assertEqual(meta['runtime']['inferred_packages'], ['httpx'])

        def test_inferred_manifest_package_validation(self):
            (self.root / 'localllmchat.py').write_text('import httpx\n')
            target = target_for(str(self.root / 'localllmchat.py'))
            plan = Planner().build(Settings(target, dependency_mode='infer'), Inspector().inspect(target))
            files = Renderer().files(plan)
            meta = json.loads(files['manifest.json'])
            meta['runtime']['inferred_packages'] = ['bad; rm -rf /']
            files['manifest.json'] = json.dumps(meta)
            manager = OutputManager(self.root)
            manager.write(files)
            valid, errors = manager.validate()
            self.assertFalse(valid)
            self.assertIn('runtime metadata', ' '.join(errors))

        def test_dependency_audit_reachable_helper_imports(self):
            (self.root / 'localllmchat.py').write_text('import helper\n')
            (self.root / 'helper.py').write_text('import httpx\n')
            report = DependencyAudit().report(Inspector().inspect(target_for(str(self.root / 'localllmchat.py'))))
            self.assertIn('httpx', report.observed)
            self.assertIn('httpx', report.possible_missing)

        def test_dependency_audit_reachable_relative_imports(self):
            pkg = self.root / 'mypkg'
            pkg.mkdir()
            (pkg / '__init__.py').write_text('')
            (pkg / 'app.py').write_text('from . import helpers\n')
            (pkg / 'helpers.py').write_text('import httpx\n')
            report = DependencyAudit().report(Inspector().inspect(target_for(str(pkg / 'app.py'))))
            self.assertIn('httpx', report.observed)

        def test_dependency_audit_local_cycle_stays_bounded(self):
            (self.root / 'localllmchat.py').write_text('import a\n')
            (self.root / 'a.py').write_text('import b\n')
            (self.root / 'b.py').write_text('import a\nimport httpx\n')
            report = DependencyAudit().report(Inspector().inspect(target_for(str(self.root / 'localllmchat.py'))))
            self.assertIn('httpx', report.observed)
            self.assertFalse(any('hit 64' in x for x in report.warnings))

        def test_dependency_audit_does_not_follow_external_helpers(self):
            outside = Path(self.tmp.name) / 'external.py'
            outside.write_text('import httpx\n')
            self._safe_symlink(self.root / 'external.py', outside)
            (self.root / 'localllmchat.py').write_text('import external\n')
            report = DependencyAudit().report(Inspector().inspect(target_for(str(self.root / 'localllmchat.py'))))
            self.assertNotIn('httpx', report.observed)

        def test_deps_inference_keeps_image_dependencies_separate_from_sources(self):
            (self.root / 'localllmchat.py').write_text('import httpx\n')
            target = target_for(str(self.root / 'localllmchat.py'))
            files = Renderer().files(Planner().build(Settings(target, dependency_mode='infer'), Inspector().inspect(target)))
            self.assertNotIn('COPY . /build', files['Dockerfile'])
            self.assertIn('COPY --chown=app:app . /app', files['Dockerfile'])
            self.assertEqual(set(files), {'Dockerfile','Dockerfile.dockerignore','compose.yaml','INSTRUCTIONS.txt','manifest.json'})

        def test_dependency_audit_fastapi_needs_uvicorn(self):
            script = self.root / "api.py"
            script.write_text("from fastapi import FastAPI\napp = FastAPI()\n")
            (self.root / "requirements.txt").write_text("fastapi==0.118.0\n")
            report = DependencyAudit().report(Inspector().inspect(target_for(str(script))))
            self.assertIn("uvicorn", report.possible_missing)

        def test_dependency_audit_fastapi_standard_extra(self):
            script = self.root / "api.py"
            script.write_text("from fastapi import FastAPI\napp = FastAPI()\n")
            (self.root / "requirements.txt").write_text("fastapi[standard]>=0.118\n")
            report = DependencyAudit().report(Inspector().inspect(target_for(str(script))))
            self.assertNotIn("uvicorn", report.possible_missing)

        def test_dependency_audit_pyproject_dependencies(self):
            (self.root / "pyproject.toml").write_text('[project]\nname = "example-app"\nversion = "0.1.0"\ndependencies = ["ollama>=0.3"]\n')
            report = DependencyAudit().report(self.config()[0].inspection)
            if sys.version_info >= (3, 11):
                self.assertIn("ollama", report.declared)
                self.assertEqual(report.possible_missing, ())
            else:
                self.assertTrue(any("tomllib" in x for x in report.warnings))

        def test_preflight_console_does_not_write(self):
            console = Console(Settings(target_for(str(self.root / "localllmchat.py"))))
            with patch("sys.stdout", new_callable=io.StringIO) as captured:
                self.assertTrue(console.execute("preflight"))
            self.assertIn("dependency preflight", captured.getvalue())
            self.assertFalse((self.root / OUTPUT).exists())

        def test_preflight_cli(self):
            with patch("sys.stdout", new_callable=io.StringIO) as captured:
                code = main(["preflight", str(self.root / "localllmchat.py")])
            self.assertEqual(code, 0)
            self.assertIn("No requirements.txt", captured.getvalue())

        def test_generated_files_validate_after_brand_upgrade(self):
            _, files = self.config(Mode.GENAI)
            manager = OutputManager(self.root)
            manager.write(files)
            self.assertTrue(manager.validate()[0])
            self.assertEqual(len(manager.runtime_ports()), 2)
            _, changed, same = manager.state(self.config(Mode.GENAI)[1])
            self.assertFalse(changed)
            self.assertEqual(len(same), 5)


        def test_web_apps_have_tcp_healthcheck(self):
            code = {
                "fastapi": "from fastapi import FastAPI\napp = FastAPI()\n",
                "flask": "from flask import Flask\napp = Flask(__name__)\n",
                "streamlit": "import streamlit\nstreamlit.title('Hello')\n",
                "gradio": "import gradio as gr\ngr.Interface(lambda x:x,'text','text').launch()\n",
            }
            for framework, src in code.items():
                with self.subTest(framework=framework):
                    file = self.root / f"{framework}_demo.py"
                    file.write_text(src)
                    target = target_for(str(file))
                    plan = Planner().build(Settings(target, mode=Mode.BASE), Inspector().inspect(target))
                    compose = Renderer().files(plan)['compose.yaml']
                    self.assertIn('healthcheck:', compose)
                    self.assertIn('socket.create_connection', compose)
                    self.assertIn(str(plan.port), compose)

        def test_batch_apps_do_not_have_tcp_healthcheck(self):
            _, files = self.config(Mode.BASE)
            self.assertNotIn('healthcheck:', files['compose.yaml'])

        def test_runtime_evaluator_flags_web_app_health_failure(self):
            status = [{'Service': 'app', 'State': 'running', 'Health': 'unhealthy', 'ExitCode': 0}]
            good, messages = RuntimeHealth.evaluate(['app'], False, status)
            self.assertFalse(good)
            self.assertIn('NOT READY', messages[0])

        def test_doctor_sandbox_shows_distinct_host_state(self):
            host_commands = []
            def fake_probe(args):
                host_commands.append(args)
                if 'compose version' in args[-1]:
                    return False, 'docker: unknown command: docker compose'
                if 'docker info' in args[-1]:
                    return False, 'failed to connect to the Docker API'
                return True, 'Docker version 29.9.2'
            with patch.object(EnvironmentDoctor, 'in_flatpak', return_value=True), \
                 patch('shutil.which', side_effect=lambda x: '/usr/bin/flatpak-spawn' if x == 'flatpak-spawn' else None), \
                 patch.object(EnvironmentDoctor, '_probe', side_effect=fake_probe):
                rows = EnvironmentDoctor().report()
            state = {name: (ok, detail) for name, ok, detail in rows}
            self.assertTrue(state['Environment'][0])
            self.assertFalse(state['Docker CLI'][0])
            self.assertTrue(state['Host Docker CLI'][0])
            self.assertFalse(state['Host Compose'][0])
            self.assertFalse(state['Host Engine'][0])
            self.assertTrue(all(cmd[:3] == ['/usr/bin/flatpak-spawn', '--host', 'sh'] for cmd in host_commands))

        def test_doctor_sandbox_no_host_bridge_degrades_cleanly(self):
            with patch.object(EnvironmentDoctor, 'in_flatpak', return_value=True), \
                 patch('shutil.which', return_value=None):
                rows = EnvironmentDoctor().report()
            self.assertEqual(dict((name, detail) for name, _, detail in rows)['Host access'],
                             'flatpak-spawn is not available in this sandbox')

        def test_missing_docker_build_does_not_request_approval(self):
            console = Console(Settings(target_for(str(self.root / 'localllmchat.py'))))
            OutputManager(self.root).write(self.config()[1])
            with patch('shutil.which', return_value=None), \
                 patch.object(EnvironmentDoctor, 'in_flatpak', return_value=True), \
                 patch('sys.stdout', new_callable=io.StringIO) as output:
                with self.assertRaisesRegex(OpsError, 'Flatpak'):
                    console.execute('build')
            self.assertIsNone(console.pending)
            self.assertNotIn('Approval pending', output.getvalue())

        def test_missing_compose_fails_before_build_confirmation(self):
            console = Console(Settings(target_for(str(self.root / 'localllmchat.py'))))
            OutputManager(self.root).write(self.config()[1])
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(False, 'unknown command')):
                with self.assertRaisesRegex(OpsError, 'Docker Compose unavailable'):
                    console.execute('build YES')
            self.assertIsNone(console.pending)

        def test_missing_engine_fails_before_up_confirmation(self):
            console = Console(Settings(target_for(str(self.root / 'localllmchat.py'))))
            OutputManager(self.root).write(self.config()[1])
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(EnvironmentDoctor, '_probe', side_effect=[(True, 'Compose 2'), (False, 'no daemon')]):
                with self.assertRaisesRegex(OpsError, 'Docker Engine unavailable'):
                    console.execute('up')
            self.assertIsNone(console.pending)

        def test_cli_build_requires_docker_before_prompt(self):
            OutputManager(self.root).write(self.config()[1])
            with patch('shutil.which', return_value=None), \
                 patch('builtins.input', side_effect=AssertionError('no confirmation prompt allowed')), \
                 patch('sys.stderr', new_callable=io.StringIO) as stderr:
                code = main(['build', str(self.root / 'localllmchat.py')])
            self.assertEqual(code, 2)
            self.assertIn('Docker CLI not found', stderr.getvalue())

        def test_probe_exposes_readonly_daemon_error(self):
            from subprocess import CompletedProcess
            with patch('subprocess.run', return_value=CompletedProcess(['docker'], 1, '',
                               'failed to connect to Docker API at unix:///var/run/docker.sock')):
                result = EnvironmentDoctor._probe(['docker', 'info'])
            self.assertFalse(result[0])
            self.assertIn('failed to connect', result[1])


        def test_context_validation_rejects_dangerous_names(self):
            for value in ('-bad', '../escape', 'name with spaces', '', 'x' * 70, 'bad;echo'):
                with self.subTest(value=value), self.assertRaises(OpsError):
                    DockerAdapter.validate_context(value)

        def test_context_named_prefix_never_changes_default(self):
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch('subprocess.run', side_effect=AssertionError('must not change global context')):
                adapter = DockerAdapter(self.root, 'remote-ssh')
                self.assertEqual(adapter._prefix(), ['/usr/bin/docker', '--context', 'remote-ssh'])
                self.assertEqual(adapter._compose_command('ps')[:4],
                                 ['/usr/bin/docker', '--context', 'remote-ssh', 'compose'])

        def test_console_context_restores_via_back(self):
            c = Console(Settings(target_for(str(self.root / 'localllmchat.py'))))
            with patch('subprocess.run', side_effect=AssertionError('context setter is read-only')):
                c.execute('context remote-ssh')
            self.assertEqual(c.settings.docker_context, 'remote-ssh')
            c.execute('back')
            self.assertIsNone(c.settings.docker_context)
            c.execute('context another')
            c.execute('project ' + str(self.root / 'localllmchat.py'))
            self.assertEqual(c.settings.docker_context, 'another')
            c.execute('context auto')
            self.assertIsNone(c.settings.docker_context)

        def test_preflight_checks_compose_after_engine(self):
            OutputManager(self.root).write(self.config()[1])
            captured = []
            def probe(args):
                captured.append(args)
                return True, 'ok'
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(EnvironmentDoctor, '_probe', side_effect=probe):
                DockerAdapter(self.root, 'remote-ssh').preflight()
            self.assertEqual(len(captured), 3)
            self.assertEqual([c[1:3] for c in captured], [['--context','remote-ssh']] * 3)
            self.assertEqual(captured[-1][-2:], ['config', '-q'])

        def test_preflight_rejects_compose_syntax_before_approval(self):
            OutputManager(self.root).write(self.config()[1])
            c = Console(Settings(target_for(str(self.root / 'localllmchat.py'))))
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(EnvironmentDoctor, '_probe', side_effect=[
                     (True, 'Compose OK'), (True, 'Engine OK'), (False, 'invalid YAML')]):
                with self.assertRaisesRegex(OpsError, 'Compose configuration unavailable'):
                    c.execute('build YES')
            self.assertIsNone(c.pending)

        def test_remote_up_does_not_probe_local_ports(self):
            OutputManager(self.root).write(self.config(Mode.LLMOPS)[1])
            c = Console(Settings(target_for(str(self.root / 'localllmchat.py')),
                                 mode=Mode.LLMOPS, docker_context='remote-ssh'))
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'ok')), \
                 patch.object(PortAudit, 'print_report', side_effect=AssertionError('no local ports')), \
                 patch('sys.stdout', new_callable=io.StringIO) as captured:
                c.execute('up')
            self.assertIsNotNone(c.pending)
            self.assertIn('Remote context', captured.getvalue())

        def test_dryrun_uses_compose_flag_and_no_up_command(self):
            OutputManager(self.root).write(self.config()[1])
            from subprocess import CompletedProcess
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'ok')), \
                 patch('subprocess.run', return_value=CompletedProcess(['docker'],0)) as run:
                DockerAdapter(self.root, 'remote-ssh').run('dryrun')
            args = run.call_args.args[0]
            self.assertEqual(args[:4], ['/usr/bin/docker', '--context', 'remote-ssh', 'compose'])
            self.assertEqual(args[4], '--dry-run')
            self.assertEqual(args[-3:], ['up', '--build', '-d'])

        def test_diagnose_reports_actionable_health_without_logs(self):
            OutputManager(self.root).write(self.config()[1])
            c = Console(Settings(target_for(str(self.root / 'localllmchat.py'))))
            with patch.object(DockerAdapter, 'verify', return_value=(False, [
                     'app: NOT READY (state=exited, health=n/a, exit=1)'])), \
                 patch('subprocess.run', side_effect=AssertionError('read-only diagnose should not run logs')), \
                 patch('sys.stdout', new_callable=io.StringIO) as captured:
                c.execute('diagnose')
            self.assertIn('SUGGESTION', captured.getvalue())
            self.assertIn('exited', captured.getvalue())

        def test_offline_audit_warning_for_unpinned_packages(self):
            (self.root / 'localllmchat.py').write_text('import httpx\n')
            target = target_for(str(self.root / 'localllmchat.py'))
            plan = Planner().build(Settings(target, Mode.GENAI, dependency_mode='infer'),
                                   Inspector().inspect(target))
            OutputManager(self.root).write(Renderer().files(plan))
            ok, rows = OfflineDeploymentAudit.evaluate(self.root)
            self.assertTrue(ok, rows)
            self.assertIn('unpinned', ' '.join(rows).lower())
            self.assertIn('latest tag', ' '.join(rows).lower())

        def test_offline_audit_detects_tampered_output(self):
            OutputManager(self.root).write(self.config()[1])
            (self.root / OUTPUT / 'Dockerfile').write_text('USER root\n')
            ok, lines = OfflineDeploymentAudit.evaluate(self.root)
            self.assertFalse(ok)
            self.assertTrue(any('FAIL' in line for line in lines))

        def test_offline_audit_never_calls_docker(self):
            OutputManager(self.root).write(self.config()[1])
            with patch('subprocess.run', side_effect=AssertionError('offline audit uses no Docker')):
                self.assertTrue(OfflineDeploymentAudit.print_report(self.root))

        def test_cli_audit_works_without_docker(self):
            OutputManager(self.root).write(self.config()[1])
            with patch('shutil.which', return_value=None), \
                 patch('sys.stdout', new_callable=io.StringIO) as captured:
                code = main(['audit', str(self.root / 'localllmchat.py')])
            self.assertEqual(code, 0)
            self.assertIn('offline deployment audit', captured.getvalue())

        def test_cli_context_rejects_injection(self):
            with patch('sys.stderr', new_callable=io.StringIO) as stderr:
                code = main(['preview', str(self.root / 'localllmchat.py'),
                             '--docker-context', '../oops'])
            self.assertEqual(code, 2)
            self.assertIn('Invalid Docker context', stderr.getvalue())

        def test_doctor_and_generation_preserve_legacy_single_file_layout(self):
            _, files = self.config(Mode.GENAI)
            self.assertEqual(set(files), {'Dockerfile', 'Dockerfile.dockerignore',
                                         'compose.yaml', 'INSTRUCTIONS.txt', 'manifest.json'})
            self.assertIn('schema', files['manifest.json'])
            OutputManager(self.root).write(files)
            self.assertTrue(OutputManager(self.root).validate()[0])

        def test_runtime_hints_are_bounded_and_not_unsafely_speculative(self):
            hints = DockerAdapter.health_hints(['app: MISSING (container not found)'])
            self.assertIn('up', hints[0])
            self.assertNotIn('delete', ' '.join(hints))
            self.assertIn('readiness', ' '.join(DockerAdapter.health_hints(['app: RUNNING (no healthcheck; readiness unknown)'])).lower())

        def test_dryrun_without_docker_fails_before_container_operations(self):
            OutputManager(self.root).write(self.config()[1])
            with patch('shutil.which', return_value=None):
                with self.assertRaises(OpsError):
                    DockerAdapter(self.root).run('dryrun')

        def test_interactive_stdin_detected_automatically(self):
            (self.root / 'chat.py').write_text("name = input('name? ')\nprint(name)\n")
            target = target_for(str(self.root / 'chat.py'))
            inspection = Inspector().inspect(target)
            self.assertTrue(inspection.entry.needs_stdin)
            plan = Planner().build(Settings(target), inspection)
            self.assertEqual(plan.execution_kind, 'interactive')
            output = Renderer().files(plan)
            self.assertIn('stdin_open: true', output['compose.yaml'])
            self.assertIn('tty: true', output['compose.yaml'])
            self.assertIn('"execution_kind": "interactive"', output['manifest.json'])
            self.assertIn('run --rm app', output['INSTRUCTIONS.txt'])

        def test_readline_and_getpass_stdin_detection(self):
            for source in ('import sys\nsys.stdin.readline()\n',
                           'import getpass\ngetpass.getpass()\n'):
                (self.root / 'chat.py').write_text(source)
                target = target_for(str(self.root / 'chat.py'))
                self.assertEqual(Planner().build(Settings(target), Inspector().inspect(target)).execution_kind,
                                 'interactive')

        def test_noninteractive_python_is_batch(self):
            plan, files = self.config(Mode.BASE)
            self.assertEqual(plan.execution_kind, 'batch')
            self.assertIn('"batch": true', files['manifest.json'])
            self.assertNotIn('stdin_open:', files['compose.yaml'])
            self.assertIn('job YES', files['INSTRUCTIONS.txt'])

        def test_generic_web_contract_requires_port(self):
            target = target_for(str(self.root / 'localllmchat.py'))
            with self.assertRaisesRegex(OpsError, 'need a port'):
                Planner().build(Settings(target, execution_kind='web'), Inspector().inspect(target))
            plan = Planner().build(Settings(target, port=8502, execution_kind='web'), Inspector().inspect(target))
            self.assertEqual(plan.execution_kind, 'web')
            output = Renderer().files(plan)['compose.yaml']
            self.assertIn('healthcheck:', output)
            self.assertIn('restart: "unless-stopped"', output)

        def test_unknown_kind_and_conflicting_web_kind_rejected(self):
            target = target_for(str(self.root / 'localllmchat.py'))
            with self.assertRaisesRegex(OpsError, 'Execution kind'):
                Planner().build(Settings(target, execution_kind='foo'), Inspector().inspect(target))
            (self.root / 'api.py').write_text('from fastapi import FastAPI\napp = FastAPI()\n')
            target = target_for(str(self.root / 'api.py'))
            with self.assertRaisesRegex(OpsError, 'Web-framework'):
                Planner().build(Settings(target, execution_kind='interactive'), Inspector().inspect(target))

        def test_interactive_up_refused_before_docker_preflight(self):
            (self.root / 'chat.py').write_text("print(input('chat> '))\n")
            target = target_for(str(self.root / 'chat.py'))
            plan = Planner().build(Settings(target), Inspector().inspect(target))
            OutputManager(self.root).write(Renderer().files(plan))
            console = Console(Settings(target))
            with patch.object(DockerAdapter, 'preflight', side_effect=AssertionError('must not call Docker')):
                with self.assertRaisesRegex(OpsError, 'cannot be started detached'):
                    console.execute('up YES')
            self.assertIsNone(console.pending)

        def test_saved_kind_controls_up_not_unwritten_settings(self):
            (self.root / 'chat.py').write_text('print(input())\n')
            target = target_for(str(self.root / 'chat.py'))
            OutputManager(self.root).write(Renderer().files(Planner().build(Settings(target), Inspector().inspect(target))))
            self.assertEqual(OutputManager(self.root).execution_kind(), 'interactive')
            console = Console(Settings(target, execution_kind='batch'))
            with patch.object(DockerAdapter, 'preflight', side_effect=AssertionError('must not call Docker')):
                with self.assertRaisesRegex(OpsError, 'cannot be started detached'):
                    console.execute('up YES')

        def test_batch_job_includes_rm_and_no_tty(self):
            OutputManager(self.root).write(self.config()[1])
            from subprocess import CompletedProcess
            with patch.object(DockerAdapter, 'preflight'), \
                 patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch('subprocess.run', return_value=CompletedProcess(['docker'], 0)) as run:
                DockerAdapter(self.root).run('job')
            args = run.call_args.args[0]
            self.assertEqual(args[-4:], ['run', '--rm', '-T', 'app'])
            self.assertIsNone(run.call_args.kwargs['timeout'])

        def test_interactive_run_requires_real_tty(self):
            OutputManager(self.root).write(self.config()[1])
            with patch.object(DockerAdapter, 'preflight'), \
                 patch('sys.stdin', new_callable=io.StringIO), \
                 patch('subprocess.run', side_effect=AssertionError('must not run')):
                with self.assertRaisesRegex(OpsError, 'real terminal'):
                    DockerAdapter(self.root).run('run')

        def test_interactive_run_command_keeps_tty(self):
            OutputManager(self.root).write(self.config()[1])
            from subprocess import CompletedProcess
            with patch.object(DockerAdapter, 'preflight'), \
                 patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch('sys.stdin') as stdin, patch('sys.stdout') as stdout, \
                 patch('subprocess.run', return_value=CompletedProcess(['docker'], 0)) as run:
                stdin.isatty.return_value = True
                stdout.isatty.return_value = True
                DockerAdapter(self.root).run('run')
            self.assertEqual(run.call_args.args[0][-3:], ['run', '--rm', 'app'])
            self.assertIsNone(run.call_args.kwargs['timeout'])

        def test_job_command_rejects_web_contract_before_docker(self):
            target = target_for(str(self.root / 'localllmchat.py'))
            plan = Planner().build(Settings(target, port=8081, execution_kind='web'), Inspector().inspect(target))
            OutputManager(self.root).write(Renderer().files(plan))
            console = Console(Settings(target, execution_kind='batch'))
            with patch.object(DockerAdapter, 'preflight', side_effect=AssertionError('no Docker')):
                with self.assertRaisesRegex(OpsError, 'batch scripts only'):
                    console.execute('job YES')

        def test_cli_kind_preview(self):
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                code = main(['preview', str(self.root / 'localllmchat.py'), '--kind', 'interactive'])
            self.assertEqual(code, 0)
            self.assertIn('Execution  : interactive', output.getvalue())

        def test_legacy_manifest_kind_defaults_from_batch(self):
            OutputManager(self.root).write(self.config()[1])
            manifest_path = self.root / OUTPUT / 'manifest.json'
            document = json.loads(manifest_path.read_text())
            del document['runtime']['execution_kind']
            manifest_path.write_text(json.dumps(document))
            manager = OutputManager(self.root)
            self.assertTrue(manager.validate()[0])
            self.assertEqual(manager.execution_kind(), 'batch')

        def test_invalid_runtime_kind_refused(self):
            OutputManager(self.root).write(self.config()[1])
            manifest_path = self.root / OUTPUT / 'manifest.json'
            document = json.loads(manifest_path.read_text())
            document['runtime']['execution_kind'] = 'unknown'
            manifest_path.write_text(json.dumps(document))
            self.assertFalse(OutputManager(self.root).validate()[0])

        def test_real_integration_test_requires_explicit_yes(self):
            with patch('sys.stderr', new_callable=io.StringIO) as output, \
                 patch('subprocess.run', side_effect=AssertionError('no real Docker')):
                code = main(['integration-test'])
            self.assertEqual(code, 2)
            self.assertIn('pass --yes', output.getvalue())

        def test_real_integration_test_missing_engine_graceful(self):
            with patch.object(EnvironmentDoctor, 'in_flatpak', return_value=False), \
                 patch('shutil.which', return_value=None), \
                 patch('sys.stderr', new_callable=io.StringIO) as output:
                code = main(['integration-test', '--yes'])
            self.assertEqual(code, 2)
            self.assertIn('Docker CLI', output.getvalue())

        def test_kind_setting_back_restores_previous(self):
            console = Console(Settings(target_for(str(self.root / 'localllmchat.py'))))
            console.execute('kind interactive')
            self.assertEqual(console.settings.execution_kind, 'interactive')
            console.execute('back')
            self.assertEqual(console.settings.execution_kind, 'auto')

        # RC3: proof quality, negative cases, and path-only security checks.
        def test_rc3_proof_report_atomic_and_blocked(self):
            report = self.root / 'proof.json'
            evidence = ProofEvidence('unit', report)
            evidence.record('engine', 'BLOCKED', 'No Engine')
            evidence.save()
            document = json.loads(report.read_text())
            self.assertEqual(document['overall'], 'BLOCKED')
            self.assertEqual(document['suite'], 'unit')
            self.assertFalse(evidence.passed())
            if os.name == 'posix':
                self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)

        def test_rc3_proof_failing_cleanup_cannot_pass(self):
            adapter = DockerAdapter(self.root)
            from subprocess import CompletedProcess
            with patch.object(DockerAdapter, '_compose_command', return_value=['fake-docker']), \
                 patch(f"{__name__}._proof_ok"), \
                 patch(f"{__name__}._proof_command", return_value=CompletedProcess([], 0, 'container123', '')):
                with self.assertRaisesRegex(OpsError, 'Cleanup failed'):
                    _cleanup_proof(adapter, self.root)

        def test_rc3_proof_duplicate_bad_status_refused(self):
            evidence = ProofEvidence('unit')
            with self.assertRaises(OpsError):
                evidence.record('x', 'UNKNOWN', 'bad')
            evidence.record('one', 'PASS', 'ok')
            evidence.record('two', 'FAIL', 'not ok')
            self.assertFalse(evidence.passed())

        def test_rc3_no_engine_generates_blocked_evidence(self):
            path = self.root / 'blocked.json'
            with patch.object(EnvironmentDoctor, 'in_flatpak', return_value=False), \
                 patch('shutil.which', return_value=None), \
                 patch('sys.stderr', new_callable=io.StringIO):
                rc = main(['integration-test', '--yes', '--report', str(path)])
            self.assertEqual(rc, 2)
            self.assertEqual(json.loads(path.read_text())['overall'], 'BLOCKED')

        def test_rc3_ai_engine_unavailable_reports_blocked(self):
            path = self.root / 'ai.json'
            with patch.object(EnvironmentDoctor, 'in_flatpak', return_value=False), \
                 patch('shutil.which', return_value=None), \
                 patch('sys.stderr', new_callable=io.StringIO):
                rc = main(['ai-integration-test', '--yes', '--model', 'tinyllama', '--report', str(path)])
            self.assertEqual(rc, 2)
            self.assertEqual(json.loads(path.read_text())['overall'], 'BLOCKED')

        def test_rc3_ai_pull_requires_model(self):
            with self.assertRaisesRegex(OpsError, 'requires'):
                ai_integration_test(pull=True)

        def test_rc3_ai_invalid_model_refused_before_docker(self):
            with self.assertRaisesRegex(OpsError, 'Invalid model'):
                ai_integration_test(model='bad model; rm -rf /')

        def test_rc3_test_requires_explicit_approval(self):
            with patch('sys.stderr', new_callable=io.StringIO) as stderr:
                rc = main(['ai-integration-test', '--model', 'tinyllama'])
            self.assertEqual(rc, 2)
            self.assertIn('pass --yes', stderr.getvalue())

        def test_rc3_path_security_excludes_env_and_model(self):
            _, files = self.config()
            (self.root / '.env').write_text('SECRET=do-not-read')
            (self.root / 'weights.gguf').write_bytes(b'fake')
            ok, messages = BuildContextSafety.scan(self.root, files['Dockerfile.dockerignore'])
            self.assertTrue(ok)
            self.assertTrue(any('Sensitive file excluded' in item for item in messages))
            self.assertFalse(any('FAIL:' in item for item in messages))

        def test_rc3_unprotected_secret_is_release_failure(self):
            (self.root / 'private.key').write_text('fake')
            ok, messages = BuildContextSafety.scan(self.root, '')
            self.assertFalse(ok)
            self.assertTrue(any('private.key' in item for item in messages))

        def test_rc3_context_scan_never_follows_symlink(self):
            self._safe_symlink(self.root / 'external', Path(self.tmp.name), target_is_directory=True)
            ok, messages = BuildContextSafety.scan(self.root, '')
            self.assertTrue(ok)
            self.assertTrue(any('symlink' in item for item in messages))

        def test_rc3_smoke_refuses_bad_url_before_network(self):
            _, files = self.config()
            # Replan as an explicitly declared generic web server.
            settings = Settings(target_for(str(self.root / 'localllmchat.py')),
                                mode=Mode.BASE, port=8123, execution_kind='web')
            OutputManager(self.root).write(Renderer().files(Planner().build(settings, Inspector().inspect(settings.target))))
            with self.assertRaisesRegex(OpsError, 'path'):
                AppSmoke.probe(self.root, '//another-host/')

        def test_rc3_smoke_requires_web_contract(self):
            OutputManager(self.root).write(self.config()[1])
            with self.assertRaisesRegex(OpsError, 'web application'):
                AppSmoke.probe(self.root)

        def test_rc3_smoke_returns_pass_only_with_expected_body(self):
            from http.server import BaseHTTPRequestHandler, HTTPServer
            import threading
            class H(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'AUTO_DOCKER_E2E_OK')
                def log_message(self, *args):
                    pass
            server = HTTPServer(('127.0.0.1', 0), H)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            target = target_for(str(self.root / 'localllmchat.py'))
            settings = Settings(target, mode=Mode.BASE, port=8000,
                                execution_kind='web', host_ports=(('app', server.server_port),))
            OutputManager(self.root).write(Renderer().files(Planner().build(settings, Inspector().inspect(target))))
            ok, message = AppSmoke.probe(self.root, '/health', 'AUTO_DOCKER_E2E_OK')
            self.assertTrue(ok, message)
            ok, _ = AppSmoke.probe(self.root, '/', 'DIFFERENT_CONTENT')
            self.assertFalse(ok)

        def test_rc3_true_pty_fixture_without_docker(self):
            if os.name != 'posix':
                self.skipTest('POSIX-only pseudo-terminal fixture')
            command = [sys.executable, '-u', '-c',
                       "print('AUTO_DOCKER_CHAT_OK:' + input('prompt> '))"]
            _proof_tty(command, self.root, timeout=10)

        def test_rc3_cleanup_checks_ps_exit_status(self):
            from subprocess import CompletedProcess
            adapter = DockerAdapter(self.root)
            with patch.object(DockerAdapter, '_compose_command', return_value=['fake-docker']), \
                 patch(f"{__name__}._proof_ok"), \
                 patch(f"{__name__}._proof_command", return_value=CompletedProcess([], 1, '', '')):
                with self.assertRaisesRegex(OpsError, 'Cleanup failed'):
                    _cleanup_proof(adapter, self.root)

        def test_rc3_report_does_not_follow_existing_symlink(self):
            report = self.root / 'proof.json'
            destination = self.root / 'target.json'
            destination.write_text('safe')
            self._safe_symlink(report, destination)
            evidence = ProofEvidence('unit', report)
            evidence.record('x', 'PASS', 'ok')
            with self.assertRaisesRegex(OpsError, 'symlink'):
                evidence.save()
            self.assertEqual(destination.read_text(), 'safe')

        def test_rc3_command_override_preserved_as_exec_form(self):
            target = target_for(str(self.root / 'localllmchat.py'))
            settings = Settings(target, command_override=('python', '-u', 'localllmchat.py', '--message', 'hello world'))
            plan = Planner().build(settings, Inspector().inspect(target))
            files = Renderer().files(plan)
            self.assertIn('"hello world"', files['Dockerfile'])
            self.assertIn('"app_command"', files['manifest.json'])
            self.assertNotIn('sh -c', files['Dockerfile'])

        def test_rc3_command_override_invalid_newline_refused(self):
            target = target_for(str(self.root / 'localllmchat.py'))
            settings = Settings(target, command_override=('python', 'a\nb'))
            with self.assertRaisesRegex(OpsError, 'exec-form'):
                Planner().build(settings, Inspector().inspect(target))

        def test_rc3_web_custom_health_path_uses_real_http_probe(self):
            target = target_for(str(self.root / 'localllmchat.py'))
            settings = Settings(target, port=8801, execution_kind='web', health_path='/ready')
            plan = Planner().build(settings, Inspector().inspect(target))
            files = Renderer().files(plan)
            self.assertIn('urllib.request.urlopen', files['compose.yaml'])
            self.assertIn('/ready', files['compose.yaml'])
            self.assertIn('"health_path": "/ready"', files['manifest.json'])

        def test_rc3_health_path_not_allowed_for_batch(self):
            target = target_for(str(self.root / 'localllmchat.py'))
            settings = Settings(target, health_path='/health')
            with self.assertRaisesRegex(OpsError, 'HTTP health path'):
                Planner().build(settings, Inspector().inspect(target))

        def test_rc3_health_path_never_accepts_external_url(self):
            target = target_for(str(self.root / 'localllmchat.py'))
            settings = Settings(target, port=8443, execution_kind='web', health_path='//external.test')
            with self.assertRaisesRegex(OpsError, 'HTTP health path'):
                Planner().build(settings, Inspector().inspect(target))

        def test_rc3_pin_python_base_digest(self):
            target = target_for(str(self.root / 'localllmchat.py'))
            digest = 'sha256:'+'a'*64
            settings = Settings(target, python_image='python:3.12-slim-bookworm@'+digest)
            plan = Planner().build(settings, Inspector().inspect(target))
            files = Renderer().files(plan)
            self.assertIn('PYTHON_IMAGE=python:3.12-slim-bookworm@'+digest, files['Dockerfile'])
            self.assertIn(digest, files['manifest.json'])
            OutputManager(self.root).write(files)
            _, messages = OfflineDeploymentAudit.evaluate(self.root)
            self.assertFalse(any('Python base image uses a mutable' in m for m in messages))

        def test_rc3_invalid_python_base_digest_is_rejected(self):
            target = target_for(str(self.root / 'localllmchat.py'))
            with self.assertRaisesRegex(OpsError, 'Python image'):
                Planner().build(Settings(target, python_image='python:3.12@sha256:bad'), Inspector().inspect(target))

        def test_rc3_service_images_explicit_digest(self):
            target = target_for(str(self.root / 'localllmchat.py'))
            ref = 'redis:7.4-alpine@sha256:'+'f'*64
            settings = Settings(target, mode=Mode.GENAI, service_images=(('redis', ref),))
            plan = Planner().build(settings, Inspector().inspect(target))
            files = Renderer().files(plan)
            self.assertIn(ref, files['compose.yaml'])
            self.assertEqual(json.loads(files['manifest.json'])['runtime']['service_images']['redis'], ref)

        def test_rc3_unselected_service_cannot_override_image(self):
            target = target_for(str(self.root / 'localllmchat.py'))
            settings = Settings(target, mode=Mode.BASE, service_images=(('redis','redis:7.4'),))
            with self.assertRaisesRegex(OpsError, 'Service image overrides'):
                Planner().build(settings, Inspector().inspect(target))

        def test_rc3_cli_image_options_preview(self):
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                rc = main(['preview', str(self.root / 'localllmchat.py'), '--kind', 'web',
                           '--port', '8081', '--health-path', '/ready', '--cmd',
                           'python -u localllmchat.py', '--python-image',
                           'python:3.12-slim-bookworm'])
            self.assertEqual(rc, 0)
            self.assertIn('python -u localllmchat.py', output.getvalue())

        def test_rc3_console_config_modifications_can_be_undone(self):
            console = Console(Settings(target_for(str(self.root / 'localllmchat.py'))))
            console.execute('cmd python -u localllmchat.py')
            self.assertEqual(console.settings.command_override, ('python','-u','localllmchat.py'))
            console.execute('health /ready')
            self.assertEqual(console.settings.health_path, '/ready')
            console.execute('back')
            self.assertIsNone(console.settings.health_path)
            console.execute('python-image python:3.12-slim-bookworm')
            self.assertEqual(console.settings.python_image,'python:3.12-slim-bookworm')

        def test_rc3_console_service_image_setting(self):
            console = Console(Settings(target_for(str(self.root / 'localllmchat.py')), mode=Mode.GENAI))
            console.execute('image redis redis:7.4-alpine')
            self.assertEqual(console.settings.service_images, (('redis','redis:7.4-alpine'),))
            console.execute('image redis auto')
            self.assertEqual(console.settings.service_images, ())

        def test_rc3_requirements_unpinned_reproducibility_warning(self):
            (self.root / 'requirements.txt').write_text('httpx>=0.23\nflask==3.1.0\n')
            files = self.config()[1]
            OutputManager(self.root).write(files)
            _, messages = OfflineDeploymentAudit.evaluate(self.root)
            self.assertTrue(any('Requirements are not strictly pinned' in m for m in messages))

        def test_rc3_output_contains_app_security_controls(self):
            files = self.config()[1]
            self.assertIn('no-new-privileges:true', files['compose.yaml'])
            self.assertIn('cap_drop:', files['compose.yaml'])

        def test_rc3_evidence_failure_precedes_blocked(self):
            path = self.root / 'report.json'
            evidence = ProofEvidence('unit', path)
            evidence.record('first','FAIL','failed')
            evidence.record('second','BLOCKED','blocked')
            evidence.save()
            self.assertEqual(json.loads(path.read_text())['overall'], 'FAIL')

        def test_rc3_model_service_pull_never_occurs_without_yes(self):
            with patch(f"{__name__}._proof_engine", side_effect=AssertionError('must not contact Docker')):
                self.assertEqual(main(['ai-integration-test', '--model', 'tinyllama']), 2)

        def test_rc3_nested_ai_python_scripts_compile(self):
            # Python source is generated dynamically for docker compose run -c.
            # Compiling the outer module does NOT validate the nested source.
            import ast as _ast
            tree = _ast.parse(Path(__file__).read_text(encoding='utf-8'))
            function = next(node for node in tree.body
                            if isinstance(node, _ast.FunctionDef) and node.name == 'ai_integration_test')
            names = set()
            for node in _ast.walk(function):
                if isinstance(node, _ast.Assign):
                    for target in node.targets:
                        if isinstance(target, _ast.Name) and target.id in {'script','app_script','model_script'}:
                            source = eval(compile(_ast.Expression(node.value), '<nested-proof>', 'eval'),
                                          {'unique': 'SAFEID', 'model': 'tinyllama'})
                            compile(source, '<application-container>', 'exec')
                            names.add(target.id)
            self.assertEqual(names, {'script','app_script','model_script'})


        # RC4: large repository with vendor-managed Compose/Helm, no guessed entrypoint.
        def make_existing_repo(self):
            root = self.root / 'auto-ontology'
            root.mkdir()
            (root / 'Dockerfile').write_text('FROM python:3.12-slim\n')
            (root / 'docker-compose.yml').write_text('''# vendor-managed
services:
  postgres:
    image: postgres:17
  auto-ontology:
    build: .
  auto-ontology-migrate:
    command: ["migrate"]
  ingestion-service:
    build: .
  frontend-migrate:
    command: ["migrate"]
  auto-ontology-frontend:
    build: ./frontend
  pgadmin:
    image: dpage/pgadmin4
volumes:
  postgres_data:
''')
            (root / '.env.example').write_text('API_KEY=replace_me\n')
            (root / 'uv.lock').write_text('version = 1\n')
            (root / 'pyproject.toml').write_text('[project]\nname = "fixture"\n')
            (root / 'helm').mkdir()
            (root / 'frontend').mkdir()
            (root / 'frontend' / 'Dockerfile').write_text('FROM node:22-alpine\n')
            vendor = root / 'vendor'
            vendor.mkdir()
            for j in range(260):
                (vendor / ('module_'+str(j)+'.py')).write_text('print("fixture")\n')
            return root

        def test_rc4_large_vendor_repo_inventory_bypasses_python_cap(self):
            root = self.make_existing_repo()
            stack = ExistingInfrastructure.discover(root)
            self.assertEqual(len(stack.services), 7)
            self.assertTrue(stack.has_helm and stack.has_uv_lock)
            console = Console(Settings(target_for(str(root))))
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                console.execute('inspect')
            self.assertIn('auto-ontology-migrate', output.getvalue())
            self.assertNotIn('ERROR', output.getvalue())
            self.assertFalse((root / OUTPUT).exists())

        def test_rc4_vendor_stack_not_replaced(self):
            root = self.make_existing_repo()
            console = Console(Settings(target_for(str(root))))
            with self.assertRaisesRegex(OpsError, 'already owns'):
                console.execute('generate YES')
            self.assertEqual((root / 'Dockerfile').read_text(), 'FROM python:3.12-slim\n')
            self.assertFalse((root / OUTPUT).exists())

        def test_rc4_missing_docker_native_check_is_not_a_pass(self):
            root = self.make_existing_repo()
            with patch('shutil.which', return_value=None), patch('sys.stdout', new_callable=io.StringIO):
                with self.assertRaisesRegex(OpsError, 'BLOCKED.*NOT verified'):
                    Console(Settings(target_for(str(root)))).execute('check')
            self.assertFalse((root / OUTPUT).exists())

        def test_rc4_native_approval_required_and_missing_env_protected(self):
            root = self.make_existing_repo()
            with self.assertRaisesRegex(OpsError, 'Approval required'):
                ExistingInfrastructure.apply(root, 'build')
            with self.assertRaisesRegex(OpsError, 'no .env'):
                ExistingInfrastructure.apply(root, 'build', approved=True)
            self.assertFalse((root / OUTPUT).exists())

        def test_rc4_native_compose_check_no_shell_no_replacement(self):
            root = self.make_existing_repo()
            from subprocess import CompletedProcess
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'version OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([],0)) as run, \
                 patch('sys.stdout', new_callable=io.StringIO):
                ExistingInfrastructure.apply(root, 'check')
            cmd=run.call_args.args[0]
            self.assertEqual(cmd, ['/usr/bin/docker','compose','-f','docker-compose.yml','config','--quiet'])
            self.assertEqual(run.call_args.kwargs['cwd'], root)

        def test_rc4_native_build_context_and_explicit_yes(self):
            root = self.make_existing_repo()
            (root / '.env').write_text('SECRET=private\n')
            from subprocess import CompletedProcess
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([],0)) as run, \
                 patch('sys.stdout', new_callable=io.StringIO):
                Console(Settings(target_for(str(root)), docker_context='my-remote')).execute('native build YES')
            cmd=run.call_args.args[0]
            self.assertEqual(cmd[:5], ['/usr/bin/docker','--context','my-remote','compose','-f'])
            self.assertEqual(cmd[-1], 'build')
            self.assertFalse((root / OUTPUT).exists())

        def test_rc4_native_multiple_files_requires_selection(self):
            root = self.make_existing_repo()
            (root / 'compose.yaml').write_text('services:\n  second:\n    image: busybox\n')
            stack=ExistingInfrastructure.discover(root)
            self.assertIsNone(stack.selected_compose)
            with self.assertRaisesRegex(OpsError, 'Multiple Compose'):
                ExistingInfrastructure.apply(root,'check')
            self.assertEqual(ExistingInfrastructure.discover(root,'docker-compose.yml').services[0],'postgres')

        def test_rc4_native_rejects_symlink_compose_and_unknown_choice(self):
            root = self.make_existing_repo()
            with self.assertRaises(OpsError):
                ExistingInfrastructure.discover(root, '../wrong.yml')
            self._safe_symlink(root / 'compose.yaml', root / 'docker-compose.yml')
            self.assertNotIn('compose.yaml', ExistingInfrastructure.discover(root).compose_files)
            with self.assertRaises(OpsError):
                ExistingInfrastructure.discover(root,'compose.yaml')

        def test_rc4_cli_existing_works_without_docker(self):
            root = self.make_existing_repo()
            with patch('subprocess.run', side_effect=AssertionError('existing must not call Docker')), \
                 patch('sys.stdout', new_callable=io.StringIO) as output:
                code = main(['existing',str(root)])
            self.assertEqual(code,0)
            self.assertIn('Existing deployment inventory', output.getvalue())

        def test_rc4_cli_existing_generation_is_refused_without_writes(self):
            root=self.make_existing_repo()
            with patch('sys.stderr', new_callable=io.StringIO) as output:
                code=main(['generate',str(root),'--yes'])
            self.assertEqual(code,2)
            self.assertIn('Existing Compose stack',output.getvalue())
            self.assertFalse((root / OUTPUT).exists())


        def test_rc5_parent_compose_root_suggestion(self):
            repo = self.root / 'complex-repo'
            (repo / 'dev_tools').mkdir(parents=True)
            (repo / 'docker-compose.yml').write_text('services:\n  api:\n    build: .\n')
            self.assertEqual(ProjectRootDiscovery.suggest(repo / 'dev_tools'), repo)
            console = Console(Settings(target_for(str(repo / 'dev_tools'))))
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                console.execute('root')
            self.assertIn(str(repo), output.getvalue())
            self.assertEqual(console.settings.target.root, repo / 'dev_tools')
            console.execute('root use')
            self.assertEqual(console.settings.target.root, repo)
            self.assertFalse((repo / OUTPUT).exists())

        def test_rc5_root_no_ancestor_project(self):
            self.assertIsNone(ProjectRootDiscovery.suggest(self.root))

        def test_rc5_readiness_missing_env_blocks(self):
            repo = self.root / 'repo'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  api:\n    image: busybox\n')
            (repo / '.env.example').write_text('API_KEY=replace_me\n')
            result = NativeReadiness.inspect(repo, disk_check=False)
            self.assertFalse(result.ready)
            self.assertTrue(any('Missing .env' in item for item in result.failures))
            self.assertFalse((repo / '.env').exists())

        def test_rc5_no_secret_value_printed(self):
            repo = self.root / 'repo'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  api:\n    image: busybox\n')
            (repo / '.env.example').write_text('API_KEY=example-secret-do-not-print\n')
            (repo / '.env').write_text('API_KEY=example-secret-do-not-print\n')
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                result = NativeReadiness.report(repo, disk_check=False)
            self.assertFalse(result.ready)
            self.assertIn('API_KEY', output.getvalue())
            self.assertNotIn('example-secret-do-not-print', output.getvalue())

        def test_rc5_secure_credential_can_pass(self):
            repo = self.root / 'repo'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  api:\n    image: busybox\n')
            (repo / '.env.example').write_text('API_KEY=replace_me\n')
            (repo / '.env').write_text('API_KEY=unique-private-value-of-real-configuration\n')
            self.assertTrue(NativeReadiness.inspect(repo, disk_check=False).ready)

        def test_rc5_env_symlink_refused(self):
            repo = self.root / 'repo'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  api:\n    image: busybox\n')
            secrets = self.root / 'elsewhere.env'
            secrets.write_text('API_KEY=private\n')
            self._safe_symlink(repo / '.env', secrets)
            result = NativeReadiness.inspect(repo, disk_check=False)
            self.assertFalse(result.ready)
            self.assertTrue(any('symlink' in item for item in result.failures))

        def test_rc5_required_compose_variable(self):
            repo = self.root / 'repo'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  api:\n    image: "${RC5_REQUIRED_TEST_VALUE:?set it}"\n')
            with patch.dict(os.environ, {'RC5_REQUIRED_TEST_VALUE': ''}):
                result = NativeReadiness.inspect(repo, disk_check=False)
                # Presence of a variable is not equivalent to a usable required value.
                self.assertTrue(any('RC5_REQUIRED_TEST_VALUE' in issue for issue in result.failures))

        def test_rc5_readiness_requires_explicit_compose_selection(self):
            repo = self.root / 'repo'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  api:\n    image: busybox\n')
            (repo / 'docker-compose.yml').write_text('services:\n  db:\n    image: postgres\n')
            self.assertFalse(NativeReadiness.inspect(repo, disk_check=False).ready)
            self.assertTrue(NativeReadiness.inspect(repo, 'compose.yaml', disk_check=False).ready)

        def test_rc5_cli_readiness_no_writes(self):
            repo = self.root / 'repo'
            repo.mkdir()
            (repo / 'docker-compose.yml').write_text('services:\n  api:\n    image: busybox\n')
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                status = main(['readiness', str(repo)])
            self.assertEqual(status, 0)
            self.assertIn('Static prerequisites checked', output.getvalue())
            self.assertFalse((repo / OUTPUT).exists())

        def test_rc5_build_blocks_example_credentials_before_docker(self):
            repo = self.root / 'repo'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  api:\n    image: busybox\n')
            (repo / '.env.example').write_text('API_KEY=replace_me\n')
            (repo / '.env').write_text('API_KEY=replace_me\n')
            with patch('subprocess.run', side_effect=AssertionError('must not start Docker')):
                with self.assertRaisesRegex(OpsError, 'BLOCKED'):
                    ExistingInfrastructure.apply(repo, 'build', approved=True)
            self.assertFalse((repo / OUTPUT).exists())

        def test_rc5_native_ready_command_without_docker(self):
            repo = self.root / 'repo'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  api:\n    image: busybox\n')
            console = Console(Settings(target_for(str(repo))))
            with patch('subprocess.run', side_effect=AssertionError('must not call Docker')), \
                 patch('sys.stdout', new_callable=io.StringIO) as output:
                console.execute('native ready')
            self.assertIn('Static prerequisites checked', output.getvalue())

        # ---------- RC6 Acceptance Tests ----------

        def test_rc6_nvidia_auto_ontology_fixture(self):
            repo = self.root / 'nvidia-auto-ontology'
            repo.mkdir()
            compose_content = """
services:
  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    ports:
      - '5432:5432'
    volumes:
      - pgdata:/var/lib/postgresql/data
    environment:
      POSTGRES_PASSWORD: ${DB_PASS:?required}
  migration:
    build:
      context: ./backend
      dockerfile: Dockerfile.migration
    restart: 'no'
    depends_on:
      postgres:
        condition: service_healthy
    env_file:
      - .env
  backend:
    build: ./backend
    ports:
      - '127.0.0.1:8000:8000'
    depends_on:
      - migration
      - postgres
    env_file: .env
  frontend:
    build: ./frontend
    ports:
      - '3000:3000'
    depends_on:
      - backend
  ingestion:
    build: ./backend
    depends_on:
      - postgres
    env_file: .env
  pgadmin:
    image: dpage/pgadmin4
    ports:
      - '127.0.0.1:5050:80'
    depends_on:
      - postgres
volumes:
  pgdata:
"""
            (repo / 'docker-compose.yml').write_text(compose_content.strip())
            (repo / '.env.example').write_text('DB_PASS=replace_me\nAPI_KEY=replace_me\n')
            (repo / 'pyproject.toml').write_text('[project]\nname="auto-ontology"\n')
            (repo / 'uv.lock').write_text('# uv lock\n')
            (repo / 'helm').mkdir()
            (repo / 'helm' / 'Chart.yaml').write_text('name: auto-ontology\n')
            (repo / 'backend').mkdir()
            (repo / 'frontend').mkdir()
            (repo / 'dev_tools').mkdir()

            # 320 python files across repository
            for i in range(200):
                (repo / 'backend' / f'mod_{i}.py').write_text('x=1\n')
            for i in range(120):
                (repo / 'dev_tools' / f'tool_{i}.py').write_text('y=2\n')

            # 1. Project discovery & root suggestion
            self.assertEqual(ProjectRootDiscovery.suggest(repo / 'dev_tools'), repo)

            # 2. Existing infrastructure discovery
            stack = ExistingInfrastructure.discover(repo)
            self.assertEqual(len(stack.discovered_services), 6)
            role_map = {s.name: s.role for s in stack.discovered_services}
            self.assertEqual(role_map['postgres'], 'database')
            self.assertEqual(role_map['migration'], 'migration')
            self.assertEqual(role_map['backend'], 'backend')
            self.assertEqual(role_map['frontend'], 'frontend')
            self.assertEqual(role_map['ingestion'], 'worker')
            self.assertEqual(role_map['pgadmin'], 'admin')

            # 3. Environment readiness checks
            res_missing = NativeReadiness.inspect(repo, disk_check=False)
            self.assertFalse(res_missing.ready)
            self.assertTrue(any('Missing .env' in f for f in res_missing.failures))

            (repo / '.env').write_text('DB_PASS=replace_me\nAPI_KEY=secret123\n')
            res_placeholder = NativeReadiness.inspect(repo, disk_check=False)
            self.assertFalse(res_placeholder.ready)
            self.assertTrue(any('placeholder credential detected: DB_PASS' in f for f in res_placeholder.failures))

            (repo / '.env').write_text('DB_PASS=real-prod-db-pass-123\nAPI_KEY=real-prod-api-key-456\n')
            res_valid = NativeReadiness.inspect(repo, disk_check=False)
            self.assertTrue(res_valid.ready)

            # 4. Deployment plan & topological order
            plan = ExistingInfrastructure.plan(repo)
            self.assertTrue(plan.ready)
            top = plan.topological_order
            self.assertLess(top.index('postgres'), top.index('migration'))
            self.assertLess(top.index('migration'), top.index('backend'))
            self.assertLess(top.index('backend'), top.index('frontend'))

            # Verify startup phases
            phase_names = [p[1] for p in plan.startup_phases]
            self.assertIn("Storage / Databases", phase_names)
            self.assertIn("Migrations / Initialization", phase_names)
            self.assertIn("Core Application Services", phase_names)
            self.assertIn("Frontend & Administration", phase_names)

            # JSON export contains no secrets
            plan_json = json.dumps(plan.to_dict())
            self.assertNotIn('real-prod-db-pass-123', plan_json)
            self.assertNotIn('real-prod-api-key-456', plan_json)

            # Existing files preserved
            self.assertTrue((repo / 'docker-compose.yml').exists())
            self.assertFalse((repo / OUTPUT).exists())

        def test_rc6_large_repository_discovery_over_1000_files(self):
            repo = self.root / 'large-repo'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  worker:\n    image: busybox\n')
            src = repo / 'src'
            src.mkdir()
            for i in range(1050):
                (src / f'mod_{i}.py').write_text('v=1\n')
            (repo / 'app.py').write_text('if __name__ == "__main__": pass\n')
            stack = ExistingInfrastructure.discover(repo)
            self.assertEqual(len(stack.discovered_services), 1)
            self.assertEqual(stack.discovered_services[0].name, 'worker')
            # Inspector should not crash with ScanLimit when Compose exists
            insp = Inspector().inspect(target_for(str(repo)))
            self.assertIsNotNone(insp)
            self.assertEqual(insp.entry.path, 'app.py')

        def test_rc6_nested_selection_root_suggestion(self):
            repo = self.root / 'nested-proj'
            (repo / 'backend' / 'src' / 'utils').mkdir(parents=True)
            (repo / 'compose.yaml').write_text('services:\n  api:\n    image: busybox\n')
            start = repo / 'backend' / 'src' / 'utils'
            suggested = ProjectRootDiscovery.suggest(start)
            self.assertEqual(suggested, repo)

            console = Console(Settings(target_for(str(start))))
            self.assertEqual(console.settings.target.root, start)
            with patch('sys.stdout', new_callable=io.StringIO) as out:
                console.execute('root')
            self.assertIn(str(repo), out.getvalue())
            self.assertEqual(console.settings.target.root, start)
            console.execute('root use')
            self.assertEqual(console.settings.target.root, repo)

        def test_rc6_monorepo_frontend_backend(self):
            repo = self.root / 'monorepo'
            repo.mkdir()
            (repo / 'frontend').mkdir()
            (repo / 'backend').mkdir()
            (repo / 'frontend' / 'package.json').write_text('{"name": "ui"}')
            (repo / 'backend' / 'pyproject.toml').write_text('[project]\nname="api"\n')
            (repo / 'compose.yaml').write_text('services:\n  fe:\n    build: ./frontend\n  be:\n    build: ./backend\n')
            stack = ExistingInfrastructure.discover(repo)
            self.assertTrue(stack.has_frontend)
            self.assertTrue(stack.has_pyproject)
            roles = {s.name: s.role for s in stack.discovered_services}
            self.assertEqual(roles['fe'], 'frontend')
            self.assertEqual(roles['be'], 'backend')

        def test_rc6_multi_file_compose_ordering(self):
            repo = self.root / 'multi-compose'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n    ports:\n      - "8080:80"\n')
            (repo / 'compose.override.yaml').write_text('services:\n  app:\n    environment:\n      - DEBUG=1\n')
            stack = ExistingInfrastructure.discover(repo)
            self.assertEqual(len(stack.selected_compose_files), 2)
            self.assertEqual(stack.selected_compose_files[0], 'compose.yaml')
            self.assertEqual(stack.selected_compose_files[1], 'compose.override.yaml')
            self.assertEqual(len(stack.discovered_services), 1)
            self.assertIn('8080:80', stack.discovered_services[0].ports)

        def test_rc6_symlink_loop_safety(self):
            repo = self.root / 'symlink-repo'
            (repo / 'sub').mkdir(parents=True)
            (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n')
            # Create a loop if OS supports symlinks
            try:
                (repo / 'sub' / 'loop').symlink_to(repo, target_is_directory=True)
            except (OSError, NotImplementedError):
                pass
            suggested = ProjectRootDiscovery.suggest(repo / 'sub')
            self.assertEqual(suggested, repo)

        def test_rc6_secrets_validation_classified_findings(self):
            repo = self.root / 'classified-repo'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n')
            (repo / '.env.example').write_text('SECRET_TOKEN=replace_me\nOPTIONAL_VAR=demo\n')
            (repo / '.env').write_text('SECRET_TOKEN=super-private-token-do-not-print\n')
            res = NativeReadiness.inspect(repo, disk_check=False)
            self.assertTrue(res.ready)
            levels = {f.level for f in res.findings}
            self.assertIn('INFO', levels)
            self.assertIn('WARNING', levels)
            for f in res.findings:
                self.assertNotIn('super-private-token-do-not-print', f.message)

        def test_rc6_deployment_plan_json_determinism(self):
            repo = self.root / 'plan-json-repo'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  api:\n    image: busybox\n')
            (repo / '.env').write_text('KEY=val\n')
            plan1 = ExistingInfrastructure.plan(repo)
            dict1 = plan1.to_dict()
            json1 = json.dumps(dict1, sort_keys=True)
            plan2 = ExistingInfrastructure.plan(repo)
            dict2 = plan2.to_dict()
            json2 = json.dumps(dict2, sort_keys=True)
            self.assertEqual(json1, json2)
            self.assertEqual(dict1['infrastructure_mode'], 'existing')
            self.assertEqual(dict1['services'][0]['name'], 'api')

        def test_rc6_remote_docker_context_resource_unknown(self):
            repo = self.root / 'remote-ctx-repo'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  api:\n    image: busybox\n')
            (repo / '.env').write_text('K=V\n')
            res = NativeReadiness.inspect(repo, disk_check=False, docker_context="remote-ssh")
            self.assertTrue(any(f.level == 'UNKNOWN' for f in res.findings))

        def test_rc6_security_risks_public_ports_and_privileged(self):
            repo = self.root / 'security-repo'
            repo.mkdir()
            compose_text = """
services:
  db:
    image: postgres:16
    ports:
      - "5432:5432"
  agent:
    image: busybox
    privileged: true
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
"""
            (repo / 'compose.yaml').write_text(compose_text.strip())
            (repo / '.env').write_text('X=1\n')
            plan = ExistingInfrastructure.plan(repo)
            self.assertEqual(plan.risk_classification, 'CRITICAL')
            warning_text = ' '.join(plan.warnings)
            self.assertIn('Privileged container detected', warning_text)
            self.assertIn('Docker socket mounted', warning_text)
            self.assertIn('publishes port 5432 on all network interfaces', warning_text)

        def test_rc6_deployment_state_machine(self):
            orchestrator = DeploymentOrchestrator(self.root)
            self.assertEqual(orchestrator.state, DeploymentState.DISCOVERED)
            orchestrator.transition(DeploymentState.ANALYZED, "Project analyzed")
            orchestrator.transition(DeploymentState.PLANNED, "Plan generated")
            orchestrator.transition(DeploymentState.VALIDATED, "Prerequisites validated")
            orchestrator.transition(DeploymentState.APPROVED, "User approved")
            orchestrator.transition(DeploymentState.BUILDING, "Building images")
            orchestrator.transition(DeploymentState.STARTING, "Starting containers")
            orchestrator.transition(DeploymentState.VERIFYING, "Checking health")
            orchestrator.transition(DeploymentState.HEALTHY, "All checks passed")
            self.assertEqual(orchestrator.state, DeploymentState.HEALTHY)
            # Illegal transition: cannot jump from HEALTHY directly to BUILDING
            with self.assertRaises(OpsError):
                orchestrator.transition(DeploymentState.BUILDING, "Illegal jump")

        def test_rc6_native_plan_cli_and_console(self):
            repo = self.root / 'cli-plan-repo'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  web:\n    image: nginx\n')
            (repo / '.env').write_text('ENV=prod\n')
            with patch('sys.stdout', new_callable=io.StringIO) as out:
                code = main(['native', str(repo), '--native-action', 'plan', '--json'])
            self.assertEqual(code, 0)
            parsed = json.loads(out.getvalue())
            self.assertEqual(parsed['services'][0]['name'], 'web')

        def test_rc6_multi_file_compose_required_variables(self):
            repo = self.root / 'multi-compose-vars'
            repo.mkdir()
            (repo / 'compose.yaml').write_text(
                'services:\n  api:\n    image: "${BASE_IMAGE:?base image required}"\n'
            )
            (repo / 'compose.override.yaml').write_text(
                'services:\n  api:\n    ports:\n      - "${HOST_PORT:?host port required}:8080"\n'
            )
            (repo / '.env.example').write_text('BASE_IMAGE=python:3.12\nHOST_PORT=8000\n')
            
            # Case 1: Missing .env and neither variable set -> both flagged
            res = NativeReadiness.inspect(repo)
            err_text = ' '.join(res.failures)
            self.assertIn('Missing required Compose variable: BASE_IMAGE', err_text)
            self.assertIn('Missing required Compose variable: HOST_PORT', err_text)
            
            # Case 2: Only one variable set in .env
            (repo / '.env').write_text('BASE_IMAGE=python:3.12-slim\n')
            res2 = NativeReadiness.inspect(repo)
            err_text2 = ' '.join(res2.failures)
            self.assertNotIn('BASE_IMAGE', err_text2)
            self.assertIn('Missing required Compose variable: HOST_PORT', err_text2)
            
            # Case 3: Both set in .env
            (repo / '.env').write_text('BASE_IMAGE=python:3.12-slim\nHOST_PORT=8000\n')
            res3 = NativeReadiness.inspect(repo)
            self.assertTrue(res3.ready)

        def test_rc6_compose_interpolation_precedence(self):
            repo = self.root / 'interp-precedence'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('''
services:
  app:
    image: "${APP_IMAGE:?err}"
    ports:
      - "${PORT:-8080}:80"
      - "${OPTIONAL_PORT}:8081"
''')
            # 1. APP_IMAGE is empty in .env, but set in os.environ (shell takes precedence)
            (repo / '.env').write_text('APP_IMAGE=\n')
            with patch.dict(os.environ, {'APP_IMAGE': 'my-custom-image:v1'}):
                res = NativeReadiness.inspect(repo)
                err_text = ' '.join(res.failures)
                self.assertNotIn('APP_IMAGE', err_text)
                self.assertNotIn('PORT', err_text)  # has default 8080
                # OPTIONAL_PORT is not set and has no default -> noted in findings, not failure
                self.assertTrue(any(f.level == 'INFO' and f.key == 'OPTIONAL_PORT' for f in res.findings))

        def test_rc6_security_gate_blocks_critical_without_override(self):
            repo = self.root / 'security-gate-test'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('''
services:
  db:
    image: postgres:16
    ports:
      - "5432:5432"
  daemon:
    image: docker:dind
    privileged: true
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
''')
            (repo / '.env').write_text('KEY=1\n')
            plan = ExistingInfrastructure.plan(repo)
            self.assertEqual(plan.risk_classification, 'CRITICAL')
            
            # Mutating action without allow_insecure must raise OpsError and set state to BLOCKED
            orch = DeploymentOrchestrator(repo)
            with self.assertRaisesRegex(OpsError, 'BLOCKED: Critical container security risks detected'):
                ExistingInfrastructure.apply(repo, 'build', approved=True, allow_insecure=False, orchestrator=orch)
            self.assertEqual(orch.state, DeploymentState.BLOCKED)

        def test_rc6_orchestrator_integrated_in_execution_flow(self):
            repo = self.root / 'orch-flow-test'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n')
            (repo / '.env').write_text('KEY=val\n')
            from subprocess import CompletedProcess
            orch = DeploymentOrchestrator(repo)
            
            # Action: 'ready' transitions through ANALYZED -> PLANNED -> VALIDATED
            ExistingInfrastructure.apply(repo, 'ready', orchestrator=orch)
            self.assertEqual(orch.state, DeploymentState.VALIDATED)

            # Mutating action 'up YES' with mocked docker
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 0)) as run, \
                 patch('sys.stdout', new_callable=io.StringIO):
                ExistingInfrastructure.apply(repo, 'up', approved=True, orchestrator=orch)
            
            # Must be in VERIFYING state after up (NEVER immediately HEALTHY!)
            self.assertEqual(orch.state, DeploymentState.VERIFYING)
            transitions = [t.to_state for t in orch.history]
            self.assertIn(DeploymentState.APPROVED, transitions)
            self.assertIn(DeploymentState.STARTING, transitions)
            self.assertIn(DeploymentState.VERIFYING, transitions)

        def test_rc6_workbench_menu_and_navigation(self):
            repo = self.root / 'workbench-test'
            repo.mkdir()
            (repo / 'app.py').write_text('if __name__ == "__main__": print("hello")\n')
            (repo / 'helper.py').write_text('def run(): pass\n')
            sub = repo / 'subpkg'
            sub.mkdir()
            
            console = Console(Settings(target_for(str(repo))))
            
            # Test menu display
            with patch('sys.stdout', new_callable=io.StringIO) as out:
                console.execute('menu')
            self.assertIn('AUTO DOCKER', out.getvalue())
            self.assertIn('[1] Open Project', out.getvalue())
            self.assertIn('[11] System & Environment Doctor', out.getvalue())
            
            # Option 2: Browse directory
            with patch('sys.stdout', new_callable=io.StringIO) as out:
                console.execute('2')
            self.assertIn('[DIR]  subpkg', out.getvalue())
            self.assertIn('[FILE] app.py', out.getvalue())
            
            # Option 3: Recent projects
            with patch('sys.stdout', new_callable=io.StringIO) as out:
                console.execute('3')
            self.assertIn('Recent Projects:', out.getvalue())
            
            # Option 6: Settings customization
            with patch('sys.stdout', new_callable=io.StringIO) as out:
                console.execute('6 mode mlops')
            self.assertEqual(console.settings.mode, Mode.MLOPS)

            # Option 6: Insecure toggle
            console.execute('6 insecure on')
            self.assertTrue(console.allow_insecure)
            console.execute('6 insecure off')
            self.assertFalse(console.allow_insecure)

            # Option 0: Exit
            self.assertFalse(console.execute('0'))

        def test_rc6_scenario_1_fastapi_beginner(self):
            repo = self.root / 'scenario1-fastapi'
            repo.mkdir()
            (repo / 'main.py').write_text('''
from fastapi import FastAPI
app = FastAPI()
@app.get("/")
def read_root():
    return {"message": "Hello World"}
''')
            (repo / 'requirements.txt').write_text('fastapi==0.115.0\nuvicorn==0.32.0\n')
            console = Console(Settings(target_for(str(repo))))
            plan = console._plan()
            self.assertEqual(plan.port, 8000)
            self.assertIn('uvicorn', ' '.join(plan.command))
            files = console.renderer.files(plan)
            self.assertIn('Dockerfile', files)
            self.assertIn('compose.yaml', files)

        def test_rc6_scenario_2_existing_multi_file_compose_with_env(self):
            repo = self.root / 'scenario2-multifile'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('''
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: testdb
  api:
    image: "${API_IMAGE:?api image required}"
    depends_on:
      - db
''')
            (repo / 'compose.override.yaml').write_text('''
services:
  api:
    ports:
      - "${API_PORT:-8000}:8000"
''')
            (repo / '.env').write_text('API_IMAGE=myapi:v1.0\n')
            plan = ExistingInfrastructure.plan(repo)
            self.assertEqual(plan.infrastructure_mode, 'existing')
            self.assertEqual(len(plan.services), 2)
            self.assertEqual(plan.services[0].name, 'db')
            self.assertEqual(plan.services[1].name, 'api')
            self.assertTrue(NativeReadiness.report(repo).ready)

        def test_rc6_scenario_3_security_gate_critical_override(self):
            repo = self.root / 'scenario3-sec-override'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('''
services:
  db:
    image: postgres:16
    ports:
      - "5432:5432"
''')
            (repo / '.env').write_text('POSTGRES_PASSWORD=strongpassword123\n')
            from subprocess import CompletedProcess
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 0)) as run, \
                 patch('sys.stdout', new_callable=io.StringIO):
                # With allow_insecure=True, it executes through the gate
                ExistingInfrastructure.apply(repo, 'build', approved=True, allow_insecure=True)
            self.assertEqual(run.call_args.args[0][-1], 'build')

        def test_rc6_scenario_4_mlops_llmops_workload(self):
            repo = self.root / 'scenario4-llmops'
            repo.mkdir()
            (repo / 'train.py').write_text('''
import torch
from transformers import AutoModelForCausalLM
def train():
    pass
if __name__ == '__main__':
    train()
''')
            (repo / 'requirements.txt').write_text('torch==2.5.0\ntransformers==4.46.0\n')
            console = Console(Settings(target_for(str(repo)), mode=Mode.LLMOPS, gpu=True))
            plan = console._plan()
            self.assertEqual(plan.mode, Mode.LLMOPS)
            self.assertTrue(plan.gpu)
            files = console.renderer.files(plan)
            compose_text = files['compose.yaml']
            self.assertIn('ollama', compose_text)
            self.assertIn('capabilities:', compose_text)
            self.assertIn('"gpu"', compose_text)

        def test_rc6_scenario_5_offline_docker_graceful(self):
            repo = self.root / 'scenario5-offline'
            repo.mkdir()
            (repo / 'app.py').write_text('print("hello")\n')
            console = Console(Settings(target_for(str(repo))))
            with patch('shutil.which', return_value=None), \
                 patch('sys.stdout', new_callable=io.StringIO) as out:
                # Option 10 without docker must print diagnostic banner and not crash
                res = console.execute('10')
            self.assertTrue(res)
            self.assertIn('DOCKER ENGINE NOT DETECTED ON HOST PATH', out.getvalue())

        def test_rc6_lifecycle_build_then_up(self):
            repo = self.root / 'lc-build-then-up'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n')
            orch = DeploymentOrchestrator(repo)
            from subprocess import CompletedProcess
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 0)):
                ExistingInfrastructure.apply(repo, 'build', approved=True, orchestrator=orch)
                self.assertEqual(orch.state, DeploymentState.VALIDATED)
                ExistingInfrastructure.apply(repo, 'up', approved=True, orchestrator=orch)
                self.assertEqual(orch.state, DeploymentState.VERIFYING)

        def test_rc6_lifecycle_up_then_down(self):
            repo = self.root / 'lc-up-then-down'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n')
            orch = DeploymentOrchestrator(repo)
            from subprocess import CompletedProcess
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 0)):
                ExistingInfrastructure.apply(repo, 'up', approved=True, orchestrator=orch)
                self.assertEqual(orch.state, DeploymentState.VERIFYING)
                ExistingInfrastructure.apply(repo, 'down', approved=True, orchestrator=orch)
                self.assertEqual(orch.state, DeploymentState.ANALYZED)

        def test_rc6_lifecycle_ready_twice(self):
            repo = self.root / 'lc-ready-twice'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n')
            orch = DeploymentOrchestrator(repo)
            ExistingInfrastructure.apply(repo, 'ready', orchestrator=orch)
            self.assertEqual(orch.state, DeploymentState.VALIDATED)
            ExistingInfrastructure.apply(repo, 'ready', orchestrator=orch)
            self.assertEqual(orch.state, DeploymentState.VALIDATED)

        def test_rc6_lifecycle_failed_check(self):
            repo = self.root / 'lc-failed-check'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n')
            orch = DeploymentOrchestrator(repo)
            from subprocess import CompletedProcess
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 1, stdout='', stderr='yaml syntax error')):
                with self.assertRaises(OpsError) as ctx:
                    ExistingInfrastructure.apply(repo, 'check', orchestrator=orch)
                self.assertIn('Compose configuration check failed', str(ctx.exception))
                self.assertIn('yaml syntax error', str(ctx.exception))
                self.assertEqual(orch.state, DeploymentState.BLOCKED)

        def test_rc6_lifecycle_verify_health(self):
            repo = self.root / 'lc-verify-health'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n')
            orch = DeploymentOrchestrator(repo)
            from subprocess import CompletedProcess
            # Case A: healthy
            healthy_json = json.dumps([{"Service": "app", "Name": "app-1", "State": "running", "Health": "healthy", "ExitCode": 0}])
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', side_effect=[CompletedProcess([], 0), CompletedProcess([], 0, stdout=healthy_json, stderr='')]):
                ExistingInfrastructure.apply(repo, 'up', approved=True, orchestrator=orch)
                ExistingInfrastructure.apply(repo, 'verify', orchestrator=orch)
                self.assertEqual(orch.state, DeploymentState.HEALTHY)

            # Case B: unhealthy
            unhealthy_json = json.dumps([{"Service": "app", "Name": "app-1", "State": "running", "Health": "unhealthy", "ExitCode": 0}])
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 0, stdout=unhealthy_json, stderr='')):
                ExistingInfrastructure.apply(repo, 'verify', orchestrator=orch)
                self.assertEqual(orch.state, DeploymentState.DEGRADED)

            # Case C: no healthcheck (unknown health) -> stays in VERIFYING
            no_hc_json = json.dumps([{"Service": "app", "Name": "app-1", "State": "running", "Health": "", "ExitCode": 0}])
            orch.state = DeploymentState.VERIFYING
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 0, stdout=no_hc_json, stderr='')):
                ExistingInfrastructure.apply(repo, 'verify', orchestrator=orch)
                self.assertEqual(orch.state, DeploymentState.VERIFYING)

        def test_rc6_lifecycle_retry_after_blocked(self):
            repo = self.root / 'lc-retry-blocked'
            repo.mkdir()
            (repo / 'compose.yaml').write_text("services:\n  app:\n    image: '${REQ:?missing}'\n")
            orch = DeploymentOrchestrator(repo)
            with self.assertRaises(OpsError):
                ExistingInfrastructure.apply(repo, 'ready', orchestrator=orch)
            self.assertEqual(orch.state, DeploymentState.BLOCKED)

            # Fix configuration and retry
            (repo / '.env').write_text('REQ=busybox\n')
            ExistingInfrastructure.apply(repo, 'ready', orchestrator=orch)
            self.assertEqual(orch.state, DeploymentState.VALIDATED)

        def test_rc6_option_10_safe_confirmation_no_auto_forward(self):
            repo = self.root / 'opt10-safe-test'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n')
            (repo / '.env').write_text('KEY=val\n')
            console = Console(Settings(target_for(str(repo))))
            from subprocess import CompletedProcess
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(DockerHostProbe, 'probe', return_value=DockerHostStatus(True, '/usr/bin/docker', True, '2.29', True, '27.0')), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run') as mock_run:
                # Selecting option 10 alone
                console.execute('10')
                # Must NOT execute any Docker mutations
                mock_run.assert_not_called()
                # Must stage a pending action with digest
                self.assertIsNotNone(console.pending)
                self.assertEqual(console.pending.name, 'native_up')
                self.assertTrue(bool(console.pending.digest))

                # Now explicit YES executes the staged action
                mock_run.return_value = CompletedProcess([], 0)
                console.execute('YES')
                self.assertIsNone(console.pending)
                mock_run.assert_called()

        def test_rc6_security_override_reset_on_project_switch(self):
            repo1 = self.root / 'sec-proj-1'
            repo1.mkdir()
            (repo1 / 'app.py').write_text('print(1)\n')
            repo2 = self.root / 'sec-proj-2'
            repo2.mkdir()
            (repo2 / 'app.py').write_text('print(2)\n')

            console = Console(Settings(target_for(str(repo1))))
            console.execute('insecure on')
            self.assertTrue(console.allow_insecure)

            # Switching project must reset allow_insecure to False
            console.execute(f"project '{repo2}'")
            self.assertFalse(console.allow_insecure)

        def test_rc6_recent_projects_store_clean_persistence(self):
            temp_store = self.root / 'recent.json'
            with patch.object(RecentProjectsStore, 'STORE_PATH', temp_store):
                p1 = self.root / 'proj1'
                p2 = self.root / 'proj2'
                p1.mkdir()
                p2.mkdir()
                RecentProjectsStore.add(p1)
                RecentProjectsStore.add(p2)
                loaded = RecentProjectsStore.load()
                self.assertEqual(len(loaded), 2)
                self.assertEqual(loaded[0], p2.resolve())
                self.assertEqual(loaded[1], p1.resolve())
                # Verify JSON format contains strings only
                data = json.loads(temp_store.read_text(encoding='utf-8'))
                self.assertIsInstance(data, list)
                self.assertNotIn('env', json.dumps(data))

        def test_rc6_docker_host_probe_accurately_reports_states(self):
            # CLI missing
            with patch('shutil.which', return_value=None):
                status = DockerHostProbe.probe()
                self.assertFalse(status.cli_installed)
                self.assertEqual(status.summary, 'CLI missing')

            from subprocess import CompletedProcess
            # Compose missing
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch('subprocess.run', side_effect=[CompletedProcess([], 1, stderr='no compose'), CompletedProcess([], 0, stdout='27.0')]):
                status = DockerHostProbe.probe()
                self.assertTrue(status.cli_installed)
                self.assertFalse(status.compose_available)
                self.assertEqual(status.summary, 'Compose missing')

            # Engine unreachable
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch('subprocess.run', side_effect=[CompletedProcess([], 0, stdout='Docker Compose v2.29'), CompletedProcess([], 1, stderr='Is the docker daemon running?')]):
                status = DockerHostProbe.probe()
                self.assertTrue(status.cli_installed)
                self.assertTrue(status.compose_available)
                self.assertFalse(status.engine_reachable)
                self.assertEqual(status.summary, 'Engine unreachable')

            # Engine active
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch('subprocess.run', side_effect=[CompletedProcess([], 0, stdout='Docker Compose v2.29'), CompletedProcess([], 0, stdout='27.1.1')]):
                status = DockerHostProbe.probe()
                self.assertTrue(status.engine_reachable)
                self.assertEqual(status.summary, 'Engine active')

        def test_rc6_browser_interactive_navigation_and_pagination(self):
            browse_dir = self.root / 'browser_test'
            browse_dir.mkdir()
            for i in range(15):
                (browse_dir / f'dir_{i:02d}').mkdir()
            (browse_dir / 'app.py').write_text('print("hello")\n')

            console = Console(Settings(target_for(str(browse_dir))))
            console.browser_dir = browse_dir
            console.render_browser()
            self.assertEqual(console.ui_mode, 'browser')
            # 15 dirs + 1 file = 16 items. Page size 12 -> 2 pages
            self.assertEqual(len(console.browser_items), 16)
            self.assertEqual(console.browser_page, 0)

            # Next page
            console._handle_browser_input('n')
            self.assertEqual(console.browser_page, 1)

            # Prev page
            console._handle_browser_input('p')
            self.assertEqual(console.browser_page, 0)

            # Filter
            console._handle_browser_input('/app')
            self.assertEqual(len(console.browser_items), 1)
            self.assertEqual(console.browser_items[0].name, 'app.py')

            # Clear filter
            console._handle_browser_input('/')
            self.assertEqual(len(console.browser_items), 16)

            # Return to menu
            console._handle_browser_input('0')
            self.assertEqual(console.ui_mode, 'menu')

        def test_rc6_multiline_paste_rejection(self):
            repo = self.root / 'multiline-paste-test'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n')
            (repo / '.env').write_text('KEY=val\n')
            console = Console(Settings(target_for(str(repo))))
            from subprocess import CompletedProcess
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(DockerHostProbe, 'probe', return_value=DockerHostStatus(True, '/usr/bin/docker', True, '2.29', True, '27.0')), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 0)) as mock_run:
                # 1. Pasting 10 and YES together in a single input turn
                console.feed_input('10\nYES')
                # Must NOT execute mutating Docker commands
                mock_run.assert_not_called()
                self.assertIsNotNone(console.pending)
                self.assertEqual(console.pending.name, 'native_up')

                # 2. Distinct subsequent input turn with YES executes
                console.feed_input('YES')
                self.assertIsNone(console.pending)
                mock_run.assert_called()

                # 3. Repeated YES in next turn is a no-op (token is single-use)
                mock_run.reset_mock()
                console.feed_input('YES')
                mock_run.assert_not_called()

                # 4. Cancellation invalidates staged action
                console.feed_input('10')
                self.assertIsNotNone(console.pending)
                console.feed_input('cancel')
                self.assertIsNone(console.pending)
                console.feed_input('YES')
                mock_run.assert_not_called()

                # 5. Project switch invalidates staged action
                console.feed_input('10')
                self.assertIsNotNone(console.pending)
                other = self.root / 'other_proj'
                other.mkdir()
                (other / 'app.py').write_text('print(1)\n')
                console.feed_input(f"project '{other}'")
                self.assertIsNone(console.pending)
                console.feed_input('YES')
                mock_run.assert_not_called()

        def test_rc6_fingerprint_invalidated_by_command_change(self):
            repo = self.root / 'fp-cmd-change'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n    command: echo hello\n')
            console = Console(Settings(target_for(str(repo))))
            from subprocess import CompletedProcess
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(DockerHostProbe, 'probe', return_value=DockerHostStatus(True, '/usr/bin/docker', True, '2.29', True, '27.0')), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 0)) as mock_run:
                console.feed_input('10')
                self.assertIsNotNone(console.pending)
                orig_digest = console.pending.digest

                # Edit only the command in compose.yaml
                (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n    command: echo altered\n')
                new_digest = DeploymentDigest.compute(repo, 'native_up')
                self.assertNotEqual(orig_digest, new_digest)

                # Attempting approval on next turn must fail
                with self.assertRaises(OpsError) as ctx:
                    console._approve()
                self.assertIn('Repository deployment configuration or files changed', str(ctx.exception))
                mock_run.assert_not_called()

        def test_rc6_fingerprint_invalidated_by_entrypoint_change(self):
            repo = self.root / 'fp-entry-change'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n    entrypoint: /bin/sh\n')
            console = Console(Settings(target_for(str(repo))))
            from subprocess import CompletedProcess
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(DockerHostProbe, 'probe', return_value=DockerHostStatus(True, '/usr/bin/docker', True, '2.29', True, '27.0')), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 0)):
                console.feed_input('10')
                orig_digest = console.pending.digest

                (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n    entrypoint: /bin/bash\n')
                new_digest = DeploymentDigest.compute(repo, 'native_up')
                self.assertNotEqual(orig_digest, new_digest)

                with self.assertRaises(OpsError):
                    console._approve()

        def test_rc6_fingerprint_invalidated_by_env_change(self):
            repo = self.root / 'fp-env-change'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n')
            (repo / '.env').write_text('SECRET=secret123\n')
            console = Console(Settings(target_for(str(repo))))
            from subprocess import CompletedProcess
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(DockerHostProbe, 'probe', return_value=DockerHostStatus(True, '/usr/bin/docker', True, '2.29', True, '27.0')), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 0)):
                console.feed_input('10')
                orig_digest = console.pending.digest

                (repo / '.env').write_text('SECRET=secret456\n')
                new_digest = DeploymentDigest.compute(repo, 'native_up')
                self.assertNotEqual(orig_digest, new_digest)

                with self.assertRaises(OpsError):
                    console._approve()

        def test_rc6_fingerprint_invalidated_by_dockerfile_change(self):
            repo = self.root / 'fp-df-change'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  app:\n    build: .\n')
            (repo / 'Dockerfile').write_text('FROM alpine:3.18\n')
            console = Console(Settings(target_for(str(repo))))
            from subprocess import CompletedProcess
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(DockerHostProbe, 'probe', return_value=DockerHostStatus(True, '/usr/bin/docker', True, '2.29', True, '27.0')), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 0)):
                console.feed_input('10')
                orig_digest = console.pending.digest

                (repo / 'Dockerfile').write_text('FROM alpine:3.19\n')
                new_digest = DeploymentDigest.compute(repo, 'native_up')
                self.assertNotEqual(orig_digest, new_digest)

                with self.assertRaises(OpsError):
                    console._approve()

        def test_rc6_fingerprint_invalidated_by_override_file(self):
            repo = self.root / 'fp-override-change'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n')
            (repo / 'compose.override.yaml').write_text('services:\n  app:\n    environment:\n      - FOO=bar\n')
            console = Console(Settings(target_for(str(repo))))
            from subprocess import CompletedProcess
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(DockerHostProbe, 'probe', return_value=DockerHostStatus(True, '/usr/bin/docker', True, '2.29', True, '27.0')), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 0)):
                console.feed_input('10')
                orig_digest = console.pending.digest

                (repo / 'compose.override.yaml').write_text('services:\n  app:\n    environment:\n      - FOO=baz\n')
                new_digest = DeploymentDigest.compute(repo, 'native_up')
                self.assertNotEqual(orig_digest, new_digest)

                with self.assertRaises(OpsError):
                    console._approve()

        def test_rc6_fingerprint_invalidated_by_security_policy(self):
            repo = self.root / 'fp-sec-policy'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n')
            console = Console(Settings(target_for(str(repo))))
            from subprocess import CompletedProcess
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(DockerHostProbe, 'probe', return_value=DockerHostStatus(True, '/usr/bin/docker', True, '2.29', True, '27.0')), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 0)):
                console.feed_input('10')
                orig_digest = console.pending.digest

                console.allow_insecure = True
                new_digest = DeploymentDigest.compute(repo, 'native_up', allow_insecure=True)
                self.assertNotEqual(orig_digest, new_digest)

                with self.assertRaises(OpsError):
                    console._approve()

        def test_rc6_fingerprint_invalidated_by_file_deletion_or_symlink(self):
            repo = self.root / 'fp-del-sym'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n')
            (repo / 'Dockerfile').write_text('FROM busybox\n')
            console = Console(Settings(target_for(str(repo))))
            from subprocess import CompletedProcess
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(DockerHostProbe, 'probe', return_value=DockerHostStatus(True, '/usr/bin/docker', True, '2.29', True, '27.0')), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 0)):
                console.feed_input('10')
                orig_digest = console.pending.digest

                (repo / 'Dockerfile').unlink()
                new_digest = DeploymentDigest.compute(repo, 'native_up')
                self.assertNotEqual(orig_digest, new_digest)

                with self.assertRaises(OpsError):
                    console._approve()

        def test_rc6_runtime_state_preserved_during_readiness(self):
            repo = self.root / 'lc-ready-runtime-preserve'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n')
            orch = DeploymentOrchestrator(repo)
            from subprocess import CompletedProcess
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 0)):
                ExistingInfrastructure.apply(repo, 'up', approved=True, orchestrator=orch)
                self.assertEqual(orch.state, DeploymentState.VERIFYING)

                # Running readiness while running must NOT raise illegal transition
                ExistingInfrastructure.apply(repo, 'ready', orchestrator=orch)
                self.assertEqual(orch.state, DeploymentState.VERIFYING)
                self.assertEqual(orch.last_validation, DeploymentState.VALIDATED)

                # Running it again
                ExistingInfrastructure.apply(repo, 'ready', orchestrator=orch)
                self.assertEqual(orch.state, DeploymentState.VERIFYING)

        def test_rc6_runtime_state_preserved_during_failing_check(self):
            repo = self.root / 'lc-failing-check-runtime-preserve'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n')
            orch = DeploymentOrchestrator(repo)
            from subprocess import CompletedProcess
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 0)):
                ExistingInfrastructure.apply(repo, 'up', approved=True, orchestrator=orch)
                self.assertEqual(orch.state, DeploymentState.VERIFYING)

            # Compose check fails with syntax error
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 1, stdout='', stderr='yaml syntax invalid')):
                with self.assertRaises(OpsError) as ctx:
                    ExistingInfrastructure.apply(repo, 'check', orchestrator=orch)
                # Primary error is preserved and returned directly!
                self.assertIn('Compose configuration check failed', str(ctx.exception))
                self.assertIn('yaml syntax invalid', str(ctx.exception))
                # Runtime state is preserved, NOT corrupted into BLOCKED!
                self.assertEqual(orch.state, DeploymentState.VERIFYING)
                self.assertEqual(orch.last_validation, DeploymentState.BLOCKED)

        def test_rc6_runtime_state_preserved_during_successful_check(self):
            repo = self.root / 'lc-succ-check-runtime-preserve'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n')
            orch = DeploymentOrchestrator(repo)
            from subprocess import CompletedProcess
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 0)):
                ExistingInfrastructure.apply(repo, 'up', approved=True, orchestrator=orch)
                self.assertEqual(orch.state, DeploymentState.VERIFYING)

                ExistingInfrastructure.apply(repo, 'check', orchestrator=orch)
                self.assertEqual(orch.state, DeploymentState.VERIFYING)
                self.assertEqual(orch.last_validation, DeploymentState.VALIDATED)

        def test_rc6_browser_compose_file_selection(self):
            repo = self.root / 'browse-compose-sel'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n')

            # target_for accepts compose.yaml directly
            t = target_for(str(repo / 'compose.yaml'))
            self.assertEqual(t.root, repo)
            self.assertIsNone(t.pinned)

            # In Console, selecting compose.yaml activates repo and shows existing stack
            console = Console()
            console.browser_dir = repo
            console.render_browser()
            with patch('sys.stdout', new_callable=io.StringIO) as out:
                console._handle_browser_input('compose.yaml')
            self.assertIsNotNone(console.settings)
            self.assertEqual(console.settings.target.root, repo)
            self.assertIsNone(console.settings.target.pinned)
            output = out.getvalue()
            self.assertIn('Existing Infrastructure Stack Detected', output)
            self.assertIn('Deployment Plan & Phasing', output)

        def test_rc6_browser_dockerfile_selection(self):
            repo = self.root / 'browse-df-sel'
            repo.mkdir()
            (repo / 'Dockerfile').write_text('FROM busybox\n')

            t = target_for(str(repo / 'Dockerfile'))
            self.assertEqual(t.root, repo)
            self.assertIsNone(t.pinned)

        def test_rc6_browser_unrelated_file_rejection(self):
            repo = self.root / 'browse-unrelated'
            repo.mkdir()
            (repo / 'notes.txt').write_text('notes\n')

            with self.assertRaises(OpsError) as ctx:
                target_for(str(repo / 'notes.txt'))
            self.assertIn("Selected file 'notes.txt' is not an accepted target", str(ctx.exception))
            self.assertIn("Choose a Python file (.py), a Docker Compose file", str(ctx.exception))

        def test_rc6_cli_native_verify_exit_codes(self):
            repo = self.root / 'cli-verify-codes'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n')
            from subprocess import CompletedProcess

            # 1. Healthy -> exit code 0
            healthy_json = json.dumps([{"Service": "app", "Name": "app-1", "State": "running", "Health": "healthy", "ExitCode": 0}])
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 0, stdout=healthy_json)):
                ret = main(['native', str(repo), '--native-action', 'verify'])
                self.assertEqual(ret, 0)

            # 2. Degraded -> exit code 1
            unhealthy_json = json.dumps([{"Service": "app", "Name": "app-1", "State": "running", "Health": "unhealthy", "ExitCode": 0}])
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 0, stdout=unhealthy_json)):
                ret = main(['native', str(repo), '--native-action', 'verify'])
                self.assertEqual(ret, 1)

            # 3. Docker unavailable -> exit code 2
            with patch('shutil.which', return_value=None):
                ret = main(['native', str(repo), '--native-action', 'verify'])
                self.assertEqual(ret, 2)

        def test_rc6_beginner_guided_workflow(self):
            repo = self.root / 'beginner-journey-test'
            repo.mkdir()
            (repo / 'compose.yaml').write_text('services:\n  app:\n    image: busybox\n    ports:\n      - "8080:8080"\n')
            from subprocess import CompletedProcess

            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(DockerHostProbe, 'probe', return_value=DockerHostStatus(True, '/usr/bin/docker', True, '2.29', True, '27.0')), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 0)) as mock_run:
                console = Console()
                # Step 1: Open Workbench
                with patch('sys.stdout', new_callable=io.StringIO) as out:
                    console.print_workbench_menu()
                self.assertIn('(Current terminal folder; not yet selected as project', out.getvalue())

                # Step 2: Open Browser
                console.execute('2')
                self.assertEqual(console.ui_mode, 'browser')

                # Step 3: Browse and select compose.yaml
                console.browser_dir = repo
                console.render_browser()
                console._handle_browser_input('compose.yaml')
                self.assertEqual(console.settings.target.root, repo)

                # Step 4: Intelligent Analysis
                self.assertTrue(console.execute('4'))

                # Step 5: Recommendations & Readiness
                self.assertTrue(console.execute('5'))

                # Step 6: Settings
                self.assertTrue(console.execute('6'))
                self.assertEqual(console.ui_mode, 'settings')
                console.dispatch_input('0')
                self.assertEqual(console.ui_mode, 'menu')

                # Step 7: Preview Plan
                self.assertTrue(console.execute('7'))

                # Step 8: View Plan
                self.assertTrue(console.execute('9'))

                # Step 9: Stage Deployment (Option 10)
                mock_run.reset_mock()
                self.assertTrue(console.execute('10'))
                self.assertIsNotNone(console.pending)
                mock_run.assert_not_called()

                # Step 10: Multiline paste rejected, separate turn approved
                console.feed_input('10\nYES')
                mock_run.assert_not_called()
                console.feed_input('YES')
                mock_run.assert_called()

        def test_rc6_live_test_compose_yaml_validity(self):
            # 1. Compose YAML generated by LiveAcceptanceTest is valid YAML, parses cleanly, and binds to 127.0.0.1
            run_id = "test1234"
            project_name = f"autodocker_live_{run_id}"
            port = 54321
            compose_content = (
                f"name: {project_name}\n"
                "services:\n"
                "  web:\n"
                "    build:\n"
                "      context: .\n"
                "      dockerfile: Dockerfile\n"
                f"    image: autodocker-live-test:{run_id}\n"
                "    ports:\n"
                f'      - "127.0.0.1:{port}:8080"\n'
                "    labels:\n"
                '      com.auto_docker.test: "true"\n'
                f'      com.auto_docker.run_id: "{run_id}"\n'
            )
            parsed = parse_compose_services_static(compose_content)
            self.assertIn('web', parsed)
            self.assertEqual(parsed['web']['build'], '.')
            self.assertEqual(parsed['web']['dockerfile'], 'Dockerfile')
            self.assertEqual(parsed['web']['image'], f"autodocker-live-test:{run_id}")
            self.assertEqual(parsed['web']['ports'], [f"127.0.0.1:{port}:8080"])
            self.assertTrue(parsed['web']['ports'][0].startswith("127.0.0.1:"))
            self.assertNotIn("0.0.0.0", compose_content)

        def test_rc6_live_test_port_mapping_loopback_only(self):
            # Live-test port mapping must be strictly 127.0.0.1, never 0.0.0.0 or bare port:8080
            port = 49152
            run_id = "loopback1"
            project_name = f"autodocker_live_{run_id}"
            compose_content = (
                f"name: {project_name}\n"
                "services:\n"
                "  web:\n"
                "    build:\n"
                "      context: .\n"
                "      dockerfile: Dockerfile\n"
                f"    image: autodocker-live-test:{run_id}\n"
                "    ports:\n"
                f'      - "127.0.0.1:{port}:8080"\n'
                "    labels:\n"
                '      com.auto_docker.test: "true"\n'
                f'      com.auto_docker.run_id: "{run_id}"\n'
            )
            self.assertIn(f'"127.0.0.1:{port}:8080"', compose_content)
            self.assertNotIn(f'"{port}:8080"', compose_content)
            self.assertNotIn('0.0.0.0', compose_content)
            parsed = parse_compose_services_static(compose_content)
            self.assertEqual(parsed['web']['ports'], [f"127.0.0.1:{port}:8080"])

        def test_rc6_live_test_server_py_valid_python(self):
            # 2. Temporary HTTP test server is valid Python and AST parses without SyntaxError
            server_py_content = (
                "from http.server import HTTPServer, BaseHTTPRequestHandler\n\n"
                "class LiveHandler(BaseHTTPRequestHandler):\n"
                "    def do_GET(self):\n"
                "        self.send_response(200)\n"
                "        self.send_header('Content-Type', 'text/plain')\n"
                "        self.end_headers()\n"
                "        self.wfile.write(b'LIVE_TEST_OK')\n\n"
                "    def log_message(self, format, *args):\n"
                "        pass\n\n"
                "if __name__ == '__main__':\n"
                "    httpd = HTTPServer(('0.0.0.0', 8080), LiveHandler)\n"
                "    httpd.serve_forever()\n"
            )
            tree = ast.parse(server_py_content)
            self.assertGreater(len(tree.body), 0)

        def test_rc6_live_test_files_and_build_context_exist(self):
            # 3. Dockerfile and build context actually exist
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir).resolve()
                (root / "server.py").write_text("print('test')\n", encoding="utf-8")
                (root / "Dockerfile").write_text("FROM python:3.11-alpine\nCOPY server.py .\n", encoding="utf-8")
                self.assertTrue((root / "server.py").is_file())
                self.assertTrue((root / "Dockerfile").is_file())
                df_text = (root / "Dockerfile").read_text(encoding="utf-8")
                self.assertIn("COPY server.py .", df_text)

        def test_rc6_live_test_build_invokes_real_image_build(self):
            # 4. Build stage invokes a real image build when authorized
            repo = self.root / "live-test-build-ctx"
            repo.mkdir()
            (repo / "server.py").write_text("print('test')\n", encoding="utf-8")
            (repo / "Dockerfile").write_text("FROM python:3.11-alpine\nCOPY server.py .\n", encoding="utf-8")
            (repo / "compose.yaml").write_text(
                "services:\n  web:\n    build:\n      context: .\n      dockerfile: Dockerfile\n    image: test:1\n"
            )
            from subprocess import CompletedProcess
            orch = DeploymentOrchestrator(repo)
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 0)) as mock_run:
                ExistingInfrastructure.apply(repo, "build", approved=True, orchestrator=orch, project_name="live_test_p")
                self.assertEqual(orch.state, DeploymentState.VALIDATED)
                mock_run.assert_called()
                cmd = mock_run.call_args[0][0]
                self.assertIn("build", cmd)
                self.assertIn("-p", cmd)
                self.assertIn("live_test_p", cmd)

        def test_rc6_live_test_requires_approval_no_mutation(self):
            # 5. No Docker mutations happen without approval
            from subprocess import CompletedProcess
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(LiveAcceptanceTest, 'verify_docker_endpoint', return_value=(True, 'local', 'default')), \
                 patch.object(DockerHostProbe, 'probe', return_value=DockerHostStatus(True, '/usr/bin/docker', True, '2.29', True, '27.0')), \
                 patch('subprocess.run', return_value=CompletedProcess([], 0)) as mock_run:
                with patch('sys.stdin.isatty', return_value=False):
                    ok = LiveAcceptanceTest.run(yes=False)
                    self.assertFalse(ok)
                    mock_run.assert_not_called()

        def test_rc6_live_test_failure_triggers_scoped_cleanup(self):
            # 6. A failed startup triggers scoped cleanup
            from subprocess import CompletedProcess
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(LiveAcceptanceTest, 'verify_docker_endpoint', return_value=(True, 'local', 'default')), \
                 patch.object(DockerHostProbe, 'probe', return_value=DockerHostStatus(True, '/usr/bin/docker', True, '2.29', True, '27.0')), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch('subprocess.run') as mock_run:
                def fake_run(cmd, *a, **kw):
                    if "up" in cmd:
                        return CompletedProcess(cmd, 1, stdout="", stderr="container crash")
                    return CompletedProcess(cmd, 0, stdout="", stderr="")
                mock_run.side_effect = fake_run
                ok = LiveAcceptanceTest.run(yes=True)
                self.assertFalse(ok)
                cleanup_calls = [call for call in mock_run.call_args_list if 'down' in call[0][0]]
                self.assertTrue(len(cleanup_calls) > 0)
                self.assertIn('-p', cleanup_calls[0][0][0])

        def test_rc6_live_test_cleanup_failure_not_reported_as_pass(self):
            # 7. Cleanup failure is not reported as PASS
            with patch('shutil.which', return_value='/usr/bin/docker'), \
                 patch.object(LiveAcceptanceTest, 'verify_docker_endpoint', return_value=(True, 'local', 'default')), \
                 patch.object(DockerHostProbe, 'probe', return_value=DockerHostStatus(True, '/usr/bin/docker', True, '2.29', True, '27.0')), \
                 patch.object(EnvironmentDoctor, '_probe', return_value=(True, 'OK')), \
                 patch.object(LiveAcceptanceTest, 'scoped_cleanup', return_value=(False, 'lingering containers detected')):
                with patch('auto_docker._wait_http', return_value=None), \
                     patch('urllib.request.urlopen') as mock_url:
                    mock_resp = mock_url.return_value.__enter__.return_value
                    mock_resp.status = 200
                    mock_resp.read.return_value = b'LIVE_TEST_OK'
                    ok = LiveAcceptanceTest.run(yes=True)
                    self.assertFalse(ok)

        def test_rc6_live_test_cleanup_failure_compose_down_fails(self):
            # Negative test: failed compose down must cause cleanup to fail
            from subprocess import CompletedProcess
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir).resolve()
                (root / "compose.yaml").write_text("name: test\n", encoding="utf-8")
                with patch.object(ResourceCleaner, '_run_docker') as mock_docker:
                    def fake_run(args, prefix=None, timeout=30):
                        if args and args[0] == "compose":
                            return CompletedProcess([], 1, stderr="compose down daemon connection lost")
                        return CompletedProcess([], 0, stdout="")
                    mock_docker.side_effect = fake_run
                    clean_ok, clean_msg = LiveAcceptanceTest.scoped_cleanup(root, "test_proj", "r123", ["docker"])
                    self.assertFalse(clean_ok)
                    self.assertIn("compose down failed", clean_msg)
                    self.assertIn("daemon connection lost", clean_msg)

        def test_rc6_live_test_cleanup_failure_image_still_exists(self):
            # Negative test: failed image removal (image still present in post-scan) must cause cleanup to fail
            from subprocess import CompletedProcess
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir).resolve()
                (root / "compose.yaml").write_text("name: test\n", encoding="utf-8")
                with patch.object(ResourceCleaner, '_run_docker') as mock_docker:
                    def fake_run(args, prefix=None, timeout=30):
                        if args and args[0] == "images":
                            return CompletedProcess([], 0, stdout=json.dumps({"ID": "img123", "Repository": "autodocker-live-test", "Tag": "r123"}))
                        if args and args[0] == "rmi":
                            return CompletedProcess([], 1, stderr="image is referenced by stopped container")
                        return CompletedProcess([], 0, stdout="")
                    mock_docker.side_effect = fake_run
                    clean_ok, clean_msg = LiveAcceptanceTest.scoped_cleanup(root, "test_proj", "r123", ["docker"])
                    self.assertFalse(clean_ok)
                    self.assertTrue("image rmi failed" in clean_msg or "lingering resources" in clean_msg)

        def test_rc6_live_test_cleanup_failure_inspection_fails(self):
            # Negative test: failed container removal (rm exits non-zero) must cause cleanup to fail
            from subprocess import CompletedProcess
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir).resolve()
                (root / "compose.yaml").write_text("name: test\n", encoding="utf-8")
                with patch.object(ResourceCleaner, '_run_docker') as mock_docker:
                    def fake_run(args, prefix=None, timeout=30):
                        if args and args[0] == "ps":
                            return CompletedProcess([], 0, stdout=json.dumps({
                                "ID": "c12345678901",
                                "Names": "autodocker_live_r123",
                                "Image": "autodocker-live-test:r123",
                                "Labels": "com.auto_docker.test=true,com.auto_docker.run_id=r123",
                            }))
                        if args and args[0] == "rm":
                            return CompletedProcess([], 1, stderr="cannot remove container: permission denied")
                        return CompletedProcess([], 0, stdout="")
                    mock_docker.side_effect = fake_run
                    clean_ok, clean_msg = LiveAcceptanceTest.scoped_cleanup(root, "test_proj", "r123", ["docker"])
                    self.assertFalse(clean_ok)
                    self.assertIn("container rm failed", clean_msg)

        def test_rc6_live_test_cleanup_failure_stopped_containers_remain(self):
            # Negative test: stopped/exited containers lingering in post-scan must cause cleanup to fail
            from subprocess import CompletedProcess
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir).resolve()
                (root / "compose.yaml").write_text("name: test\n", encoding="utf-8")
                with patch.object(ResourceCleaner, '_run_docker') as mock_docker:
                    def fake_run(args, prefix=None, timeout=30):
                        if args and args[0] == "ps":
                            return CompletedProcess([], 0, stdout=json.dumps({
                                "ID": "c_lingering_1",
                                "Names": "autodocker_live_lingering",
                                "Image": "python:3.11-slim",
                                "Labels": "com.auto_docker.test=true,com.docker.compose.project=test_proj",
                                "State": "exited",
                            }))
                        return CompletedProcess([], 0, stdout="")
                    mock_docker.side_effect = fake_run
                    clean_ok, clean_msg = LiveAcceptanceTest.scoped_cleanup(root, "test_proj", "r123", ["docker"])
                    self.assertFalse(clean_ok)
                    self.assertIn("lingering resources", clean_msg)
                    self.assertIn("autodocker_live_lingering", clean_msg)

        def test_rc6_live_test_cleanup_verification_complete_success(self):
            # Positive test: complete cleanup removes resources and verifies 0 leftovers
            from subprocess import CompletedProcess
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir).resolve()
                (root / "compose.yaml").write_text("name: test\n", encoding="utf-8")
                call_counts = {"ps": 0}
                with patch.object(ResourceCleaner, '_run_docker') as mock_docker:
                    def fake_run(args, prefix=None, timeout=30):
                        if args and args[0] == "ps":
                            call_counts["ps"] += 1
                            if call_counts["ps"] == 1:
                                return CompletedProcess([], 0, stdout=json.dumps({
                                    "ID": "c_success_1",
                                    "Names": "autodocker_live_success",
                                    "Image": "autodocker-live-test:r123",
                                    "Labels": "com.auto_docker.test=true",
                                }))
                            # post-scan returns empty: container cleanly gone!
                            return CompletedProcess([], 0, stdout="")
                        return CompletedProcess([], 0, stdout="")
                    mock_docker.side_effect = fake_run
                    clean_ok, clean_msg = LiveAcceptanceTest.scoped_cleanup(root, "test_proj", "r123", ["docker"])
                    self.assertTrue(clean_ok)
                    self.assertEqual(clean_msg, "all project resources cleanly removed and verified")

        def test_rc6_cleaner_scan_classification(self):
            # ResourceCleaner.scan classifies test, project, orphan, and external resources
            from subprocess import CompletedProcess
            c_lines = "\n".join([
                json.dumps({"ID": "c_test_1", "Names": "autodocker_live_worker", "Image": "autodocker-live-test:r1", "Labels": "com.auto_docker.test=true,com.auto_docker.run_id=r1"}),
                json.dumps({"ID": "c_proj_1", "Names": "my_app_web_1", "Image": "my_app:latest", "Labels": "com.docker.compose.project=my_app"}),
                json.dumps({"ID": "c_ext_1", "Names": "external_db", "Image": "postgres:16", "Labels": ""}),
            ])
            img_lines = "\n".join([
                json.dumps({"ID": "img_test_1", "Repository": "autodocker-live-test", "Tag": "r1", "CreatedAt": "2026-11-08"}),
                json.dumps({"ID": "img_other_1", "Repository": "python", "Tag": "3.11-slim", "CreatedAt": "2026-11-08"}),
            ])
            net_lines = "\n".join([
                json.dumps({"ID": "net_test_1", "Name": "autodocker_live_r1_default", "Driver": "bridge"}),
                json.dumps({"ID": "net_proj_1", "Name": "my_app_default", "Driver": "bridge"}),
                json.dumps({"ID": "net_bridge", "Name": "bridge", "Driver": "bridge"}),
            ])
            vol_lines = "\n".join([
                json.dumps({"ID": "vol_test_1", "Name": "autodocker_live_r1_data", "Driver": "local"}),
                json.dumps({"ID": "vol_proj_1", "Name": "my_app_data", "Driver": "local"}),
            ])
            with patch.object(ResourceCleaner, '_run_docker') as mock_docker:
                def fake_run(args, prefix=None, timeout=30):
                    if args[0] == "ps":
                        return CompletedProcess([], 0, stdout=c_lines)
                    if args[0] == "images":
                        return CompletedProcess([], 0, stdout=img_lines)
                    if args[0] == "network":
                        return CompletedProcess([], 0, stdout=net_lines)
                    if args[0] == "volume":
                        return CompletedProcess([], 0, stdout=vol_lines)
                    return CompletedProcess([], 0, stdout="")
                mock_docker.side_effect = fake_run
                with patch('shutil.which', return_value='/usr/bin/docker'):
                    res = ResourceCleaner.scan()
                    self.assertEqual(len(res), 9)  # 3 containers + 2 images + 2 networks (bridge ignored) + 2 volumes
                    by_name = {r.name: r for r in res}
                    self.assertTrue(by_name["autodocker_live_worker"].is_test)
                    self.assertTrue(by_name["autodocker_live_worker"].is_orphan)
                    self.assertEqual(by_name["my_app_web_1"].project, "my_app")
                    self.assertFalse(by_name["my_app_web_1"].is_test)
                    self.assertFalse(by_name["external_db"].is_test)
                    self.assertIsNone(by_name["external_db"].project)
                    self.assertTrue(by_name["autodocker-live-test:r1"].is_test)
                    self.assertFalse(by_name["python:3.11-slim"].is_test)
                    self.assertTrue(by_name["autodocker_live_r1_default"].is_test)
                    self.assertEqual(by_name["my_app_default"].project, "my_app")

        def test_rc6_cleaner_plan_volume_protection(self):
            # Volume protection: preserve_volumes=True preserves named project volumes by default
            r_c = CleanableResource("container", "c1", "my_app_web", project="my_app", is_test=False, is_orphan=False, status="running")
            r_v = CleanableResource("volume", "vol1", "my_app_data", project="my_app", is_test=False, is_orphan=False, status="created")
            resources = [r_c, r_v]

            plan_protected = ResourceCleaner.plan_cleanup(mode="project", target_project="my_app", preserve_volumes=True, resources=resources)
            self.assertEqual(len(plan_protected.containers), 1)
            self.assertEqual(len(plan_protected.volumes), 0)
            self.assertEqual(len(plan_protected.protected_volumes), 1)
            self.assertEqual(plan_protected.protected_volumes[0].name, "my_app_data")
            self.assertEqual(plan_protected.total_count, 1)

            plan_destructive = ResourceCleaner.plan_cleanup(mode="project", target_project="my_app", preserve_volumes=False, resources=resources)
            self.assertEqual(len(plan_destructive.volumes), 1)
            self.assertEqual(len(plan_destructive.protected_volumes), 0)
            self.assertEqual(plan_destructive.total_count, 2)

        def test_rc6_cleaner_unrelated_resources_strictly_protected(self):
            # Unrelated / external resources must never be targeted by any cleaner plan
            ext_c = CleanableResource("container", "c_prod", "prod_postgres", project=None, is_test=False, is_orphan=False, status="running")
            ext_img = CleanableResource("image", "img_prod", "postgres:16", project=None, is_test=False, is_orphan=False, status="available")
            ext_net = CleanableResource("network", "net_prod", "custom_prod_net", project=None, is_test=False, is_orphan=False, status="active")
            ext_vol = CleanableResource("volume", "vol_prod", "pg_production_data", project=None, is_test=False, is_orphan=False, status="created")
            resources = [ext_c, ext_img, ext_net, ext_vol]

            plan_test = ResourceCleaner.plan_cleanup(mode="test", resources=resources)
            self.assertEqual(plan_test.total_count, 0)

            plan_proj = ResourceCleaner.plan_cleanup(mode="project", target_project="other_proj", resources=resources)
            self.assertEqual(plan_proj.total_count, 0)

            plan_orphan = ResourceCleaner.plan_cleanup(mode="orphan", resources=resources)
            self.assertEqual(plan_orphan.total_count, 0)

        def test_rc6_cleaner_orphan_recovery(self):
            # Orphan recovery matches abandoned test containers, networks, and images
            r1 = CleanableResource("container", "c_orp", "autodocker_live_crash", project=None, is_test=True, is_orphan=True, status="exited")
            r2 = CleanableResource("network", "net_orp", "autodocker_live_crash_default", project=None, is_test=True, is_orphan=True, status="active")
            r3 = CleanableResource("container", "c_keep", "regular_container", project="regular", is_test=False, is_orphan=False, status="running")
            resources = [r1, r2, r3]

            plan = ResourceCleaner.plan_cleanup(mode="orphan", resources=resources)
            self.assertEqual(plan.total_count, 2)
            self.assertIn(r1, plan.containers)
            self.assertIn(r2, plan.networks)
            self.assertNotIn(r3, plan.containers)

        def test_rc6_cleaner_execute_stops_and_removes(self):
            # Execute performs ordered removal and re-scans for zero leftovers
            from subprocess import CompletedProcess
            r_c = CleanableResource("container", "c100", "test_c", project=None, is_test=True, is_orphan=True, status="running")
            r_n = CleanableResource("network", "n100", "test_n", project=None, is_test=True, is_orphan=True, status="active")
            r_i = CleanableResource("image", "i100", "test_i:tag", project=None, is_test=True, is_orphan=True, status="available")
            plan = CleanupPlan(mode="test", target_project=None, containers=(r_c,), images=(r_i,), networks=(r_n,), volumes=(), protected_volumes=())

            commands_run = []
            with patch.object(ResourceCleaner, '_run_docker') as mock_docker:
                def fake_run(args, prefix=None, timeout=30):
                    commands_run.append(args[0])
                    if args[0] == "image" and args[1] == "inspect":
                        return CompletedProcess([], 1, stderr="no such image")
                    return CompletedProcess([], 0, stdout="")
                mock_docker.side_effect = fake_run
                with patch.object(ResourceCleaner, 'scan', return_value=[]):
                    res = ResourceCleaner.execute(plan)
                    self.assertTrue(res.success)
                    self.assertEqual(len(res.cleaned), 3)
                    self.assertEqual(len(res.failed), 0)
                    self.assertEqual(len(res.leftovers), 0)
                    self.assertIn("Successfully cleaned 3 resources; 0 leftovers.", res.message)
                    self.assertIn("stop", commands_run)
                    self.assertIn("rm", commands_run)
                    self.assertIn("network", commands_run)
                    self.assertIn("rmi", commands_run)

        def test_rc6_cleaner_execute_detects_lingering_leftovers(self):
            # Execute marks cleanup incomplete when resources remain in post-scan
            r_c = CleanableResource("container", "c100", "test_c", project=None, is_test=True, is_orphan=True, status="running")
            plan = CleanupPlan(mode="test", target_project=None, containers=(r_c,), images=(), networks=(), volumes=(), protected_volumes=())

            with patch.object(ResourceCleaner, '_run_docker', return_value=subprocess.CompletedProcess([], 0)):
                with patch.object(ResourceCleaner, 'scan', return_value=[r_c]):
                    res = ResourceCleaner.execute(plan)
                    self.assertFalse(res.success)
                    self.assertEqual(len(res.leftovers), 1)
                    self.assertIn("Cleanup incomplete", res.message)
                    self.assertIn("1 lingering resources", res.message)

        def test_rc6_cleaner_idempotency_safe(self):
            # Execute on empty plan succeeds cleanly with 0 operations
            plan = CleanupPlan(mode="test", target_project=None, containers=(), images=(), networks=(), volumes=(), protected_volumes=())
            res = ResourceCleaner.execute(plan)
            self.assertTrue(res.success)
            self.assertEqual(len(res.cleaned), 0)
            self.assertEqual(len(res.leftovers), 0)
            self.assertIn("Successfully cleaned 0 resources", res.message)

        def test_rc6_cleaner_format_plan_preview(self):
            # Preview format accurately displays resource counts, names, and protected volumes
            r_c = CleanableResource("container", "c_id_123456789", "my_app_web", project="my_app", is_test=False, is_orphan=False, status="running")
            r_v = CleanableResource("volume", "vol_data", "my_app_db_data", project="my_app", is_test=False, is_orphan=False, status="created")
            plan = CleanupPlan(mode="project", target_project="my_app", containers=(r_c,), images=(), networks=(), volumes=(), protected_volumes=(r_v,))
            preview = ResourceCleaner.format_plan_preview(plan)
            self.assertIn("Auto Docker Resource Cleaner — Cleanup Plan Preview", preview)
            self.assertIn("Containers to remove : 1", preview)
            self.assertIn("Volumes to remove    : 0", preview)
            self.assertIn("Volumes protected    : 1 (User data preserved by default)", preview)
            self.assertIn("my_app_web", preview)
            self.assertIn("[PROTECTED] my_app_db_data", preview)

        def test_rc6_cleaner_cli_scan_and_clean(self):
            # CLI actions 'cleaner' and 'clean' with --scan, --clean-test, and missing project errors
            with patch.object(DockerHostProbe, 'probe', return_value=DockerHostStatus(True, "/usr/bin/docker", True, "2.29", True, "27.0")):
                with patch.object(ResourceCleaner, 'scan', return_value=[
                    CleanableResource("container", "c12345678901", "test_app", project="test", is_test=True, is_orphan=False, status="running")
                ]):
                    with patch('sys.stdout', new_callable=io.StringIO) as out:
                        code = main(["cleaner", "--scan"])
                        self.assertEqual(code, 0)
                        self.assertIn("Discovered 1 Docker resources:", out.getvalue())
                        self.assertIn("test_app", out.getvalue())

                # Missing project path with --clean-project returns exit code 2
                with patch('sys.stderr', new_callable=io.StringIO) as err:
                    code = main(["clean", "--clean-project"])
                    self.assertEqual(code, 2)
                    self.assertIn("--clean-project requires a project path argument", err.getvalue())

                # Non-interactive confirmation required without --yes
                with patch.object(ResourceCleaner, 'scan', return_value=[
                    CleanableResource("container", "c12345678901", "test_app", project=None, is_test=True, is_orphan=True, status="running")
                ]):
                    with patch('sys.stdin.isatty', return_value=False):
                        with patch('sys.stderr', new_callable=io.StringIO) as err:
                            code = main(["clean", "--clean-test"])
                            self.assertEqual(code, 2)
                            self.assertIn("Explicit confirmation required for cleaner operations. Pass --yes.", err.getvalue())

                # Passing --yes executes cleanup successfully
                with patch.object(ResourceCleaner, 'scan', return_value=[]):
                    with patch.object(ResourceCleaner, 'execute', return_value=CleanupResult(True, (), (), (), "Cleaned 0 resources")):
                        with patch('sys.stdout', new_callable=io.StringIO) as out:
                            code = main(["clean", "--clean-test", "--yes"])
                            self.assertEqual(code, 0)

        def test_rc6_console_cleaner_menu_navigation(self):
            # Interactive Console Option 12 enters cleaner UI and menu options 1, 2, 0 operate correctly
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir).resolve()
                con = Console(Settings(target_for(str(root))))
                # Command "12" or "cleaner" opens cleaner menu
                con.execute("12")
                self.assertEqual(con.ui_mode, "cleaner")

                with patch.object(ResourceCleaner, 'scan', return_value=[
                    CleanableResource("container", "c12345678901", "live_test", project=None, is_test=True, is_orphan=True, status="running")
                ]):
                    with patch('sys.stdout', new_callable=io.StringIO) as out:
                        # "1" = scan
                        con.dispatch_input("1")
                        self.assertIn("Scanning Docker resources", out.getvalue())
                        self.assertIn("live_test", out.getvalue())

                    with patch('sys.stdout', new_callable=io.StringIO) as out:
                        # "2" = preview
                        con.dispatch_input("2")
                        self.assertIn("Cleanup Plan Preview", out.getvalue())

                # "0" returns to main menu
                con.dispatch_input("0")
                self.assertEqual(con.ui_mode, "menu")

        def test_rc6_live_test_implicit_remote_context_rejected(self):
            # Implicit remote context (when context is omitted but active context is remote) must be rejected
            from subprocess import CompletedProcess
            with patch('subprocess.run') as mock_run:
                mock_run.side_effect = [
                    CompletedProcess([], 0, stdout="remote-cluster\n"),
                    CompletedProcess([], 0, stdout=json.dumps([{"Name": "remote-cluster", "Endpoints": {"docker": {"Host": "ssh://user@remote-box"}}}])),
                ]
                ok, msg, ctx = LiveAcceptanceTest.verify_docker_endpoint(context=None, docker_cli='/usr/bin/docker')
                self.assertFalse(ok)
                self.assertEqual(ctx, "remote-cluster")
                self.assertIn("Remote Docker context 'remote-cluster' is unsupported for live-test", msg)
                self.assertIn("SSH remote daemon", msg)

        def test_rc6_live_test_implicit_remote_docker_host_rejected(self):
            # Remote endpoint configured via DOCKER_HOST must be detected and rejected
            with patch.dict(os.environ, {"DOCKER_HOST": "tcp://192.168.1.100:2375"}):
                ok, msg, _ = LiveAcceptanceTest.verify_docker_endpoint(context=None, docker_cli='/usr/bin/docker')
                self.assertFalse(ok)
                self.assertIn("Remote Docker Engine detected via DOCKER_HOST=tcp://192.168.1.100:2375", msg)
                self.assertIn("remote network host", msg)

        def test_rc6_live_test_explicit_local_context_accepted(self):
            # Explicit local context (e.g. desktop-linux or colima) with local unix socket must be accepted
            from subprocess import CompletedProcess
            with patch('subprocess.run') as mock_run:
                mock_run.side_effect = [
                    CompletedProcess([], 0, stdout=json.dumps([{"Name": "desktop-linux", "Endpoints": {"docker": {"Host": "unix:///Users/test/.docker/run/docker.sock"}}}])),
                ]
                ok, msg, ctx = LiveAcceptanceTest.verify_docker_endpoint(context="desktop-linux", docker_cli='/usr/bin/docker')
                self.assertTrue(ok)
                self.assertEqual(ctx, "desktop-linux")
                self.assertIn("local Docker Engine verified", msg)

        def test_rc6_live_test_context_inspection_uncertainty_blocked(self):
            # Inspection failure or inconclusive context state must report BLOCKED
            from subprocess import CompletedProcess
            with patch('subprocess.run') as mock_run:
                mock_run.side_effect = [
                    CompletedProcess([], 1, stderr="context inspect failed: permission denied"),
                ]
                ok, msg, ctx = LiveAcceptanceTest.verify_docker_endpoint(context="uncertain-ctx", docker_cli='/usr/bin/docker')
                self.assertFalse(ok)
                self.assertEqual(ctx, "uncertain-ctx")
                self.assertIn("BLOCKED: Unable to inspect Docker context 'uncertain-ctx'", msg)

        def test_rc6_live_test_remote_context_unsupported(self):
            # 8. Remote Docker contexts are explicitly unsupported for live-test with clear message
            with patch('shutil.which', return_value=None), \
                 patch('sys.stderr', new_callable=io.StringIO) as err:
                ok = LiveAcceptanceTest.run(yes=True, context='remote-server')
                self.assertFalse(ok)
                self.assertIn("Remote Docker context 'remote-server' is unsupported for live-test", err.getvalue())

        def test_rc6_live_test_procedure_offline_safety(self):
            # 9. Missing Docker Engine produces a safe BLOCKED result
            with patch('shutil.which', return_value=None):
                ok = LiveAcceptanceTest.run(yes=True)
                self.assertFalse(ok)

        def test_rc6_python_compatibility_check(self):
            # 10. Python compatibility check: passes on >= 3.10, friendly error on < 3.10
            ok, msg = check_python_compatibility((3, 11, 2))
            self.assertTrue(ok)
            self.assertIn("compatible", msg)

            ok, msg = check_python_compatibility((3, 8, 20))
            self.assertFalse(ok)
            self.assertIn("Auto Docker requires Python 3.10+", msg)
            self.assertIn("detected Python 3.8.20", msg)
            self.assertIn("python3.11", msg)

        def test_rc6_wizard_3_step_guided_flow(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                repo = Path(tmpdir).resolve()
                (repo / "train.py").write_text("import time\nprint('Training...')\n")
                (repo / "artifacts").mkdir()
                con = Console(Settings(target_for(str(repo))))

                # Step 1: Trigger wizard
                with patch('sys.stdout', new_callable=io.StringIO) as out:
                    con.render_wizard()
                    self.assertEqual(con.ui_mode, "wizard")
                    self.assertEqual(con.wizard_step, 1)
                    val = out.getvalue()
                    self.assertIn("Step 1 of 3: Detection", val)
                    self.assertIn("train.py", val)
                    self.assertIn("artifacts", val)

                # Step 1 -> Step 2
                with patch('sys.stdout', new_callable=io.StringIO) as out:
                    con.dispatch_input("")
                    self.assertEqual(con.wizard_step, 2)
                    val = out.getvalue()
                    self.assertIn("Step 2 of 3: Configuration", val)
                    self.assertIn("Workload Profile", val)

                # Step 2 -> Step 3
                with patch('sys.stdout', new_callable=io.StringIO) as out:
                    con.dispatch_input("")
                    self.assertEqual(con.wizard_step, 3)
                    val = out.getvalue()
                    self.assertIn("Step 3 of 3: Safe Deploy", val)
                    self.assertIn("Deployment Plan & Preflight Safety Review", val)

                # Step 3 -> Deploy & Run Now ('1')
                with patch('sys.stdout', new_callable=io.StringIO) as out:
                    con.dispatch_input("1")
                    val = out.getvalue()
                    self.assertIn("Auto-Pilot Deployment Staged", val)
                    self.assertIn("Type YES to confirm deployment", val)
                    self.assertIsNotNone(con.pending)
                    self.assertEqual(con.pending.name, "generate")
                    self.assertEqual(con.ui_mode, "menu")

        def test_rc6_wizard_step2_toggle_options(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                repo = Path(tmpdir).resolve()
                (repo / "app.py").write_text("print('hello')\n")
                con = Console(Settings(target_for(str(repo))))
                con.render_wizard()
                con.dispatch_input("")  # Move to step 2
                self.assertEqual(con.wizard_step, 2)

                # Toggle mode (1)
                initial_mode = con.settings.mode
                con.dispatch_input("1")
                self.assertNotEqual(con.settings.mode, initial_mode)

                # Toggle execution_kind (2)
                initial_kind = con.settings.execution_kind
                con.dispatch_input("2")
                self.assertNotEqual(con.settings.execution_kind, initial_kind)

                # Toggle persistence (3)
                con.dispatch_input("3")
                self.assertTrue(len(con.settings.persistent_dirs) > 0)
                con.dispatch_input("3")
                self.assertEqual(con.settings.persistent_dirs, ())

                # Toggle services (4)
                con.dispatch_input("4")
                self.assertEqual(con.settings.services_override, ("mlflow",))
                con.dispatch_input("4")
                self.assertEqual(con.settings.services_override, ())

                # Set port (5)
                con.dispatch_input("5")
                self.assertTrue(con.wizard_awaiting_port)
                con.dispatch_input("8088")
                self.assertFalse(con.wizard_awaiting_port)
                self.assertEqual(con.settings.port, 8088)

                # Toggle GPU (6)
                self.assertFalse(con.settings.gpu)
                con.dispatch_input("6")
                self.assertTrue(con.settings.gpu)

                # Cancel back to menu
                con.dispatch_input("0")
                self.assertEqual(con.ui_mode, "menu")

        def test_rc6_top_level_app_vs_nested_function_detection(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                repo = Path(tmpdir).resolve()
                # Subcase 1: True top-level FastAPI
                api_file = repo / "fastapi_top.py"
                api_file.write_text("from fastapi import FastAPI\napp = FastAPI()\n")
                entry_top = _check_source(api_file, repo)
                self.assertEqual(entry_top.framework, "fastapi")
                self.assertTrue(entry_top.app_object)

                plan_top = Planner().build(Settings(target_for(str(api_file))), Inspector().inspect(target_for(str(api_file))))
                self.assertIn("uvicorn", " ".join(plan_top.command))

                # Subcase 2: CLI script with nested app = FastAPI() inside a helper function
                cli_file = repo / "cli_nested.py"
                cli_code = (
                    "import argparse\n"
                    "def serve_cmd(args):\n"
                    "    from fastapi import FastAPI\n"
                    "    app = FastAPI()\n"
                    "def main():\n"
                    "    name = input('Name: ')\n"
                    "    print(f'Hello {name}')\n"
                    "if __name__ == '__main__':\n"
                    "    main()\n"
                )
                cli_file.write_text(cli_code)
                entry_nested = _check_source(cli_file, repo)
                self.assertEqual(entry_nested.framework, "python")
                self.assertFalse(entry_nested.app_object)
                self.assertTrue(entry_nested.needs_stdin)

                plan_nested = Planner().build(Settings(target_for(str(cli_file))), Inspector().inspect(target_for(str(cli_file))))
                self.assertEqual(plan_nested.command, ("python", "cli_nested.py"))
                self.assertEqual(plan_nested.execution_kind, "interactive")

        def test_rc6_mlops_persistent_directories_discovery_and_mounting(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                repo = Path(tmpdir).resolve()
                (repo / "automl.py").write_text("print('AutoML running')\n")
                (repo / "artifacts").mkdir()
                (repo / "models").mkdir()
                (repo / "checkpoints").mkdir()

                settings = Settings(target_for(str(repo)))
                inspection = Inspector().inspect(settings.target)
                plan = Planner().build(settings, inspection)
                self.assertIn("artifacts", plan.persistent_dirs)
                self.assertIn("models", plan.persistent_dirs)
                self.assertIn("checkpoints", plan.persistent_dirs)

                # Check compose.yaml generation contains volume mounts
                files = Renderer().files(plan)
                compose_content = files["compose.yaml"]
                self.assertIn("../artifacts:/app/artifacts", compose_content)
                self.assertIn("../models:/app/models", compose_content)
                self.assertIn("../checkpoints:/app/checkpoints", compose_content)

        def test_rc6_wizard_cli_actions(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                repo = Path(tmpdir).resolve()
                (repo / "main.py").write_text("print('ready')\n")
                # Invoke main with 'wizard'
                with patch.object(Console, 'loop', return_value=None) as mock_loop:
                    with patch('sys.stdout', new_callable=io.StringIO) as out:
                        code = main(["wizard", str(repo)])
                        self.assertEqual(code, 0)
                        mock_loop.assert_called_once()
                        self.assertIn("Step 1 of 3: Detection", out.getvalue())

                # Invoke main with 'autopilot'
                with patch.object(Console, 'loop', return_value=None) as mock_loop:
                    with patch('sys.stdout', new_callable=io.StringIO) as out:
                        code = main(["autopilot", str(repo)])
                        self.assertEqual(code, 0)
                        mock_loop.assert_called_once()

        def test_rc6_scalability_replicas_and_resource_limits(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                repo = Path(tmpdir).resolve()
                (repo / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
                target = target_for(str(repo / "app.py"))
                settings = Settings(target, replicas=3, cpus="1.5", memory="1G")
                plan = Planner().build(settings, Inspector().inspect(target))
                self.assertEqual(plan.replicas, 3)
                self.assertEqual(plan.cpus, "1.5")
                self.assertEqual(plan.memory, "1G")

                files = Renderer().files(plan)
                self.assertIn("deploy:", files["compose.yaml"])
                self.assertIn("replicas: 3", files["compose.yaml"])
                self.assertIn("cpus: \"1.5\"", files["compose.yaml"])
                self.assertIn("memory: \"1G\"", files["compose.yaml"])

                # Test Console commands
                con = Console(settings)
                con.execute("replicas 4")
                self.assertEqual(con.settings.replicas, 4)
                con.execute("resources cpus=2.0 memory=2G")
                self.assertEqual(con.settings.cpus, "2.0")
                self.assertEqual(con.settings.memory, "2G")
                con.execute("resources none")
                self.assertIsNone(con.settings.cpus)
                self.assertIsNone(con.settings.memory)

                # Validation checks
                with self.assertRaises(OpsError):
                    con.execute("replicas 0")
                with self.assertRaises(OpsError):
                    con.execute("replicas 100")
                with self.assertRaises(OpsError):
                    con.execute("resources cpus=-1")
                with self.assertRaises(OpsError):
                    con.execute("resources memory=invalid_size")

        def test_rc6_observability_prometheus_and_grafana_services(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                repo = Path(tmpdir).resolve()
                (repo / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
                target = target_for(str(repo / "app.py"))
                settings = Settings(target, services_override=("prometheus", "grafana"), port=8000)
                plan = Planner().build(settings, Inspector().inspect(target))
                self.assertIn("prometheus", plan.services)
                self.assertIn("grafana", plan.services)

                # Check ports
                pub = {p.service: p for p in plan.published_ports}
                self.assertEqual(pub["prometheus"].container, 9090)
                self.assertEqual(pub["grafana"].container, 3000)

                files = Renderer().files(plan)
                self.assertIn("prometheus:", files["compose.yaml"])
                self.assertIn("prom/prometheus:v3.0.1", files["compose.yaml"])
                self.assertIn("grafana:", files["compose.yaml"])
                self.assertIn("grafana/grafana:11.4.0", files["compose.yaml"])
                self.assertIn("prometheus.yml", files)
                self.assertIn('job_name: "app"', files["prometheus.yml"])
                self.assertIn('app:8000', files["prometheus.yml"])

                # OutputManager validation
                manager = OutputManager(repo)
                manager.write(files)
                valid, errors = manager.validate()
                self.assertTrue(valid, f"Validation errors: {errors}")

                # classify_service_role
                self.assertEqual(classify_service_role("prometheus", "prom/prometheus", None, None), "observability")
                self.assertEqual(classify_service_role("grafana", "grafana/grafana", None, None), "observability")

                # Host port overrides
                settings_ports = Settings(target, services_override=("prometheus", "grafana"),
                                          host_ports=(("prometheus", 9095), ("grafana", 3005)), port=8000)
                plan_ports = Planner().build(settings_ports, Inspector().inspect(target))
                pub_ports = {p.service: p for p in plan_ports.published_ports}
                self.assertEqual(pub_ports["prometheus"].host, 9095)
                self.assertEqual(pub_ports["grafana"].host, 3005)

        def test_rc6_wizard_step2_scalability_toggle(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                repo = Path(tmpdir).resolve()
                (repo / "app.py").write_text("print('hello')\n")
                con = Console(Settings(target_for(str(repo))))
                con.render_wizard()
                con.dispatch_input("")  # Move to step 2
                self.assertEqual(con.wizard_step, 2)
                self.assertEqual(con.settings.replicas, 1)

                con.dispatch_input("7")  # 1 -> 2
                self.assertEqual(con.settings.replicas, 2)
                con.dispatch_input("7")  # 2 -> 3
                self.assertEqual(con.settings.replicas, 3)
                con.dispatch_input("7")  # 3 -> 1
                self.assertEqual(con.settings.replicas, 1)

        def test_pass9_replica_port_range_scaling(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                repo = Path(tmpdir).resolve()
                (repo / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
                target = target_for(str(repo / "app.py"))

                # Replicas = 3: Should use port range 8000-8002
                settings = Settings(target, replicas=3, port=8000)
                plan = Planner().build(settings, Inspector().inspect(target))
                files = Renderer().files(plan)
                self.assertIn("127.0.0.1:8000-8002:8000", files["compose.yaml"])

                # Replicas = 1: Should use exact port 8000
                settings_single = Settings(target, replicas=1, port=8000)
                plan_single = Planner().build(settings_single, Inspector().inspect(target))
                files_single = Renderer().files(plan_single)
                self.assertIn("127.0.0.1:8000:8000", files_single["compose.yaml"])
                self.assertNotIn("8000-8000", files_single["compose.yaml"])

        def test_pass9_observability_grafana_password_and_prometheus_self_scrape(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                repo = Path(tmpdir).resolve()
                (repo / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
                target = target_for(str(repo / "app.py"))
                settings = Settings(target, services_override=("prometheus", "grafana"), port=8000)
                plan = Planner().build(settings, Inspector().inspect(target))
                files = Renderer().files(plan)

                # Grafana: verify secure password generated and not hardcoded "admin"
                self.assertNotIn('GF_SECURITY_ADMIN_PASSWORD: "admin"', files["compose.yaml"])
                self.assertIn("GF_SECURITY_ADMIN_PASSWORD:", files["compose.yaml"])

                # Prometheus: verify localhost:9090 self-scrape and app scrape for web
                self.assertIn("prometheus.yml", files)
                self.assertIn('job_name: "prometheus"', files["prometheus.yml"])
                self.assertIn('localhost:9090', files["prometheus.yml"])
                self.assertIn('job_name: "app"', files["prometheus.yml"])
                self.assertIn('app:8000', files["prometheus.yml"])

                # For batch job: verify Prometheus does NOT scrape app to prevent 404 errors
                (repo / "batch.py").write_text("print('running batch')\n")
                target_b = target_for(str(repo / "batch.py"))
                settings_b = Settings(target_b, services_override=("prometheus",), execution_kind="batch")
                plan_b = Planner().build(settings_b, Inspector().inspect(target_b))
                files_b = Renderer().files(plan_b)
                self.assertIn("prometheus.yml", files_b)
                self.assertIn('job_name: "prometheus"', files_b["prometheus.yml"])
                self.assertNotIn('job_name: "app"', files_b["prometheus.yml"])

        def test_pass9_cleaner_docker_missing_fails_exit_code_2(self):
            with patch.object(DockerHostProbe, 'probe', return_value=DockerHostStatus(False, None, False, None, False, None, "No CLI")):
                with patch('sys.stderr', new_callable=io.StringIO) as err:
                    code = main(["clean", "--clean-test", "--yes"])
                    self.assertEqual(code, 2)
                    self.assertIn("Docker CLI not found", err.getvalue())

            with patch.object(DockerHostProbe, 'probe', return_value=DockerHostStatus(True, "/usr/bin/docker", True, "2.29", False, None, "daemon unreachable")):
                with patch('sys.stderr', new_callable=io.StringIO) as err:
                    code = main(["clean", "--clean-test", "--yes"])
                    self.assertEqual(code, 2)
                    self.assertIn("Docker Engine is unreachable", err.getvalue())

        def test_pass9_cleaner_opsforge_project_slug_resolution(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                repo = (Path(tmpdir) / "demo-ml-service").resolve()
                repo.mkdir()
                (repo / "app.py").write_text("print('hello')\n")

                # Test get_project_identifiers resolves folder name and opsforge slug
                ids = ResourceCleaner.get_project_identifiers(repo)
                self.assertIn("demo-ml-service", ids)
                slug = re.sub(r"[^a-z0-9-]", "-", "demo-ml-service".lower()).strip("-")[:24]
                fingerprint = hashlib.sha256(str(repo).encode()).hexdigest()[:7]
                expected_opsforge = f"opsforge-{slug}-{fingerprint}"
                self.assertIn(expected_opsforge, ids)

                # Simulated resources: one container from this project, one external
                r_proj = CleanableResource("container", "c1", f"{expected_opsforge}-app-1", project=expected_opsforge,
                                          is_test=False, is_orphan=False, status="running")
                r_other = CleanableResource("container", "c2", "unrelated-app-1", project="other-project",
                                           is_test=False, is_orphan=False, status="running")
                v_proj = CleanableResource("volume", "v1", f"{expected_opsforge}_data", project=expected_opsforge,
                                          is_test=False, is_orphan=False, status="created")
                v_other = CleanableResource("volume", "v2", "postgres_data", project=None,
                                           is_test=False, is_orphan=False, status="created")

                plan = ResourceCleaner.plan_cleanup(mode="project", target_project=str(repo),
                                                    resources=[r_proj, r_other, v_proj, v_other])
                self.assertEqual(len(plan.containers), 1)
                self.assertEqual(plan.containers[0].id, "c1")
                # Volumes protected by default
                self.assertEqual(len(plan.protected_volumes), 1)
                self.assertEqual(plan.protected_volumes[0].id, "v1")

        def test_pass9_cleaner_orphan_separation_active_test_containers_protected(self):
            # Active running test container must NOT be classified as orphan
            c_running = CleanableResource("container", "c1", "autodocker_live_1234", project="autodocker_live_1234",
                                          is_test=True, is_orphan=False, status="running")
            # Exited test container IS an orphan
            c_exited = CleanableResource("container", "c2", "autodocker_live_5678", project="autodocker_live_5678",
                                        is_test=True, is_orphan=True, status="exited")

            plan = ResourceCleaner.plan_cleanup(mode="orphan", resources=[c_running, c_exited])
            self.assertEqual(len(plan.containers), 1)
            self.assertEqual(plan.containers[0].id, "c2")
            self.assertNotIn("c1", [c.id for c in plan.containers])

        def test_pass9_cleaner_safe_volume_and_network_scans_no_unsafe_splitting(self):
            with patch.object(ResourceCleaner, '_run_docker') as mock_run:
                from subprocess import CompletedProcess
                # Mock ps -a
                mock_run.side_effect = [
                    CompletedProcess([], 0, stdout=''),  # ps -a
                    CompletedProcess([], 0, stdout=''),  # images
                    CompletedProcess([], 0, stdout=json.dumps({"ID": "net1", "Name": "my_network_custom", "Driver": "bridge"})),  # network ls
                    CompletedProcess([], 0, stdout=json.dumps({"Name": "postgres_data", "Driver": "local"})),  # volume ls
                ]
                with patch('shutil.which', return_value='/usr/bin/docker'):
                    resources = ResourceCleaner.scan()
                    net = [r for r in resources if r.kind == "network"][0]
                    vol = [r for r in resources if r.kind == "volume"][0]
                    # Must NOT unsafely assign proj='my_network' or proj='postgres'
                    self.assertIsNone(net.project)
                    self.assertIsNone(vol.project)
                    self.assertFalse(net.is_test)
                    self.assertFalse(vol.is_test)

        def test_pass9_wizard_unmanifested_dependencies_blocking_and_resolution(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                repo = Path(tmpdir).resolve()
                # App with external imports (fastapi, joblib, numpy) but no requirements.txt
                (repo / "app.py").write_text("import fastapi\nimport joblib\nimport numpy\nprint('ml')\n")
                con = Console(Settings(target_for(str(repo / "app.py"))))
                con.render_wizard()

                # Step 1 -> Step 2
                con.dispatch_input("")
                self.assertEqual(con.wizard_step, 2)
                with patch('sys.stdout', new_callable=io.StringIO) as out:
                    con.render_wizard()
                self.assertIn("STRICT (Unmanifested: ", out.getvalue())

                # Step 2 -> Step 3 without resolving: readiness must show RESOLUTION REQUIRED
                con.dispatch_input("")
                self.assertEqual(con.wizard_step, 3)
                with patch('sys.stdout', new_callable=io.StringIO) as out:
                    con.render_wizard()
                self.assertIn("Readiness Status     : RESOLUTION REQUIRED", out.getvalue())

                # Attempting Option 1 (Deploy) must be BLOCKED
                with patch('sys.stdout', new_callable=io.StringIO) as out:
                    con.dispatch_input("1")
                self.assertIn("DEPLOYMENT BLOCKED — UNMANIFESTED DEPENDENCIES DETECTED", out.getvalue())
                self.assertIsNone(con.pending)

                # Return to Step 2 and toggle Option 8 (deps infer)
                con.wizard_step = 2
                con.dispatch_input("8")
                self.assertEqual(con.settings.dependency_mode, "infer")

                # Advance to Step 3 with infer enabled
                con.dispatch_input("")
                self.assertEqual(con.wizard_step, 3)
                with patch('sys.stdout', new_callable=io.StringIO) as out:
                    con.render_wizard()
                self.assertIn("Readiness Status     : PASS", out.getvalue())

                # Deploy Option 1 should now succeed in staging deployment
                con.dispatch_input("1")
                self.assertIsNotNone(con.pending)
                self.assertEqual(con.pending.name, "generate")

        def test_rc7_stdlib_immunity_never_missing(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                repo = Path(tmpdir).resolve()
                code = (
                    "import json\nimport sqlite3\nimport pathlib\nimport logging\n"
                    "import dataclasses\nimport argparse\nimport sys\nimport os\n"
                    "import re\nimport hashlib\nimport math\nimport typing\n"
                    "import asyncio\nprint('pure stdlib app')\n"
                )
                (repo / "main.py").write_text(code)
                target = target_for(str(repo / "main.py"))
                inspection = Inspector().inspect(target)
                report = DependencyAudit().report(inspection)
                # Standard library modules must NEVER be classified as missing
                self.assertEqual(report.possible_missing, ())
                self.assertEqual(report.observed, ())

        def test_rc7_pypi_alias_resolution(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                repo = Path(tmpdir).resolve()
                code = (
                    "import cv2\nfrom PIL import Image\nimport yaml\nfrom dotenv import load_dotenv\n"
                    "from bs4 import BeautifulSoup\nimport psycopg2\nimport fitz\nfrom skimage import io\n"
                )
                (repo / "vision_app.py").write_text(code)
                target = target_for(str(repo / "vision_app.py"))
                inspection = Inspector().inspect(target)
                report = DependencyAudit().report(inspection)

                # Top-level import aliases mapped to canonical PyPI packages
                expected_observed = {
                    "opencv-python", "Pillow", "PyYAML", "python-dotenv",
                    "beautifulsoup4", "psycopg2-binary", "PyMuPDF", "scikit-image"
                }
                self.assertTrue(expected_observed.issubset(set(report.observed)))

                # Now add requirements.txt declaring canonical names
                reqs = (
                    "opencv-python>=4.8\npillow>=10.0\npyyaml>=6.0\npython-dotenv>=1.0\n"
                    "beautifulsoup4>=4.12\npsycopg2-binary>=2.9\npymupdf>=1.23\nscikit-image>=0.22\n"
                )
                (repo / "requirements.txt").write_text(reqs)
                inspection_with_reqs = Inspector().inspect(target)
                report_with_reqs = DependencyAudit().report(inspection_with_reqs)
                # All aliases successfully matched against declared distributions
                self.assertEqual(report_with_reqs.possible_missing, ())

        def test_rc7_multi_manifest_discovery_setup_cfg(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                repo = Path(tmpdir).resolve()
                (repo / "setup.cfg").write_text("[options]\ninstall_requires =\n    fastapi\n    requests\n    pydantic\n")
                (repo / "app.py").write_text("import fastapi\nimport requests\nimport pydantic\n")
                self.assertEqual(_deps(repo), "setup.cfg")

                target = target_for(str(repo / "app.py"))
                inspection = Inspector().inspect(target)
                self.assertEqual(inspection.dependency, "setup.cfg")

                report = DependencyAudit().report(inspection)
                self.assertTrue({"fastapi", "requests", "pydantic"}.issubset(set(report.declared)))
                # uvicorn is added as implied for fastapi
                self.assertIn("fastapi", report.declared)

                # Check Dockerfile generation runs 'pip install --no-cache-dir .'
                plan = Planner().build(Settings(target), inspection)
                files = Renderer().files(plan)
                self.assertIn("pip install --no-cache-dir .", files["Dockerfile"])

        def test_rc7_multi_manifest_discovery_pipfile(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                repo = Path(tmpdir).resolve()
                (repo / "Pipfile").write_text("[packages]\nfastapi = \"*\"\nhttpx = \"*\"\n")
                (repo / "app.py").write_text("import fastapi\nimport httpx\n")
                self.assertEqual(_deps(repo), "Pipfile")

                target = target_for(str(repo / "app.py"))
                inspection = Inspector().inspect(target)
                self.assertEqual(inspection.dependency, "Pipfile")

                report = DependencyAudit().report(inspection)
                self.assertTrue({"fastapi", "httpx"}.issubset(set(report.declared)))

                plan = Planner().build(Settings(target), inspection)
                files = Renderer().files(plan)
                self.assertIn("pip install --no-cache-dir pipenv", files["Dockerfile"])

        def test_rc7_multi_manifest_discovery_environment_yml(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                repo = Path(tmpdir).resolve()
                (repo / "environment.yml").write_text("name: ml_env\ndependencies:\n  - python=3.11\n  - numpy\n  - pandas\n")
                (repo / "app.py").write_text("import numpy\nimport pandas\n")
                self.assertEqual(_deps(repo), "environment.yml")

                target = target_for(str(repo / "app.py"))
                inspection = Inspector().inspect(target)
                self.assertEqual(inspection.dependency, "environment.yml")

                report = DependencyAudit().report(inspection)
                self.assertTrue({"numpy", "pandas"}.issubset(set(report.declared)))

        def test_rc7_expanded_persistent_storage_detection(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                repo = Path(tmpdir).resolve()
                for folder in ("datasets", "storage", "outputs", "logs", "weights", "db"):
                    (repo / folder).mkdir()
                (repo / "app.py").write_text("print('storage test')\n")
                target = target_for(str(repo / "app.py"))
                inspection = Inspector().inspect(target)
                plan = Planner().build(Settings(target), inspection)

                for folder in ("datasets", "storage", "outputs", "logs", "weights", "db"):
                    self.assertIn(folder, plan.persistent_dirs)

                files = Renderer().files(plan)
                compose = files["compose.yaml"]
                self.assertIn("../datasets:/app/datasets", compose)
                self.assertIn("../storage:/app/storage", compose)
                self.assertIn("../outputs:/app/outputs", compose)
                self.assertIn("../logs:/app/logs", compose)
                self.assertIn("../weights:/app/weights", compose)
                self.assertIn("../db:/app/db", compose)

        def test_rc7_wizard_execution_command_customization(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                repo = Path(tmpdir).resolve()
                (repo / "server.py").write_text("print('custom server')\n")
                con = Console(Settings(target_for(str(repo / "server.py"))))
                con.render_wizard()

                # Step 1 -> Step 2
                con.dispatch_input("")
                self.assertEqual(con.wizard_step, 2)

                # Option 9: Execution Command Customization
                con.dispatch_input("9")
                self.assertTrue(con.wizard_awaiting_command)

                # Provide custom command
                con.dispatch_input("python server.py --host 0.0.0.0 --port 9000")
                self.assertFalse(con.wizard_awaiting_command)
                self.assertEqual(con.settings.command_override, ("python", "server.py", "--host", "0.0.0.0", "--port", "9000"))

                plan = con._plan()
                self.assertEqual(plan.command, ("python", "server.py", "--host", "0.0.0.0", "--port", "9000"))

                # Revert to auto
                con.dispatch_input("9")
                con.dispatch_input("auto")
                self.assertIsNone(con.settings.command_override)
                plan_reset = con._plan()
                self.assertEqual(plan_reset.command, ("python", "server.py"))

        def test_rc7_deployment_verification_reliability_differentiates_unverified(self):
            statuses = [{'Service': 'app', 'State': 'running', 'Health': '', 'Name': 'my_app_1'}]
            good, lines = RuntimeHealth.evaluate(['app'], False, statuses)
            self.assertTrue(good)
            # Must explicitly identify unverified readiness
            self.assertIn("readiness unknown / unverified", lines[0])

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Tests)
    outcome = unittest.TextTestRunner(verbosity=2).run(suite)
    if not outcome.wasSuccessful():
        print("\n" + "=" * 80, file=sys.stderr)
        print("REGRESSION SUITE FAILED — DETAILED FAILURE LIST:", file=sys.stderr)
        print("=" * 80, file=sys.stderr)
        for test, trace in outcome.failures:
            print(f"\n[FAILURE] {test.id()}:\n{trace}", file=sys.stderr)
        for test, trace in outcome.errors:
            print(f"\n[ERROR] {test.id()}:\n{trace}", file=sys.stderr)
        print("=" * 80 + "\n", file=sys.stderr)
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            try:
                with open(summary_path, "a", encoding="utf-8") as sf:
                    sf.write(f"### Regression Test Failures on {sys.platform} (Python {sys.version.split()[0]})\n\n")
                    for test, trace in outcome.failures:
                        sf.write(f"#### FAILURE: `{test.id()}`\n```\n{trace}\n```\n")
                    for test, trace in outcome.errors:
                        sf.write(f"#### ERROR: `{test.id()}`\n```\n{trace}\n```\n")
            except Exception:
                pass
    return outcome.wasSuccessful()


def main(argv: list[str] | None = None) -> int:
    compat, compat_msg = check_python_compatibility()
    if not compat:
        print(compat_msg, file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description="Auto Docker: generate Docker infrastructure from one Python script")
    parser.add_argument("action", nargs="?", default="console", choices=(
        "console", "wizard", "quick", "autopilot", "doctor", "existing", "native", "readiness", "preflight", "inspect", "preview", "generate", "check", "ports", "verify", "diagnose", "audit", "dryrun", "build", "up", "run", "job", "down", "ps", "logs", "recover", "unlock", "lock", "contexts", "self-test", "integration-test", "ai-integration-test", "live-test", "smoke", "cleaner", "clean"))
    parser.add_argument("path", nargs="?", help="Project folder or exact Python .py file")
    parser.add_argument("--native-action", choices=('inspect','plan','check','ready','build','up','down','ps','logs','verify'), default='inspect',
                        help='Existing Compose project action for native mode')
    parser.add_argument("--compose-file",
                        help='Choose an existing root-level Compose file when multiple exist')
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON for plan, readiness, or audit")
    parser.add_argument("--mode", choices=MODES, default="auto")
    parser.add_argument("--kind", choices=EXECUTION_KINDS, default="auto", help="Application execution: web, batch, interactive, or auto")
    parser.add_argument("--cmd", help="Override application command; executed without a shell")
    parser.add_argument("--health-path", help="Application-specific HTTP health endpoint, e.g. /health")
    parser.add_argument("--python-image", help="Reviewed Python base-image reference (digest recommended)")
    parser.add_argument("--service-image", action="append", default=[], metavar="SERVICE=IMAGE",
                        help="Reviewed image for mlflow/ollama/redis (digest recommended)")
    parser.add_argument("--entry", help="Relative .py entrypoint within project")
    parser.add_argument("--port", type=int)
    parser.add_argument("--gpu", action="store_true")
    parser.add_argument("--replicas", type=int, default=1,
                        help="Number of application container replicas in compose.yaml deploy.replicas")
    parser.add_argument("--cpus", help="CPU limit for application container (e.g. '1.5', '2')")
    parser.add_argument("--memory", help="Memory limit for application container (e.g. '512M', '2G')")
    parser.add_argument("--services", help=f"Override services: {','.join(SERVICE_ORDER)} | none | auto")
    parser.add_argument("--docker-context", help="Named Docker context for runtime actions (does not change Docker's default)")
    parser.add_argument("--deps", choices=("strict", "infer"), default="strict",
                        help="Explicitly opt into unpinned inferred pip packages when no manifest exists")
    parser.add_argument("--host-port", action="append", default=[], metavar="SERVICE=PORT",
                        help="Override a published host port, e.g. --host-port mlflow=5050")
    parser.add_argument("--yes", action="store_true", help="Explicitly permit generation/build/start without asking")
    parser.add_argument("--allow-insecure", action="store_true", help="Authorize operations on stacks with critical security risks")
    parser.add_argument("--full", action="store_true", help="Show generated content in preview")
    parser.add_argument("--scan", action="store_true", help="Resource cleaner: scan and classify Docker resources (read-only)")
    parser.add_argument("--clean-test", action="store_true", help="Resource cleaner: remove verified temporary test resources")
    parser.add_argument("--clean-project", action="store_true", help="Resource cleaner: stop and remove current project resources")
    parser.add_argument("--recover-orphans", action="store_true", help="Resource cleaner: recover and delete orphaned Auto Docker resources")
    parser.add_argument("--include-volumes", action="store_true", help="Resource cleaner: also delete associated named volumes (default: preserve data)")
    parser.add_argument("--report", help="Save a redacted JSON proof report to an existing folder")
    parser.add_argument("--repeat", type=int, default=1, help="Repeat the disposable P0 proof (1-3)")
    parser.add_argument("--model", help="Existing Ollama model to test during AI integration proof")
    parser.add_argument("--pull-model", action="store_true", help="Explicitly authorize pulling --model in disposable AI proof")
    parser.add_argument("--smoke-path", default="/", help="HTTP path to test for a generated web app")
    parser.add_argument("--expect", help="Optional literal marker expected in the HTTP response body")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json-logs", action="store_true", help="Write structured JSON log events")
    parser.add_argument("--log-file", help="Optional rotating diagnostic log file")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args(argv)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonLogFormatter() if args.json_logs else logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handlers: list[logging.Handler] = [handler]
    if args.log_file:
        try:
            logfile = RotatingFileHandler(args.log_file, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
        except OSError as exc:
            parser.error(f"Cannot open log file: {exc}")
        logfile.setFormatter(JsonLogFormatter())
        handlers.append(logfile)
    log_level = logging.DEBUG if args.verbose else logging.INFO if (args.json_logs or args.log_file) else logging.WARNING
    logging.basicConfig(level=log_level, handlers=handlers, force=True)
    if args.action == "self-test":
        return 0 if self_test() else 1
    if args.action in ("integration-test", "ai-integration-test"):
        if not args.yes:
            print("ERROR: Real Docker tests pull/build images and start temporary containers; pass --yes.", file=sys.stderr)
            return 2
        try:
            path = Path(args.report) if args.report else None
            if args.action == 'integration-test':
                return 0 if integration_test(repeat=args.repeat, report=path) else 1
            return 0 if ai_integration_test(model=args.model, pull=args.pull_model, report=path) else 1
        except (OpsError, OSError, subprocess.TimeoutExpired) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    if args.action == "live-test":
        try:
            return 0 if LiveAcceptanceTest.run(report=Path(args.report) if args.report else None, yes=args.yes, context=args.docker_context) else 1
        except (OpsError, OSError, subprocess.TimeoutExpired) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    if args.action == 'contexts':
        try:
            docker = DockerAdapter(Path.cwd())._docker()
            return subprocess.run([docker, 'context', 'ls'], check=False, timeout=15).returncode
        except (OpsError, OSError, subprocess.TimeoutExpired) as exc:
            print(f'ERROR: {exc}', file=sys.stderr)
            return 2
    if args.action == "doctor":
        # Report failures without forcing users to select a project or open Docker.
        try:
            EnvironmentDoctor().print_report(target_for(args.path) if args.path else None,
                                             DockerAdapter.validate_context(args.docker_context))
        except OpsError as exc:
            print(f'ERROR: {exc}', file=sys.stderr)
            return 2
        return 0
    if args.action in ("cleaner", "clean"):
        probe = DockerHostProbe.probe(context=args.docker_context)
        if not probe.cli_installed:
            print("ERROR: Docker CLI not found on host PATH. Cannot perform Docker cleaner operations.", file=sys.stderr)
            return 2
        if not probe.engine_reachable:
            print(f"ERROR: Docker Engine is unreachable ({probe.error_detail or 'daemon not running'}). Cannot perform Docker cleaner operations.", file=sys.stderr)
            return 2

        docker_bin = shutil.which("docker") or "docker"
        docker_prefix = [docker_bin]
        if args.docker_context:
            docker_prefix.extend(["--context", args.docker_context])
        try:
            if args.scan:
                resources = ResourceCleaner.scan(prefix=docker_prefix)
                if not resources:
                    print("No Docker resources detected.")
                    return 0
                print(f"Discovered {len(resources)} Docker resources:")
                for r in resources:
                    owner = "[TEST]" if r.is_test else (f"[PROJECT: {r.project}]" if r.project else "[EXTERNAL/SHARED]")
                    print(f"  {r.kind:10s} : {r.name:30s} (ID: {r.id[:12]}) {owner} — {r.status}")
                return 0

            target_proj = args.path if args.path else None
            if args.clean_project and not target_proj:
                print("ERROR: --clean-project requires a project path argument (e.g. 'auto_docker.py clean <path> --clean-project').", file=sys.stderr)
                return 2

            mode = "test" if args.clean_test else ("project" if args.clean_project else ("orphan" if args.recover_orphans else ("project" if target_proj else "test")))
            plan = ResourceCleaner.plan_cleanup(
                mode=mode,
                target_project=target_proj,
                preserve_volumes=not args.include_volumes,
                prefix=docker_prefix,
            )
            print(ResourceCleaner.format_plan_preview(plan))
            if plan.total_count == 0:
                print("No matching resources to clean.")
                return 0
            if not args.yes:
                if not sys.stdin.isatty():
                    print("ERROR: Explicit confirmation required for cleaner operations. Pass --yes.", file=sys.stderr)
                    return 2
                confirm = input(f"Proceed with cleanup ({plan.total_count} resources)? Type YES: ").strip()
                if confirm != "YES":
                    print("Cleanup cancelled by operator.")
                    return 0
            res = ResourceCleaner.execute(plan, prefix=docker_prefix)
            print(res.message)
            return 0 if res.success else 1
        except OpsError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    if args.action not in ("console", "wizard", "quick", "autopilot") and args.path is None:
        print("ERROR: Pass the explicit project path or Python file for this CLI action.", file=sys.stderr)
        return 2
    default = args.path
    if default is None:
        # For the interactive console only, suggest the script's sibling project.
        sibling = Path(__file__).resolve().parent
        if sibling != Path.home().resolve() and sibling != Path(sibling.anchor):
            default = str(sibling)
    try:
        port_overrides: list[tuple[str, int]] = []
        for entry in args.host_port:
            name, sep, host = entry.partition('=')
            if not sep or name not in ('app', *SERVICE_ORDER) or not host.isdecimal():
                raise OpsError(f'Expected --host-port SERVICE=PORT (app, {", ".join(SERVICE_ORDER)}).')
            port_overrides.append((name, int(host)))
        context = DockerAdapter.validate_context(args.docker_context)
        service_image_overrides: dict[str, str] = {}
        for item in args.service_image:
            name, sep, image_ref = item.partition('=')
            if not sep or name not in SERVICE_ORDER or not image_ref or name in service_image_overrides:
                raise OpsError(f'Expected unique --service-image NAME=IMAGE for {", ".join(SERVICE_ORDER)}')
            service_image_overrides[name] = image_ref
        if args.replicas < 1 or args.replicas > 64:
            raise OpsError("Replicas must be an integer between 1 and 64.")
        if args.cpus:
            try:
                c_val = float(args.cpus)
                if c_val <= 0 or c_val > 256:
                    raise ValueError
            except ValueError:
                raise OpsError("CPUs limit must be a positive number (e.g. 1.5, 2, 0.5).")
        if args.memory:
            if not re.fullmatch(r"^[0-9]+(\.[0-9]+)?(b|k|m|g|t|kb|mb|gb|tb|ki|mi|gi|ti)?$", args.memory, re.IGNORECASE):
                raise OpsError("Memory limit must be a valid size (e.g. 512M, 2G, 4096M).")
        settings = (Settings(target_for(default), Mode(args.mode), args.entry, args.port, args.gpu,
                             parse_services(args.services) if args.services is not None else None,
                             tuple(port_overrides), args.deps, context, args.kind,
                             tuple(shlex.split(args.cmd)) if args.cmd else None,
                             args.health_path, args.python_image,
                             tuple(sorted(service_image_overrides.items())),
                             replicas=args.replicas, cpus=args.cpus, memory=args.memory) if default is not None else None)
        if args.action == "console":
            Console(settings, allow_insecure=args.allow_insecure).loop()
            return 0
        if args.action in ("wizard", "quick", "autopilot"):
            con = Console(settings, allow_insecure=args.allow_insecure)
            con.render_wizard()
            con.loop()
            return 0
        if args.action == 'readiness':
            if settings is None:
                raise OpsError('Specify a project directory')
            rep = NativeReadiness.report(settings.target.root, args.compose_file, docker_context=settings.docker_context)
            if args.json:
                print(json.dumps([dataclasses.asdict(f) for f in rep.findings], indent=2))
            return 0 if rep.ready else 1
        if args.action in {'existing','native'}:
            if settings is None:
                raise OpsError('Specify a project directory')
            action = 'inspect' if args.action == 'existing' else args.native_action
            orch = DeploymentOrchestrator(settings.target.root)
            ExistingInfrastructure.apply(settings.target.root, action,
                                         choice=args.compose_file, approved=args.yes,
                                         context=settings.docker_context,
                                         json_output=args.json,
                                         allow_insecure=args.allow_insecure,
                                         orchestrator=orch)
            if action == 'verify':
                if orch.state == DeploymentState.DEGRADED:
                    return 1
                return 0
            return 0
        if settings is None:
            raise OpsError("Pass the project path or .py file as the second argument.")
        if (not settings.target.pinned and
                ExistingInfrastructure.discover(settings.target.root).compose_files):
            if args.action == 'verify':
                orch = DeploymentOrchestrator(settings.target.root)
                ExistingInfrastructure.apply(settings.target.root, 'verify',
                                             choice=args.compose_file, approved=args.yes,
                                             context=settings.docker_context,
                                             allow_insecure=args.allow_insecure,
                                             orchestrator=orch)
                if orch.state == DeploymentState.DEGRADED:
                    return 1
                return 0
            if args.action in {'inspect', 'preflight', 'preview'}:
                ExistingInfrastructure.apply(settings.target.root, 'inspect',
                                             allow_insecure=args.allow_insecure)
                if args.action == 'preflight':
                    return 0 if NativeReadiness.report(settings.target.root, args.compose_file, docker_context=settings.docker_context).ready else 1
                return 0
            if args.action == 'generate':
                raise OpsError('Existing Compose stack detected; refusing generic generation. '
                               'Use native --native-action inspect/check/build/up.')
            if args.action == 'check' and not (settings.target.root / OUTPUT).exists():
                ExistingInfrastructure.apply(settings.target.root, 'check',
                                             choice=args.compose_file, context=settings.docker_context,
                                             allow_insecure=args.allow_insecure)
                return 0
        if args.action in {"inspect", "preflight", "preview", "generate", "ports"}:
            plan = Planner().build(settings, Inspector().inspect(settings.target, settings.entry))
            Console._summarize(plan)
            if args.action == "inspect":
                return 0
            if args.action == "preflight":
                DependencyAudit.print_report(DependencyAudit().report(plan.inspection))
                if settings.docker_context:
                    print('Remote context: inspect published ports on the Docker Engine host.')
                else:
                    PortAudit.print_report(plan.published_ports)
                return 0
            if args.action == "ports":
                if settings.docker_context:
                    print('Remote context ports belong to the Docker Engine host:')
                    for port in plan.published_ports:
                        print(f'  {port.service}: 127.0.0.1:{port.host} -> {port.container}')
                    return 0
                return 0 if PortAudit.print_report(plan.published_ports) else 1
            files = Renderer().files(plan)
            if plan.inferred_packages:
                print("UNPINNED inferred packages (explicit opt-in): " + ", ".join(plan.inferred_packages))
            out = OutputManager(settings.target.root)
            created, changed, same = out.state(files)
            print("To create:", ", ".join(created) or "(none)")
            print("To replace:", ", ".join(changed) or "(none)")
            if args.action == "preview":
                if args.full:
                    for name, content in files.items():
                        print(f"\n--- {name} ---\n{content}")
                return 0
            if not created and not changed:
                print("No changes required.")
                return 0
            if not args.yes:
                if not sys.stdin.isatty() or input("Generate files? Type YES: ").strip() != "YES":
                    raise OpsError("Generation cancelled. Pass --yes for intentional noninteractive generation.")
            backup = out.write(files)
            print(f"Generated {out.folder}" + (f"; backup: {backup}" if backup else ""))
            return 0
        if args.action == 'audit':
            return 0 if OfflineDeploymentAudit.print_report(settings.target.root) else 1
        if args.action == 'smoke':
            if settings.docker_context:
                raise OpsError('Remote context localhost belongs to the Docker host, not this terminal; smoke here is unsafe/inaccurate.')
            good, detail = AppSmoke.probe(settings.target.root, args.smoke_path, args.expect)
            print(('PASS: ' if good else 'FAIL: ') + detail)
            return 0 if good else 1
        if args.action == "lock":
            print(ProjectLock(settings.target.root).status())
            return 0
        if args.action in {"unlock", "recover"}:
            if not args.yes:
                if not sys.stdin.isatty() or input(f"Run {args.action}? Type YES: ").strip() != "YES":
                    raise OpsError("Operation cancelled. Pass --yes for intentional noninteractive recovery.")
            if args.action == "unlock":
                print("Removed stale lock: " + ProjectLock(settings.target.root).clear_stale())
            else:
                print("Recovered from: " + str(OutputManager(settings.target.root).restore_last_backup()))
            return 0
        valid, errors = OutputManager(settings.target.root).validate()
        if not valid:
            raise OpsError("Invalid generated configuration: " + "; ".join(errors))
        if args.action in {'verify', 'diagnose'}:
            adapter = DockerAdapter(settings.target.root, settings.docker_context)
            ok, rows = adapter.verify()
            for row in rows:
                print(row)
            if args.action == 'diagnose':
                for hint in adapter.health_hints(rows):
                    print('SUGGESTION: ' + hint)
                return 0 if ok else 1
            if not ok:
                print('One or more containers are missing, unhealthy, or failed. Use diagnose or logs for details.', file=sys.stderr)
                return 1
            if any('no healthcheck' in r for r in rows):
                print('Runtime states: running without explicit healthchecks (application readiness is unverified).')
            else:
                print('Runtime states: verified healthy.')
            return 0
        if args.action == "check":
            print("Generated files: verified")
            probe = DockerHostProbe.probe(context=settings.docker_context)
            if probe.compose_available:
                DockerAdapter(settings.target.root, settings.docker_context).run("check")
                print("Docker Compose: verified")
            else:
                print("Docker Compose unavailable: Compose syntax NOT checked")
            return 0
        if args.action in {"build", "up", "run", "job", "dryrun"}:
            if args.action in {'up', 'run', 'job'}:
                runtime = json.loads((settings.target.root / OUTPUT / 'manifest.json').read_text(encoding='utf-8')).get('runtime', {})
                kind = runtime.get('execution_kind', 'batch' if runtime.get('batch') else 'web')
                if args.action == 'up' and kind == 'interactive':
                    raise OpsError("Interactive application cannot start detached; use run --yes from a terminal.")
                if args.action == 'run' and kind != 'interactive':
                    raise OpsError("Run needs an interactive application. Use --kind interactive at generation.")
                if args.action == 'job' and kind != 'batch':
                    raise OpsError("Job needs a batch application. Use --kind batch at generation.")
            DockerAdapter(settings.target.root, settings.docker_context).preflight()
        if args.action in {"build", "up", "run", "job"} and not args.yes:
            if not sys.stdin.isatty() or input(f"Run Docker {args.action}? Type YES: ").strip() != "YES":
                raise OpsError("Operation cancelled. Pass --yes for noninteractive execution.")
        if args.action == 'up':
            if settings.docker_context:
                print('Remote context: published localhost ports belong to the Docker Engine host.')
            else:
                PortAudit.print_report(OutputManager(settings.target.root).runtime_ports())
        DockerAdapter(settings.target.root, settings.docker_context).run(args.action)
        if args.action == 'up':
            print('Start command completed; use verify to check actual service readiness.')
        return 0
    except (OpsError, PermissionError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
