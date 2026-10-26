#!/usr/bin/env bash
# Build the extras of the v9 Vis profiles into artifacts/vis-extras/.
# Usage: vis-extras/build.sh <vis-agent-linux-x64.tar.gz>
# It needs Docker or Podman with linux/amd64. Pin the printed sha256 values in profiles.toml.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
out="$here/../artifacts/vis-extras"
mkdir -p "$out"
cp "$1" "$out/vis.tgz"
docker run --rm --platform linux/amd64 -v "$out:/x" -v "$here:/s:ro" python:3.12-slim bash -s <<'EOF'
set -e
apt-get update -qq >/dev/null && apt-get install -y -qq ca-certificates git >/dev/null 2>&1
mkdir -p /installed-agent && tar -xzf /x/vis.tgz -C /installed-agent
V=/installed-agent/vis-agent
$V python -m pip install -q --disable-pip-version-check --root-user-action=ignore \
  --target /installed-agent/python-packages -r /s/python-packages.lock
export HOME=/tmp/ctb-home VIS_HOME=/tmp/ctb-home/.vis
mkdir -p $VIS_HOME
$V extension install Blockether/vis-lang-python --version 1.14.0 --global --trust
# One start prepares the extension environment, with ruff and pytest.
timeout 300 $V --db :memory -- x >/dev/null 2>&1 || true
grep -h 'loaded Python extension' $VIS_HOME/logs/*/*.log
tar -czf /x/python-packages.tgz -C /installed-agent python-packages
tar -czf /x/vis-extensions.tgz -C $VIS_HOME extensions
EOF
rm "$out/vis.tgz"
sha256sum "$out/python-packages.tgz" "$out/vis-extensions.tgz" 2>/dev/null || shasum -a 256 "$out"/*.tgz
