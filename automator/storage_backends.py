"""S3 storage backends, used in production when USE_S3_STORAGE is enabled
(see settings.STORAGES). Kept separate from static/media roots so each gets
its own prefix in the bucket and its own cache/overwrite behavior.
"""

from django.conf import settings
from storages.backends.s3 import S3ManifestStaticStorage
from storages.backends.s3boto3 import S3Boto3Storage


class StaticToS3Storage(S3ManifestStaticStorage):
    """Static files under content-hashed names (js/app.624f0e9a1f3a.js).

    They're served with a one-year max-age (AWS_S3_OBJECT_PARAMETERS), so a fixed name
    kept last deploy's script in browsers and CloudFront: a fresh template ran against old
    JavaScript. A new hash is a new URL. Not strict: a name missing from the manifest falls
    back to its plain name rather than failing the page.
    """

    location = settings.STATICFILES_LOCATION
    manifest_strict = False


class mediaRootS3Boto3Storage(S3Boto3Storage):  # noqa: N801
    location = settings.MEDIAFILES_LOCATION
    file_overwrite = False
