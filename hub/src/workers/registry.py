"""Worker registry - reads worker list from a JSON config file.

Config format (array of objects):
[
  {
    "id": "local",
    "label": "Laptop",
    "url": "ws://worker-claude:8002/ws",
    "secret": "changeme"
  },
  {
    "id": "pc1",
    "label": "Desktop PC",
    "url": "ws://192.168.1.50:8002/ws",
    "secret": "other-secret"
  }
]
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger()

WORKERS_CONFIG_PATH = os.getenv(
    "WORKERS_CONFIG", "/config/workers.json"
)


@dataclass(frozen=True)
class WorkerInfo:
    id: str
    label: str
    url: str
    secret: str


def load_workers(config_path: str = WORKERS_CONFIG_PATH) -> list[WorkerInfo]:
    """Load worker definitions from JSON config file."""
    path = Path(config_path)
    if not path.exists():
        logger.warning("workers_config_missing", path=config_path)
        return []

    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.error("workers_config_invalid", path=config_path, error=str(e))
        return []

    if not isinstance(data, list):
        logger.error("workers_config_not_array", path=config_path)
        return []

    workers: list[WorkerInfo] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        worker_id = entry.get("id", "")
        url = entry.get("url", "")
        if not worker_id or not url:
            logger.warning("workers_config_skip_entry", entry=entry)
            continue
        workers.append(WorkerInfo(
            id=worker_id,
            label=entry.get("label", worker_id),
            url=url,
            secret=entry.get("secret", ""),
        ))

    logger.info("workers_loaded", count=len(workers), workers=[w.id for w in workers])
    return workers
