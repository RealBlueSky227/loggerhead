from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Any

from .config import MQTTConfig, TelegramConfig

LOGGER = logging.getLogger(__name__)


class TelegramNotifier:
    """Telegram push notifications.

    Implements SRS 4.1 through 4.5.
    """

    def __init__(self, config: TelegramConfig) -> None:
        self.config = config

    def send(self, text: str) -> bool:
        if not self.config.enabled or not self.config.bot_token or not self.config.chat_id:
            LOGGER.info("Telegram disabled: %s", text)
            return False
        url = f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": self.config.chat_id, "text": text}).encode("utf-8")
        try:
            with urllib.request.urlopen(url, data=data, timeout=5) as response:
                return response.status < 300
        except Exception as exc:
            LOGGER.warning("Telegram notification failed: %s", exc)
            return False


class MQTTHomeAssistantBridge:
    """MQTT telemetry/command bridge for Home Assistant.

    Implements SRS 4.8.1 and 4.8.2.
    """

    def __init__(self, config: MQTTConfig) -> None:
        self.config = config
        self.client: Any = None
        self.command_handler = None
        if config.enabled:
            try:
                import paho.mqtt.client as mqtt  # type: ignore

                self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
                if config.username:
                    self.client.username_pw_set(config.username, config.password)
                self.client.on_message = self._on_message
                self.client.connect(config.host, config.port, keepalive=60)
                self.client.subscribe(f"{config.base_topic}/command/#")
                self.client.loop_start()
            except Exception as exc:
                LOGGER.warning("MQTT bridge disabled after connection failure: %s", exc)
                self.client = None

    def set_command_handler(self, handler) -> None:
        self.command_handler = handler

    def publish(self, topic: str, payload: Any, *, retain: bool = True) -> None:
        if not self.client:
            return
        full_topic = f"{self.config.base_topic}/{topic.strip('/')}"
        self.client.publish(full_topic, json.dumps(payload, sort_keys=True, default=str), retain=retain)

    def _on_message(self, _client, _userdata, message) -> None:
        if not self.command_handler:
            return
        topic = message.topic.removeprefix(f"{self.config.base_topic}/command/").strip("/")
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except json.JSONDecodeError:
            payload = message.payload.decode("utf-8")
        self.command_handler(topic, payload)

    def close(self) -> None:
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()


class NotificationLimiter:
    """Per-alert notification frequency limiter.

    Implements SRS 4.5 Notification Frequency Management.
    """

    def __init__(self) -> None:
        self.last_sent: dict[str, float] = {}

    def should_send(self, key: str, frequency_seconds: float) -> bool:
        now = time.time()
        previous = self.last_sent.get(key, 0.0)
        if now - previous >= frequency_seconds:
            self.last_sent[key] = now
            return True
        return False
