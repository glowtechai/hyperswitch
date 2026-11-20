#!/usr/bin/env bash
# Reproducible, loopback-only quickstart using the fork's config and migrations.
set -euo pipefail

REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$REPO_ROOT"
COMPOSE=(docker compose -f deploy/demo.compose.yml)
EVIDENCE_DIR="$REPO_ROOT/.run-evidence"
DIESEL_VERSION=2.3.5
DIESEL_SHA256=264d6f71426d9cf25ce389d1669ef9a18df0bc8f1807736be76a757843ee1645

fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }

require_runtime() {
    for command in docker curl jq python3 sha256sum tar; do
        command -v "$command" >/dev/null || fail "Missing prerequisite: $command"
    done
    [[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || \
        fail 'This launcher currently requires Linux x86_64.'
    python3 -c 'import tomllib' || fail 'Python 3.11+ is required.'
    docker compose version >/dev/null || fail 'Docker Compose v2 is required.'
    docker info >/dev/null || fail 'A running Docker engine is required.'
}

capture_images() {
    "${COMPOSE[@]}" config --images | sort -u | while IFS= read -r image; do
        docker image inspect "$image" \
            --format '{"reference":"{{index .RepoTags 0}}","id":"{{.Id}}","digests":{{json .RepoDigests}}}'
    done | jq -s . > "$EVIDENCE_DIR/images.json"
}

prepare_demo_config() {
    # The current standalone image predates the fork's Ecommpay connector.
    # Omit that unsupported mandate entry only in this generated demo config.
    # Preserve the tracked source configuration for builds from Rust source.
    python3 - <<'PY'
import json, pathlib, re, tomllib
source = pathlib.Path('config/docker_compose.toml').read_text()
removed = 0
def compatible_list(match):
    global removed
    values = match[2].split(',')
    supported = [value for value in values if value.strip() != 'ecommpay']
    removed += len(values) - len(supported)
    return match[1] + ','.join(supported) + match[3]
result = re.sub(r'(connector_list\s*=\s*")([^"\n]*)(")', compatible_list, source)
tomllib.loads(result)
pathlib.Path('.demo-tools/router.toml').write_text(result)
pathlib.Path('.run-evidence/configuration.json').write_text(json.dumps({
    'source': 'config/docker_compose.toml',
    'demo_only_omitted_connector': 'ecommpay',
    'omitted_mandate_entries': removed,
    'reason': 'The upstream standalone image rejects this newer connector enum.'
}, indent=2) + '\n')
PY
}

start_demo() {
    require_runtime
    mkdir -p "$EVIDENCE_DIR" .demo-tools
    prepare_demo_config
    "${COMPOSE[@]}" config --quiet
    "${COMPOSE[@]}" pull
    capture_images
    "${COMPOSE[@]}" run --rm --no-deps hyperswitch-server \
        /local/bin/router --version | tee "$EVIDENCE_DIR/router-version.txt"
    "${COMPOSE[@]}" up -d --wait --wait-timeout 150 pg redis-standalone superposition

    # The upstream Compose file invokes this Bash script with Alpine sh.
    # Use Bash explicitly and retain all of its seed validation checks.
    SUPERPOSITION_URL=http://localhost:8081 \
        SEED_FILE=./config/superposition_seed.toml \
        bash scripts/seed_superposition.sh

    local archive="$REPO_ROOT/.demo-tools/diesel-${DIESEL_VERSION}.tar.xz"
    curl --fail --show-error --silent --location --retry 3 \
        "https://github.com/diesel-rs/diesel/releases/download/v${DIESEL_VERSION}/diesel_cli-x86_64-unknown-linux-gnu.tar.xz" \
        --output "$archive"
    printf '%s  %s\n' "$DIESEL_SHA256" "$archive" | sha256sum --check -
    tar -xJf "$archive" -C .demo-tools
    local diesel
    diesel=$(find .demo-tools -type f -name diesel -print -quit)
    [[ -n "$diesel" ]] || fail 'Verified Diesel archive did not contain the binary.'
    "$diesel" --version
    # An empty CLI config prevents migration tooling from rewriting tracked schema.rs.
    printf '' > .demo-tools/diesel.toml
    "$diesel" migration run \
        --database-url postgresql://db_user:db_pass@localhost:5432/hyperswitch_db \
        --migration-dir migrations --config-file .demo-tools/diesel.toml

    "${COMPOSE[@]}" up -d --wait --wait-timeout 180 hyperswitch-server
    "${COMPOSE[@]}" up -d hyperswitch-web hyperswitch-control-center
    for url in http://localhost:9000/ http://localhost:9050/HyperLoader.js; do
        curl --fail --show-error --silent --retry 20 --retry-connrefused \
            --retry-delay 2 --max-time 10 "$url" --output /dev/null
    done
    python3 scripts/verify_demo.py
    printf '\nDemo verified. Dashboard: http://localhost:9000\nAPI: http://localhost:8080\n'
}

case "${1:-up}" in
    up) start_demo ;;
    check) python3 scripts/verify_demo.py ;;
    status) "${COMPOSE[@]}" ps ;;
    logs) "${COMPOSE[@]}" logs --no-color --tail=100 ;;
    down) "${COMPOSE[@]}" down ;;
    *) fail 'Usage: bash scripts/run_demo.sh [up|check|status|logs|down]' ;;
esac
