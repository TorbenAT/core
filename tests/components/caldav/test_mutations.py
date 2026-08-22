"""Tests for CalDAV calendar mutations."""

import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from caldav.lib.error import DAVError, NotFoundError
from icalendar import Calendar as ICalendar, Event as ICalendarEvent
import pytest
import requests

from homeassistant.components.caldav.calendar import (
    WebDavCalendarEntity,
    _delete_recurrence,
    _delete_whole_event,
    _event_by_uid,
    _update_recurrence,
    _update_whole_event,
)
from homeassistant.components.calendar import EVENT_END, EVENT_START, EVENT_SUMMARY
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError


def _ics_event(data: str) -> ICalendarEvent:
    """Return the VEVENT from iCalendar data."""
    calendar = ICalendar.from_ical(data)
    return next(
        component for component in calendar.walk() if component.name == "VEVENT"
    )


def test_update_whole_event_preserves_unrelated_properties() -> None:
    """Test a whole-event update keeps server-managed properties."""
    calendar = MagicMock()
    resource = MagicMock()
    resource.icalendar_component = _ics_event(
        """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:abc
DTSTAMP:20260810T180000Z
DTSTART:20260815T100000Z
DTEND:20260815T110000Z
RRULE:FREQ=DAILY;COUNT=3
SUMMARY:Old
ATTENDEE:mailto:someone@example.com
X-TEST:preserve-me
END:VEVENT
END:VCALENDAR
"""
    )
    calendar.event_by_url.return_value = resource

    _update_whole_event(
        calendar,
        "abc",
        {
            EVENT_SUMMARY: "New",
            EVENT_START: datetime.datetime(2026, 8, 15, 12, 0, tzinfo=datetime.UTC),
            EVENT_END: datetime.datetime(2026, 8, 15, 13, 0, tzinfo=datetime.UTC),
        },
    )

    component = resource.icalendar_component
    assert str(component["SUMMARY"]) == "New"
    assert str(component["ATTENDEE"]) == "mailto:someone@example.com"
    assert str(component["X-TEST"]) == "preserve-me"
    assert str(component["UID"]) == "abc"
    assert component["RRULE"].to_ical() == b"FREQ=DAILY;COUNT=3"
    resource.save.assert_called_once_with()


def test_update_whole_all_day_event_preserves_uid() -> None:
    """Test an all-day update retains the resource UID and date value types."""
    calendar = MagicMock()
    resource = MagicMock()
    resource.icalendar_component = _ics_event(
        """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:all-day
DTSTART;VALUE=DATE:20260815
DTEND;VALUE=DATE:20260816
SUMMARY:Old
END:VEVENT
END:VCALENDAR
"""
    )
    calendar.event_by_url.return_value = resource

    _update_whole_event(
        calendar,
        "all-day",
        {
            EVENT_SUMMARY: "Updated",
            EVENT_START: datetime.date(2026, 8, 16),
            EVENT_END: datetime.date(2026, 8, 17),
        },
    )

    component = resource.icalendar_component
    assert str(component["UID"]) == "all-day"
    assert component.decoded("DTSTART") == datetime.date(2026, 8, 16)
    assert component.decoded("DTEND") == datetime.date(2026, 8, 17)
    resource.save.assert_called_once_with()


def test_delete_whole_event() -> None:
    """Test whole-event deletion."""
    calendar = MagicMock()
    resource = MagicMock()
    calendar.event_by_url.return_value = resource

    _delete_whole_event(calendar, "abc")

    calendar.event_by_url.assert_called_once()
    resource.delete.assert_called_once_with()


def test_delete_single_recurrence_adds_exdate() -> None:
    """Test deleting one recurrence excludes it from the master series."""
    calendar = MagicMock()
    master = MagicMock()
    master.icalendar_component = _ics_event(
        """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:series
DTSTART:20260811T090000Z
DTEND:20260811T100000Z
RRULE:FREQ=DAILY;COUNT=5
SUMMARY:Series
END:VEVENT
END:VCALENDAR
"""
    )
    calendar.event_by_url.return_value = master

    _delete_recurrence(calendar, "series", "2026-08-12 09:00:00+00:00", None)

    assert "EXDATE" in master.icalendar_component
    master.save.assert_called_once_with()


def test_delete_all_day_recurrence_adds_date_exdate() -> None:
    """Test deleting an all-day recurrence preserves its date value type."""
    calendar = MagicMock()
    master = MagicMock()
    master.icalendar_component = _ics_event(
        """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:series
DTSTART;VALUE=DATE:20260811
DTEND;VALUE=DATE:20260812
RRULE:FREQ=DAILY;COUNT=5
SUMMARY:Series
END:VEVENT
END:VCALENDAR
"""
    )
    calendar.event_by_url.return_value = master

    _delete_recurrence(calendar, "series", "2026-08-12", None)

    assert master.icalendar_component.decoded("EXDATE").dts[0].dt == datetime.date(
        2026, 8, 12
    )
    master.save.assert_called_once_with()


def test_delete_rdate_recurrence_adds_exdate() -> None:
    """Test deleting an RDATE occurrence is supported."""
    calendar = MagicMock()
    master = MagicMock()
    master.icalendar_component = _ics_event(
        """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:rdate-series
DTSTART:20260811T090000Z
DTEND:20260811T100000Z
RDATE:20260812T090000Z
SUMMARY:Series
END:VEVENT
END:VCALENDAR
"""
    )
    calendar.event_by_url.return_value = master

    _delete_recurrence(calendar, "rdate-series", "2026-08-12 09:00:00+00:00", None)

    assert "EXDATE" in master.icalendar_component
    master.save.assert_called_once_with()


def test_update_this_and_future_sets_range_parameter() -> None:
    """Test a future recurrence update carries RANGE=THISANDFUTURE."""
    calendar = MagicMock()
    master = MagicMock()
    master.icalendar_component = _ics_event(
        """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:series
DTSTART:20260811T090000Z
DTEND:20260811T100000Z
RRULE:FREQ=DAILY;COUNT=5
SUMMARY:Series
END:VEVENT
END:VCALENDAR
"""
    )
    master.icalendar_instance = ICalendar.from_ical(
        """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:series
DTSTART:20260811T090000Z
DTEND:20260811T100000Z
RRULE:FREQ=DAILY;COUNT=5
SUMMARY:Series
END:VEVENT
END:VCALENDAR
"""
    )
    calendar.event_by_url.return_value = master

    _update_recurrence(
        calendar,
        "series",
        "2026-08-12 09:00:00+00:00",
        {
            EVENT_SUMMARY: "Updated series",
            EVENT_START: datetime.datetime(2026, 8, 12, 11, 0, tzinfo=datetime.UTC),
            EVENT_END: datetime.datetime(2026, 8, 12, 12, 0, tzinfo=datetime.UTC),
        },
        "THISANDFUTURE",
    )

    override = next(
        component
        for component in master.icalendar_instance.walk()
        if component.name == "VEVENT" and "RECURRENCE-ID" in component
    )
    assert "RRULE" in master.icalendar_component
    assert "RRULE" not in override
    assert override["RECURRENCE-ID"].params["RANGE"] == "THISANDFUTURE"
    master.save.assert_called_once_with()


def test_delete_this_and_future_creates_cancelled_override() -> None:
    """Test deleting future recurrences creates a cancelled range override."""
    calendar = MagicMock()
    master = MagicMock()
    master.icalendar_component = _ics_event(
        """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:series
DTSTART:20260811T090000Z
DTEND:20260811T100000Z
RRULE:FREQ=DAILY;COUNT=5
SUMMARY:Series
END:VEVENT
END:VCALENDAR
"""
    )
    master.icalendar_instance = ICalendar.from_ical(
        """BEGIN:VCALENDAR
PRODID:-//Example//Calendar//EN
VERSION:2.0
CALSCALE:GREGORIAN
BEGIN:VEVENT
UID:series
DTSTART:20260811T090000Z
DTEND:20260811T100000Z
RRULE:FREQ=DAILY;COUNT=5
SUMMARY:Series
END:VEVENT
END:VCALENDAR
"""
    )
    calendar.event_by_url.return_value = master

    _delete_recurrence(
        calendar,
        "series",
        "2026-08-12 09:00:00+00:00",
        "THISANDFUTURE",
    )

    override = next(
        component
        for component in master.icalendar_instance.walk()
        if component.name == "VEVENT" and "RECURRENCE-ID" in component
    )
    assert "RRULE" not in override
    assert str(override["STATUS"]) == "CANCELLED"
    assert override["RECURRENCE-ID"].params["RANGE"] == "THISANDFUTURE"
    assert str(master.icalendar_instance["PRODID"]) == "-//Example//Calendar//EN"
    master.save.assert_called_once_with()


def test_missing_event_is_home_assistant_error() -> None:
    """Test a missing CalDAV event has a user-facing error."""
    calendar = MagicMock()
    calendar.event_by_url.side_effect = NotFoundError("missing")
    calendar.events.return_value = []

    with pytest.raises(HomeAssistantError, match="missing"):
        _event_by_uid(calendar, "missing")


def test_event_by_uid_falls_back_to_event_listing() -> None:
    """Test a non-addressable UID falls back to an iCloud-compatible listing."""
    calendar = MagicMock()
    calendar.url = "https://caldav.example.test/calendar/"
    calendar.event_by_url.side_effect = NotFoundError("not found")
    candidate = MagicMock()
    candidate.icalendar_instance = ICalendar.from_ical(
        """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:series
DTSTART:20260811T090000Z
DTEND:20260811T100000Z
SUMMARY:Series
END:VEVENT
END:VCALENDAR
"""
    )
    calendar.events.return_value = [candidate]

    assert _event_by_uid(calendar, "series") is candidate
    calendar.events.assert_called_once_with()
    calendar.search.assert_not_called()


def test_delete_recurrence_rejects_unknown_range() -> None:
    """Test a CalDAV recurrence rejects unsupported range values."""
    with pytest.raises(HomeAssistantError, match="Unsupported"):
        _delete_recurrence(MagicMock(), "series", "2026-08-12", "THISANDPRIOR")


def test_delete_recurrence_rejects_nonrecurring_master() -> None:
    """Test deleting a recurrence requires a recurring master event."""
    calendar = MagicMock()
    master = MagicMock()
    master.icalendar_component = _ics_event(
        """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:single
DTSTART:20260811T090000Z
DTEND:20260811T100000Z
SUMMARY:Single
END:VEVENT
END:VCALENDAR
"""
    )
    calendar.event_by_url.return_value = master

    with pytest.raises(HomeAssistantError, match="RRULE or RDATE"):
        _delete_recurrence(calendar, "single", "2026-08-12", None)

    master.save.assert_not_called()


def test_update_recurrence_rejects_unknown_range() -> None:
    """Test an update rejects an unsupported recurrence range before mutation."""
    with pytest.raises(HomeAssistantError, match="Unsupported"):
        _update_recurrence(
            MagicMock(),
            "series",
            "2026-08-12",
            {EVENT_SUMMARY: "Updated"},
            "THISANDPRIOR",
        )


async def test_async_create_event_refreshes_entity(hass: HomeAssistant) -> None:
    """Test an entity create refreshes coordinator data and listeners."""
    entity = object.__new__(WebDavCalendarEntity)
    entity.hass = hass
    entity.coordinator = MagicMock()
    entity.coordinator.async_request_refresh = AsyncMock()
    entity.async_update_event_listeners = MagicMock()
    entity._mutation_lock = asyncio.Lock()

    await entity.async_create_event(
        summary="Created",
        dtstart=datetime.datetime(2026, 8, 15, 12, 0, tzinfo=datetime.UTC),
        dtend=datetime.datetime(2026, 8, 15, 13, 0, tzinfo=datetime.UTC),
    )

    entity.coordinator.calendar.add_event.assert_called_once_with(
        summary="Created",
        dtstart=datetime.datetime(2026, 8, 15, 12, 0, tzinfo=datetime.UTC),
        dtend=datetime.datetime(2026, 8, 15, 13, 0, tzinfo=datetime.UTC),
    )
    entity.coordinator.async_request_refresh.assert_awaited_once()
    entity.async_update_event_listeners.assert_called_once_with()


async def test_async_update_event_refreshes_entity(hass: HomeAssistant) -> None:
    """Test an entity update refreshes the coordinator and its listeners."""
    entity = object.__new__(WebDavCalendarEntity)
    entity.hass = hass
    entity.coordinator = MagicMock()
    entity.coordinator.async_request_refresh = AsyncMock()
    entity.async_update_event_listeners = MagicMock()
    entity._mutation_lock = asyncio.Lock()

    event = {
        EVENT_SUMMARY: "Updated",
        EVENT_START: datetime.datetime(2026, 8, 15, 12, 0, tzinfo=datetime.UTC),
        EVENT_END: datetime.datetime(2026, 8, 15, 13, 0, tzinfo=datetime.UTC),
    }
    with patch(
        "homeassistant.components.caldav.calendar._update_whole_event"
    ) as update_event:
        await entity.async_update_event("abc", event)

    update_event.assert_called_once_with(entity.coordinator.calendar, "abc", event)
    entity.coordinator.async_request_refresh.assert_awaited_once()
    entity.async_update_event_listeners.assert_called_once_with()


async def test_async_delete_event_refreshes_entity(hass: HomeAssistant) -> None:
    """Test an entity delete refreshes the coordinator and its listeners."""
    entity = object.__new__(WebDavCalendarEntity)
    entity.hass = hass
    entity.coordinator = MagicMock()
    entity.coordinator.async_request_refresh = AsyncMock()
    entity.async_update_event_listeners = MagicMock()
    entity._mutation_lock = asyncio.Lock()

    with patch(
        "homeassistant.components.caldav.calendar._delete_whole_event"
    ) as delete_event:
        await entity.async_delete_event("abc")

    delete_event.assert_called_once_with(entity.coordinator.calendar, "abc")
    entity.coordinator.async_request_refresh.assert_awaited_once()
    entity.async_update_event_listeners.assert_called_once_with()


async def test_async_delete_event_rejects_range_without_recurrence_id(
    hass: HomeAssistant,
) -> None:
    """Test an entity rejects an invalid recurrence-range request."""
    entity = object.__new__(WebDavCalendarEntity)
    entity.hass = hass
    entity.coordinator = MagicMock()

    with pytest.raises(HomeAssistantError, match="requires a recurrence_id"):
        await entity.async_delete_event("abc", recurrence_range="THISANDFUTURE")


@pytest.mark.parametrize(
    ("method", "mutation", "error_message"),
    [
        ("delete", "_delete_whole_event", "CalDAV delete error"),
        ("update", "_update_whole_event", "CalDAV update error"),
    ],
)
@pytest.mark.parametrize(
    "error", [requests.ConnectionError, requests.Timeout, DAVError]
)
async def test_failed_mutation_does_not_refresh_entity(
    hass: HomeAssistant,
    method: str,
    mutation: str,
    error_message: str,
    error: type[Exception],
) -> None:
    """Test a failed CalDAV mutation does not refresh or notify listeners."""
    entity = object.__new__(WebDavCalendarEntity)
    entity.hass = hass
    entity.coordinator = MagicMock()
    entity.coordinator.async_request_refresh = AsyncMock()
    entity.async_update_event_listeners = MagicMock()
    entity._mutation_lock = asyncio.Lock()

    async def call_mutation() -> None:
        if method == "delete":
            await entity.async_delete_event("abc")
        else:
            await entity.async_update_event(
                "abc",
                {
                    EVENT_SUMMARY: "Updated",
                    EVENT_START: datetime.datetime(
                        2026, 8, 15, 12, 0, tzinfo=datetime.UTC
                    ),
                    EVENT_END: datetime.datetime(
                        2026, 8, 15, 13, 0, tzinfo=datetime.UTC
                    ),
                },
            )

    with (
        patch(
            f"homeassistant.components.caldav.calendar.{mutation}",
            side_effect=error(),
        ),
        pytest.raises(HomeAssistantError, match=error_message),
    ):
        await call_mutation()

    entity.coordinator.async_request_refresh.assert_not_awaited()
    entity.async_update_event_listeners.assert_not_called()


async def test_mutations_are_serialized_per_entity() -> None:
    """Test concurrent mutations do not overlap for one CalDAV entity."""
    entity = object.__new__(WebDavCalendarEntity)
    entity.coordinator = MagicMock()
    entity.coordinator.async_request_refresh = AsyncMock()
    entity.async_update_event_listeners = MagicMock()
    entity._mutation_lock = asyncio.Lock()
    entity.hass = MagicMock()
    active_mutations = 0
    max_active_mutations = 0

    async def run_mutation(*args: object) -> None:
        nonlocal active_mutations, max_active_mutations
        active_mutations += 1
        max_active_mutations = max(max_active_mutations, active_mutations)
        await asyncio.sleep(0)
        active_mutations -= 1

    entity.hass.async_add_executor_job = AsyncMock(side_effect=run_mutation)
    event = {
        EVENT_SUMMARY: "Updated",
        EVENT_START: datetime.datetime(2026, 8, 15, 12, 0, tzinfo=datetime.UTC),
        EVENT_END: datetime.datetime(2026, 8, 15, 13, 0, tzinfo=datetime.UTC),
    }

    await asyncio.gather(
        entity.async_update_event("first", event), entity.async_delete_event("second")
    )

    assert max_active_mutations == 1
