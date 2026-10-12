# llm-proxy

A lightweight LLM reverse proxy built with Starlette and httpx. It routes requests to multiple upstream providers, rewrites model names on the fly, and adds optional MCP, image, and status-page features — all configured from a single `config.yaml`.

## Features

- **Multi-provider routing with model rewriting** — clients select a provider by prefixing their API key with its identifier (`Authorization: Bearer [glm]your-real-key`). Model names are mapped to provider-native names via exact entries or regex patterns; exact mappings are advertised under `/v1/models`.
- **MCP reverse proxy** (optional) — mounts `/mcp` and injects auth headers before forwarding, so MCP servers need no API key.
- **Image proxy** (optional) — forwards `/v1/images/generations` and `/v1/images/edits` to an image upstream and records usage in a local SQLite database.
- **CCH status page** (optional) — serves a public `/cch/status` page with weekly quota cards, user/model leaderboards, a model price table, and coding-plan usage monitors (Kimi / GLM / MiniMax) that can auto-disable providers at quota.

## Requirements

- Python ≥ 3.13
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Installation

```bash
git clone <repo-url> llm_proxy
cd llm_proxy

# Install dependencies (creates .venv from uv.lock)
uv sync
```

Or with plain pip:

```bash
python3 -m venv .venv
.venv/bin/pip install .
```

## Configuration

```bash
cp config.yaml.example config.yaml
```

Edit `config.yaml` and set at minimum:

- `listen_host` / `listen_port` — where the proxy listens (default `0.0.0.0:8080`)
- `providers` — a list of upstreams, each with an `identifier`, `upstream_url`, and a `models` mapping

Optional sections (`mcp`, `image`, `proxy`, `cch_status`) are documented with comments in `config.yaml.example`; leave them out to disable the corresponding features.

## Running

From the project root (the directory containing `config.yaml`):

```bash
uv run python main.py
```

Then point your client at the proxy, e.g.:

```bash
curl http://localhost:8080/v1/messages \
  -H "Authorization: Bearer [glm]your-real-key" \
  -H "content-type: application/json" \
  -d '{"model": "claude-sonnet-4-6", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}'
```

`GET /v1/models` lists the exact model mappings available per provider.

### Run as a systemd service

Ready-made unit files are in [`systemd/`](systemd/). Adjust the paths and user in the unit file, then:

```bash
sudo cp systemd/llm-proxy.service /etc/systemd/system/
sudo systemctl enable --now llm-proxy
```

## Tests

```bash
uv run pytest
```
