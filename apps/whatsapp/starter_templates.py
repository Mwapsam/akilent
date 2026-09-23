"""Starter WhatsApp template library (amendment to in-Akilent template
creation, 2026-09-22): a business owner picks a scenario ("Payment
reminder") rather than starting from a blank template form. Meta's approval
lifecycle is unchanged — a starter only prefills the create-template form;
the owner still reviews, edits, and submits it themselves.

Grouped by business scenario, not by Meta template feature, per the product
principle of hiding infrastructure complexity behind the business task. Each
category maps to one card in the picker; each entry to one "Use template"
row within it.
"""
from __future__ import annotations

STARTER_CATEGORIES: list[dict] = [
    {
        "key": "conversations",
        "label": "Customer conversations",
        "icon": "message-circle",
        "blurb": "Welcome, enquiries, customer assistance",
        "templates": [
            {
                "slug": "welcome_new_customer",
                "name": "Welcome new customer",
                "use_case": "First contact",
                "meta_category": "utility",
                "body": "Hi {{1}}, thanks for contacting {{2}}.\n\nWe're happy to help. How can we assist you today?",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Business name", "example": "Acme Store"},
                ],
            },
            {
                "slug": "enquiry_follow_up",
                "name": "Enquiry follow-up",
                "use_case": "Customer asked about something",
                "meta_category": "utility",
                "body": "Hi {{1}}, following up on your question about {{2}}.\n\nHere's what you need to know: {{3}}",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "What they asked about", "example": "our delivery times"},
                    {"label": "Answer/detail", "example": "We deliver within 2-3 business days"},
                ],
            },
            {
                "slug": "quote_follow_up_conversation",
                "name": "Quote follow-up",
                "use_case": "Follow up after sending a price",
                "meta_category": "utility",
                "body": "Hi {{1}}, just following up on the quote we sent for {{2}}.\n\nLet us know if you have any questions.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "What was quoted", "example": "the website package"},
                ],
            },
            {
                "slug": "information_requested",
                "name": "Information requested",
                "use_case": "Customer asked for details",
                "meta_category": "utility",
                "body": "Hi {{1}}, here's the information you asked for:\n\n{{2}}\n\nLet us know if you need anything else.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Information", "example": "Our store is open Mon-Sat, 9am-6pm"},
                ],
            },
            {
                "slug": "human_assistance",
                "name": "Human assistance",
                "use_case": "Hand conversation to staff",
                "meta_category": "utility",
                "body": "Hi {{1}}, one of our team members will take it from here and get back to you shortly.",
                "variables": [{"label": "Customer name", "example": "Ada"}],
            },
        ],
    },
    {
        "key": "orders",
        "label": "Orders & sales",
        "icon": "building",
        "blurb": "Orders, quotes, sales",
        "templates": [
            {
                "slug": "order_confirmation",
                "name": "Order confirmation",
                "use_case": "Confirm new order",
                "meta_category": "utility",
                "body": "Hi {{1}}, your order {{2}} has been confirmed.\n\nThank you for choosing {{3}}. We'll keep you updated on the next steps.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Order number", "example": "1029"},
                    {"label": "Business name", "example": "Acme Store"},
                ],
            },
            {
                "slug": "order_update",
                "name": "Order update",
                "use_case": "Update customer",
                "meta_category": "utility",
                "body": "Hi {{1}}, an update on your order {{2}}: {{3}}",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Order number", "example": "1029"},
                    {"label": "Update", "example": "it's now being prepared"},
                ],
            },
            {
                "slug": "order_ready",
                "name": "Order ready",
                "use_case": "Product/service ready",
                "meta_category": "utility",
                "body": "Hi {{1}}, your order {{2}} is ready.\n\nLet us know when you'd like to collect it, or if you need it delivered.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Order number", "example": "1029"},
                ],
            },
            {
                "slug": "order_completed",
                "name": "Order completed",
                "use_case": "Completed order",
                "meta_category": "utility",
                "body": "Hi {{1}}, your order {{2}} is complete. Thank you for your business!",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Order number", "example": "1029"},
                ],
            },
            {
                "slug": "order_cancelled",
                "name": "Order cancelled",
                "use_case": "Cancellation",
                "meta_category": "utility",
                "body": "Hi {{1}}, your order {{2}} has been cancelled as requested.\n\nLet us know if you'd like to place a new order.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Order number", "example": "1029"},
                ],
            },
        ],
    },
    {
        "key": "payments",
        "label": "Payments",
        "icon": "card",
        "blurb": "Reminders, payment links",
        "templates": [
            {
                "slug": "payment_reminder",
                "name": "Payment reminder",
                "use_case": "Unpaid order",
                "meta_category": "utility",
                "body": "Hi {{1}}, your order {{2}} is still awaiting payment.\n\nReply to this message if you need any help.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Order number", "example": "1029"},
                ],
                "buttons": [
                    {"text": "Pay now", "url": "https://pay.example.com/{{1}}", "example": "https://pay.example.com/1029"},
                ],
            },
            {
                "slug": "payment_received",
                "name": "Payment received",
                "use_case": "Successful payment",
                "meta_category": "utility",
                "body": "Hi {{1}}, we've received your payment for order {{2}}. Thank you!",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Order number", "example": "1029"},
                ],
            },
            {
                "slug": "payment_failed",
                "name": "Payment failed",
                "use_case": "Failed payment",
                "meta_category": "utility",
                "body": "Hi {{1}}, your payment for order {{2}} didn't go through.\n\nYou can try again here:\n{{3}}",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Order number", "example": "1029"},
                    {"label": "Payment link", "example": "https://pay.example.com/1029"},
                ],
            },
            {
                "slug": "payment_link",
                "name": "Payment link",
                "use_case": "Send payment request",
                "meta_category": "utility",
                "body": "Hi {{1}}, here's your payment link for {{2}}:\n{{3}}",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "What it's for", "example": "your order"},
                    {"label": "Payment link", "example": "https://pay.example.com/1029"},
                ],
            },
            {
                "slug": "payment_overdue",
                "name": "Payment overdue",
                "use_case": "Outstanding payment",
                "meta_category": "utility",
                "body": "Hi {{1}}, your payment for order {{2}} is now overdue.\n\nPlease complete it as soon as possible: {{3}}",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Order number", "example": "1029"},
                    {"label": "Payment link", "example": "https://pay.example.com/1029"},
                ],
            },
        ],
    },
    {
        "key": "delivery",
        "label": "Delivery",
        "icon": "send",
        "blurb": "Delivery and order status",
        "templates": [
            {
                "slug": "order_dispatched",
                "name": "Order dispatched",
                "use_case": "Sent for delivery",
                "meta_category": "utility",
                "body": "Hi {{1}}, your order {{2}} has been dispatched and is on its way.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Order number", "example": "1029"},
                ],
            },
            {
                "slug": "out_for_delivery",
                "name": "Out for delivery",
                "use_case": "Driver is delivering",
                "meta_category": "utility",
                "body": "Hi {{1}}, your order {{2}} is on its way.\n\nYour order is expected to arrive {{3}}.\n\nWe'll let you know when it's delivered.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Order number", "example": "1029"},
                    {"label": "Expected time", "example": "within the next hour"},
                ],
            },
            {
                "slug": "delivery_update",
                "name": "Delivery update",
                "use_case": "Delay/change",
                "meta_category": "utility",
                "body": "Hi {{1}}, an update on your delivery for order {{2}}: {{3}}",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Order number", "example": "1029"},
                    {"label": "Update", "example": "it will now arrive tomorrow morning"},
                ],
            },
            {
                "slug": "delivered",
                "name": "Delivered",
                "use_case": "Delivery completed",
                "meta_category": "utility",
                "body": "Hi {{1}}, your order {{2}} has been delivered. Enjoy!",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Order number", "example": "1029"},
                ],
            },
        ],
    },
    {
        "key": "appointments",
        "label": "Appointments & bookings",
        "icon": "clock",
        "blurb": "Bookings and reminders",
        "templates": [
            {
                "slug": "booking_confirmation",
                "name": "Booking confirmation",
                "use_case": "Confirm booking",
                "meta_category": "utility",
                "body": "Hi {{1}}, your booking is confirmed for {{2}} at {{3}}.\n\nWe look forward to seeing you.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Date", "example": "Friday, 26 Sep"},
                    {"label": "Time", "example": "2:00pm"},
                ],
            },
            {
                "slug": "appointment_reminder",
                "name": "Appointment reminder",
                "use_case": "Upcoming appointment",
                "meta_category": "utility",
                "body": "Hi {{1}}, this is a reminder that your appointment is scheduled for {{2}} at {{3}}.\n\nPlease reply if you need to make any changes.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Date", "example": "Friday, 26 Sep"},
                    {"label": "Time", "example": "2:00pm"},
                ],
            },
            {
                "slug": "appointment_change",
                "name": "Appointment change",
                "use_case": "Reschedule",
                "meta_category": "utility",
                "body": "Hi {{1}}, your appointment has been moved to {{2}} at {{3}}.\n\nLet us know if this doesn't work for you.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "New date", "example": "Monday, 29 Sep"},
                    {"label": "New time", "example": "10:00am"},
                ],
            },
            {
                "slug": "appointment_cancelled",
                "name": "Appointment cancelled",
                "use_case": "Cancellation",
                "meta_category": "utility",
                "body": "Hi {{1}}, your appointment on {{2}} has been cancelled as requested.\n\nLet us know if you'd like to rebook.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Date", "example": "Friday, 26 Sep"},
                ],
            },
            {
                "slug": "booking_follow_up",
                "name": "Booking follow-up",
                "use_case": "Customer hasn't completed booking",
                "meta_category": "marketing",
                "body": "Hi {{1}}, we noticed you started booking {{2}} but didn't finish.\n\nWould you like to complete it? We're happy to help.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "What they were booking", "example": "an appointment"},
                ],
            },
        ],
    },
    {
        "key": "followups",
        "label": "Follow-ups",
        "icon": "workflow",
        "blurb": "Re-engage customers and opportunities",
        "templates": [
            {
                "slug": "checking_in",
                "name": "Checking in",
                "use_case": "General follow-up",
                "meta_category": "utility",
                "body": "Hi {{1}}, just checking in about {{2}}. Let us know if there's anything we can help with.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "What to check in about", "example": "your recent enquiry"},
                ],
            },
            {
                "slug": "still_interested",
                "name": "Still interested?",
                "use_case": "Dormant opportunity",
                "meta_category": "marketing",
                "body": "Hi {{1}}, just checking in about {{2}}.\n\nAre you still interested? If you have any questions, we're happy to help.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "What they were interested in", "example": "the website package"},
                ],
            },
            {
                "slug": "quote_follow_up",
                "name": "Quote follow-up",
                "use_case": "No response after quote",
                "meta_category": "marketing",
                "body": "Hi {{1}}, following up on the quote for {{2}}.\n\nWould you like to go ahead, or do you have any questions?",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "What was quoted", "example": "the website package"},
                ],
            },
            {
                "slug": "order_follow_up",
                "name": "Order follow-up",
                "use_case": "Incomplete order",
                "meta_category": "utility",
                "body": "Hi {{1}}, you have an order for {{2}} that hasn't been completed yet.\n\nWould you like help finishing it?",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "What the order was for", "example": "your cart"},
                ],
            },
            {
                "slug": "need_help",
                "name": "Need help?",
                "use_case": "Customer stopped responding",
                "meta_category": "marketing",
                "body": "Hi {{1}}, we haven't heard back from you about {{2}}.\n\nIs there anything we can help with?",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "What to reference", "example": "your enquiry"},
                ],
            },
        ],
    },
    {
        "key": "retention",
        "label": "Customer retention",
        "icon": "users",
        "blurb": "Keep customers coming back",
        "templates": [
            {
                "slug": "thank_you",
                "name": "Thank you",
                "use_case": "After purchase",
                "meta_category": "utility",
                "body": "Hi {{1}}, thank you for your purchase! We hope you enjoy {{2}}.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "What they bought", "example": "your order"},
                ],
            },
            {
                "slug": "customer_check_in",
                "name": "Customer check-in",
                "use_case": "Existing customer",
                "meta_category": "marketing",
                "body": "Hi {{1}}, we hope you're enjoying {{2}}.\n\nIf you need anything or would like to explore our latest products and services, we're here to help.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "What they bought", "example": "your purchase"},
                ],
            },
            {
                "slug": "come_back",
                "name": "Come back",
                "use_case": "Re-engagement",
                "meta_category": "marketing",
                "body": "Hi {{1}}, it's been a while! We'd love to see you again.\n\n{{2}}",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Offer/reason to return", "example": "Here's 10% off your next order"},
                ],
            },
            {
                "slug": "new_product",
                "name": "New product",
                "use_case": "Existing customers",
                "meta_category": "marketing",
                "body": "Hi {{1}}, we thought you'd like to know about {{2}}.\n\nLet us know if you'd like more details.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "New product/service", "example": "our new arrivals"},
                ],
            },
            {
                "slug": "service_reminder",
                "name": "Service reminder",
                "use_case": "Repeat business",
                "meta_category": "marketing",
                "body": "Hi {{1}}, it might be time for {{2}} again.\n\nLet us know if you'd like to book.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Service", "example": "your regular service"},
                ],
            },
        ],
    },
    {
        "key": "promotions",
        "label": "Promotions",
        "icon": "sparkles",
        "blurb": "Offers and announcements",
        "templates": [
            {
                "slug": "new_offer",
                "name": "New offer",
                "use_case": "General promotion",
                "meta_category": "marketing",
                "body": "Hi {{1}}, we have a special offer for you.\n\n{{2}}\n\nReply to this message if you'd like more information.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Offer details", "example": "20% off all orders this week"},
                ],
                "buttons": [
                    {"text": "Learn more", "url": "https://example.com/offer", "example": ""},
                ],
            },
            {
                "slug": "product_launch",
                "name": "Product launch",
                "use_case": "New product",
                "meta_category": "marketing",
                "body": "Hi {{1}}, we just launched {{2}}!\n\nCheck it out here:\n{{3}}",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "New product", "example": "our new collection"},
                    {"label": "Link", "example": "https://example.com/new"},
                ],
            },
            {
                "slug": "special_promotion",
                "name": "Special promotion",
                "use_case": "Limited offer",
                "meta_category": "marketing",
                "body": "Hi {{1}}, for a limited time: {{2}}\n\nOffer ends {{3}}.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Offer details", "example": "buy one get one free"},
                    {"label": "End date", "example": "30 Sep"},
                ],
            },
            {
                "slug": "seasonal_offer",
                "name": "Seasonal offer",
                "use_case": "Holiday/seasonal",
                "meta_category": "marketing",
                "body": "Hi {{1}}, celebrate the season with us!\n\n{{2}}",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Offer details", "example": "15% off all holiday orders"},
                ],
            },
            {
                "slug": "returning_customer_offer",
                "name": "Returning customer offer",
                "use_case": "Existing customers",
                "meta_category": "marketing",
                "body": "Hi {{1}}, as a valued customer, here's something special for you: {{2}}",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Offer details", "example": "10% off your next order"},
                ],
            },
        ],
    },
    {
        "key": "feedback",
        "label": "Feedback",
        "icon": "check",
        "blurb": "Ask customers how it went",
        "templates": [
            {
                "slug": "request_feedback",
                "name": "Request feedback",
                "use_case": "After interaction",
                "meta_category": "utility",
                "body": "Hi {{1}}, we'd love to hear how your experience with {{2}} was.\n\nYour feedback helps us improve. Thank you for choosing us.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "What to ask about", "example": "our service"},
                ],
            },
            {
                "slug": "product_feedback",
                "name": "Product feedback",
                "use_case": "After purchase",
                "meta_category": "utility",
                "body": "Hi {{1}}, how are you finding {{2}}?\n\nWe'd love to hear your thoughts.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Product", "example": "your recent purchase"},
                ],
            },
            {
                "slug": "service_feedback",
                "name": "Service feedback",
                "use_case": "After service",
                "meta_category": "utility",
                "body": "Hi {{1}}, how was your recent experience with {{2}}?\n\nWe'd appreciate your feedback.",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "Service", "example": "our service"},
                ],
            },
            {
                "slug": "review_request",
                "name": "Review request",
                "use_case": "Ask for review",
                "meta_category": "marketing",
                "body": "Hi {{1}}, if you have a moment, we'd really appreciate a review of {{2}}.\n\n{{3}}",
                "variables": [
                    {"label": "Customer name", "example": "Ada"},
                    {"label": "What to review", "example": "your recent order"},
                    {"label": "Review link", "example": "https://example.com/review"},
                ],
            },
        ],
    },
]
