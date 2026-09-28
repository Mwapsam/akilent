"""Starter EmailTemplate content offered to new accounts.

Used two ways:
  - apps.email.signals seeds one EmailTemplate row per entry for every newly
    created Account (get_or_create by slug, so re-running is a no-op).
  - templates_list passes this same content to the frontend so the "Start
    from a sample" picker on the create-template form can prefill fields
    client-side before the user saves their own copy.

Kept as plain data (no DB dependency) so both call sites share one source of
truth without a migration.
"""

from __future__ import annotations

STARTER_TEMPLATES: list[dict] = [
    {
        "slug": "welcome-email",
        "name": "Welcome email",
        "subject": "Welcome to {{ company_name }}, {{ first_name }}!",
        "text_body": (
            "Hi {{ first_name }},\n\n"
            "Welcome to {{ company_name }}! We're glad to have you on board.\n\n"
            "If you have any questions, just reply to this email — we're happy to help.\n\n"
            "— The {{ company_name }} team"
        ),
        "html_body": (
            '<div style="font-family:Arial,Helvetica,sans-serif;max-width:520px;margin:0 auto;color:#12182B;">\n'
            '  <h1 style="font-size:20px;margin:0 0 16px;">Welcome, {{ first_name }}!</h1>\n'
            '  <p style="font-size:14px;line-height:1.6;margin:0 0 16px;">Thanks for joining '
            "{{ company_name }}. We're glad to have you on board.</p>\n"
            '  <p style="font-size:14px;line-height:1.6;margin:0 0 24px;">'
            "If you have any questions, just reply to this email — we're happy to help.</p>\n"
            '  <a href="{{ login_url }}" style="display:inline-block;background:#FFB020;color:#12182B;'
            'font-weight:600;text-decoration:none;padding:10px 20px;border-radius:8px;font-size:14px;">'
            "Get started</a>\n"
            "</div>"
        ),
        "sample_variables": {
            "first_name": "Ada",
            "company_name": "Acme Inc",
            "login_url": "https://example.com/login",
        },
    },
    {
        "slug": "payment-receipt",
        "name": "Payment receipt",
        "subject": "Your receipt from {{ company_name }} — {{ invoice_number }}",
        "text_body": (
            "Hi {{ first_name }},\n\n"
            "Thanks for your payment. Here's your receipt:\n\n"
            "Invoice: {{ invoice_number }}\n"
            "Amount: {{ amount }}\n"
            "Date: {{ date }}\n\n"
            "— The {{ company_name }} team"
        ),
        "html_body": (
            '<div style="font-family:Arial,Helvetica,sans-serif;max-width:520px;margin:0 auto;color:#12182B;">\n'
            '  <h1 style="font-size:20px;margin:0 0 16px;">Payment received</h1>\n'
            '  <p style="font-size:14px;line-height:1.6;margin:0 0 16px;">Hi {{ first_name }}, '
            "thanks for your payment — here's your receipt.</p>\n"
            '  <table style="width:100%;border-collapse:collapse;font-size:14px;margin:0 0 16px;">\n'
            '    <tr><td style="padding:8px 0;color:#657089;">Invoice</td>'
            '<td style="padding:8px 0;text-align:right;">{{ invoice_number }}</td></tr>\n'
            '    <tr style="border-top:1px solid #E8EDF3;"><td style="padding:8px 0;color:#657089;">Amount</td>'
            '<td style="padding:8px 0;text-align:right;font-weight:600;">{{ amount }}</td></tr>\n'
            '    <tr style="border-top:1px solid #E8EDF3;"><td style="padding:8px 0;color:#657089;">Date</td>'
            '<td style="padding:8px 0;text-align:right;">{{ date }}</td></tr>\n'
            "  </table>\n"
            '  <p style="font-size:12px;color:#657089;">{{ company_name }}</p>\n'
            "</div>"
        ),
        "sample_variables": {
            "first_name": "Ada",
            "company_name": "Acme Inc",
            "invoice_number": "INV-1042",
            "amount": "$49.00",
            "date": "July 14, 2026",
        },
    },
    {
        "slug": "newsletter-update",
        "name": "Newsletter update",
        "subject": "{{ company_name }} update: {{ headline }}",
        "text_body": (
            "Hi {{ first_name }},\n\n"
            "{{ headline }}\n\n"
            "Here's what's new this month — reply to this email if you have any questions.\n\n"
            "— The {{ company_name }} team"
        ),
        "html_body": (
            '<div style="font-family:Arial,Helvetica,sans-serif;max-width:520px;margin:0 auto;color:#12182B;">\n'
            '  <div style="background:#0E1526;color:#fff;padding:20px;border-radius:12px 12px 0 0;">\n'
            '    <p style="margin:0;font-size:12px;letter-spacing:.05em;text-transform:uppercase;opacity:.7;">'
            "{{ company_name }}</p>\n"
            '    <h1 style="margin:8px 0 0;font-size:20px;">{{ headline }}</h1>\n'
            "  </div>\n"
            '  <div style="padding:20px;border:1px solid #E8EDF3;border-top:none;border-radius:0 0 12px 12px;">\n'
            "    <p style=\"font-size:14px;line-height:1.6;margin:0;\">Hi {{ first_name }}, here's what's "
            "new this month. Reply to this email if you have any questions.</p>\n"
            "  </div>\n"
            "</div>"
        ),
        "sample_variables": {
            "first_name": "Ada",
            "company_name": "Acme Inc",
            "headline": "New features this month",
        },
    },
]
