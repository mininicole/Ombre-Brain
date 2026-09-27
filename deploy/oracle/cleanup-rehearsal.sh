#!/usr/bin/env bash
# Remove Evan Ombre migration rehearsal leftovers on Oracle (2026-09-27).
# Kept on purpose: the final cutover restore /var/lib/evan-ombre-rehearsal/20260927T082623Z
# (delete together with Fly after the observation period), the current and previous
# code release, and evan-provider-candidate (unrelated, still in use).
set -euo pipefail
KEY=/e/Oracle/.ssh-active/gale-vps.key
VPS=ubuntu@146.181.24.74

ssh -i "$KEY" -o BatchMode=yes "$VPS" 'sudo bash -s' <<'REMOTE'
set -euo pipefail
shopt -s nullglob
targets=(
  /var/lib/evan-memory-candidate
  /var/lib/evan-memory-candidate.prev-*
  /var/lib/evan-memory/data.pretest-*
  /var/lib/evan-ombre-rehearsal/20260926T104217Z
  /etc/evan-memory-candidate.env
  /etc/systemd/system/evan-memory-candidate.service
  /etc/systemd/system/evan-chatnest-candidate.service
  /tmp/evan-ombre-test /tmp/evan-ombre-venv /tmp/evan-wheels /tmp/evan-inst
  /tmp/evan-flyfix-test /tmp/evan-test-home /tmp/evan-release.tgz /tmp/evan-src.tgz
  /tmp/smoke_evan.py /tmp/verify_evan.py /tmp/verify_evan_restore.py /tmp/probe.py
  /tmp/evan-bot-server.mjs /tmp/evan-memory-keeper.mjs /tmp/evan-stack-backup.new
)
current=$(readlink -f /opt/evan-memory/current)
mapfile -t releases < <(ls -1d /opt/evan-memory/releases/*/ | sed 's#/$##' | sort)
keep_prev=""
for ((i=${#releases[@]}-1; i>=0; i--)); do
  if [[ ${releases[i]} == "$current" && i -gt 0 ]]; then keep_prev=${releases[i-1]}; fi
done
for r in "${releases[@]}"; do
  [[ $r == "$current" || $r == "$keep_prev" ]] || targets+=("$r")
done

if systemctl is-active --quiet evan-memory-candidate evan-chatnest-candidate; then
  echo "candidate still running, aborting"; exit 1
fi
echo "Will delete:"
existing=()
for t in "${targets[@]}"; do [[ -e $t ]] && existing+=("$t") && du -sh "$t" | sed 's/^/  /'; done
rm -rf -- "${existing[@]}"
systemctl daemon-reload
echo "Kept: current release $(basename "$current"), previous $(basename "${keep_prev:-none}"), final restore 20260927T082623Z"
echo "Deleted ${#existing[@]} items. Disk now:"; df -h / | tail -1
systemctl is-active evan-memory evan-chatnest evan-bot evan-provider-candidate
REMOTE
