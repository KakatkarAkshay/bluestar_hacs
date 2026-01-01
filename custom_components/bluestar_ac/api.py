"""Bluestar Smart AC API client with MQTT support."""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiohttp

# Try to import MQTT, but make it optional
try:
    import paho.mqtt.client as mqtt_client
    import ssl
    import requests
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False
    _LOGGER = logging.getLogger(__name__)
    _LOGGER.warning("paho-mqtt not available, MQTT functionality disabled")

from .const import (
    DEFAULT_BASE_URL,
    LOGIN_ENDPOINT,
    DEVICES_ENDPOINT,
    CONTROL_ENDPOINT,
    PREFERENCES_ENDPOINT,
    STATE_ENDPOINT,
)

_LOGGER = logging.getLogger(__name__)


class BluestarAPIError(Exception):
    """Exception raised for Bluestar API errors."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class BluestarCredentialExtractor:
    """Extract and manage MQTT credentials from login response."""
    
    def __init__(self):
        self.credentials = None
    
    def extract_credentials(self, login_response: Dict[str, Any]) -> Dict[str, str]:
        """Extract MQTT credentials from login response."""
        try:
            mi = login_response.get("mi")
            if not mi:
                raise BluestarAPIError("No 'mi' field in login response")
            
            # Decode Base64 and split by "::" or ":"
            decoded = base64.b64decode(mi).decode('utf-8')
            # Try both delimiters - some responses use '::' and others use ':'
            if '::' in decoded:
                parts = decoded.split('::')
            else:
                parts = decoded.split(':')
            
            if len(parts) < 3:
                raise BluestarAPIError(f"Invalid credential format. Expected at least 3 parts, got {len(parts)}")
            
            endpoint = parts[0]
            access_key = parts[1]
            secret_key = parts[2]
            aws_session_token = parts[3] if len(parts) > 3 else None
            
            self.credentials = {
                "endpoint": endpoint,
                "access_key": access_key,
                "secret_key": secret_key,
                "session_id": login_response.get("session"),
                "session_token": aws_session_token,
                "user_id": login_response.get("user", {}).get("id"),
                "raw": mi
            }
            
            _LOGGER.info("✅ Credentials extracted successfully")
            _LOGGER.info(f"📍 Endpoint: {endpoint}")
            _LOGGER.info(f"🔑 Access Key: {access_key[:8]}...")
            _LOGGER.info(f"🔐 Secret Key: {secret_key[:8]}...")
            _LOGGER.info(f"🔑 AWS Session Token: {'present' if aws_session_token else 'none'}")
            
            return self.credentials
            
        except Exception as error:
            _LOGGER.error(f"❌ Failed to extract credentials: {error}")
            raise BluestarAPIError(f"Failed to extract credentials: {error}")
    
    def get_credentials(self) -> Optional[Dict[str, str]]:
        """Get current credentials."""
        return self.credentials
    
    def is_valid(self) -> bool:
        """Check if credentials are valid."""
        return (self.credentials and 
                self.credentials.get("endpoint") and 
                self.credentials.get("access_key") and 
                self.credentials.get("secret_key") and 
                self.credentials.get("session_id"))


class AWSSigV4:
    """AWS SigV4 signing for WebSocket MQTT connection."""
    
    @staticmethod
    def sign(key: bytes, msg: str) -> bytes:
        """Create HMAC-SHA256 signature."""
        return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()
    
    @staticmethod
    def get_signature_key(key: str, date_stamp: str, region_name: str, service_name: str) -> bytes:
        """Generate signing key for AWS SigV4."""
        k_date = AWSSigV4.sign(('AWS4' + key).encode('utf-8'), date_stamp)
        k_region = AWSSigV4.sign(k_date, region_name)
        k_service = AWSSigV4.sign(k_region, service_name)
        k_signing = AWSSigV4.sign(k_service, 'aws4_request')
        return k_signing
    
    @staticmethod
    def create_websocket_url(endpoint: str, region: str, access_key: str, secret_key: str, session_token: Optional[str] = None) -> str:
        """Create AWS IoT WebSocket URL with SigV4 authentication."""
        # Parse endpoint to get host
        if '://' in endpoint:
            host = endpoint.split('://')[1].split('/')[0]
        else:
            host = endpoint.split('/')[0]
        
        # Get current time
        now = datetime.utcnow()
        amz_date = now.strftime('%Y%m%dT%H%M%SZ')
        date_stamp = now.strftime('%Y%m%d')
        
        # Service and region
        service = 'iotdevicegateway'
        
        # Create canonical request
        canonical_uri = '/mqtt'
        canonical_querystring = f'X-Amz-Algorithm=AWS4-HMAC-SHA256'
        canonical_querystring += f'&X-Amz-Credential={urllib.parse.quote_plus(f"{access_key}/{date_stamp}/{region}/{service}/aws4_request")}'
        canonical_querystring += f'&X-Amz-Date={amz_date}'
        canonical_querystring += f'&X-Amz-SignedHeaders=host'
        
        if session_token:
            canonical_querystring += f'&X-Amz-Security-Token={urllib.parse.quote_plus(session_token)}'
        
        canonical_headers = f'host:{host}\n'
        signed_headers = 'host'
        payload_hash = hashlib.sha256(''.encode('utf-8')).hexdigest()
        
        canonical_request = f'GET\n{canonical_uri}\n{canonical_querystring}\n{canonical_headers}\n{signed_headers}\n{payload_hash}'
        
        # Create string to sign
        algorithm = 'AWS4-HMAC-SHA256'
        credential_scope = f'{date_stamp}/{region}/{service}/aws4_request'
        string_to_sign = f'{algorithm}\n{amz_date}\n{credential_scope}\n{hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()}'
        
        # Calculate signature
        signing_key = AWSSigV4.get_signature_key(secret_key, date_stamp, region, service)
        signature = hmac.new(signing_key, string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
        
        # Add signature to query string
        canonical_querystring += f'&X-Amz-Signature={signature}'
        
        # Create WebSocket URL
        url = f'wss://{host}{canonical_uri}?{canonical_querystring}'
        return url


class BluestarMQTTClient:
    """MQTT client for Bluestar Smart AC control."""
    
    def __init__(self, credentials: Dict[str, str]):
        if not MQTT_AVAILABLE:
            raise ImportError("MQTT functionality not available - paho-mqtt not installed")
            
        self.credentials = credentials
        self.client = None
        self.is_connected = False
        self.client_id = f"u-{credentials['session_id']}"
        self.message_callback = None  # Callback for handling MQTT messages
        self.subscribed_devices = set()  # Track subscribed device IDs
        self._reconnecting = False  # Flag to prevent multiple simultaneous reconnection attempts
        self._event_loop = None  # Store event loop for reconnection from callback thread
        
        # Extract region from endpoint (e.g., a26381dl7mudo4-ats.iot.ap-south-1.amazonaws.com -> ap-south-1)
        endpoint = credentials.get("endpoint", "")
        if ".iot." in endpoint:
            # Extract region from endpoint
            parts = endpoint.split(".iot.")
            if len(parts) > 1:
                region_part = parts[1].split(".amazonaws.com")[0]
                # Extract region (e.g., ap-south-1 from ap-south-1.amazonaws.com)
                if "." in region_part:
                    self.region = region_part.split(".")[0]
                else:
                    self.region = region_part
            else:
                self.region = "ap-south-1"
        else:
            self.region = "ap-south-1"
        
        self.PUB_CONTROL_TOPIC_NAME = "things/%s/control"
        self.PUB_STATE_UPDATE_TOPIC_NAME = "$aws/things/%s/shadow/update"
        self.SUB_STATE_REPORTED_TOPIC_NAME = "things/%s/state/reported"
        self.SRC_KEY = "src"
        self.SRC_VALUE = "anmq"
        
        _LOGGER.info("🔧 Bluestar MQTT Client created")
        _LOGGER.info(f"📍 Endpoint: {endpoint}")
        _LOGGER.info(f"🌍 Region: {self.region}")
    
    async def connect(self) -> bool:
        """Connect to MQTT broker using AWS IoT WebSocket with SigV4."""
        # Store event loop for reconnection from callback thread
        try:
            self._event_loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop, will try to get it later
            pass
        
        try:
            endpoint = self.credentials["endpoint"]
            access_key = self.credentials["access_key"]
            secret_key = self.credentials["secret_key"]
            session_token = self.credentials.get("session_token")  # May not be present
            
            # Create WebSocket URL with SigV4 authentication
            websocket_url = AWSSigV4.create_websocket_url(
                endpoint=endpoint,
                region=self.region,
                access_key=access_key,
                secret_key=secret_key,
                session_token=session_token
            )
            
            _LOGGER.info(f"🔌 Connecting to MQTT broker via WebSocket: {endpoint}")
            _LOGGER.debug(f"WebSocket URL: {websocket_url[:100]}...")  # Don't log full URL with credentials
            
            # Parse WebSocket URL to get host and port
            parsed = urllib.parse.urlparse(websocket_url)
            host = parsed.hostname
            port = parsed.port or 443
            
            # Create MQTT client with WebSocket transport
            self.client = mqtt_client.Client(
                client_id=self.client_id,
                transport="websockets",
                callback_api_version=mqtt_client.CallbackAPIVersion.VERSION1
            )
            
            # Configure WebSocket options with the full URL path and query string
            # paho-mqtt expects the path and query to be set separately
            path_with_query = parsed.path
            if parsed.query:
                path_with_query += "?" + parsed.query
            
            self.client.ws_set_options(
                path=path_with_query,
                headers=None
            )
            
            # Configure SSL/TLS - run in executor to avoid blocking event loop
            loop = asyncio.get_event_loop()
            context = await loop.run_in_executor(None, ssl.create_default_context)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            self.client.tls_set_context(context)
            
            # Set up event handlers
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_error = self._on_error
            self.client.on_message = self._on_message
            
            # Connect to broker - run in executor to avoid blocking
            _LOGGER.info(f"🔌 Connecting to {host}:{port} with client_id: {self.client_id}")
            _LOGGER.debug(f"WebSocket path: {path_with_query[:200]}...")  # Log partial path
            
            # Run connect in executor since it's a blocking call
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.client.connect, host, port, 60)
            self.client.loop_start()
            
            # Wait for connection
            timeout = 10  # Reduced timeout
            while not self.is_connected and timeout > 0:
                await asyncio.sleep(0.5)
                timeout -= 0.5
            
            if self.is_connected:
                _LOGGER.info("✅ MQTT Connected successfully via WebSocket")
                return True
            else:
                _LOGGER.warning("⚠️ MQTT connection timeout")
                # Clean up failed connection
                try:
                    self.client.loop_stop()
                    self.client.disconnect()
                except:
                    pass
                return False
                
        except Exception as error:
            _LOGGER.warning(f"⚠️ MQTT connection failed: {error}")
            # Don't log full traceback for MQTT failures - it's expected to fail sometimes
            try:
                if self.client:
                    self.client.loop_stop()
                    self.client.disconnect()
            except:
                pass
            return False
    
    def _on_connect(self, client, userdata, flags, rc):
        """Handle MQTT connection."""
        if rc == 0:
            self.is_connected = True
            _LOGGER.info("🔗 MQTT Connected successfully")
        else:
            _LOGGER.error(f"❌ MQTT Connection failed with code {rc}")
            self.is_connected = False
    
    def _on_disconnect(self, client, userdata, rc):
        """Handle MQTT disconnection."""
        self.is_connected = False
        _LOGGER.info("📴 MQTT Disconnected")
        # Trigger immediate reconnection if not already reconnecting
        if not self._reconnecting and rc != 0:
            # rc != 0 means unexpected disconnect, rc == 0 means intentional
            _LOGGER.info("🔄 MQTT unexpected disconnect, attempting immediate reconnection...")
            self._schedule_reconnect()
    
    def _on_error(self, client, userdata, error):
        """Handle MQTT errors."""
        _LOGGER.error(f"❌ MQTT Error: {error}")
        self.is_connected = False
        # Trigger immediate reconnection
        if not self._reconnecting:
            _LOGGER.info("🔄 MQTT error occurred, attempting immediate reconnection...")
            self._schedule_reconnect()
    
    def _on_message(self, client, userdata, msg):
        """Handle MQTT messages."""
        try:
            import json
            payload = json.loads(msg.payload.decode())
            
            # Check if this is a device state report
            if "state/reported" in msg.topic:
                # Extract device ID from topic: things/{device_id}/state/reported
                parts = msg.topic.split("/")
                if len(parts) >= 2:
                    device_id = parts[1]
                    
                    # Call the message callback if set
                    if self.message_callback:
                        self.message_callback(device_id, payload)
                    else:
                        _LOGGER.debug(f"📥 MQTT state report for {device_id}: {json.dumps(payload, indent=2)[:200]}")
        except Exception as error:
            _LOGGER.warning(f"⚠️ Error processing MQTT message: {error}")
    
    def set_message_callback(self, callback):
        """Set callback for handling MQTT messages."""
        self.message_callback = callback
    
    def _schedule_reconnect(self):
        """Schedule reconnection from MQTT callback thread."""
        if self._reconnecting:
            return
        
        # Try to get event loop
        loop = self._event_loop
        if loop is None:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                _LOGGER.warning("⚠️ No event loop available for immediate reconnection, will retry on next operation")
                return
        
        # Schedule reconnection task from callback thread
        if loop.is_running():
            loop.call_soon_threadsafe(self._start_reconnect_task, loop)
        else:
            # If loop is not running, try to run the reconnection
            try:
                loop.run_until_complete(self._reconnect())
            except Exception as e:
                _LOGGER.warning(f"⚠️ Could not reconnect immediately: {e}, will retry on next operation")
    
    def _start_reconnect_task(self, loop):
        """Start reconnection task from event loop thread."""
        if not self._reconnecting:
            asyncio.create_task(self._reconnect())
    
    async def _reconnect(self) -> bool:
        """Attempt to reconnect to MQTT broker."""
        if self._reconnecting:
            _LOGGER.debug("Reconnection already in progress, skipping")
            return False
        
        self._reconnecting = True
        try:
            _LOGGER.info("🔄 Attempting to reconnect to MQTT broker...")
            
            # Clean up existing connection
            if self.client:
                try:
                    self.client.loop_stop()
                    self.client.disconnect()
                except:
                    pass
            
            # Attempt to reconnect
            success = await self.connect()
            
            if success:
                # Resubscribe to all devices
                for device_id in list(self.subscribed_devices):
                    await self.subscribe_to_device(device_id)
                _LOGGER.info("✅ MQTT reconnected and resubscribed to devices")
            else:
                _LOGGER.warning("⚠️ MQTT reconnection failed")
            
            return success
        finally:
            self._reconnecting = False
    
    async def ensure_connected(self) -> bool:
        """Ensure MQTT is connected, reconnect if necessary."""
        if self.is_connected:
            return True
        
        if not self._reconnecting:
            return await self._reconnect()
        
        # Wait for reconnection to complete
        timeout = 10
        while self._reconnecting and timeout > 0:
            await asyncio.sleep(0.5)
            timeout -= 0.5
        
        return self.is_connected
    
    async def subscribe_to_device(self, device_id: str) -> bool:
        """Subscribe to device state reports."""
        # Ensure we're connected before subscribing
        if not await self.ensure_connected():
            _LOGGER.warning(f"⚠️ Cannot subscribe to {device_id}: MQTT not connected")
            return False
        
        if not self.client:
            _LOGGER.warning(f"⚠️ Cannot subscribe to {device_id}: MQTT client not initialized")
            return False
        
        try:
            topic = self.SUB_STATE_REPORTED_TOPIC_NAME % device_id
            result = self.client.subscribe(topic, qos=0)
            if result[0] == mqtt_client.MQTT_ERR_SUCCESS:
                self.subscribed_devices.add(device_id)
                _LOGGER.info(f"✅ Subscribed to MQTT topic: {topic}")
                return True
            else:
                _LOGGER.warning(f"⚠️ Failed to subscribe to {topic}: {result[0]}")
                return False
        except Exception as error:
            _LOGGER.warning(f"⚠️ Error subscribing to {device_id}: {error}")
            return False
    
    async def publish(self, device_id: str, control_payload: Dict[str, Any]) -> bool:
        """Publish control command via MQTT."""
        # Ensure we're connected before publishing
        if not await self.ensure_connected():
            _LOGGER.error("❌ MQTT not connected and reconnection failed")
            return False
        
        try:
            formatted_payload = {}
            
            # Handle mode as object with value, fspd, and optionally stemp
            # Based on actual MQTT protocol: {"mode": {"value": 0, "fspd": 2}}
            if "mode" in control_payload:
                mode_value = control_payload["mode"]
                mode_obj = {}
                
                # If mode is already an object (from control_device), use it
                if isinstance(mode_value, dict) and "value" in mode_value:
                    mode_obj = mode_value.copy()
                else:
                    # Convert simple mode integer to object format
                    mode_obj["value"] = int(mode_value)
                    
                    # Include fspd if provided in control_payload
                    if "fspd" in control_payload:
                        mode_obj["fspd"] = int(control_payload["fspd"])
                    
                    # Include stemp if provided in control_payload (for modes that need temperature)
                    if "stemp" in control_payload:
                        stemp_value = control_payload["stemp"]
                        if isinstance(stemp_value, (int, float)):
                            mode_obj["stemp"] = f"{float(stemp_value):.1f}"
                        else:
                            mode_obj["stemp"] = str(stemp_value)
                
                formatted_payload["mode"] = mode_obj
            else:
                # No mode change, but include fspd if provided
                if "fspd" in control_payload:
                    formatted_payload["fspd"] = int(control_payload["fspd"])
            
            # Handle other fields
            for key, value in control_payload.items():
                if key == "mode":
                    # Already handled above
                    continue
                elif key == "stemp":
                    # Only include stemp if mode is not being set (mode object will include it)
                    if "mode" not in control_payload:
                        if isinstance(value, (int, float)):
                            formatted_payload[key] = f"{float(value):.1f}"
                        else:
                            formatted_payload[key] = str(value)
                elif key == "fspd":
                    # Only include fspd if mode is not being set (mode object will include it)
                    if "mode" not in control_payload:
                        formatted_payload[key] = int(value)
                elif key in ["pow", "vswing", "hswing", "display"]:
                    formatted_payload[key] = int(value)
                elif key not in ["ts", "src"]:  # Skip ts and src, we'll add them separately
                    formatted_payload[key] = value
            
            formatted_payload[self.SRC_KEY] = self.SRC_VALUE
            
            if "ts" not in formatted_payload:
                formatted_payload["ts"] = int(time.time() * 1000)
            
            state_object = {
                "state": {
                    "desired": formatted_payload.copy()
                }
            }
            
            topic = self.PUB_STATE_UPDATE_TOPIC_NAME % device_id
            
            _LOGGER.warning(f"📤 MQTT Publish: {json.dumps(state_object, indent=2)}")
            _LOGGER.warning(f"📤 Topic: {topic}")
            
            result = self.client.publish(topic, json.dumps(state_object), qos=0)
            
            if result.rc == mqtt_client.MQTT_ERR_SUCCESS:
                _LOGGER.warning("✅ Successfully published via MQTT")
                return True
            else:
                _LOGGER.error(f"❌ Failed to publish via MQTT: {result.rc}")
                return False
                
        except Exception as error:
            _LOGGER.error(f"❌ MQTT Publish Error: {error}")
            return False
    
    def disconnect(self):
        """Disconnect from MQTT broker."""
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            self.is_connected = False
            _LOGGER.info("🔌 MQTT Disconnected")


class BluestarAPI:
    """Bluestar Smart AC API client with MQTT support."""

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
        """Get or create aiohttp session."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        """Close the aiohttp session."""
        if self.mqtt_client:
            self.mqtt_client.disconnect()
        if self._session:
            await self._session.close()

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()

    def _get_auth_headers(self, session_token: Optional[str] = None) -> Dict[str, str]:
        """Get authentication headers (EXACTLY matching the Android app)."""
        token = session_token or self.session_token
        return {
            "X-APP-VER": "v4.11.4-133",
            "X-OS-NAME": "Android",
            "X-OS-VER": "v13-33",
            "User-Agent": "com.bluestarindia.bluesmart",
            "Content-Type": "application/json",
            "X-APP-SESSION": token or "",
        }

    async def login(self) -> Dict[str, Any]:
        """Login to Bluestar API with retry logic and multiple phone formats."""
        _LOGGER.info(f"🔐 Attempting login for phone: {self.phone}")
        
        # Use exact phone format that works with API
        phone_formats = [self.phone]
        
        for phone_format in phone_formats:
            _LOGGER.info(f"📱 Trying phone format: {phone_format}")
            
            payload = {
                "auth_id": phone_format,
                "auth_type": 1,
                "password": self.password,
            }

            max_retries = 3
            for attempt in range(max_retries):
                try:
                    async with self.session.post(
                        f"{self.base_url}{LOGIN_ENDPOINT}",
                        headers={
                            "Content-Type": "application/json",
                            "X-APP-VER": "v4.11.4-133",
                            "X-OS-NAME": "Android",
                            "X-OS-VER": "v13-33",
                            "User-Agent": "com.bluestarindia.bluesmart",
                        },
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as response:
                        response_text = await response.text()
                        _LOGGER.info(f"API Response (attempt {attempt + 1}): {response.status} - {response_text}")
                        
                        if response.status == 200:
                            try:
                                data = await response.json()
                                self.session_token = data.get("session")
                                
                                # Initialize MQTT client with credentials
                                await self._initialize_mqtt_client(data)
                                
                                _LOGGER.info("✅ Login successful")
                                return data
                                
                            except json.JSONDecodeError:
                                _LOGGER.error(f"Invalid JSON response: {response_text}")
                                raise BluestarAPIError("Invalid JSON response from server")
                        
                        elif response.status == 403:
                            _LOGGER.error("Access forbidden (403) - Check if account is locked or credentials are correct")
                            raise BluestarAPIError("Access forbidden - Account may be locked", response.status)
                        
                        elif response.status == 401:
                            _LOGGER.error("Unauthorized (401) - Invalid credentials")
                            raise BluestarAPIError("Invalid credentials", response.status)
                        
                        elif response.status == 502:
                            _LOGGER.warning(f"502 Internal Server Error (attempt {attempt + 1}/{max_retries})")
                            if attempt < max_retries - 1:
                                await asyncio.sleep(2 ** attempt)  # Exponential backoff
                                continue
                            else:
                                raise BluestarAPIError("API temporarily unavailable (502 error). Please try again in a few minutes.", response.status)
                        
                        else:
                            _LOGGER.error(f"Unexpected response: {response.status} - {response_text}")
                            raise BluestarAPIError(f"Unexpected response: {response.status}", response.status)
                
                except aiohttp.ClientError as err:
                    _LOGGER.error(f"Network error with phone {phone_format} (attempt {attempt + 1}): {err}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 ** attempt)  # Exponential backoff
                        continue
                    else:
                        break
        
        raise BluestarAPIError("Login failed with all phone number formats")

    async def _initialize_mqtt_client(self, login_data: Dict[str, Any]) -> bool:
        """Initialize MQTT client with credentials from login response."""
        if not MQTT_AVAILABLE:
            _LOGGER.error("❌ MQTT not available - device control will not work")
            return False
            
        try:
            # Extract credentials from login response
            credentials = self.credential_extractor.extract_credentials(login_data)
            
            # Disconnect existing client if any
            if self.mqtt_client:
                self.mqtt_client.disconnect()
            
            # Create new MQTT client
            self.mqtt_client = BluestarMQTTClient(credentials)
            
            # Try to connect to MQTT (non-blocking, don't fail if it doesn't work)
            try:
                success = await self.mqtt_client.connect()
                if success:
                    _LOGGER.info("✅ MQTT client initialized and connected")
                    # Subscribe to all known devices after connection
                    # This will be called after devices are fetched
                    return True
                else:
                    _LOGGER.error("❌ MQTT client failed to connect - device control will not work")
                    return False
            except Exception as connect_error:
                _LOGGER.error(f"❌ MQTT connection error: {connect_error}")
                return False
                
        except Exception as error:
            _LOGGER.error(f"❌ MQTT initialization failed: {error}")
            return False

    async def subscribe_to_devices(self, device_ids: List[str]) -> None:
        """Subscribe to MQTT state reports for given devices."""
        if not self.mqtt_client:
            return
        
        # Ensure connected before subscribing
        if not await self.mqtt_client.ensure_connected():
            _LOGGER.warning("⚠️ Cannot subscribe to devices: MQTT not connected")
            return
        
        for device_id in device_ids:
            await self.mqtt_client.subscribe_to_device(device_id)

    async def get_devices(self) -> Dict[str, Any]:
        """Get list of devices."""
        if not self.session_token:
            raise BluestarAPIError("Not authenticated. Call login() first.")

        headers = self._get_auth_headers()
        _LOGGER.info(f"Fetching devices with headers: {headers}")

        async with self.session.get(
            f"{self.base_url}{DEVICES_ENDPOINT}", 
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status == 401:
                _LOGGER.warning("Session expired, attempting re-login")
                await self.login()
                headers = self._get_auth_headers()
                async with self.session.get(
                    f"{self.base_url}{DEVICES_ENDPOINT}", 
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as retry_response:
                    if not retry_response.ok:
                        raise BluestarAPIError(
                            f"Failed to fetch devices: {retry_response.status}"
                        )
                    return await retry_response.json()

            if not response.ok:
                raise BluestarAPIError(f"Failed to fetch devices: {response.status}")

            data = await response.json()
            _LOGGER.info(f"Raw Bluestar response: {data}")
            return data

    async def control_device(self, device_id: str, control_data: Dict[str, Any]) -> Dict[str, Any]:
        """Control device using EXACT BLUESTAR CONTROL ALGORITHM."""
        _LOGGER.warning("=" * 80)
        _LOGGER.warning(f"🎛️ API.control_device CALLED - device_id: {device_id}")
        _LOGGER.warning(f"🎛️ Control data: {json.dumps(control_data, indent=2)}")
        _LOGGER.warning(f"🎛️ Session token present: {self.session_token is not None}")
        
        if not self.session_token:
            _LOGGER.error("❌ Not authenticated. Call login() first.")
            raise BluestarAPIError("Not authenticated. Call login() first.")

        headers = self._get_auth_headers()
        control_payload = {}
        
        if control_data.get("pow") is not None:
            control_payload["pow"] = int(control_data["pow"])
        
        # Handle mode - preserve object format if it's already an object, otherwise convert to int
        if control_data.get("mode") is not None:
            mode_value = control_data["mode"]
            if isinstance(mode_value, dict) and "value" in mode_value:
                # Mode is already in object format (from climate.py), preserve it
                control_payload["mode"] = mode_value
                # Also extract fspd and stemp from mode object if present
                if "fspd" in mode_value:
                    control_payload["fspd"] = int(mode_value["fspd"])
                if "stemp" in mode_value:
                    control_payload["stemp"] = mode_value["stemp"]
            else:
                # Mode is a simple integer, convert it
                control_payload["mode"] = int(mode_value)
        
        # Only include stemp if not already in mode object
        if control_data.get("stemp") is not None and "stemp" not in control_payload:
            stemp_value = control_data["stemp"]
            if isinstance(stemp_value, (int, float)):
                control_payload["stemp"] = f"{float(stemp_value):.1f}"
            else:
                control_payload["stemp"] = str(stemp_value)
        
        # Only include fspd if not already in mode object
        if control_data.get("fspd") is not None and "fspd" not in control_payload:
            control_payload["fspd"] = int(control_data["fspd"])
        
        if control_data.get("vswing") is not None:
            control_payload["vswing"] = int(control_data["vswing"])
        if control_data.get("hswing") is not None:
            control_payload["hswing"] = int(control_data["hswing"])
        if control_data.get("display") is not None:
            control_payload["display"] = int(control_data["display"])
        if control_data.get("esave") is not None:
            control_payload["esave"] = int(control_data["esave"])
        if control_data.get("turbo") is not None:
            control_payload["turbo"] = int(control_data["turbo"])
        if control_data.get("sleep") is not None:
            control_payload["sleep"] = int(control_data["sleep"])

        control_payload["ts"] = int(time.time() * 1000)
        control_payload["src"] = "anmq"

        # Only use MQTT for control - no HTTP fallback
        if not MQTT_AVAILABLE:
            raise BluestarAPIError("MQTT not available - cannot control device")
        
        if not self.mqtt_client:
            raise BluestarAPIError("MQTT client not initialized - cannot control device")
        
        # Ensure MQTT is connected, attempt reconnection if needed
        if not await self.mqtt_client.ensure_connected():
            raise BluestarAPIError("MQTT not connected and reconnection failed - cannot control device")
        
        control_result = None
        try:
            _LOGGER.warning(f"📤 Sending MQTT shadow update: {json.dumps(control_payload, indent=2)}")
            success = await self.mqtt_client.publish(device_id, control_payload)
            
            if success:
                control_result = {"method": "MQTT", "status": "success"}
                _LOGGER.warning("✅ MQTT shadow update published successfully")
            else:
                _LOGGER.error("❌ MQTT shadow update failed")
                raise BluestarAPIError("Failed to publish MQTT control command")
        except Exception as error:
            _LOGGER.error(f"❌ MQTT control failed: {error}", exc_info=True)
            raise BluestarAPIError(f"MQTT control failed: {error}") from error

        # Get updated device state (wait a bit to allow device to process MQTT command)
        if control_result and control_result.get("method") == "MQTT":
            await asyncio.sleep(1)  # Give device time to process MQTT command
        
        state = {}
        try:
            updated_device_response = await self.session.get(f"{self.base_url}{DEVICES_ENDPOINT}", headers=headers)
            if updated_device_response.ok:
                updated_device_data = await updated_device_response.json()
                updated_state = updated_device_data.get("states", {}).get(device_id, {})
                
                state = {
                    "power": updated_state.get("state", {}).get("pow") == 1,
                    "mode": updated_state.get("state", {}).get("mode", 2),
                    "temperature": updated_state.get("state", {}).get("stemp", "24"),
                    "currentTemp": updated_state.get("state", {}).get("ctemp", "27.5"),
                    "fanSpeed": updated_state.get("state", {}).get("fspd", 2),
                    "swing": updated_state.get("state", {}).get("vswing") != 0,
                    "display": updated_state.get("state", {}).get("display") != 0,
                    "connected": updated_state.get("connected", False),
                    "timestamp": int(time.time() * 1000),
                    "rssi": updated_state.get("state", {}).get("rssi", -45),
                    "error": updated_state.get("state", {}).get("err", 0),
                    "source": updated_state.get("state", {}).get("src", "unknown")
                }
            else:
                _LOGGER.warning(f"Failed to get updated device state: {updated_device_response.status}")
        except Exception as error:
            _LOGGER.warning(f"Error getting updated device state: {error}")

        # Return result
        message = "Control command sent successfully"
        method = control_result.get("method", "MQTT") if control_result else "MQTT"
        
        _LOGGER.info(f"📤 Final control result: {json.dumps(control_result, indent=2)}")
        
        return {
            "message": message,
            "deviceId": device_id,
            "controlData": control_data,
            "state": state,
            "method": method,
            "api": control_result
        }