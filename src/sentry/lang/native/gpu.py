"""GPU crash dump handling — teapot response → synthetic native error event.

Self-contained module that owns everything specific to the GPU crash flow:

* attachment-type detection (`find_gpu_crash_dump_attachment` +
  `find_all_shader_debug_attachments` in utils)
* the HTTP call-out to teapot (in `teapot.py`)
* building a standalone native ERROR event from teapot's decode and saving it
  via `EventManager.save`, so the GPU crash becomes its own error issue —
  grouped by teapot's fingerprint, trace-connected to the CPU crash, and billed
  like any error event.

The GPU event is a sibling of the CPU crash, not an enrichment of it: it's a
fresh error event with the shader stacktrace, so it groups and renders on its
own while sharing the CPU event's `contexts.trace.trace_id`.

`processing.py` re-exports `GPU_CRASH_DUMP_ATTACHMENT_TYPE` for the attachment
finders in `utils.py`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sentry.utils import metrics

logger = logging.getLogger(__name__)

# Attachment type used for NVIDIA Aftermath GPU crash dumps. Processed by
# teapot (sibling service to Symbolicator).
GPU_CRASH_DUMP_ATTACHMENT_TYPE = "event.nv_gpudmp"


# ─────────────────────────── public entry point ────────────────────────────


def emit_gpu_crash_event(
    project: Any,
    cpu_event_id: str,
    cpu_event_data: Mapping[str, Any],
    response: Mapping[str, Any],
) -> bool:
    """Save a standalone native ERROR event for the GPU crash.

    Public entry point for the async GPU task (``sentry.tasks.gpu_crash``).
    Builds an error event from teapot's decode and persists it via
    ``EventManager.save`` — so the GPU crash becomes its own error issue,
    grouped by teapot's fingerprint and trace-connected to the CPU crash, and
    consumes error quota like any ingested event. (That's bounded: a GPU event
    only exists because a CPU error was already ingested, so at worst it doubles
    an already-billed native crash.)

    ``cpu_event_data`` is the saved CPU event's data — used only to source the
    trace id, tags, release, environment, and sdk. Returns True iff an event was
    saved. ``failed``/unknown statuses are a no-op: teapot couldn't decode, so
    there's no useful fingerprint. May raise — the task wraps this.
    """
    from sentry.event_manager import EventManager

    status = response.get("status")
    if status not in ("completed", "partial"):
        metrics.incr("process.gpu.event.skipped", tags={"status": status or "unknown"})
        return False

    event_data = _build_gpu_error_event(cpu_event_id, cpu_event_data, response)
    manager = EventManager(event_data)
    manager.normalize()
    manager.save(project_id=project.id)
    metrics.incr(
        "process.gpu.event.saved",
        tags={"fault_category": response.get("fault_category") or "unknown"},
    )
    return True


# ─────────────────────────── event construction ────────────────────────────


def _build_flat_gpu_context(response: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten teapot's response into a UI-friendly ``contexts.gpu_crash`` dict.

    Sentry's context renderer surfaces top-level scalars directly but
    collapses nested objects under ``> { N items }``. Flattening the
    fault / gpu_state into scalar fields means every useful value shows
    up without the user having to expand.
    """

    fault = response.get("fault") or {}
    gpu_state = response.get("gpu_state") or {}
    shader_context = response.get("shader_context") or {}
    active_shaders = shader_context.get("active_shaders") or []
    primary_shader = active_shaders[0] if active_shaders else {}

    flat: dict[str, Any] = {
        "type": "gpu_crash",
        "status": response.get("status"),
        # Top-level teapot fields — drive grouping + UX. Always populated
        # in successful responses; missing only for `failed` ones.
        "fault_category": response.get("fault_category"),
        "title": response.get("title"),
        "handler": response.get("handler"),
        "sdk_version": response.get("sdk_version"),
        "decode_time_ms": response.get("decode_time_ms"),
        # Fault
        "fault_type": fault.get("type"),
        "fault_description": fault.get("description"),
        "fault_code": fault.get("code"),
        "virtual_address": fault.get("virtual_address"),
        "access_type": fault.get("access_type"),
        # GPU / host
        "device_name": gpu_state.get("device_name"),
        "device_status": gpu_state.get("device_status"),
        "driver_version": gpu_state.get("driver_version"),
        "graphics_api": gpu_state.get("api"),
        "os_version": gpu_state.get("os_version"),
        "application_name": gpu_state.get("application_name"),
        "engine_reset": gpu_state.get("engine_reset"),
        "adapter_reset": gpu_state.get("adapter_reset"),
        # Shader (when present)
        "shader_hash": primary_shader.get("shader_hash"),
        "shader_type": primary_shader.get("shader_type"),
        "shader_debug_info_uid": primary_shader.get("shader_debug_info_uid"),
        # Missing debug files (surface count so users see when they need
        # to fix their SDK integration).
        "missing_dif_count": len(response.get("missing_difs") or []),
    }
    warnings = response.get("warnings") or []
    if warnings:
        flat["warnings"] = warnings
    return {k: v for k, v in flat.items() if v is not None}


def _build_gpu_error_event(
    cpu_event_id: str,
    cpu_event_data: Mapping[str, Any],
    response: Mapping[str, Any],
) -> dict[str, Any]:
    """Assemble the raw native ERROR event payload for a decoded GPU crash.

    Grouping is delegated to teapot: the `fingerprint` array is used verbatim as
    the event fingerprint, so GPU crashes group independently of the CPU crash
    (and stay stable across crash shapes — e.g. all page faults on
    `StreamingTextureAtlas` group together regardless of randomised VA). The
    exception `type`/`value` drive the issue title; the CPU event's trace
    context links the two issues in the trace view.
    """

    fault = response.get("fault") or {}
    gpu_state = response.get("gpu_state") or {}
    shader_context = response.get("shader_context") or {}
    active_shaders = shader_context.get("active_shaders") or []
    primary_shader = active_shaders[0] if active_shaders else {}

    category = response.get("fault_category") or "unknown"
    fingerprint = list(response.get("fingerprint") or [])
    if not fingerprint:
        fingerprint = ["gpu", category]

    # Exception drives the issue title; the fingerprint above drives grouping.
    exc_type = response.get("title") or f"GPU crash ({category})"
    subtitle_parts: list[str] = []
    if fault.get("virtual_address"):
        subtitle_parts.append(f"@ {fault['virtual_address']}")
    if gpu_state.get("device_name"):
        subtitle_parts.append(str(gpu_state["device_name"]))
    if gpu_state.get("driver_version"):
        subtitle_parts.append(f"driver {gpu_state['driver_version']}")
    exc_value = " · ".join(subtitle_parts) or fault.get("description") or category

    frames = response.get("frames") or []
    markers = response.get("markers") or []
    now = datetime.now(timezone.utc)

    contexts: dict[str, Any] = {"gpu_crash": _build_flat_gpu_context(response)}
    trace_context = ((cpu_event_data.get("contexts") or {}).get("trace") or {}).copy()
    if trace_context:
        contexts["trace"] = trace_context
    if gpu_state.get("device_name"):
        contexts["gpu"] = {
            "name": gpu_state["device_name"],
            "driver_version": gpu_state.get("driver_version"),
            "vendor_name": "NVIDIA",
            "api": gpu_state.get("api"),
        }
    if gpu_state.get("os_version"):
        contexts["os"] = {"name": gpu_state["os_version"], "type": "os"}
    if gpu_state.get("application_name"):
        contexts["app"] = {"app_name": gpu_state["application_name"], "type": "app"}

    event_data: dict[str, Any] = {
        "event_id": uuid.uuid4().hex,
        "platform": "native",
        "level": "fatal",
        "timestamp": now.timestamp(),
        # Verbatim teapot fingerprint → GPU crashes group on their own, apart
        # from the CPU crash's stacktrace grouping.
        "fingerprint": fingerprint,
        "exception": {
            "values": [
                {
                    "type": exc_type,
                    "value": exc_value,
                    "stacktrace": {"frames": _normalize_gpu_frames(frames)},
                    "mechanism": {"type": "gpu_crash", "handled": False},
                }
            ]
        },
        "contexts": contexts,
        "tags": _merge_cpu_tags(
            cpu_event_data.get("tags"),
            {
                "gpu.fault_category": category,
                "gpu.fault_type": fault.get("type") or "Unknown",
                "cpu_event_id": cpu_event_id,
                **(
                    {"gpu.shader_hash": primary_shader["shader_hash"]}
                    if primary_shader.get("shader_hash")
                    else {}
                ),
                **(
                    {"gpu.shader_type": primary_shader["shader_type"]}
                    if primary_shader.get("shader_type")
                    else {}
                ),
            },
        ),
        "release": cpu_event_data.get("release"),
        "environment": cpu_event_data.get("environment"),
        "sdk": cpu_event_data.get("sdk") or {"name": "teapot", "version": "0.1.0"},
    }
    breadcrumbs = _markers_to_breadcrumbs(markers)
    if breadcrumbs:
        event_data["breadcrumbs"] = {"values": breadcrumbs}
    return event_data


# ─────────────────────────── frame / tag helpers ──────────────────────────


def _merge_cpu_tags(
    cpu_tags: Any,
    extra: Mapping[str, str],
) -> dict[str, str]:
    """Merge tags from the CPU event (dict or list-of-pairs form) with extras."""

    merged: dict[str, str] = {}
    if isinstance(cpu_tags, dict):
        for k, v in cpu_tags.items():
            if k is not None and v is not None:
                merged[str(k)] = str(v)
    elif isinstance(cpu_tags, list):
        for entry in cpu_tags:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                key, value = entry
                if key is not None and value is not None:
                    merged[str(key)] = str(value)
            elif isinstance(entry, dict) and "key" in entry and "value" in entry:
                merged[str(entry["key"])] = str(entry["value"])
    for k, v in extra.items():
        if v is not None:
            merged[k] = str(v)
    return merged


def _markers_to_breadcrumbs(markers: Any) -> list[dict[str, Any]]:
    """Map teapot's `markers` array to Sentry event breadcrumbs.

    For non-shader crashes (page faults, OOM, device reset, ...) these
    are usually the only actionable signal — Aftermath captures the
    CPU-side context that submitted the faulting GPU work via
    `Aftermath markers` and `UserDefined+N` description keys. Surfacing
    them as breadcrumbs puts them in the standard issue-page timeline
    rather than buried in a context blob.
    """

    if not isinstance(markers, list):
        return []
    out: list[dict[str, Any]] = []
    for m in markers:
        if not isinstance(m, dict):
            continue
        kind = m.get("kind") or "marker"
        label = m.get("label") or kind
        data = m.get("data")
        msg = label if isinstance(data, (dict, list)) else f"{label}: {data}"
        out.append(
            {
                "category": f"gpu.{kind}",
                "message": str(msg)[:512],
                "type": "info",
                "level": "info",
                "data": data if isinstance(data, (dict, list)) else None,
            }
        )
    return out


def _normalize_gpu_frames(teapot_frames: Any) -> list[dict[str, Any]]:
    """Map teapot's ``frames[]`` to Sentry event stacktrace frames.

    Teapot now emits frames with all the relevant fields pre-populated —
    `function`, `module`, `filename`, `abs_path`, `lineno`, and
    `data.synthetic` — so this is mostly a pass-through with a couple
    of normalisations:

    * Mark every frame `symbolicator_status=symbolicated` so Sentry's
      UI doesn't paint the warning triangle (no `debug_meta.images`
      entry exists for shader frames; without the explicit status the
      symbolicator walker marks them `missing`).
    * Synthesise a `package` from the shader hash so the frame's
      module column renders something useful.

    Synthetic frames (`data.synthetic = true`, emitted for non-shader
    crashes like page faults) carry the same metadata shape; the only
    difference is they have no `data.shader_hash`.
    """

    if not isinstance(teapot_frames, list):
        return []

    normalized: list[dict[str, Any]] = []
    for raw in teapot_frames:
        if not isinstance(raw, dict):
            continue

        frame: dict[str, Any] = {}
        for src, dst in (
            ("function", "function"),
            ("module", "module"),
            ("filename", "filename"),
            ("abs_path", "abs_path"),
            ("lineno", "lineno"),
            ("colno", "colno"),
            ("instruction_addr", "instruction_addr"),
            ("pre_context", "pre_context"),
            ("context_line", "context_line"),
            ("post_context", "post_context"),
        ):
            value = raw.get(src)
            if value is not None:
                frame[dst] = value
        # `data` comes from teapot's external response; only trust it if it's a
        # mapping. A truthy non-dict (str/list) would crash both `dict(...)` and
        # the `.get()` below.
        raw_data = raw.get("data")
        if not isinstance(raw_data, dict):
            raw_data = {}
        if raw_data:
            frame["data"] = dict(raw_data)

        # Synthesise a package from the shader hash so the module column
        # renders something useful (only for real shader frames; synthetic
        # frames already have a meaningful `module` like "Graphics").
        shader_hash = raw_data.get("shader_hash")
        # shader_hash comes from teapot's response; don't assume it's a str.
        if isinstance(shader_hash, str) and shader_hash and not frame.get("package"):
            frame["package"] = (
                shader_hash if shader_hash.startswith("shader_") else f"shader_{shader_hash}"
            )
        if not frame.get("module") and frame.get("package"):
            frame["module"] = frame["package"]

        frame.setdefault("data", {})
        frame["data"].setdefault("symbolicator_status", "symbolicated")
        frame.setdefault("in_app", True)
        normalized.append(frame)
    return normalized
