#!/bin/bash
# One command to switch on the LLM analyst:   sudo /opt/paper-agent/scripts/enable-llm.sh [anthropic|github]
# Stores the key in /etc/paper-agent/market-data.env (survives auto-updates), turns the feature on, and runs a live test.
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo"; exit 1; }
PROV="${1:-anthropic}"
case "$PROV" in
  anthropic) VAR=ANTHROPIC_API_KEY;  HINT="console.anthropic.com -> API keys; set a monthly spend limit there too" ;;
  github)    VAR=GITHUB_MODELS_TOKEN; HINT="github.com/settings/personal-access-tokens, fine-grained, 'Models: read'. Free tier is rate-limited (see docs/INTELLIGENCE.md)" ;;
  *) echo "usage: $0 [anthropic|github]"; exit 1 ;;
esac
read -rsp "Paste your $VAR ($HINT): " KEY; echo
[[ -n "$KEY" ]] || { echo "no key entered"; exit 1; }
ENVF=/etc/paper-agent/market-data.env; touch "$ENVF"
{ grep -v "^$VAR=" "$ENVF" || true; echo "$VAR=$KEY"; } > "$ENVF.tmp"
install -m0640 -o root -g paperagent "$ENVF.tmp" "$ENVF"; rm -f "$ENVF.tmp"
read -rp "Your email for the SEC User-Agent (SEC asks for a real contact; press Enter to skip): " EMAIL || true
python3 - "$PROV" "${EMAIL:-}" <<'PY'
import json, sys
p = "/etc/paper-agent/config.json"
try:
    c = json.load(open(p))
except (OSError, ValueError):
    c = {}
i = c.setdefault("intelligence", {})
i["llm_enabled"] = True
if len(sys.argv) > 2 and "@" in sys.argv[2]:
    c.setdefault("runtime", {})["contact_email"] = sys.argv[2]
    print("config updated: runtime.contact_email set")
if sys.argv[1] == "github":     # model ids are provider-specific; adjust here if the test below fails
    i["llm_tiers"] = {"triage": {"provider": "github", "model": "openai/gpt-4.1-mini"}, "analyst": {"provider": "github", "model": "openai/gpt-4.1"}}
json.dump(c, open(p, "w"), indent=2)
print("config updated: intelligence.llm_enabled = true")
PY
echo "Running a live test call..."
sudo -u paperagent env PYTHONPATH=/opt/paper-agent python3 -m paper_agent llm-test && echo "OK. The next cycle (within 10 minutes) starts using it. Check spend on the dashboard System tab." \
  || echo "TEST FAILED: paste the output above to Claude."
