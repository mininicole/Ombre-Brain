#!/bin/sh
# Install the Evan Memory loopback candidate on Oracle.
# usage (as root): install-candidate.sh <source.tgz> <rehearsal_root> <wheelhouse>
# - source.tgz: git archive of migration/evan-oracle
# - rehearsal_root: /var/lib/evan-ombre-rehearsal/<ts> (read-only input, never modified)
# - wheelhouse: directory with the pinned ARM64 wheels
set -eu

SRC_TGZ=$1
REHEARSAL=$2
WHEELS=$3
REL=$(date -u +%Y%m%dT%H%M%SZ)
CAND=/var/lib/evan-memory-candidate

test -f "$SRC_TGZ"
test -d "$REHEARSAL/data"
test -d "$WHEELS"
if systemctl is-active --quiet evan-memory-candidate; then
  echo "candidate is running; stop it first" >&2
  exit 1
fi

id evan-memory >/dev/null 2>&1 || useradd --system --home-dir /var/lib/evan-memory --shell /usr/sbin/nologin evan-memory

# --- code release ---
install -d -o root -g root -m 0755 /opt/evan-memory /opt/evan-memory/releases
install -d -o root -g root -m 0755 "/opt/evan-memory/releases/$REL"
tar -xzf "$SRC_TGZ" -C "/opt/evan-memory/releases/$REL" --no-same-owner
install -o root -g evan-memory -m 0640 "/opt/evan-memory/releases/$REL/deploy/oracle/evan-memory-config.yaml" "/opt/evan-memory/releases/$REL/config.yaml"
chmod -R go-w "/opt/evan-memory/releases/$REL"
ln -sfn "/opt/evan-memory/releases/$REL" /opt/evan-memory/current.new
mv -T /opt/evan-memory/current.new /opt/evan-memory/current

# --- venv (offline, pinned) ---
if [ ! -x /opt/evan-memory/venv/bin/python ]; then
  python3.12 -m venv /opt/evan-memory/venv
  /opt/evan-memory/venv/bin/pip install -q --no-index --find-links "$WHEELS" \
    -r /opt/evan-memory/current/requirements-oracle-arm64.txt \
    -c /opt/evan-memory/current/constraints-oracle-arm64.txt
  chown -R root:evan-memory /opt/evan-memory/venv
  chmod -R o-rwx /opt/evan-memory/venv
fi

# --- candidate data: fresh copy of the rehearsal restore ---
if [ -e "$CAND" ]; then
  mv "$CAND" "$CAND.prev-$REL"
fi
install -d -o evan-memory -g evan-memory -m 0700 "$CAND"
cp -a "$REHEARSAL/data" "$CAND/data"
# Old Fly embeddings came from a different model and are disabled there;
# keep them aside, never mix with BGE-M3 vectors.
if [ -f "$CAND/data/embeddings.db" ]; then
  mv "$CAND/data/embeddings.db" "$CAND/data/embeddings.legacy-fly.db"
fi
chown -R evan-memory:evan-memory "$CAND"
chmod -R u+rwX,go-rwx "$CAND"

# --- env (candidate-only local token; no production secrets) ---
if [ ! -f /etc/evan-memory-candidate.env ]; then
  umask 077
  printf 'OMBRE_AUTH_TOKEN=%s\n' "$(python3 -c 'import secrets; print(secrets.token_hex(32))')" > /etc/evan-memory-candidate.env
  chown root:evan-memory /etc/evan-memory-candidate.env
  chmod 0640 /etc/evan-memory-candidate.env
fi

install -o root -g root -m 0644 /opt/evan-memory/current/deploy/oracle/evan-memory-candidate.service /etc/systemd/system/evan-memory-candidate.service
systemctl daemon-reload
echo "release=$REL"
