#!/bin/bash
# Prompts for each secret without echoing it and writes it to .env (chmod 600).
# Press Enter to skip a key or keep its current value. Values are never printed.
set -euo pipefail
cd "$(dirname "$0")/.."
ENV=.env
[ -f "$ENV" ] || cp .env.example "$ENV"
chmod 600 "$ENV"

set_key() {
  local k="$1" v="$2" tmp
  tmp=$(mktemp)
  if grep -q "^${k}=" "$ENV"; then
    awk -v k="$k" -v v="$v" 'BEGIN{FS=OFS="="} $1==k{print k"="v; next} {print}' "$ENV" > "$tmp"
  else
    cat "$ENV" > "$tmp"; printf '%s=%s\n' "$k" "$v" >> "$tmp"
  fi
  mv "$tmp" "$ENV"; chmod 600 "$ENV"
}

KEYS="MESHY_API_KEY MESHY_TEST_API_KEY ANTHROPIC_API_KEY HF_TOKEN RUNPOD_API_KEY SUPABASE_SERVICE_ROLE_KEY"
for k in ${1:-$KEYS}; do
  cur=$(grep "^${k}=" "$ENV" | cut -d= -f2- || true)
  state=$([ -n "$cur" ] && echo "set" || echo "empty")
  printf '%s [%s]: ' "$k" "$state"
  IFS= read -rs val; echo
  [ -n "$val" ] && set_key "$k" "$val" && echo "  saved"
done
echo; echo "Status (names only):"
for k in $KEYS; do grep -q "^${k}=..*" "$ENV" && echo "  $k: set" || echo "  $k: empty"; done
