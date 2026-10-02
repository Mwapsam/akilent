from django.urls import path

from apps.conversations import insights_views

# Unnamespaced, like apps.accounts.urls' dashboard — {% url 'insights' %} works from any
# template with no namespace prefix.
urlpatterns = [
    path("", insights_views.insights, name="insights"),
    path(
        "<int:pk>/acknowledge/",
        insights_views.insight_acknowledge,
        name="insight-acknowledge",
    ),
    path("<int:pk>/dismiss/", insights_views.insight_dismiss, name="insight-dismiss"),
    path("<int:pk>/act/", insights_views.insight_act, name="insight-act"),
    path(
        "goals/create/",
        insights_views.insights_goal_create,
        name="insights-goal-create",
    ),
    path(
        "goals/<int:pk>/update/",
        insights_views.insights_goal_update,
        name="insights-goal-update",
    ),
    path(
        "goals/<int:pk>/delete/",
        insights_views.insights_goal_delete,
        name="insights-goal-delete",
    ),
    path(
        "report-settings/",
        insights_views.insights_report_settings,
        name="insights-report-settings",
    ),
    path("policies/", insights_views.policies, name="insight-policies"),
    path(
        "<int:pk>/automate/",
        insights_views.policy_from_insight,
        name="policy-from-insight",
    ),
    path(
        "policies/<int:pk>/status/",
        insights_views.policy_update_status,
        name="insight-policy-update-status",
    ),
]
