"""
Unit tests for the teapot client + GPU error-event construction.

Covers:
* `TeapotClient.symbolicate` — both multipart and JSON+storage_url paths
* `submit_to_teapot` wrapper — best-effort error swallowing
* `_build_gpu_error_event` / `emit_gpu_crash_event` — synthetic error event shape
* `_normalize_gpu_frames` — frame normalization edge cases
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any
from unittest import mock

import pytest
import requests

from sentry.lang.native.gpu import (
    _build_gpu_error_event,
    _normalize_gpu_frames,
    emit_gpu_crash_event,
)
from sentry.lang.native.processing import GPU_CRASH_DUMP_ATTACHMENT_TYPE
from sentry.lang.native.teapot import (
    TeapotClient,
    TeapotUnavailable,
    submit_to_teapot,
)


class _FakeProject:
    def __init__(self, id: int = 42, organization_id: int = 7) -> None:
        self.id = id
        self.organization_id = organization_id


class _FakeAttachment:
    """Stand-in for `sentry.attachments.CachedAttachment` in unit tests.

    Carries enough surface for the teapot client: bytes via `load_data`,
    the attachment `type`, the filename `name`, and an optional
    `stored_id`. When `stored_id` is set teapot's client routes via the
    JSON+storage_url path; otherwise it falls back to multipart.
    """

    def __init__(
        self,
        data: bytes,
        attachment_type: str = GPU_CRASH_DUMP_ATTACHMENT_TYPE,
        name: str = "dump.nv-gpudmp",
        stored_id: str | None = None,
    ) -> None:
        self._data = data
        self.type = attachment_type
        self.name = name
        self.stored_id = stored_id

    def load_data(self, _project: Any) -> bytes:
        return self._data


class _FakeResponse:
    def __init__(self, status_code: int, body: Any = None, raise_on_json: bool = False) -> None:
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = "" if isinstance(body, dict) or body is None else str(body)
        self._raise_on_json = raise_on_json

    def json(self) -> Any:
        if self._raise_on_json:
            raise ValueError("not JSON")
        return self._body


def _completed_response(**overrides: Any) -> dict[str, Any]:
    """Minimal `status=completed` teapot response with the new top-level fields.

    Tests that care about a specific field can override individual keys.
    """

    base: dict[str, Any] = {
        "status": "completed",
        "handler": "aftermath",
        "sdk_version": "2025.5.0",
        "decode_time_ms": 241,
        "fault_category": "shader_hang",
        "title": "GPU hang in vertex_02",
        "fingerprint": ["gpu", "shader_hang", "abc123"],
        "markers": [],
        "fault": {"type": "Timeout"},
        "gpu_state": {"device_name": "RTX 4090"},
        "shader_context": {
            "active_shaders": [{"shader_hash": "abc123", "shader_type": "Vertex"}],
        },
        "frames": [{"function": "Vertex", "module": "shader_abc123"}],
        "missing_difs": [],
    }
    base.update(overrides)
    return base


@contextlib.contextmanager
def _configured_teapot(url: str = "http://teapot.test") -> Iterator[None]:
    """Context manager: sets SENTRY_TEAPOT_URL so the client resolves an endpoint."""

    from django.conf import settings

    with mock.patch.object(settings, "SENTRY_TEAPOT_URL", url, create=True):
        yield


@pytest.fixture(autouse=True)
def _skip_retry_backoff() -> Iterator[None]:
    """Skip teapot's inter-retry backoff sleep so retry tests stay instant."""
    with mock.patch("sentry.lang.native.teapot.time.sleep"):
        yield


# ---------------------------------------------------------------------------
# _build_gpu_error_event
# ---------------------------------------------------------------------------


def test_build_gpu_error_event_shape() -> None:
    cpu_event_data = {
        "contexts": {"trace": {"trace_id": "a" * 32, "span_id": "b" * 16}},
        "release": "game@1.2.3",
        "environment": "prod",
        "tags": [["device", "pc"]],
        "sdk": {"name": "sentry.native", "version": "0.6.0"},
    }
    response = _completed_response(
        fault={"type": "PageFault", "virtual_address": "0xdeadbeef", "description": "write"},
        gpu_state={"device_name": "RTX 4090", "driver_version": "555.1", "api": "D3D12"},
        shader_context={"active_shaders": [{"shader_hash": "abc123", "shader_type": "Vertex"}]},
        frames=[{"function": "Vertex", "module": "shader_abc123"}],
    )

    event = _build_gpu_error_event("cpu-1", cpu_event_data, response)

    # Grouping: teapot's fingerprint verbatim → groups apart from the CPU crash.
    assert event["fingerprint"] == ["gpu", "shader_hang", "abc123"]
    # Trace-connected: shares the CPU event's trace id.
    assert event["contexts"]["trace"]["trace_id"] == "a" * 32
    # It's a native error event with the shader stacktrace.
    assert event["platform"] == "native"
    assert event["level"] == "fatal"
    exc = event["exception"]["values"][0]
    assert exc["type"] == "GPU hang in vertex_02"
    assert exc["mechanism"] == {"type": "gpu_crash", "handled": False}
    assert exc["stacktrace"]["frames"][0]["function"] == "Vertex"
    # gpu_crash context card data + cross-link tag.
    assert event["contexts"]["gpu_crash"]["fault_category"] == "shader_hang"
    assert event["tags"]["cpu_event_id"] == "cpu-1"
    assert event["tags"]["gpu.fault_category"] == "shader_hang"
    assert event["tags"]["device"] == "pc"  # merged from the CPU event
    assert event["release"] == "game@1.2.3"


def test_build_gpu_error_event_fingerprint_fallback() -> None:
    """A response without a fingerprint still groups deterministically."""
    response = _completed_response(fingerprint=[], fault_category="page_fault")
    event = _build_gpu_error_event("cpu-1", {}, response)
    assert event["fingerprint"] == ["gpu", "page_fault"]


def test_emit_gpu_crash_event_saves_via_event_manager() -> None:
    project = _FakeProject()
    with mock.patch("sentry.event_manager.EventManager") as em:
        produced = emit_gpu_crash_event(project, "cpu-1", {}, _completed_response())

    assert produced is True
    em.assert_called_once()
    em.return_value.normalize.assert_called_once()
    em.return_value.save.assert_called_once_with(project_id=project.id)


def test_emit_gpu_crash_event_skips_failed_status() -> None:
    project = _FakeProject()
    with mock.patch("sentry.event_manager.EventManager") as em:
        produced = emit_gpu_crash_event(project, "cpu-1", {}, {"status": "failed"})

    assert produced is False
    em.assert_not_called()


# ---------------------------------------------------------------------------
# TeapotClient — multipart wire format
# ---------------------------------------------------------------------------


def test_client_multipart_success() -> None:
    project = _FakeProject()
    dump = _FakeAttachment(b"dummy-dump-bytes", stored_id=None)
    expected = _completed_response()

    with (
        _configured_teapot(),
        mock.patch("sentry.lang.native.teapot.requests.post") as mock_post,
    ):
        mock_post.return_value = _FakeResponse(200, expected)

        result = TeapotClient(project, "abc").symbolicate(dump)

    assert result == expected
    assert mock_post.call_count == 1
    args, kwargs = mock_post.call_args
    assert args[0] == "http://teapot.test/symbolicate"
    # Identifiers carried as multipart form fields, NOT as a `sources` JSON
    # — we no longer send a source-config block.
    assert kwargs["data"]["event_id"] == "abc"
    assert kwargs["data"]["project_id"] == "42"
    assert kwargs["data"]["organization_id"] == "7"
    assert "sources" not in kwargs["data"]
    # Dump arrives as the canonical `upload_file` multipart field.
    files = dict(kwargs["files"])
    assert files["upload_file"][1] == b"dummy-dump-bytes"
    assert kwargs["headers"]["X-Teapot-Version"] == "1"
    assert kwargs["headers"]["X-Request-Id"] == "abc"
    # event_id doubles as the idempotency key so a retried task replays
    # teapot's cached decode instead of re-running it.
    assert kwargs["headers"]["Idempotency-Key"] == "abc"


def test_client_multipart_carries_shader_debug_attachments() -> None:
    """Each .nvdbg attachment becomes its own `nv_shader_debug.<uid>` field."""

    project = _FakeProject()
    dump = _FakeAttachment(b"dump-bytes")
    nvdbg1 = _FakeAttachment(
        b"nvdbg-bytes-1",
        attachment_type="event.nv_shader_debug",
        name="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.nvdbg",
    )
    nvdbg2 = _FakeAttachment(
        b"nvdbg-bytes-2",
        attachment_type="event.nv_shader_debug",
        name="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.nvdbg",
    )

    with (
        _configured_teapot(),
        mock.patch("sentry.lang.native.teapot.requests.post") as mock_post,
    ):
        mock_post.return_value = _FakeResponse(200, _completed_response())
        TeapotClient(project, "abc").symbolicate(
            dump,
            [
                ("a" * 32, nvdbg1),
                ("b" * 32, nvdbg2),
            ],
        )

    files = mock_post.call_args.kwargs["files"]
    # `files` is a list-of-tuples (the only way to repeat field names),
    # so map by the first element for ergonomic assertions.
    by_field = {field_name: payload for field_name, payload in files}
    assert "upload_file" in by_field
    assert by_field[f"nv_shader_debug.{'a' * 32}"][1] == b"nvdbg-bytes-1"
    assert by_field[f"nv_shader_debug.{'b' * 32}"][1] == b"nvdbg-bytes-2"


def test_client_retries_on_503() -> None:
    project = _FakeProject()
    dump = _FakeAttachment(b"dump")

    with (
        _configured_teapot(),
        mock.patch("sentry.lang.native.teapot.requests.post") as mock_post,
    ):
        mock_post.side_effect = [
            _FakeResponse(503),
            _FakeResponse(200, _completed_response(status="partial")),
        ]

        result = TeapotClient(project, "abc").symbolicate(dump)

    assert result["status"] == "partial"
    assert mock_post.call_count == 2


def test_client_retries_on_network_error() -> None:
    project = _FakeProject()
    dump = _FakeAttachment(b"dump")

    with (
        _configured_teapot(),
        mock.patch("sentry.lang.native.teapot.requests.post") as mock_post,
    ):
        mock_post.side_effect = [
            requests.ConnectionError("boom"),
            _FakeResponse(200, _completed_response()),
        ]

        result = TeapotClient(project, "abc").symbolicate(dump)

    assert result["status"] == "completed"
    assert mock_post.call_count == 2


def test_client_exhausts_retries() -> None:
    project = _FakeProject()
    dump = _FakeAttachment(b"dump")

    with (
        _configured_teapot(),
        mock.patch("sentry.lang.native.teapot.requests.post") as mock_post,
    ):
        mock_post.return_value = _FakeResponse(503)

        with pytest.raises(TeapotUnavailable):
            TeapotClient(project, "abc").symbolicate(dump)

    # Default teapot.max-attempts is 2 (kept low so a slow teapot can't pile up
    # work on the GPU task worker).
    assert mock_post.call_count == 2


def test_client_400_is_not_retried() -> None:
    project = _FakeProject()
    dump = _FakeAttachment(b"dump")

    with (
        _configured_teapot(),
        mock.patch("sentry.lang.native.teapot.requests.post") as mock_post,
    ):
        mock_post.return_value = _FakeResponse(400, "bad request")

        with pytest.raises(TeapotUnavailable):
            TeapotClient(project, "abc").symbolicate(dump)

    assert mock_post.call_count == 1


def test_client_non_json_body() -> None:
    project = _FakeProject()
    dump = _FakeAttachment(b"dump")

    with (
        _configured_teapot(),
        mock.patch("sentry.lang.native.teapot.requests.post") as mock_post,
    ):
        mock_post.return_value = _FakeResponse(200, "not-json", raise_on_json=True)

        with pytest.raises(TeapotUnavailable):
            TeapotClient(project, "abc").symbolicate(dump)


def test_client_missing_url_raises() -> None:
    from django.conf import settings

    with (
        mock.patch.object(settings, "SENTRY_TEAPOT_URL", None, create=True),
        mock.patch(
            "sentry.lang.native.teapot.options.get",
            lambda key: {} if key == "teapot.options" else None,
        ),
    ):
        with pytest.raises(TeapotUnavailable):
            TeapotClient(_FakeProject(), "abc")


def test_client_falls_back_to_options() -> None:
    from django.conf import settings

    project = _FakeProject()
    dump = _FakeAttachment(b"dump")
    with (
        mock.patch.object(settings, "SENTRY_TEAPOT_URL", None, create=True),
        mock.patch(
            "sentry.lang.native.teapot.options.get",
            lambda key: (
                {"url": "http://teapot-from-options.test"} if key == "teapot.options" else None
            ),
        ),
        mock.patch("sentry.lang.native.teapot.requests.post") as mock_post,
    ):
        mock_post.return_value = _FakeResponse(200, _completed_response())
        TeapotClient(project, "abc").symbolicate(dump)

    assert mock_post.call_args[0][0] == "http://teapot-from-options.test/symbolicate"


# ---------------------------------------------------------------------------
# TeapotClient — JSON + storage_url + storage_token (objectstore path)
# ---------------------------------------------------------------------------


def test_client_uses_json_path_when_all_attachments_stored() -> None:
    """When every attachment has `stored_id`, pass URLs not bytes.

    Mirrors `Symbolicator.process_minidump`'s objectstore path. Teapot
    fetches the bytes directly from objectstore using the minted tokens.
    Bytes never pass through the Sentry worker.
    """

    project = _FakeProject()
    dump = _FakeAttachment(b"dump-bytes-should-not-be-sent", stored_id="dump-obj-id")
    nvdbg = _FakeAttachment(
        b"nvdbg-bytes-should-not-be-sent",
        attachment_type="event.nv_shader_debug",
        name="cafebabecafebabecafebabecafebabe.nvdbg",
        stored_id="nvdbg-obj-id",
    )

    fake_session = mock.Mock()
    fake_session.mint_token.side_effect = ["token-dump", "token-nvdbg"]

    with (
        _configured_teapot(),
        mock.patch(
            "sentry.lang.native.teapot.get_attachments_session",
            return_value=fake_session,
        ),
        mock.patch(
            "sentry.lang.native.teapot.get_symbolicator_url",
            side_effect=lambda _sess, key: f"http://objectstore/{key}",
        ),
        mock.patch("sentry.lang.native.teapot.requests.post") as mock_post,
    ):
        mock_post.return_value = _FakeResponse(200, _completed_response())

        TeapotClient(project, "abc").symbolicate(dump, [("c" * 32, nvdbg)])

    # JSON path: `files` empty, Content-Type set, body is JSON-encoded.
    kwargs = mock_post.call_args.kwargs
    assert kwargs.get("files") is None
    assert kwargs["headers"]["Content-Type"] == "application/json"
    import orjson

    body = orjson.loads(kwargs["data"])
    assert body["event_id"] == "abc"
    assert body["dump"]["storage_url"] == "http://objectstore/dump-obj-id"
    assert body["dump"]["storage_token"] == "token-dump"
    assert len(body["shader_debug_info"]) == 1
    assert body["shader_debug_info"][0]["uid"] == "c" * 32
    assert body["shader_debug_info"][0]["storage_url"] == "http://objectstore/nvdbg-obj-id"
    assert body["shader_debug_info"][0]["storage_token"] == "token-nvdbg"


def test_client_falls_back_to_multipart_when_any_attachment_lacks_stored_id() -> None:
    """Mixed-state attachments → multipart (we can't combine wire formats)."""

    project = _FakeProject()
    dump = _FakeAttachment(b"dump-bytes", stored_id="dump-obj-id")
    # Second attachment has NO stored_id — forces multipart path.
    nvdbg = _FakeAttachment(
        b"nvdbg-bytes",
        attachment_type="event.nv_shader_debug",
        name="cafebabecafebabecafebabecafebabe.nvdbg",
        stored_id=None,
    )

    with (
        _configured_teapot(),
        mock.patch("sentry.lang.native.teapot.requests.post") as mock_post,
        mock.patch("sentry.lang.native.teapot.get_attachments_session") as mock_session_fn,
    ):
        mock_post.return_value = _FakeResponse(200, _completed_response())
        TeapotClient(project, "abc").symbolicate(dump, [("c" * 32, nvdbg)])

    # Objectstore session is never opened because the mixed-state check
    # routes to multipart immediately.
    assert mock_session_fn.call_count == 0
    # Multipart: `files` populated with inline bytes.
    files = mock_post.call_args.kwargs["files"]
    by_field = {field_name: payload for field_name, payload in files}
    assert by_field["upload_file"][1] == b"dump-bytes"
    assert by_field[f"nv_shader_debug.{'c' * 32}"][1] == b"nvdbg-bytes"


# ---------------------------------------------------------------------------
# submit_to_teapot (best-effort wrapper)
# ---------------------------------------------------------------------------


def test_submit_to_teapot_success() -> None:
    project = _FakeProject()
    dump = _FakeAttachment(b"dump")
    with (
        _configured_teapot(),
        mock.patch("sentry.lang.native.teapot.requests.post") as mock_post,
    ):
        mock_post.return_value = _FakeResponse(200, _completed_response())
        result = submit_to_teapot(project, "abc", dump, [])
    assert result is not None
    assert result["status"] == "completed"


def test_submit_to_teapot_returns_none_when_unavailable() -> None:
    from django.conf import settings

    with (
        mock.patch.object(settings, "SENTRY_TEAPOT_URL", None, create=True),
        mock.patch(
            "sentry.lang.native.teapot.options.get",
            lambda key: None,
        ),
    ):
        assert submit_to_teapot(_FakeProject(), "abc", _FakeAttachment(b"dump"), []) is None


def test_submit_to_teapot_swallows_unexpected() -> None:
    project = _FakeProject()
    dump = _FakeAttachment(b"dump")
    with (
        _configured_teapot(),
        mock.patch("sentry.lang.native.teapot.TeapotClient") as mock_client,
        mock.patch("sentry.lang.native.teapot.sentry_sdk.capture_exception") as cap,
    ):
        mock_client.return_value.symbolicate.side_effect = RuntimeError("unexpected")
        assert submit_to_teapot(project, "abc", dump, []) is None
        cap.assert_called_once()


def test_normalize_gpu_frames_tolerates_non_mapping_data() -> None:
    """teapot's `frames[].data` is external and may not be a dict.

    A truthy non-dict (string/list) must not crash `dict(...)` or the
    `shader_hash` lookup — the frame is still normalized, just without `data`.
    """

    frames = [
        {"function": "vertex", "data": "not-a-dict"},
        {"function": "pixel", "data": [1, 2, 3]},
        {"function": "compute", "data": {"shader_hash": 12345}},  # non-str hash
        {"function": "ok", "data": {"shader_hash": "abc123"}},
    ]

    result = _normalize_gpu_frames(frames)

    assert [f["function"] for f in result] == ["vertex", "pixel", "compute", "ok"]
    # Non-dict data is dropped (no crash); no synthetic package is derived.
    assert "package" not in result[0]
    assert "package" not in result[1]
    # Non-str shader_hash is ignored rather than crashing `.startswith`.
    assert "package" not in result[2]
    # A well-formed str shader_hash still produces a package.
    assert result[3]["package"] == "shader_abc123"
