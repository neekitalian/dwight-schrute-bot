#!/usr/bin/env bash
# Install a reviewed checkout on an existing Ubuntu host. Never starts trading.
set -euo pipefail
if [[ $# != 2 || $1 != --commit || ! $2 =~ ^[0-9a-f]{40}$ ]]; then
  echo 'Usage: sudo bash scripts/install-shadow-server.sh --commit FULL_REVIEWED_SHA' >&2
  exit 2
fi
revision=$2
if [[ $(id -u) != 0 ]]; then echo 'Run as root on the selected Ubuntu host.' >&2; exit 2; fi
if ! grep -q '^ID=ubuntu$' /etc/os-release; then echo 'Ubuntu is required.' >&2; exit 2; fi
for service in dwight-shadow.service dwight-alert-inbox.service; do
  if systemctl is-active --quiet "$service"; then
    echo "Active $service must be reviewed and stopped before replacing its release." >&2
    exit 2
  fi
done
for path in /opt/dwight /var/lib/dwight /etc/dwight; do
  if [[ -L $path ]]; then echo "Refusing symlink: $path" >&2; exit 2; fi
done
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y python3-venv git ca-certificates
if ! id dwight >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/dwight --create-home --shell /usr/sbin/nologin dwight
fi
if [[ $(getent passwd dwight | cut -d: -f6) != /var/lib/dwight || $(id -u dwight) == 0 ]]; then
  echo 'Existing dwight user has an unexpected identity or home.' >&2; exit 2
fi
install -d -m 700 -o dwight -g dwight /var/lib/dwight
install -d -m 750 -o root -g dwight /etc/dwight
if [[ ! -e /opt/dwight ]]; then
  git clone https://github.com/neekitalian/dwight-schrute-bot.git /opt/dwight
fi
cd /opt/dwight
if [[ ! -d .git || $(git remote get-url origin) != https://github.com/neekitalian/dwight-schrute-bot.git ]]; then
  echo 'Unexpected repository; refusing replacement.' >&2; exit 2
fi
if [[ -n $(git status --porcelain) ]]; then echo 'Checkout is dirty; refusing replacement.' >&2; exit 2; fi
git fetch origin "$revision"
git checkout --detach "$revision"
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.lock
.venv/bin/python -m pip install --disable-pip-version-check --no-deps .
export MPLCONFIGDIR=/var/lib/dwight/matplotlib XDG_CACHE_HOME=/var/lib/dwight/cache
install -d -m 700 -o dwight -g dwight "$MPLCONFIGDIR" "$XDG_CACHE_HOME"
runuser -u dwight -- env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=/opt/dwight .venv/bin/python -m unittest discover -s tests -q
install -m 644 deploy/linux/dwight-shadow.service /etc/systemd/system/dwight-shadow.service
install -m 644 deploy/linux/dwight-reports.service /etc/systemd/system/dwight-reports.service
install -m 644 deploy/linux/dwight-reports.timer /etc/systemd/system/dwight-reports.timer
install -m 644 deploy/linux/dwight-alert-inbox.service /etc/systemd/system/dwight-alert-inbox.service
systemd-analyze verify /etc/systemd/system/dwight-shadow.service /etc/systemd/system/dwight-reports.service /etc/systemd/system/dwight-reports.timer /etc/systemd/system/dwight-alert-inbox.service
systemctl daemon-reload
# No environment file, credentials or release is fabricated or overwritten.
# Activation is a separate step after real-data evaluation and release checks.
.venv/bin/python scripts/server-preflight.py
