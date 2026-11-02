"""Managing the link-in-bio page (apps.publishing.bio). Behind the ``cs_bio_page``
release gate: until it is released, ReleaseGateMiddleware 404s these pages."""

from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.api import is_account_admin
from apps.accounts.utils import get_current_account
from apps.core.module_gate import module_required

from . import bio
from .links import link_for_variant
from .models import BioPage, BioTile, ContentPostVariant

ONLY_ADMIN = "Only an Owner or Admin can change the bio page."

STATS_WINDOW = timedelta(days=30)  # tile clicks and chats shown on the manage page


def _page_or_none(account) -> BioPage | None:
    return BioPage.objects.filter(account=account).first()


@login_required
@module_required("content_studio")
def bio_manage(request):
    """Create the page, see its tiles and whether the Instagram bio points at it."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    page = _page_or_none(account)
    can_admin = is_account_admin(request.user, account)

    if request.method == "POST":
        if not can_admin:
            messages.error(request, "Only an Owner or Admin can do that.")
            return redirect("publishing:bio")
        action = request.POST.get("action", "")
        try:
            if action == "create" and page is None:
                page = bio.create_page(
                    account, request.POST.get("slug", ""), request.POST.get("title", "")
                )
                # A failed read shows as "not checked yet"; the daily check retries.
                bio.check_instagram_website(page)
                messages.success(
                    request,
                    "Your bio page is live. Put its address in your Instagram bio.",
                )
            elif action == "settings" and page is not None:
                _save_settings(request, page)
                messages.success(request, "Bio page saved.")
            elif action == "check" and page is not None:
                if bio.check_instagram_website(page):
                    messages.success(request, "Checked your Instagram profile.")
                else:
                    messages.error(
                        request,
                        "Couldn't read your Instagram profile. Check that Instagram "
                        "is connected, then try again.",
                    )
        except bio.BioError as exc:
            messages.error(request, str(exc))
        return redirect("publishing:bio")

    tiles = list(bio.active_tiles(page)) if page else []
    stats = bio.tile_stats(page, timezone.now() - STATS_WINDOW) if page else {}
    for tile in tiles:
        tile.stats = stats.get(tile.pk, {"clicks": 0, "chats": 0})
    archived = (
        list(page.tiles.filter(archived_at__isnull=False).select_related("link__post"))
        if page
        else []
    )
    return render(
        request,
        "publishing/bio.html",
        {
            "page": page,
            "page_url": bio.page_url(page) if page else "",
            "tiles": tiles,
            "archived": archived[:20],
            "bio_link_set": bio.bio_link_set(page) if page else None,
            "has_instagram": account.instagram_accounts.filter(is_active=True).exists(),
            "suggested_slug": bio.suggested_slug(account) if page is None else "",
            "suggested_title": account.company_name or "",
            "can_admin": can_admin,
            "stats_days": STATS_WINDOW.days,
        },
    )


def _save_settings(request, page: BioPage) -> None:
    title = " ".join(request.POST.get("title", "").split())[:100]
    if not title:
        raise bio.BioError("Give the page a title.")
    try:
        max_tiles = int(request.POST.get("max_tiles", page.max_tiles))
        days = int(request.POST.get("archive_after_days", page.archive_after_days))
    except ValueError as exc:
        raise bio.BioError("Use whole numbers for the tile settings.") from exc
    if not 1 <= max_tiles <= 30:
        raise bio.BioError("Show between 1 and 30 posts.")
    if not 1 <= days <= 365:
        raise bio.BioError("Archive posts after 1 to 365 days.")
    # A colour input always sends a value, so the site's colours are a checkbox away.
    custom = request.POST.get("custom_colors") == "on"
    background = (
        bio.clean_color(request.POST.get("background_color", "")) if custom else ""
    )
    button = bio.clean_color(request.POST.get("button_color", "")) if custom else ""
    avatar = request.FILES.get("avatar")
    if avatar is not None:
        page.avatar_url = bio.save_avatar(page, avatar)
    elif request.POST.get("remove_avatar") == "on":
        page.avatar_url = ""
    page.title = title
    page.max_tiles = max_tiles
    page.archive_after_days = days
    page.background_color = background
    page.button_color = button
    page.save(
        update_fields=[
            "title",
            "max_tiles",
            "archive_after_days",
            "avatar_url",
            "background_color",
            "button_color",
            "updated_at",
        ]
    )
    bio.set_active(page, request.POST.get("is_active") == "on")
    bio.enforce_limit(page)


@login_required
@module_required("content_studio")
@require_POST
def bio_tile_add(request, variant_pk: int):
    """Add an Instagram channel's post to the bio page (from the post's Results card)."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    variant = get_object_or_404(
        ContentPostVariant,
        pk=variant_pk,
        post__account=account,
        platform=ContentPostVariant.Platform.INSTAGRAM,
    )
    back = redirect("publishing:post-detail", pk=variant.post_id)
    if not is_account_admin(request.user, account):
        messages.error(request, ONLY_ADMIN)
        return back
    page = _page_or_none(account)
    if page is None:
        messages.error(request, "Create your bio page first.")
        return redirect("publishing:bio")
    from .links import NoWhatsAppNumber

    try:
        link = link_for_variant(variant)
        bio.add_tile(page, link)
    except NoWhatsAppNumber:
        messages.error(request, "Connect a WhatsApp number to add chat links.")
        return back
    except bio.BioError as exc:
        messages.error(request, str(exc))
        return back
    messages.success(request, "Added to your bio page.")
    return back


@login_required
@module_required("content_studio")
@require_POST
def bio_tile_update(request, pk: int):
    """Pin, unpin, archive, restore or rename a tile."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    tile = get_object_or_404(BioTile, pk=pk, page__account=account)
    if not is_account_admin(request.user, account):
        messages.error(request, ONLY_ADMIN)
        return redirect("publishing:bio")
    action = request.POST.get("action", "")
    if action in ("pin", "unpin"):
        tile.pinned = action == "pin"
        tile.save(update_fields=["pinned"])
    elif action in ("up", "down"):
        bio.move_tile(tile, -1 if action == "up" else 1)
    elif action == "archive":
        tile.archived_at = timezone.now()
        tile.save(update_fields=["archived_at"])
    elif action == "restore":
        try:
            bio.add_tile(tile.page, tile.link, tile.label)
        except bio.BioError as exc:
            messages.error(request, str(exc))
    elif action == "label":
        label = " ".join(request.POST.get("label", "").split())[: bio.LABEL_MAX]
        if label:
            tile.label = label
            tile.save(update_fields=["label"])
    return redirect("publishing:bio")
