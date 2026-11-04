"""Media library pages (apps.publishing.media_library). Behind the ``cs_media_library``
release gate: until it is released, ReleaseGateMiddleware 404s every URL under
``publishing/media/``."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account
from apps.core.htmx import overlay_or_page
from apps.core.module_gate import module_required

from . import direct_upload, media_library
from .models import CaptionTemplate, HashtagSet, MediaAsset

PAGE_SIZE = 48


def _account_or_none(request):
    return get_current_account(request)


@login_required
@module_required("content_studio")
def library(request):
    account = _account_or_none(request)
    if account is None:
        return redirect("dashboard")
    archived = request.GET.get("archived") == "1"
    kind = request.GET.get("kind", "")
    tag = request.GET.get("tag", "").strip().lower()
    q = request.GET.get("q", "").strip()
    qs = MediaAsset.objects.filter(account=account, archived_at__isnull=not archived)
    if kind in ("image", "video"):
        qs = qs.filter(kind=kind)
    if tag:
        qs = qs.filter(tags__icontains=f'"{tag}"')
    if q:
        qs = qs.filter(name__icontains=q)
    page = Paginator(qs.select_related("parent"), PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    assets = list(page)
    media_library.sync_from_checks(assets)
    counts = media_library.usage_counts(account, assets)
    for asset in assets:
        asset.used_in = counts.get(asset.pk, 0)  # type: ignore[attr-defined]
    tags = sorted(
        {t for tags in qs.values_list("tags", flat=True) for t in (tags or [])}
    )
    return render(
        request,
        "publishing/media_library.html",
        {
            "account": account,
            "page": page,
            "archived": archived,
            "kind": kind,
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


@login_required
@module_required("content_studio")
@require_POST
def asset_update(request, pk):
    """Tags, name, remove (blocked, archived or deleted), or put an archived one back."""
    account = _account_or_none(request)
    if account is None:
        return redirect("dashboard")
    asset = get_object_or_404(MediaAsset, pk=pk, account=account)
    action = request.POST.get("action")
    back = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(
        back, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        back = reverse("publishing:media-library")
    if action == "tags":
        media_library.set_tags(asset, request.POST.get("tags", ""))
        name = " ".join(request.POST.get("name", "").split())[:200]
        if name and name != asset.name:
            asset.name = name
            asset.save(update_fields=["name"])
        messages.success(request, "Saved.")
    elif action == "remove":
        outcome, posts = media_library.remove(asset)
        if outcome == "blocked":
            messages.error(
                request,
                "This file is in a draft, scheduled or publishing post, so it can't be "
                "removed yet: " + ", ".join(f"post {p.pk}" for p in posts) + ".",
            )
        elif outcome == "archived":
            messages.success(
                request,
                "Archived: published posts still use this file, so it's kept but "
                "hidden from the library.",
            )
        else:
            messages.success(request, "Deleted.")
    elif action == "restore" and asset.archived_at:
        asset.archived_at = None
        asset.save(update_fields=["archived_at"])
        messages.success(request, "Back in the library.")
    return redirect(back)


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
        size="xl",
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
