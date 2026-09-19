"""Test urlconf: the project only mounts /whatsapp/ when WHATSAPP_ENABLED.

Keeps every project route (full page layouts reverse names like
``settings-profile``) and adds the WhatsApp routes if they aren't mounted.
"""
from django.urls import include, path

from automator.urls import urlpatterns as project_patterns

urlpatterns = list(project_patterns)
if not any(getattr(p, "namespace", None) is None and str(p.pattern) == "whatsapp/" for p in urlpatterns):
    urlpatterns.append(path("whatsapp/", include("apps.whatsapp.urls")))
