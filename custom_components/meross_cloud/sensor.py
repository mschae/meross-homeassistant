import logging
from typing import Any, Optional, Iterable, Union

import homeassistant.util.color as color_util
from homeassistant.const import DEVICE_CLASS_BATTERY, DEVICE_CLASS_TEMPERATURE, TEMP_CELSIUS
from homeassistant.helpers.entity import Entity
from meross_iot.controller.device import BaseDevice
from meross_iot.controller.mixins.electricity import ElectricityMixin
from meross_iot.controller.mixins.light import LightMixin
from meross_iot.controller.subdevice import Ms100Sensor
from meross_iot.manager import MerossManager
from meross_iot.model.enums import OnlineStatus, Namespace
from meross_iot.model.exception import CommandTimeoutError
from datetime import timedelta

from meross_iot.model.push.bind import BindPushNotification
from meross_iot.model.push.generic import GenericPushNotification

from .common import (DOMAIN, MANAGER, log_exception, RELAXED_SCAN_INTERVAL, HA_SENSOR, calculate_sensor_id)


_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 1
SCAN_INTERVAL = timedelta(seconds=RELAXED_SCAN_INTERVAL)


class TemperatureSensorWrapper(Entity):
    """Wrapper class to adapt the Meross bulbs into the Homeassistant platform"""

    def __init__(self, device: Ms100Sensor, channel: int = 0):
        self._device = device
        self._channel_id = channel

        # Each Meross Device might expose more than 1 sensor. In this case, we cannot rely only on the
        # uuid value to uniquely identify a sensor wrapper.
        if len(device.channels) > 1:
            self._id = calculate_sensor_id(uuid=device.internal_id, type="temperature", channel=channel)
            channel_data = device.channels[channel]
            self._entity_name = "{} - {} - {}".format(device.name, channel_data.name, "temperature sensor")
        else:
            self._id = calculate_sensor_id(uuid=device.internal_id, type="temperature", channel=0)
            self._entity_name = "{} - {} - {}".format(device.name, "", "temperature sensor")

    # region Device wrapper common methods
    async def async_update(self):
        if self._device.online_status == OnlineStatus.ONLINE:
            try:
                await self._device.async_update()
            except CommandTimeoutError as e:
                log_exception(logger=_LOGGER, device=self._device)
                pass

    async def _async_push_notification_received(self, namespace: Namespace, data: dict):
        if namespace == Namespace.CONTROL_UNBIND:
            _LOGGER.info("Received unbind event. Removing the device from HA")
            await self.platform.async_remove_entity(self.entity_id)
        else:
            self.async_schedule_update_ha_state(force_refresh=False)

    async def async_added_to_hass(self) -> None:
        self._device.register_push_notification_handler_coroutine(self._async_push_notification_received)

    async def async_will_remove_from_hass(self) -> None:
        self._device.unregister_push_notification_handler_coroutine(self._async_push_notification_received)
    # endregion

    # region Device wrapper common properties
    @property
    def unique_id(self) -> str:
        return self._id

    @property
    def name(self) -> str:
        return self._entity_name

    @property
    def device_info(self):
        return {
            'identifiers': {(DOMAIN, self._device.internal_id)},
            'name': self._device.name,
            'manufacturer': 'Meross',
            'model': self._device.type + " " + self._device.hardware_version,
            'sw_version': self._device.firmware_version
        }

    @property
    def available(self) -> bool:
        # A device is available if the client library is connected to the MQTT broker and if the
        # device we are contacting is online
        return self._device.online_status == OnlineStatus.ONLINE

    @property
    def should_poll(self) -> bool:
        # Even though we use PUSH notifications to quickly react to cloud-events,
        # we also rely on a super-relaxed polling system which allows us to recover from
        # state inconsistency that might arise when connection quality is not good enough.
        return True
    # endregion

    # region Platform-specific command methods
    # endregion

    # region Platform specific properties
    @property
    def device_class(self) -> Optional[str]:
        return DEVICE_CLASS_TEMPERATURE

    @property
    def state(self) -> Union[None, str, int, float]:
        """Return the state of the entity."""
        return self._device.last_sampled_temperature

    @property
    def unit_of_measurement(self) -> Optional[str]:
        return TEMP_CELSIUS
    # endregion


# ----------------------------------------------
# PLATFORM METHODS
# ----------------------------------------------
def _add_entities(hass, devices: Iterable[BaseDevice], async_add_entities):
    new_entities = []
    # For now, we handle the following sensors:
    # -> Temperature-Humidity (Ms100Sensor)
    # -> Power-sensing smart plugs (Mss310)
    # TODO: In the future, we might add support for Mts100 valve. We need to think about battery effects, though.
    humidity_temp_sensors = filter(lambda d: isinstance(d, Ms100Sensor), devices)
    power_sensors = filter(lambda d: isinstance(d, ElectricityMixin), devices)

    # Add Temperature & Humidity sensors
    for d in humidity_temp_sensors:
        t = TemperatureSensorWrapper(device=d)
        if t.unique_id not in hass.data[DOMAIN][HA_SENSOR]:
            _LOGGER.debug(f"Device {t.unique_id} is new, will be added to HA")
            new_entities.append(t)
        else:
            _LOGGER.debug(f"Skipping device {t.unique_id} as it's already present in HA")

    # Add Power Sensors
    """
    for d in power_sensors:
        for channel_index, channel in enumerate(d.channels):
            w = PowerEntityWrapper(device=d, channel=channel_index)
            if w.unique_id not in hass.data[DOMAIN][HA_SENSOR]:
                _LOGGER.debug(f"Device {w.unique_id} is new, will be added to HA")
                new_entities.append(w)
            else:
                _LOGGER.debug(f"Skipping device {w.unique_id} as it's already present in HA")
    """
    async_add_entities(new_entities, True)
    

async def async_setup_entry(hass, config_entry, async_add_entities):
    # When loading the platform, immediately add currently available
    # bulbs.
    manager = hass.data[DOMAIN][MANAGER]  # type:MerossManager
    devices = manager.find_devices()
    _add_entities(hass=hass, devices=devices, async_add_entities=async_add_entities)

    # Register a listener for the Bind push notification so that we can add new entities at runtime
    async def platform_async_add_entities(push_notification: GenericPushNotification, target_device: BaseDevice):
        if isinstance(push_notification, BindPushNotification):
            devs = manager.find_devices(device_uuids=(push_notification.hwinfo.uuid,))
            _add_entities(hass=hass, devices=devs, async_add_entities=async_add_entities)

    # Register a listener for new bound devices
    manager.register_push_notification_handler_coroutine(platform_async_add_entities)


# TODO: Unload entry
# TODO: Remove entry


def setup_platform(hass, config, async_add_entities, discovery_info=None):
    _LOGGER.info("SETUP PLATFORM")
    pass