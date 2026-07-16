#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Avnet

set -e

SRC_DIR="./src"
ARCHIVE_NAME="environmental-data-src.zip"
STAGING_DIR="/tmp/sama7d65-environmental-data-package"

rm -rf "$STAGING_DIR"
mkdir -p "$STAGING_DIR"

cp -r "$SRC_DIR"/. "$STAGING_DIR/"
find "$STAGING_DIR" -type d -name "__pycache__" -exec rm -rf {} +
find "$STAGING_DIR" -type f -name "*.pyc" -delete
chmod +x "$STAGING_DIR/install.sh"

(cd "$STAGING_DIR" && zip -qr "$OLDPWD/$ARCHIVE_NAME" .)

rm -rf "$STAGING_DIR"

echo "Created archive $ARCHIVE_NAME in the environmental-data directory."
echo "Upload $ARCHIVE_NAME to wherever this repo's packages are hosted (matching the quickstart's wifi-module-src.zip) so the README's wget URL resolves."
