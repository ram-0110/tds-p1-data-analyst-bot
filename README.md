# TDS Project 1 — Data Analyst Telegram Bot

This bot:

- polls Telegram without a webhook;
- keeps per-chat context for multi-turn questions;
- uses local Ollama `qwen2.5:3b`;
- can search, fetch public data, and run constrained calculations;
- replies with exactly `{"answer": ..., "log_url": "..."}`;
- serves one-object-per-line public run logs.

## Configuration

Set:

- `BOT_TOKEN` — token from BotFather.
- `PUBLIC_BASE_URL` — HTTPS base URL of the public tunnel, with no trailing slash.
- `OLLAMA_MODEL` — defaults to `qwen2.5:3b`.
- `PORT` — defaults to `8765`.

These may be environment variables or entries in an uncommitted `.env` file:

```text
BOT_TOKEN=123456:replace_with_botfather_token
PUBLIC_BASE_URL=https://replace-after-tunnel.example
```

Run `python app.py`. The health check is at `/health`.

Never commit the BotFather token or any `.env` file.
