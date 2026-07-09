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

    See ``DERIVED_DATA.md`` in the repository root for the full lifecycle,
    versioning, and promotion protocol.
    """

    __relocation_scope__ = RelocationScope.Excluded

    group = FlexibleForeignKey("sentry.Group")
    is_live = models.BooleanField(default=False)

    # Monotonically increasing value reflecting how complete this row's view
    # of the action log is. A higher version means the build observed a more
    # complete log. Used in promotion to ensure a build that missed log
    # mutations cannot replace one that saw them. Currently populated from
    # max(GroupActionLogEntry.id) for the group at processing start time.
    version = BoundedBigIntegerField(default=0)

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

    __repr__ = sane_repr("group_id", "is_live", "version", "cursor_date", "cursor_id")
