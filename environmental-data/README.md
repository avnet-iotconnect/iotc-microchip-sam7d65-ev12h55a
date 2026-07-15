# Environmental Data Expansion Demo

Upgrades this repo's WiFi quickstart demo on the Microchip SAMA7D65-Curiosity Kit to read real temperature, humidity,
pressure, and gas (VOC) data from a [MikroE Environment Click](https://www.mikroe.com/environment-click)
(Bosch BME680) board and publish it as telemetry to /IOTCONNECT, in place of the quickstart's random-integer
telemetry. The EV12H55A WiFi Add-on Board stays attached and continues to provide connectivity — this demo only
changes what data gets sent, not how it gets there.

> [!IMPORTANT]
> Complete the [quickstart guide](../README.md) for this repo first, including the WiFi module setup. This guide
> assumes the EV12H55A WiFi Add-on Board is already seated in mikroBUS1 (J25) and working.

1. [Introduction](#1-introduction)
2. [Set Up Hardware](#2-set-up-hardware)
3. [Download and Install the Package](#3-download-and-install-the-package)
4. [Enable the mikroBUS2 I2C Bus](#4-enable-the-mikrobus2-i2c-bus)
5. [Change Device Template](#5-change-device-template)
6. [Run the Demo](#6-run-the-demo)
7. [Telemetry](#7-telemetry)
8. [Resources](#8-resources)

# 1. Introduction

The board has two mikroBUS™ Click sockets (see the quickstart's hardware setup step). The EV12H55A WiFi module
occupies mikroBUS1. The Environment Click plugs into the other one, mikroBUS2 (J26), and communicates with the
SAMA7D65 over I2C.

The Environment Click carries a Bosch **BME680** environmental sensor, a 4-in-1 digital sensor that measures:

* Temperature
* Relative humidity
* Barometric pressure
* Gas resistance (an indicator of VOC/air-quality changes — not a calibrated ppm reading)

> [!NOTE]
> mikroBUS1 and mikroBUS2 share the same physical I2C bus on this board (SCL/SDA on pins PC6/PC7), broken out to
> both sockets and to the 40-pin Raspberry Pi connector. This isn't a problem here since the WiFi module doesn't use
> I2C, but keep it in mind if you ever add another I2C Click board to mikroBUS1 as well — they'd need distinct I2C
> addresses.

# 2. Set Up Hardware

With the WiFi module already seated in mikroBUS1 from the quickstart guide, plug the Environment Click into
**mikroBUS2** — the socket that the quickstart's WiFi guide marked with a red X (i.e., the one it told you *not* to
use for the WiFi module). Align the Click board's pins with the socket and press it down firmly until fully seated.

The Environment Click ships from the factory already configured the way this demo expects it — no jumper changes are
needed:

* **COMM. SEL** jumpers (JP1, JP3, JP4, JP5): factory default, I2C mode
* **ADDR. SEL** jumper (JP2): factory default, position `1` (I2C address `0x77`)

# 3. Download and Install the Package

On the board, run:

```bash
cd /opt/demo
wget -O env-package.tar.gz https://avnetpublicaccess.s3.us-east-1.amazonaws.com/environmental-data-src.zip
tar -xzf env-package.tar.gz --overwrite
bash ./install.sh
```

> [!IMPORTANT]
> Installation will ask whether to overwrite the existing `app.py`. Enter **`y`** this time — unlike the initial
> quickstart onboarding step, you *do* want this package's `app.py` (with real sensor readings) to replace the
> WiFi-only quickstart version.

This lands `apply_environment_overlay.py`, `sama7d65_curiosity_environment_click2.dtbo`, `app.py`, and the rest of
this demo's files in `/opt/demo`, alongside the quickstart's existing files.

# 4. Enable the mikroBUS2 I2C Bus

Just like mikroBUS1's UART, mikroBUS2's I2C pins are not enabled by default — the board's device tree leaves the
FLEXCOM peripheral behind them unconfigured out of the box. The package you just installed includes a second device
tree overlay and setup script that enables it permanently, alongside the WiFi overlay from the quickstart.

1. Run the setup script:

   ```
   python3 /opt/demo/apply_environment_overlay.py
   ```

   This patches the board's saved boot environment to load *both* the WiFi UART overlay and the new I2C overlay on
   every boot, and copies the new overlay file onto the boot partition. It's safe to run regardless of whether the
   WiFi overlay was applied first — it rewrites the full boot sequence rather than assuming the prior state.

2. Power-cycle the board (unplug and replug the USB-C power cable — **a soft reset/reboot is not enough**, the same
   as with the WiFi overlay). After it boots back up, confirm the sensor is visible on I2C:

   ```
   for b in /dev/i2c-*; do n=${b##*i2c-}; v=$(i2cget -y "$n" 0x77 0xd0 2>/dev/null); [ "$v" = "0x61" ] && echo "BME680 found on $b"; done
   ```

   If this prints a bus, the overlay is working — the BME680's chip-ID register (`0xd0`) reading back `0x61`
   confirms it.

   > [!NOTE]
   > Don't use `i2cdetect` to check for the sensor — its default probe (a zero-length "quick write") gets no ACK
   > from this particular sensor even when it's present and working fine, so it reports the address as empty. A real
   > register read, as above, is the reliable check.

> [!TIP]
> If you ever need to undo this change, the original boot environment (from before either overlay was applied) was
> backed up to `/uboot.env.bak` on the boot partition.

> [!NOTE]
> **A note on hardware quirks:** mikroBUS2's I2C bus uses DMA for longer register-block transfers, and that DMA path
> reliably times out when a single transfer is more than 8 bytes *and* a multiple of 4 (verified directly against
> this sensor — e.g. a 16-byte read fails every time, while 15- and 17-byte reads of the same data succeed). The
> `bme680` Python driver reads its calibration data in exactly such fixed-size blocks, so `environment_click.py`
> wraps the I2C bus to transparently split those reads into 8-byte chunks. This is already handled for you — nothing
> to configure — but worth knowing if you extend this driver or write your own I2C code against mikroBUS2.

# 5. Change Device Template

This demo sends different telemetry fields than the quickstart's random-integer template, so your device needs a
template that defines them (see [Telemetry](#7-telemetry) below for the field list). Import the
[environmental-data-template.json](./environmental-data-template.json) device template to /IOTCONNECT and set it as
your device's template (template code `sama7dEnviro`).

> [!NOTE]
> `gas_resistance_ohms` is omitted from telemetry messages for the first several readings after startup while the
> BME680's gas heater stabilizes (see the [Run the Demo](#6-run-the-demo) note below) -- the template defines it
> as an optional attribute, so those early messages are expected to show it as `null` rather than indicating a
> problem.

# 6. Run the Demo

```bash
cd /opt/demo
python3 app.py
```

The application connects to WiFi through the RNWF11 module exactly as the quickstart does, initializes the
Environment Click over I2C, then reads and publishes all four measurements every 10 seconds. View the readings
under the **Live Data** tab for your device on /IOTCONNECT.

> [!NOTE]
> The BME680's gas sensor needs its internal heater to stabilize after power-up before its readings are trustworthy.
> The app omits `gas_resistance_ohms` from telemetry for the first several readings while this happens — temperature,
> humidity, and pressure are reported from the very first reading.

# 7. Telemetry

| Field | Unit | Description |
|-------|------|--------------|
| `sdk_version` | - | /IOTCONNECT Python Lite SDK version string |
| `temperature_c` | °C | Ambient temperature |
| `humidity_rh` | %RH | Relative humidity |
| `pressure_hpa` | hPa | Barometric pressure |
| `gas_resistance_ohms` | Ω | Gas sensor resistance (higher = cleaner air); omitted until the heater stabilizes after startup |

# 8. Resources

* [MikroE Environment Click Product Page](https://www.mikroe.com/environment-click)
* [Environment Click Datasheet](https://www.mikroe.com/environment-click#/263-clickid-yes)
* [Bosch BME680 Product Page](https://www.bosch-sensortec.com/products/environmental-sensors/gas-sensors-bme680/)
* [/IOTCONNECT Quickstart for this Repo](../README.md)
* [/IOTCONNECT Overview](https://www.iotconnect.io/)
* [/IOTCONNECT Knowledgebase](https://help.iotconnect.io/)
