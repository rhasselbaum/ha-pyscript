@service
def front_door_alert():
    """Alert for front door motion."""

    notify.ephemeral_notifications_group(title="Front door motion", message="There is motion at the front door.")
    pyscript.flash_lights(
        entity_ids=["light.tree_lamp_left", "light.table_north", "light.office_fan_ne"],
        rgb_color=[255, 50, 0]
    )

@service
def back_yard_alert():
    """Alert for back yard motion."""

    notify.ephemeral_notifications_group(title="Backyard motion", message="There is motion in the back yard.")
    pyscript.flash_lights(
        entity_ids=["light.tree_lamp_right", "light.table_south", "light.office_fan_sw"],
        rgb_color=[77, 0, 255]
    )

@service
def front_door_flood():
    """Turn on front door lights if off."""

    if light.outside == "off":
        front_lights = ["light.front_door_east", "light.front_door_west"]
        def color_lights(front_lights, rgb_color):
            for light_entity in front_lights:
                light.turn_on(entity_id=light_entity, rgb_color=rgb_color, brightness=255)
            task.sleep(2)
        color_lights(front_lights, [255, 253, 253])
        color_lights(front_lights, [255, 45, 43])
        color_lights(front_lights, [255, 253, 253])

@service
def front_door_end_flood():
    """Turn off front door lights if flooding."""

    if light.outside == "on" and light.outside.rgb_color == (255, 252, 252):
        light.turn_off(entity_id="light.outside")
