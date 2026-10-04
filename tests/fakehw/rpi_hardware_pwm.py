"""Stand-in for rpi_hardware_pwm so the app runs off a Pi (tests/test_ui.py)."""


class HardwarePWM:
    def __init__(self, pwm_channel=0, hz=1000, chip=0):
        self.duty = None

    def start(self, d):
        self.duty = d

    def change_duty_cycle(self, d):
        self.duty = d

    def change_frequency(self, hz):
        pass

    def stop(self):
        self.duty = None
