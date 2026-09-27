#!/usr/bin/env bash
# Evan Ombre: copy the needed Fly ombre-brain secrets straight into an
# Oracle root-only env file. Values only travel through the pipe: nothing is
# written on Windows and nothing is printed; the VPS side prints key names
# and counts only.
#
# Not transferred on purpose:
#   CLAUDE_CODE_OAUTH_TOKEN  old account; copied from /etc/evan-bot.env.json on the VPS instead
#   GALE_MCP_SLUG            Gale proxy is off on Oracle
#   EVAN_SEND_URL            set to loopback in the systemd unit
#
# Result: /etc/evan-memory.env (root:evan-memory 0640), KEY=VALUE lines.
set -euo pipefail
export PATH="$PATH:/c/Users/Administrator/.fly/bin"

KEY=/e/Oracle/.ssh-active/gale-vps.key
VPS=ubuntu@146.181.24.74
NAMES="OMBRE_API_KEY,OMBRE_AUTH_TOKEN,OMBRE_DEHYDRATION_MODEL,NIGHT_FALL_API_KEY,NIGHT_FALL_BASE_URL,NIGHT_FALL_MODEL,DEEPSEEK_API_KEY,GIST_TOKEN,STATE_GIST_URL,EVAN_SEND_SECRET,GALE_SEND_SECRET,CHAT_SECRET,CHAT_PASSWORD,AUTH_MODE,CHAT_DISABLE_SUMMARIES,CHAT_COMPACT_AFTER_TURNS,CHAT_COMPACT_KEEP_TURNS"

PY="import os,sys,json;n=sys.argv[1].split(',');sys.stdout.write(json.dumps({k:os.environ[k] for k in n if k in os.environ})+'\n')"
B64=$(printf '%s' "$PY" | base64 -w0)

fly machine start 1854407b502168 -a ombre-brain >/dev/null 2>&1 || true

# flyctl on Windows may end with "The handle is invalid" after the data is
# complete; the VPS-side check below decides whether the result is usable.
MSYS_NO_PATHCONV=1 fly ssh console -a ombre-brain -C "python3 -c \"import base64;exec(base64.b64decode('$B64'))\" $NAMES" 2>/dev/null \
  | ssh -i "$KEY" -o BatchMode=yes "$VPS" "sudo sh -c 'umask 077; cat > /etc/evan-memory.env.incoming'" || true

ssh -i "$KEY" -o BatchMode=yes "$VPS" "sudo python3 - '$NAMES'" <<'PY'
import grp, json, os, sys
names = sys.argv[1].split(',')
incoming = '/etc/evan-memory.env.incoming'
raw = open(incoming, encoding='utf-8').read()
data = json.loads(raw[raw.find('{'):].strip().splitlines()[0])
os.remove(incoming)
bot = json.load(open('/etc/evan-bot.env.json', encoding='utf-8'))
if bot.get('CLAUDE_CODE_OAUTH_TOKEN'):
    data['CLAUDE_CODE_OAUTH_TOKEN'] = bot['CLAUDE_CODE_OAUTH_TOKEN']
missing = [n for n in names if n not in data]
empty = [k for k, v in data.items() if not str(v).strip()]
bad = [k for k, v in data.items() if '\n' in str(v) or '\r' in str(v)]
print(f'keys={len(data)} (expected {len(names)} + CLAUDE_CODE_OAUTH_TOKEN from evan-bot)')
print('missing:', missing or 'none')
print('empty:', empty or 'none')
print('multi-line (not allowed):', bad or 'none')
if 'CLAUDE_CODE_OAUTH_TOKEN' not in data:
    print('WARNING: evan-bot has no CLAUDE_CODE_OAUTH_TOKEN')
if missing or bad:
    print('NOT saved')
    sys.exit(1)
out = '/etc/evan-memory.env'
with open(out, 'w', encoding='utf-8') as f:
    for k in sorted(data):
        v = str(data[k]).replace('\\', '\\\\').replace('"', '\\"')
        f.write(f'{k}="{v}"\n')
os.chown(out, 0, grp.getgrnam('evan-memory').gr_gid)
os.chmod(out, 0o640)
print('saved', out)
PY
