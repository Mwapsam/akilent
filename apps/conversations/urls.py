from django.urls import path

from apps.conversations import views

app_name = "conversations"

urlpatterns = [
    path("", views.inbox, name="inbox"),
    path("feed/", views.inbox_feed, name="inbox_feed"),
    path("followups/", views.followups_due, name="followups_due"),
    path(
        "followups/<int:pk>/complete/",
        views.followup_complete,
        name="followup_complete",
    ),
    path("saved-replies/", views.saved_replies, name="saved_replies"),
    path(
        "saved-replies/<int:pk>/delete/",
        views.saved_reply_delete,
        name="saved_reply_delete",
    ),
    path("teams/", views.teams, name="teams"),
    path("teams/<int:pk>/", views.team_detail, name="team_detail"),
    path("routing-rules/", views.routing_rules, name="routing_rules"),
    path(
        "routing-rules/<int:pk>/toggle/",
        views.routing_rule_toggle,
        name="routing_rule_toggle",
    ),
    path(
        "routing-rules/<int:pk>/delete/",
        views.routing_rule_delete,
        name="routing_rule_delete",
    ),
    path("forms/", views.forms_list, name="forms_list"),
    path("forms/<int:pk>/", views.form_detail, name="form_detail"),
    path("forms/<int:pk>/delete/", views.form_delete, name="form_delete"),
    path(
        "recommendations/<int:pk>/act/",
        views.recommendation_act,
        name="recommendation-act",
    ),
    path("<str:public_id>/", views.conversation_detail, name="detail"),
    path("<str:public_id>/messages/", views.messages_feed, name="messages_feed"),
    path("<str:public_id>/ai/suggest/", views.ai_suggest, name="ai_suggest"),
    path("<str:public_id>/ai/dismiss/", views.ai_dismiss, name="ai_dismiss"),
    path("<str:public_id>/ai/apply/", views.ai_apply, name="ai_apply"),
    path(
        "<str:public_id>/create-ticket/",
        views.create_ticket_from_conversation,
        name="create_ticket",
    ),
]
