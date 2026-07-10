from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from django.db import router, transaction

from sentry.hybridcloud.models.outbox import CellOutbox
from sentry.hybridcloud.outbox.category import OutboxCategory
from sentry.issues.action_log.publish import publish_action
from sentry.issues.action_log.types import (
    SYSTEM_ACTOR,
    ActionSource,
    GroupAction,
    GroupActionActor,
    GroupActionType,
    GroupActorType,
    ResolveAction,
    ResolvedInPullRequestAction,
    UnresolveAction,
    ViewAction,
)
from sentry.issues.derived import processing
from sentry.issues.derived.aggregators import AGGREGATORS
from sentry.issues.derived.features import (
    LAST_PROGRESSED_AT,
    PROGRESS,
    STATUS,
    VIEW_COUNT,
    IssueStatus,
)
from sentry.issues.derived.framework import (
    AggregatorResult,
    DateTimeCodec,
    EnumCodec,
    Feature,
    OptionalCodec,
    Pipeline,
    State,
    StateUpdate,
    StateView,
    aggregator,
)
from sentry.issues.derived.processing import (
    PIPELINE,
    PromotionResult,
    build_and_promote_derived_data,
    cleanup_stale_processing_rows,
    create_processing_row,
    invalidate_group_derived_data,
    process_group_log,
    promote_to_live,
)
from sentry.issues.derived.store import GroupDerivedDataStore
from sentry.issues.models.groupactionlogentry import GroupActionLogEntry
from sentry.issues.models.groupderiveddata import GroupDerivedData
from sentry.issues.progress_state import IssueProgressState
from sentry.models.group import Group
from sentry.testutils.cases import TestCase
from sentry.testutils.helpers.features import with_feature
from sentry.testutils.outbox import outbox_runner
from sentry.utils import json

SOURCE = ActionSource.API


def _publish(*, group: Group, action: GroupAction, actor: GroupActionActor = SYSTEM_ACTOR) -> None:
    with outbox_runner():
        publish_action(
            action,
            source=SOURCE,
            group_id=group.id,
            project=group.project,
            actor=actor,
        )


@with_feature("projects:issue-action-log-write-to-db")
class ProcessGroupLogTest(TestCase):
    def setUp(self) -> None:
        super().setUp()
        # Enable mutation checking so aggregators that modify state in place fail.
        self._original_pipeline = processing.PIPELINE
        processing.PIPELINE = Pipeline(
            AGGREGATORS, version=processing.PIPELINE.version, check_mutations=True
        )

    def tearDown(self) -> None:
        processing.PIPELINE = self._original_pipeline
        super().tearDown()

    def test_records_and_processes(self) -> None:
        group = self.create_group()
        user = self.user

        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(user.id))
        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(user.id))
        _publish(group=group, action=ResolveAction(), actor=GroupActionActor.user(user.id))

        entries = list(GroupActionLogEntry.objects.filter(group_id=group.id).order_by("id"))
        assert len(entries) == 3
        assert entries[0].type == GroupActionType.VIEW
        assert entries[0].actor_type == GroupActorType.USER
        assert entries[0].actor_id == user.id

        derived = process_group_log(group.id)
        assert derived is not None
        assert derived.cursor_id == entries[-1].id
        assert isinstance(derived.data, dict)
        assert derived.is_live

    def test_incremental_processing(self) -> None:
        group = self.create_group()
        user = self.user

        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(user.id))
        derived = process_group_log(group.id)
        assert derived is not None
        first_cursor = derived.cursor_id

        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(user.id))
        derived = process_group_log(group.id)
        assert derived is not None
        assert derived.cursor_id > first_cursor

    def test_noop_when_no_new_entries(self) -> None:
        group = self.create_group()
        user = self.user

        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(user.id))
        derived = process_group_log(group.id)
        assert derived is not None
        old_updated = derived.date_updated

        derived = process_group_log(group.id)
        assert derived is not None
        assert derived.date_updated == old_updated

    def test_process_group_log_only_affects_target(self) -> None:
        group_a = self.create_group()
        group_b = self.create_group()
        user = self.user

        _publish(group=group_a, action=ViewAction(), actor=GroupActionActor.user(user.id))
        _publish(group=group_b, action=ViewAction(), actor=GroupActionActor.user(user.id))

        cursor_b = GroupDerivedData.objects.get(group_id=group_b.id, is_live=True).cursor_id

        GroupActionLogEntry.objects.create(
            group_id=group_a.id,
            project_id=group_a.project_id,
            type=0,
            actor_type=GroupActorType.SYSTEM,
            actor_id=0,
            source=SOURCE,
            data={},
        )
        process_group_log(group_a.id)

        assert GroupDerivedData.objects.get(group_id=group_b.id, is_live=True).cursor_id == cursor_b

    def test_batched_processing(self) -> None:
        group = self.create_group()
        user = self.user

        for _ in range(5):
            _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(user.id))

        # Process in batches of 2 — should take 3 batches (2+2+1)
        derived = process_group_log(group.id, batch_size=2)
        assert derived is not None

        entries = list(GroupActionLogEntry.objects.filter(group_id=group.id).order_by("id"))
        assert derived.cursor_id == entries[-1].id
        assert len(entries) == 5

    def test_system_action_no_user(self) -> None:
        group = self.create_group()

        _publish(group=group, action=ResolveAction())

        entry = GroupActionLogEntry.objects.get(group_id=group.id)
        assert entry.actor_type == GroupActorType.SYSTEM
        assert entry.actor_id == 0

    def test_status_starts_open(self) -> None:
        group = self.create_group()

        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(self.user.id))
        derived = process_group_log(group.id)
        assert derived is not None
        state = GroupDerivedDataStore.load(PIPELINE, derived)
        assert state[STATUS] == IssueStatus.OPEN

    def test_resolve_closes(self) -> None:
        group = self.create_group()
        user = self.user

        _publish(group=group, action=ResolveAction(), actor=GroupActionActor.user(user.id))
        derived = process_group_log(group.id)
        assert derived is not None
        assert derived.data["status"] == "closed"

    def test_unresolve_reopens(self) -> None:
        group = self.create_group()
        user = self.user

        _publish(group=group, action=ResolveAction(), actor=GroupActionActor.user(user.id))
        _publish(group=group, action=UnresolveAction(), actor=GroupActionActor.user(user.id))
        derived = process_group_log(group.id)
        assert derived is not None
        assert derived.data["status"] == "open"

    def test_duplicate_resolve_ignored(self) -> None:
        group = self.create_group()
        user = self.user

        _publish(group=group, action=ResolveAction(), actor=GroupActionActor.user(user.id))
        _publish(group=group, action=ResolveAction(), actor=GroupActionActor.user(user.id))
        derived = process_group_log(group.id)
        assert derived is not None
        assert derived.data["status"] == "closed"

    def test_duplicate_unresolve_ignored(self) -> None:
        group = self.create_group()
        user = self.user

        _publish(group=group, action=UnresolveAction(), actor=GroupActionActor.user(user.id))
        derived = process_group_log(group.id)
        assert derived is not None
        state = GroupDerivedDataStore.load(PIPELINE, derived)
        assert state[STATUS] == IssueStatus.OPEN

    def test_status_toggle(self) -> None:
        group = self.create_group()
        user = self.user

        _publish(group=group, action=ResolveAction(), actor=GroupActionActor.user(user.id))
        _publish(group=group, action=UnresolveAction(), actor=GroupActionActor.user(user.id))
        _publish(group=group, action=ResolveAction(), actor=GroupActionActor.user(user.id))
        derived = process_group_log(group.id)
        assert derived is not None
        assert derived.data["status"] == "closed"

    # --- invalidation ---

    def test_invalidate_deletes_live_row(self) -> None:
        group = self.create_group()
        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(self.user.id))
        process_group_log(group.id)
        assert GroupDerivedData.objects.filter(group_id=group.id, is_live=True).exists()

        invalidate_group_derived_data(group.id)
        assert not GroupDerivedData.objects.filter(group_id=group.id, is_live=True).exists()

    def test_invalidate_with_cursor_deletes_if_past(self) -> None:
        group = self.create_group()
        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(self.user.id))
        derived = process_group_log(group.id)
        assert derived is not None

        invalidate_group_derived_data(group.id, cursor=(derived.cursor_date, derived.cursor_id))
        assert not GroupDerivedData.objects.filter(group_id=group.id, is_live=True).exists()

    def test_invalidate_with_cursor_noop_if_not_reached(self) -> None:
        group = self.create_group()
        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(self.user.id))
        derived = process_group_log(group.id)
        assert derived is not None
        old_cursor = derived.cursor_id

        future = derived.cursor_date.replace(year=derived.cursor_date.year + 1)
        invalidate_group_derived_data(group.id, cursor=(future, old_cursor + 1000))
        derived.refresh_from_db()
        assert derived.cursor_id == old_cursor

    def test_invalidate_then_reprocess(self) -> None:
        group = self.create_group()
        user = self.user
        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(user.id))
        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(user.id))
        derived = process_group_log(group.id)
        assert derived is not None
        assert derived.view_count == 2

        invalidate_group_derived_data(group.id)
        derived = process_group_log(group.id)
        assert derived is not None
        assert derived.view_count == 2  # rebuilt from scratch

    def test_invalidate_soft_schedules_rebuild(self) -> None:
        group = self.create_group()
        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(self.user.id))
        derived = process_group_log(group.id)
        assert derived is not None
        original_id = derived.id

        with patch("sentry.issues.derived.tasks.rebuild_group_derived_data_task") as mock_task:
            invalidate_group_derived_data(group.id, hard_delete=False)
            mock_task.delay.assert_called_once_with(group.id)

        # Live row is still in place
        derived.refresh_from_db()
        assert derived.id == original_id
        assert derived.is_live

    def test_resolved_in_pull_request_closes(self) -> None:
        group = self.create_group()
        user = self.user

        _publish(
            group=group,
            action=ResolvedInPullRequestAction(pull_request=101),
            actor=GroupActionActor.user(user.id),
        )
        derived = process_group_log(group.id)
        assert derived is not None
        assert derived.data["status"] == "closed"

    def test_noop_when_on_demand_disabled(self) -> None:
        group = self.create_group()

        with self.options({"issues.derived-data.create-on-demand": False}):
            # Publish with async strategy so inline processing doesn't create a row
            with patch("sentry.issues.derived.tasks.process_group_log_task"):
                _publish(
                    group=group, action=ViewAction(), actor=GroupActionActor.user(self.user.id)
                )

            derived = process_group_log(group.id)

        assert derived is None
        assert not GroupDerivedData.objects.filter(group_id=group.id, is_live=True).exists()


# --- Non-live row lifecycle ---


@with_feature("projects:issue-action-log-write-to-db")
class PromoteToLiveTest(TestCase):
    def test_promote_with_no_existing_live(self) -> None:
        group = self.create_group()
        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(self.user.id))

        row = create_processing_row(group.id)
        processing._drain_log(row)
        assert promote_to_live(row) is PromotionResult.PROMOTED
        assert row.is_live

        row.refresh_from_db()
        assert row.is_live
        assert row.view_count == 1

    def test_promote_replaces_older_live(self) -> None:
        group = self.create_group()
        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(self.user.id))

        old = process_group_log(group.id)
        assert old is not None
        old_id = old.id

        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(self.user.id))

        new = create_processing_row(group.id)
        processing._drain_log(new)
        assert promote_to_live(new) is PromotionResult.PROMOTED

        assert not GroupDerivedData.objects.filter(id=old_id).exists()
        assert new.is_live
        assert new.view_count == 2

    def test_promote_rejected_if_candidate_older(self) -> None:
        group = self.create_group()
        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(self.user.id))

        old_candidate = create_processing_row(group.id)
        processing._drain_log(old_candidate)

        # Create and promote a newer candidate first
        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(self.user.id))
        new_candidate = create_processing_row(group.id)
        processing._drain_log(new_candidate)
        assert promote_to_live(new_candidate) is PromotionResult.PROMOTED

        assert promote_to_live(old_candidate) is PromotionResult.SUPERSEDED

        # New candidate is still live
        live = GroupDerivedData.objects.get(group_id=group.id, is_live=True)
        assert live.id == new_candidate.id

    def test_promote_rejected_if_cursor_behind(self) -> None:
        group = self.create_group()
        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(self.user.id))
        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(self.user.id))

        # Create a live row that has processed everything
        live = process_group_log(group.id)
        assert live is not None

        # Create a candidate that only processes the first entry
        candidate = create_processing_row(group.id)
        processing._process_batch(PIPELINE, candidate, batch_size=1)

        # Candidate cursor is behind the live row
        assert (candidate.cursor_date, candidate.cursor_id) < (
            live.cursor_date,
            live.cursor_id,
        )
        assert promote_to_live(candidate) is PromotionResult.CURSOR_BEHIND

    def test_build_and_promote(self) -> None:
        group = self.create_group()
        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(self.user.id))
        _publish(group=group, action=ResolveAction(), actor=GroupActionActor.user(self.user.id))

        derived = build_and_promote_derived_data(group.id)
        assert derived is not None
        assert derived.is_live
        assert derived.view_count == 1
        assert derived.data["status"] == "closed"

    def test_build_and_promote_replaces_stale_live(self) -> None:
        group = self.create_group()
        user = self.user

        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(user.id))
        old = process_group_log(group.id)
        assert old is not None
        old_id = old.id

        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(user.id))
        new = build_and_promote_derived_data(group.id)
        assert new is not None
        assert new.id != old_id
        assert new.view_count == 2
        assert not GroupDerivedData.objects.filter(id=old_id).exists()

    def test_cleanup_stale_processing_rows(self) -> None:
        group = self.create_group()
        row = create_processing_row(group.id)

        assert cleanup_stale_processing_rows(max_age=timedelta(hours=1)) == 0

        GroupDerivedData.objects.filter(id=row.id).update(
            date_added=row.date_added - timedelta(hours=2)
        )

        assert cleanup_stale_processing_rows(max_age=timedelta(hours=1)) == 1
        assert not GroupDerivedData.objects.filter(id=row.id).exists()

    def test_drain_log_respects_time_limit(self) -> None:
        group = self.create_group()
        for _ in range(5):
            _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(self.user.id))

        row = create_processing_row(group.id)

        # Zero time limit forces bail after first batch
        drained = processing._drain_log(row, batch_size=2, time_limit=timedelta(0))
        assert not drained
        # Processed one batch (2 entries) but not all 5
        assert row.cursor_id > 0
        entries = list(GroupActionLogEntry.objects.filter(group_id=group.id).order_by("id"))
        assert row.cursor_id < entries[-1].id

    def test_process_group_log_reenqueues_on_partial_drain(self) -> None:
        group = self.create_group()
        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(self.user.id))

        with (
            patch("sentry.issues.derived.processing._drain_log", return_value=False),
            patch("sentry.issues.derived.tasks.process_group_log_task") as mock_task,
        ):
            derived = process_group_log(group.id)

        assert derived is not None
        mock_task.delay.assert_called_once_with(group.id)

    def test_build_and_promote_exhaustion_reenqueues(self) -> None:
        group = self.create_group()
        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(self.user.id))

        # Create a live row, then keep advancing its cursor so the candidate
        # can never catch up (simulated by patching promote_to_live).
        process_group_log(group.id)

        with patch(
            "sentry.issues.derived.processing.promote_to_live",
            return_value=PromotionResult.CURSOR_BEHIND,
        ):
            with patch("sentry.issues.derived.tasks.rebuild_group_derived_data_task") as mock_task:
                result = build_and_promote_derived_data(group.id)

        assert result is None
        mock_task.delay.assert_called_once_with(group.id)

    def test_build_and_promote_breaks_on_superseded(self) -> None:
        group = self.create_group()
        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(self.user.id))
        process_group_log(group.id)

        with patch(
            "sentry.issues.derived.processing.promote_to_live",
            return_value=PromotionResult.SUPERSEDED,
        ) as mock_promote:
            result = build_and_promote_derived_data(group.id)

        assert result is None
        # Should only try once, not retry
        assert mock_promote.call_count == 1


# --- Pure Python tests (no DB) ---


def test_mutation_checking_catches_in_place_mutation() -> None:
    ITEMS = Feature[list[str]]("items", default_factory=list)

    @aggregator((ITEMS,))
    def bad_mutator(state: StateView, entry: object) -> AggregatorResult:
        state[ITEMS].append("oops")
        return None

    p = Pipeline([bad_mutator], version=1, check_mutations=True)
    state = p.initial_state()

    class FakeEntry:
        type = 0

    with pytest.raises(RuntimeError, match="mutated feature 'items' in place"):
        p.step(state, FakeEntry())


def test_state_updated_tracks_merged_features() -> None:
    A = Feature[int]("a", default=0)
    B = Feature[int]("b", default=0)
    state = State({A: 0, B: 0})

    assert state.updated == frozenset()

    state.merge(StateUpdate({A: 1}))
    assert state.updated == frozenset({A})
    assert state[A] == 1
    assert state[B] == 0


def test_build_update_json_blob_includes_all_json_features() -> None:
    A = Feature[int]("a", default=0)
    B = Feature[int]("b", default=0)

    @aggregator((A, B))
    def compute(state: StateView, entry: object) -> AggregatorResult:
        return None

    pipeline = Pipeline([compute], version=1)
    state = pipeline.initial_state()

    # Update only A — blob should still contain both A and B
    state.merge(StateUpdate({A: 1}))
    update = GroupDerivedDataStore.build_update(pipeline, state)

    assert update["data"] == {"a": 1, "b": 0}


def test_store_apply_to_instance() -> None:
    derived = GroupDerivedData()
    derived.data = {}
    update = {"data": {"status": "closed"}, "view_count": 5}
    GroupDerivedDataStore.apply_to_instance(derived, update)
    assert derived.data == {"status": "closed"}
    assert derived.view_count == 5


def test_all_feature_defaults_round_trip_through_json() -> None:
    state = PIPELINE.initial_state()
    blob = {f.name: f.to_json(state[f]) for f in PIPELINE.features}
    serialized = json.loads(json.dumps(blob))
    for f in PIPELINE.features:
        assert f.from_json(serialized[f.name]) == state[f], f"round-trip failed for {f.name}"


# --- Codec tests ---


class TestDateTimeCodec:
    def test_json_round_trip(self) -> None:
        codec = DateTimeCodec()
        dt = datetime(2025, 3, 15, 12, 30, 45, tzinfo=timezone.utc)
        assert codec.from_json(codec.to_json(dt)) == dt

    def test_to_json_produces_iso_string(self) -> None:
        codec = DateTimeCodec()
        dt = datetime(2025, 3, 15, 12, 30, 45, tzinfo=timezone.utc)
        dumped = codec.to_json(dt)
        assert isinstance(dumped, str)
        assert dumped == dt.isoformat()

    def test_column_round_trip_is_identity(self) -> None:
        codec = DateTimeCodec()
        dt = datetime(2025, 3, 15, 12, 30, 45, tzinfo=timezone.utc)
        assert codec.from_column(codec.to_column(dt)) == dt
        assert codec.to_column(dt) is dt

    def test_optional_none(self) -> None:
        codec = OptionalCodec(DateTimeCodec())
        assert codec.to_json(None) is None
        assert codec.from_json(None) is None

    def test_optional_json_round_trip(self) -> None:
        codec = OptionalCodec(DateTimeCodec())
        dt = datetime(2025, 3, 15, 12, 30, 45, tzinfo=timezone.utc)
        assert codec.from_json(codec.to_json(dt)) == dt


class TestEnumCodecCoverage:
    @pytest.mark.parametrize("raw", ["open", "closed"])
    def test_issue_status_json_round_trip(self, raw: str) -> None:
        codec = EnumCodec(IssueStatus)
        loaded = codec.from_json(raw)
        assert codec.to_json(loaded) == raw

    @pytest.mark.parametrize("raw", ["open", "closed"])
    def test_issue_status_column_round_trip(self, raw: str) -> None:
        codec = EnumCodec(IssueStatus)
        loaded = codec.from_column(raw)
        assert isinstance(loaded, IssueStatus)
        assert codec.to_column(loaded) == raw

    @pytest.mark.parametrize(
        "raw", ["identified", "assigned", "diagnosed", "fix_proposed", "fix_applied"]
    )
    def test_issue_progress_state_json_round_trip(self, raw: str) -> None:
        codec = EnumCodec(IssueProgressState)
        loaded = codec.from_json(raw)
        assert codec.to_json(loaded) == raw

    @pytest.mark.parametrize(
        "raw", ["identified", "assigned", "diagnosed", "fix_proposed", "fix_applied"]
    )
    def test_issue_progress_state_column_produces_enum(self, raw: str) -> None:
        codec = EnumCodec(IssueProgressState)
        loaded = codec.from_column(raw)
        assert isinstance(loaded, IssueProgressState)

    @pytest.mark.parametrize(
        "raw",
        [None, "identified", "assigned", "diagnosed", "fix_proposed", "fix_applied"],
    )
    def test_optional_progress_json_round_trip(self, raw: str | None) -> None:
        codec = OptionalCodec(EnumCodec(IssueProgressState))
        loaded = codec.from_json(raw)
        assert codec.to_json(loaded) == raw

    @pytest.mark.parametrize(
        "raw",
        [None, "identified", "assigned", "diagnosed", "fix_proposed", "fix_applied"],
    )
    def test_optional_progress_column_round_trip(self, raw: str | None) -> None:
        codec = OptionalCodec(EnumCodec(IssueProgressState))
        loaded = codec.from_column(raw)
        if raw is not None:
            assert isinstance(loaded, IssueProgressState)
        assert codec.to_column(loaded) == raw


# --- Store tests (need DB) ---


@with_feature("projects:issue-action-log-write-to-db")
class GroupDerivedDataStoreTest(TestCase):
    def test_feature_default_matches_column_default(self) -> None:
        field = GroupDerivedData._meta.get_field("progress")
        assert PROGRESS.initial_value() == field.default

    def test_load_returns_defaults_for_empty_data(self) -> None:
        group = self.create_group()
        derived = GroupDerivedData.objects.create(
            group=group,
            is_live=True,
            data={},
        )
        state = GroupDerivedDataStore.load(PIPELINE, derived)
        assert state[VIEW_COUNT] == 0
        assert state[STATUS] == IssueStatus.OPEN

    def test_load_populates_columns_and_json(self) -> None:
        group = self.create_group()
        derived = GroupDerivedData.objects.create(
            group=group,
            is_live=True,
            view_count=3,
            progress="diagnosed",
            data={"status": "closed"},
        )
        state = GroupDerivedDataStore.load(PIPELINE, derived)
        assert state[VIEW_COUNT] == 3
        assert state[PROGRESS] == IssueProgressState.DIAGNOSED
        assert isinstance(state[PROGRESS], IssueProgressState)
        assert state[STATUS] == IssueStatus.CLOSED

    def test_load_null_progress(self) -> None:
        group = self.create_group()
        derived = GroupDerivedData.objects.create(
            group=group,
            is_live=True,
            progress=None,
            data={},
        )
        state = GroupDerivedDataStore.load(PIPELINE, derived)
        assert state[PROGRESS] is None

    def test_round_trip_preserves_state(self) -> None:
        group = self.create_group()
        user = self.user

        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(user.id))
        _publish(group=group, action=ResolveAction(), actor=GroupActionActor.user(user.id))
        first = process_group_log(group.id)
        assert first is not None

        first_data = first.data.copy()
        first_view_count = first.view_count
        first_progress = first.progress
        first_last_progressed_at = first.last_progressed_at

        invalidate_group_derived_data(group.id)
        second = process_group_log(group.id)
        assert second is not None

        assert second.data == first_data
        assert second.view_count == first_view_count
        assert second.progress == first_progress
        assert second.last_progressed_at == first_last_progressed_at

    def test_build_update_only_includes_updated_features(self) -> None:
        state = PIPELINE.initial_state()

        # Update only STATUS (lives in JSON) — column features stay clean
        state.merge(StateUpdate({STATUS: IssueStatus.CLOSED}))

        update = GroupDerivedDataStore.build_update(PIPELINE, state)

        assert "view_count" not in update
        assert "progress" not in update
        assert "last_progressed_at" not in update
        assert "data" in update
        assert update["data"]["status"] == "closed"

        # Update a column-mapped feature — it should appear in the update
        state.merge(StateUpdate({VIEW_COUNT: 5}))
        update = GroupDerivedDataStore.build_update(PIPELINE, state)
        assert update["view_count"] == 5

    def test_build_update_excludes_json_blob_when_no_json_features_updated(self) -> None:
        state = PIPELINE.initial_state()

        # Update only a column-mapped feature — JSON blob should be excluded
        state.merge(StateUpdate({VIEW_COUNT: 3}))

        update = GroupDerivedDataStore.build_update(PIPELINE, state)

        assert update["view_count"] == 3
        assert "data" not in update

    def test_progress_round_trip(self) -> None:
        group = self.create_group()
        user = self.user
        actor = GroupActionActor.user(user.id)

        _publish(group=group, action=ViewAction(), actor=actor)
        derived = process_group_log(group.id)
        assert derived is not None

        state = GroupDerivedDataStore.load(PIPELINE, derived)
        assert state[PROGRESS] == IssueProgressState.IDENTIFIED
        assert state[LAST_PROGRESSED_AT] is None

        _publish(group=group, action=ResolveAction(), actor=actor)
        derived = process_group_log(group.id)
        assert derived is not None

        state = GroupDerivedDataStore.load(PIPELINE, derived)
        assert state[PROGRESS] is None
        assert state[LAST_PROGRESSED_AT] is not None

        _publish(group=group, action=UnresolveAction(), actor=actor)
        derived = process_group_log(group.id)
        assert derived is not None

        state = GroupDerivedDataStore.load(PIPELINE, derived)
        assert state[PROGRESS] == IssueProgressState.IDENTIFIED
        assert state[LAST_PROGRESSED_AT] is not None


class _IntentionalRollback(Exception):
    pass


@with_feature("projects:issue-action-log-write-to-db")
class DerivedDataTransactionTest(TestCase):
    def test_rolled_back_action_does_not_produce_derived_data(self) -> None:
        group = self.create_group()

        try:
            with transaction.atomic(using=router.db_for_write(CellOutbox)):
                publish_action(
                    ViewAction(),
                    source=SOURCE,
                    group_id=group.id,
                    project=group.project,
                    actor=GroupActionActor.user(self.user.id),
                )
                assert CellOutbox.objects.filter(
                    category=OutboxCategory.GROUP_ACTION_LOG_EVENT
                ).exists()
                raise _IntentionalRollback
        except _IntentionalRollback:
            pass

        assert not CellOutbox.objects.filter(
            category=OutboxCategory.GROUP_ACTION_LOG_EVENT
        ).exists()
        assert GroupActionLogEntry.objects.filter(group_id=group.id).count() == 0
        assert not GroupDerivedData.objects.filter(group_id=group.id).exists()

    def test_committed_action_produces_derived_data(self) -> None:
        group = self.create_group()

        _publish(group=group, action=ViewAction(), actor=GroupActionActor.user(self.user.id))

        assert GroupActionLogEntry.objects.filter(group_id=group.id).count() == 1
        assert GroupDerivedData.objects.filter(group_id=group.id, is_live=True).exists()
        derived = GroupDerivedData.objects.get(group_id=group.id, is_live=True)
        assert derived.view_count == 1
