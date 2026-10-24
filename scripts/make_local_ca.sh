#!/usr/bin/env bash
# Create a clinic-local certificate authority and a server certificate so phones
# can use the camera over trusted HTTPS on the LAN. Install ca.crt on each
# phone (iOS: Settings > General > About > Certificate Trust Settings).
# Prefer an institution-managed certificate when one is available.
#
#   scripts/make_local_ca.sh 192.168.1.20 medassist.local
set -euo pipefail
OUT="${OUT:-$HOME/.local/share/medassist/tls}"
mkdir -p "$OUT"
chmod 700 "$OUT"
cd "$OUT"
[ $# -ge 1 ] || { echo "usage: $0 <lan-ip> [hostname...]"; exit 1; }

if [ ! -f ca.key ]; then
  openssl req -x509 -newkey rsa:3072 -sha256 -days 1825 -nodes -keyout ca.key -out ca.crt \
    -subj "/CN=MedAssist Clinic Local CA"
  chmod 600 ca.key
fi

SAN="IP:$1"
shift
for h in "$@"; do SAN="$SAN,DNS:$h"; done
openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr -subj "/CN=medassist"
printf "subjectAltName=%s\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n" "$SAN" > ext.cnf
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt -days 397 -sha256 -extfile ext.cnf
chmod 600 server.key
rm -f server.csr ext.cnf
echo "CA (install on phones): $OUT/ca.crt"
echo "Run: HOST=0.0.0.0 TLS_CERT=$OUT/server.crt TLS_KEY=$OUT/server.key MEDASSIST_ALLOWED_ORIGINS=https://$SAN scripts/run.sh"
