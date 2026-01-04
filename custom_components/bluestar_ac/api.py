"""Bluestar Smart AC API client with MQTT support."""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import ssl
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp

try:
    import paho.mqtt.client as mqtt_client
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False

from .const import (
    DEFAULT_BASE_URL,
    LOGIN_ENDPOINT,
    DEVICES_ENDPOINT,
)

_LOGGER = logging.getLogger(__name__)


class BluestarAPIError(Exception):
    """Exception raised for Bluestar API errors."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class BluestarCredentialExtractor:
    """Extract MQTT credentials from login response."""
    
    def __init__(self):
        self.credentials = None
    
    def extract_credentials(self, login_response: Dict[str, Any]) -> Dict[str, str]:
        """Extract MQTT credentials from login response."""
        try:
            mi = login_response.get("mi")
            if not mi:
                raise BluestarAPIError("No 'mi' field in login response")
            
            decoded = base64.b64decode(mi).decode('utf-8')
            parts = decoded.split('::') if '::' in decoded else decoded.split(':')
            
            if len(parts) < 3:
                raise BluestarAPIError(f"Invalid credential format")
            
            self.credentials = {
                "endpoint": parts[0],
                "access_key": parts[1],
                "secret_key": parts[2],
                "session_id": login_response.get("session"),
                "session_token": parts[3] if len(parts) > 3 else None,
            }
            
            return self.credentials
            
        except Exception as error:
            raise BluestarAPIError(f"Failed to extract credentials: {error}")


class AWSSigV4:
    """AWS SigV4 signing for WebSocket MQTT connection."""
    
    @staticmethod
    def sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()
    
    @staticmethod
    def get_signature_key(key: str, date_stamp: str, region_name: str, service_name: str) -> bytes:
        k_date = AWSSigV4.sign(('AWS4' + key).encode('utf-8'), date_stamp)
        k_region = AWSSigV4.sign(k_date, region_name)
        k_service = AWSSigV4.sign(k_region, service_name)
        return AWSSigV4.sign(k_service, 'aws4_request')
    
    @staticmethod
    def create_websocket_url(endpoint: str, region: str, access_key: str, secret_key: str, session_token: Optional[str] = None) -> str:
        host = endpoint.split('://')[1].split('/')[0] if '://' in endpoint else endpoint.split('/')[0]
        
        now = datetime.now(timezone.utc)
        amz_date = now.strftime('%Y%m%dT%H%M%SZ')
        date_stamp = now.strftime('%Y%m%d')
        
        service = 'iotdevicegateway'
        canonical_uri = '/mqtt'
        canonical_querystring = f'X-Amz-Algorithm=AWS4-HMAC-SHA256'
        canonical_querystring += f'&X-Amz-Credential={urllib.parse.quote_plus(f"{access_key}/{date_stamp}/{region}/{service}/aws4_request")}'
        canonical_querystring += f'&X-Amz-Date={amz_date}'
        canonical_querystring += f'&X-Amz-SignedHeaders=host'
        
        if session_token:
            canonical_querystring += f'&X-Amz-Security-Token={urllib.parse.quote_plus(session_token)}'
        
        canonical_headers = f'host:{host}\n'
        payload_hash = hashlib.sha256(''.encode('utf-8')).hexdigest()
        canonical_request = f'GET\n{canonical_uri}\n{canonical_querystring}\n{canonical_headers}\nhost\n{payload_hash}'
        
        credential_scope = f'{date_stamp}/{region}/{service}/aws4_request'
        string_to_sign = f'AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n{hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()}'
        
        signing_key = AWSSigV4.get_signature_key(secret_key, date_stamp, region, service)
        signature = hmac.new(signing_key, string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
        
        canonical_querystring += f'&X-Amz-Signature={signature}'
        return f'wss://{host}{canonical_uri}?{canonical_querystring}'


class BluestarMQTTClient:
    """MQTT client for Bluestar Smart AC control."""
    
    def __init__(self, credentials: Dict[str, str]):
        if not MQTT_AVAILABLE:
            raise ImportError("paho-mqtt not installed")
            
        self.credentials = credentials
        self.client = None
        self.is_connected = False
        self.client_id = f"u-{credentials['session_id']}"
        self.message_callback = None
        self.subscribed_devices = set()
        self._reconnecting = False
        self._reconnect_lock = asyncio.Lock()
        self._event_loop = None
        self._connection_attempts = 0
        
        endpoint = credentials.get("endpoint", "")
        if ".iot." in endpoint:
            parts = endpoint.split(".iot.")
            if len(parts) > 1:
                region_part = parts[1].split(".amazonaws.com")[0]
                self.region = region_part.split(".")[0] if "." in region_part else region_part
            else:
                self.region = "ap-south-1"
        else:
            self.region = "ap-south-1"
        
        self.PUB_STATE_UPDATE_TOPIC = "$aws/things/%s/shadow/update"
        self.SUB_STATE_REPORTED_TOPIC = "things/%s/state/reported"
        self.PUB_SHADOW_GET_TOPIC = "$aws/things/%s/shadow/get"
        self.SUB_SHADOW_GET_ACCEPTED_TOPIC = "$aws/things/%s/shadow/get/accepted"
    
    async def connect(self) -> bool:
        """Connect to MQTT broker."""
        try:
            self._event_loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        
        try:
            endpoint = self.credentials["endpoint"]
            access_key = self.credentials["access_key"]
            secret_key = self.credentials["secret_key"]
            session_token = self.credentials.get("session_token")
            
            if self.client:
                try:
                    self.client.loop_stop()
                    self.client.disconnect()
                except Exception:
                    pass
                self.client = None
                self.is_connected = False
            
            websocket_url = AWSSigV4.create_websocket_url(
                endpoint=endpoint,
                region=self.region,
                access_key=access_key,
                secret_key=secret_key,
                session_token=session_token
            )
            
            parsed = urllib.parse.urlparse(websocket_url)
            host = parsed.hostname
            port = parsed.port or 443
            
            self.client = mqtt_client.Client(
                client_id=self.client_id,
                transport="websockets",
                callback_api_version=mqtt_client.CallbackAPIVersion.VERSION1
            )
            
            path_with_query = parsed.path + ("?" + parsed.query if parsed.query else "")
            self.client.ws_set_options(path=path_with_query)
            
            loop = asyncio.get_event_loop()
            context = await loop.run_in_executor(None, ssl.create_default_context)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            self.client.tls_set_context(context)
            
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message
            self.client.on_subscribe = self._on_subscribe
            
            await loop.run_in_executor(None, self.client.connect, host, port, 60)
            self.client.loop_start()
            
            elapsed = 0.0
            while not self.is_connected and elapsed < 15:
                await asyncio.sleep(0.25)
                elapsed += 0.25
            
            if self.is_connected:
                self._connection_attempts = 0
                return True
            else:
                self._connection_attempts += 1
                try:
                    self.client.loop_stop()
                    self.client.disconnect()
                except Exception:
                    pass
                return False
                
        except Exception as error:
            _LOGGER.warning(f"MQTT connection failed: {error}")
            self._connection_attempts += 1
            return False
    
    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.is_connected = True
        else:
            self.is_connected = False
            _LOGGER.warning(f"MQTT connection failed with rc={rc}")
    
    def _on_disconnect(self, client, userdata, rc):
        was_connected = self.is_connected
        self.is_connected = False
        if rc != 0 and was_connected and not self._reconnecting:
            self._schedule_reconnect()
    
    def _on_subscribe(self, client, userdata, mid, granted_qos):
        pass  # Subscription confirmed
    
    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            
            if "state/reported" in msg.topic:
                parts = msg.topic.split("/")
                if len(parts) >= 2:
                    device_id = parts[1]
                    if self.message_callback:
                        self.message_callback(device_id, payload)
            
            elif "shadow/get/accepted" in msg.topic:
                parts = msg.topic.split("/")
                if len(parts) >= 3:
                    device_id = parts[2]
                    # Try both "desired" and "reported" state
                    shadow_state = payload.get("state", {}).get("desired", {})
                    if not shadow_state:
                        shadow_state = payload.get("state", {}).get("reported", {})
                    if shadow_state and self.message_callback:
                        self.message_callback(device_id, shadow_state)
        except Exception as error:
            _LOGGER.warning(f"Error processing MQTT message: {error}")
    
    def set_message_callback(self, callback):
        self.message_callback = callback
    
    def _schedule_reconnect(self):
        if self._reconnecting:
            return
        loop = self._event_loop
        if loop is None:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                return
        if loop.is_running():
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self._reconnect_with_backoff()))
    
    async def _reconnect_with_backoff(self) -> bool:
        async with self._reconnect_lock:
            if self._reconnecting or self.is_connected:
                return self.is_connected
            
            self._reconnecting = True
            try:
                backoff = min(30, 2 ** self._connection_attempts)
                if self._connection_attempts > 0:
                    await asyncio.sleep(backoff)
                
                if self.is_connected:
                    return True
                
                success = await self.connect()
                if success:
                    for device_id in list(self.subscribed_devices):
                        await self.subscribe_to_device(device_id)
                return success
            finally:
                self._reconnecting = False
    
    async def ensure_connected(self) -> bool:
        if self.is_connected:
            return True
        return await self._reconnect_with_backoff()
    
    async def subscribe_to_device(self, device_id: str) -> bool:
        if not await self.ensure_connected() or not self.client:
            return False
        
        try:
            topic = self.SUB_STATE_REPORTED_TOPIC % device_id
            self.client.subscribe(topic, qos=0)
            
            shadow_topic = self.SUB_SHADOW_GET_ACCEPTED_TOPIC % device_id
            self.client.subscribe(shadow_topic, qos=0)
            
            self.subscribed_devices.add(device_id)
            return True
        except Exception:
            return False
    
    async def request_device_state(self, device_id: str) -> bool:
        if not await self.ensure_connected() or not self.client:
            return False
        
        try:
            topic = self.PUB_SHADOW_GET_TOPIC % device_id
            result = self.client.publish(topic, "", qos=0)
            return result.rc == mqtt_client.MQTT_ERR_SUCCESS
        except Exception:
            return False
    
    async def publish(self, device_id: str, control_payload: Dict[str, Any]) -> bool:
        if not await self.ensure_connected():
            return False
        
        try:
            formatted_payload = {}
            
            if "mode" in control_payload:
                mode_value = control_payload["mode"]
                if isinstance(mode_value, dict) and "value" in mode_value:
                    formatted_payload["mode"] = mode_value.copy()
                else:
                    mode_obj = {"value": int(mode_value)}
                    if "fspd" in control_payload:
                        mode_obj["fspd"] = int(control_payload["fspd"])
                    if "stemp" in control_payload:
                        stemp = control_payload["stemp"]
                        mode_obj["stemp"] = f"{float(stemp):.1f}" if isinstance(stemp, (int, float)) else str(stemp)
                    formatted_payload["mode"] = mode_obj
            
            for key, value in control_payload.items():
                if key == "mode":
                    continue
                elif key == "stemp" and "mode" not in control_payload:
                    formatted_payload[key] = f"{float(value):.1f}" if isinstance(value, (int, float)) else str(value)
                elif key == "fspd" and "mode" not in control_payload:
                    formatted_payload[key] = int(value)
                elif key in ["pow", "vswing", "hswing", "display"]:
                    formatted_payload[key] = int(value)
                elif key not in ["ts", "src"]:
                    formatted_payload[key] = value
            
            formatted_payload["src"] = "anmq"
            formatted_payload["ts"] = int(time.time() * 1000)
            
            state_object = {"state": {"desired": formatted_payload}}
            topic = self.PUB_STATE_UPDATE_TOPIC % device_id
            
            result = self.client.publish(topic, json.dumps(state_object), qos=0)
            return result.rc == mqtt_client.MQTT_ERR_SUCCESS
                
        except Exception as error:
            _LOGGER.error(f"MQTT Publish Error: {error}")
            return False
    
    def disconnect(self):
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            self.is_connected = False


class BluestarAPI:
    """Bluestar Smart AC API client."""

    def __init__(
        self,
        phone: str,
        password: str,
        base_url: str = DEFAULT_BASE_URL,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.phone = phone
        self.password = password
        self.base_url = base_url
        self._session = session
        self.session_token: Optional[str] = None
        self.credential_extractor = BluestarCredentialExtractor()
        self.mqtt_client: Optional[BluestarMQTTClient] = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self.mqtt_client:
            self.mqtt_client.disconnect()
        if self._session:
            await self._session.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def _get_auth_headers(self) -> Dict[str, str]:
        return {
            "X-APP-VER": "v4.11.4-133",
            "X-OS-NAME": "Android",
            "X-OS-VER": "v13-33",
            "User-Agent": "com.bluestarindia.bluesmart",
            "Content-Type": "application/json",
            "X-APP-SESSION": self.session_token or "",
        }

    async def login(self) -> Dict[str, Any]:
        """Login to Bluestar API."""
        payload = {
            "auth_id": self.phone,
            "auth_type": 1,
            "password": self.password,
        }
        headers = {
            "Content-Type": "application/json",
            "X-APP-VER": "v4.11.4-133",
            "X-OS-NAME": "Android",
            "X-OS-VER": "v13-33",
            "User-Agent": "com.bluestarindia.bluesmart",
        }

        for attempt in range(3):
            try:
                async with self.session.post(
                    f"{self.base_url}{LOGIN_ENDPOINT}",
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        self.session_token = data.get("session")
                        await self._initialize_mqtt_client(data)
                        return data
                    elif response.status == 502 and attempt < 2:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    else:
                        raise BluestarAPIError(f"Login failed: {response.status}", response.status)
            except aiohttp.ClientError as err:
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise BluestarAPIError(f"Network error: {err}")
        
        raise BluestarAPIError("Login failed after retries")

    async def _initialize_mqtt_client(self, login_data: Dict[str, Any]) -> bool:
        if not MQTT_AVAILABLE:
            return False
            
        try:
            credentials = self.credential_extractor.extract_credentials(login_data)
            
            if self.mqtt_client:
                self.mqtt_client.disconnect()
            
            self.mqtt_client = BluestarMQTTClient(credentials)
            return await self.mqtt_client.connect()
        except Exception:
            return False

    async def subscribe_to_devices(self, device_ids: List[str]) -> None:
        if not self.mqtt_client or not await self.mqtt_client.ensure_connected():
            return
        for device_id in device_ids:
            await self.mqtt_client.subscribe_to_device(device_id)
        # Wait for subscriptions to be confirmed by broker
        await asyncio.sleep(1.0)
    
    async def request_device_states(self, device_ids: List[str]) -> None:
        if not self.mqtt_client or not await self.mqtt_client.ensure_connected():
            return
        for device_id in device_ids:
            await self.mqtt_client.request_device_state(device_id)

    async def get_devices(self) -> Dict[str, Any]:
        """Get list of devices."""
        if not self.session_token:
            raise BluestarAPIError("Not authenticated")

        async with self.session.get(
            f"{self.base_url}{DEVICES_ENDPOINT}", 
            headers=self._get_auth_headers(),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status == 401:
                await self.login()
                async with self.session.get(
                    f"{self.base_url}{DEVICES_ENDPOINT}", 
                    headers=self._get_auth_headers(),
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as retry_response:
                    if not retry_response.ok:
                        raise BluestarAPIError(f"Failed to fetch devices: {retry_response.status}")
                    return await retry_response.json()

            if not response.ok:
                raise BluestarAPIError(f"Failed to fetch devices: {response.status}")
            return await response.json()

    async def control_device(self, device_id: str, control_data: Dict[str, Any]) -> Dict[str, Any]:
        """Control device via MQTT."""
        if not self.session_token:
            raise BluestarAPIError("Not authenticated")

        control_payload = {}
        
        if control_data.get("pow") is not None:
            control_payload["pow"] = int(control_data["pow"])
        
        if control_data.get("mode") is not None:
            mode_value = control_data["mode"]
            if isinstance(mode_value, dict) and "value" in mode_value:
                control_payload["mode"] = mode_value
                if "fspd" in mode_value:
                    control_payload["fspd"] = int(mode_value["fspd"])
                if "stemp" in mode_value:
                    control_payload["stemp"] = mode_value["stemp"]
            else:
                control_payload["mode"] = int(mode_value)
        
        if control_data.get("stemp") is not None and "stemp" not in control_payload:
            stemp = control_data["stemp"]
            control_payload["stemp"] = f"{float(stemp):.1f}" if isinstance(stemp, (int, float)) else str(stemp)
        
        if control_data.get("fspd") is not None and "fspd" not in control_payload:
            control_payload["fspd"] = int(control_data["fspd"])
        
        for key in ["vswing", "hswing", "display", "esave", "turbo", "sleep"]:
            if control_data.get(key) is not None:
                control_payload[key] = int(control_data[key])

        control_payload["ts"] = int(time.time() * 1000)
        control_payload["src"] = "anmq"

        if not MQTT_AVAILABLE or not self.mqtt_client:
            raise BluestarAPIError("MQTT not available")
        
        if not await self.mqtt_client.ensure_connected():
            raise BluestarAPIError("MQTT not connected")
        
        if await self.mqtt_client.publish(device_id, control_payload):
            return {"message": "Control command sent", "deviceId": device_id}
        else:
            raise BluestarAPIError("Failed to publish control command")
