# Run this fork's isolated Hyperswitch demo

This launcher starts the API, Control Center dashboard, web SDK, PostgreSQL,
Redis, and Superposition with this checkout's configuration and migrations.
It uses the upstream prebuilt runtime images from the project's quickstart;
it does **not** compile this fork's Rust source. The resolved image digests
are recorded under `.run-evidence/images.json` on each run.

## Start on a development machine

Requirements: Linux x86_64, a running Docker engine with Compose v2, Bash,
curl, jq, Python 3.11+, sha256sum, and tar with XZ support. The downloaded
official Diesel CLI release is verified against its published SHA-256 before
execution. No credentials for a real payment processor are needed.

```bash
git clone --branch codex/run-hyperswitch-demo https://github.com/glowtechai/hyperswitch.git
cd hyperswitch
bash scripts/run_demo.sh up
```

After the checks pass, open **http://localhost:9000** on that machine.
The API is at **http://localhost:8080** and the checkout SDK is served at
**http://localhost:9050/HyperLoader.js**.

```bash
bash scripts/run_demo.sh status
bash scripts/run_demo.sh check
bash scripts/run_demo.sh logs
bash scripts/run_demo.sh down
```

Stopping preserves the demonstration database. Ports bind only to loopback.
This configuration contains public example credentials and a mock card vault;
it is for a private development machine, not an internet-facing payment service.

## GitHub Actions

The `Run isolated Hyperswitch demo` workflow runs the same launcher on an
ephemeral GitHub-hosted Ubuntu runner. It requires no repository secrets and
gives its token read-only repository permission. Pushes affecting the launcher
on `codex/run-hyperswitch-demo` trigger it once Actions is enabled for the fork.
The workflow also supports manual dispatch when available in GitHub's UI.

The run retains sanitized check results and image digests as an artifact for
seven days, then shuts down its containers. **A successful Actions run is not
a persistent website or dashboard deployment.** Its localhost URLs are on the
temporary runner and cannot be opened from your phone or laptop.

## Scope of verification

The launcher retains independent checks instead of relying on the upstream
setup script's success banner. Check `.run-evidence/` for the actual outcome.
The readiness endpoint includes an outbound, credential-free probe to
`https://api.stripe.com/healthcheck` in the upstream application. No real
payment-processor credentials or merchant applications are used.

The upstream seed script is run with Bash, fixing its invocation in the default
Compose quickstart. The original telemetry hook containers and piped shell
installers are not started. The seed script still validates its own
configuration; an incompatible image must fail rather than be counted as ready.

## What this does not activate

This demonstration does not change Credo Aminos, WooCommerce, or an existing
merchant account. It does not establish AllayPay/NMI eligibility, settlement,
Apple Pay, or Google Pay. Production processing requires a supported processor
account, approved credentials, secure persistent hosting, and separate checkout
integration and testing.
