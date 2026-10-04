"""Stand-in for gpiozero so the app runs off a Pi (tests/test_ui.py)."""


class _Dev:
    def __init__(self, pin=None, *a, **k):
        self.pin = pin
        self.value = 0
        self.is_pressed = False
        self.when_pressed = self.when_released = None

    def on(self):
        self.value = 1

    def off(self):
        self.value = 0

    def close(self):
        pass


OutputDevice = PWMOutputDevice = Button = DigitalInputDevice = _Dev
