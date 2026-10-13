#!/bin/sh
# Install the argus page (:8082) and daily 06:00 scan as systemd user units.
# Expects the repo at ~/work/argus. Undo: systemctl --user disable --now
# argus-web.service argus-scan.timer && rm ~/.config/systemd/user/argus-*
set -e
dir=$(dirname "$0")
mkdir -p ~/.config/systemd/user
cp "$dir"/argus-web.service "$dir"/argus-scan.service "$dir"/argus-scan.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now argus-web.service argus-scan.timer
systemctl --user --no-pager status argus-web.service | head -5
systemctl --user list-timers argus-scan.timer --no-pager
