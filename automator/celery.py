import os

from celery import Celery
from celery.signals import before_task_publish, task_postrun, task_prerun

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "automator.settings")

app = Celery("automator")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

_HEADER = "x_request_id"
_prerun_tokens: dict = {}


@before_task_publish.connect
def _attach_request_id(headers=None, **_):
    """Stamp the enqueuing request's id onto the task message headers."""
    from apps.core.request_context import get_request_id

    request_id = get_request_id()
    if request_id and headers is not None:
        headers[_HEADER] = request_id


@task_prerun.connect
def _restore_request_id(task_id=None, task=None, **_):
    from apps.core.request_context import set_request_id

    request = getattr(task, "request", None)
    request_id = getattr(request, _HEADER, None) if request is not None else None
    if request_id:
        _prerun_tokens[task_id] = set_request_id(request_id)


@task_postrun.connect
def _clear_request_id(task_id=None, **_):
    from apps.core.request_context import reset_request_id

    token = _prerun_tokens.pop(task_id, None)
    if token is not None:
        reset_request_id(token)
