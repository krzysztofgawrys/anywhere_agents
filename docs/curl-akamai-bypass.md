# `fetch` - getting agents past Akamai/Cloudflare bot-mitigation

## Symptom

An agent tries to download a datasheet / page with `curl` and gets nothing:

```
$ curl https://www.analog.com/.../ad7380-7381.pdf
curl: (92) HTTP/2 stream 1 was not closed cleanly: INTERNAL_ERROR (err 2)
# or, with --http1.1:
curl: (28) Operation timed out ... 0 bytes received
```

It looks like the container can't reach the internet. **It can.** In the same
container, `curl https://www.google.com` returns `200`. The block is specific to
certain hosts (analog.com, mouser.com, many vendor sites).

## Root cause - it is NOT an egress block

The destination sits behind **Akamai** (or Cloudflare) bot-mitigation. Trace of a
request to analog.com:

1. TCP connect to the CDN edge - OK.
2. TLS 1.3 handshake - completes, certificate valid, connection encrypted.
3. We send `GET ... HTTP/2`.
4. The edge immediately sends `RST_STREAM: INTERNAL_ERROR` (or just never
   answers on HTTP/1.1).

The connection is fine; the server *chooses* to drop us. It fingerprints the
client before responding:

- **TLS + HTTP/2 fingerprint (JA3 / JA4)** - curl's handshake has a different
  shape (cipher order, extensions, ALPN, HTTP/2 SETTINGS) than Chrome/Firefox.
  This is below the HTTP layer, so **adding `User-Agent: Chrome` headers does
  not help** - verified, still reset.
- **datacenter IP reputation** - we egress from a Docker/WSL2 IP, which scores
  as "likely bot".

So no amount of curl flags fixes it: the tell is curl's own fingerprint.

## Solution - `fetch`

`fetch` (in every agent image, on `PATH`) is a small CLI backed by
[`curl_cffi`](https://github.com/lexiforest/curl_cffi), which is libcurl built to
**impersonate a real browser's TLS/HTTP-2 fingerprint**. Same request, browser
fingerprint, and the WAF lets it through.

```
$ fetch https://www.analog.com/.../ad7380-7381.pdf -o ds.pdf
fetch: 200 527053 bytes application/pdf
```

### Usage (curl-like)

```
fetch URL                          # body -> stdout, status -> stderr
fetch URL -o file.pdf              # save to file
fetch URL -H 'Accept: text/html'   # add header (repeatable)
fetch URL -X POST -d 'payload'     # method + body
fetch URL -I                       # HEAD: dump response headers
fetch URL --impersonate firefox    # profile: chrome (default), firefox, safari, edge, ...
fetch URL --fail                   # exit 22 on HTTP >= 400
fetch URL -s                       # silent (no status line on stderr)
```

### When to use which

- **Internal services, APIs, localhost, most of the web** -> plain `curl` is
  fine, keep using it.
- **A host that resets / times out / 403s a bare curl** -> use `fetch`.

`fetch` is not a curl replacement for everything (no `-u`, no upload, no cookie
jar yet); it is the escape hatch for fingerprint-blocked hosts. Extend
`docker/fetch` if an agent needs more curl features.

## Where it lives / how it's wired

- Source: [`docker/fetch`](../docker/fetch) (a self-contained Python CLI,
  shebang `#!/usr/local/bin/python3`).
- Installed into the agent images by their Dockerfiles
  (`worker-claude`, `worker-codex`, `worker-copilot`; `worker-claude-elec`
  inherits from `worker-claude`):
  ```dockerfile
  RUN pip install --no-cache-dir curl_cffi
  COPY docker/fetch /usr/local/bin/fetch
  RUN chmod +x /usr/local/bin/fetch
  ```
  `curl_cffi` installs into the base image's `/usr/local` Python, which is what
  `fetch`'s shebang targets. (The app runs from `/app/.venv`, a separate uv venv
  that has no pip - intentionally left untouched.)

> Note: existing running containers only get `fetch` after the image is rebuilt.
> Per repo policy, do not rebuild `worker-claude`/`hub` without explicit
> authorization (it kills the live session). The build args are already in the
> Dockerfiles, so the next normal rebuild picks it up.

## Ethics / scope

This is for letting our own automation read public datasheets and docs that
happen to sit behind a CDN's default bot rules. It is not for defeating
authentication, paywalls, rate limits, or any access control - don't use it to
get at something you couldn't get at in a browser.
