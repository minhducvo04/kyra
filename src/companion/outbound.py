"""Public transport seam. Configure an explicit gate to send requests."""
class ReleaseRefused(RuntimeError):
    def __init__(self, classes=(), **kwargs):
        self.classes = frozenset(classes)
        self.secret = kwargs.get("secret", False)
        self.completed_tools = kwargs.get("completed_tools", ())
        self.screen_offline = kwargs.get("screen_offline", False)
        super().__init__("Outbound transport is unavailable in this public package.")
class OutboundGate:
    def __init__(self, *args, **kwargs):
        raise ReleaseRefused()
def default_gate():
    raise ReleaseRefused()
