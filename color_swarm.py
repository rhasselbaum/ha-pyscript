"""
A collection of lighting effects that runs asynchronously on Philips Hue rooms/groups.
Pyscript must be configured to expose the "hass" global variable and allow all imports
so that we can access the Hue bridge configs and entity registry.
"""
import aiohttp
from aiohue.bridge import Bridge
from homeassistant.helpers import entity_registry as er
import time
import heapq
import random


# Swarm definitions. Add your own here. To favor a particular color, add multiple instances of it to the palette.
# Max hold is the maximum number of seconds a bulb will hold its setting before transitioning to a new random color.
# The other attributes are self-explanatory, I hope.
swarms = {
    "Christmas": {
        "transition_secs": 10,
        "max_hold_secs": 60,
        "palette": [
            {
                "rgb_color": (255, 0, 0),
                "brightness": 100,
            },
            {
                "rgb_color": (0, 255, 0),
                "brightness": 100,                
            }
        ],
    },
    "Bright Christmas": {
        "transition_secs": 1,
        "max_hold_secs": 5,
        "palette": [
            {
                "rgb_color": (255, 13, 24),
                "brightness": 240,
            },
            {
                "rgb_color": (255, 0, 0),
                "brightness": 255,
            },
            {
                "rgb_color": (0, 255, 0),
                "brightness": 255,
            },
            {
                "rgb_color": (21, 255, 13),
                "brightness": 240,
            },
        ],
    },
    "Casino": {
        "transition_secs": 10,
        "max_hold_secs": 60,
        "palette": [
            {
                # Magenta
                "rgb_color": (255, 40, 230),
                "brightness": 214,
            },
            {
                # Blue
                "rgb_color": (70, 82, 255),
                "brightness": 145,
            },
            {
                # Gold
                "rgb_color": (255, 163, 49),
                "brightness": 206,
            },
            {
                # Lavender
                "rgb_color": (115, 56, 255),
                "brightness": 255,
            },
        ],
    },
    "Dim arcade": {
        "transition_secs": 10,
        "max_hold_secs": 60,
        "palette": [
            {
                # White-ish
                "rgb_color": (245, 215, 255),
                "brightness": 88,
            },
            {
                # Blue
                "rgb_color": (64, 29, 255),
                "brightness": 226,
            },
            {
                # Red
                "rgb_color": (255, 71, 44),
                "brightness": 70,
            },
            {
                # Purple
                "rgb_color": (117, 12, 255),
                "brightness": 130,
            },
        ],
    },
    "Neon sea": {
        "transition_secs": 10,
        "max_hold_secs": 60,
        "palette": [
            {
                # Blue 1
                "rgb_color": (65, 8, 255),
                "brightness": 255,
            },
            {
                # Blue 2
                "rgb_color": (64, 10, 255),
                "brightness": 255,
            },
            {
                # Sea green
                "rgb_color": (119, 255, 200),
                "brightness": 255,
            },
        ],
    },
    "Ocean city": {
        "transition_secs": 10,
        "max_hold_secs": 60,
        "palette": [
            {
                # White-ish
                "rgb_color": (255, 246, 250),
                "brightness": 96,
            },
            {
                # Salmon
                "rgb_color": (255, 171, 89),
                "brightness": 130,
            },
            {
                # Light blue
                "rgb_color": (61, 125, 255),
                "brightness": 120,
            },
            {
                # Dark blue
                "rgb_color": (63, 44, 255),
                "brightness": 83,
            },
        ],
    },
    "Murder": {
        "transition_secs": 1,
        "max_hold_secs": 8,
        "palette": [
            {
                "rgb_color": (255, 56, 18),
                "brightness": 55,
            },
            {
                "rgb_color": (255, 53, 4),
                "brightness": 18,
            },
            {
                "rgb_color": (255, 58, 21),
                "brightness": 40,
            },
            {
                "rgb_color": (255, 51, 0),
                "brightness": 54,
            },
        ],
    },
    "Purple rain": {
        "transition_secs": 1,
        "max_hold_secs": 8,
        "palette": [
            {
                "rgb_color": (153, 116, 255),
                "brightness": 110,
            },
            {
                "rgb_color": (195, 67, 255),
                "brightness": 62,
            },
            {
                "rgb_color": (163, 82, 255),
                "brightness": 106,
            },
            {
                "rgb_color": (152, 20, 255),
                "brightness": 80,
            },
        ],
    },
}
    

def light_entities_for_group(group_name):
    """Find light entity IDs for the Philips Hue group/room name.
    
    All configured Hue bridges are queried for the group, since HA doesn't store Hue group membership data.
    Pyscript must be configured to expose the "hass" global variable and allow all imports so that we can
    access the bridge configs and entity registry.
    
    :param group_name: The Hue group/room name exactly as it appears in the Hue app (e.g. "Living room").
    :return: List of light entity IDs for the group name or empty list if no matching group or entities are found.
    """
    entity_ids = []

    # Load entity registry.
    entity_registry = er.async_get_registry(hass)
    # Find Hue bridge config(s).
    for config_entry in hass.config_entries.async_entries(domain="hue"):
        host, username = config_entry.data["host"], config_entry.data["username"]
        async with aiohttp.ClientSession() as session:
            bridge = Bridge(host, session, username=username)
            # Query Hue bridge for lights in the matching group (if any).
            await bridge.initialize()
            local_light_id_groups = [group.lights for group in bridge.groups.values() if group.name == group_name]
            if not local_light_id_groups:
                continue
            local_light_ids = [id for id_group in local_light_id_groups for id in id_group]
            # Found the group and lights within it.
            log.debug(f"Found Hue group '{group_name}' on {host}; lights: {local_light_ids}")
            # Get unique IDs for the lights.
            unique_ids = {bridge.lights[id].uniqueid for id in local_light_ids}
            # Get entity IDs for unique IDs.
            entity_ids += [id for id, entity in entity_registry.entities.items() 
                            if entity.unique_id in unique_ids and entity.platform == "hue"]
            log.debug(f"Entites added to group {group_name}: {entity_ids}")
    return entity_ids


@service
def color_swarm_turn_on(hue_group_name = "Office", swarm_name = "Christmas"):
    """Start the color swarm effect on the specified Philips Hue light group.
    
    The color swarm comtinues running on the group until it is turned off or turned on with different parameters.
    
    :param hue_group_name: Name of the Hue light group or room, exactly as it appears in the Hue app. Case-sensitive.
    :param swarm_name: The predefined swarm definition including color palette and transitions.
    """
    
    if swarm_name not in swarms:
        raise ValueError(f"Swarm '{swarm_name}' does not exist.")
    task.unique(f"color-swarm-{hue_group_name}")
    entity_ids = light_entities_for_group(hue_group_name)
    if entity_ids:
        log.info(f"Started '{swarm_name}' color swarm for Hue group '{hue_group_name}' consisting of {len(entity_ids)} light(s).")
    else:
        log.error(f"No light entities found for Hue group '{hue_group_name}'.")
    
    # Create a priority queue of the next transition per light, sorted by random future transition times.
    swarm = swarms[swarm_name]
    transition_q = []
    start_time = time.monotonic()
    for entity_id in entity_ids:
        change_time = random.uniform(start_time, start_time + swarm["max_hold_secs"])
        change_color = random.choice(swarm["palette"])
        heapq.heappush(transition_q, (change_time, entity_id, change_color))
    
    # This will loop forever as long as there are lights and the task isn't killed.
    while transition_q:
        head_time, entity_id, head_color = heapq.heappop(transition_q)
        now = time.monotonic()
        if head_time > now:
            task.sleep(head_time - now)
        light_args = {
            "entity_id": entity_id,
            "transition": swarm["transition_secs"],
            **head_color,
        }
        light.turn_on(**light_args)
        log.debug(f"Applied transition: {light_args}")
        now = time.monotonic()
        next_time = swarm["transition_secs"] + random.uniform(now, now + swarm["max_hold_secs"])
        next_color = random.choice(swarm["palette"])
        heapq.heappush(transition_q, (next_time, entity_id, next_color))    


@service
def color_swarm_turn_off(hue_group_name = "Office"):
    """Stop any running color swarm effect on the specified Philips Hue light group."""
    task.unique(f"color-swarm-{hue_group_name}")
