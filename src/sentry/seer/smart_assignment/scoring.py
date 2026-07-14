"""Score smart-assignment predictions against the observed ground truth.

The dedicated result table was removed: Seer is the system of record for a run's
verdict/transcript (queryable per issue via ``category_value=<group_id>``, ~30-day
TTL). The run's Sentry-side mirror -- ``SeerAgentRun`` with
``source="smart_assignment"`` -- carries the thin bookkeeping needed to emit the
``smart_assignment.scored`` metric with no cross-service call: the dispatch trigger,
the delivered top pick resolved to a Sentry user, and the observed ground truth, all
in its ``extras`` JSON.

Correctness can't be scored at a single moment: the prediction (delivery) and the
ground truth (assignment/resolution) land in either order. Each side writes its half
into the mirror's ``extras`` under a row lock; whichever completes the pair emits the
metric exactly once (guarded by ``extras["scored"]``).

Outcomes give partial credit for landing on the right team:
  - ``exact`` -- predicted user is the actual assignee
  - ``team``  -- predicted user isn't the assignee but is on the correct team
  - ``miss``  -- neither
The "correct team" is the team the issue was assigned to, or (for a user assignment)
any team the actual assignee belongs to.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import router, transaction

from sentry.models.activity import Activity
from sentry.models.group import Group
from sentry.models.groupassignee import GroupAssignee
from sentry.models.organizationmemberteam import OrganizationMemberTeam
from sentry.seer.models.run import SeerAgentRun
from sentry.seer.smart_assignment.models import (
    FEATURE_ID,
    RESOLUTION_ACTIVITIES,
    SmartAssignmentScore,
)
from sentry.types.activity import ActivityType
from sentry.utils import metrics

logger = logging.getLogger(__name__)

# Marker the acting code stamps into the ASSIGNED activity's data (via the assign()
# `extra` dict) when it auto-assigns based on a prediction. Lets ground-truth capture
# skip our own assignment -- recording it would just score us against ourselves.
AUTO_ASSIGN_SOURCE = "seer_smart_assignment"


def _get_run(group_id: int) -> SeerAgentRun | None:
    """The canonical smart-assignment run mirror for a group (earliest wins).

    Dedup keeps this to one per group in practice; always picking the earliest keeps
    the prediction and ground-truth writers agreeing on the same row even in the rare
    case a race dispatched more than one run.
    """
    return (
        SeerAgentRun.objects.filter(group_id=group_id, source=FEATURE_ID)
        .select_related("run")
        .order_by("date_added")
        .first()
    )


def record_prediction(group_id: int, predicted_assignee_user_id: int | None) -> None:
    """Record the delivered top pick (resolved to a user) on the run mirror, then
    score if the ground truth already landed. ``None`` means the agent abstained or
    we couldn't map its pick to an org user -- stored so we don't re-treat the run as
    undelivered, but never scored."""
    run = _get_run(group_id)
    if run is None:
        return
    _apply(run.id, {"predicted_assignee_user_id": predicted_assignee_user_id})


def record_ground_truth(
    group: Group,
    activity_type: ActivityType,
    activity: Activity | None = None,
) -> None:
    """Record who the issue actually belonged to on the run mirror, then score.

    No-op if no run was dispatched for the group, or the outcome carries no useful
    signal: a Seer AI-step start, an automatic resolution with no acting user, or our
    own auto-assignment (tagged ``AUTO_ASSIGN_SOURCE``, which would score us against
    ourselves). For an assignment we mirror the current assignee (user and/or team).
    For a user-driven resolution we record the resolver as the assumed assignee only
    when no explicit assignee has been recorded -- an assignment is better truth.
    """
    run = _get_run(group.id)
    if run is None:
        return

    updates: dict[str, Any] = {}
    if activity_type == ActivityType.ASSIGNED:
        if activity is not None and (activity.data or {}).get("source") == AUTO_ASSIGN_SOURCE:
            return
        assignee = GroupAssignee.objects.filter(group=group).first()
        if assignee is None:
            return
        updates["actual_assignee_user_id"] = assignee.user_id
        updates["actual_assignee_team_id"] = assignee.team_id
    elif activity_type in RESOLUTION_ACTIVITIES:
        if activity is None or activity.user_id is None:
            return
        if (run.extras or {}).get("actual_assignee_user_id") is not None:
            # An explicit assignee is better ground truth than the resolver.
            return
        updates["actual_assignee_user_id"] = activity.user_id
    else:
        return

    updates["ground_truth_source"] = activity_type.name
    _apply(run.id, updates)
    metrics.incr("smart_assignment.ground_truth.recorded", tags={"trigger": activity_type.name})


def _apply(run_id: int, updates: dict[str, Any]) -> None:
    """Merge `updates` into the run mirror's extras under a row lock and, if that
    completes the (prediction, ground truth) pair, emit `smart_assignment.scored`
    once. The lock serializes the prediction and ground-truth writers so neither a
    lost update nor a double emit is possible."""
    with transaction.atomic(using=router.db_for_write(SeerAgentRun)):
        run = SeerAgentRun.objects.select_for_update().select_related("run").get(id=run_id)
        extras = dict(run.extras or {})
        extras.update(updates)
        outcome = _score(run.run.organization_id, extras)
        if outcome is not None:
            extras["scored"] = str(outcome)
        run.extras = extras
        run.save(update_fields=["extras"])

    if outcome is not None:
        metrics.incr(
            "smart_assignment.scored",
            tags={"result": outcome, "trigger": extras.get("trigger")},
        )


def _score(organization_id: int, extras: dict[str, Any]) -> SmartAssignmentScore | None:
    """Grade the prediction in `extras` against its ground truth, or None if the pair
    isn't complete yet or it's already been scored."""
    if extras.get("scored"):
        return None
    predicted_user_id = extras.get("predicted_assignee_user_id")
    actual_user_id = extras.get("actual_assignee_user_id")
    actual_team_id = extras.get("actual_assignee_team_id")
    if predicted_user_id is None or (actual_user_id is None and actual_team_id is None):
        return None

    if predicted_user_id == actual_user_id:
        return SmartAssignmentScore.EXACT
    if _is_team_match(organization_id, predicted_user_id, actual_user_id, actual_team_id):
        return SmartAssignmentScore.TEAM
    return SmartAssignmentScore.MISS


def _user_team_ids(organization_id: int, user_id: int) -> set[int]:
    return set(
        OrganizationMemberTeam.objects.filter(
            is_active=True,
            organizationmember__organization_id=organization_id,
            organizationmember__user_id=user_id,
        ).values_list("team_id", flat=True)
    )


def _correct_team_ids(
    organization_id: int, actual_user_id: int | None, actual_team_id: int | None
) -> set[int]:
    """The team(s) a correct prediction could belong to for this ground truth."""
    if actual_team_id is not None:
        return {actual_team_id}
    if actual_user_id is not None:
        return _user_team_ids(organization_id, actual_user_id)
    return set()


def _is_team_match(
    organization_id: int,
    predicted_user_id: int,
    actual_user_id: int | None,
    actual_team_id: int | None,
) -> bool:
    """Whether the predicted user is on a team the ground truth points at."""
    correct_team_ids = _correct_team_ids(organization_id, actual_user_id, actual_team_id)
    if not correct_team_ids:
        return False
    return bool(_user_team_ids(organization_id, predicted_user_id) & correct_team_ids)
