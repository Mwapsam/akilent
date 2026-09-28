"""Email services package.

Business logic that views and Celery tasks call.

Provisioning services (DomainService, SmtpCredentialService) orchestrate:
  - Plan limit enforcement
  - Provider calls (domain provisioning)
  - Django ORM sync
  - Audit log writes

Sending helpers (smtp_send, apply_tracking) are exported here for backwards
compatibility with existing tasks.py imports.
"""

from .domain import DomainService
from .render import (
    find_variable_paths,
    find_variables,
    flatten_variable_paths,
    render_string,
    render_template,
    validate_variables,
)
from .send import apply_tracking, smtp_send
from .smtp_credential import SmtpCredentialService

__all__ = [
    "DomainService",
    "SmtpCredentialService",
    "smtp_send",
    "apply_tracking",
    "render_template",
    "render_string",
    "find_variables",
    "find_variable_paths",
    "flatten_variable_paths",
    "validate_variables",
]
