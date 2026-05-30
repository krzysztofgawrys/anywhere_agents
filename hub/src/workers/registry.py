"""Worker registry - reads worker list from a JSON config file.

Config format (array of objects):
[
  {
    "id": "local-claude",
    "type": "claude",
    "label": "Laptop (Claude)",
    "url": "ws://worker-claude:8002/ws",
    "secret": "changeme"
  },
  {
    "id": "vpc-copilot",
    "type": "copilot",
    "label": "VPC Copilot",
    "mode": "inbound",
    "secret": "another-secret"
  }
]

`type` is optional and free-form (string); default "claude" for backward
compat with pre-multi-agent configs. Frontend uses it for a per-project
badge and to suffix the worker filter dropdown label.

`mode` is optional and one of:
  - "outbound" (default): hub dials the worker at `url`. Classic mode.
    Requires `url` and `secret`. The worker must run a WS server
    reachable from the hub (LAN, VPN, Tailscale, etc.).
  - "inbound": the worker dials the hub. `url` is ignored; the hub waits
    for the worker to register itself by id over the
    /worker-register endpoint. Useful when the worker sits in a closed
    network (VPC, behind NAT, corporate firewall) and only egress to the
    hub is available. `secret` still required (validated on the worker's
    inbound handshake).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog

logger = structlog.get_logger()

WORKERS_CONFIG_PATH = os.getenv(
    "WORKERS_CONFIG", "/config/workers.json"
)

WorkerMode = Literal["outbound", "inbound"]


@dataclass(frozen=True)
class WorkerInfo:
    id: str
    label: str
    url: str
    secret: str
    # Agent SDK family this worker runs. Free-form string so future SDKs can
    # be added without touching the hub. Frontend uses it cosmetically only.
    type: str = "claude"
    # Connection direction. "outbound" = hub dials worker (default, classic).
    # "inbound" = worker dials hub (reverse mode, for closed VPCs / NATed
    # workers). See module docstring.
    mode: WorkerMode = "outbound"


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
        if not worker_id:
            logger.warning("workers_config_skip_entry", entry=entry, reason="missing id")
            continue

        raw_mode = entry.get("mode", "outbound")
        mode: WorkerMode = "inbound" if raw_mode == "inbound" else "outbound"

        # outbound mode requires a URL the hub can dial. inbound mode has no
        # URL (worker dials in) so we synthesize a placeholder for logging.
        url = entry.get("url", "")
        if mode == "outbound" and not url:
            logger.warning("workers_config_skip_entry", entry=entry, reason="missing url for outbound mode")
            continue
        if mode == "inbound":
            url = url or f"inbound://{worker_id}"

        worker_type = entry.get("type", "claude")
        if not isinstance(worker_type, str) or not worker_type:
            worker_type = "claude"
        workers.append(WorkerInfo(
            id=worker_id,
            label=entry.get("label", worker_id),
            url=url,
            secret=entry.get("secret", ""),
            type=worker_type,
            mode=mode,
        ))

    logger.info(
        "workers_loaded",
        count=len(workers),
        workers=[{"id": w.id, "mode": w.mode} for w in workers],
    )
    return workers
