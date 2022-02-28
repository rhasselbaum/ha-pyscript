"""
Apply scheduled temperature changes to climate entities. The climate_updates service should be called periodically
(at least on the local times indicated in the schedules). When the current local date/time matches a day pattern
and time in the scedule, the corrsponding temperature is applied to all entities in the zone OR the vacation mode
temperature if vacation mode is on. The service uses a different schedule depending on whether the HVAC mode of
the first entity per zone is "heat" or "cool". (If it is neither, nothing happens.)
"""

from enum import Enum
from datetime import datetime

# Zones and climate entities.
Zone = Enum("Zone", "UPSTAIRS DOWNSTAIRS MASTER_BEDROOM")
# First entity deterines HVAC mode for each zone.
ZONE_ENTITIES = {
    Zone.UPSTAIRS: [
        "climate.back_bedroom_mini_split",
        "climate.front_bedroom_mini_split",
    ],
    Zone.DOWNSTAIRS: ["climate.living_room_mini_split", "climate.office_mini_split"],
    Zone.MASTER_BEDROOM: ["climate.master_bedroom_mini_split"],
}

# Day of week patterns
DayPattern = Enum("DayPattern", "ALL WEEKDAYS WEEKENDS")

# Schedules. An automation calling the service must be triggered on these times. Otherwise, nothing will happen.
HEAT_SCHEDULE = {
    Zone.UPSTAIRS: {
        DayPattern.WEEKDAYS: {
            "08:00": 62,
        },
        DayPattern.ALL: {
            "14:00": 69,
            "23:00": 69,
        },
    },
    Zone.MASTER_BEDROOM: {
        DayPattern.WEEKDAYS: {
            "06:30": 68,
            "12:00": 65,
        },
        DayPattern.ALL: {
            "06:30": 68,
            "17:00": 65,
        },
    },
    Zone.DOWNSTAIRS: {
        DayPattern.ALL: {
            "06:00": 68,
            "22:30": 60,
        },
    },
}
VACATION_HEAT_TEMP = 60


def _apply_zone_temp(zone, zone_schedule, vacation_mode_temp, heat_boost, now):
    """Apply scheduled temperatute change for one zone.

    Check the current day of week against the day patterns in the zone schedule and if they match,
    apply scheduled temperature change for the current day and time, if any. More specific day
    patterns (weekdays and weekends) override less specific ones ("all").

    If the current time matches a time in the schedule, set the zone temperature to the scheduled
    temp or the vacation mode temp if vacation mode is on. If heat boost is enabled, the scheduled
    temp is adjusted slightly higher to compensate for stubborn controller.

    Params:
        zone: Name of top-most element from schedule structures.
        zone_schedule: Dict of day patterns (weekends, weekdays, etc.) to time-temp schedule.
        vacation_mode_temp: Override temperature value to set when vacation mode is enabled.
        heat_boost: If true, increase target temp by a small amount above the scheduled temp to
            compensate for controller not quite hitting target temp when using oil.
        now: Current datetime.
    """
    # Create a merged schedule from all applicable day patterns.
    merged_day_schedule = {}
    if zone_schedule.get(DayPattern.ALL):
        merged_day_schedule.update(zone_schedule[DayPattern.ALL])
    if now.weekday() < 5 and zone_schedule.get(DayPattern.WEEKDAYS):
        merged_day_schedule.update(zone_schedule[DayPattern.WEEKDAYS])
    elif now.weekday() >= 5 and zone_schedule.get(DayPattern.WEEKENDS):
        merged_day_schedule.update(zone_schedule[DayPattern.WEEKENDS])

    # Apply change for the current time, if any.
    scheduled_temp = merged_day_schedule.get(now.strftime("%H:%M"))
    if scheduled_temp is not None:
        target_temp = (
            vacation_mode_temp
            if input_boolean.vacation_mode == "on"
            else scheduled_temp
        )
        adjusted_temp = target_temp if not heat_boost else target_temp + 2
        climate.set_temperature(
            entity_id=ZONE_ENTITIES[zone], temperature=adjusted_temp, blocking=True
        )
        log.info(f"Setting new temperature {adjusted_temp} on {ZONE_ENTITIES[zone]}.")


@service
def climate_updates():
    """Update thermostats if necessary based on schedule."""
    now = datetime.now()
    for zone in list(Zone):
        # Pick one entity from each zone to determine if we're in "heat" or "cool" mode.
        # Assumption is that all units in a zone will be the same mode. Otherwise, weirdness.
        sample_zone_entity = ZONE_ENTITIES[zone][0]
        hvac_mode = state.get(sample_zone_entity)
        if hvac_mode == "heat":
            # When minisplits are idle in heat mode, we're using oil. But for some reason, the
            # controller keeps max temp 2 degrees colder than target so we give a small boost
            # to target temps.
            heat_boost = state.get(f"{sample_zone_entity}.hvac_action") == "idle"
            # Apply heat schedule.
            _apply_zone_temp(
                zone, HEAT_SCHEDULE[zone], VACATION_HEAT_TEMP, heat_boost, now
            )
        elif hvac_mode == "cool":
            log.error("Cooling schedule not defined yet.")


@service
def dial_temperature(zone, degrees):
    """Change relative temperature in a zone.

    Params:
        zone: Name of top-most element from zone entities structure.
        degrees: Amount of change (integer). Positive increases temp, negative decreases.
    """
    if zone not in [zone.name for zone in Zone]:
        raise ValueError(f"Climate zone not found: {zone}")
    entities = ZONE_ENTITIES.get(Zone[zone])
    for entity_id in entities:
        old_temp = state.get(f"{entity_id}.temperature")
        new_temp = old_temp + degrees
        if old_temp != new_temp:
            climate.set_temperature(entity_id=entity_id, temperature=new_temp, blocking=True)
            direction = "Increasing" if old_temp < new_temp else "Decreasing"
            log.info(f"{direction} {entity_id} temperature from {old_temp} to {new_temp}.")
        else:
            log.warning(f"Temperature change of 0 was requested for {entity_id}.")
