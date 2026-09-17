#!/usr/bin/env bash
# scandbox self-wiring entrypoint (shared across all harness images).
#
# Reads the baked-in manifest (/etc/scandbox/harness.json), configures the agent from the
# recorder environment scandbox-run injects, then exec's the agent command + "$@".
# Per-harness differences are DATA (the manifest), not code---one entrypoint for every harness.
set -euo pipefail

MANIFEST="${SCANDBOX_MANIFEST:-/etc/scandbox/harness.json}"
[ -r "$MANIFEST" ] || { echo "scandbox: no manifest at $MANIFEST" >&2; exit 1; }

model_config=$(jq -r '.model_config // ""' "$MANIFEST")
mapfile -t cmd < <(jq -r '.command[]?' "$MANIFEST")
[ "${#cmd[@]}" -gt 0 ] || { echo "scandbox: manifest has no command" >&2; exit 1; }

host="${LLAMA_HOST:-scandbox-gateway}"
port="${LLAMA_PORT:-8080}"
base="http://${host}:${port}"

case "$model_config" in
  pi_models_json)
    # pi's llamacpp provider needs ~/.pi/agent/models.json---probe the recorder and write it.
    auth=(); [ -n "${LLAMA_API_KEY:-}" ] && auth=(-H "Authorization: Bearer ${LLAMA_API_KEY}")
    for _ in $(seq 1 20); do curl -sf "${auth[@]}" "$base/props" >/dev/null 2>&1 && break; sleep 0.3; done
    props=$(curl -sf "${auth[@]}" "$base/props" 2>/dev/null || echo '{}')
    models=$(curl -sf "${auth[@]}" "$base/v1/models" 2>/dev/null || echo '{}')
    ctx=$(printf '%s' "$props" | jq -r '.default_generation_settings.n_ctx // 4096')
    slots=$(printf '%s' "$props" | jq -r '.total_slots // 1')
    [ "$slots" -gt 0 ] 2>/dev/null || slots=1
    mid=$(printf '%s' "$models" | jq -r '.data[0].id // "model"' | xargs basename)
    perctx=$(( ctx / slots ))
    mkdir -p "$HOME/.pi/agent"
    cfg="$HOME/.pi/agent/models.json"
    cur='{}'; [ -f "$cfg" ] && jq empty "$cfg" >/dev/null 2>&1 && cur=$(cat "$cfg")   # merge, don't clobber
    printf '%s' "$cur" | jq --arg url "$base/v1" --arg id "$mid" --argjson ctx "$perctx" --arg key "${LLAMA_API_KEY:-scandbox}" '
      .providers = (.providers // {})
      | .providers.llamacpp = {baseUrl:$url, api:"openai-completions", apiKey:$key,
          models:[{id:$id, name:$id, contextWindow:$ctx, maxTokens:($ctx/4), reasoning:true,
            input:["text","image"],
            compat:{thinkingFormat:"chat-template", supportsDeveloperRole:false,
                    supportsReasoningEffort:true}}]}' \
      > "$cfg.tmp" && mv "$cfg.tmp" "$cfg"
    cmd+=(--model "$mid")
    echo "scandbox: wired pi -> $base/v1 (model $mid, ctx $perctx)" >&2
    ;;
  ""|none)
    # openai / anthropic harnesses: the base-URL env scandbox-run set is enough; nothing to write.
    :
    ;;
  *)
    echo "scandbox: unknown model_config '$model_config' in $MANIFEST" >&2; exit 1
    ;;
esac

# --shell: the agent is already wired; drop to an interactive (job-control) shell so you can
# launch the agent as a CHILD job---then Ctrl+Z suspends it back to this shell, exactly like
# running pi locally. `fg` resumes it.
if [ -n "${SCANDBOX_SHELL:-}" ]; then
  echo "scandbox: wired. launch the agent with:  ${cmd[*]}" >&2
  exec bash -i
fi

exec "${cmd[@]}" "$@"
