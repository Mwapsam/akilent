"""Production static files get content-hashed names (automator/storage_backends.py).

They're served with a one-year max-age, so under a fixed name a browser kept running last
deploy's app.js and media_library.js against new templates (a ReferenceError in the media
editor). These check the storage hashes, and that every stylesheet's url() references
resolve, since a broken one fails collectstatic and with it the deploy.
"""

import importlib
import json

from django.contrib.staticfiles.storage import ManifestFilesMixin
from django.core.management import call_command
from django.test import override_settings


def test_production_static_storage_hashes_names_without_failing_pages():
    with override_settings(STATICFILES_LOCATION="static", MEDIAFILES_LOCATION="media"):
        backends = importlib.import_module("automator.storage_backends")
    assert issubclass(backends.StaticToS3Storage, ManifestFilesMixin)
    assert backends.StaticToS3Storage.manifest_strict is False


def test_collectstatic_with_hashed_names_succeeds(tmp_path, settings):
    settings.STATIC_ROOT = str(tmp_path)
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"
        },
    }
    call_command("collectstatic", interactive=False, verbosity=0)
    paths = json.loads((tmp_path / "staticfiles.json").read_text())["paths"]
    for name in ("js/app.js", "js/media_library.js", "css/app.css"):
        assert paths[name] != name
