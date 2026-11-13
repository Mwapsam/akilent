import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def post_message(text: str, *, dedupe_key: str = "", cooldown: int = 3600) -> bool:
    """Post ``text`` to the configured Slack incoming webhook. Returns whether it posted.

    A no-op (debug-logged) if SLACK_WEBHOOK_URL isn't set, and never raises —
    Slack being down must not block payment review, which also goes out by
    email.

    ``dedupe_key``: alerts that can fire once per failing item (a message, a send) pass a
    key, and only the first post for that key within ``cooldown`` seconds goes out. The
    rest are logged, so a burst of the same failure is one Slack message, not hundreds.
    """
    url = getattr(settings, "SLACK_WEBHOOK_URL", "")
    if not url:
        logger.debug("slack.post_message: SLACK_WEBHOOK_URL not configured, skipping")
        return False

    if dedupe_key:
        try:
            from django.core.cache import cache

            # add() is atomic: two workers racing on the same alert can't both post.
            if not cache.add(f"slack-alert:{dedupe_key}", 1, timeout=cooldown):
                logger.info("slack.post_message: %s within cooldown, skipped", dedupe_key)
                return False
        except Exception:
            # No cache: better a duplicate alert than a missing one.
            logger.exception("slack.post_message: dedupe check failed, posting anyway")

    try:
        response = requests.post(url, json={"text": text}, timeout=10)
        response.raise_for_status()
    except requests.RequestException:
        logger.exception("slack.post_message: failed to post to Slack")
        return False
    return True
