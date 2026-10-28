#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

find . -type d -name __pycache__ -prune -exec rm -rf {} +
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=linux python3 -m unittest discover -s tests
find . -type d -name __pycache__ -prune -exec rm -rf {} +
desktop-file-validate packaging/ds-linux-balance-pet.desktop

rm -rf dist
mkdir -p dist

dpkg-buildpackage -b -us -uc
cp ../ds-linux-balance-pet_*_all.deb dist/
dpkg-deb -I "dist/$(basename "$(ls -t dist/ds-linux-balance-pet_*_all.deb | head -1)")"
dpkg-deb -c "dist/$(basename "$(ls -t dist/ds-linux-balance-pet_*_all.deb | head -1)")" | sed -n '1,80p'

debian/rules clean >/dev/null 2>&1 || true
rm -rf debian/.debhelper debian/ds-linux-balance-pet debian/files debian/*.substvars debian/debhelper-build-stamp
printf '\nBuilt: %s\n' "$ROOT/dist/$(basename "$(ls -t dist/ds-linux-balance-pet_*_all.deb | head -1)")"
