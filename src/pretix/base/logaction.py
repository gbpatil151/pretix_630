from typing import Optional

class LogActionType:
    def __init__(self, action_type: str, display_text=None, webhook_event=None, notification_type=None):
        self.action_type = action_type
        self.display_text = display_text
        self.webhook_event = webhook_event
        self.notification_type = notification_type

class LogActionMediator:
    def __init__(self):
        self._actions = {}

    def register(self, action: LogActionType):
        self._actions[action.action_type] = action

    def get_action(self, action_type: str) -> Optional[LogActionType]:
        return self._actions.get(action_type)

    def get_all(self):
        return self._actions

log_action_mediator = LogActionMediator()
