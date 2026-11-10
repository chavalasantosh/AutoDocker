# Auto Docker — New Teammate Quickstart & Guide

> **Zero Docker knowledge required.**  
> If you know how to write Python code, you can run and deploy everything on this team in under 60 seconds.

---

## 1. Welcome to the Team! 👋

On this team, we use **Auto Docker** to package and run our applications and machine learning workloads.

### Why do we use it?
- **No environment headaches**: You never have to spend hours debugging pip version conflicts or CUDA driver mismatches.
- **"It works on my machine" is solved**: What runs on your laptop runs identically on teammates' laptops and in production.
- **You do NOT need to learn Docker**: No Dockerfiles to write, no complex YAML configurations to learn, and no Docker commands to memorize.

---

## 2. One-Time Setup (2 Minutes) ⏱️

Before running your first project, ensure you have **Python** and **Docker Desktop**:

### Windows
1. Download and install **[Docker Desktop for Windows](https://www.docker.com/products/docker-desktop/)**.
2. Keep the recommended option **"Use WSL 2 instead of Hyper-V"** checked during installation.
3. Launch **Docker Desktop** from your Start Menu. Once the Docker whale icon in your taskbar is steady, you're ready!

### macOS (Apple Silicon or Intel)
1. Download and install **[Docker Desktop for Mac](https://www.docker.com/products/docker-desktop/)**.
2. Drag Docker to your **Applications** folder.
3. Open Docker from Applications to complete setup.

### Linux (Ubuntu / Debian / RHEL)
Run the one-line official installer in your terminal:
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
sudo systemctl start docker
```

---

## 3. How to Run Any Project (1 Simple Command) 🚀

Whenever you want to run your code, open your terminal in your project directory and type:

```bash
python auto_docker.py
```

Then press **`[ENTER]`**!

### What happens behind the scenes?
1. **Auto-Detects Your Code**: Finds your entrypoint (e.g. `app.py`, `main.py`, or `server.py`).
2. **Audits Dependencies**: Inspects `requirements.txt` or `pyproject.toml` and includes any missing libraries.
3. **Resolves Network Ports**: Automatically sets up safe local loopback ports (e.g. `http://localhost:8000`). If port 8000 is occupied by another program, Auto Docker automatically relocates to `8001` with zero conflict.
4. **Launches & Verifies**: Starts your application inside a clean container and prints your web address.

```text
================================================================================
AUTO DOCKER — SMART: Project Understanding
================================================================================
Project Name     : incident-prediction-service
Language         : Python / FastAPI application [CONFIRMED]
Entrypoint       : app.py [CONFIRMED]
Dependencies     : Manifest verified: requirements.txt [CONFIRMED]
Application Port : Port 8000 -> 8000 (available) [CONFIRMED]
Persistent Data  : Data folders detected: artifacts, models [CONFIRMED]

Next Steps:
  Press [ENTER] or '1' to Prepare Deployment Plan
================================================================================
```

---

## 4. Passing a Project Folder Directly 📂

You can also point Auto Docker directly to any script or project folder:

```bash
# Point directly to a Python script
python auto_docker.py my_app.py

# Point directly to a project directory
python auto_docker.py ../other-project/
```

Auto Docker automatically recognizes the path and launches SMART Mode for that project!

---

## 5. Day-to-Day Workflow 💻

Here is the daily cycle for our team:

```
┌──────────────────┐       ┌──────────────────────┐       ┌──────────────────────┐
│  Write/Edit Code │ ────> │ python auto_docker.py│ ────> │ View Live Web App    │
│  in VS Code      │       │ (press [ENTER])      │       │ http://localhost:8000│
└──────────────────┘       └──────────────────────┘       └──────────────────────┘
```

1. Edit your Python files normally in your favorite code editor (VS Code, Cursor, PyCharm).
2. Run `python auto_docker.py` and press `[ENTER]`.
3. Test your web endpoints, API, or model predictions in your browser.
4. When finished, shut down cleanly:
   ```bash
   python auto_docker.py down
   ```

---

## 6. Where Are My Files and Models Stored? 💾

**Q: If the container stops, do I lose my work?**  
**A: NO! Your files are 100% safe.**

Auto Docker automatically links your project's data directories:
- `artifacts/` (evaluation graphs, training outputs)
- `models/` (saved `.pkl`, `.pt`, `.onnx` model weights)
- `data/` or `datasets/` (input datasets and tables)
- `logs/` (runtime execution logs)

Any file written by your code inside the container appears immediately in your local folder on your computer in real-time. Stopping or deleting containers will **never delete your saved models or files**.

---

## 7. Interactive 60-Second Terminal Guide 📚

Want an interactive refresher right in your terminal? Run:

```bash
python auto_docker.py guide
```

or choose Option `[2]` from the SMART Mode home menu. Use `n` (Next) and `p` (Previous) to flip through the 4 interactive cards.

---

## 8. Common Beginner FAQs ❓

### Q1: It says "Docker is not running yet". What do I do?
Just launch **Docker Desktop** from your Start menu or Applications. You don't even need to restart your terminal. Run `python auto_docker.py` again, and you're good to go!

### Q2: How do I stop all running containers?
Run:
```bash
python auto_docker.py down
```
Auto Docker safely stops all containers for your project and cleans up all temporary resources.

### Q3: How do I view logs if my code crashes?
Run:
```bash
python auto_docker.py logs
```
Auto Docker displays real-time container output and pinpoints the exact line number of any Python exception.

---

*Need help or want to customize advanced flags? Run `python auto_docker.py console` or ask any teammate on the team channel!*

