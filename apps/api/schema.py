"""drf-spectacular hooks for the custom API-key auth."""
import re

from drf_spectacular.extensions import OpenApiAuthenticationExtension

_VERSION_SEG = re.compile(r"/\{version\}")


def only_public_api(endpoints, **kwargs):
    """Preprocessing hook: keep only the versioned public API surface."""
    kept = [
        (path, path_regex, method, callback)
        for (path, path_regex, method, callback) in endpoints
        if path.startswith("/api/") and "schema" not in path and "docs" not in path
        and "reference" not in path
    ]
    return kept


def strip_version_param(result, generator, request, public, **kwargs):
    """Collapse ``/api/{version}/x`` to ``/api/v1/x`` and drop the version param.

    Our URLconf uses a ``<str:version>`` segment for URLPathVersioning; the
    published spec pins v1 via SERVERS, so the placeholder is noise.
    """
    new_paths = {}
    for path, item in result.get("paths", {}).items():
        new_path = _VERSION_SEG.sub("/v1", path, count=1)
        for operation in item.values():
            if not isinstance(operation, dict):
                continue
            params = operation.get("parameters")
            if params:
                operation["parameters"] = [
                    p for p in params if p.get("name") != "version"
                ]
        new_paths[new_path] = item
    result["paths"] = new_paths
    return result


class EmailApiKeyScheme(OpenApiAuthenticationExtension):
    target_class = "apps.api.authentication.EmailApiKeyAuthentication"
    name = "ApiKeyAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "header",
            "name": "X-Api-Key",
            "description": (
                "Your account API key. Also accepted as `Authorization: Bearer <key>`."
            ),
        }
