"""Kinds of data sources the analysis treats differently.

  lab       - laboratory samples (source profiles with "lab_samples", lab protocol documents).
              A value stays current for MAI_RATING_SAMPLING_DAYS, the same window in which the
              sample counts for «Отбор проб»;
  snapshot  - summaries without a measurement time of their own (Реестр коррозии НКТ). Their
              values are used like a sample's, but they are not observations: they do not make
              the data fresh, count towards the minimum of measurements, mark the well as
              operating or enter trends;
  telemetry - everything else (ДАРС, API push): a value stays current MAI_MAX_FORWARD_FILL_DAYS.
A point merged from several sources at one moment ("DARS+KHIMIK") has the kinds of all of them.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def point_sources(point: Any) -> set[str]:
    """Source ids of a telemetry point (or of a normalised row's raw payload)."""
    value = point.get("source_id") if isinstance(point, dict) else getattr(point, "source_id", None)
    return {part for part in str(value or "").split("+") if part}


def lab_source_ids() -> set[str]:
    from app.core.documents.types import lab_source_ids as document_lab_ids
    from app.core.excel_ingestion import load_profiles

    ids = set(document_lab_ids())
    try:
        profiles = load_profiles()
    except Exception:  # a broken profile file must not stop the analysis
        logger.exception("Source profiles could not be loaded for the source kinds")
        profiles = {}
    for profile_id, profile in profiles.items():
        if profile.get("lab_samples"):
            ids.add(str(profile.get("source_id", profile_id)).strip().upper())
    return ids


def snapshot_source_ids() -> set[str]:
    from app.core.documents.nkt_registry import SOURCE_ID

    return {SOURCE_ID}


def is_snapshot(point: Any, snapshot_ids: set[str]) -> bool:
    """Only snapshot sources: a point merged with a real measurement is an observation."""
    sources = point_sources(point)
    return bool(sources) and sources <= snapshot_ids


def is_lab_only(point: Any, lab_ids: set[str]) -> bool:
    sources = point_sources(point)
    return bool(sources) and sources <= lab_ids


def observations(records: list, snapshot_ids: set[str] | None = None) -> list:
    """The points that are real measurements (snapshots left out)."""
    snapshot_ids = snapshot_source_ids() if snapshot_ids is None else snapshot_ids
    return [point for point in records if not is_snapshot(point, snapshot_ids)]
