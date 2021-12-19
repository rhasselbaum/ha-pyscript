from hashlib import sha1

@service
def flash_lights(entity_ids=[], rgb_color=[255, 50, 0], count=3, delay_sec=1.25):
    """Flash lights a specific color and then restore their original states.
    
    :param entity_ids: List of light entity IDs. May omit the leading "light." prefix.
    :param rgb_color: Tuple of RGB values between 0 and 255.
    :param count: Number of times to flash.
    :param delay_sec: Number of seconds between flash transitions.
    """
    light_entities = ["light." + e if not e.startswith("light.") else e for e in entity_ids]
    # Create a scene by snapshotting the current entity states, using hash of the entities as scene name.
    snapshot_id = "snapshot_" + sha1(bytes(repr(light_entities), "utf-8")).hexdigest()
    task.unique("flash_lights_" + snapshot_id)
    scene.create(scene_id=snapshot_id, snapshot_entities=light_entities)
    # Alternate between the color and the original scene.
    for _ in range(0, count):
        for light_entity in light_entities:
            light.turn_on(entity_id=light_entity, rgb_color=rgb_color, brightness=255)
        task.sleep(delay_sec)
        scene.turn_on(entity_id="scene." + snapshot_id)
        task.sleep(delay_sec)