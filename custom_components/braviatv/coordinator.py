"""Update coordinator for Bravia TV integration with full user label support."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine, Iterable
from datetime import datetime, timedelta
from functools import wraps
import logging
from typing import Any, Concatenate, Final

from pybravia import (
    BraviaAuthError,
    BraviaClient,
    BraviaConnectionError,
    BraviaConnectionTimeout,
    BraviaError,
    BraviaNotFound,
    BraviaTurnedOff,
)

from homeassistant.components.media_player import MediaType
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_CLIENT_ID, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_ENABLE_USER_LABELS,
    CONF_NICKNAME,
    CONF_USE_PSK,
    DOMAIN,
    LEGACY_CLIENT_ID,
    NICKNAME_PREFIX,
    SourceType,
)

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL: Final = timedelta(seconds=10)

type BraviaTVConfigEntry = ConfigEntry[BraviaTVCoordinator]


def catch_braviatv_errors[_BraviaTVCoordinatorT: BraviaTVCoordinator, **_P](
    func: Callable[Concatenate[_BraviaTVCoordinatorT, _P], Awaitable[None]],
) -> Callable[Concatenate[_BraviaTVCoordinatorT, _P], Coroutine[Any, Any, None]]:
    @wraps(func)
    async def wrapper(
        self: _BraviaTVCoordinatorT,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> None:
        try:
            await func(self, *args, **kwargs)
        except BraviaError as err:
            _LOGGER.error("Command error: %s", err)
        await self.async_request_refresh()
    return wrapper


class BraviaTVCoordinator(DataUpdateCoordinator[None]):
    """Representation of a Bravia TV Coordinator with optional user labels."""

    config_entry: BraviaTVConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: BraviaTVConfigEntry,
        client: BraviaClient,
    ) -> None:
        self.client = client
        self.pin = config_entry.data[CONF_PIN]
        self.use_psk = config_entry.data.get(CONF_USE_PSK, False)
        self.client_id = config_entry.data.get(CONF_CLIENT_ID, LEGACY_CLIENT_ID)
        self.nickname = config_entry.data.get(CONF_NICKNAME, NICKNAME_PREFIX)
        self.enable_user_labels = config_entry.options.get(CONF_ENABLE_USER_LABELS, True)

        self.system_info: dict[str, str] = {}
        self.source: str | None = None
        self.source_list: list[str] = []
        self.source_map: dict[str, dict] = {}
        self.media_title: str | None = None
        self.media_channel: str | None = None
        self.media_content_id: str | None = None
        self.media_content_type: MediaType | None = None
        self.media_uri: str | None = None
        self.media_duration: int | None = None
        self.media_position: int | None = None
        self.media_position_updated_at: datetime | None = None
        self.volume_level: float | None = None
        self.volume_target: str | None = None
        self.volume_muted = False
        self.is_on = False
        self.connected = False
        self.skipped_updates = 0

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
            request_refresh_debouncer=Debouncer(
                hass, _LOGGER, cooldown=1.0, immediate=False
            ),
        )

    def _sources_extend(
        self,
        sources: list[dict],
        source_type: SourceType,
        add_to_list: bool = False,
        sort_by: str | None = None,
    ) -> None:
        if sort_by:
            sources = sorted(sources, key=lambda d: d.get(sort_by, ""))
        for item in sources:
            title = item.get("title")
            if self.enable_user_labels and (label := item.get("label")):
                title = label
            uri = item.get("uri")
            if not title or not uri:
                continue
            self.source_map[uri] = {**item, "type": source_type}
            if add_to_list and title not in self.source_list:
                self.source_list.append(title)

    async def _async_update_data(self) -> None:
        try:
            if not self.connected:
                try:
                    if self.use_psk:
                        await self.client.connect(psk=self.pin)
                    else:
                        await self.client.connect(
                            pin=self.pin,
                            clientid=self.client_id,
                            nickname=self.nickname,
                        )
                    self.connected = True
                except BraviaAuthError as err:
                    raise ConfigEntryAuthFailed from err

            power_status = await self.client.get_power_status()
            self.is_on = power_status == "active"
            self.skipped_updates = 0

            if not self.system_info:
                self.system_info = await self.client.get_system_info()

            if not self.is_on:
                return

            if not self.source_map:
                await self.async_update_sources()
            await self.async_update_volume()
            await self.async_update_playing()

        except BraviaNotFound as err:
            if self.skipped_updates < 10:
                self.connected = False
                self.skipped_updates += 1
                _LOGGER.debug("Update skipped, Bravia API service is reloading")
                return
            raise UpdateFailed("Error communicating with device") from err
        except (BraviaConnectionError, BraviaConnectionTimeout, BraviaTurnedOff):
            self.is_on = False
            self.connected = False
            _LOGGER.debug("Update skipped, Bravia TV is off")
        except BraviaError as err:
            self.is_on = False
            self.connected = False
            raise UpdateFailed(f"Error communicating with device: {err}") from err

    async def async_update_volume(self) -> None:
        volume_info = await self.client.get_volume_info()
        if (volume_level := volume_info.get("volume")) is not None:
            self.volume_level = volume_level / 100
            self.volume_muted = volume_info.get("mute", False)
            self.volume_target = volume_info.get("target")

    async def async_update_playing(self) -> None:
        playing_info = await self.client.get_playing_info()
        self.media_title = playing_info.get("title")
        self.media_uri = playing_info.get("uri")
        self.media_duration = playing_info.get("durationSec")
        self.media_channel = None
        self.media_content_id = None
        self.media_content_type = None
        self.source = None

        if start_datetime := playing_info.get("startDateTime"):
            start_datetime = datetime.fromisoformat(start_datetime)
            current_datetime = datetime.now().replace(tzinfo=start_datetime.tzinfo)
            self.media_position = int((current_datetime - start_datetime).total_seconds())
            self.media_position_updated_at = datetime.now()
        else:
            self.media_position = None
            self.media_position_updated_at = None

        if not playing_info:
            self.media_title = "Smart TV"
            self.media_content_type = MediaType.APP
            return

        # assign media content id first
        if self.media_uri:
            self.media_content_id = self.media_uri

        # handle HDMI / external inputs
        if self.media_uri and self.media_uri.startswith("extInput"):
            # try to get user label from source_map
            label = None
            if self.enable_user_labels and self.media_uri in self.source_map:
                label = self.source_map[self.media_uri].get("label")
            if not label:
                label = playing_info.get("label")
            if label:
                self.source = label
                self.media_title = label
            else:
                self.source = playing_info.get("title")
                self.media_title = self.source

        # handle TV channels
        elif self.media_uri and self.media_uri.startswith("tv"):
            self.media_content_id = playing_info.get("dispNum")
            self.media_title = (
                playing_info.get("programTitle") or self.media_content_id
            )
            self.media_channel = playing_info.get("title") or self.media_content_id
            self.media_content_type = MediaType.CHANNEL

        # generic fallback for any app / other
        else:
            label = playing_info.get("label")
            if self.enable_user_labels and self.media_uri in self.source_map:
                label = self.source_map[self.media_uri].get("label", label)
            if label:
                self.source = label
                self.media_title = label
            elif title := playing_info.get("title"):
                self.source = title
                self.media_title = title

    async def async_update_sources(self) -> None:
        self.source_list = []
        self.source_map = {}

        inputs = await self.client.get_external_status()
        self._sources_extend(inputs, SourceType.INPUT, add_to_list=True)
        apps = await self.client.get_app_list()
        self._sources_extend(apps, SourceType.APP, sort_by="title")
        channels = await self.client.get_content_list_all("tv")
        self._sources_extend(channels, SourceType.CHANNEL)

    async def async_source_start(self, uri: str, source_type: SourceType | str) -> None:
        if source_type == SourceType.APP:
            await self.client.set_active_app(uri)
        else:
            await self.client.set_play_content(uri)

    async def async_source_find(
        self, query: str, source_type: SourceType | str
    ) -> None:
        if query.startswith(("extInput:", "tv:", "com.sony.dtv.")):
            return await self.async_source_start(query, source_type)
        coarse_uri = None
        is_numeric_search = source_type == SourceType.CHANNEL and query.isnumeric()
        for uri, item in self.source_map.items():
            if item["type"] == source_type:
                if is_numeric_search:
                    num = item.get("dispNum")
                    if num and int(query) == int(num):
                        return await self.async_source_start(uri, source_type)
                else:
                    title = item.get("title")
                    if self.enable_user_labels and (label := item.get("label")):
                        title = label
                    if query.lower() == title.lower():
                        return await self.async_source_start(uri, source_type)
                    if query.lower() in title.lower():
                        coarse_uri = uri
        if coarse_uri:
            return await self.async_source_start(coarse_uri, source_type)
        raise ValueError(f"Not found {source_type}: {query}")

    @catch_braviatv_errors
    async def async_turn_on(self) -> None:
        await self.client.turn_on()

    @catch_braviatv_errors
    async def async_turn_off(self) -> None:
        await self.client.turn_off()

    @catch_braviatv_errors
    async def async_set_volume_level(self, volume: float) -> None:
        await self.client.volume_level(round(volume * 100))

    @catch_braviatv_errors
    async def async_volume_up(self) -> None:
        await self.client.volume_up()

    @catch_braviatv_errors
    async def async_volume_down(self) -> None:
        await self.client.volume_down()

    @catch_braviatv_errors
    async def async_volume_mute(self, mute: bool) -> None:
        await self.client.volume_mute()

    @catch_braviatv_errors
    async def async_media_play(self) -> None:
        await self.client.play()

    @catch_braviatv_errors
    async def async_media_pause(self) -> None:
        await self.client.pause()

    @catch_braviatv_errors
    async def async_media_stop(self) -> None:
        await self.client.stop()

    @catch_braviatv_errors
    async def async_media_next_track(self) -> None:
        if self.media_content_type == MediaType.CHANNEL:
            await self.client.channel_up()
        else:
            await self.client.next_track()

    @catch_braviatv_errors
    async def async_media_previous_track(self) -> None:
        if self.media_content_type == MediaType.CHANNEL:
            await self.client.channel_down()
        else:
            await self.client.previous_track()

    @catch_braviatv_errors
    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        if media_type not in (MediaType.APP, MediaType.CHANNEL):
            raise ValueError(f"Invalid media type: {media_type}")
        await self.async_source_find(media_id, media_type)

    @catch_braviatv_errors
    async def async_select_source(self, source: str) -> None:
        await self.async_source_find(source, SourceType.INPUT)

    @catch_braviatv_errors
    async def async_send_command(self, command: Iterable[str], repeats: int) -> None:
        for _ in range(repeats):
            for cmd in command:
                response = await self.client.send_command(cmd)
                if not response:
                    commands = await self.client.get_command_list()
                    _LOGGER.error(
                        "Unsupported command: %s, available: %s",
                        cmd,
                        ", ".join(commands.keys()),
                    )

    @catch_braviatv_errors
    async def async_reboot_device(self) -> None:
        await self.client.reboot()

    @catch_braviatv_errors
    async def async_terminate_apps(self) -> None:
        await self.client.terminate_apps()
