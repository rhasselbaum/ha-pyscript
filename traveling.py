import re

@service
def approaching_neighborhood_alert(entity_id=None):
    """Notify about someone approaching the neighborhood.
    
    :param entity_id: ID of the person approaching."""
    # Send a notification for this person at most once every 20 minutes.
    task.unique("approaching_neighborhood_alert_" + entity_id)
    friendly_name = state.get(entity_id + ".friendly_name")
    notify.ephemeral_notifications_group(
        title="Approaching neighborhood", 
        message=f"{friendly_name} is nearly home."
    )
    task.sleep(1200)  # Don't notify again for awhile to account for GPS jitter, etc.


@service
def conditional_driving_alert(entity_id=None):
    """Notify that someone appears to have started driving if and only if their matching notification flag is set.
    
    Clear the notification flag once a notification has gone out.
    
    :param entity_id: ID of the driving binary sensor.
    """
    # Convert binary sensor ID to person ID.
    person_name = re.match(r"sensor.([^_]+)_driving", entity_id).group(1)
    person_id = f"person.{person_name}"
    # Was a notification requested? Check virtual switch.
    notification_input_boolean_id = f"input_boolean.{person_name}_driving_notification_requested"
    if state.get(notification_input_boolean_id) == "on":
        # Turn off switch and send notifications.
        input_boolean.turn_off(entity_id=notification_input_boolean_id)
        friendly_name = state.get(person_id + ".friendly_name")
        notify.ephemeral_notifications_group(
            title="Driving detected", 
            message=f"{friendly_name} has started driving."
        )
        notify.mobile_notifications_high_priority_group(
            title="Driving detected", 
            message=f"{friendly_name} has started driving."
        )

