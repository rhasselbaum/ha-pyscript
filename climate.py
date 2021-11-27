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
    Zone.UPSTAIRS: ["climate.back_bedroom_mini_split", "climate.front_bedroom_mini_split"],
    Zone.DOWNSTAIRS: ["climate.living_room_mini_split", "climate.office_mini_split"],
    Zone.MASTER_BEDROOM: ["climate.master_bedroom_mini_split"],
}

# Day of week patterns
DayPattern = Enum("DayPattern", "ALL WEEKDAYS WEEKENDS")

# Schedules. An automation calling the service must be triggered on these times. Otherwise, nothing will happen.
HEAT_SCHEDULE = {
    Zone.UPSTAIRS: {
        DayPattern.WEEKDAYS: {
            "08:00": 58,
        },
        DayPattern.ALL: {
            "15:00": 69,
            "23:00": 69,
        },
    },
    Zone.MASTER_BEDROOM: {
        DayPattern.ALL: {
            "07:00": 65,
            "17:00": 62,
        },
    },
    Zone.DOWNSTAIRS: {
        DayPattern.ALL: {
            "22:30": 58,
            "06:30": 68,
        },
    },
}
VACATION_HEAT_TEMP = 58


def _apply_day_zone_temps(zone, day_schedule, vacation_mode_temp, now):
    """Apply temperature change for one zone and day.
    
    If the current time matches a time in the schedule, set the zone temperature to the scheduled
    temp or the vacation mode temp if vacation mode is on.
    """
    scheduled_temp = day_schedule.get(now.strftime("%H:%M"))
    if scheduled_temp is not None:
        target_temp = vacation_mode_temp if input_boolean.vacation_mode == "on" else scheduled_temp 
        climate.set_temperature(entity_id=ZONE_ENTITIES[zone], temperature=target_temp, blocking=True)


def _apply_zone_temps(zone, zone_schedule, vacation_mode_temp, now):
    """Apply scheduled temperatute changes for one zone.
    
    Check the current day of week against the day patterns in the zone schedule and if they match,
    apply scheduled temperature changes for the day.
    """
    if zone_schedule.get(DayPattern.ALL):
        _apply_day_zone_temps(zone, zone_schedule[DayPattern.ALL], vacation_mode_temp, now)
    if now.weekday() < 5 and zone_schedule.get(DayPattern.WEEKDAYS):
        # Today is a weekday and there's a weekday schedule.
        _apply_day_zone_temps(zone, zone_schedule[DayPattern.WEEKDAYS], vacation_mode_temp, now)
    elif now.weekday() >= 5 and zone_schedule.get(DayPattern.WEEKENDS):
        # Today's a weekend and there's a weekend schedule.
        _apply_day_zone_temps(zone, zone_schedule[DayPattern.WEEKENDS], vacation_mode_temp, now)


@service
def climate_updates():
    """Update thermostats if necessary based on schedule."""
    now = datetime.now()
    for zone in list(Zone):
        hvac_mode = state.get(ZONE_ENTITIES[zone][0])
        if hvac_mode == "heat":
            _apply_zone_temps(zone, HEAT_SCHEDULE[zone], VACATION_HEAT_TEMP, now)
        elif hvac_mode == "cool":
            log,error("Cooling schedule not defined yet.")

