"""Project index — maps hub-level project IDs to (worker_id, worker_project_id).

Hub assigns globally unique IDs by offsetting:
  hub_id = worker_index * 1_000_000 + worker_project_id

This is transparent to the frontend — it sees hub_ids everywhere.
When routing to a worker, hub resolves back to the original worker-side id.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ProjectMapping:
    worker_id: str
    worker_label: str
    worker_project_id: int
    data: dict[str, Any]


# Each worker gets a range of 1M IDs. Supports up to 1000 workers.
_WORKER_ID_MULTIPLIER = 1_000_000


class ProjectIndex:
    """In-memory bidirectional project ID mapping."""

    def __init__(self) -> None:
        self._mappings: dict[int, ProjectMapping] = {}
        # worker_id → assigned offset (0, 1, 2, ...)
        self._worker_offsets: dict[str, int] = {}
        self._next_offset = 0

    def _offset_for(self, worker_id: str) -> int:
        if worker_id not in self._worker_offsets:
            self._worker_offsets[worker_id] = self._next_offset
            self._next_offset += 1
        return self._worker_offsets[worker_id]

    def to_hub_id(self, worker_id: str, worker_project_id: int) -> int:
        offset = self._offset_for(worker_id)
        return offset * _WORKER_ID_MULTIPLIER + worker_project_id

    def update_from_worker(
        self,
        worker_id: str,
        worker_label: str,
        projects: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Replace all projects for a worker. Returns projects with remapped hub IDs."""
        # Clear old entries for this worker
        self._mappings = {
            k: v for k, v in self._mappings.items() if v.worker_id != worker_id
        }

        remapped: list[dict[str, Any]] = []
        for p in projects:
            worker_pid = p["id"]
            hub_id = self.to_hub_id(worker_id, worker_pid)
            project_data = {
                **p,
                "id": hub_id,
                "worker_id": worker_id,
                "worker_label": worker_label,
            }
            self._mappings[hub_id] = ProjectMapping(
                worker_id=worker_id,
                worker_label=worker_label,
                worker_project_id=worker_pid,
                data=project_data,
            )
            remapped.append(project_data)

        return remapped

    def resolve(self, hub_id: int) -> tuple[str, int] | None:
        """Resolve hub_id to (worker_id, worker_project_id). None if not found."""
        mapping = self._mappings.get(hub_id)
        if mapping is None:
            return None
        return mapping.worker_id, mapping.worker_project_id

    def get_all(self) -> list[dict[str, Any]]:
        """All projects, sorted: available first, then by activity."""
        projects = [m.data for m in self._mappings.values()]
        projects.sort(
            key=lambda p: (
                not p.get("available", True),
                -(p.get("last_session_mtime") or 0),
            )
        )
        return projects
