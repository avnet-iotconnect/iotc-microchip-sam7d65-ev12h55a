#!/usr/bin/env python3
"""
Run this ON THE BOARD (over SSH) to permanently enable the mikroBUS2 I2C bus
used by the MikroE Environment Click (BME680).

Usage:
    python3 apply_environment_overlay.py

This patches the board's saved U-Boot environment so every future boot loads
both the mikroBUS1 WiFi UART overlay and the mikroBUS2 I2C overlay. It edits
the environment directly as a file (not via the U-Boot serial console), since
hand-typing a long setenv command over serial is prone to silent truncation.

This script assumes the EV12H55A WiFi Add-on Board's UART overlay has already
been applied (per the quickstart guide) -- it replaces bootcmd_boot with a
version that loads both overlays, so it is safe to run whether or not the
WiFi overlay step ran first.
"""
import binascii
import os
import shutil
import struct
import subprocess
import sys

BOOT_DEV = "/dev/mmcblk0p1"
MOUNT_POINT = "/mnt/boot"
ENV_FILE = os.path.join(MOUNT_POINT, "uboot.env")
WIFI_OVERLAY_NAME = "sama7d65_curiosity_wifi_click1.dtbo"
ENV_OVERLAY_NAME = "sama7d65_curiosity_environment_click2.dtbo"
PAD_BYTE = b"\xff"

NEW_BOOTCMD_BOOT = (
    b"bootcmd_boot=fatload mmc 0:1 0x63000000 sama7d65_curiosity.itb; "
    b"imxtract 0x63000000 kernel 0x62000000; "
    b"imxtract 0x63000000 base_fdt 0x61000000; "
    b"fatload mmc 0:1 0x61100000 " + WIFI_OVERLAY_NAME.encode() + b"; "
    b"fatload mmc 0:1 0x61180000 " + ENV_OVERLAY_NAME.encode() + b"; "
    b"fdt addr 0x61000000; fdt resize 0x8000; "
    b"fdt apply 0x61100000; fdt apply 0x61180000; "
    b"bootz 0x62000000 - 0x61000000"
)


def mount_boot():
    os.makedirs(MOUNT_POINT, exist_ok=True)
    subprocess.run(["mount", "-t", "vfat", "-o", "rw", BOOT_DEV, MOUNT_POINT], check=True)


def unmount_boot():
    subprocess.run(["umount", MOUNT_POINT], check=False)


def patch_env(env_bytes: bytes) -> bytes:
    body = env_bytes[4:]
    end = body.find(PAD_BYTE * 4)
    text = body[:end] if end != -1 else body.rstrip(b"\x00")
    entries = [e for e in text.split(b"\x00") if e]

    replaced = False
    new_entries = []
    for entry in entries:
        if entry.startswith(b"bootcmd_boot="):
            new_entries.append(NEW_BOOTCMD_BOOT)
            replaced = True
        else:
            new_entries.append(entry)
    if not replaced:
        new_entries.append(NEW_BOOTCMD_BOOT)

    new_body = b"\x00".join(new_entries) + b"\x00\x00"
    total_len = len(body)
    if len(new_body) > total_len:
        raise ValueError("new environment is too large to fit in the existing env partition size")
    padded = new_body + PAD_BYTE * (total_len - len(new_body))

    crc = binascii.crc32(padded) & 0xFFFFFFFF
    return struct.pack("<I", crc) + padded


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    overlay_src = os.path.join(script_dir, ENV_OVERLAY_NAME)
    if not os.path.isfile(overlay_src):
        sys.exit(f"Expected {ENV_OVERLAY_NAME} next to this script - copy it to the board alongside this file.")

    mount_boot()
    try:
        with open(ENV_FILE, "rb") as f:
            original_env = f.read()

        if NEW_BOOTCMD_BOOT in original_env:
            print("Overlay boot sequence is already applied - nothing to do.")
        else:
            backup_path = ENV_FILE + ".bak"
            if not os.path.exists(backup_path):
                shutil.copyfile(ENV_FILE, backup_path)
                print(f"Backed up original environment to {backup_path}")

            new_env = patch_env(original_env)
            with open(ENV_FILE, "wb") as f:
                f.write(new_env)
            print("Patched bootcmd_boot to load the WiFi UART and Environment Click I2C overlays on every boot.")

        shutil.copyfile(overlay_src, os.path.join(MOUNT_POINT, ENV_OVERLAY_NAME))
        print(f"Copied {ENV_OVERLAY_NAME} to the boot partition.")

        wifi_overlay_boot_path = os.path.join(MOUNT_POINT, WIFI_OVERLAY_NAME)
        if not os.path.isfile(wifi_overlay_boot_path):
            print(f"WARNING: {WIFI_OVERLAY_NAME} not found on the boot partition. "
                  f"Run the quickstart's apply_wifi_overlay.py first.")
    finally:
        unmount_boot()

    print("Done. Power-cycle the board (unplug/replug power) for the change to take effect.")


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("Run this as root.")
    main()
