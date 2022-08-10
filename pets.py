
# Maps pet food container vibration sensors to todo item names and recently fed input booleans (helpers).
SENSOR_MAPPINGS = {
    "binary_sensor.cat_food_ias_zone": ("feed cats", "input_boolean.cats_recently_fed"),
    "binary_sensor.dog_food_ias_zone": ("feed dogs", "input_boolean.dogs_recently_fed"),
}


def _run_alexa_command(command):
    """Run given custom command on Kitchen Echo Show and wait 10 seconds to complete."""

    media_player.play_media(
        entity_id="media_player.kitchen_echo_show",
        media_content_type="custom", 
        media_content_id=command
    )
    task.sleep(10)


@service
def clear_pet_food_reminder(entity_id):
    """Remove "feed pet" reminder from Alexa to-do list and set the recently fed boolean if it was unset.

    If the recently fed boolean was already set, do nothing.

    :param entity_id: Entity ID of the sensor that was activated (from SENSOR_MAPPINGS).
    """
    if entity_id not in SENSOR_MAPPINGS:
        raise ValueError(f"Not a pet food sensor: {entity_id}")
    todo_name, recently_fed_input_boolean = SENSOR_MAPPINGS[entity_id]
    if state.get(recently_fed_input_boolean) == "off":
        _run_alexa_command(f"remove {todo_name} to my todo list")
        state.set(recently_fed_input_boolean, "on")
    else:
        task.sleep(5)


@service
def set_pet_food_reminders():
    """Add "feed pets" to Alexa to-do list and clear the recently fed booleans."""

    _run_alexa_command("set volume 0")
    for todo_name, recently_fed_input_boolean in SENSOR_MAPPINGS.values():
        _run_alexa_command(f"add {todo_name} to my todo list")
        state.set(recently_fed_input_boolean, "off")
    _run_alexa_command("set volume 4")