"""Support for WebDav Calendar."""

import asyncio
from datetime import date, datetime
from functools import partial
import logging
from typing import Any, override
from urllib.parse import quote, urljoin

import caldav
from caldav.lib.error import DAVError, NotFoundError
from icalendar import Event as ICalendarEvent, vRecur
import requests
import voluptuous as vol

from homeassistant.components.calendar import (
    ENTITY_ID_FORMAT,
    EVENT_DESCRIPTION,
    EVENT_END,
    EVENT_LOCATION,
    EVENT_RRULE,
    EVENT_START,
    EVENT_SUMMARY,
    PLATFORM_SCHEMA as CALENDAR_PLATFORM_SCHEMA,
    CalendarEntity,
    CalendarEntityFeature,
    CalendarEvent,
    is_offset_reached,
)
from homeassistant.const import (
    CONF_NAME,
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.entity_platform import (
    AddConfigEntryEntitiesCallback,
    AddEntitiesCallback,
)
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import CalDavConfigEntry
from .api import async_get_calendars
from .const import TIMEOUT
from .coordinator import CalDavUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

CONF_CALENDARS = "calendars"
CONF_CUSTOM_CALENDARS = "custom_calendars"
CONF_CALENDAR = "calendar"
CONF_SEARCH = "search"
CONF_DAYS = "days"

# Number of days to look ahead for next event when configured by ConfigEntry
CONFIG_ENTRY_DEFAULT_DAYS = 7

RECURRENCE_THIS_AND_FUTURE = "THISANDFUTURE"
# Only allow VCALENDARs that support this component type
SUPPORTED_COMPONENT = "VEVENT"

PLATFORM_SCHEMA = CALENDAR_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_URL): vol.Url(),
        vol.Optional(CONF_CALENDARS, default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Inclusive(CONF_USERNAME, "authentication"): cv.string,
        vol.Inclusive(CONF_PASSWORD, "authentication"): cv.string,
        vol.Optional(CONF_CUSTOM_CALENDARS, default=[]): vol.All(
            cv.ensure_list,
            [
                vol.Schema(
                    {
                        vol.Required(CONF_CALENDAR): cv.string,
                        vol.Required(CONF_NAME): cv.string,
                        vol.Required(CONF_SEARCH): cv.string,
                    }
                )
            ],
        ),
        vol.Optional(CONF_VERIFY_SSL, default=True): cv.boolean,
        vol.Optional(CONF_DAYS, default=1): cv.positive_int,
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    disc_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the WebDav Calendar platform."""
    url = config[CONF_URL]
    username = config.get(CONF_USERNAME)
    password = config.get(CONF_PASSWORD)
    days = config[CONF_DAYS]

    client = caldav.DAVClient(
        url,
        None,
        username,
        password,
        ssl_verify_cert=config[CONF_VERIFY_SSL],
        timeout=TIMEOUT,
    )

    calendars = await async_get_calendars(hass, client, SUPPORTED_COMPONENT)

    entities = []
    device_id: str | None
    for calendar in list(calendars):
        # If a calendar name was given in the configuration,
        # ignore all the others
        if config[CONF_CALENDARS] and calendar.name not in config[CONF_CALENDARS]:
            _LOGGER.debug("Ignoring calendar '%s'", calendar.name)
            continue

        # Create additional calendars based on custom filtering rules
        for cust_calendar in config[CONF_CUSTOM_CALENDARS]:
            # Check that the base calendar matches
            if cust_calendar[CONF_CALENDAR] != calendar.name:
                continue

            name = cust_calendar[CONF_NAME]
            device_id = f"{cust_calendar[CONF_CALENDAR]} {cust_calendar[CONF_NAME]}"
            entity_id = async_generate_entity_id(ENTITY_ID_FORMAT, device_id, hass=hass)
            coordinator = CalDavUpdateCoordinator(
                hass,
                None,
                calendar=calendar,
                days=days,
                include_all_day=True,
                search=cust_calendar[CONF_SEARCH],
            )
            entities.append(
                WebDavCalendarEntity(name, entity_id, coordinator, supports_offset=True)
            )

        # Create a default calendar if there was no custom one for all calendars
        # that support events.
        if not config[CONF_CUSTOM_CALENDARS]:
            name = calendar.name
            device_id = calendar.name
            entity_id = async_generate_entity_id(ENTITY_ID_FORMAT, device_id, hass=hass)
            coordinator = CalDavUpdateCoordinator(
                hass,
                None,
                calendar=calendar,
                days=days,
                include_all_day=False,
                search=None,
            )
            entities.append(
                WebDavCalendarEntity(name, entity_id, coordinator, supports_offset=True)
            )

    async_add_entities(entities, True)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalDavConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the CalDav calendar platform for a config entry."""
    calendars = await async_get_calendars(hass, entry.runtime_data, SUPPORTED_COMPONENT)
    async_add_entities(
        (
            WebDavCalendarEntity(
                calendar.name,
                async_generate_entity_id(ENTITY_ID_FORMAT, calendar.name, hass=hass),
                CalDavUpdateCoordinator(
                    hass,
                    entry,
                    calendar=calendar,
                    days=CONFIG_ENTRY_DEFAULT_DAYS,
                    include_all_day=True,
                    search=None,
                ),
                unique_id=f"{entry.entry_id}-{calendar.id}",
            )
            for calendar in calendars
            if calendar.name
        ),
        True,
    )


def _as_recurrence_value(value: str, reference: date | datetime) -> date | datetime:
    """Parse an HA recurrence ID into a value compatible with the event."""
    if isinstance(reference, datetime):
        parsed = dt_util.parse_datetime(value)
        if parsed is None:
            raise HomeAssistantError(f"Invalid recurrence ID: {value}")
        if reference.tzinfo is None:
            return dt_util.as_local(parsed).replace(tzinfo=None)
        return parsed.astimezone(reference.tzinfo)

    parsed_date = dt_util.parse_date(value)
    if parsed_date is None:
        raise HomeAssistantError(f"Invalid recurrence ID: {value}")
    return parsed_date


def _replace_property(component: ICalendarEvent, name: str, value: Any | None) -> None:
    """Replace a property without disturbing unrelated iCalendar fields."""
    component.pop(name, None)
    if value is not None:
        component.add(name, value)


def _parse_rrule(value: str) -> vRecur:
    """Parse an RFC5545 RRULE string into an icalendar value."""
    rule = value[6:] if value.upper().startswith("RRULE:") else value
    return vRecur.from_ical(rule)


def _apply_event_payload(
    component: ICalendarEvent,
    event: dict[str, Any],
    *,
    allow_rrule: bool = True,
) -> None:
    """Apply an HA replacement event payload to an existing VEVENT."""
    _replace_property(component, "SUMMARY", event[EVENT_SUMMARY])
    _replace_property(component, "DTSTART", event[EVENT_START])
    _replace_property(component, "DTEND", event[EVENT_END])
    _replace_property(component, "DESCRIPTION", event.get(EVENT_DESCRIPTION))
    _replace_property(component, "LOCATION", event.get(EVENT_LOCATION))

    component.pop("DURATION", None)
    if not allow_rrule:
        component.pop("RRULE", None)
    elif EVENT_RRULE in event:
        component.pop("RRULE", None)
        if rrule := event[EVENT_RRULE]:
            component.add("RRULE", _parse_rrule(rrule))


def _uid_of(resource: Any) -> str | None:
    """Return the UID carried by a CalDAV event resource."""
    for component in resource.icalendar_instance.walk("VEVENT"):
        if (value := component.get("UID")) is not None:
            return str(value)
    return None


# python-caldav's type stubs omit `icalendar_component` available in the pinned
# runtime version.
def _event_by_uid(calendar: caldav.Calendar, uid: str) -> Any:
    """Return a server event by UID without an iCloud-incompatible UID REPORT."""
    href = urljoin(
        f"{str(calendar.url).rstrip('/')}/",
        f"{quote(uid, safe='')}.ics",
    )
    try:
        return calendar.event_by_url(href)
    except NotFoundError, DAVError:
        pass

    # Do not use python-caldav's UID REPORT here: iCloud rejects that query.
    # Fall back to an unbounded VEVENT listing so an otherwise valid event does
    # not become impossible to mutate merely because it is old or far ahead.
    for candidate in calendar.events():
        if _uid_of(candidate) == uid:
            return candidate

    raise HomeAssistantError(f"CalDAV event {uid!r} was not found")


def _update_whole_event(
    calendar: caldav.Calendar, uid: str, event: dict[str, Any]
) -> None:
    """Update a non-recurring event or the master of a recurring series."""
    resource = _event_by_uid(calendar, uid)
    _apply_event_payload(resource.icalendar_component, event)
    resource.save()


def _delete_whole_event(calendar: caldav.Calendar, uid: str) -> None:
    """Delete an event resource, including the whole recurring series."""
    _event_by_uid(calendar, uid).delete()


def _validate_recurrence_range(recurrence_range: str | None) -> None:
    """Validate a recurrence range supported by this integration."""
    if recurrence_range not in (None, RECURRENCE_THIS_AND_FUTURE):
        raise HomeAssistantError(
            f"Unsupported CalDAV recurrence range: {recurrence_range}"
        )


def _validate_recurring_master(master: Any) -> None:
    """Reject recurrence mutations when the resource is not recurring."""
    component = master.icalendar_component
    if "RRULE" not in component and "RDATE" not in component:
        raise HomeAssistantError(
            "recurrence_id requires an event with an RRULE or RDATE"
        )


def _recurrence_override(
    master: Any,
    recurrence_id: str,
    event: dict[str, Any],
    recurrence_range: str | None,
) -> ICalendarEvent:
    """Build a single recurrence override for an existing resource."""
    master_component = master.icalendar_component
    recurrence_override = master_component.copy()

    for name in ("RRULE", "RDATE", "EXRULE", "EXDATE"):
        recurrence_override.pop(name, None)

    recurrence_value = _as_recurrence_value(
        recurrence_id,
        master_component.decoded("DTSTART"),
    )
    recurrence_override.pop("RECURRENCE-ID", None)
    recurrence_override.add("RECURRENCE-ID", recurrence_value)
    if recurrence_range == RECURRENCE_THIS_AND_FUTURE:
        recurrence_override["RECURRENCE-ID"].params["RANGE"] = (
            RECURRENCE_THIS_AND_FUTURE
        )

    _apply_event_payload(recurrence_override, event, allow_rrule=False)

    return recurrence_override


def _save_recurrence_override(master: Any, recurrence_override: ICalendarEvent) -> None:
    """Merge an override into an existing resource and save by its known URL."""
    calendar = master.icalendar_instance
    for index, component in enumerate(calendar.subcomponents):
        if (
            component.name == "VEVENT"
            and "RECURRENCE-ID" in component
            and component.decoded("RECURRENCE-ID")
            == recurrence_override.decoded("RECURRENCE-ID")
        ):
            calendar.subcomponents[index] = recurrence_override
            break
    else:
        calendar.add_component(recurrence_override)
    master.save()


def _update_recurrence(
    calendar: caldav.Calendar,
    uid: str,
    recurrence_id: str,
    event: dict[str, Any],
    recurrence_range: str | None,
) -> None:
    """Update one recurrence or a this-and-future override."""
    _validate_recurrence_range(recurrence_range)
    master = _event_by_uid(calendar, uid)
    _validate_recurring_master(master)
    recurrence_override = _recurrence_override(
        master, recurrence_id, event, recurrence_range
    )
    _save_recurrence_override(master, recurrence_override)


def _add_exdate(master: Any, recurrence_id: str) -> None:
    """Exclude a single occurrence from a recurring event."""
    component = master.icalendar_component
    recurrence_value = _as_recurrence_value(
        recurrence_id,
        component.decoded("DTSTART"),
    )
    component.add("EXDATE", recurrence_value)
    master.save()


def _cancel_recurrence_range(master: Any, recurrence_id: str) -> None:
    """Cancel this and future occurrences with a recurrence override."""
    component = master.icalendar_component
    recurrence_value = _as_recurrence_value(
        recurrence_id,
        component.decoded("DTSTART"),
    )
    recurrence_override = component.copy()
    for name in ("RRULE", "RDATE", "EXRULE", "EXDATE"):
        recurrence_override.pop(name, None)
    recurrence_override.pop("RECURRENCE-ID", None)
    recurrence_override.add("RECURRENCE-ID", recurrence_value)
    recurrence_override["RECURRENCE-ID"].params["RANGE"] = RECURRENCE_THIS_AND_FUTURE
    _replace_property(recurrence_override, "STATUS", "CANCELLED")

    _save_recurrence_override(master, recurrence_override)


def _delete_recurrence(
    calendar: caldav.Calendar,
    uid: str,
    recurrence_id: str,
    recurrence_range: str | None,
) -> None:
    """Delete one occurrence or this and future occurrences."""
    _validate_recurrence_range(recurrence_range)
    master = _event_by_uid(calendar, uid)
    _validate_recurring_master(master)
    if recurrence_range == RECURRENCE_THIS_AND_FUTURE:
        _cancel_recurrence_range(master, recurrence_id)
        return
    _add_exdate(master, recurrence_id)


class WebDavCalendarEntity(CoordinatorEntity[CalDavUpdateCoordinator], CalendarEntity):
    """A device for getting the next Task from a WebDav Calendar."""

    _attr_supported_features = (
        CalendarEntityFeature.CREATE_EVENT
        | CalendarEntityFeature.DELETE_EVENT
        | CalendarEntityFeature.UPDATE_EVENT
    )

    def __init__(
        self,
        name: str | None,
        entity_id: str,
        coordinator: CalDavUpdateCoordinator,
        unique_id: str | None = None,
        supports_offset: bool = False,
    ) -> None:
        """Create the WebDav Calendar Event Device."""
        super().__init__(coordinator)
        self.entity_id = entity_id
        self._event: CalendarEvent | None = None
        self._attr_name = name
        if unique_id is not None:
            self._attr_unique_id = unique_id
        self._supports_offset = supports_offset
        self._mutation_lock = asyncio.Lock()
        if coordinator.search is not None:
            # A filtered entity is only a view of another calendar. Do not
            # advertise update/delete because UID mutation could affect events
            # outside the view's filter semantics.
            self._attr_supported_features = CalendarEntityFeature.CREATE_EVENT

    @property
    @override
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming event."""
        return self._event

    @override
    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Get all events in a specific time frame."""
        return await self.coordinator.async_get_events(hass, start_date, end_date)

    @override
    async def async_create_event(self, **kwargs: Any) -> None:
        """Create a new event in the calendar."""
        _LOGGER.debug("Event: %s", kwargs)

        item_data: dict[str, Any] = {
            "summary": kwargs["summary"],
            "dtstart": kwargs["dtstart"],
            "dtend": kwargs["dtend"],
        }
        if description := kwargs.get("description"):
            item_data["description"] = description
        if location := kwargs.get("location"):
            item_data["location"] = location
        if rrule := kwargs.get("rrule"):
            item_data["rrule"] = rrule

        _LOGGER.debug("ICS data %s", item_data)

        async with self._mutation_lock:
            try:
                await self.hass.async_add_executor_job(
                    partial(self.coordinator.calendar.add_event, **item_data),
                )
            except (requests.ConnectionError, requests.Timeout, DAVError) as err:
                raise HomeAssistantError(f"CalDAV save error: {err}") from err

        await self.coordinator.async_request_refresh()
        self.async_update_event_listeners()

    @override
    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Delete an event from a CalDAV calendar."""
        if recurrence_id is None and recurrence_range is not None:
            raise HomeAssistantError("recurrence_range requires a recurrence_id")

        async with self._mutation_lock:
            try:
                if recurrence_id is None:
                    await self.hass.async_add_executor_job(
                        _delete_whole_event, self.coordinator.calendar, uid
                    )
                else:
                    await self.hass.async_add_executor_job(
                        _delete_recurrence,
                        self.coordinator.calendar,
                        uid,
                        recurrence_id,
                        recurrence_range,
                    )
            except HomeAssistantError:
                raise
            except (requests.ConnectionError, requests.Timeout, DAVError) as err:
                raise HomeAssistantError(f"CalDAV delete error: {err}") from err

        await self.coordinator.async_request_refresh()
        self.async_update_event_listeners()

    @override
    async def async_update_event(
        self,
        uid: str,
        event: dict[str, Any],
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Update an event on a CalDAV calendar."""
        if recurrence_id is None and recurrence_range is not None:
            raise HomeAssistantError("recurrence_range requires a recurrence_id")

        async with self._mutation_lock:
            try:
                if recurrence_id is None:
                    await self.hass.async_add_executor_job(
                        _update_whole_event,
                        self.coordinator.calendar,
                        uid,
                        event,
                    )
                else:
                    await self.hass.async_add_executor_job(
                        _update_recurrence,
                        self.coordinator.calendar,
                        uid,
                        recurrence_id,
                        event,
                        recurrence_range,
                    )
            except HomeAssistantError:
                raise
            except (requests.ConnectionError, requests.Timeout, DAVError) as err:
                raise HomeAssistantError(f"CalDAV update error: {err}") from err

        await self.coordinator.async_request_refresh()
        self.async_update_event_listeners()

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Update event data."""
        self._event = self.coordinator.data
        if self._supports_offset:
            self._attr_extra_state_attributes = {
                "offset_reached": is_offset_reached(
                    self._event.start_datetime_local,
                    self.coordinator.offset,  # type: ignore[arg-type]
                )
                if self._event
                else False
            }
        super()._handle_coordinator_update()

    @override
    async def async_added_to_hass(self) -> None:
        """When entity is added to hass update state from existing coordinator data."""
        await super().async_added_to_hass()
        self._handle_coordinator_update()
