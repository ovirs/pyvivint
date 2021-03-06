"""Module that implements the VivintSkyApi class."""
import json
import logging
import re
import ssl
from datetime import datetime
from types import MethodType
from typing import Any, Dict, Optional

import aiohttp
import certifi
from aiohttp.client_reqrep import ClientResponse

from pyvivint.constants import VivintDeviceAttribute
from pyvivint.enums import ArmedState, GarageDoorState
from pyvivint.exceptions import VivintSkyApiAuthenticationError, VivintSkyApiError

_LOGGER = logging.getLogger(__name__)

VIVINT_API_ENDPOINT = "https://www.vivintsky.com/api"


class VivintSkyApi:
    """Class to communicate with the VivintSky API."""

    def __init__(
        self,
        username: str,
        password: str,
        client_session: Optional[aiohttp.ClientSession] = None,
    ):
        self.__username = username
        self.__password = password
        self.__client_session = client_session or self.__get_new_client_session()
        self.__has_custom_client_session = client_session is not None
        self.__zwave_device_info = {}

    def is_session_valid(self) -> dict:
        """Return the state of the current session."""
        cookie = self.__client_session.cookie_jar._cookies["www.vivintsky.com"].get("s")
        if not cookie:
            return False
        cookie_expiration = datetime.strptime(
            cookie.get("expires"), "%a, %d %b %Y %H:%M:%S %Z"
        )
        return True if cookie_expiration > datetime.utcnow() else False

    async def connect(self) -> dict:
        """Connect to VivintSky Cloud Service."""
        authuser_data = await self.__get_vivintsky_session(
            self.__username, self.__password
        )
        if not authuser_data:
            raise VivintSkyApiAuthenticationError("Unable to login to Vivint.")
        return authuser_data

    async def disconnect(self) -> None:
        """Disconnect from VivintSky Cloud Service."""
        if not self.__has_custom_client_session:
            await self.__client_session.close()

    async def get_authuser_data(self) -> dict:
        """
        Get the authuser data

        Poll the Vivint authuser API endpoint resource to gather user-related data including enumeration of the systems
        that user has access to.
        """
        resp = await self.__get("authuser")
        async with resp:
            if resp.status == 200:
                return await resp.json(encoding="utf-8")
            else:
                raise VivintSkyApiAuthenticationError("Missing auth user data.")

    async def get_panel_credentials(self, panel_id: int) -> dict:
        """Get the panel credentials."""
        resp = await self.__get(f"panel-login/{panel_id}")
        async with resp:
            if resp.status == 200:
                return await resp.json(encoding="utf-8")
            else:
                raise VivintSkyApiAuthenticationError(
                    "Unable to retrieve panel credentials."
                )

    async def get_system_data(self, panel_id: int) -> dict:
        """Gets the raw data for a system."""
        resp = await self.__get(
            f"systems/{panel_id}",
            headers={"Accept-Encoding": "application/json"},
            params={"includerules": "false"},
        )
        async with resp:
            if resp.status == 200:
                return await resp.json(encoding="utf-8")
            else:
                raise VivintSkyApiError("Unable to retrieve system data.")

    async def set_alarm_state(
        self, panel_id: int, partition_id: int, state: bool
    ) -> aiohttp.ClientResponse:
        resp = await self.__put(
            f"{panel_id}/{partition_id}/armedstates",
            headers={"Content-Type": "application/json;charset=UTF-8"},
            data=json.dumps(
                {
                    "system": panel_id,
                    "partitionId": partition_id,
                    "armState": state,
                    "forceArm": False,
                }
            ).encode("utf-8"),
        )
        async with resp:
            if resp.status != 200:
                resp_body = await resp.text()
                _LOGGER.error(
                    f"failed to set state {ArmedState.name(state)}. Code: {resp.status}, body: {resp_body},"
                    f"request url: {resp.request_info}"
                )
                raise VivintSkyApiError(
                    f"failed to set alarm status {ArmedState.name(state)} for panel {self.id}"
                )

    async def set_garage_door_state(
        self, panel_id: int, partition_id: int, device_id: int, state: int
    ) -> None:
        """Open/Close garage door."""
        resp = await self.__put(
            f"{panel_id}/{partition_id}/door/{device_id}",
            headers={
                "Content-Type": "application/json;charset=utf-8",
            },
            data=json.dumps(
                {
                    VivintDeviceAttribute.STATE: state,
                    VivintDeviceAttribute.ID: device_id,
                }
            ).encode("utf-8"),
        )
        async with resp:
            if resp.status != 200:
                _LOGGER.info(
                    f"failed to set state to {GarageDoorState.name(state)} for garage door: {device_id} @ {panel_id}:{partition_id}"
                )
                raise VivintSkyApiError(f"failed to update garage door state")

    async def set_lock_state(
        self, panel_id: int, partition_id: int, device_id: int, locked: bool
    ) -> None:
        """Lock/Unlock door lock."""
        resp = await self.__put(
            f"{panel_id}/{partition_id}/locks/{device_id}",
            headers={
                "Content-Type": "application/json;charset=utf-8",
            },
            data=json.dumps(
                {
                    VivintDeviceAttribute.STATE: locked,
                    VivintDeviceAttribute.ID: device_id,
                }
            ).encode("utf-8"),
        )
        async with resp:
            if resp.status != 200:
                _LOGGER.info(
                    f"failed to set status locked: {locked} for lock: {device_id} @ {panel_id}:{partition_id}"
                )
                raise VivintSkyApiError(f"failed to update lock status")

    async def request_camera_thumbnail(
        self, panel_id: int, partition_id: int, device_id: int
    ) -> None:
        resp = await self.__get(
            f"{panel_id}/{partition_id}/{device_id}/request-camera-thumbnail",
        )
        async with resp:
            if resp.status < 200 or resp.status > 299:
                _LOGGER.info(
                    f"failed to request thumbnail for camera id {self.id}. Error code: {resp.status}"
                )

    async def get_camera_thumbnail_url(
        self,
        panel_id: int,
        partition_id: int,
        device_id: int,
        thumbnail_timestamp: datetime,
    ) -> str:
        resp = await self.__get(
            f"{panel_id}/{partition_id}/{device_id}/camera-thumbnail",
            params={"time": thumbnail_timestamp},
            allow_redirects=False,
        )
        async with resp:
            if resp.status != 302:
                _LOGGER.info(
                    f"failed to request thumbnail for camera id {self.id}. Status code: {resp.status}"
                )
                return

            return resp.headers.get("Location")

    def __get_new_client_session(self) -> aiohttp.ClientSession:
        """Create a new aiohttp.ClientSession object."""
        ssl_context = ssl.create_default_context(
            purpose=ssl.Purpose.SERVER_AUTH, cafile=certifi.where()
        )
        connector = aiohttp.TCPConnector(enable_cleanup_closed=True, ssl=ssl_context)

        return aiohttp.ClientSession(connector=connector)

    async def __get_vivintsky_session(self, username: str, password: str) -> dict:
        """Login into the Vivint Sky platform with the given username and password.

        Returns auth user data if successful.
        """
        resp = await self.__post(
            "login",
            data=json.dumps({"username": username, "password": password}).encode(
                "utf-8"
            ),
        )
        async with resp:
            data = await resp.json(encoding="utf-8")
            if resp.status == 200:
                return data
            elif resp.status == 401:
                raise VivintSkyApiAuthenticationError(data["msg"])
            else:
                resp.raise_for_status()
                return None

    async def __get(
        self,
        path: str,
        headers: Dict[str, Any] = None,
        params: Dict[str, Any] = None,
        allow_redirects: bool = None,
    ) -> ClientResponse:
        """Perform a get request."""
        return await self.__call(
            self.__client_session.get,
            path,
            headers=headers,
            params=params,
            allow_redirects=allow_redirects,
        )

    async def __post(
        self,
        path: str,
        data: bytes = None,
    ) -> ClientResponse:
        """Perform a post request."""
        return await self.__call(self.__client_session.post, path, data=data)

    async def __put(
        self,
        path: str,
        headers: Dict[str, Any] = None,
        data: bytes = None,
    ) -> ClientResponse:
        """Perform a put request."""
        return await self.__call(
            self.__client_session.put, path, headers=headers, data=data
        )

    async def __call(
        self,
        method: MethodType,
        path: str,
        headers: Dict[str, Any] = None,
        params: Dict[str, Any] = None,
        data: bytes = None,
        allow_redirects: bool = None,
    ) -> ClientResponse:
        """Perform a request with supplied parameters and reauthenticate if necessary."""
        if path != "login" and not self.is_session_valid():
            await self.connect()

        return await method(
            f"{VIVINT_API_ENDPOINT}/{path}",
            headers=headers,
            params=params,
            data=data,
            allow_redirects=allow_redirects,
        )

    async def get_zwave_details(self, manufacturer_id, product_id, product_type_id):
        """Gets the zwave details by looking up the details on the openzwave device database."""
        zwave_lookup = f"{manufacturer_id}:{product_id}:{product_type_id}"
        device_info = self.__zwave_device_info.get(zwave_lookup)
        if device_info is not None:
            return device_info

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url=f"http://openzwave.net/device-database/{zwave_lookup}"
            ) as response:
                if response.status == 200:
                    text = await response.text()
                    d = re.search("<title>(.*)</title>", text, re.IGNORECASE)
                    result = self.__zwave_device_info[zwave_lookup] = d[1].split(" - ")
                    return result
                else:
                    response.raise_for_status()
                    return None
