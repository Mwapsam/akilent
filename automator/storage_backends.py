"""S3 storage backends, used in production when USE_S3_STORAGE is enabled
(see settings.STORAGES). Kept separate from static/media roots so each gets
its own prefix in the bucket and its own cache/overwrite behavior.
"""

from django.conf import settings
from storages.backends.s3boto3 import S3Boto3Storage


class StaticToS3Storage(S3Boto3Storage):
    location = settings.STATICFILES_LOCATION


class mediaRootS3Boto3Storage(S3Boto3Storage):
    location = settings.MEDIAFILES_LOCATION
    file_overwrite = False
