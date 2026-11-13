#!/bin/sh
# TEMPLATE: install a private edited copy; do not commit your instrument identity.
# The forced command deliberately ignores SSH_ORIGINAL_COMMAND and caller arguments.
exec /ABSOLUTE/PATH/rigol-mcp/.venv/bin/rigol-mcp \
  --resource 'REPLACE_WITH_ACTUAL_VISA_RESOURCE' --allow-control
