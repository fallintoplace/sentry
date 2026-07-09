import logging

from sentry.silo.base import SiloMode
from sentry.tasks.base import instrumented_task
from sentry.taskworker.namespaces import issues_tasks

logger = logging.getLogger(__name__)


@instrumented_task(
    name="sentry.issues.derived.tasks.process_group_log_task",
    namespace=issues_tasks,
    silo_mode=SiloMode.CELL,
)
def process_group_log_task(group_id: int, **kwargs: object) -> None:
    from sentry.issues.derived.processing import process_group_log
    from sentry.models.group import Group

    try:
        process_group_log(group_id)
    except Group.DoesNotExist:
        logger.info("process_group_log_task.group_not_found", extra={"group_id": group_id})


@instrumented_task(
    name="sentry.issues.derived.tasks.rebuild_group_derived_data_task",
    namespace=issues_tasks,
    silo_mode=SiloMode.CELL,
)
def rebuild_group_derived_data_task(group_id: int, **kwargs: object) -> None:
    """Build a new GroupDerivedData row from scratch and promote it to live."""
    from sentry.issues.derived.processing import build_and_promote_derived_data

    build_and_promote_derived_data(group_id)


@instrumented_task(
    name="sentry.issues.derived.tasks.backfill_and_activate_group_derived_data_task",
    namespace=issues_tasks,
    silo_mode=SiloMode.CELL,
)
def backfill_and_activate_group_derived_data_task(
    group_id: int, project_id: int, **kwargs: object
) -> None:
    """Backfill the action log from Activity records, then build and activate derived data.

    This is the full lifecycle task for bringing a group's derived data online:
    1. Translate historical Activity records into GroupActionLogEntry rows.
    2. Create a non-live GroupDerivedData row and drain the full log into it.
    3. Promote the row to live, atomically replacing any existing live row.
    """
    from sentry.issues.action_log.backfill import backfill_group_activities
    from sentry.issues.derived.processing import build_and_promote_derived_data

    backfill_group_activities(group_id=group_id, project_id=project_id)
    build_and_promote_derived_data(group_id)
