"""Template for a source with a non-standard API or input format.

Register a packaged adapter in the ``mai_corrosion.sources`` Python entry-point
group, then set ``"adapter": "your-entry-point-name"`` on its source profile.
The adapter may return .xlsx bytes or canonical JSON as described in README.md.
"""
from __future__ import annotations

from typing import Any


class ExampleSourceAdapter:
    def fetch_for_well(self, well_id: str, profile: dict[str, Any]) -> bytes | dict[str, Any]:
        """Fetch a source response and return bytes or canonical rows.

        Keep credentials in environment variables or a secret manager. Apply
        request timeouts, validate the upstream response, and never log tokens.
        For telemetry, return {"records": [{"timestamp": "...", ...}]}.
        For work history, return {"work_history": [...], "failure_history": [...]}.
        For crew availability, return {"crew_availability": [...]}.
        """
        raise NotImplementedError("Implement the API contract for your source")
