"""Read a published channel's performance numbers from Meta and store them.

One read is one VariantMetricSnapshot. Each metric ends with a number or with a state
saying why there is none (see ``apps.publishing.metrics.State``), so the UI never shows
a missing number as 0. Which metrics to ask for comes from the registry.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from .metrics import State, metrics_for, next_collection_at
from .models import ContentPostVariant, VariantMetricSnapshot
from .providers.errors import MetaPublishError

logger = logging.getLogger(__name__)

RATE_LIMIT_BACKOFF = timedelta(minutes=30)


class MetricsRateLimited(Exception):
    """Meta asked us to slow down; read again after RATE_LIMIT_BACKOFF."""


def _state_for(exc: Exception) -> str:
    if not isinstance(exc, MetaPublishError):
        return State.FAILED
    if exc.token_invalid:
        return State.NEEDS_PERMISSION
    message = str(exc).lower()
    if exc.code == 10 and "not enough viewers" in message:
        return State.BELOW_THRESHOLD
    if exc.code == 10 or (exc.code is not None and 200 <= exc.code <= 299):
        return State.NEEDS_PERMISSION
    if exc.code == 100 and exc.subcode != 33:  # 33: the object no longer exists
        return State.UNSUPPORTED
    return State.FAILED


def _raise_if_rate_limited(exc: Exception) -> None:
    from .models import ContentPostVariant as V

    if isinstance(exc, MetaPublishError) and exc.error_type == V.ErrorType.RATE_LIMIT:
        raise MetricsRateLimited(str(exc)) from exc


def _field_value(platform: str, key: str, data: dict):
    if platform == "facebook":
        if key == "comments":
            return ((data.get("comments") or {}).get("summary") or {}).get(
                "total_count"
            )
        if key == "shares":
            # Facebook leaves ``shares`` out of a post nobody has shared.
            return (data.get("shares") or {}).get("count", 0)
        return None
    graph = {"likes": "like_count", "comments": "comments_count"}[key]
    return data.get(graph)


def _provider(variant: ContentPostVariant):
    from .services import _fb_provider_for_post, _ig_provider_for_post

    if variant.platform == "instagram":
        return _ig_provider_for_post(variant.post)
    found = _fb_provider_for_post(variant.post)
    return found[0] if found else None


def _target_id(variant: ContentPostVariant, provider) -> str:
    """The object insights are read from: the IG media, or the Facebook Page post."""
    if variant.platform == "instagram":
        return variant.publication_id
    cached = (variant.metadata or {}).get("fb_post_id")
    if cached:
        return cached
    post_id = provider.resolve_post_id(variant.publication_id)
    if post_id:
        variant.metadata = {**(variant.metadata or {}), "fb_post_id": post_id}
        ContentPostVariant.objects.filter(pk=variant.pk).update(
            metadata=variant.metadata
        )
    return post_id


def collect_variant_metrics(variant: ContentPostVariant) -> VariantMetricSnapshot:
    """Read every metric this channel supports, store a snapshot and plan the next read.

    Raises MetricsRateLimited when Meta throttles us; the caller reads again later.
    """
    from .services import _mark_token_invalid

    post = variant.post
    channel = variant.get_platform_display()
    defs = metrics_for(variant.platform, post.media_type)
    values: dict[str, int | None] = {m.key: None for m in defs}
    states: dict[str, str] = {m.key: State.PENDING for m in defs}
    error, error_type = "", ""
    stop = False

    def fail_all(keys, exc):
        nonlocal error, error_type, stop
        _raise_if_rate_limited(exc)
        state = _state_for(exc)
        for key in keys:
            states[key] = state
        error = str(exc)[:500]
        error_type = getattr(exc, "error_type", "") or "unknown"
        if isinstance(exc, MetaPublishError):
            if exc.token_invalid:
                _mark_token_invalid(post, variant.platform)
            if exc.code == 100 and exc.subcode == 33:
                error = (
                    f"The post can't be found on {channel}; it may have been deleted."
                )
                stop = True

    provider = _provider(variant)
    target = ""
    if provider is None:
        for key in states:
            states[key] = State.NEEDS_PERMISSION
        error = f"{channel} isn't connected, or its connection expired."
        error_type = ContentPostVariant.ErrorType.PERMISSION
    else:
        try:
            target = _target_id(variant, provider)
            if not target:
                for key in states:
                    states[key] = State.FAILED
                error = f"Couldn't find the {channel} post to read its numbers."
                error_type = ContentPostVariant.ErrorType.VALIDATION
        except Exception as exc:
            fail_all(list(states), exc)

    if target:
        fields = [m for m in defs if m.source == "field"]
        insights = [m for m in defs if m.source == "insights"]
        read_fields = (
            provider.fetch_fields
            if variant.platform == "facebook"
            else provider.fetch_media_fields
        )
        if fields:
            try:
                data = read_fields(target, [m.graph_name for m in fields])
                for m in fields:
                    value = _field_value(variant.platform, m.key, data)
                    if value is not None:
                        values[m.key] = int(value)
                        states[m.key] = State.AVAILABLE
            except Exception as exc:
                fail_all([m.key for m in fields], exc)
        if insights and not stop:
            _collect_insights(
                variant, provider, target, insights, values, states, fail_all
            )

    now = timezone.now()
    snapshot = VariantMetricSnapshot.objects.create(
        variant=variant,
        collected_at=now,
        values=values,
        states=states,
        error_type=error_type,
        error=error,
    )
    variant.metrics_latest = {"values": values, "states": states, "error": error}
    variant.metrics_collected_at = now
    variant.metrics_next_at = (
        None
        if stop
        else next_collection_at(variant.published_at or now, post.media_type, now)
    )
    ContentPostVariant.objects.filter(pk=variant.pk).update(
        metrics_latest=variant.metrics_latest,
        metrics_collected_at=now,
        metrics_next_at=variant.metrics_next_at,
    )
    return snapshot


def _collect_insights(variant, provider, target, defs, values, states, fail_all):
    """Ask for every insight in one call. An invalid-metric error names no metric, so
    on that error each metric is asked for on its own to find the ones that work."""
    fetch = (
        provider.fetch_post_insights
        if variant.platform == "facebook"
        else provider.fetch_media_insights
    )

    def store(reported, batch):
        for m in batch:
            if m.graph_name in reported:
                values[m.key] = reported[m.graph_name]
                states[m.key] = State.AVAILABLE

    try:
        store(fetch(target, [m.graph_name for m in defs]), defs)
        return
    except Exception as exc:
        if not (
            isinstance(exc, MetaPublishError)
            and exc.code == 100
            and exc.subcode != 33
            and len(defs) > 1
        ):
            fail_all([m.key for m in defs], exc)
            return
    for m in defs:
        try:
            store(fetch(target, [m.graph_name]), [m])
        except Exception as exc:
            fail_all([m.key], exc)
