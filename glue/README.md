# Glue service

The glue service is the small Python app that ties Rocket.Chat and
OpenClaw together. It is where the actual youragentnetwork behavior lives.

## What it does (eventually)

1. Receives an outgoing webhook from Rocket.Chat when a message is
   posted in a channel where an agent should respond.
2. Decides whether to engage (private channel: always; public channel:
   only if the agent's handle is @-tagged in the message).
3. Calls OpenClaw to generate the agent's response, using the right
   memory scope for the channel context (the "hard wall" between
   private and team memory lives here).
4. Posts the response back into the same Rocket.Chat channel as the
   agent's bot user, via the Rocket.Chat REST API.

## What it does right now

This version is a **skeleton**. It exposes two endpoints:

- `GET /health` returns `{"status": "ok"}` when the process is up.
- `POST /webhook` accepts a JSON payload, logs it, returns
  `{"received": true}`.

That is intentionally the smallest thing we can verify end-to-end. We
add the OpenClaw call and the response-posting in subsequent commits.

## Running it locally during development

The service is wired into the main docker-compose stack
(`infra/rocketchat/docker-compose.yml`). To bring it up:

```bash
cd ~/youragentnetwork/infra/rocketchat
docker compose up -d --build glue
docker compose logs -f glue
```

Then from your laptop, with the SSH tunnel open (`-L 8000:127.0.0.1:8000`),
verify it from a browser or curl:

```bash
curl http://localhost:8000/health
# -> {"status":"ok"}

curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d '{"text":"hello"}'
# -> {"received":true}
# (look at the glue logs and you should see the payload printed)
```

## Configuring Rocket.Chat to call it

In the Rocket.Chat admin UI:

1. Go to **Admin -> Integrations -> New**.
2. Choose **Outgoing Webhook**.
3. **Event Trigger:** Message Sent.
4. **Channel:** leave blank to receive ALL channels, or specify a test
   channel like `#smoke-test-channel` to limit scope.
5. **URLs:** `http://glue:8000/webhook` (this works because Rocket.Chat
   and the glue container share the docker-compose network).
6. Save.

Send a message in the configured channel and watch
`docker compose logs -f glue`. You should see the payload appear.
