from unittest.mock import MagicMock, patch

from django.utils import timezone

from sentry.seer.models.run import SeerAgentRun, SeerRun, SeerRunType
from sentry.seer.smart_assignment.delivery import deliver_smart_assignment_result
from sentry.seer.smart_assignment.models import FEATURE_ID, SmartAssignmentScore
from sentry.testutils.cases import TestCase
from sentry.types.activity import ActivityType

METRICS_PATH = "sentry.seer.smart_assignment.delivery.metrics"


class DeliverSmartAssignmentResultTest(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.group = self.create_group()
        self.seer_run = SeerRun.objects.create(
            organization=self.organization,
            type=SeerRunType.FEATURE_RUN,
            last_triggered_at=timezone.now(),
        )
        # The run mirror the client would have created at dispatch.
        self.mirror = SeerAgentRun.objects.create(
            run=self.seer_run,
            source=FEATURE_ID,
            group=self.group,
            extras={"trigger": ActivityType.SEER_RCA_STARTED.name},
        )

    def _extras(self) -> dict:
        self.mirror.refresh_from_db()
        return self.mirror.extras

    def _deliver(self, result: dict | None, status: str = "completed", error: str | None = None):
        deliver_smart_assignment_result(
            self.organization.id, str(self.seer_run.uuid), status, result, error
        )

    def _assert_outcome(self, mock_metrics: MagicMock, expected: str) -> None:
        mock_metrics.incr.assert_called_once_with(
            "smart_assignment.delivery", tags={"outcome": expected}
        )

    @patch(METRICS_PATH)
    def test_records_top_pick_resolved_to_user(self, mock_metrics: MagicMock) -> None:
        alice = self.create_user(username="alice")
        self.create_member(user=alice, organization=self.organization)
        result = {
            "candidates": [
                {
                    "identifier": "alice",
                    "identifier_kind": "username",
                    "reason": "suspect commit",
                    "confidence": "high",
                },
                {
                    "identifier": "bob",
                    "identifier_kind": "username",
                    "reason": "code owner",
                    "confidence": "low",
                },
            ]
        }
        self._deliver(result)

        # Top pick resolved to a real user and cached on the run mirror (the full
        # verdict itself lives on the Seer run, not here).
        assert self._extras()["predicted_assignee_user_id"] == alice.id
        self._assert_outcome(mock_metrics, "resolved")

    @patch(METRICS_PATH)
    def test_email_kind_resolves_by_verified_email(self, mock_metrics: MagicMock) -> None:
        # An email-kind pick (unlinked commit author) resolves by verified org email,
        # even when the address happens to also be someone's username-shaped handle.
        carol = self.create_user(email="carol@example.com")
        self.create_member(user=carol, organization=self.organization)
        result = {
            "candidates": [
                {
                    "identifier": "carol@example.com",
                    "identifier_kind": "email",
                    "reason": "unlinked commit author",
                    "confidence": "low",
                },
            ]
        }
        self._deliver(result)

        assert self._extras()["predicted_assignee_user_id"] == carol.id
        self._assert_outcome(mock_metrics, "resolved")

    @patch(METRICS_PATH)
    def test_unresolvable_identifier_records_no_user(self, mock_metrics: MagicMock) -> None:
        result = {
            "candidates": [
                {
                    "identifier": "nobody-here",
                    "identifier_kind": "username",
                    "reason": "guess",
                    "confidence": "low",
                },
            ]
        }
        self._deliver(result)

        assert self._extras()["predicted_assignee_user_id"] is None
        self._assert_outcome(mock_metrics, "unlinked")

    @patch(METRICS_PATH)
    def test_empty_candidates_is_abstain(self, mock_metrics: MagicMock) -> None:
        self._deliver({"candidates": []})
        assert self._extras()["predicted_assignee_user_id"] is None
        self._assert_outcome(mock_metrics, "abstain")

    @patch(METRICS_PATH)
    def test_error_status_records_nothing(self, mock_metrics: MagicMock) -> None:
        self._deliver(None, status="error", error="boom")
        # No prediction recorded on error; the Seer run holds the failure.
        assert "predicted_assignee_user_id" not in self._extras()
        self._assert_outcome(mock_metrics, "error")

    @patch("sentry.seer.smart_assignment.scoring.metrics")
    def test_scores_when_ground_truth_already_present(
        self, mock_scoring_metrics: MagicMock
    ) -> None:
        # Assignment landed before Seer finished: ground truth is already on the run
        # mirror, so delivering the prediction completes the pair and scores it.
        alice = self.create_user(username="alice")
        self.create_member(user=alice, organization=self.organization)
        self.mirror.extras = {**self.mirror.extras, "actual_assignee_user_id": alice.id}
        self.mirror.save(update_fields=["extras"])
        result = {
            "candidates": [
                {
                    "identifier": "alice",
                    "identifier_kind": "username",
                    "reason": "x",
                    "confidence": "high",
                }
            ]
        }
        self._deliver(result)

        mock_scoring_metrics.incr.assert_called_once_with(
            "smart_assignment.scored",
            tags={
                "result": SmartAssignmentScore.EXACT,
                "trigger": ActivityType.SEER_RCA_STARTED.name,
            },
        )

    @patch(METRICS_PATH)
    def test_missing_run_is_noop(self, mock_metrics: MagicMock) -> None:
        # Unknown run uuid: should not raise.
        deliver_smart_assignment_result(
            self.organization.id,
            "00000000-0000-0000-0000-000000000000",
            "completed",
            {"candidates": []},
            None,
        )
        self._assert_outcome(mock_metrics, "missing_run")
