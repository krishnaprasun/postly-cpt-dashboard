#!/bin/bash
# Postly live CPT dashboard. Read-only: it never writes to Meta.
cd "$(dirname "$0")" || exit 1
PORT="${PORT:-8787}" exec python3 server.py
