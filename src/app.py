# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Avnet
# Fully-wireless demo: both the SDK's HTTP discovery/identity calls and the MQTT connection
# use the EV12H55A WiFi Add-on Board (RNWF11). No Ethernet is needed at runtime.
# urllib is patched before the SDK Client is created so its HTTPS calls go through
# dns_resolve + https_get in rnwf11_transport.py. MQTT is bridged through the same module.

import io
import json
import random
import sys
import time
import urllib.request

from avnet.iotconnect.sdk.lite import Client, DeviceConfig, C2dCommand, Callbacks, DeviceConfigError
from avnet.iotconnect.sdk.lite import __version__ as SDK_VERSION
from avnet.iotconnect.sdk.sdklib.mqtt import C2dAck

from rnwf11_transport import Rnwf11Error, Rnwf11MqttTransport, Rnwf11Uart, patch_paho_transport

WIFI_CONFIG_PATH = "wifi_config.json"
MQTT_PORT = 8883

c = None


def load_wifi_config(path=WIFI_CONFIG_PATH) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        print("Missing %s -- copy wifi_config.json.example to %s and fill in your WiFi credentials." % (path, path))
        sys.exit(1)


def patch_urllib(uart: Rnwf11Uart):
    # redirect all urllib HTTPS calls through the WiFi module so no Ethernet is needed
    def rnwf11_urlopen(url, *args, **kwargs):
        url_str = url.full_url if hasattr(url, 'full_url') else url
        print("HTTPS via RNWF11: %s" % url_str)
        return io.BytesIO(uart.https_get(url_str))
    urllib.request.urlopen = rnwf11_urlopen


def on_command(msg: C2dCommand):
    print("Received command", msg.command_name, msg.command_args, msg.ack_id)
    print("Command %s not implemented!" % msg.command_name)
    if msg.ack_id is not None:
        c.send_command_ack(msg, C2dAck.CMD_FAILED, "Not Implemented")


def on_disconnect(reason: str, disconnected_from_server: bool):
    print("Disconnected%s. Reason: %s" % (" from server" if disconnected_from_server else "", reason))


def connect_rnwf11_socket(uart: Rnwf11Uart, host: str, port: int) -> Rnwf11MqttTransport:
    resolved_ip = uart.dns_resolve(host)
    print("Resolved %s -> %s" % (host, resolved_ip))
    sock_id = uart.socket_open_tcp()
    print("Opened RNWF11 socket %d, connecting to %s:%d..." % (sock_id, resolved_ip, port))
    uart.socket_connect(sock_id, resolved_ip, port)
    print("RNWF11 socket connected.")
    return Rnwf11MqttTransport(uart, sock_id)


try:
    wifi_cfg = load_wifi_config()
    uart = Rnwf11Uart()

    print('Joining WiFi network "%s" via the RNWF11 module...' % wifi_cfg["ssid"])
    uart.connect_wifi_if_needed(wifi_cfg["ssid"], wifi_cfg["password"], security=wifi_cfg.get("security"))
    print("RNWF11 WiFi module connected.")

    patch_urllib(uart)

    device_config = DeviceConfig.from_iotc_device_config_json_file(
        device_config_json_path="iotcDeviceConfig.json",
        device_cert_path="device-cert.pem",
        device_pkey_path="device-pkey.pem"
    )

    c = Client(
        config=device_config,
        callbacks=Callbacks(
            command_cb=on_command,
            disconnected_cb=on_disconnect
        )
    )

    transport = connect_rnwf11_socket(uart, c._identity_data.host, MQTT_PORT)
    patch_paho_transport(c.mqtt, transport)
    transport.start()

    while True:
        if not c.is_connected():
            print('(re)connecting...')
            c.connect()
            if not c.is_connected():
                print('Unable to connect. Exiting.')
                sys.exit(2)

        c.send_telemetry({
            'sdk_version': SDK_VERSION,
            'random': random.randint(0, 100)
        })
        time.sleep(10)

except DeviceConfigError as dce:
    print(dce)
    sys.exit(1)

except Rnwf11Error as rnwf_err:
    print("RNWF11 WiFi module error:", rnwf_err)
    sys.exit(1)

except KeyboardInterrupt:
    print("Exiting.")
    sys.exit(0)
