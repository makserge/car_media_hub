#!/usr/bin/with-contenv sh
# with-contenv: the base image's s6 init starts this script with an empty environment. This brings back
# the Supervisor's variables: SUPERVISOR_TOKEN for the Home Assistant API, TZ for local check times.
# the browser user (Chromium) must not read the Supervisor token from the s6 environment folder
chmod 0700 /run/s6/container_environment 2>/dev/null || true
# the browser user may enter /data (its folders are in there) but neither list it nor read the add-on's own files
chmod 0711 /data 2>/dev/null || true
chmod 0600 /data/options.json 2>/dev/null || true
export PYTHONPATH=/app
exec python3 -m car_media_hub.main
