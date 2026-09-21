"""Filesystem locations shared by main.py, the dashboard and the services.

Everything is relative to the working directory (``/app`` in Docker), matching
the bind mounts in docker-compose.yml (``./LOGS:/app/LOGS``, ``./data:/app/data``).
"""

from pathlib import Path

LOG_DIR = Path("LOGS")
LOG_FILE = LOG_DIR / "vmc.log"
DATA_DIR = Path("data")
