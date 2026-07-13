"""GroupType definition for GPU crash issues surfaced by teapot.

A GPU crash gets its own issue — distinct from any CPU exception on the
same event — so engineers can search, silence, and triage shader-side
faults as a cohort. Grouping is by `{fault.type, shader_hash}` via the
occurrence fingerprint; see `process_gpu_crash_dump` for the producer.

The type is auto-registered when this module is imported. `processing.py`
imports it at the call site so the class definition — and therefore the
registration — runs before any `IssueOccurrence` is produced.
"""

from __future__ import annotations

from dataclasses import dataclass

from sentry.issues.grouptype import GroupCategory, GroupType, NotificationConfig
from sentry.ratelimits.sliding_windows import Quota
from sentry.types.group import PriorityLevel


@dataclass(frozen=True)
class GpuCrashGroupType(GroupType):
    # ID 9100 reserves 9100-9199 for GPU-related group types (e.g. a
    # future split of NVIDIA / DirectX / AMD). 9001 is held by
    # SendTestNotification; starting a new bucket at 9100 keeps this clean.
    type_id = 9100
    slug = "gpu_crash"
    description = "GPU Crash"
    # OUTAGE category — a TDR-style GPU hang or device reset is the most
    # literal outage signal we have: the graphics subsystem is unresponsive.
    # Must also be a non-ERROR category because the search pipeline at
    # src/sentry/issues/search.py:225-234 routes `GroupCategory.ERROR` to
    # the Errors Snuba dataset while everything else goes through the
    # IssuePlatform dataset where our occurrences actually live. Staying
    # non-ERROR is what makes the issue show up in the default feed.
    category = GroupCategory.OUTAGE.value
    category_v2 = GroupCategory.OUTAGE.value
    # Dark launch. A FULL rollout requires enabling all of these together —
    # flipping only the symbolication flag produces occurrences that the
    # consumer then drops as `dropped_feature_disabled`, so no GPU issue appears:
    #   * organizations:gpu-crash-symbolication          (producer / teapot gate)
    #   * organizations:issue-gpu-crash-ingest           (auto: allow_ingest)
    #   * organizations:issue-gpu-crash-post-process-group (auto)
    #   * organizations:issue-gpu-crash-visible          (auto: UI visibility)
    # The last three are auto-registered by the issue platform because
    # `released=False`; set `released=True` (and drop the symbolication flag)
    # once GPU crashes ship to everyone.
    released = False
    default_priority = PriorityLevel.HIGH
    # The base GroupType default is Quota(3600, 60, 5) — 5 new groups per
    # project per hour. That spam guard is sized for derived/heuristic issue
    # kinds and is far too tight for a crash-heavy native title, where distinct
    # GPU faults (per fault_category + shader_hash) legitimately exceed it.
    # Match the error group type's headroom so we don't silently drop real GPU
    # issues; this throttles group *creation* only and has no billing effect.
    creation_quota = Quota(3600, 60, 1000)  # 1000 per hour, sliding window of 60s
    # A GPU crash is never self-healing — disable auto-resolve and
    # escalation detection until the product story catches up.
    enable_auto_resolve = False
    enable_escalation_detection = False
    notification_config = NotificationConfig(context=[])
