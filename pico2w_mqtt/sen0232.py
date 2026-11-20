"""DFRobot SEN0232 analog sound-level sensor support.

The conversion functions are pure and run on any Python implementation.
Only the SEN0232 class touches hardware, and it imports ``machine`` lazily.

The SEN0232 output is already a sound-level voltage. No microphone waveform
or RMS processing is applied here. The result is an estimated A-weighted
sensor sound level, not a standards-compliant LAeq or LAFmax value.
"""

ADC_FULL_SCALE = 65535  # machine.ADC.read_u16() range is 0..65535
DB_A_PER_VOLT = 50.0  # DFRobot: dBA = voltage * 50
SENSOR_MIN_V = 0.6  # 30 dBA, lower end of the documented range
SENSOR_MAX_V = 2.6  # 130 dBA, upper end of the documented range

# Pico 2 W GPIOs routed to user-accessible ADC inputs (ADC0..ADC2).
VALID_ADC_GPIOS = (26, 27, 28)


def raw_to_voltage(raw, reference_v):
    """Convert a read_u16() result to volts using the ADC reference voltage."""
    if raw < 0 or raw > ADC_FULL_SCALE:
        raise ValueError("raw ADC value out of range: %r" % (raw,))
    return raw * reference_v / ADC_FULL_SCALE


def voltage_to_db_a(voltage_v):
    """DFRobot SEN0232 conversion, without clamping."""
    return voltage_v * DB_A_PER_VOLT


def apply_calibration(level_db_a, cal_a=1.0, cal_b=0.0):
    """Per-device linear correction: a * level + b."""
    return cal_a * level_db_a + cal_b


def in_measurement_range(voltage_v):
    """True when the voltage lies within the documented 0.6-2.6 V span."""
    return SENSOR_MIN_V <= voltage_v <= SENSOR_MAX_V


def convert(raw, reference_v, cal_a=1.0, cal_b=0.0):
    """Return (voltage_v, level_db_a_estimate, in_range) for one raw reading.

    Out-of-range voltages are reported with in_range=False and are not
    clamped. The range check applies to the sensor voltage, before
    calibration.
    """
    voltage_v = raw_to_voltage(raw, reference_v)
    level = apply_calibration(voltage_to_db_a(voltage_v), cal_a, cal_b)
    return voltage_v, level, in_measurement_range(voltage_v)


class SEN0232:
    """Hardware wrapper around one Pico ADC input."""

    def __init__(self, gpio=26, reference_v=3.3, cal_a=1.0, cal_b=0.0, adc=None):
        if adc is None:
            import machine

            adc = machine.ADC(machine.Pin(gpio))
        self._adc = adc
        self.gpio = gpio
        self.reference_v = reference_v
        self.cal_a = cal_a
        self.cal_b = cal_b

    def read(self):
        """Take one ADC sample; return (raw, voltage_v, level_db_a, in_range)."""
        raw = self._adc.read_u16()
        voltage_v, level, in_range = convert(
            raw, self.reference_v, self.cal_a, self.cal_b
        )
        return raw, voltage_v, level, in_range


def energy_average_db(levels_sum_power, count):
    """10*log10(mean(10^(L/10))) from a pre-computed sum of 10^(L/10)."""
    import math

    return 10.0 * math.log10(levels_sum_power / count)


class LevelAggregator:
    """Accumulates samples taken during one reporting interval.

    The level summary is an energy (power) average of the per-sample dBA
    estimates: 10*log10(mean(10^(L/10))). It is a sensor estimate over the
    interval, not a standards-compliant LAeq.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.count = 0
        self.out_of_range = 0
        self.voltage_sum = 0.0
        self.power_sum = 0.0
        self.level_min = None
        self.level_max = None
        self.last_raw = None

    def add(self, raw, voltage_v, level_db_a, in_range):
        self.count += 1
        self.last_raw = raw
        self.voltage_sum += voltage_v
        self.power_sum += 10.0 ** (level_db_a / 10.0)
        if not in_range:
            self.out_of_range += 1
        if self.level_min is None or level_db_a < self.level_min:
            self.level_min = level_db_a
        if self.level_max is None or level_db_a > self.level_max:
            self.level_max = level_db_a

    def summary(self):
        """Return a dict for the interval; requires at least one sample."""
        if self.count == 0:
            raise ValueError("no samples in interval")
        return {
            "count": self.count,
            "last_raw": self.last_raw,
            "voltage_mean_v": self.voltage_sum / self.count,
            "level_db_a": energy_average_db(self.power_sum, self.count),
            "level_min_db_a": self.level_min,
            "level_max_db_a": self.level_max,
            "out_of_range": self.out_of_range,
        }
