from django import template

from apps.automation.labels import step_label, trigger_label

register = template.Library()

register.filter("trigger_label", trigger_label)
register.filter("step_label", step_label)
