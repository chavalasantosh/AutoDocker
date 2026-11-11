# Auto Docker (Rust Core Engine)

High-performance, memory-safe, compiled Rust implementation of the **Auto Docker** container infrastructure generator and deployment orchestrator.

Auto Docker now exists in both **Python** (`auto_docker.py`) and **Rust** (`rust/`), sharing identical inspection heuristics, storage budgeting models, and configuration rendering contracts.

---

## Performance Comparison: Python vs. Rust

Benchmarked against an enterprise AutoML script (445 KB, 43 imports, complex conditional and guarded branches):

| Implementation | Average Latency | Speedup | Memory Safety | Binary Distribution |
| :--- | :---: | :---: | :---: | :---: |
| **Python 3.13 (`auto_docker.py`)** | ~2,107 ms | Baseline | Dynamic | Source / `pip install` |
| **Rust (`autodocker-rs`)** | **85 ms** | **24.7x faster** | Compile-time guaranteed | Standalone native binary |

---

## Architecture Overview

The Rust codebase is organized into modular, decoupled components:

- **`src/models.rs`**: Core domain types (`Mode`, `ExecutionKind`, `StorageVerdict`, `Inspection`, `Plan`, `PublishedPort`, `ResourceBudget`).
- **`src/ast_visitor.rs`**: Fast lexical Python import analyzer; accurately handles `if __name__ == '__main__':` main guards, `if TYPE_CHECKING:` typing exclusions, unconditional branches, and guarded `try/except` feature flags.
- **`src/storage.rs`**: Non-destructive Docker daemon storage prober with non-Linux VM / WSL2 isolation handling.
- **`src/budget.rs`**: Resource budgeting estimator and heavy package footprint calculator (PyTorch, TensorFlow, CatBoost, LightGBM, XGBoost, etc.).
- **`src/inspector.rs`**: Project scanner, heuristic entrypoint scoring, framework inference (FastAPI, Flask, Streamlit, Gradio).
- **`src/planner.rs`**: Deployment plan synthesizer and container contract evaluator.
- **`src/renderer.rs`**: Generation of production-grade `Dockerfile`, `compose.yaml`, `manifest.json`, and `INSTRUCTIONS.txt`.
- **`src/docker.rs`**: Docker adapter, Compose runner, and execution safety barrier.
- **`src/output.rs`**: Atomic writer, validation, timestamped rollback backups, and non-destructive existing Compose preservation.
- **`src/audit.rs`**: Loopback TCP port auditor and offline dependency auditor.
- **`src/main.rs`**: Command-line interface and built-in self-test suite.

---

## Building and Running

### Prerequisites
- Rust 1.80+ or latest stable (installed via `rustup`)

### Compile
```bash
cd rust
cargo build --release
```
The compiled binary will be located at `rust/target/release/autodocker` (or `autodocker.exe` on Windows).

### Usage
```bash
# Inspect any project
./autodocker inspect /path/to/project

# Run preflight safety check (audits ports, dependencies, and storage)
./autodocker preflight /path/to/project

# Preview generated container configuration
./autodocker preview /path/to/project

# Generate Docker infrastructure into .opsforge/
./autodocker generate /path/to/project

# Run built-in self-test suite
./autodocker self-test
```

### Run Tests
```bash
cd rust
cargo test
```
