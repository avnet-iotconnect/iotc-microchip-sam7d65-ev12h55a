# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Avnet
# Thin wrapper around the `bme680` PyPI driver for the MikroE Environment
# Click (BME680, MIKROE-2467) plugged into mikroBUS2.

import glob
import re
import time

import bme680
import smbus2

# The Environment Click ships with its ADDR SEL jumper in the "1" position,
# which selects the BME680's secondary I2C address (0x77) rather than the
# library default of 0x76.
DEFAULT_I2C_ADDR = bme680.constants.I2C_ADDR_SECONDARY

# The first several gas-resistance readings after power-up are unreliable
# while the sensor's internal heater is still stabilizing.
GAS_BURN_IN_READS = 5

# The mikroBUS2 I2C bus (FLEXCOM0) uses DMA for block reads longer than a few
# bytes, and that DMA path reliably times out (errno 110) whenever the
# transfer length is a multiple of 4 and longer than 8 bytes -- reproduced
# directly against this sensor (e.g. a 16-byte read fails every time, while
# 15- and 17-byte reads of the same registers succeed). Reads of 8 bytes or
# fewer are unaffected. The `bme680` library issues fixed-size block reads
# (16 and 25 bytes) to fetch calibration data, so those must be chunked to
# stay clear of the bad transfer lengths.
_SAFE_BLOCK_CHUNK = 8


def _candidate_bus_numbers():
    numbers = []
    for path in glob.glob("/dev/i2c-*"):
        m = re.search(r"i2c-(\d+)$", path)
        if m:
            numbers.append(int(m.group(1)))
    return sorted(numbers)


class _ChunkedI2CBus:
    """Wraps smbus2.SMBus to work around a mikroBUS2 DMA block-read bug.

    See _SAFE_BLOCK_CHUNK above. Only read_i2c_block_data needs chunking --
    the bme680 driver's other transfers (byte reads/writes) are all small
    enough to be unaffected.
    """

    def __init__(self, bus: smbus2.SMBus):
        self._bus = bus

    def read_byte_data(self, i2c_addr, register):
        return self._bus.read_byte_data(i2c_addr, register)

    def write_byte_data(self, i2c_addr, register, value):
        return self._bus.write_byte_data(i2c_addr, register, value)

    def write_i2c_block_data(self, i2c_addr, register, data):
        return self._bus.write_i2c_block_data(i2c_addr, register, data)

    def read_i2c_block_data(self, i2c_addr, register, length):
        result = []
        offset = 0
        while offset < length:
            chunk_len = min(_SAFE_BLOCK_CHUNK, length - offset)
            result += self._bus.read_i2c_block_data(i2c_addr, register + offset, chunk_len)
            offset += chunk_len
        return result


def _open_sensor(bus_no, i2c_addr):
    bus = _ChunkedI2CBus(smbus2.SMBus(bus_no))
    return bme680.BME680(i2c_addr=i2c_addr, i2c_device=bus)


def open_environment_click(bus_no=None, i2c_addr=DEFAULT_I2C_ADDR):
    """Locate and initialize the Environment Click's BME680 sensor.

    mikroBUS1 and mikroBUS2 share the same physical I2C bus (PC6/PC7), so
    once the overlay is applied the BME680 shows up on whichever /dev/i2c-N
    node the kernel assigned to that bus. If bus_no isn't given explicitly,
    every /dev/i2c-* node is probed until one ACKs at i2c_addr with the
    BME680's chip ID.
    """
    if bus_no is not None:
        return _open_sensor(bus_no, i2c_addr)

    errors = []
    for candidate in _candidate_bus_numbers():
        try:
            return _open_sensor(candidate, i2c_addr)
        except (RuntimeError, OSError) as exc:
            errors.append("i2c-%d: %s" % (candidate, exc))
    raise RuntimeError(
        "Could not find the Environment Click's BME680 on any I2C bus. Checked:\n  " +
        "\n  ".join(errors) +
        "\nMake sure sama7d65_curiosity_environment_click2.dtbo has been applied "
        "(see apply_environment_overlay.py) and the board has been power-cycled, and "
        "that the click board is firmly seated in mikroBUS2 (J26)."
    )


class EnvironmentClick:
    """Reads temperature, humidity, pressure and gas resistance from the BME680."""

    def __init__(self, bus_no=None, i2c_addr=DEFAULT_I2C_ADDR):
        self._sensor = open_environment_click(bus_no=bus_no, i2c_addr=i2c_addr)
        self._sensor.set_gas_heater_temperature(320)
        self._sensor.set_gas_heater_duration(150)
        self._sensor.select_gas_heater_profile(0)
        self._gas_reads = 0

    def read(self) -> dict:
        """Return the latest sensor reading, blocking until data is ready.

        With the gas heater enabled, a forced-mode conversion can take longer
        than the underlying library's internal poll window, particularly on
        the first read after initialization -- retry a few times rather than
        failing on that one slow conversion.
        """
        for attempt in range(4):
            if self._sensor.get_sensor_data():
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Timed out waiting for BME680 sensor data")

        data = self._sensor.data
        reading = {
            "temperature_c": round(data.temperature, 2),
            "humidity_rh": round(data.humidity, 2),
            "pressure_hpa": round(data.pressure, 2),
        }

        if data.heat_stable:
            self._gas_reads += 1
            if self._gas_reads > GAS_BURN_IN_READS:
                reading["gas_resistance_ohms"] = round(data.gas_resistance, 1)

        return reading
