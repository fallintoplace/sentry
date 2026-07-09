import enum
import logging
from datetime import UTC, datetime, timedelta

from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, router, transaction
from django.db.models import Max, Q

from sentry import options
from sentry.issues.derived.aggregators import AGGREGATORS
from sentry.issues.derived.framework import Pipeline
from sentry.issues.derived.store import GroupDerivedDataStore
from sentry.issues.models.groupactionlogentry import GroupActionLogEntry
from sentry.issues.models.groupderiveddata import EPOCH, GroupDerivedData
from sentry.models.group import Group
from sentry.utils import metrics

logger = logging.getLogger(__name__)

# Pipeline with current aggregators. Versioned because in principle
# we may want to change it in place and correlate that to existing derived data
# for invalidation purposes.
# TODO: Shouldn't it be versioned by a feature set hash? To be sorted out later.
PIPELINE: Pipeline[GroupActionLogEntry] = Pipeline(AGGREGATORS, version=1)

DEFAULT_BATCH_SIZE = 1000
INLINE_BATCH_SIZE = 100


class ProcessingStrategy(enum.Enum):
    SYNC = "sync"  # process all pending actions now
    ASYNC = "async"  # schedule a task to process all pending actions
    INLINE = "inline"  # try to process all pending actions quickly; fall back to ASYNC


def _ensure_derived(group_id: int) -> GroupDerivedData | None:
    """Get the live GroupDerivedData row for a group, optionally creating one.

    When ``issues.derived-data.create-on-demand`` is enabled, a live row is
    created if none exists. When disabled, returns None — processing will be
    a no-op until a backfill task creates and promotes a row.

    Raises Group.DoesNotExist if the group has been deleted.
    """
    # Fast path: read-only check avoids a write attempt when the row exists
    # or when on-demand creation is disabled.
    try:
        return GroupDerivedData.objects.get(group_id=group_id, is_live=True)
    except GroupDerivedData.DoesNotExist:
        pass

    if not options.get("issues.derived-data.create-on-demand"):
        return None

    try:
        derived, _created = GroupDerivedData.objects.get_or_create(
            group_id=group_id,
            is_live=True,
            defaults={"cursor_date": EPOCH, "cursor_id": 0, "data": {}},
        )
    except IntegrityError:
        raise Group.DoesNotExist(f"Group {group_id} does not exist")
    return derived


def _entries_after_cursor(
    group_id: int, cursor_date: datetime, cursor_id: int, batch_size: int
) -> list[GroupActionLogEntry]:
    return list(
        GroupActionLogEntry.objects.filter(
            Q(group_id=group_id)
            & (Q(date_added__gt=cursor_date) | Q(date_added=cursor_date, id__gt=cursor_id))
        ).order_by("date_added", "id")[:batch_size]
    )


def _process_batch(
    p: Pipeline[GroupActionLogEntry],
    derived: GroupDerivedData,
    batch_size: int,
) -> bool:
    """
    Process up to `batch_size` entries for a group. Updates derived in place.
    Returns True if there are more entries to process.

    Concurrency: multiple callers may process the same row simultaneously.
    Safety relies on two properties:

    1. The action log is append-only and the pipeline is deterministic, so
       any caller processing the same entries produces the same result.
    2. The UPDATE uses a cursor guard scoped to the specific row (by id)
       that only succeeds if no other caller has already advanced the cursor
       past our batch. If it fails (updated == 0), a concurrent caller
       already wrote a superset of our work, so we refresh and check if
       more remains.

    This is an optimistic concurrency scheme — no locks are held, and the
    last-writer-wins semantics are safe because all writers compute the
    same deterministic result for overlapping entry ranges.
    """
    group_id = derived.group_id
    entries = _entries_after_cursor(group_id, derived.cursor_date, derived.cursor_id, batch_size)

    if not entries:
        return False

    result = p.run(entries, state=GroupDerivedDataStore.load(p, derived))

    last = entries[-1]
    last_date = last.date_added
    last_id = last.id
    state_update = GroupDerivedDataStore.build_update(p, result)

    updated = GroupDerivedData.objects.filter(
        Q(id=derived.id)
        & (Q(cursor_date__lt=last_date) | Q(cursor_date=last_date, cursor_id__lte=last_id))
    ).update(cursor_date=last_date, cursor_id=last_id, **state_update)

    if updated:
        # Features updated in this batch (not total; a feature appears at most once per batch)
        for f in result.updated:
            metrics.incr(
                "issues.derived.feature_updated", sample_rate=1.0, tags={"feature": f.name}
            )
        derived.cursor_date = last_date
        derived.cursor_id = last_id
        GroupDerivedDataStore.apply_to_instance(derived, state_update)
        logger.info(
            "issues.derived.processed",
            extra={
                "group_id": group_id,
                "cursor_date": str(last_date),
                "cursor_id": last_id,
                "batch_size": len(entries),
            },
        )
        return len(entries) == batch_size
    else:
        try:
            derived.refresh_from_db()
        except GroupDerivedData.DoesNotExist:
            return False
        logger.info(
            "issues.derived.superseded",
            extra={
                "group_id": group_id,
                "our_cursor_id": last_id,
                "db_cursor_id": derived.cursor_id,
            },
        )
        # A concurrent caller advanced the cursor past us. Check whether
        # there are still entries beyond the refreshed cursor so we don't
        # silently stop processing.
        return bool(_entries_after_cursor(group_id, derived.cursor_date, derived.cursor_id, 1))


def _drain_log(
    derived: GroupDerivedData,
    batch_size: int = DEFAULT_BATCH_SIZE,
    pipeline: Pipeline[GroupActionLogEntry] | None = None,
) -> None:
    """Process all pending log entries into *derived*, batching as needed."""
    p = pipeline or PIPELINE
    while _process_batch(p, derived, batch_size):
        pass


# ---------------------------------------------------------------------------
# Live-row processing (incremental, on event arrival)
# ---------------------------------------------------------------------------


def process_group_log(
    group_id: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
    pipeline: Pipeline[GroupActionLogEntry] | None = None,
) -> GroupDerivedData | None:
    """Fully drain all pending entries for a group's live row.

    Returns None if no live row exists and on-demand creation is disabled.
    Raises Group.DoesNotExist if the group has been deleted.
    """
    with transaction.atomic(using=router.db_for_write(GroupDerivedData)):
        derived = _ensure_derived(group_id)

    if derived is None:
        return None

    _drain_log(derived, batch_size, pipeline)
    return derived


def trigger_group_log_processing(group_id: int, *, strategy: ProcessingStrategy) -> None:
    """Trigger derived data processing for a group.

    Silently returns if the group has been deleted or no live row exists.

    Strategy controls how processing is dispatched:
      SYNC   — process all pending actions now
      ASYNC  — schedule a task to process all pending actions
      INLINE — try to process all pending actions quickly; fall back to ASYNC
    """
    from sentry.issues.derived.tasks import process_group_log_task

    if strategy is ProcessingStrategy.ASYNC:
        process_group_log_task.delay(group_id)
        return

    if strategy is ProcessingStrategy.SYNC:
        try:
            process_group_log(group_id)
        except ObjectDoesNotExist:
            pass
        return

    assert strategy is ProcessingStrategy.INLINE

    with metrics.timer("issues.derived.inline_processing"):
        try:
            with transaction.atomic(using=router.db_for_write(GroupDerivedData)):
                derived = _ensure_derived(group_id)
        except ObjectDoesNotExist:
            return

        if derived is None:
            return

        has_more = _process_batch(PIPELINE, derived, INLINE_BATCH_SIZE)
    if has_more:
        # Derived data will be stale for any code running between now and
        # when the task completes.
        metrics.incr("issues.derived.inline_fallback_to_async")
        process_group_log_task.delay(group_id)


# ---------------------------------------------------------------------------
# Non-live row lifecycle: create, build, promote, cleanup
# ---------------------------------------------------------------------------


def _max_entry_id(group_id: int) -> int:
    return (
        GroupActionLogEntry.objects.filter(group_id=group_id).aggregate(max_id=Max("id"))["max_id"]
        or 0
    )


def create_processing_row(group_id: int) -> GroupDerivedData:
    """Create a new non-live GroupDerivedData row for background processing.

    The version is set to the current max GroupActionLogEntry id for the group,
    capturing the log state at the time processing starts.
    """
    version = _max_entry_id(group_id)
    return GroupDerivedData.objects.create(
        group_id=group_id,
        is_live=False,
        version=version,
        cursor_date=EPOCH,
        cursor_id=0,
        data={},
    )


class PromotionResult(enum.Enum):
    PROMOTED = "promoted"
    CURSOR_BEHIND = "cursor_behind"
    SUPERSEDED = "superseded"
    CANDIDATE_MISSING = "candidate_missing"
    RACE_LOST = "race_lost"


class _PromotionAborted(Exception):
    pass


def promote_to_live(candidate: GroupDerivedData) -> PromotionResult:
    """Atomically promote a non-live row to live, replacing any existing live row.

    On success the old live row is deleted within the same transaction.
    On any failure the transaction rolls back, restoring the old live row.
    """
    try:
        with transaction.atomic(using=router.db_for_write(GroupDerivedData)):
            current_live = GroupDerivedData.objects.filter(
                group_id=candidate.group_id, is_live=True
            ).first()

            if current_live is not None:
                if candidate.version < current_live.version:
                    return PromotionResult.SUPERSEDED
                if (candidate.cursor_date, candidate.cursor_id) < (
                    current_live.cursor_date,
                    current_live.cursor_id,
                ):
                    return PromotionResult.CURSOR_BEHIND
                current_live.delete()

            updated = GroupDerivedData.objects.filter(id=candidate.id).update(is_live=True)
            if not updated:
                raise _PromotionAborted

            candidate.is_live = True
    except _PromotionAborted:
        return PromotionResult.CANDIDATE_MISSING
    except IntegrityError:
        return PromotionResult.RACE_LOST

    return PromotionResult.PROMOTED


MAX_PROMOTION_ATTEMPTS = 5


def build_and_promote_derived_data(
    group_id: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> GroupDerivedData | None:
    """Create a non-live row, drain the full log into it, and promote to live.

    If promotion fails because the live row's cursor is ahead (it received
    incremental updates while we were building), we drain additional entries
    to catch up and retry. This avoids discarding a rebuild that contains
    corrected historical data just because the live row is more current.

    Retries are bounded to avoid starvation if the live row is being updated
    faster than we can catch up. On exhaustion, a rebuild task is re-enqueued
    so the corrections are not permanently lost.

    Returns the promoted row on success, or None if promotion was permanently
    rejected or the group no longer exists.
    """
    from sentry.issues.derived.tasks import rebuild_group_derived_data_task

    try:
        derived = create_processing_row(group_id)
    except IntegrityError:
        return None

    result = PromotionResult.CURSOR_BEHIND
    for attempt in range(MAX_PROMOTION_ATTEMPTS):
        _drain_log(derived, batch_size)

        # Refresh version after draining — new entries may have arrived since
        # the row was created, and the live row's version may have advanced.
        version = _max_entry_id(group_id)
        if version > derived.version:
            GroupDerivedData.objects.filter(id=derived.id).update(version=version)
            derived.version = version

        result = promote_to_live(derived)
        if result is PromotionResult.PROMOTED:
            logger.info(
                "issues.derived.promoted",
                extra={
                    "group_id": group_id,
                    "derived_id": derived.id,
                    "cursor_date": str(derived.cursor_date),
                    "cursor_id": derived.cursor_id,
                    "attempts": attempt + 1,
                },
            )
            return derived

        if result is not PromotionResult.CURSOR_BEHIND:
            break

    derived.delete()

    if result is PromotionResult.CURSOR_BEHIND:
        metrics.incr("issues.derived.promotion_exhausted", sample_rate=1.0)
        logger.warning(
            "issues.derived.promotion_exhausted",
            extra={
                "group_id": group_id,
                "derived_id": derived.id,
                "attempts": MAX_PROMOTION_ATTEMPTS,
            },
        )
        rebuild_group_derived_data_task.delay(group_id)

    logger.info(
        "issues.derived.promotion_rejected",
        extra={
            "group_id": group_id,
            "derived_id": derived.id,
            "result": result.value,
        },
    )

    return None


def cleanup_stale_processing_rows(
    max_age: timedelta = timedelta(days=2),
) -> int:
    """Delete non-live rows older than *max_age* that were never promoted."""
    cutoff = datetime.now(UTC) - max_age
    deleted, _ = GroupDerivedData.objects.filter(
        is_live=False,
        date_added__lt=cutoff,
    ).delete()
    return deleted


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------


def invalidate_group_derived_data(
    group_id: int,
    cursor: tuple[datetime, int] | None = None,
    *,
    hard_delete: bool = True,
) -> None:
    """Invalidate derived state so it is rebuilt.

    *hard_delete* controls the strategy:

    - ``True`` (default): delete the live row immediately and kick off an
      async task to rebuild from scratch. Use this when the existing data is
      known to be wrong and must not be served.
    - ``False``: leave the current live row in place and kick off a background
      build-and-promote. The existing live row continues serving reads until
      the replacement is ready.

    If *cursor* is ``(date_added, id)`` of the earliest affected entry, the
    invalidation only fires when the live row's cursor is at or past that
    point; otherwise the mutation is still ahead of processing and no
    invalidation is needed. *cursor* is only meaningful with
    ``hard_delete=True``.
    """
    from sentry.issues.derived.tasks import rebuild_group_derived_data_task

    if not hard_delete:
        rebuild_group_derived_data_task.delay(group_id)
        return

    if cursor is None:
        GroupDerivedData.objects.filter(group_id=group_id, is_live=True).delete()
        rebuild_group_derived_data_task.delay(group_id)
        return

    # Only invalidate if the row has already processed past the affected point.
    cursor_date, cursor_id = cursor
    deleted, _ = GroupDerivedData.objects.filter(
        Q(group_id=group_id, is_live=True)
        & (Q(cursor_date__gt=cursor_date) | Q(cursor_date=cursor_date, cursor_id__gte=cursor_id)),
    ).delete()
    if deleted:
        logger.info(
            "issues.derived.invalidated",
            extra={
                "group_id": group_id,
                "cursor_date": str(cursor_date),
                "cursor_id": cursor_id,
            },
        )
        rebuild_group_derived_data_task.delay(group_id)
