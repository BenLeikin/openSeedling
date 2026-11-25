"""Announce that something changed, without knowing who is listening.

The control loops and actuators call publish() when a value the dashboard
shows has changed; status.py subscribes and pushes the news to open pages.
Nothing here imports the rest of the app, so the lower layers (hardware,
light, watering) can announce a change without importing the web side.
"""

_listeners = []


def subscribe(fn):
    """Have fn(reason) called on every publish."""
    _listeners.append(fn)


def unsubscribe(fn):
    if fn in _listeners:
        _listeners.remove(fn)


def publish(reason="update"):
    """Tell every subscriber that the status changed, and why."""
    for fn in list(_listeners):
        fn(reason)
