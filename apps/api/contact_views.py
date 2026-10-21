"""Public API for contacts, lists, and segments (Phase 4)."""

from __future__ import annotations

from django.db import IntegrityError
from django.utils.text import slugify
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.response import Response

from apps.api.base import BaseApiView
from apps.api.permissions import HasEmailApiFeature
from apps.contacts.models import Contact, ContactList, Segment
from apps.contacts.segments import SegmentError, contacts_for, count_for
from apps.contacts.services import (
    import_csv,
    record_contact_event,
    upsert_contact,
    upsert_contact_by_phone,
)


def _already_exists(message: str) -> Response:
    return Response(
        {"error": {"code": "already_exists", "message": message}},
        status=status.HTTP_409_CONFLICT,
    )


def _paging(request, *, default=50, cap=200):
    try:
        limit = min(int(request.query_params.get("limit", default)), cap)
    except (TypeError, ValueError):
        limit = default
    try:
        offset = max(int(request.query_params.get("offset", 0)), 0)
    except (TypeError, ValueError):
        offset = 0
    return max(limit, 1), offset


def _contact_dict(c: Contact) -> dict:
    """One contact as the API returns it. ``whatsapp`` is the reachability state
    campaigns act on: opted_in · updates · opted_out · none."""
    from apps.contacts import reachability

    wa_state = getattr(c, "wa_state", None) or reachability.state_for(c)
    return {
        "id": c.public_id,
        "email": c.email,
        "phone": c.phone,
        "first_name": c.first_name,
        "last_name": c.last_name,
        "locale": c.locale,
        "status": c.status,
        "source": c.source,
        "lifecycle_stage": c.lifecycle_stage,
        "whatsapp": wa_state,
        "tags": [t.name for t in c.tags.all()],
        "lists": [cl.slug for cl in c.lists.all()],
        "attributes": {
            k: v for k, v in (c.attributes or {}).items() if not k.startswith("_")
        },
        "first_seen": c.first_seen,
        "last_engaged_at": c.last_engaged_at,
    }


def _contacts_qs(account):
    """Contacts ready for _contact_dict without a query per row."""
    from apps.contacts import reachability

    return reachability.annotate(
        Contact.objects.filter(account=account).order_by("-first_seen", "-id")
    ).prefetch_related("tags", "lists")


def _error(code: str, message: str, http=status.HTTP_400_BAD_REQUEST) -> Response:
    return Response({"error": {"code": code, "message": message}}, status=http)


def _set_whatsapp_consent(request, contact, value, attested) -> Response | None:
    """PATCH ``whatsapp``: "opted_in" (needs attested=true) or "opted_out"."""
    from apps.contacts import reachability
    from apps.whatsapp.services.contacts import ensure_whatsapp_identity

    if value not in ("opted_in", "opted_out"):
        return _error("validation_error", "whatsapp must be opted_in or opted_out")
    identity = reachability.primary_identity(contact) or ensure_whatsapp_identity(
        contact
    )
    if identity is None:
        return _error("validation_error", "This contact has no phone number.")
    if value == "opted_out":
        identity.record_opt_out(f"api key {request.auth.pk}")
        return None
    if identity.opt_in_status == identity.OptInStatus.OPTED_IN:
        return None
    if attested is not True:
        return _error(
            "attestation_required",
            "Set attested: true to confirm the customer agreed to WhatsApp messages "
            "from your business.",
        )
    if identity.opt_in_status == identity.OptInStatus.OPTED_OUT:
        return _error(
            "opted_out",
            "The customer opted out (replied STOP). Only they can opt back in.",
            status.HTTP_409_CONFLICT,
        )
    identity.record_opt_in(f"api_attestation:key:{request.auth.pk}")
    return None


class ContactCollectionView(BaseApiView):
    permission_classes = [HasEmailApiFeature]
    idempotency_endpoint = "POST /v1/contacts"

    @extend_schema(
        operation_id="contacts_list",
        responses=OpenApiResponse(OpenApiTypes.OBJECT),
        tags=["Contacts"],
    )
    def get(self, request, *args, **kwargs):
        qs = _contacts_qs(request.user)
        if request.query_params.get("phone"):
            from django.core.exceptions import ValidationError

            from apps.whatsapp.models.contact import normalize_phone

            try:
                qs = qs.filter(phone=normalize_phone(request.query_params["phone"]))
            except ValidationError:
                qs = qs.none()
        if request.query_params.get("whatsapp"):
            from apps.contacts import reachability

            qs = reachability.filter_state(qs, request.query_params["whatsapp"])
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"])
        if request.query_params.get("email"):
            qs = qs.filter(email__iexact=request.query_params["email"])
        limit, offset = _paging(request)
        total = qs.count()
        return Response(
            {
                "data": [_contact_dict(c) for c in qs[offset : offset + limit]],
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )

    @extend_schema(
        operation_id="contacts_create",
        request=None,
        responses=OpenApiResponse(OpenApiTypes.OBJECT),
        tags=["Contacts"],
    )
    def post(self, request, *args, **kwargs):
        from django.core.exceptions import ValidationError

        from apps.contacts.services import ContactLimitReached, ensure_room_for_contact
        from apps.whatsapp.models.contact import normalize_phone

        d = request.data if isinstance(request.data, dict) else {}
        email = (d.get("email") or "").strip()
        phone = (d.get("phone") or "").strip()
        if not email and not phone:
            return _error("validation_error", "email or phone is required")
        if phone:
            try:
                phone = normalize_phone(phone)
            except ValidationError:
                return _error(
                    "validation_error",
                    "phone isn't a valid number. Include the country code, e.g. +260971234567.",
                )
        if d.get("whatsapp") not in (None, "opted_in", "opted_out"):
            return _error("validation_error", "whatsapp must be opted_in or opted_out")
        idem = self.begin_idempotency(request)
        try:
            ensure_room_for_contact(
                request.user, email=email or None, phone=phone or None
            )
        except ContactLimitReached as exc:
            self.release_idempotency(idem)
            return _error("plan_limit", str(exc), status.HTTP_403_FORBIDDEN)
        fields = {
            "first_name": d.get("first_name", ""),
            "last_name": d.get("last_name", ""),
            "locale": d.get("locale", ""),
            "source": d.get("source", "api"),
        }
        try:
            if email:
                if phone:
                    fields["phone"] = phone
                contact, created = upsert_contact(
                    request.user, email, attributes=d.get("attributes") or {}, **fields
                )
            else:
                contact, created = upsert_contact_by_phone(
                    request.user, phone, attributes=d.get("attributes") or {}, **fields
                )
        except IntegrityError:
            self.release_idempotency(idem)
            return _already_exists(
                "Another contact already has that phone number or email address."
            )
        if d.get("whatsapp"):
            # The identity is created on commit by the Contact signal; make sure now.
            from apps.whatsapp.services.contacts import ensure_whatsapp_identity

            ensure_whatsapp_identity(contact)
            problem = _set_whatsapp_consent(
                request, contact, d["whatsapp"], d.get("attested")
            )
            if problem is not None:
                self.release_idempotency(idem)
                return problem
        request.auth.touch()
        contact = _contacts_qs(request.user).get(pk=contact.pk)
        return self.finish_idempotency(
            idem,
            Response(
                _contact_dict(contact),
                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
            ),
        )


class ContactDetailView(BaseApiView):
    permission_classes = [HasEmailApiFeature]

    def _get(self, request, cid):
        return _contacts_qs(request.user).get(public_id=cid)

    @extend_schema(
        operation_id="contacts_retrieve",
        responses=OpenApiResponse(OpenApiTypes.OBJECT),
        tags=["Contacts"],
    )
    def get(self, request, cid, *args, **kwargs):
        return Response(_contact_dict(self._get(request, cid)))

    @extend_schema(
        operation_id="contacts_update",
        request=None,
        responses=OpenApiResponse(OpenApiTypes.OBJECT),
        tags=["Contacts"],
    )
    def patch(self, request, cid, *args, **kwargs):
        c = self._get(request, cid)
        d = request.data if isinstance(request.data, dict) else {}
        # A phone-only contact (e.g. created from WhatsApp) has no email to key on.
        upsert, identity = (
            (upsert_contact, c.email) if c.email else (upsert_contact_by_phone, c.phone)
        )
        contact, _ = upsert(
            request.user,
            identity,
            attributes=d.get("attributes") or {},
            first_name=d.get("first_name", ""),
            last_name=d.get("last_name", ""),
            locale=d.get("locale", ""),
        )
        if d.get("status") in dict(Contact.Status.choices):
            contact.status = d["status"]
            contact.save(update_fields=["status", "updated_at"])
        if "whatsapp" in d:
            problem = _set_whatsapp_consent(
                request, c, d["whatsapp"], d.get("attested")
            )
            if problem is not None:
                return problem
        return Response(_contact_dict(self._get(request, cid)))

    @extend_schema(
        operation_id="contacts_delete",
        responses=OpenApiResponse(OpenApiTypes.OBJECT),
        tags=["Contacts"],
    )
    def delete(self, request, cid, *args, **kwargs):
        from apps.contacts.erasure import delete_customer

        delete_customer(self._get(request, cid), source="api")
        return Response(status=status.HTTP_204_NO_CONTENT)


class ContactEventsView(BaseApiView):
    permission_classes = [HasEmailApiFeature]

    @extend_schema(
        operation_id="contacts_events",
        responses=OpenApiResponse(OpenApiTypes.OBJECT),
        tags=["Contacts"],
    )
    def get(self, request, cid, *args, **kwargs):
        c = Contact.objects.get(account=request.user, public_id=cid)
        events = c.events.all()[:200]
        return Response(
            {
                "data": [
                    {"type": e.type, "occurred_at": e.occurred_at, "data": e.data}
                    for e in events
                ]
            }
        )

    @extend_schema(
        operation_id="contacts_track",
        request=None,
        responses=OpenApiResponse(OpenApiTypes.OBJECT),
        tags=["Contacts"],
    )
    def post(self, request, cid, *args, **kwargs):
        c = Contact.objects.get(account=request.user, public_id=cid)
        d = request.data if isinstance(request.data, dict) else {}
        if not d.get("type"):
            return Response(
                {"error": {"code": "validation_error", "message": "type is required"}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        ev = record_contact_event(c, d["type"], data=d.get("data") or {})
        return Response(
            {"type": ev.type, "occurred_at": ev.occurred_at},
            status=status.HTTP_201_CREATED,
        )


class ContactImportView(BaseApiView):
    permission_classes = [HasEmailApiFeature]

    @extend_schema(
        operation_id="contacts_import",
        request=None,
        responses=OpenApiResponse(OpenApiTypes.OBJECT),
        tags=["Contacts"],
    )
    def post(self, request, *args, **kwargs):
        d = request.data if isinstance(request.data, dict) else {}
        csv_text = d.get("csv")
        if not csv_text:
            return Response(
                {"error": {"code": "validation_error", "message": "csv is required"}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            imp = import_csv(
                request.user,
                csv_text,
                filename=d.get("filename", ""),
                mapping=d.get("mapping"),
            )
        except ValueError as exc:
            return Response(
                {"error": {"code": "validation_error", "message": str(exc)}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {
                "id": imp.pk,
                "rows": imp.row_count,
                "created": imp.created_count,
                "updated": imp.updated_count,
                "skipped": imp.skipped_count,
                "not_added_plan_limit": imp.limit_skipped_count,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class ListCollectionView(BaseApiView):
    permission_classes = [HasEmailApiFeature]

    @extend_schema(
        operation_id="lists_list",
        responses=OpenApiResponse(OpenApiTypes.OBJECT),
        tags=["Contacts"],
    )
    def get(self, request, *args, **kwargs):
        from django.db.models import Count

        return Response(
            {
                "data": [
                    {"id": l.slug, "name": l.name, "count": l.n}
                    for l in ContactList.objects.filter(account=request.user)
                    .annotate(n=Count("contacts", distinct=True))
                    .order_by("name")
                ]
            }
        )

    @extend_schema(
        operation_id="lists_create",
        request=None,
        responses=OpenApiResponse(OpenApiTypes.OBJECT),
        tags=["Contacts"],
    )
    def post(self, request, *args, **kwargs):
        name = (request.data or {}).get("name")
        if not name:
            return Response(
                {"error": {"code": "validation_error", "message": "name is required"}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if ContactList.objects.filter(
            account=request.user, slug=slugify(name)[:160]
        ).exists():
            return _already_exists(f'A list called "{name}" already exists.')
        l = ContactList.objects.create(account=request.user, name=name)
        return Response({"id": l.slug, "name": l.name}, status=status.HTTP_201_CREATED)


_MEMBERS_MAX = 1000


class ListDetailView(BaseApiView):
    """GET a list (with who WhatsApp can reach), PATCH to rename, DELETE it."""

    permission_classes = [HasEmailApiFeature]

    def _get(self, request, slug):
        return ContactList.objects.get(account=request.user, slug=slug)

    @extend_schema(
        operation_id="lists_retrieve",
        responses=OpenApiResponse(OpenApiTypes.OBJECT),
        tags=["Contacts"],
    )
    def get(self, request, slug, *args, **kwargs):
        from apps.contacts import reachability

        cl = self._get(request, slug)
        counts = reachability.counts(cl.contacts.all())
        return Response(
            {
                "id": cl.slug,
                "name": cl.name,
                "count": counts.pop("total"),
                "whatsapp": counts,
            }
        )

    @extend_schema(
        operation_id="lists_update",
        request=None,
        responses=OpenApiResponse(OpenApiTypes.OBJECT),
        tags=["Contacts"],
    )
    def patch(self, request, slug, *args, **kwargs):
        cl = self._get(request, slug)
        name = ((request.data or {}).get("name") or "").strip()[:150]
        if not name:
            return _error("validation_error", "name is required")
        new_slug = slugify(name)[:160]
        if (
            ContactList.objects.filter(account=request.user, slug=new_slug)
            .exclude(pk=cl.pk)
            .exists()
        ):
            return _already_exists(f'A list called "{name}" already exists.')
        cl.name, cl.slug = name, new_slug
        cl.save(update_fields=["name", "slug"])
        return Response({"id": cl.slug, "name": cl.name})

    @extend_schema(
        operation_id="lists_delete",
        responses=OpenApiResponse(OpenApiTypes.OBJECT),
        tags=["Contacts"],
    )
    def delete(self, request, slug, *args, **kwargs):
        cl = self._get(request, slug)
        if cl.whatsapp_campaigns.exists():
            return _error(
                "in_use",
                "This list was used by a campaign, so it's kept for that campaign's history.",
                status.HTTP_409_CONFLICT,
            )
        cl.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ListMembersView(BaseApiView):
    """Add or remove up to 1,000 people at once, by contact id or by phone.

    POST {"contacts": [ids], "phones": ["+260…" or {"phone", "name"}], "attested": bool}
    adds them (phones not yet customers are created). ``attested: true`` records
    that they agreed to WhatsApp messages (never overrides an opt-out).
    DELETE {"contacts": [ids], "phones": [...]} removes them from the list only.
    """

    permission_classes = [HasEmailApiFeature]
    idempotency_endpoint = "POST /v1/lists/members"

    def _parse(self, request):
        d = request.data if isinstance(request.data, dict) else {}
        ids = [str(x) for x in (d.get("contacts") or []) if x]
        phones = d.get("phones") or []
        if not isinstance(phones, list) or not isinstance(
            d.get("contacts") or [], list
        ):
            return (
                None,
                None,
                d,
                _error("validation_error", "contacts and phones must be lists"),
            )
        if len(ids) + len(phones) > _MEMBERS_MAX:
            return (
                None,
                None,
                d,
                _error(
                    "validation_error", f"At most {_MEMBERS_MAX} people per request."
                ),
            )
        if not ids and not phones:
            return (
                None,
                None,
                d,
                _error("validation_error", "contacts or phones is required"),
            )
        return ids, phones, d, None

    @extend_schema(
        operation_id="lists_members_add",
        request=None,
        responses=OpenApiResponse(OpenApiTypes.OBJECT),
        tags=["Contacts"],
    )
    def post(self, request, slug, *args, **kwargs):
        from apps.contacts import list_building as lb

        cl = ContactList.objects.get(account=request.user, slug=slug)
        ids, phones, d, problem = self._parse(request)
        if problem is not None:
            return problem
        idem = self.begin_idempotency(request)
        added = 0
        invalid: list = []
        created = opted_in = opted_out_unchanged = over_limit = 0
        if ids:
            result = lb.add_contacts_to_list(
                request.user,
                cl,
                Contact.objects.filter(account=request.user, public_id__in=ids),
            )
            added += result.added
        if phones:
            rows = []
            for i, p in enumerate(phones, start=1):
                raw = p.get("phone", "") if isinstance(p, dict) else str(p)
                first, _, last = (
                    (p.get("name", "") if isinstance(p, dict) else "")
                    .strip()
                    .partition(" ")
                )
                rows.append(
                    lb.Row(line=i, phone=raw, first_name=first, last_name=last.strip())
                )
            result = lb.add_people_to_list(
                request.user,
                cl,
                rows,
                attested_by=f"key:{request.auth.pk}"
                if d.get("attested") is True
                else None,
                source="api",
            )
            added += result.added
            created, opted_in = result.created, result.opted_in
            opted_out_unchanged, over_limit = (
                result.opted_out_unchanged,
                result.over_limit,
            )
            invalid = [
                {"row": n, "value": raw, "problem": why}
                for n, raw, why in result.invalid
            ]
        return self.finish_idempotency(
            idem,
            Response(
                {
                    "added": added,
                    "created": created,
                    "opted_in": opted_in,
                    "opted_out_unchanged": opted_out_unchanged,
                    "not_added_plan_limit": over_limit,
                    "invalid": invalid,
                }
            ),
        )

    @extend_schema(
        operation_id="lists_members_remove",
        request=None,
        responses=OpenApiResponse(OpenApiTypes.OBJECT),
        tags=["Contacts"],
    )
    def delete(self, request, slug, *args, **kwargs):
        from django.core.exceptions import ValidationError

        from apps.contacts.models import ContactListMembership
        from apps.whatsapp.models.contact import normalize_phone

        cl = ContactList.objects.get(account=request.user, slug=slug)
        ids, phones, _d, problem = self._parse(request)
        if problem is not None:
            return problem
        normalized = []
        for p in phones:
            try:
                normalized.append(
                    normalize_phone(
                        p.get("phone", "") if isinstance(p, dict) else str(p)
                    )
                )
            except ValidationError:
                continue
        from django.db.models import Q

        removed, _ = (
            ContactListMembership.objects.filter(contact_list=cl)
            .filter(Q(contact__public_id__in=ids) | Q(contact__phone__in=normalized))
            .delete()
        )
        return Response({"removed": removed})


class SegmentCollectionView(BaseApiView):
    permission_classes = [HasEmailApiFeature]

    @extend_schema(
        operation_id="segments_list",
        responses=OpenApiResponse(OpenApiTypes.OBJECT),
        tags=["Contacts"],
    )
    def get(self, request, *args, **kwargs):
        return Response(
            {
                "data": [
                    {"id": s.slug, "name": s.name, "definition": s.definition}
                    for s in Segment.objects.filter(account=request.user)
                ]
            }
        )

    @extend_schema(
        operation_id="segments_create",
        request=None,
        responses=OpenApiResponse(OpenApiTypes.OBJECT),
        tags=["Contacts"],
    )
    def post(self, request, *args, **kwargs):
        d = request.data if isinstance(request.data, dict) else {}
        if not d.get("name"):
            return Response(
                {"error": {"code": "validation_error", "message": "name is required"}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if Segment.objects.filter(
            account=request.user, slug=slugify(d["name"])[:160]
        ).exists():
            return _already_exists(f'A segment called "{d["name"]}" already exists.')
        definition = d.get("definition") or {}
        try:
            count = count_for(definition, request.user)
        except SegmentError as exc:
            return Response(
                {"error": {"code": "invalid_segment", "message": str(exc)}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        s = Segment.objects.create(
            account=request.user, name=d["name"], definition=definition
        )
        return Response(
            {"id": s.slug, "name": s.name, "count": count},
            status=status.HTTP_201_CREATED,
        )


class SegmentPreviewView(BaseApiView):
    permission_classes = [HasEmailApiFeature]

    @extend_schema(
        operation_id="segments_preview",
        request=None,
        responses=OpenApiResponse(OpenApiTypes.OBJECT),
        tags=["Contacts"],
    )
    def post(self, request, *args, **kwargs):
        definition = (request.data or {}).get("definition") or {}
        try:
            count = count_for(definition, request.user)
        except SegmentError as exc:
            return Response(
                {"error": {"code": "invalid_segment", "message": str(exc)}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"count": count})


class SegmentContactsView(BaseApiView):
    permission_classes = [HasEmailApiFeature]

    @extend_schema(
        operation_id="segments_contacts",
        responses=OpenApiResponse(OpenApiTypes.OBJECT),
        tags=["Contacts"],
    )
    def get(self, request, slug, *args, **kwargs):
        s = Segment.objects.get(account=request.user, slug=slug)
        limit, offset = _paging(request)
        qs = contacts_for(s.definition, request.user)
        total = qs.count()
        return Response(
            {
                "data": [_contact_dict(c) for c in qs[offset : offset + limit]],
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )
