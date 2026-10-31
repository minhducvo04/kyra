"""Public notice for unavailable private screening integration."""
NOTICE = "This request requires a separately configured privacy screen."

CLEAR, HEALTH, OFFLINE, OFF = "clear", "health", "offline", "off"


def check(texts):
    """No local screen model in this build: OFF, as in the private app with the screen switched off."""
    return OFF
