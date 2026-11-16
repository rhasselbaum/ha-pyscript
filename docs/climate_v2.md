# Climate v2

`climate_v2.py` is a service-only controller for scheduled mini-split operation.
It does not register automatic triggers or change mini-splits when loaded.
External Home Assistant automations call its services to apply schedules,
reconcile device settings, record human overrides, and shut down all units.

Pyscript must have `allow_all_imports: true` enabled. `hass_is_global` is not
required. Temperatures and offsets assume a Fahrenheit installation.
The controller creates its own bookkeeping entity; the required schedule,
HVAC-mode, and vacation helpers are described below.

## Setup and validation

1. Load `climate_v2.py` without wiring its actuator services to automations.
2. Check the Pyscript logs and the four services in Developer Tools → Actions.
3. Wire the override recorder below, then test user-context propagation with an
   app/UI temperature change in an automatically managed zone. Inspect
   `pyscript.climate_v2_bookkeeping` for that unit's `override: true` entry.
4. Ensure no competing automation automatically writes the same mini-splits.
   Independent temperature, mode, power, or relative oil-boost adjustments can
   conflict with this controller's scheduled settings and override handling.
5. Invoke the actuator services manually for a controlled test, then enable your
   triggers when satisfied. Actuator services are live, not dry runs.

The unit tests use simulated HA/Pyscript state and services. They cannot prove
that a specific mini-split integration preserves user context, reports state
promptly, or supports the expected commands. Verify those in HA before relying
on the controller. If an integration drops `context.user_id` from user changes,
the state-based recorder cannot reliably distinguish them from device updates;
that would require a different event source, not merely an override helper.

## Configuration

The module maps five mini-splits into `UPSTAIRS`, `DOWNSTAIRS`, and
`MASTER_BEDROOM`. The overheating guard treats both `UPSTAIRS` and
`MASTER_BEDROOM` as upstairs.

| Dropdown option | Managed zones | Schedule family |
| --- | --- | --- |
| `Manual` | None | None |
| `Cool - auto - all zones` | All | Cool |
| `Heat - auto - all zones` | All | Heat |
| `Heat - auto - downstairs only` | Downstairs only | Heat |

Schedule entities:

- `schedule.cool_schedule_upstairs`
- `schedule.cool_schedule_downstairs`
- `schedule.cool_schedule_master_bedroom`
- `schedule.heat_schedule_upstairs`
- `schedule.heat_schedule_downstairs`
- `schedule.heat_schedule_master_bedroom`

Each active block must provide a numeric `target_temperature` in its additional
data. An `off` schedule means the zone should be off, not held at a fallback
temperature. Schedule `next_event`, state, target, and global mode identify the
current range. This detects adjacent `on` ranges, even with identical targets,
and detects missed transitions during periodic reconciliation. The dated next
event also distinguishes a later day/week from a previous identical range.
An empty schedule with no next event is supported as continuously off.

## Services and example automations

These examples are for you to install/adapt; the module does not install them.
The override recorder should run promptly and independently of actuator passes.
Do not put both behind a single shared `mode: single` automation.

### `pyscript.climate_v2_turn_off_all`

No arguments. Selects `Manual` on `input_select.hvac_mode` first, then turns off
all five configured mini-splits, regardless of managed zones or human overrides.
Works even when already in Manual; units already off are skipped. Calls share
the actuator lock with schedule changes and reconciliation.

If selecting Manual fails, the service stops. Individual unit failures are logged
without stopping the remaining shutdowns. Call again to retry failed/offline units;
periodic reconciliation does not retry shutdowns in Manual. Simply selecting
Manual still leaves devices alone.

```yaml
action: pyscript.climate_v2_turn_off_all
```

### `pyscript.climate_v2_schedule_changed`

Optional argument: `entity_id`, identifying the schedule or helper that changed.
Omit it to inspect all active schedules. A global mode change always inspects
all newly managed zones. Repeated calls in the same range do not clear overrides.

Watch schedule **attribute updates as well as on/off changes**. Omitting `from`
and `to` in the state trigger below includes attribute-only changes. Switching
vacation mode also applies the current targets, without expiring human overrides.

```yaml
alias: Climate v2 - schedule and mode changes
triggers:
  - trigger: state
    entity_id:
      - schedule.cool_schedule_upstairs
      - schedule.cool_schedule_downstairs
      - schedule.cool_schedule_master_bedroom
      - schedule.heat_schedule_upstairs
      - schedule.heat_schedule_downstairs
      - schedule.heat_schedule_master_bedroom
      - input_select.hvac_mode
      - input_boolean.vacation_mode
actions:
  - action: pyscript.climate_v2_schedule_changed
    data:
      entity_id: "{{ trigger.entity_id }}"
mode: queued
max: 30
```

Do not exclude `Manual` from this particular trigger if you want internal
mode bookkeeping to be reset immediately on entering Manual. The service itself
never changes devices in Manual. Running reconciliation once after HA startup
is also useful; it can alternatively wait for the first 15-minute interval.

### `pyscript.climate_v2_reconcile`

No arguments. Reconciles all currently managed zones, including schedule changes
missed by the first automation. It runs the overheating guard as part of this
pass. Calls are serialized with the schedule-change service.

```yaml
alias: Climate v2 - periodic reconciliation
triggers:
  - trigger: time_pattern
    minutes: "/15"
actions:
  - action: pyscript.climate_v2_reconcile
mode: single
```

### `pyscript.climate_v2_record_override`

Arguments:

| Argument | Value from the original climate state change |
| --- | --- |
| `entity_id` | Mini-split entity ID |
| `old_state` | Previous HVAC state string |
| `new_state` | New HVAC state string |
| `user_id` | **New climate state's** `context.user_id`, or null |
| `old_temperature` | Previous `temperature` attribute, or null |
| `new_temperature` | New `temperature` attribute, or null |

All authenticated HA users count. Do not substitute the automation's own
context/user ID. Initial entity creation/deletion, transitions to/from
`unknown`/`unavailable`, changes without a user ID, and telemetry-only changes
are ignored. Changes in actual room temperature and `hvac_action` do not count.

```yaml
alias: Climate v2 - record human overrides
triggers:
  - trigger: state
    entity_id:
      - climate.back_bedroom_mini_split
      - climate.front_bedroom_mini_split
      - climate.master_bedroom_mini_split
      - climate.living_room_mini_split
      - climate.office_mini_split
conditions:
  - condition: template
    value_template: >-
      {{ trigger.from_state is not none and trigger.to_state is not none
         and trigger.from_state.state not in ['unknown', 'unavailable']
         and trigger.to_state.state not in ['unknown', 'unavailable']
         and trigger.to_state.context.user_id is not none }}
actions:
  - action: pyscript.climate_v2_record_override
    data:
      entity_id: "{{ trigger.entity_id }}"
      old_state: "{{ trigger.from_state.state }}"
      new_state: "{{ trigger.to_state.state }}"
      user_id: "{{ trigger.to_state.context.user_id }}"
      old_temperature: "{{ trigger.from_state.attributes.get('temperature') }}"
      new_temperature: "{{ trigger.to_state.attributes.get('temperature') }}"
mode: queued
max: 100
```

An override applies to one mini-split, not its entire logical zone. It prevents
all automatic commands to that unit, including scheduled-off enforcement, oil
boost adjustments, and overheating protection. It expires at the next range of
that unit's active schedule or a global HVAC-mode change. Other zones' schedule
transitions do not expire it. An outage does not itself create/clear an override.
If a user change occurs while a schedule is unavailable, the override is kept;
an already known range is compared when the helper recovers. If no previous
range was known, the first valid range establishes a baseline and the following
range expires the override.

## Oil, vacation, and overheating behavior

- **Oil:** For each unit in heat mode, `hvac_action: idle` means oil-active;
  `heating` means not oil-active. Desired temperature is the scheduled/vacation
  base plus 2°F when oil-active, never a relative increment. Unknown/off actions
  retain the last known indication. Initial oil status is learned once the unit
  reports heat mode and a usable action; a subsequent interval corrects it.
- **Vacation:** `input_boolean.vacation_mode: on` substitutes 62°F in heat or
  75°F in cool, while respecting scheduled off periods, manual zones, and human
  overrides. Oil boost still applies to the heating vacation target.
- **Overheating:** Only periodic reconciliation in full-house auto heat checks
  upstairs units. Above effective target + 2°F, v2 marks and shuts off the unit.
  It releases the guard and restores scheduled heat only when actual temperature
  falls strictly below the effective target. Between the target and target + 2°F
  (including both boundaries), the guard retains its previous state to reduce
  cycling. For example, with a 68°F target it shuts off above 70°F and stays off
  until below 68°F.
  A missing actual-temperature reading does not release an existing guard.
  Ordinary schedule-on reconciliation can also restore a non-overridden unit
  switched off by an unrelated, non-user automation; that is separate from guard
  ownership. A new scheduled range/global mode change resets the guard so the
  newly requested settings apply immediately; the next interval checks again.
- **Failures:** Unavailable devices are skipped; a command failure is logged
  per unit without stopping other units. Desired settings are retried on the
  next interval rather than assumed successful. Missing/invalid active schedule
  temperatures or unknown global mode cause affected units to be left alone.
  An unknown vacation-helper state prevents on-period temperature commands.

Automatic commands carry a fresh HA context without a user ID. A user changing
the dropdown must not accidentally cause every resulting device update to be
recorded as a human override. An in-flight device command cannot be recalled,
but v2 rechecks the dropdown, range, and override before each subsequent command.

## Persistent bookkeeping and offline tests

`pyscript.climate_v2_bookkeeping` is created/restored automatically by
`state.persist`. Its `data` attribute stores the last global mode, per-zone range
signatures, and per-unit override/guard/oil flags. It survives script reloads
and HA restarts. Do not edit it during normal operation. Like other restored HA
state, an abrupt crash can lose changes not yet flushed to disk. It cannot
reconstruct manual actions while HA/the recorder was not running, or global
mode changes whose triggers were skipped and whose final value is unchanged.

Run offline tests from the repository root:

```sh
python -B -m unittest discover -s tests -v
```

No HA connection or third-party Python packages are needed for these tests.
