"""GPU crash dump handling — teapot response → synthetic native error event.

Builds a standalone native ERROR event from teapot's decode and saves it via
`EventManager.save`, so the GPU crash becomes its own error issue: grouped by
teapot's fingerprint, trace-connected to the CPU crash, and billed like any
error event. It's a sibling of the CPU crash, not an enrichment of it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sentry.utils import metrics

logger = logging.getLogger(__name__)

# NVIDIA Aftermath GPU crash dump attachment, decoded by teapot.
GPU_CRASH_DUMP_ATTACHMENT_TYPE = "event.nv_gpudmp"


# ─────────────────────────── public entry point ────────────────────────────


def emit_gpu_crash_event(
    project: Any,
    cpu_event_id: str,
    cpu_event_data: Mapping[str, Any],
    response: Mapping[str, Any],
) -> bool:
    """Save a standalone native ERROR event for the GPU crash.

    Entry point for the async GPU task. Returns True iff an event was saved;
    ``failed``/unknown statuses are a no-op. May raise — the task wraps this.
    ``cpu_event_data`` is only read for the trace id, tags, release, env, sdk.
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
    """Flatten teapot's response into a ``contexts.gpu_crash`` dict.

    Top-level scalars render inline in the context card; nested objects would
    collapse behind ``> { N items }``, so we flatten fault / gpu_state out.
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

    The fingerprint is teapot's, verbatim, so GPU crashes group independently of
    the CPU crash and stay stable across randomised fault addresses. The
    exception type/value drive the title; the CPU trace context links the two.
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
    """Map teapot's `markers` to breadcrumbs.

    For non-shader crashes these are often the only actionable signal (the
    CPU-side context that submitted the faulting GPU work).
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
    """Map teapot's ``frames[]`` to Sentry stacktrace frames.

    Mostly a pass-through of pre-populated fields, plus two normalisations:
    force ``symbolicator_status=symbolicated`` (shader frames have no debug
    image, so the walker would otherwise mark them missing), and synthesise a
    ``package`` from the shader hash for the module column.
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
        # `data` is external; only trust it if it's a mapping (a truthy
        # str/list would crash `dict(...)` and the `.get()` below).
        raw_data = raw.get("data")
        if not isinstance(raw_data, dict):
            raw_data = {}
        if raw_data:
            frame["data"] = dict(raw_data)

        shader_hash = raw_data.get("shader_hash")
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
