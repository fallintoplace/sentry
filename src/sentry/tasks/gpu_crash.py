"""Isolated async task for GPU crash symbolication via teapot.

Runs in its own ``gpu.crash_dump`` namespace, scheduled from
``post_process_group`` after the primary event is saved. Isolated so a slow or
unavailable teapot can never affect the CPU issue: own worker pool,
``at_most_once`` (never retried), and every stage swallows its errors — worst
case is no GPU issue, never anything worse.
"""

from __future__ import annotations

import logging
from typing import Any

import sentry_sdk

from sentry import features, options
from sentry.silo.base import SiloMode
from sentry.tasks.base import instrumented_task
from sentry.taskworker.namespaces import gpu_crash_dump_tasks
from sentry.utils import metrics

logger = logging.getLogger(__name__)

# How long a produced GPU crash is remembered so redelivered tasks no-op.
_ONCE_TTL = 3600


class _RawAttachment:
    """``TeapotAttachment`` adapter over ``EventAttachment`` bytes loaded post-save.

    ``stored_id = None`` forces the multipart path (attachments aren't in
    objectstore by default yet).
    """

    stored_id: str | None = None

    def __init__(self, name: str, data: bytes) -> None:
        self.name = name
        self._data = data

    def load_data(self, project: Any) -> bytes:
        return self._data


def _claim_once(event_id: str) -> bool:
    """Return True the first time we see ``event_id`` (best-effort dedupe).

    A cache error returns True so an outage never blocks issue creation.
    """
    try:
        from django.core.cache import cache

        return bool(cache.add(f"gpu-crash-done:{event_id}", 1, timeout=_ONCE_TTL))
    except Exception:
        return True


@instrumented_task(
    name="sentry.tasks.gpu_crash.symbolicate_gpu_crash",
    namespace=gpu_crash_dump_tasks,
    # Comfortably exceeds teapot's worst case (timeout-seconds * max-attempts +
    # objectstore reads). Bounds how long a stuck task can hold a GPU worker.
    processing_deadline_duration=120,
    # Never retry — a failure just means "no GPU issue".
    at_most_once=True,
    silo_mode=SiloMode.CELL,
)
def symbolicate_gpu_crash(
    project_id: int,
    cpu_event_id: str,
    group_id: int | None = None,
    **kwargs: Any,
) -> None:
    """Decode a GPU crash dump via teapot and emit the GPU error event."""
    try:
        _run(project_id, cpu_event_id, group_id)
    except Exception as e:
        # Never re-raise — the task must always succeed so a poison event can't loop.
        metrics.incr("tasks.gpu_crash.error", tags={"reason": "unexpected"})
        logger.warning(
            "tasks.gpu_crash.unexpected_error", extra={"event_id": cpu_event_id, "error": repr(e)}
        )
        sentry_sdk.capture_exception(e)


def _run(project_id: int, cpu_event_id: str, group_id: int | None) -> None:
    from sentry.lang.native.gpu import emit_gpu_crash_event
    from sentry.lang.native.teapot import submit_to_teapot
    from sentry.lang.native.utils import (
        find_all_shader_debug_eventattachments,
        find_gpu_crash_dump_eventattachment,
    )
    from sentry.models.project import Project
    from sentry.services import eventstore

    # Kill switch + flag re-checked here so ops can halt already-queued work.
    if not options.get("teapot.enabled"):
        metrics.incr("tasks.gpu_crash.skipped", tags={"reason": "disabled"})
        return

    try:
        project = Project.objects.get_from_cache(id=project_id)
    except Project.DoesNotExist:
        metrics.incr("tasks.gpu_crash.skipped", tags={"reason": "project_missing"})
        return

    if not features.has("organizations:gpu-crash-symbolication", project.organization):
        metrics.incr("tasks.gpu_crash.skipped", tags={"reason": "flag_off"})
        return

    dump = find_gpu_crash_dump_eventattachment(project_id, cpu_event_id)
    if dump is None:
        metrics.incr("tasks.gpu_crash.skipped", tags={"reason": "attachment_missing"})
        return

    shader_atts = find_all_shader_debug_eventattachments(project_id, cpu_event_id)

    try:
        dump_raw = _RawAttachment(dump.name or "dump.nv-gpudmp", dump.getfile().read())
        shader_raw = [
            (uid, _RawAttachment(att.name or f"{uid}.nvdbg", att.getfile().read()))
            for uid, att in shader_atts
        ]
    except Exception as e:
        metrics.incr("tasks.gpu_crash.error", tags={"reason": "attachment_read"})
        logger.warning(
            "tasks.gpu_crash.attachment_read_failed",
            extra={"event_id": cpu_event_id, "error": repr(e)},
        )
        return

    metrics.incr(
        "tasks.gpu_crash.request",
        tags={"shader_debug_count": str(min(len(shader_raw), 10))},
    )
    with metrics.timer("tasks.gpu_crash.teapot"):
        response = submit_to_teapot(project, cpu_event_id, dump_raw, shader_raw)
    if response is None:
        metrics.incr("tasks.gpu_crash.teapot_unavailable")
        return

    # Only read for trace id / tags / release / env / sdk; best-effort.
    cpu_event = eventstore.backend.get_event_by_id(project_id, cpu_event_id, group_id=group_id)
    cpu_event_data = cpu_event.data if cpu_event is not None else {}
    if cpu_event is None:
        metrics.incr("tasks.gpu_crash.cpu_event_missing")

    # Claim only after a successful decode, so a transient failure above stays
    # retryable on redelivery. Also prevents a duplicate (billed) GPU event.
    if not _claim_once(cpu_event_id):
        metrics.incr("tasks.gpu_crash.skipped", tags={"reason": "already_processed"})
        return

    produced = emit_gpu_crash_event(project, cpu_event_id, cpu_event_data, response)
    metrics.incr(
        "tasks.gpu_crash.completed",
        tags={
            "produced": str(produced),
            "fault_category": response.get("fault_category") or "unknown",
        },
    )
