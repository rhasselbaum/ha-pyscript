"""Schedule-helper HVAC control.

Controls mini-splits in three logical zones: downstairs, upstairs bedrooms, and
the master bedroom. Home Assistant Schedule helpers supply heating/cooling
temperatures and on/off periods. The HVAC dropdown selects manual control,
automatic heating/cooling for all zones, or automatic heating downstairs only.
Human changes temporarily override automation until the next scheduled range
or global mode change; periodic reconciliation recovers missed device updates.
Vacation mode substitutes fixed temperatures, oil heat adds a small target boost,
and an overheating guard can pause upstairs heating. See docs/climate_v2.md for
the detailed behavior, configuration, and automation setup.

Entry points (all called by external HA automations, not internal triggers):
    climate_v2_schedule_changed: Apply the current range after a helper change.
    climate_v2_reconcile: Retry desired settings and check overheating every N min.
    climate_v2_record_override: Remember a user-attributed mini-split change.

Both actuator services follow the same path:
    _update -> _sync_mode -> _read_schedule/_sync_zone -> _apply_entity -> _command
The override recorder updates bookkeeping independently, so a human change can
block subsequent commands even while an actuator pass is waiting on the network.

Control precedence is: manual/unmanaged zone or human override (no commands),
scheduled off, then scheduled/vacation target plus oil boost. In full-house auto
heat, the periodic overheating guard can hold upstairs units off instead of
applying that target. Loading the module only restores bookkeeping; it does not
issue device commands.

Requires Pyscript's allow_all_imports setting. See docs/climate_v2.md for
service parameters, trigger examples, and testing/deployment precautions.
"""

from asyncio import Lock
from copy import deepcopy
from datetime import datetime
from math import isfinite

from homeassistant.core import Context


# Zone identifiers and exact labels from the global HVAC dropdown.
ZONE_UPSTAIRS = "UPSTAIRS"
ZONE_DOWNSTAIRS = "DOWNSTAIRS"
ZONE_MASTER_BEDROOM = "MASTER_BEDROOM"
MANUAL_MODE = "Manual"
FULL_COOL_MODE = "Cool - auto - all zones"
FULL_HEAT_MODE = "Heat - auto - all zones"
DOWNSTAIRS_HEAT_MODE = "Heat - auto - downstairs only"

# HA state values, device HVAC modes/actions, and entity attribute names.
STATE_ON = "on"
STATE_OFF = "off"
STATE_UNKNOWN = "unknown"
STATE_UNAVAILABLE = "unavailable"
HVAC_HEAT = "heat"
HVAC_COOL = "cool"
ACTION_IDLE = "idle"
ACTION_HEATING = "heating"
ATTR_TARGET_TEMPERATURE = "target_temperature"
ATTR_NEXT_EVENT = "next_event"
ATTR_TEMPERATURE = "temperature"
ATTR_HVAC_ACTION = "hvac_action"
ATTR_CURRENT_TEMPERATURE = "current_temperature"
SERVICE_SET_HVAC_MODE = "set_hvac_mode"

# Persisted bookkeeping keys. Keep their string values stable across reloads.
BOOK_DATA = "data"
BOOK_READY = "ready"
BOOK_MODE = "mode"
BOOK_ZONES = "zones"
BOOK_ENTITIES = "entities"
ENTRY_OVERRIDE = "override"
ENTRY_GUARD_OFF = "guard_off"
ENTRY_OIL_ACTIVE = "oil_active"

# Keys in the transient, validated schedule snapshot returned by _read_schedule.
# Keep these separate from HA state/attribute constants despite shared values:
# SCHEDULE_ON is a boolean field, not an entity state string.
SCHEDULE_SIGNATURE = "signature"
SCHEDULE_ON = "on"
SCHEDULE_TEMPERATURE = "temperature"

# Logical zones are separate from physical units. Schedules apply per zone,
# but overrides, oil indications, and guard ownership apply per physical unit.
ZONE_ENTITIES = {
    ZONE_UPSTAIRS: [
        "climate.back_bedroom_mini_split",
        "climate.front_bedroom_mini_split",
    ],
    ZONE_DOWNSTAIRS: ["climate.living_room_mini_split", "climate.office_mini_split"],
    ZONE_MASTER_BEDROOM: ["climate.master_bedroom_mini_split"],
}
# Each active schedule block supplies its target_temperature attribute.
SCHEDULES = {
    HVAC_HEAT: {
        ZONE_UPSTAIRS: "schedule.heat_schedule_upstairs",
        ZONE_DOWNSTAIRS: "schedule.heat_schedule_downstairs",
        ZONE_MASTER_BEDROOM: "schedule.heat_schedule_master_bedroom",
    },
    HVAC_COOL: {
        ZONE_UPSTAIRS: "schedule.cool_schedule_upstairs",
        ZONE_DOWNSTAIRS: "schedule.cool_schedule_downstairs",
        ZONE_MASTER_BEDROOM: "schedule.cool_schedule_master_bedroom",
    },
}
# Exact dropdown label -> (device HVAC mode, zones managed automatically).
# Zones absent from the list are left alone, just as in global Manual mode.
HVAC_MODES = {
    MANUAL_MODE: (None, []),
    FULL_COOL_MODE: (HVAC_COOL, list(ZONE_ENTITIES)),
    FULL_HEAT_MODE: (HVAC_HEAT, list(ZONE_ENTITIES)),
    DOWNSTAIRS_HEAT_MODE: (HVAC_HEAT, [ZONE_DOWNSTAIRS]),
}
MODE_ENTITY = "input_select.hvac_mode"
VACATION_ENTITY = "input_boolean.vacation_mode"
VACATION_TEMPERATURES = {HVAC_HEAT: 62, HVAC_COOL: 75}
OIL_HEAT_BOOST = 2
OVERSHOOT_DEGREES = 2
UNAVAILABLE = (None, STATE_UNKNOWN, STATE_UNAVAILABLE)
BOOK_ENTITY = "pyscript.climate_v2_bookkeeping"

# Bookkeeping schema
# ------------------
# _BOOK is a shared, mutable dict. Its persisted copy lives in the "data"
# attribute of BOOK_ENTITY; the entity's short string state is just "ready".
# Example (zone/unit entries are populated lazily, not preallocated):
#
# {
#     "mode": "Heat - auto - all zones",
#     "zones": {
#         "UPSTAIRS": [
#             "Heat - auto - all zones",       # Global dropdown label
#             "on",                           # Schedule state: on/off
#             "2026-11-15T22:00:00-05:00",     # Next boundary, ISO string/None
#             68.0,                           # Schedule target in degrees F/None
#         ],
#     },
#     "entities": {
#         "climate.back_bedroom_mini_split": {
#             "override": True,
#             "oil_active": False,
#         },
#         "climate.master_bedroom_mini_split": {
#             "guard_off": True,
#             "oil_active": True,
#         },
#     },
# }
#
# Field meanings and lifetimes:
# - mode: last valid global mode observed, not necessarily the current HA state.
#   An observed mode change clears both zone and unit records, including oil.
# - zones[zone]: last observed range signature, NOT proof settings were applied.
#   Comparing it with the current helper detects missed/adjacent transitions.
#   A changed known signature clears that zone's overrides and guard flags.
# - entities[id].override: True means a human owns all settings until the next
#   known range/global-mode change. Availability transitions do not clear it.
# - entities[id].guard_off: True means the guard has requested a shutdown; the
#   command may still need retrying. Retained until a successful release, a new
#   range/mode, a scheduled-off pass, or a human override. It is not the unit's
#   actual power state, which must always be read from HA.
# - entities[id].oil_active: last usable heat-mode action (idle=True,
#   heating=False). Retained across scheduled/guard shutdowns and range changes,
#   because powered-off units may stop reporting their target and hvac_action.
#
# Missing boolean flags mean False; missing records mean not yet observed.
# Range signatures are lists (not tuples) and dates are strings so restoring
# JSON-safe attributes does not change their equality. No live HA objects are
# stored. _save_book snapshots mutations; state.persist restores across reloads
# and normal HA restarts. Neither stores a history of every device event.
state.persist(BOOK_ENTITY, default_value=BOOK_READY, default_attributes={BOOK_DATA: {}})
_BOOK = deepcopy((state.getattr(BOOK_ENTITY) or {}).get(BOOK_DATA, {}))
_BOOK.setdefault(BOOK_ZONES, {})
_BOOK.setdefault(BOOK_ENTITIES, {})
# Only actuator passes take this lock; the recorder must remain responsive.
_UPDATE_LOCK = Lock()


def _save_book():
    """Publish a detached bookkeeping snapshot for HA/Pyscript persistence.

    Deep-copying prevents later in-memory mutations from silently changing the
    attributes of an already published HA state without a new state.set call.
    """
    state.set(BOOK_ENTITY, BOOK_READY, new_attributes={BOOK_DATA: deepcopy(_BOOK)})


def _get(entity_id):
    """Read an entity's state, returning None if the entity does not exist.

    Existing unknown/unavailable states remain strings for callers to handle.
    """
    return state.get(entity_id) if state.exist(entity_id) else None


def _number(value):
    """Return a finite float, or None for absent/invalid/NaN/infinite values."""
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _zone_for(entity_id):
    """Return a configured mini-split's logical zone, or None if unmanaged."""
    for zone, entities in ZONE_ENTITIES.items():
        if entity_id in entities:
            return zone
    return None


def _entity_book(entity_id):
    """Get/create a unit's mutable record; callers persist any changes.

    The returned dict is part of shared _BOOK, not a copy or an HA snapshot.
    """
    return _BOOK[BOOK_ENTITIES].setdefault(entity_id, {})


def _sync_mode():
    """Observe the global dropdown and reset bookkeeping when its mode changes.

    Returns (mode_label, changed), or (None, False) for an invalid/unavailable
    helper. Invalid values leave prior bookkeeping intact. A first valid mode
    also counts as changed. Entering Manual updates bookkeeping, but the caller
    must stop before issuing device commands.
    """
    mode = _get(MODE_ENTITY)
    if mode not in HVAC_MODES:
        log.warning(f"Climate v2: unknown HVAC mode {mode!r}; leaving units alone.")
        return None, False
    changed = _BOOK.get(BOOK_MODE) != mode
    if changed:
        _BOOK[BOOK_MODE] = str(mode)
        _BOOK[BOOK_ZONES] = {}
        _BOOK[BOOK_ENTITIES] = {}
        _save_book()
    return str(mode), changed


def _read_schedule(zone, mode):
    """Read/validate one managed zone's current schedule, without side effects.

    Args:
        zone: Logical zone name from ZONE_ENTITIES.
        mode: Valid, non-Manual dropdown label selecting heat or cool schedules.

    Returns:
        {"signature": [mode, state, next_event_iso, target],
         "on": bool, "temperature": float_or_none}, or None if unusable.
        An on range requires a finite target; an off range does not.

    The signature is range identity, not a device-command cache. Its dated
    next_event distinguishes adjacent on ranges (even at the same temperature)
    and later days/weeks. Vacation and oil adjustments deliberately stay out of
    it: those change desired temperatures, but must not expire human overrides.
    """
    hvac_mode = HVAC_MODES[mode][0]
    entity_id = SCHEDULES[hvac_mode][zone]
    schedule_state = _get(entity_id)
    attrs = state.getattr(entity_id) or {}
    if schedule_state not in (STATE_ON, STATE_OFF):
        log.warning(f"Climate v2: {entity_id} has no usable state; skipping.")
        return None
    temperature = _number(attrs.get(ATTR_TARGET_TEMPERATURE))
    if schedule_state == STATE_ON and temperature is None:
        log.warning(f"Climate v2: {entity_id} has no valid target_temperature; skipping.")
        return None
    # Store only JSON-safe primitives and normalize restored ISO strings.
    # A completely empty (always off) helper can legitimately have no next event.
    next_event = attrs.get(ATTR_NEXT_EVENT)
    if next_event is not None:
        try:
            next_event = datetime.fromisoformat(str(next_event)).isoformat()
        except ValueError:
            log.warning(f"Climate v2: invalid next_event on {entity_id}; skipping.")
            return None
    signature = [mode, str(schedule_state), next_event, temperature]
    return {
        SCHEDULE_SIGNATURE: signature,
        SCHEDULE_ON: schedule_state == STATE_ON,
        SCHEDULE_TEMPERATURE: temperature,
    }


def _sync_zone(zone, schedule):
    """Record range identity and expire zone-local flags on a known transition.

    schedule is the validated snapshot from _read_schedule. An unchanged range
    is a no-op, so duplicate automation calls do not erase human overrides.
    The first observed range only establishes a baseline: an override recorded
    before any schedule was readable must survive until a later known change.
    Oil indications survive all range changes; actual device state is unchanged.
    """
    signature = schedule[SCHEDULE_SIGNATURE]
    previous = _BOOK[BOOK_ZONES].get(zone)
    if previous != signature:
        _BOOK[BOOK_ZONES][zone] = signature
        for entity_id in ZONE_ENTITIES[zone]:
            # Retain the last oil indication while a unit is off. Some units
            # report neither a target nor hvac_action while powered off.
            entry = _entity_book(entity_id)
            if previous is not None:
                entry.pop(ENTRY_OVERRIDE, None)
                entry.pop(ENTRY_GUARD_OFF, None)
        _save_book()


def _allowed(entity_id, zone, mode, signature):
    """Return whether a command based on this mode/range snapshot is still valid.

    Called immediately before each command, since an earlier network wait may
    have allowed a user override, dropdown change, or schedule boundary to occur.
    This check cannot recall a command that has already been sent.
    """
    if _get(MODE_ENTITY) != mode or _entity_book(entity_id).get(ENTRY_OVERRIDE):
        return False
    current = _read_schedule(zone, mode)
    return current is not None and current[SCHEDULE_SIGNATURE] == signature


async def _command(entity_id, zone, mode, signature, action, **kwargs):
    """Send one still-valid climate action with a non-user HA context.

    action is the climate service name; kwargs are its action-specific fields.
    Returns False if the snapshot is stale/overridden, True if the blocking
    service call completed. Completion is not proof the physical unit changed;
    reconciliation always checks reported state again. Exceptions propagate to
    _update's per-unit handler so other units can continue and this one retries.
    """
    if not _allowed(entity_id, zone, mode, signature):
        return False
    # Do not inherit the caller's user_id (e.g. a user changing the dropdown).
    # Otherwise our own resulting state events could look like human overrides.
    await service.call(
        "climate", action, entity_id=entity_id, blocking=True, context=Context(), **kwargs
    )
    log.info(f"Climate v2: {entity_id}: {action} {kwargs}.")
    return True


async def _apply_entity(entity_id, zone, mode, schedule, check_guard):
    """Reconcile one physical unit against a validated zone schedule snapshot.

    mode is the global dropdown label, not the unit's current HVAC state.
    check_guard is True only for periodic passes. Decision order:
        1. Protect human overrides and skip unavailable units.
        2. Enforce a scheduled off range, regardless of vacation/oil settings.
        3. Calculate the scheduled/vacation base plus the current oil boost.
        4. On periodic passes, evaluate upstairs overheating/guard release.
        5. Hold an existing guard shutdown, or apply any differing mode/target.

    A schedule-change pass does not evaluate temperatures for overheating, but
    respects an existing guard unless _sync_zone/_sync_mode already reset it.
    Bookkeeping is intent/ownership; reported HA state determines command retries.
    """
    # 1. An override protects power and mode as well as target temperature.
    entry = _entity_book(entity_id)
    if entry.get(ENTRY_OVERRIDE) or _get(entity_id) in UNAVAILABLE:
        return
    signature = schedule[SCHEDULE_SIGNATURE]
    current_mode = _get(entity_id)
    attrs = state.getattr(entity_id) or {}
    # 2. Scheduled off takes precedence over vacation temperature and the guard.
    if not schedule[SCHEDULE_ON]:
        entry.pop(ENTRY_GUARD_OFF, None)
        _save_book()
        if current_mode != STATE_OFF:
            await _command(
                entity_id, zone, mode, signature, SERVICE_SET_HVAC_MODE,
                hvac_mode=STATE_OFF,
            )
        return

    # 3. Recompute an absolute target; never add oil boost to the previous target.
    hvac_mode = HVAC_MODES[mode][0]
    vacation = _get(VACATION_ENTITY)
    if vacation not in (STATE_ON, STATE_OFF):
        log.warning(f"Climate v2: vacation state {vacation!r} is invalid; skipping {entity_id}.")
        return
    target = (
        VACATION_TEMPERATURES[hvac_mode]
        if vacation == STATE_ON
        else schedule[SCHEDULE_TEMPERATURE]
    )
    if hvac_mode == HVAC_HEAT:
        # 'idle' is the existing installation's only oil-active signal. Do not
        # infer oil from off/cooling/unknown action, especially guard shutdowns.
        action = attrs.get(ATTR_HVAC_ACTION)
        if current_mode == HVAC_HEAT and action in (ACTION_IDLE, ACTION_HEATING):
            entry[ENTRY_OIL_ACTIVE] = action == ACTION_IDLE
        if entry.get(ENTRY_OIL_ACTIVE):
            target += OIL_HEAT_BOOST

    # 4. The guard compares actual temperature with the effective (boosted)
    # target. Trip strictly above target + 2; release strictly below target.
    # Between those thresholds (including equality), keep the prior guard state
    # to avoid cycling as the room temperature fluctuates near the trip point.
    guard_enabled = mode == FULL_HEAT_MODE and zone != ZONE_DOWNSTAIRS
    # Release is provisional until the restart succeeds (or state is already
    # correct). Clearing ownership earlier would lose it on a network failure.
    release_guard = False
    if check_guard and guard_enabled:
        actual = _number(attrs.get(ATTR_CURRENT_TEMPERATURE))
        if actual is not None:
            if actual > target + OVERSHOOT_DEGREES:
                # Save ownership before the network call: failed shutdowns retry.
                entry[ENTRY_GUARD_OFF] = True
            elif actual < target:
                release_guard = bool(entry.get(ENTRY_GUARD_OFF))
        elif entry.get(ENTRY_GUARD_OFF):
            # Never restart a guard-stopped unit without a valid reading.
            _save_book()
            return
    # 5a. A duplicate helper event must not wake a still-guarded unit between
    # periodic checks. Retry a failed shutdown if HA still reports it powered on.
    if guard_enabled and entry.get(ENTRY_GUARD_OFF) and not release_guard:
        _save_book()
        if current_mode != STATE_OFF:
            await _command(
                entity_id, zone, mode, signature, SERVICE_SET_HVAC_MODE,
                hvac_mode=STATE_OFF,
            )
        return

    # 5b. Set mode and target together, which also starts a scheduled-on unit.
    # Already-correct units need no command, including during guard release.
    _save_book()
    applied = True
    if current_mode != hvac_mode or _number(attrs.get(ATTR_TEMPERATURE)) != target:
        applied = await _command(
            entity_id, zone, mode, signature, "set_temperature",
            hvac_mode=hvac_mode, temperature=target,
        )
    if release_guard and applied:
        # Retain ownership if the restart fails; a later missing sensor reading
        # must not accidentally permit an unguarded restart.
        entry.pop(ENTRY_GUARD_OFF, None)
        _save_book()


async def _update(entity_id=None, check_guard=False):
    """Run the shared actuator pass, selecting managed zones and isolating errors.

    Args:
        entity_id: Changed schedule/helper, or None to inspect all managed zones.
            A schedule limits the pass to its matching active zone; dropdown or
            vacation events select all. An observed global mode change overrides
            that filter so every newly managed zone gets its current settings.
        check_guard: Whether this pass may evaluate overheating/guard release.

    Only this function takes the actuator lock. It synchronizes range identity
    even when units are offline/overridden, so a missed boundary can still expire
    overrides. Device failures are handled per unit, not as a house-wide failure.
    """
    # Serialize actuator passes, but not the override recorder: it must be able
    # to mark a unit while a blocking device command is awaiting the network.
    async with _UPDATE_LOCK:
        mode, mode_changed = _sync_mode()
        if mode is None or mode == MANUAL_MODE:
            return
        hvac_mode, zones = HVAC_MODES[mode]
        for zone in zones:
            schedule_entity = SCHEDULES[hvac_mode][zone]
            if not mode_changed and entity_id not in (None, MODE_ENTITY, VACATION_ENTITY, schedule_entity):
                continue
            if _get(MODE_ENTITY) != mode:
                return
            schedule = _read_schedule(zone, mode)
            if schedule is None:
                continue
            _sync_zone(zone, schedule)
            for unit in ZONE_ENTITIES[zone]:
                try:
                    await _apply_entity(unit, zone, mode, schedule, check_guard)
                except Exception as exc:
                    # One offline/failing unit must not prevent the rest of the
                    # house from updating. Next interval retries actual state.
                    log.warning(f"Climate v2: update failed for {unit}: {exc}")


@service
async def climate_v2_schedule_changed(entity_id=None):
    """Apply a schedule/dropdown/vacation change; entity_id limits its scope.

    Omit entity_id to inspect all active schedules. Duplicate calls do not
    expire overrides unless the active range or global HVAC mode changed.

    Triggers must include schedule attribute changes, not just on/off edges.
    A vacation-helper change reapplies targets but does not clear overrides.
    This service does not evaluate overheating; use climate_v2_reconcile for it.
    """
    await _update(entity_id=entity_id)


@service
async def climate_v2_reconcile():
    """Reconcile all managed zones and check upstairs overheating every N min.

    No arguments. External automation supplies the interval. This pass also
    catches missed boundaries/failed commands without overriding a human's
    settings in the same range. Manual/unmanaged zones receive no commands.
    """
    await _update(check_guard=True)


@service
def climate_v2_record_override(
    entity_id, old_state, new_state, user_id=None,
    old_temperature=None, new_temperature=None,
):
    """Record a human climate state change from an HA state-trigger payload.

    Pass user_id from trigger.to_state.context.user_id, NOT the automation's
    context. old_state/new_state are HVAC state strings; temperatures are the
    old/new target-temperature attributes. Availability/telemetry changes and
    events without a user_id are ignored. See docs/climate_v2.md.

    Any nonempty HA user ID qualifies. A mode/power change counts independently
    of temperature; a temperature-only change requires two valid numeric targets
    so absent/reappearing attributes do not create overrides. The current room
    temperature and hvac_action are intentionally irrelevant here.

    This service never commands devices or takes the actuator lock. It marks
    only the changed unit, canceling its guard ownership so the human also owns
    power control. The flag expires at the next known zone range/global mode
    change. With an unavailable schedule, preserve the override until range
    tracking can resume; _sync_zone handles establishing an initial baseline.
    """
    zone = _zone_for(entity_id)
    if zone is None or not user_id or old_state in UNAVAILABLE or new_state in UNAVAILABLE:
        return
    temperature_changed = (
        _number(old_temperature) is not None
        and _number(new_temperature) is not None
        and _number(old_temperature) != _number(new_temperature)
    )
    if old_state == new_state and not temperature_changed:
        return
    mode, _ = _sync_mode()
    if mode is None or zone not in HVAC_MODES[mode][1]:
        return
    schedule = _read_schedule(zone, mode)
    if schedule is None:
        # Still protect the unit while its schedule is temporarily unavailable.
        # The next valid range comparison will resolve the expiration.
        entry = _entity_book(entity_id)
        entry[ENTRY_OVERRIDE] = True
        entry.pop(ENTRY_GUARD_OFF, None)
        _save_book()
        return
    # Observe any already-crossed boundary before marking this new human action;
    # the new override belongs to the current range, not the previous one.
    _sync_zone(zone, schedule)
    entry = _entity_book(entity_id)
    entry[ENTRY_OVERRIDE] = True
    entry.pop(ENTRY_GUARD_OFF, None)
    _save_book()
    log.info(f"Climate v2: human override on {entity_id}, until next scheduled range/mode change.")
