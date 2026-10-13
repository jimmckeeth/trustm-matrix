#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build a NINA (ESP32 WiFi co-processor) firmware whose certificate bundle also
# trusts the intermediate CA that beacon.inmetro.gov.br forgets to send.
# See inmetro.md for the problem, the reasoning, and how to flash the result.
#
#   ./inmetro.sh            builds into ./nina-build (override with WORKDIR=...)
#
# Needs: curl, openssl, git, and podman or docker (for the ESP-IDF toolchain).
# Output: $WORKDIR/nina-fw/NINA_ADAFRUIT-esp32-<version>.bin   (flash at 0x0)

set -euo pipefail

HOST=beacon.inmetro.gov.br
NINA_TAG=3.3.0
IDF_IMAGE=docker.io/espressif/idf:v5.5   # nina-fw 3.3.0 targets ESP-IDF v5.5
WORKDIR=${WORKDIR:-$(cd "$(dirname "$0")" && pwd)/nina-build}

die() { echo "error: $*" >&2; exit 1; }

for tool in curl openssl git; do
    command -v "$tool" >/dev/null || die "$tool is required"
done
if command -v podman >/dev/null; then
    CTR=podman
elif command -v docker >/dev/null; then
    CTR=docker
else
    die "podman or docker is required"
fi

mkdir -p "$WORKDIR"
cd "$WORKDIR"

# --- 1. get the intermediate the server doesn't send ------------------------
echo "== fetching $HOST leaf certificate"
echo | openssl s_client -connect "$HOST:443" -servername "$HOST" 2>/dev/null |
    openssl x509 -out leaf.pem
openssl x509 -in leaf.pem -noout -subject -issuer -dates

aia=$(openssl x509 -in leaf.pem -noout -ext authorityInfoAccess |
    sed -n 's/.*CA Issuers - URI:\(.*\)/\1/p' | head -n1)
[[ -n $aia ]] || die "leaf certificate has no CA Issuers (AIA) URL"
echo "== downloading intermediate from $aia"
curl -fsS -m 30 -o inter.crt "$aia"
openssl x509 -inform der -in inter.crt -out inter.pem 2>/dev/null ||
    openssl x509 -in inter.crt -out inter.pem

# --- 2. prove it is really the issuer of the leaf ---------------------------
echo "== checking it issued the leaf"
openssl x509 -in inter.pem -noout -subject -issuer -dates -ext basicConstraints
openssl verify -partial_chain -CAfile inter.pem leaf.pem >/dev/null ||
    die "downloaded certificate does not verify the INMETRO leaf; refusing to use it"
echo "ok"

# --- 3. nina-fw source ------------------------------------------------------
if [[ ! -d nina-fw ]]; then
    echo "== cloning adafruit/nina-fw $NINA_TAG"
    git clone -q --depth 1 --branch "$NINA_TAG" --recurse-submodules \
        --shallow-submodules https://github.com/adafruit/nina-fw.git nina-fw
fi

# --- 4. add the certificate to the bundle -----------------------------------
cp inter.pem nina-fw/extra_certs.pem
if ! grep -q CUSTOM_CERTIFICATE_BUNDLE nina-fw/sdkconfig.defaults; then
    cat >>nina-fw/sdkconfig.defaults <<'EOF'
CONFIG_MBEDTLS_CUSTOM_CERTIFICATE_BUNDLE=y
CONFIG_MBEDTLS_CUSTOM_CERTIFICATE_BUNDLE_PATH="/project/extra_certs.pem"
EOF
fi

# --- 5. build in the ESP-IDF container --------------------------------------
echo "== building with $CTR ($IDF_IMAGE); first run downloads several GB"
"$CTR" run --rm -v "$WORKDIR/nina-fw:/project:Z" -w /project "$IDF_IMAGE" \
    bash -c 'git config --global --add safe.directory "*"; idf.py -DBOARD=esp32 build'

# combine.py appends the firmware version to the name
shopt -s nullglob
bins=("$WORKDIR"/nina-fw/NINA_ADAFRUIT-esp32-*.bin)
bin=${bins[0]:-}
[[ -n $bin ]] || die "build finished but no NINA_ADAFRUIT-esp32-*.bin was produced"
echo
echo "built: $bin ($(stat -c %s "$bin") bytes)"
echo "flash it at 0x0 -- see inmetro.md, 'Flashing the ESP32 from the Matrix Portal'"
