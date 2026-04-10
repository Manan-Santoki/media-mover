"""Webhook dispatcher for Pickrr and other services.

Sends JSON payloads to configured webhook URLs with HMAC signature
for authentication. Supports multiple event types.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime

import httpx
import structlog

from mediasorter.config import WebhookEndpoint

log = structlog.get_logger(__name__)


def send_webhook(
    config: WebhookEndpoint,
    event: str,
    payload: dict,
) -> bool:
    """POST a webhook event to the configured URL.

    Args:
        config: Webhook endpoint configuration.
        event: Event type (e.g. "upcoming_episode", "scan_complete").
        payload: Event-specific data.

    Returns:
        True if the webhook was delivered successfully.
    """
    if not config.url:
        return False

    # Check if this event type is enabled
    if config.events and event not in config.events:
        log.debug("webhook_event_filtered", event=event)
        return False

    body = {
        "event": event,
        "timestamp": datetime.now().isoformat(),
        **payload,
    }

    body_json = json.dumps(body, default=str)

    headers = {"Content-Type": "application/json"}

    # Sign payload with HMAC if secret is configured
    if config.secret:
        signature = hmac.new(
            config.secret.encode(),
            body_json.encode(),
            hashlib.sha256,
        ).hexdigest()
        headers["X-Webhook-Signature"] = f"sha256={signature}"

    try:
        response = httpx.post(
            config.url,
            content=body_json,
            headers=headers,
            timeout=30,
        )
        success = response.status_code < 300
        if success:
            log.info("webhook_sent", event=event, url=config.url)
        else:
            log.warning(
                "webhook_failed",
                event=event,
                url=config.url,
                status=response.status_code,
            )
        return success
    except Exception as e:
        log.error("webhook_error", event=event, url=config.url, error=str(e))
        return False
