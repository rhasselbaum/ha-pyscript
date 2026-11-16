"""Offline behavioral tests: python -m unittest discover -s tests -v.

Executes the module with Pyscript/HA service and state doubles. These do not
replace an on-device Pyscript/context-propagation smoke test.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "climate_v2.py"
UPSTAIRS = "climate.back_bedroom_mini_split"
FRONT = "climate.front_bedroom_mini_split"
MASTER = "climate.master_bedroom_mini_split"
DOWNSTAIRS = "climate.living_room_mini_split"
OFFICE = "climate.office_mini_split"
UNITS = [UPSTAIRS, FRONT, MASTER, DOWNSTAIRS, OFFICE]
HEAT = "Heat - auto - all zones"
COOL = "Cool - auto - all zones"
NEXT_EVENT = datetime(2026, 11, 14, 22, tzinfo=timezone.utc)


class FakeContext:
    def __init__(self):
        self.user_id = None


class FakeState:
    def __init__(self):
        self.values = {}
        self.attributes = {}

    def exist(self, entity_id):
        return entity_id in self.values

    def get(self, entity_id):
        return self.values[entity_id]

    def getattr(self, entity_id):
        return deepcopy(self.attributes.get(entity_id))

    def set(self, entity_id, value, new_attributes=None):
        self.values[entity_id] = value
        if new_attributes is not None:
            self.attributes[entity_id] = deepcopy(new_attributes)

    def persist(self, entity_id, default_value=None, default_attributes=None):
        if entity_id not in self.values:
            self.set(entity_id, default_value, default_attributes)


class FakeServices:
    def __init__(self, state):
        self.state = state
        self.calls = []
        self.fail = set()
        self.callback = None

    async def call(self, domain, action, **kwargs):
        self.calls.append((domain, action, kwargs))
        entity_id = kwargs["entity_id"]
        if self.callback:
            self.callback(entity_id)
        if entity_id in self.fail:
            raise ConnectionError("offline")
        if "temperature" in kwargs:
            self.state.attributes[entity_id]["temperature"] = kwargs["temperature"]
        if "hvac_mode" in kwargs:
            self.state.values[entity_id] = kwargs["hvac_mode"]


class ClimateV2Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = FakeState()
        self.services = FakeServices(self.state)
        self.load_module()
        self.state.set("input_select.hvac_mode", HEAT)
        self.state.set("input_boolean.vacation_mode", "off")
        for entity_id in UNITS:
            self.state.set(entity_id, "heat", {
                "temperature": 68, "current_temperature": 68, "hvac_action": "heating",
            })
        for schedules in self.module["SCHEDULES"].values():
            for entity_id in schedules.values():
                self.state.set(entity_id, "on", {
                    "target_temperature": 68, "next_event": NEXT_EVENT,
                })

    def load_module(self):
        core = ModuleType("homeassistant.core")
        core.Context = FakeContext
        with patch.dict(sys.modules, {"homeassistant": ModuleType("homeassistant"), "homeassistant.core": core}):
            self.module = {
                "state": self.state,
                "service": self.service_decorator(),
                "log": logging.getLogger("climate_v2_test"),
            }
            exec(compile(MODULE.read_text(), str(MODULE), "exec"), self.module)

    def service_decorator(self):
        class Service:
            def __call__(self, function):
                return function

        service = Service()
        service.call = self.services.call
        return service

    async def reconcile(self):
        await self.module["climate_v2_reconcile"]()

    async def changed(self, entity_id=None):
        await self.module["climate_v2_schedule_changed"](entity_id)

    def override(self, entity_id=UPSTAIRS, old_state="heat", new_state="heat", user_id="any-user", old=68, new=71):
        self.module["climate_v2_record_override"](
            entity_id, old_state, new_state, user_id, old, new,
        )

    def advance(self, zone="UPSTAIRS", hvac_mode="heat", temperature=None):
        schedule_id = self.module["SCHEDULES"][hvac_mode][zone]
        attrs = self.state.attributes[schedule_id]
        attrs["next_event"] += timedelta(hours=8)
        if temperature is not None:
            attrs["target_temperature"] = temperature
        return schedule_id

    def entry(self, entity_id=UPSTAIRS):
        return self.module["_entity_book"](entity_id)

    def calls_for(self, entity_id):
        return [kwargs for _, _, kwargs in self.services.calls if kwargs["entity_id"] == entity_id]

    async def test_loading_module_does_not_change_devices(self):
        self.assertEqual(self.services.calls, [])

    async def test_manual_means_no_commands_from_any_service(self):
        self.state.values["input_select.hvac_mode"] = "Manual"
        self.state.attributes[UPSTAIRS]["current_temperature"] = 90
        await self.changed()
        self.override()
        await self.reconcile()
        self.assertEqual(self.services.calls, [])

    async def test_downstairs_only_leaves_all_upstairs_units_alone(self):
        self.state.values["input_select.hvac_mode"] = "Heat - auto - downstairs only"
        for entity_id in UNITS:
            self.state.values[entity_id] = "off"
        await self.reconcile()
        self.assertEqual({call[2]["entity_id"] for call in self.services.calls}, {DOWNSTAIRS, OFFICE})

    async def test_auto_cool_sets_power_mode_and_temperature(self):
        self.state.values["input_select.hvac_mode"] = COOL
        self.state.values[UPSTAIRS] = "off"
        self.state.attributes["schedule.cool_schedule_upstairs"]["target_temperature"] = 73
        await self.changed()
        self.assertEqual(self.state.get(UPSTAIRS), "cool")
        self.assertEqual(self.state.attributes[UPSTAIRS]["temperature"], 73)
        self.assertIsNone(self.calls_for(UPSTAIRS)[0]["context"].user_id)

    async def test_repeated_reconcile_does_not_reissue_correct_settings(self):
        await self.reconcile()
        await self.reconcile()
        self.assertEqual(self.services.calls, [])

    async def test_human_override_survives_polling_and_duplicate_schedule_calls(self):
        await self.reconcile()
        self.state.attributes[UPSTAIRS]["temperature"] = 71
        self.override()
        await self.changed("schedule.heat_schedule_upstairs")
        await self.reconcile()
        self.assertEqual(self.calls_for(UPSTAIRS), [])
        self.assertEqual(self.state.attributes[UPSTAIRS]["temperature"], 71)

    async def test_on_to_on_boundary_expires_only_affected_zone_override(self):
        await self.reconcile()
        self.override()
        self.override(MASTER)
        self.state.attributes[UPSTAIRS]["temperature"] = 71
        schedule_id = self.advance(temperature=66)
        await self.changed(schedule_id)
        self.assertFalse(self.entry().get("override"))
        self.assertTrue(self.entry(MASTER)["override"])
        self.assertEqual(self.state.attributes[UPSTAIRS]["temperature"], 66)

    async def test_periodic_detects_missed_boundary_even_with_same_target(self):
        await self.reconcile()
        self.override()
        self.state.attributes[UPSTAIRS]["temperature"] = 71
        self.advance()
        await self.reconcile()
        self.assertEqual(self.state.attributes[UPSTAIRS]["temperature"], 68)

    async def test_mode_change_expires_override(self):
        await self.reconcile()
        self.override()
        self.state.values["input_select.hvac_mode"] = COOL
        await self.changed("input_select.hvac_mode")
        self.assertEqual(self.state.get(UPSTAIRS), "cool")
        self.assertFalse(self.entry().get("override"))

    async def test_off_and_mode_changes_are_human_overrides(self):
        await self.reconcile()
        for new_mode in ("off", "cool"):
            self.state.values[UPSTAIRS] = new_mode
            self.override(old_state="heat", new_state=new_mode, old=None, new=None)
            await self.reconcile()
            self.assertEqual(self.state.get(UPSTAIRS), new_mode)
        self.assertEqual(self.calls_for(UPSTAIRS), [])

    async def test_unknown_unavailable_and_telemetry_do_not_create_override(self):
        await self.reconcile()
        for old, new in (("heat", "unavailable"), ("unavailable", "heat"),
                         ("heat", "unknown"), (None, "heat")):
            self.override(old_state=old, new_state=new)
        self.override(old=68, new=68)
        self.override(user_id=None)
        self.assertFalse(self.entry().get("override"))

    async def test_outage_does_not_erase_override(self):
        await self.reconcile()
        self.override()
        self.state.values[UPSTAIRS] = "unavailable"
        await self.reconcile()
        self.override(old_state="unavailable", new_state="heat")
        self.state.values[UPSTAIRS] = "heat"
        self.state.attributes[UPSTAIRS]["temperature"] = 71
        await self.reconcile()
        self.assertTrue(self.entry()["override"])
        self.assertEqual(self.calls_for(UPSTAIRS), [])

    async def test_override_is_per_unit_not_zone(self):
        await self.reconcile()
        self.override()
        self.state.attributes[FRONT]["temperature"] = 60
        await self.reconcile()
        self.assertEqual(self.calls_for(UPSTAIRS), [])
        self.assertEqual(self.state.attributes[FRONT]["temperature"], 68)

    async def test_oil_boost_is_per_unit_non_cumulative_and_removed(self):
        self.state.attributes[DOWNSTAIRS]["hvac_action"] = "idle"
        await self.reconcile()
        await self.reconcile()
        self.assertEqual(self.state.attributes[DOWNSTAIRS]["temperature"], 70)
        self.assertEqual(len(self.calls_for(DOWNSTAIRS)), 1)
        self.assertEqual(self.state.attributes[OFFICE]["temperature"], 68)
        self.state.attributes[DOWNSTAIRS]["hvac_action"] = "heating"
        await self.reconcile()
        self.assertEqual(self.state.attributes[DOWNSTAIRS]["temperature"], 68)

    async def test_guard_shuts_down_upstairs_and_master_but_not_downstairs(self):
        for entity_id in UNITS:
            self.state.attributes[entity_id]["current_temperature"] = 71
        await self.reconcile()
        for entity_id in (UPSTAIRS, FRONT, MASTER):
            self.assertEqual(self.state.get(entity_id), "off")
            self.assertTrue(self.entry(entity_id)["guard_off"])
        self.assertEqual(self.state.get(DOWNSTAIRS), "heat")

    async def test_guard_only_runs_on_periodic_service_and_at_strict_threshold(self):
        self.state.attributes[UPSTAIRS]["current_temperature"] = 70
        await self.reconcile()
        self.assertEqual(self.state.get(UPSTAIRS), "heat")
        self.assertFalse(self.entry().get("guard_off"))
        self.state.attributes[UPSTAIRS]["current_temperature"] = 71
        await self.changed()
        self.assertEqual(self.state.get(UPSTAIRS), "heat")
        await self.reconcile()
        self.assertEqual(self.state.get(UPSTAIRS), "off")
        self.state.attributes[UPSTAIRS]["current_temperature"] = 67.9
        await self.changed()
        self.assertEqual(self.state.get(UPSTAIRS), "off")
        self.assertTrue(self.entry()["guard_off"])
        await self.reconcile()
        self.assertEqual(self.state.get(UPSTAIRS), "heat")
        self.assertFalse(self.entry().get("guard_off"))

    async def test_guard_hysteresis_preserves_state_between_trip_and_release_thresholds(self):
        # Target 68: trip strictly above 70, release strictly below 68.
        self.state.attributes[UPSTAIRS]["current_temperature"] = 70.1
        await self.reconcile()
        self.assertEqual(self.state.get(UPSTAIRS), "off")
        self.assertTrue(self.entry()["guard_off"])
        for actual in (70, 69, 68, 69, 70, 68):
            with self.subTest(phase="guard latched", actual=actual):
                self.state.attributes[UPSTAIRS]["current_temperature"] = actual
                await self.reconcile()
                self.assertEqual(self.state.get(UPSTAIRS), "off")
                self.assertTrue(self.entry()["guard_off"])
                self.assertEqual(len(self.calls_for(UPSTAIRS)), 1)

        self.state.attributes[UPSTAIRS]["current_temperature"] = 67.9
        await self.reconcile()
        self.assertEqual(self.state.get(UPSTAIRS), "heat")
        self.assertFalse(self.entry().get("guard_off"))
        self.assertEqual(len(self.calls_for(UPSTAIRS)), 2)
        for actual in (68, 69, 70, 69, 70):
            with self.subTest(phase="guard released", actual=actual):
                self.state.attributes[UPSTAIRS]["current_temperature"] = actual
                await self.reconcile()
                self.assertEqual(self.state.get(UPSTAIRS), "heat")
                self.assertFalse(self.entry().get("guard_off"))
                self.assertEqual(len(self.calls_for(UPSTAIRS)), 2)

        self.state.attributes[UPSTAIRS]["current_temperature"] = 70.1
        await self.reconcile()
        self.assertEqual(self.state.get(UPSTAIRS), "off")
        self.assertTrue(self.entry()["guard_off"])
        self.assertEqual(len(self.calls_for(UPSTAIRS)), 3)

    async def test_guard_retains_oil_target_when_off_attributes_disappear(self):
        self.state.attributes[UPSTAIRS].update(hvac_action="idle", current_temperature=73)
        await self.reconcile()
        # Only the actual-temperature sensor remains available while off. The
        # remembered oil boost keeps the effective target at 70, not 68.
        for actual in (72, 71, 70):
            with self.subTest(actual=actual):
                self.state.attributes[UPSTAIRS] = {"current_temperature": actual}
                await self.reconcile()
                self.assertEqual(self.state.get(UPSTAIRS), "off")
                self.assertTrue(self.entry()["guard_off"])
                self.assertTrue(self.entry()["oil_active"])
        self.state.attributes[UPSTAIRS] = {"current_temperature": 69.9}
        await self.reconcile()
        self.assertEqual(self.state.get(UPSTAIRS), "heat")
        self.assertEqual(self.state.attributes[UPSTAIRS]["temperature"], 70)
        self.assertFalse(self.entry().get("guard_off"))

    async def test_guard_uses_vacation_target_for_hysteresis(self):
        self.state.values["input_boolean.vacation_mode"] = "on"
        self.state.attributes[UPSTAIRS]["current_temperature"] = 65
        await self.reconcile()
        self.assertEqual(self.state.get(UPSTAIRS), "off")
        for actual in (64, 63, 62):
            with self.subTest(actual=actual):
                self.state.attributes[UPSTAIRS]["current_temperature"] = actual
                await self.reconcile()
                self.assertEqual(self.state.get(UPSTAIRS), "off")
                self.assertTrue(self.entry()["guard_off"])
        self.state.attributes[UPSTAIRS]["current_temperature"] = 61.9
        await self.reconcile()
        self.assertEqual(self.state.get(UPSTAIRS), "heat")
        self.assertEqual(self.state.attributes[UPSTAIRS]["temperature"], 62)
        self.assertFalse(self.entry().get("guard_off"))

    async def test_guard_holds_off_if_actual_temperature_is_unknown(self):
        self.state.attributes[UPSTAIRS]["current_temperature"] = 71
        await self.reconcile()
        self.state.attributes[UPSTAIRS]["current_temperature"] = None
        await self.reconcile()
        self.assertEqual(self.state.get(UPSTAIRS), "off")

    async def test_human_override_suspends_guard(self):
        await self.reconcile()
        self.override()
        self.state.attributes[UPSTAIRS]["current_temperature"] = 90
        await self.reconcile()
        self.assertEqual(self.state.get(UPSTAIRS), "heat")
        self.assertEqual(self.calls_for(UPSTAIRS), [])

    async def test_guard_is_disabled_in_cool_mode(self):
        self.state.values["input_select.hvac_mode"] = COOL
        self.state.attributes[UPSTAIRS]["current_temperature"] = 90
        await self.reconcile()
        self.assertEqual(self.state.get(UPSTAIRS), "cool")
        self.assertFalse(self.entry().get("guard_off"))

    async def test_vacation_temperatures_boost_and_scheduled_off(self):
        self.state.values["input_boolean.vacation_mode"] = "on"
        self.state.attributes[DOWNSTAIRS]["hvac_action"] = "idle"
        for entity_id in UNITS:
            self.state.attributes[entity_id]["current_temperature"] = 60
        await self.reconcile()
        self.assertEqual(self.state.attributes[DOWNSTAIRS]["temperature"], 64)
        self.assertEqual(self.state.attributes[MASTER]["temperature"], 62)
        self.state.values["input_select.hvac_mode"] = COOL
        self.state.values["schedule.cool_schedule_downstairs"] = "off"
        await self.changed()
        self.assertEqual(self.state.attributes[MASTER]["temperature"], 75)
        self.assertEqual(self.state.get(DOWNSTAIRS), "off")

    async def test_off_schedule_honors_override_until_next_boundary(self):
        self.state.values["schedule.heat_schedule_upstairs"] = "off"
        await self.reconcile()
        self.state.values[UPSTAIRS] = "heat"
        self.override(old_state="off", new_state="heat")
        await self.reconcile()
        self.assertEqual(self.state.get(UPSTAIRS), "heat")
        self.advance()
        await self.reconcile()
        self.assertEqual(self.state.get(UPSTAIRS), "off")

    async def test_network_failure_isolated_and_retried(self):
        self.state.values["input_select.hvac_mode"] = COOL
        self.services.fail.add(UPSTAIRS)
        with self.assertLogs("climate_v2_test", level="WARNING"):
            await self.reconcile()
        self.assertEqual(self.state.get(FRONT), "cool")
        self.services.fail.clear()
        await self.reconcile()
        self.assertEqual(self.state.get(UPSTAIRS), "cool")

    async def test_guard_shutdown_failure_is_retried(self):
        self.state.attributes[UPSTAIRS]["current_temperature"] = 71
        self.services.fail.add(UPSTAIRS)
        with self.assertLogs("climate_v2_test", level="WARNING"):
            await self.reconcile()
        self.assertTrue(self.entry()["guard_off"])
        self.services.fail.clear()
        await self.reconcile()
        self.assertEqual(self.state.get(UPSTAIRS), "off")

    async def test_failed_guard_restart_keeps_ownership_and_unknown_reading_holds_off(self):
        self.state.attributes[UPSTAIRS]["current_temperature"] = 71
        await self.reconcile()
        self.state.attributes[UPSTAIRS]["current_temperature"] = 67.9
        self.services.fail.add(UPSTAIRS)
        with self.assertLogs("climate_v2_test", level="WARNING"):
            await self.reconcile()
        self.assertTrue(self.entry()["guard_off"])
        self.state.attributes[UPSTAIRS]["current_temperature"] = None
        self.services.fail.clear()
        await self.reconcile()
        self.assertEqual(self.state.get(UPSTAIRS), "off")
        self.state.attributes[UPSTAIRS]["current_temperature"] = 67.9
        await self.reconcile()
        self.assertEqual(self.state.get(UPSTAIRS), "heat")
        self.assertFalse(self.entry().get("guard_off"))

    async def test_invalid_schedule_does_not_actuate_or_erase_override(self):
        await self.reconcile()
        self.override()
        self.state.values["schedule.heat_schedule_upstairs"] = "unavailable"
        with self.assertLogs("climate_v2_test", level="WARNING"):
            await self.reconcile()
        self.assertTrue(self.entry()["override"])
        self.state.values["schedule.heat_schedule_upstairs"] = "on"
        self.state.attributes["schedule.heat_schedule_upstairs"]["target_temperature"] = "nan"
        with self.assertLogs("climate_v2_test", level="WARNING"):
            await self.reconcile()
        self.assertEqual(self.calls_for(UPSTAIRS), [])

    async def test_empty_schedule_can_turn_units_off_without_next_event(self):
        self.state.set("schedule.heat_schedule_upstairs", "off", {"next_event": None})
        await self.reconcile()
        self.assertEqual(self.state.get(UPSTAIRS), "off")

    async def test_override_and_guard_survive_reload_with_json_safe_state(self):
        self.state.attributes[MASTER]["current_temperature"] = 71
        await self.reconcile()
        self.override()
        # Cooling into the hysteresis band must not release the restored guard.
        self.state.attributes[MASTER]["current_temperature"] = 69
        book_entity = self.module["BOOK_ENTITY"]
        self.state.attributes[book_entity] = json.loads(json.dumps(self.state.attributes[book_entity]))
        self.load_module()
        self.assertTrue(self.entry()["override"])
        self.assertTrue(self.entry(MASTER)["guard_off"])
        await self.reconcile()
        self.assertTrue(self.entry()["override"])
        self.assertEqual(self.state.get(MASTER), "off")
        self.assertTrue(self.entry(MASTER)["guard_off"])

    async def test_datetime_and_restored_iso_next_event_are_same_range(self):
        await self.reconcile()
        self.override()
        self.state.attributes["schedule.heat_schedule_upstairs"]["next_event"] = NEXT_EVENT.isoformat()
        await self.reconcile()
        self.assertTrue(self.entry()["override"])

    async def test_mode_change_during_command_stops_remaining_commands(self):
        self.state.values["input_select.hvac_mode"] = COOL
        self.services.callback = lambda entity_id: self.state.values.update({"input_select.hvac_mode": "Manual"})
        await self.reconcile()
        self.assertEqual(len(self.services.calls), 1)

    async def test_override_can_be_recorded_during_actuator_pass(self):
        self.state.values["input_select.hvac_mode"] = COOL

        def during_call(entity_id):
            self.override(FRONT, old_state="heat", new_state="off")
            self.state.values[FRONT] = "off"

        self.services.callback = during_call
        await self.reconcile()
        self.assertEqual(self.calls_for(FRONT), [])
        self.assertEqual(self.state.get(FRONT), "off")


if __name__ == "__main__":
    unittest.main()
