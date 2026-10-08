#!/usr/bin/env bash
# Install or update the bot on a server. Run from the project directory:
#   bash deploy/install.sh
# Safe to run again after an update: it reinstalls requirements, reruns the tests
# and restarts the service. It never touches anything outside this directory except
# /etc/systemd/system/gamefinder.service.
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$(pwd)"

# Leave room for everything else on the server.
MIN_FREE_MB="${MIN_FREE_MB:-400}"
free_mb=$(awk '/MemAvailable/ {print int($2 / 1024)}' /proc/meminfo)
if [ "${free_mb:-0}" -lt "$MIN_FREE_MB" ]; then
  echo "Only ${free_mb} MB of memory available (need ${MIN_FREE_MB}); not installing."
  exit 1
fi
UNIT=/etc/systemd/system/gamefinder.service
if [ -f "$UNIT" ] && ! grep -qF "WorkingDirectory=${DIR}" "$UNIT"; then
  echo "$UNIT belongs to another directory; not overwriting it."
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env: fill in BOT_TOKEN and OWNER_IDS (and GEMINI_API_KEY for the"
  echo "review analyst), then run this script again."
  exit 1
fi
chmod 600 .env

if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  echo "Python 3.10 or newer is needed (found $(python3 --version))."
  exit 1
fi
if ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
  echo "python3-venv is missing: apt install -y python3-venv"
  exit 1
fi
python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt

if [ -f tests.py ]; then
  echo "== offline tests"
  # Speed checks are for development machines: a busy shared server must not fail a deploy.
  export GF_TIMING=0
  venv/bin/python tests.py
  for t in tests_*.py; do
    if [ -f "$t" ]; then venv/bin/python "$t"; fi
  done
else
  echo "== no tests.py, skipping offline tests"
fi
if ! venv/bin/python -c 'import imageio_ffmpeg; imageio_ffmpeg.get_ffmpeg_exe()' >/dev/null 2>&1; then
  echo "!! no ffmpeg for animated cards: they will be sent as pictures (set ANIMATE_CARDS=0 to silence)"
fi

mkdir -p data/backups
chmod 700 data data/backups
# A snapshot before the new code starts (it may migrate the database). The last 2 are kept.
if [ -f data/gamefinder.db ]; then
  snap="data/backups/pre-deploy-$(date +%Y%m%d-%H%M%S).db"
  venv/bin/python -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('VACUUM INTO ?', (sys.argv[2],))" \
    data/gamefinder.db "$snap" && gzip -f "$snap" && chmod 600 "$snap.gz"
  ls -1t data/backups/pre-deploy-*.db.gz 2>/dev/null | tail -n +3 | xargs -r rm -f
fi
sed "s#__DIR__#${DIR}#g" deploy/gamefinder.service > "$UNIT"
# Optional server-specific restrictions (kept out of the repo).
if [ -f deploy/server-hardening.conf ]; then
  mkdir -p "${UNIT}.d"
  cp deploy/server-hardening.conf "${UNIT}.d/hardening.conf"
fi
systemctl daemon-reload
systemctl enable gamefinder >/dev/null
t0="$(date '+%Y-%m-%d %H:%M:%S')"
systemctl restart gamefinder
# Wait for the bot to really poll Telegram, not just for the process to exist.
for _ in $(seq 1 30); do
  if journalctl -u gamefinder --since "$t0" --no-pager -o cat 2>/dev/null | grep -q "Run polling"; then
    systemctl --no-pager --lines=5 status gamefinder
    exit 0
  fi
  sleep 1
done
echo "!! the bot did not start polling within 30 s:"
journalctl -u gamefinder --since "$t0" --no-pager -o cat | tail -40
exit 1
