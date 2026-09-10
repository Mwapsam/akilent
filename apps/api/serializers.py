from rest_framework import serializers


class MessageCreateSerializer(serializers.Serializer):
    """Validates the public POST /api/v1/messages payload.

    Field-level validation only (presence, email format) — the verified-
    sending-domain check and plan/quota gates live in apps.api.services,
    shared with the legacy /email/send/ shim, so both keep identical 403
    semantics rather than the generic 400 a serializer ValidationError
    would produce.
    """

    from_email = serializers.EmailField()
    to_email = serializers.EmailField()
    subject = serializers.CharField(required=False, allow_blank=True, default="")
    text = serializers.CharField(required=False, allow_blank=True, default="")
    html = serializers.CharField(required=False, allow_blank=True, default="")

    template_id = serializers.IntegerField(required=False, allow_null=True)
    template_variables = serializers.DictField(required=False, default=dict)

    @staticmethod
    def from_request_data(data: dict) -> dict:
        """Map the public JSON shape (`from`/`to`) onto this serializer's fields."""
        return {
            "from_email": data.get("from"),
            "to_email": data.get("to"),
            "subject": data.get("subject", ""),
            "text": data.get("text", ""),
            "html": data.get("html", ""),
            "template_id": data.get("template_id"),
            "template_variables": data.get("template_variables", {}),
        }


class TemplateSerializer(serializers.Serializer):
    """Validates POST/PATCH payloads for /api/v1/templates."""

    name = serializers.CharField(max_length=150)
    slug = serializers.SlugField(max_length=150, required=False, allow_blank=True)
    subject = serializers.CharField(max_length=998, required=False, allow_blank=True, default="")
    text = serializers.CharField(required=False, allow_blank=True, default="")
    html = serializers.CharField(required=False, allow_blank=True, default="")
    sample_variables = serializers.DictField(required=False, default=dict)
    content_blocks = serializers.DictField(required=False, default=dict)
    builder_mode = serializers.ChoiceField(choices=["raw", "blocks"], required=False, default="raw")

    @staticmethod
    def from_request_data(data: dict) -> dict:
        return {
            "name": data.get("name"),
            "slug": data.get("slug", ""),
            "subject": data.get("subject", ""),
            "text": data.get("text", ""),
            "html": data.get("html", ""),
            "sample_variables": data.get("sample_variables", {}),
            "content_blocks": data.get("content_blocks", {}),
            "builder_mode": data.get("builder_mode", "raw"),
        }


class BulkRecipientSerializer(serializers.Serializer):
    to = serializers.EmailField()
    variables = serializers.DictField(required=False, default=dict)


class CampaignCreateSerializer(serializers.Serializer):
    """Validates POST payloads for /api/v1/campaigns.

    Either template_id or inline subject/text/html must be provided — that
    cross-field rule is enforced in apps.api.services.create_and_queue_campaign
    (which also knows the plan's recipient cap), not here, mirroring
    MessageCreateSerializer's split of field-level vs. business validation.
    """

    from_email = serializers.EmailField()
    template_id = serializers.IntegerField(required=False, allow_null=True)
    subject = serializers.CharField(required=False, allow_blank=True, default="")
    text = serializers.CharField(required=False, allow_blank=True, default="")
    html = serializers.CharField(required=False, allow_blank=True, default="")
    recipients = BulkRecipientSerializer(many=True, required=False, default=list)
    list = serializers.SlugField(required=False, allow_blank=True, default="")
    segment = serializers.SlugField(required=False, allow_blank=True, default="")

    @staticmethod
    def from_request_data(data: dict) -> dict:
        return {
            "from_email": data.get("from"),
            "template_id": data.get("template_id"),
            "subject": data.get("subject", ""),
            "text": data.get("text", ""),
            "html": data.get("html", ""),
            "recipients": data.get("recipients", []),
            "list": data.get("list", ""),
            "segment": data.get("segment", ""),
        }
