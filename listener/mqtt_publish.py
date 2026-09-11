"""
MQTT publisher for device wake commands. Replaces Azure IoT Hub C2D
(listener/iot_hub_mqtt.py): same JSON payloads, delivered through the
Mosquitto broker in docker compose instead of IoT Hub.

Topic:   '{MQTT_COMMAND_TOPIC_PREFIX}/{device_id}/cmd'
QoS 1 + retained, so a device that is asleep on LTE gets the latest command
the moment it reconnects and subscribes.

Environment (documented in deploy/.env.example):
    MQTT_HOST                  broker hostname            (default: mosquitto)
    MQTT_PORT                  broker port                (default: 8883)
    MQTT_TLS                   "1"/"0": use TLS. Default: on, except on the
                               conventional plaintext port 1883 (the compose-internal
                               listener in deploy/mosquitto/mosquitto.conf).
    MQTT_USERNAME              backend/worker user
    MQTT_PASSWORD              backend/worker password
    MQTT_CA_CERT               PEM bundle that signs the broker cert; empty = system CAs
    MQTT_TLS_HOST              name the broker's certificate is issued for (the
                               public sslip.io host the device dials)
    MQTT_TLS_INSECURE          "1"/"0": skip only the hostname match (the chain is
                               still verified). Default: skipped when MQTT_HOST differs
                               from MQTT_TLS_HOST, i.e. when dialling the compose
                               service name while the broker presents the public cert.
    MQTT_COMMAND_TOPIC_PREFIX  topic prefix               (default: aipin)
    DEVICE_ID                  default target device      (default: esp32s3)
"""
import json
import logging
import os
from typing import Any

import paho.mqtt.client as mqtt
import paho.mqtt.publish as publish

log = logging.getLogger(__name__)

DEFAULT_DEVICE_ID = os.getenv("DEVICE_ID", "esp32s3")
PLAINTEXT_PORT = 1883


def command_topic(device_id: str) -> str:
    """Topic the device subscribes to for commands."""
    prefix = os.getenv("MQTT_COMMAND_TOPIC_PREFIX", "aipin").strip("/")
    return f"{prefix}/{device_id}/cmd"


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def _tls_settings(host: str, port: int) -> dict[str, Any] | None:
    """paho `tls` parameter: None for plaintext, {} for system CAs, or explicit options."""
    if not _env_flag("MQTT_TLS", default=(port != PLAINTEXT_PORT)):
        return None
    ca_cert = os.getenv("MQTT_CA_CERT", "").strip()
    tls: dict[str, Any] = {"ca_certs": ca_cert} if ca_cert else {}  # {} -> client.tls_set() with system CAs
    # The certificate names MQTT_TLS_HOST; any other name (the compose service
    # name, say) can only pass with the hostname check skipped. Chain is still verified.
    tls_host = os.getenv("MQTT_TLS_HOST", "").strip()
    if _env_flag("MQTT_TLS_INSECURE", default=bool(tls_host) and host != tls_host):
        tls["insecure"] = True
    return tls


def send_to_device(device_id: str, payload: dict[str, Any]) -> None:
    """
    Publish one command to a device (QoS 1, retained) and return once the
    broker has acknowledged it. Raises on connection/auth/TLS failure.

    Args:
        device_id: Target device ID (e.g. "esp32s3").
        payload:   JSON-serialisable command, e.g. {"command": "start_websocket", ...}.
    """
    host = os.getenv("MQTT_HOST", "mosquitto")
    port = int(os.getenv("MQTT_PORT", "8883"))
    username = os.getenv("MQTT_USERNAME", "")
    password = os.getenv("MQTT_PASSWORD", "")
    tls = _tls_settings(host, port)

    topic = command_topic(device_id)
    body = json.dumps(payload)

    # publish.single connects, waits for the QoS 1 PUBACK, then disconnects.
    # It raises MQTTException on a rejected CONNECT (bad credentials) and the
    # usual socket/ssl errors on transport failures.
    publish.single(
        topic,
        payload=body,
        qos=1,
        retain=True,
        hostname=host,
        port=port,
        client_id=f"{username or 'backend'}-pub-{os.getpid()}",
        keepalive=30,
        auth={"username": username, "password": password} if username else None,
        tls=tls,
        protocol=mqtt.MQTTv311,
    )
    log.info("published to %s via %s:%s (%s): %s", topic, host, port, "tls" if tls is not None else "plaintext", body)


def main() -> None:
    """Manual check: python listener/mqtt_publish.py  (sends a ping to DEVICE_ID)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from dotenv import load_dotenv

    load_dotenv()
    send_to_device(DEFAULT_DEVICE_ID, {"command": "ping", "message": "Hello from the backend"})


if __name__ == "__main__":
    main()
