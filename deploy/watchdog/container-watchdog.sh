#!/bin/sh
# Start containers that Docker failed to start and will not retry.
#
# A restart policy covers crashes, but not a container whose start fails when
# the daemon itself starts (e.g. after a moby-engine upgrade): Docker records
# the error in .State.Error and gives up. On 2026-11-13 academic_mcp stayed
# down for 27 hours that way ("Address already in use").
#
# Only containers with a recorded start error are touched, so a container
# stopped on purpose (docker stop) stays stopped.
#
# Usage: container-watchdog.sh NAME...   (default: the academic-mcp stack)
set -u
names=${*:-"academic_mcp academic-auth scrapling-mcp scrapling-auth"}
for name in $names; do
    state=$(docker inspect -f '{{.State.Status}}|{{.State.Error}}' "$name" 2>/dev/null) || continue
    status=${state%%|*}
    error=${state#*|}
    [ "$status" = running ] && continue
    [ -z "$error" ] && continue
    echo "$name is $status after a failed start ($error); starting it"
    if docker start "$name" >/dev/null; then
        echo "$name started"
    else
        echo "$name failed to start again" >&2
    fi
done
