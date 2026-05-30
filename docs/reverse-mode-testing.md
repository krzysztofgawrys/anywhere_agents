# Testing reverse-worker mode (worker-copilot)

End-to-end smoke test for the inbound/reverse mode shipped in
`feat/reverse-worker-mode`. Validates: control channel registration,
data channel pairing per browser session, CF Access service token auth,
graceful reconnect.

## What you need before starting

1. Hub already running and reachable at a stable URL (e.g.
   `https://hub.example.com`). Browser SSO via CF Access still works as
   before - this work only adds two new endpoints on the hub:
   `/worker-register` and `/worker-data`.
2. CF Access service token created and a policy applied to the two new
   endpoints (see README "Cloudflare Access service tokens for inbound
   mode").
3. A machine to run worker-copilot from (laptop is fine for the first
   test - egress to hub is all we need).
4. Hub's `WORKER_SECRET` in your hand.

## Step 1 - hub configuration

Add an inbound entry to `workers.json` on the hub:

```json
[
  { "id": "local-claude", "type": "claude", "label": "Laptop",
    "url": "ws://worker-claude:8002/ws", "secret": "..." },

  { "id": "remote-copilot", "type": "copilot", "label": "Remote",
    "mode": "inbound", "secret": "<WORKER_SECRET>" }
]
```

Note: the old `local-copilot` outbound entry can stay alongside if you
want both - hub treats them as separate workers. For a clean first test
remove it.

Hub re-reads `workers.json` on every new browser WS connect, so no hub
restart is needed for the registry change to take effect. The new
`/worker-register` and `/worker-data` endpoints DO require a hub rebuild
(they're new code).

```bash
# On the host (rebuild only hub - worker-claude stays untouched)
docker compose up -d --build hub
```

## Step 2 - worker-copilot configuration

Create a `.env` file on the worker machine:

```bash
UID=1000
GID=1000

# Reverse mode
WORKER_MODE=inbound
HUB_PUBLIC_URL=https://hub.example.com   # publicly reachable hub URL
WORKER_ID=remote-copilot                 # must match workers.json
WORKER_LABEL=Remote
WORKER_TYPE=copilot
WORKER_SECRET=<same as hub>

# CF Access service token (omit if hub is not behind CF Access)
CF_ACCESS_CLIENT_ID=<from CF Dashboard>
CF_ACCESS_CLIENT_SECRET=<from CF Dashboard>
```

Uncomment the reverse-mode env block in `worker-copilot-compose.yml`:

```yaml
environment:
  - WORKER_MODE=inbound
  - HUB_PUBLIC_URL=${HUB_PUBLIC_URL:-https://hub.example.com}
  - WORKER_ID=${WORKER_ID:-remote-copilot}
  - WORKER_LABEL=${WORKER_LABEL:-Remote}
  - WORKER_TYPE=copilot
  - CF_ACCESS_CLIENT_ID=${CF_ACCESS_CLIENT_ID:-}
  - CF_ACCESS_CLIENT_SECRET=${CF_ACCESS_CLIENT_SECRET:-}
```

Optionally remove the `ports:` block to prove the worker really doesn't
need any inbound port. Leave it for now if you want `/health` reachable.

Start the worker:

```bash
docker compose -f worker-copilot-compose.yml up -d --build
```

## Step 3 - verify control channel registered

Hub logs should show, within seconds of worker startup:

```
worker_control_registered worker_id=remote-copilot type=copilot
```

Worker logs should show:

```
hub_control_dialing url=wss://hub.example.com/worker-register worker_id=remote-copilot
hub_control_registered worker_id=remote-copilot
```

If you don't see registration:

- `worker_register_auth_rejected` on hub: `WORKER_SECRET` mismatch.
- 403 from CF / no log on hub at all: CF Access service token missing
  or wrong policy. Check `CF_ACCESS_CLIENT_ID/SECRET` and that the
  policy on `/worker-register` allows your service token.
- `worker_register_unknown_or_wrong_mode`: `WORKER_ID` doesn't match a
  workers.json entry, or that entry is `mode: outbound`. Check spelling.
- `worker_register_bad_handshake`: protocol mismatch - file an issue.

## Step 4 - verify data channels open on browser connect

Open the hub in a browser, log in (SSO), confirm the "Remote" worker
shows up in the worker filter dropdown as **connected**.

Hub logs on browser connect:

```
worker_connect_failed worker=remote-copilot mode=inbound error=...    # one and done if control not yet registered
...
worker_control_registered ...
worker_retry_succeeded worker=remote-copilot                          # after the retry tick
```

OR, if the worker registered first:

```
hub_data_session_open worker_id=remote-copilot session_id=<uuid>      # worker side
worker_connected url=inbound://remote-copilot mode=inbound            # hub side
```

Now try a smoke prompt against the copilot worker - start a new session,
send a prompt, watch tokens stream. Tool calls, file browser, terminal,
permissions - all should behave identically to outbound mode.

## Step 5 - verify reconnect behavior

Kill the worker container:

```bash
docker compose -f worker-copilot-compose.yml stop worker-copilot
```

Hub should show:
- `worker_control_dropped worker_id=remote-copilot`
- Browser sees the worker as offline in the sidebar within a second.

Start it again:

```bash
docker compose -f worker-copilot-compose.yml start worker-copilot
```

Within 1-30s (depends on hub's per-WS retry interval), browser sees the
worker reconnect; you can start new sessions against it again.

## Step 6 - verify network isolation actually works

Most important - the whole point of this mode. On the worker machine:

```bash
# In server mode this would expose port 8003. In inbound mode it doesn't
# need to. Verify the worker has no listening TCP socket bound to
# anything besides loopback:
docker compose -f worker-copilot-compose.yml exec worker-copilot ss -tlnp
```

You should see nothing on 8003 (or only loopback `127.0.0.1:8003`).

If you removed the `ports:` block, also verify from the host:

```bash
ss -tlnp | grep 8003     # should be empty
nc -zv localhost 8003    # should fail (Connection refused)
```

The container is talking to the hub purely through its outbound 443.
No firewall holes opened, no port-forward configured, no VPN.

## Known limitations

- One control channel per worker_id. If the same worker_id registers a
  second time (e.g. you accidentally start two copies), the older
  control is dropped with `worker_control_replaced`.
- Hub is one process. Reverse mode does not work across multiple hub
  replicas behind a load balancer - the worker's control channel lives
  on one specific replica and only that replica can route browsers to
  it. Single-process hub is documented as a deliberate constraint.
- No automatic worker_id collision detection across the workers.json -
  two inbound entries with the same id will fight for the slot. Don't
  do that.
