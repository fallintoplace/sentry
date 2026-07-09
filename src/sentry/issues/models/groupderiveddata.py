from __future__ import annotations

from datetime import UTC, datetime

from django.db import models

from sentry.backup.scopes import RelocationScope
from sentry.db.models import (
    BoundedPositiveIntegerField,
    FlexibleForeignKey,
    cell_silo_model,
    sane_repr,
)
from sentry.db.models.base import DefaultFieldsModel
from sentry.db.models.fields.bounded import BoundedBigIntegerField

# Sentinel for "no entries processed yet". Used as the initial cursor_date
# so that any real date_added compares greater.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@cell_silo_model
class GroupDerivedData(DefaultFieldsModel):
    """
    Materialized state derived from GroupActionLogEntry entries.

    Multiple rows may exist per group, but at most one may be ``is_live=True``
    at any time (enforced by a partial unique constraint). Only the live row is
    considered canonical; non-live rows are transient build artifacts.

    Lifecycle
    ---------

    **On-demand creation (development / early rollout)**

        When the ``issues.derived-data.create-on-demand`` option is enabled,
        the first call to process derived data for a group creates a live row
        and incrementally applies log entries as they arrive. This is the
        simplest path and useful when the action log is known to be complete.

    **Backfill-then-activate (production rollout)**

        When the option is disabled, processing is a no-op for groups that have
        no live row yet. Instead, a background task:

        1. Backfills historical Activity records into the action log.
        2. Creates a new *non-live* GroupDerivedData row (``is_live=False``).
        3. Drains the entire action log for that group into the new row.
        4. Atomically promotes the row to ``is_live=True``, replacing any
           existing live row.

        This ensures derived data is only visible once it reflects the full
        history.

    **Re-derivation after log mutations**

        When the action log is mutated (entries inserted, corrected, or
        reordered), the existing live row may be stale. Two strategies:

        - *Hard delete*: delete the live row and rebuild from scratch. Use
          this when the existing data is known to be wrong.
        - *Soft replacement*: leave the current live row in place, build a
          new non-live row from scratch, and promote it once caught up. This
          avoids a window where no derived data is available.

    Versioning and promotion safety
    -------------------------------

    The auto-increment ``id`` serves as a coarse version: a row created later
    always has a higher id. ``promote_to_live`` enforces two invariants:

    1. A candidate's id must be greater than the current live row's id, so an
       older build cannot replace a newer one.
    2. A candidate's cursor must be at or ahead of the current live row's
       cursor, so promotion never regresses history coverage.

    If two background builds race, the one with the lower id loses. The loser
    is cleaned up by the caller or by periodic stale-row cleanup.
    """

    __relocation_scope__ = RelocationScope.Excluded

    group = FlexibleForeignKey("sentry.Group")
    is_live = models.BooleanField(default=False)
    cursor_date = models.DateTimeField(default=EPOCH)
    cursor_id = BoundedBigIntegerField(default=0)

    # Open-ended JSON object for storing derived features that don't need their own column.
    # Data in here should be kept small; we typically have to read and write the full blob.
    # If it changes frequently, needs to be indexed, or needs convenient joins, consider a column.
    data = models.JSONField(default=dict)

    # Column-backed features — promoted from JSON for indexing/querying.

    view_count = BoundedPositiveIntegerField(default=0)
    # Stores the current Progress value as a string.
    progress = models.CharField(max_length=32, null=True, default="identified")

    # The last time the above column was changed.
    last_progressed_at = models.DateTimeField(null=True, default=None)

    class Meta:
        app_label = "sentry"
        db_table = "sentry_groupderiveddata"
        constraints = [
            models.UniqueConstraint(
                fields=["group"],
                condition=models.Q(is_live=True),
                name="uniq_live_gdd_per_group",
            ),
        ]
        indexes = [
            # Only live rows participate in joins/filters on these columns.
            models.Index(
                fields=["progress", "group"],
                condition=models.Q(is_live=True),
                name="sentry_gdd_progress_live",
            ),
            models.Index(
                fields=["last_progressed_at", "group"],
                condition=models.Q(is_live=True),
                name="sentry_gdd_lastprog_live",
            ),
            models.Index(fields=["group", "is_live"]),
            models.Index(
                fields=["date_added"],
                condition=models.Q(is_live=False),
                name="sentry_gdd_stale_cleanup",
            ),
        ]

    __repr__ = sane_repr("group_id", "is_live", "cursor_date", "cursor_id")
