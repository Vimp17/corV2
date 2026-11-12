"""Retry stored invalidations after a crash or a transient rating failure."""
import logging

from app import db
from app.db import recompute as queue

logger = logging.getLogger(__name__)


def process_pending(well_ids=None, limit=200) -> dict:
    from app.core import rating_builder

    done, failed = [], []
    with db.advisory_lock("rating-recompute") as acquired:
        if acquired:
            for job in queue.pending(well_ids, limit):
                well = job["well_id"]
                try:
                    if db.well_has_data(well):
                        rating_builder.update(well)
                    else:
                        db.forget_well(well)
                except Exception as exc:
                    logger.exception("Pending rating recompute failed well_id=%s", well)
                    queue.finish(job, exc)
                    failed.append(well)
                else:
                    queue.finish(job)
                    done.append(well)
    return {"recomputed": done, "failed": failed}
