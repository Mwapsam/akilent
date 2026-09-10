"""Public API for contacts, lists, and segments (Phase 4)."""
from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.response import Response

from apps.api.base import BaseApiView
from apps.api.permissions import HasEmailApiFeature
from apps.contacts.models import Contact, ContactList, Segment
from apps.contacts.segments import SegmentError, contacts_for, count_for
from apps.contacts.services import import_csv, record_contact_event, upsert_contact


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
    return {
        "id": c.public_id,
        "email": c.email,
        "first_name": c.first_name,
        "last_name": c.last_name,
        "locale": c.locale,
        "status": c.status,
        "source": c.source,
        "attributes": c.attributes,
        "first_seen": c.first_seen,
        "last_engaged_at": c.last_engaged_at,
    }


class ContactCollectionView(BaseApiView):
    permission_classes = [HasEmailApiFeature]
    idempotency_endpoint = "POST /v1/contacts"

    @extend_schema(operation_id="contacts_list", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Contacts"])
    def get(self, request, *args, **kwargs):
        qs = Contact.objects.filter(account=request.user)
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"])
        if request.query_params.get("email"):
            qs = qs.filter(email__iexact=request.query_params["email"])
        limit, offset = _paging(request)
        total = qs.count()
        return Response({
            "data": [_contact_dict(c) for c in qs[offset:offset + limit]],
            "total": total, "limit": limit, "offset": offset,
        })

    @extend_schema(operation_id="contacts_create", request=None, responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Contacts"])
    def post(self, request, *args, **kwargs):
        idem = self.begin_idempotency(request)
        d = request.data if isinstance(request.data, dict) else {}
        email = d.get("email")
        if not email:
            return Response({"error": {"code": "validation_error", "message": "email is required"}},
                            status=status.HTTP_400_BAD_REQUEST)
        contact, created = upsert_contact(
            request.user, email,
            attributes=d.get("attributes") or {},
            first_name=d.get("first_name", ""),
            last_name=d.get("last_name", ""),
            locale=d.get("locale", ""),
            source=d.get("source", "api"),
        )
        request.auth.touch()
        return self.finish_idempotency(
            idem,
            Response(_contact_dict(contact),
                     status=status.HTTP_201_CREATED if created else status.HTTP_200_OK),
        )


class ContactDetailView(BaseApiView):
    permission_classes = [HasEmailApiFeature]

    def _get(self, request, cid):
        return Contact.objects.get(account=request.user, public_id=cid)

    @extend_schema(operation_id="contacts_retrieve", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Contacts"])
    def get(self, request, cid, *args, **kwargs):
        return Response(_contact_dict(self._get(request, cid)))

    @extend_schema(operation_id="contacts_update", request=None, responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Contacts"])
    def patch(self, request, cid, *args, **kwargs):
        c = self._get(request, cid)
        d = request.data if isinstance(request.data, dict) else {}
        contact, _ = upsert_contact(
            request.user, c.email,
            attributes=d.get("attributes") or {},
            first_name=d.get("first_name", ""),
            last_name=d.get("last_name", ""),
            locale=d.get("locale", ""),
        )
        if d.get("status") in dict(Contact.Status.choices):
            contact.status = d["status"]
            contact.save(update_fields=["status", "updated_at"])
        return Response(_contact_dict(contact))

    @extend_schema(operation_id="contacts_delete", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Contacts"])
    def delete(self, request, cid, *args, **kwargs):
        self._get(request, cid).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ContactEventsView(BaseApiView):
    permission_classes = [HasEmailApiFeature]

    @extend_schema(operation_id="contacts_events", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Contacts"])
    def get(self, request, cid, *args, **kwargs):
        c = Contact.objects.get(account=request.user, public_id=cid)
        events = c.events.all()[:200]
        return Response({"data": [
            {"type": e.type, "occurred_at": e.occurred_at, "data": e.data} for e in events
        ]})

    @extend_schema(operation_id="contacts_track", request=None, responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Contacts"])
    def post(self, request, cid, *args, **kwargs):
        c = Contact.objects.get(account=request.user, public_id=cid)
        d = request.data if isinstance(request.data, dict) else {}
        if not d.get("type"):
            return Response({"error": {"code": "validation_error", "message": "type is required"}},
                            status=status.HTTP_400_BAD_REQUEST)
        ev = record_contact_event(c, d["type"], data=d.get("data") or {})
        return Response({"type": ev.type, "occurred_at": ev.occurred_at}, status=status.HTTP_201_CREATED)


class ContactImportView(BaseApiView):
    permission_classes = [HasEmailApiFeature]

    @extend_schema(operation_id="contacts_import", request=None, responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Contacts"])
    def post(self, request, *args, **kwargs):
        d = request.data if isinstance(request.data, dict) else {}
        csv_text = d.get("csv")
        if not csv_text:
            return Response({"error": {"code": "validation_error", "message": "csv is required"}},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            imp = import_csv(request.user, csv_text, filename=d.get("filename", ""),
                             mapping=d.get("mapping"))
        except ValueError as exc:
            return Response({"error": {"code": "validation_error", "message": str(exc)}},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({
            "id": imp.pk, "rows": imp.row_count, "created": imp.created_count,
            "updated": imp.updated_count, "skipped": imp.skipped_count,
        }, status=status.HTTP_202_ACCEPTED)


class ListCollectionView(BaseApiView):
    permission_classes = [HasEmailApiFeature]

    @extend_schema(operation_id="lists_list", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Contacts"])
    def get(self, request, *args, **kwargs):
        return Response({"data": [
            {"id": l.slug, "name": l.name, "count": l.contacts.count()}
            for l in ContactList.objects.filter(account=request.user)
        ]})

    @extend_schema(operation_id="lists_create", request=None, responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Contacts"])
    def post(self, request, *args, **kwargs):
        name = (request.data or {}).get("name")
        if not name:
            return Response({"error": {"code": "validation_error", "message": "name is required"}},
                            status=status.HTTP_400_BAD_REQUEST)
        l = ContactList.objects.create(account=request.user, name=name)
        return Response({"id": l.slug, "name": l.name}, status=status.HTTP_201_CREATED)


class SegmentCollectionView(BaseApiView):
    permission_classes = [HasEmailApiFeature]

    @extend_schema(operation_id="segments_list", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Contacts"])
    def get(self, request, *args, **kwargs):
        return Response({"data": [
            {"id": s.slug, "name": s.name, "definition": s.definition}
            for s in Segment.objects.filter(account=request.user)
        ]})

    @extend_schema(operation_id="segments_create", request=None, responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Contacts"])
    def post(self, request, *args, **kwargs):
        d = request.data if isinstance(request.data, dict) else {}
        if not d.get("name"):
            return Response({"error": {"code": "validation_error", "message": "name is required"}},
                            status=status.HTTP_400_BAD_REQUEST)
        definition = d.get("definition") or {}
        try:
            count = count_for(definition, request.user)
        except SegmentError as exc:
            return Response({"error": {"code": "invalid_segment", "message": str(exc)}},
                            status=status.HTTP_400_BAD_REQUEST)
        s = Segment.objects.create(account=request.user, name=d["name"], definition=definition)
        return Response({"id": s.slug, "name": s.name, "count": count},
                        status=status.HTTP_201_CREATED)


class SegmentPreviewView(BaseApiView):
    permission_classes = [HasEmailApiFeature]

    @extend_schema(operation_id="segments_preview", request=None, responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Contacts"])
    def post(self, request, *args, **kwargs):
        definition = (request.data or {}).get("definition") or {}
        try:
            count = count_for(definition, request.user)
        except SegmentError as exc:
            return Response({"error": {"code": "invalid_segment", "message": str(exc)}},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({"count": count})


class SegmentContactsView(BaseApiView):
    permission_classes = [HasEmailApiFeature]

    @extend_schema(operation_id="segments_contacts", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Contacts"])
    def get(self, request, slug, *args, **kwargs):
        s = Segment.objects.get(account=request.user, slug=slug)
        limit, offset = _paging(request)
        qs = contacts_for(s.definition, request.user)
        total = qs.count()
        return Response({
            "data": [_contact_dict(c) for c in qs[offset:offset + limit]],
            "total": total, "limit": limit, "offset": offset,
        })
