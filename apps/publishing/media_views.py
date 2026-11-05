"""Media library pages (apps.publishing.media_library). Behind the ``cs_media_library``
release gate: until it is released, ReleaseGateMiddleware 404s every URL under
``publishing/media/``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Page, Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account
from apps.core.htmx import (
    action_done,
    is_background_post,
    overlay_done,
    overlay_or_page,
)
from apps.core.module_gate import module_required

from . import direct_upload, media_library
from .models import CaptionTemplate, HashtagSet, MediaAsset

if TYPE_CHECKING:
    from django.db.models.expressions import Combinable

PAGE_SIZE = 48


def _account_or_none(request):
    return get_current_account(request)


SHOW_CHOICES = [
    ("all", "All"),
    ("image", "Images"),
    ("video", "Videos"),
    ("unused", "Unused"),
    ("archived", "Archived"),
]
SORT_CHOICES = [
    ("new", "Newest"),
    ("used", "Most used"),
    ("name", "Name"),
    ("size", "Size"),
]


def _badge(asset) -> str:
    """The small label on a file's picture: "Video 0:42", "Reel cover", "PNG"."""
    if asset.kind == "video":
        if asset.duration:
            seconds = round(asset.duration)
            return f"Video {seconds // 60}:{seconds % 60:02d}"
        return "Video"
    if asset.source == MediaAsset.Source.COVER:
        return "Reel cover"
    if asset.source == MediaAsset.Source.EDIT:
        return "Edited"
    sub = (asset.mime or "").rpartition("/")[2]
    ext = sub if sub else asset.name.rpartition(".")[2] if "." in asset.name else ""
    return {"jpeg": "JPG", "quicktime": "MOV"}.get(ext.lower(), ext.upper()[:4])


def _decorate(account, assets, counts=None) -> None:
    """What the grid and the details show beyond the model: uses, a clean name, the
    badge, and the older file it looks like a copy of."""
    if counts is None:
        counts = media_library.usage_counts(account, assets)
    dups = media_library.duplicates(account, assets)
    for asset in assets:
        asset.used_in = counts.get(asset.pk, 0)  # type: ignore[attr-defined]
        asset.display = media_library.display_name(asset.name)  # type: ignore[attr-defined]
        asset.badge = _badge(asset)  # type: ignore[attr-defined]
        asset.duplicate_of = dups.get(asset.pk)  # type: ignore[attr-defined]


@login_required
@module_required("content_studio")
def library(request):
    """The library as a gallery: one filter row (search, All/Images/Videos/Unused/
    Archived, sort, grid or list) whose results region is swapped in place, and a count
    of what's in it. ``?kind=`` and ``?archived=1`` from older links still work."""
    from django.db.models import Count, F, Sum
    from django.db.models.functions import Lower

    account = _account_or_none(request)
    if account is None:
        return redirect("dashboard")
    show = request.GET.get("show", "")
    if show not in dict(SHOW_CHOICES):
        show = (
            "archived"
            if request.GET.get("archived") == "1"
            else request.GET.get("kind", "")
            if request.GET.get("kind") in ("image", "video")
            else "all"
        )
    sort = request.GET.get("sort", "new")
    if sort not in dict(SORT_CHOICES):
        sort = "new"
    view = "list" if request.GET.get("view") == "list" else "grid"
    tag = request.GET.get("tag", "").strip().lower()
    q = request.GET.get("q", "").strip()
    archived = show == "archived"

    live = MediaAsset.objects.filter(account=account, archived_at__isnull=True)
    totals = live.aggregate(files=Count("pk"), size=Sum("size_bytes"))
    qs = MediaAsset.objects.filter(account=account, archived_at__isnull=not archived)
    if show in ("image", "video"):
        qs = qs.filter(kind=show)
    if tag:
        qs = qs.filter(tags__icontains=f'"{tag}"')
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(tags__icontains=q.lower()))
    all_counts = (
        media_library.usage_counts_all(account)
        if sort == "used" or show == "unused"
        else None
    )
    if show == "unused" and all_counts is not None:
        qs = qs.exclude(pk__in=[pk for pk, n in all_counts.items() if n])
    page: Page
    if sort == "used" and all_counts is not None:
        rows = sorted(
            qs.values_list("pk", "created_at"),
            key=lambda r: (-all_counts.get(r[0], 0), -r[1].timestamp()),
        )
        page = Paginator([pk for pk, _ in rows], PAGE_SIZE).get_page(
            request.GET.get("page")
        )
        found = MediaAsset.objects.select_related("parent").in_bulk(list(page))
        assets = [found[pk] for pk in page if pk in found]
    else:
        orderings: dict[str, list[str | Combinable]] = {
            "name": [Lower("name"), "-pk"],
            "size": [F("size_bytes").desc(nulls_last=True), "-pk"],
        }
        ordering = orderings.get(sort, ["-created_at", "-pk"])
        page = Paginator(
            qs.select_related("parent").order_by(*ordering), PAGE_SIZE
        ).get_page(request.GET.get("page"))
        assets = list(page)
    media_library.sync_from_checks(assets)
    _decorate(account, assets, all_counts)
    if sort == "new" and view == "grid":
        today = timezone.localdate()
        for asset in assets:
            asset.group = media_library.date_group(asset.created_at, today)  # type: ignore[attr-defined]
    tags = sorted(
        {t for tags in live.values_list("tags", flat=True) for t in (tags or [])}
    )
    from .live_filters import chips

    active: list[tuple[tuple[str, ...], str]] = []
    if q:
        active.append(
            (
                ("q",),
                f"\N{LEFT DOUBLE QUOTATION MARK}{q}\N{RIGHT DOUBLE QUOTATION MARK}",
            )
        )
    if show != "all":
        active.append((("show", "kind", "archived"), dict(SHOW_CHOICES)[show]))
    if tag:
        active.append((("tag",), f"#{tag}"))
    return render(
        request,
        "publishing/media_library.html",
        {
            "account": account,
            "page": page,
            "assets": assets,
            "archived": archived,
            "show": show,
            "show_choices": SHOW_CHOICES,
            "sort": sort,
            "sort_choices": SORT_CHOICES,
            "view": view,
            "filtered": bool(q or tag or show != "all"),
            "clear_url": reverse("publishing:media-library"),
            "chips": chips(request, active),
            "remove_rules": REMOVE_RULES,
            "totals": totals,
            "tag": tag,
            "q": q,
            "tags": tags,
            "stock": media_library.stock_available(),
            "uploader": {
                "direct": direct_upload.available(),
                "upload": reverse("publishing:post-media-upload"),
                "upload_start": reverse("publishing:post-media-direct-start"),
                "upload_confirm": reverse("publishing:post-media-direct-confirm"),
                "stock_search": reverse("publishing:media-stock-search"),
                "stock_import": reverse("publishing:media-stock-import"),
            },
        },
    )


REMOVE_RULES = (
    "A file a draft or scheduled post uses stays until that post no longer needs it. "
    "One only published posts use is archived: hidden here, with the file kept so those "
    "posts keep working. One no post uses is deleted for good."
)


def _back(request) -> str:
    back = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(
        back, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        back = reverse("publishing:media-library")
    return back


def _remove_message(outcome: str, posts) -> tuple[str, str]:
    if outcome == "blocked":
        return "error", (
            "This file is in a draft, scheduled or publishing post, so it can't be "
            "removed yet: " + ", ".join(f"post {p.pk}" for p in posts) + "."
        )
    if outcome == "archived":
        return "success", (
            "Archived: published posts still use this file, so it's kept but "
            "hidden from the library."
        )
    return "success", "Deleted."


@login_required
@module_required("content_studio")
def asset_update(request, pk):
    """GET: the file's details (a drawer over the grid). POST: tags and name, remove
    (blocked, archived or deleted), or put an archived one back."""
    account = _account_or_none(request)
    if account is None:
        return redirect("dashboard")
    asset = get_object_or_404(MediaAsset, pk=pk, account=account)
    if request.method != "POST":
        return _asset_detail(request, account, asset)
    action = request.POST.get("action")
    back = _back(request)
    level, message, undo = "success", "Saved.", None
    if action == "tags":
        media_library.set_tags(asset, request.POST.get("tags", ""))
        name = " ".join(request.POST.get("name", "").split())[:200]
        if name and name != asset.name:
            asset.name = name
            asset.save(update_fields=["name"])
    elif action == "remove":
        outcome, posts = media_library.remove(asset)
        level, message = _remove_message(outcome, posts)
        # Archiving can be undone (the file is kept); a delete can't, so it only had the confirm.
        if outcome == "archived":
            undo = reverse("publishing:media-restore", args=[asset.pk])
        if request.POST.get("card") and is_background_post(request):
            return _card_response(
                request, account, asset, outcome, level, message, undo
            )
    elif action == "restore" and asset.archived_at:
        asset.archived_at = None
        asset.save(update_fields=["archived_at"])
        message = "Back in the library."
        if request.POST.get("card") and is_background_post(request):
            return _card_response(
                request, account, asset, "restored", level, message, None
            )
    return overlay_done(request, message, back, level=level, undo=undo)


def _card_response(request, account, asset, outcome, level, message, undo):
    """A remove from the card's own menu, in place: the card goes (archived or deleted)
    or stays as it was (blocked), with the toast saying which."""
    from django.http import HttpResponse

    from apps.accounts.utils import hx_toast

    if outcome == "blocked":
        _decorate(account, [asset])
        response = render(
            request,
            "publishing/_media_card.html",
            {"a": asset, "archived": False},
        )
    else:
        response = HttpResponse("")
    return hx_toast(response, "danger" if level == "error" else level, message, undo)


@login_required
@module_required("content_studio")
@require_POST
def asset_restore(request, pk):
    """Put an archived file back (an archive's Undo, or Put back)."""
    account = _account_or_none(request)
    if account is None:
        return redirect("dashboard")
    asset = get_object_or_404(MediaAsset, pk=pk, account=account)
    if asset.archived_at:
        asset.archived_at = None
        asset.save(update_fields=["archived_at"])
    return action_done(
        request, "Back in the library.", reverse("publishing:media-library")
    )


def _asset_detail(request, account, asset):
    media_library.sync_from_check(asset)
    _decorate(account, [asset])
    used = list(
        media_library.usage(asset)
        .order_by("-created_at")
        .only("pk", "caption", "status", "media_urls", "created_at")[:12]
    )
    outcome, blocking = media_library.remove_preview(asset)
    return overlay_or_page(
        request,
        "publishing/_media_detail.html",
        "publishing/media_detail.html",
        {
            "account": account,
            "asset": asset,
            "used_posts": used,
            "remove_outcome": outcome,
            "remove_blocking": blocking,
            "remove_rules": REMOVE_RULES,
            "derived": asset.derived.filter(archived_at__isnull=True)[:8],
        },
        kind="drawer",
        size="xl",
        title="File",
    )


@login_required
@module_required("content_studio")
@require_POST
def bulk(request):
    """Tag, remove or put back several files at once (the selection bar)."""
    account = _account_or_none(request)
    if account is None:
        return redirect("dashboard")
    ids = [int(i) for i in request.POST.getlist("ids") if i.isdigit()]
    action = request.POST.get("action", "")
    done = media_library.bulk(account, ids, action, request.POST.get("tags", ""))
    parts = []
    for key, words in (
        ("tagged", "tagged"),
        ("deleted", "deleted"),
        ("archived", "archived (published posts use them)"),
        ("blocked", "kept (a draft or scheduled post uses them)"),
        ("restored", "back in the library"),
    ):
        if done.get(key):
            n = done[key]
            parts.append(f"{n} file{'s' if n != 1 else ''} {words}")
    if parts:
        level = "warning" if done.get("blocked") else "success"
        getattr(messages, level)(request, "; ".join(parts).capitalize() + ".")
    elif action == "tag":
        messages.error(request, "Type a tag to add.")
    else:
        messages.error(request, "Choose some files first.")
    return redirect(_back(request))


@login_required
@module_required("content_studio")
def asset_edit(request, pk):
    """Crop, rotate and add one line of text in the browser; the result is saved as a
    new asset and the original is kept. Videos get the Reel cover-frame tool."""
    account = _account_or_none(request)
    if account is None:
        return redirect("dashboard")
    asset = get_object_or_404(MediaAsset, pk=pk, account=account)
    if request.method == "POST":
        try:
            copy = media_library.save_edit(
                asset, request.FILES.get("file"), request.user
            )
        except (media_library.LibraryError, AttributeError) as exc:
            return JsonResponse(
                {"error": str(exc) or "Choose an edited picture."}, status=400
            )
        return JsonResponse({"id": copy.pk, "url": copy.url})
    media_library.sync_from_check(asset)
    # From the media grid it opens as an almost full-screen modal over the grid.
    return overlay_or_page(
        request,
        "publishing/_media_edit.html",
        "publishing/media_edit.html",
        {
            "account": account,
            "asset": asset,
            "cover_status": cache.get(media_library.cover_status_key(asset)),
            "derived": asset.derived.filter(archived_at__isnull=True)[:12],
        },
        kind="modal",
        size="full",
        title="Reel cover" if asset.kind == "video" else "Edit picture",
    )


@login_required
@module_required("content_studio")
@require_POST
def asset_cover(request, pk):
    """Queue a Reel cover frame from a video (ffmpeg on the media queue)."""
    from .tasks import grab_cover_frame

    account = _account_or_none(request)
    if account is None:
        return redirect("dashboard")
    asset = get_object_or_404(MediaAsset, pk=pk, account=account, kind="video")
    try:
        seconds = float(request.POST.get("seconds", "0"))
    except ValueError:
        seconds = -1
    if seconds < 0:
        messages.error(request, "Choose a time in seconds, like 2.5.")
        return redirect("publishing:media-edit", pk=asset.pk)
    cache.delete(media_library.cover_status_key(asset))
    grab_cover_frame.delay(asset.pk, seconds, request.user.pk)
    messages.success(
        request, "Taking the frame. It appears below and in the library in a moment."
    )
    return redirect("publishing:media-edit", pk=asset.pk)


@login_required
@module_required("content_studio")
def picker(request):
    """The composer's "From library" list (JSON)."""
    account = _account_or_none(request)
    if account is None:
        return JsonResponse({"error": "Not found"}, status=404)
    return JsonResponse(
        {
            "items": media_library.picker_items(
                account, request.GET.get("kind", ""), request.GET.get("q", "").strip()
            )
        }
    )


@login_required
@module_required("content_studio")
def stock_search(request):
    account = _account_or_none(request)
    if account is None or not media_library.stock_available():
        return JsonResponse({"error": "Not found"}, status=404)
    try:
        photos = media_library.stock_search(
            request.GET.get("q", ""), request.GET.get("page", 1)
        )
    except (media_library.LibraryError, ValueError) as exc:
        return JsonResponse({"error": str(exc)}, status=502)
    return JsonResponse({"photos": photos})


@login_required
@module_required("content_studio")
@require_POST
def stock_import(request):
    account = _account_or_none(request)
    if account is None or not media_library.stock_available():
        return JsonResponse({"error": "Not found"}, status=404)
    try:
        asset = media_library.stock_import(
            account, request.POST.get("photo"), request.user
        )
    except media_library.LibraryError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"id": asset.pk, "url": asset.url, "credit": asset.credit})


# --- Caption templates and hashtag sets --------------------------------------------------


@login_required
@module_required("content_studio")
def snippets(request):
    account = _account_or_none(request)
    if account is None:
        return redirect("dashboard")
    if request.method == "POST":
        action = request.POST.get("action")
        name = " ".join(request.POST.get("name", "").split())[:80]
        if action == "template":
            body = request.POST.get("body", "").strip()
            if name and body:
                CaptionTemplate.objects.create(account=account, name=name, body=body)
                messages.success(request, "Caption template saved.")
            else:
                messages.error(request, "Give the template a name and some text.")
        elif action == "hashtags":
            tags: list[str] = []
            for t in request.POST.get("tags", "").replace(",", " ").split():
                t = t.lstrip("#").strip()
                if t and t.lower() not in {x.lower() for x in tags}:
                    tags.append(t[:100])
            if name and tags:
                if len(tags) > 30:
                    messages.error(request, "Instagram allows 30 hashtags at most.")
                else:
                    HashtagSet.objects.create(account=account, name=name, tags=tags)
                    messages.success(request, "Hashtag set saved.")
            else:
                messages.error(request, "Give the set a name and some hashtags.")
        elif action == "delete_template":
            CaptionTemplate.objects.filter(
                account=account, pk=request.POST.get("pk")
            ).delete()
        elif action == "delete_hashtags":
            HashtagSet.objects.filter(
                account=account, pk=request.POST.get("pk")
            ).delete()
        return redirect("publishing:media-snippets")
    return overlay_or_page(
        request,
        "publishing/_snippets.html",
        "publishing/snippets.html",
        {
            "account": account,
            "templates": CaptionTemplate.objects.filter(account=account),
            "hashtag_sets": HashtagSet.objects.filter(account=account),
        },
        size="lg",
        title="Caption templates and hashtags",
    )
