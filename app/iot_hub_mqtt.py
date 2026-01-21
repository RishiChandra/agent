"""
Azure IoT Hub MQTT Client

This module provides functionality to send MQTT messages to Azure IoT Hub.
IoT Hub: ai-pin-iot-hub.azure-devices.net (West US 2)

Supports:
- Device-to-Cloud (D2C): Device sends messages to IoT Hub
- Cloud-to-Device (C2D): Backend sends messages to a specific device
"""

import os
import json
import asyncio
import base64
import hashlib
import hmac
import time
import urllib.parse
from typing import Optional, Any
from dotenv import load_dotenv
import requests

from azure.iot.device.aio import IoTHubDeviceClient
from azure.iot.device import Message

# `azure-iot-hub` (service SDK) is optional: on some macOS/Python 3.12 setups it
# can fail to install due to its `uamqp` dependency requiring a compatible CMake.
# We only need it for Cloud-to-Device (C2D) service-side operations.
try:
    from azure.iot.hub import IoTHubRegistryManager
    from azure.iot.hub.models import CloudToDeviceMethod
except Exception:  # pragma: no cover
    IoTHubRegistryManager = None  # type: ignore[assignment]
    CloudToDeviceMethod = None  # type: ignore[assignment]

load_dotenv()

# IoT Hub Configuration
IOT_HUB_HOSTNAME = "ai-pin-iot-hub.azure-devices.net"
DEFAULT_DEVICE_ID = "esp32s3"


def _parse_iothub_connection_string(conn_str: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    for segment in conn_str.split(";"):
        if not segment.strip():
            continue
        if "=" not in segment:
            continue
        k, v = segment.split("=", 1)
        parts[k] = v
    return parts


def _generate_sas_token(
    resource_uri: str,
    key_b64: str,
    key_name: str,
    expiry_in_seconds: int = 3600,
) -> str:
    """
    Generate an IoT Hub SAS token.

    resource_uri: typically the IoT Hub hostname (lowercased), e.g. "myhub.azure-devices.net"
    """
    expiry = int(time.time()) + int(expiry_in_seconds)
    encoded_uri = urllib.parse.quote(resource_uri.lower(), safe="")
    to_sign = f"{encoded_uri}\n{expiry}".encode("utf-8")
    key = base64.b64decode(key_b64)
    signature = base64.b64encode(hmac.new(key, to_sign, hashlib.sha256).digest())
    encoded_sig = urllib.parse.quote(signature.decode("utf-8"), safe="")
    return f"SharedAccessSignature sr={encoded_uri}&sig={encoded_sig}&se={expiry}&skn={urllib.parse.quote(key_name, safe='')}"


class IoTHubC2DHttpClient:
    """C2D sender via IoT Hub HTTPS REST API (no `azure-iot-hub` dependency)."""

    def __init__(self, connection_string: str, api_version: str = "2018-06-30"):
        parts = _parse_iothub_connection_string(connection_string)
        self.hostname = parts.get("HostName") or parts.get("Hostname") or parts.get("hostName")
        self.key_name = parts.get("SharedAccessKeyName")
        self.key = parts.get("SharedAccessKey")
        if not self.hostname or not self.key_name or not self.key:
            raise ValueError(
                "Invalid IoT Hub *service* connection string. Expected HostName, SharedAccessKeyName, SharedAccessKey."
            )
        self.api_version = api_version

    def send_c2d_message(
        self,
        device_id: str,
        payload: dict[str, Any],
        properties: Optional[dict[str, str]] = None,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        sas = _generate_sas_token(self.hostname, self.key, self.key_name, expiry_in_seconds=3600)
        url = (
            f"https://{self.hostname}/devices/{urllib.parse.quote(device_id, safe='')}"
            f"/messages/deviceBound?api-version={urllib.parse.quote(self.api_version, safe='')}"
        )
        body = json.dumps(payload).encode("utf-8")

        headers: dict[str, str] = {
            "Authorization": sas,
            "Content-Type": "application/json",
            "iothub-to": f"/devices/{device_id}/messages/deviceBound",
        }

        # Optional: message system properties via headers
        # https://learn.microsoft.com/azure/iot-hub/iot-hub-devguide-messages-construct
        if ttl_seconds is not None:
            headers["iothub-expiry"] = str(int(ttl_seconds))

        # Custom application properties are query-string parameters on this endpoint:
        # .../messages/deviceBound?api-version=...&k1=v1&k2=v2
        if properties:
            # Add only custom properties as query params; keep content-type/encoding in headers.
            parsed = urllib.parse.urlsplit(url)
            q = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            for k, v in properties.items():
                if k in ("content_type", "content_encoding"):
                    continue
                q.append((k, v))
            url = urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(q), parsed.fragment)
            )

        resp = requests.post(url, data=body, headers=headers, timeout=30)
        if resp.status_code not in (200, 204):
            raise RuntimeError(f"C2D send failed: {resp.status_code} {resp.text}")
        print(f"C2D message sent to {device_id}: {payload}")


class IoTHubC2DClient:
    """Client for sending Cloud-to-Device messages to IoT Hub devices."""
    
    def __init__(self, connection_string: Optional[str] = None):
        """
        Initialize the IoT Hub C2D client.
        
        Args:
            connection_string: IoT Hub service connection string (NOT device connection string).
                             Format: HostName=<hostname>;SharedAccessKeyName=<policy>;SharedAccessKey=<key>
                             If not provided, will read from IOT_HUB_SERVICE_CONNECTION_STRING env variable.
        """
        self.connection_string = connection_string or os.getenv("IOT_HUB_SERVICE_CONNECTION_STRING")
        if not self.connection_string:
            raise ValueError(
                "IoT Hub service connection string is required. "
                "Set IOT_HUB_SERVICE_CONNECTION_STRING environment variable or pass it directly. "
                "Get this from Azure Portal -> IoT Hub -> Shared access policies -> service or iothubowner"
            )

        # Prefer the official SDK when available; otherwise fall back to HTTPS REST API.
        self.registry_manager = IoTHubRegistryManager(self.connection_string) if IoTHubRegistryManager else None
        self.http_client = None if self.registry_manager else IoTHubC2DHttpClient(self.connection_string)
    
    def send_c2d_message(
        self,
        device_id: str,
        payload: dict[str, Any],
        properties: Optional[dict[str, str]] = None
    ) -> None:
        """
        Send a Cloud-to-Device message to a specific device.
        
        Args:
            device_id: Target device ID (e.g., "esp32s3").
            payload: Dictionary containing the message data.
            properties: Optional custom properties for the message.
        """
        message_body = json.dumps(payload)
        
        # Build properties dict
        props = properties or {}
        props["content_type"] = "application/json"
        props["content_encoding"] = "utf-8"

        if self.registry_manager:
            self.registry_manager.send_c2d_message(device_id, message_body, properties=props)
            print(f"C2D message sent to {device_id}: {message_body[:100]}...")
            return

        # HTTPS fallback: send payload + custom properties.
        assert self.http_client is not None
        self.http_client.send_c2d_message(device_id=device_id, payload=payload, properties=props)
    
    def invoke_device_method(
        self,
        device_id: str,
        method_name: str,
        payload: Optional[dict[str, Any]] = None,
        timeout: int = 30
    ) -> dict:
        """
        Invoke a direct method on a device.
        
        Args:
            device_id: Target device ID.
            method_name: Name of the method to invoke.
            payload: Optional payload for the method.
            timeout: Timeout in seconds.
            
        Returns:
            Response from the device.
        """
        if CloudToDeviceMethod is None or not self.registry_manager:
            raise ImportError(
                "Direct methods require the optional `azure-iot-hub` package. "
                "Install it (typically in Linux/Docker) to use invoke_device_method()."
            )
        method = CloudToDeviceMethod(
            method_name=method_name,
            payload=payload or {},
            response_timeout_in_seconds=timeout
        )
        
        response = self.registry_manager.invoke_device_method(device_id, method)
        print(f"Method '{method_name}' invoked on {device_id}, status: {response.status}")
        return {"status": response.status, "payload": response.payload}


class IoTHubMQTTClient:
    """Client for sending Device-to-Cloud MQTT messages to Azure IoT Hub."""
    
    def __init__(self, connection_string: Optional[str] = None):
        """
        Initialize the IoT Hub MQTT client.
        
        Args:
            connection_string: Device connection string from Azure IoT Hub.
                             Format: HostName=<hostname>;DeviceId=<device_id>;SharedAccessKey=<key>
                             If not provided, will read from IOT_HUB_CONNECTION_STRING env variable.
        """
        self.connection_string = connection_string or os.getenv("IOT_HUB_CONNECTION_STRING")
        if not self.connection_string:
            raise ValueError(
                "IoT Hub connection string is required. "
                "Set IOT_HUB_CONNECTION_STRING environment variable or pass it directly."
            )
        self.client: Optional[IoTHubDeviceClient] = None
    
    async def connect(self) -> None:
        """Establish connection to Azure IoT Hub."""
        self.client = IoTHubDeviceClient.create_from_connection_string(self.connection_string)
        await self.client.connect()
        print(f"Connected to IoT Hub: {IOT_HUB_HOSTNAME}")
    
    async def disconnect(self) -> None:
        """Disconnect from Azure IoT Hub."""
        if self.client:
            await self.client.disconnect()
            print("Disconnected from IoT Hub")
    
    async def send_message(
        self,
        payload: dict[str, Any],
        content_type: str = "application/json",
        content_encoding: str = "utf-8",
        custom_properties: Optional[dict[str, str]] = None
    ) -> None:
        """
        Send a message to Azure IoT Hub (Device-to-Cloud).
        
        Args:
            payload: Dictionary containing the message data.
            content_type: MIME type of the message content.
            content_encoding: Encoding of the message content.
            custom_properties: Optional custom properties to attach to the message.
        """
        if not self.client:
            raise RuntimeError("Client not connected. Call connect() first.")
        
        message_body = json.dumps(payload)
        message = Message(message_body)
        message.content_type = content_type
        message.content_encoding = content_encoding
        
        if custom_properties:
            for key, value in custom_properties.items():
                message.custom_properties[key] = value
        
        await self.client.send_message(message)
        print(f"D2C message sent: {message_body[:100]}...")


def send_to_device(device_id: str, payload: dict[str, Any]) -> None:
    """
    Convenience function to send a message to a specific device (C2D).
    
    Args:
        device_id: Target device ID (e.g., "esp32s3").
        payload: Message payload as a dictionary.
    """
    client = IoTHubC2DClient()
    client.send_c2d_message(device_id, payload)


# Example usage and testing
def main():
    """Example demonstrating how to send C2D messages to ESP32."""
    
    # Message to send to the ESP32
    payload = {
        "command": "ping",
        "message": "Hello from Python backend!",
        "timestamp": "2026-01-17T16:30:00Z"
    }
    
    print(f"Sending Cloud-to-Device message to {DEFAULT_DEVICE_ID}...")
    
    try:
        client = IoTHubC2DClient()
        client.send_c2d_message(DEFAULT_DEVICE_ID, payload)
        print("Message sent successfully!")
    except Exception as e:
        print(f"Error: {e}")
        print("\nMake sure IOT_HUB_SERVICE_CONNECTION_STRING is set.")
        print("Get it from: Azure Portal -> IoT Hub -> Shared access policies -> iothubowner")


if __name__ == "__main__":
    main()
