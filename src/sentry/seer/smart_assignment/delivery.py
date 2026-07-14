"""Delivery handler for smart_assignment feature results from Seer.

Seer pushes the `AssigneeVerdict` artifact back via the `deliver_feature_result`
RPC, routed here by feature_id (see `sentry.seer.agent.feature_delivery`). Seer is
the system of record for the verdict itself; here we resolve the top pick to a
Sentry user and stash it on the run mirror's `extras` (see `scoring`) so correctness
can be scored against the eventual assignee without re-fetching from Seer, and emit a
delivery outcome metric.
"""

from __future__ import annotations

import logging
from typing import Any

from sentry.organizations.services.organization import organization_service
from sentry.seer.agent.types import FeatureRunStatus
from sentry.seer.models.run import SeerAgentRun
from sentry.seer.smart_assignment.models import FEATURE_ID, AssigneeVerdict
from sentry.seer.smart_assignment.scoring import record_prediction
from sentry.users.services.user.service import user_service
from sentry.utils import metrics

logger = logging.getLogger(__name__)


def _resolve_identifier_to_user_id(
    organization_id: int, identifier: str | None, kind: str | None
) -> int | None:
    """Map an agent-produced `identifier` to a Sentry user id in the org.

    The agent tags each pick with `identifier_kind` (see the `RankedCandidate`
    contract on the Seer side) so we resolve deterministically instead of inferring
    the type from the string:
      - "email":    verified org email (the RPC already scopes to the org).
      - "username": unique username, confirmed to be an org member so a global match
                    can't attribute a prediction to someone outside the org.
    `kind` is a validated Literal by the time it gets here, so this doesn't guess.
    Returns None when the agent named no one or the identifier maps to no org user.
    """
    if not identifier:
        return None

    value = identifier.strip()
    if not value:
        return None

    if kind == "email":
        users = user_service.get_many_by_email(
            emails=[value], organization_id=organization_id, is_verified=True
        )
        return users[0].id if users else None

    if kind == "username":
        users = user_service.get_by_username(username=value)
        if not users:
            return None
        user_id = users[0].id
        member = organization_service.check_membership_by_id(
            organization_id=organization_id, user_id=user_id
        )
        return user_id if member is not None else None

    return None


def deliver_smart_assignment_result(
    organization_id: int,
    run_uuid: str,
    status: FeatureRunStatus,
    result: dict[str, Any] | None,
    error: str | None,
) -> None:
    """Resolve a delivered smart_assignment verdict's top pick and record it.

    Emits a single `smart_assignment.delivery` counter tagged with the outcome so we
    can track success vs failure, how often the agent abstains, and how often it
    names someone we can't link to a Sentry user in the org:
      - `missing_run`  -- delivery arrived with no matching run mirror (orphaned run)
      - `error`        -- Seer run failed or returned no artifact
      - `abstain`      -- completed, but the agent named no one
      - `unlinked`     -- named someone we couldn't map to an org user
      - `resolved`     -- named someone we mapped to a Sentry user
    """
    agent_run = (
        SeerAgentRun.objects.filter(
            run__uuid=run_uuid, run__organization_id=organization_id, source=FEATURE_ID
        )
        .select_related("run")
        .first()
    )
    if agent_run is None:
        metrics.incr("smart_assignment.delivery", tags={"outcome": "missing_run"})
        logger.warning(
            "smart_assignment.delivery.missing_run",
            extra={"organization_id": organization_id, "run_uuid": run_uuid},
        )
        return

    group_id = agent_run.group_id
    log_extra = {"organization_id": organization_id, "group_id": group_id, "run_uuid": run_uuid}

    if status == "error" or result is None:
        metrics.incr("smart_assignment.delivery", tags={"outcome": "error"})
        logger.warning(
            "smart_assignment.delivery.no_result",
            extra={**log_extra, "status": status, "error": error},
        )
        return

    try:
        verdict = AssigneeVerdict.parse_obj(result)
    except Exception:
        metrics.incr("smart_assignment.delivery", tags={"outcome": "error"})
        logger.warning("smart_assignment.delivery.invalid_result", extra=log_extra)
        return

    top_pick = verdict.candidates[0] if verdict.candidates else None
    predicted_identifier = top_pick.identifier if top_pick is not None else None
    predicted_kind = top_pick.identifier_kind if top_pick is not None else None

    # Resolve the top pick to a Sentry user so scoring can compare it directly against
    # the eventual assignee. Null if the agent named no one or the identifier doesn't
    # map to an org user; the raw verdict is still queryable on the Seer run.
    predicted_assignee_user_id = _resolve_identifier_to_user_id(
        organization_id, predicted_identifier, predicted_kind
    )

    # Record the prediction on the run mirror; if the ground truth already landed
    # (assignment before Seer finished), this completes the pair and scores it now.
    if group_id is not None:
        record_prediction(group_id, predicted_assignee_user_id)

    if not predicted_identifier:
        outcome = "abstain"
    elif predicted_assignee_user_id is None:
        outcome = "unlinked"
    else:
        outcome = "resolved"
    metrics.incr("smart_assignment.delivery", tags={"outcome": outcome})
