#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Avnet

set -e

SRC_DIR="./src"
ARCHIVE_NAME="wifi-module-src.zip"
STAGING_DIR="/tmp/sama7d65-wifi-module-package"

rm -rf "$STAGING_DIR"
mkdir -p "$STAGING_DIR"

cp -r "$SRC_DIR"/. "$STAGING_DIR/"
find "$STAGING_DIR" -type d -name "__pycache__" -exec rm -rf {} +
find "$STAGING_DIR" -type f -name "*.pyc" -delete
chmod +x "$STAGING_DIR/install.sh"

(cd "$STAGING_DIR" && zip -qr "$OLDPWD/$ARCHIVE_NAME" .)

rm -rf "$STAGING_DIR"

echo "Created archive $ARCHIVE_NAME in the repo root."
echo "The live quickstart guide downloads this archive from the avnetpublicaccess S3 bucket, which you cannot"
echo "upload to yourself -- if you customized the package, deliver $ARCHIVE_NAME to your board directly instead"
echo "(see 'Customizing and Redeploying the App' in README.md)."
