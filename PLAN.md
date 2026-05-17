# Claude Web

Webowy interfejs (mobile + desktop) do lokalnego Claude Code. Backend działa na hoście dewelopera, frontend dostępny z internetu przez Cloudflare Tunnel. Pozwala sterować Claude Code z telefonu jakby siedziało się przy biurku, z pełnym dostępem do projektów, sesji i toolingu.

## Architektura

```
[telefon/przeglądarka]
   ↓ HTTPS
[claude.<domena> — Cloudflare edge + Access policy (email allowlist)]
   ↓ Cloudflare Tunnel
[laptop: cloudflared (Docker)] → [FastAPI:8001 + WS — natywnie na hoście]
                                  ↓
                                  [claude-agent-sdk]
                                  ↓
                                  [~/projects/*, ~/.claude/projects/*]
```

**Hybryda Docker/natywnie jest świadomą decyzją:**
- Cloudflared w Dockerze → łatwy restart, izolacja, auto-update
- Backend natywnie → Claude Code potrzebuje pełnego środowiska dev (node, pyenv, cargo, git, dotfiles). Konteneryzacja backendu wymagałaby fat-image i walki ze ścieżkami w sesjach `.jsonl`.

## Stack

**Backend:**
- Python 3.11+, FastAPI, WebSocket (natywny FastAPI, nie socket.io)
- `claude-agent-sdk` (dawniej claude-code-sdk) — `ClaudeSDKClient` per sesja
- SQLite (projekty, locki, cache metadanych)
- `uv` do zarządzania zależnościami i venv

**Frontend:**
- React 18 + Vite + TypeScript
- Tailwind CSS, mobile-first
- Zustand (state management)
- `react-markdown` + `remark-gfm` + `rehype-highlight` (rendering wiadomości)
- Natywny `WebSocket` API z cienkim wrapperem (reconnect, heartbeat)
- Własny prosty diff viewer (linie + kolory) na MVP — `react-diff-viewer-continued` później jeśli potrzebne

**Infra:**
- Cloudflare Tunnel + Cloudflare Access (auth: email allowlist)
- Docker Compose tylko dla cloudflared
- launchd (macOS) / systemd user unit (Linux) dla auto-startu backendu

## Struktura repo

```
claude-web/
├── backend/
│   ├── pyproject.toml
│   ├── src/
│   │   ├── main.py              # FastAPI app entry
│   │   ├── ws/                  # WS handlers, routing wiadomości, protokół
│   │   ├── sdk/                 # wrapper claude-agent-sdk, sesje
│   │   ├── sessions/            # parser .jsonl, paginacja, metadata
│   │   ├── projects/            # skanowanie ~/.claude/projects, rejestracja
│   │   ├── locks/               # lock manager, heartbeat, auto-release
│   │   ├── auth/                # weryfikacja CF Access JWT
│   │   └── db.py                # SQLite, migracje
│   ├── tests/
│   └── src/static/              # build frontu trafia tutaj
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   └── src/
│       ├── components/
│       ├── hooks/               # useWebSocket, useSession, useHeartbeat
│       ├── stores/              # Zustand stores
│       └── routes/
├── docker/
│   ├── compose.yml              # tylko cloudflared
│   └── .env.example             # TUNNEL_TOKEN
├── scripts/
│   ├── dev.sh                   # backend + frontend hot reload
│   └── install-service.sh       # launchd/systemd unit
└── CLAUDE.md
```

Frontend buildje do `backend/src/static/`. FastAPI serwuje statyki na `/` i WS na `/ws`. Jedna domena, zero CORS.

## Kluczowe decyzje (warto zachować)

1. **Sesje czytane bezpośrednio z `~/.claude/projects/<hash>/*.jsonl`** — to ten sam storage co CLI. Nie wymyślamy własnego formatu. Spójność z lokalnym CLI za darmo: sesja stworzona w CLI pojawia się w webie i odwrotnie.

2. **Projekty = katalogi.** Backend trzyma w SQLite tylko listę zarejestrowanych projektów (path + nazwa + ustawienia). Skanowanie `~/.claude/projects/` przy starcie wykrywa projekty znane CLI. Wybór projektu w UI → ustawia `cwd` w `ClaudeAgentOptions`.

3. **Settings sources:** `setting_sources=["user", "project"]` w opcjach SDK, żeby załadować CLAUDE.md, skills, hooks, .claude/settings.json z dysku — dokładnie jak robi to interaktywne Claude Code.

4. **Permission modes uproszczone do dwóch:**
   - `bypassPermissions` — wszystko leci, zero pytań
   - `default` — każdy tool call czeka na approve przez WS
   - Per-projekt toggle w UI + zapis w SQLite
   - **Per-prompt override** w composerze: projekt może być na "manual", ale konkretny prompt z togglem "auto-approve this prompt"
   - **NIE** robimy allowlist regexów/globów per tool — celowo, prosto

5. **Lock per sesja, nie per projekt.** Cwd może się zmieniać w trakcie sesji (Claude robi `cd` w bash) — nie blokujemy katalogu. Lock chroni przed dwoma klientami piszącymi do tej samej sesji.

6. **Lock z UI takeover:** klient próbuje resume zablokowanej sesji → backend zwraca `session_locked` z device/timestamp → modal "Przejmij / Anuluj" → "przejmij" wysyła `lock_revoked` do starego klienta, który leci do read-only.

7. **WS heartbeat obowiązkowy** (nie opcjonalny): ping co 20s z klienta, server timeout 60s. CF Tunnel zamyka idle WS po ~100s. Brak heartbeatu = zwisające locki po zamknięciu telefonu, martwe sesje w pamięci.

8. **Auto-release locków:** WS timeout → release lock → persist stan sesji → cleanup. Bez tego pierwszy crash zostawia użytkownika zablokowanego.

9. **Paginacja historii sesji:**
   - przy resume: ostatnie ~30 wiadomości
   - pull-to-refresh / scroll-to-top → endpoint `GET /sessions/{id}/messages?before=<msg_id>&limit=50`
   - parser czyta `.jsonl` od końca w blokach (reverse read)
   - **Tool result blocks renderowane domyślnie collapsed** (output bash bywa MB-owy) — expand on tap

10. **Konkurencja CLI vs web (na laptopie):** lock jest tylko w backendzie, CLI go nie widzi. Akceptujemy że nie wolno używać tej samej sesji w CLI i webie równocześnie. Jeśli zacznie boleć — dorzuć mtime watch na `.jsonl` i ostrzeżenie w UI ("sesja zmodyfikowana z zewnątrz, reload?"). Nie blocker dla MVP.

## Protokół WebSocket

Wszystkie wiadomości to JSON z polem `type` i `payload`.

**Klient → Serwer:**
- `prompt` — wysłanie promptu (`{ session_id, text, auto_approve?: bool }`)
- `approve_tool` — zatwierdzenie tool call (`{ tool_use_id }`)
- `deny_tool` — odrzucenie tool call (`{ tool_use_id, reason? }`)
- `interrupt` — przerwanie obecnego streamu (`{ session_id }`)
- `new_session` — nowa sesja w projekcie (`{ project_id }`)
- `resume_session` — wznowienie sesji (`{ session_id, force?: bool }`)
- `list_sessions` — lista sesji projektu (`{ project_id }`)
- `ping` — heartbeat (co 20s)

**Serwer → Klient:**
- `text_delta` — fragment tekstu asystenta (`{ session_id, text }`)
- `tool_call` — Claude wywołuje tool (`{ session_id, tool_use_id, name, input }`)
- `tool_result` — wynik tool call (`{ session_id, tool_use_id, content, is_error }`)
- `permission_request` — wymagana zgoda (`{ session_id, tool_use_id, name, input }`)
- `session_started` — potwierdzenie startu/resume (`{ session_id, history: [...] }`)
- `session_locked` — sesja zablokowana (`{ session_id, locked_by, locked_at }`)
- `lock_revoked` — lock zabrany przez innego klienta (`{ session_id }`)
- `error` — błąd (`{ code, message }`)
- `pong` — odpowiedź na heartbeat

## Development

**Setup jednorazowy:**

```bash
# backend
cd backend
uv sync
uv run pytest

# frontend
cd frontend
npm install
npm run build  # buduje do ../backend/src/static/

# cloudflared
cd docker
cp .env.example .env  # uzupełnij TUNNEL_TOKEN z CF dashboard
docker compose up -d
```

**Dev (hot reload):**

```bash
./scripts/dev.sh
# uruchamia:
# - backend: uv run uvicorn src.main:app --reload --host 127.0.0.1 --port 8001
# - frontend: vite dev server na 5173, proxy /ws → 127.0.0.1:8001
```

**Auto-start na hoście:**

```bash
./scripts/install-service.sh
# wykrywa macOS/Linux, instaluje launchd plist albo systemd user unit
```

## Konwencje

**Backend:**
- Typowanie: pełne type hints, `mypy --strict`
- Async wszędzie (FastAPI WS + claude-agent-sdk są async)
- Pydantic do schematów WS messages i API responses
- Logging przez `structlog`, JSON format do plików, czytelny do stdout w dev
- Testy: `pytest` + `pytest-asyncio`, mocki SDK z `unittest.mock`

**Frontend:**
- TypeScript strict mode
- Komponenty funkcyjne + hooki, zero classów
- Tailwind utility-first, custom CSS tylko dla rzeczy niewyrażalnych w TW
- Mobile-first: bazowe style mobile, `md:` / `lg:` dla desktop
- Stores Zustand: jeden per domain (sessions, projects, ui, ws)
- Brak Redux, brak react-query — WS jest źródłem prawdy, lokalny state w Zustand

**Wspólne:**
- Commits: conventional commits (`feat:`, `fix:`, `refactor:`, etc.)
- Brak ciężkich frameworków — preferujemy małe, dobrze rozumiane dependencies

## Gotchas (to nas ugryzie jeśli zapomnimy)

1. **PATH w launchd/systemd:** unit pliki mają minimalny PATH. Claude wywołujący `npm test` zobaczy "command not found". Rozwiązanie: odpalaj backend przez `bash -lc "exec uv run ..."` żeby załadować pełny shell environment.

2. **Tool result blocks bywają wielkie.** Bash output, file reads — MB-y. Przy paginacji historii: render collapsed, expand on tap. Inaczej "załaduj 50 wcześniejszych" wrzuca 5MB do DOM-u i ekran zamiera.

3. **Binarne wyjścia tooli** (screenshoty, obrazki) — SDK zwraca content blocks z `type: image`. Frontend musi je renderować, nie traktować jak text.

4. **WebSocket idle timeout w CF Tunnel** ~100s. Heartbeat 20s to wymóg, nie luksus.

5. **CF Access JWT weryfikacja na WS handshake:** WS to HTTP upgrade, header `Cf-Access-Jwt-Assertion` jest dostępny w `websocket.headers`. Weryfikuj w `accept()` ścieżce, nie po. Bez tego ktoś w lokalnej sieci może uderzyć w `127.0.0.1:8001` z pominięciem CF.

6. **Sesje SDK są stanowe.** `ClaudeSDKClient` żyje w pamięci backendu po rozłączeniu WS — nie zabijaj go od razu. Timeout np. 30 min, dopiero wtedy cleanup. Krótki disconnect (telefon w tunelu metra) nie powinien tracić kontekstu.

7. **Bash z długim outputem** (`npm run dev`, `tail -f`) — musi mieć `interrupt` z UI. Bez tego sesja zwisa do końca świata.

8. **Ścieżki w `.jsonl` są absolutne.** Sesje zapisane przez CLI mają ścieżki hosta. Backend musi działać w tym samym mount space (czyli natywnie, nie w kontenerze z innymi ścieżkami).

9. **CF Access session duration:** ustaw rozsądnie (30 dni) inaczej re-login co godzinę z telefonu jest upierdliwy.

10. **Pierwszy `claude login` musi być zrobiony na hoście** zanim backend wystartuje. Backend czyta `~/.claude/.credentials.json`. Bez tego SDK rzuci auth error przy pierwszym query.

## Status faz implementacji

- [ ] **Faza 1:** Szkielet FastAPI + WS + CF Tunnel + Access. End-to-end ping z telefonu przez HTTPS.
- [ ] **Faza 2:** SDK z jedną sesją hardcoded. Prompt → stream → render w UI.
- [ ] **Faza 3:** Skanowanie `~/.claude/projects/`. Lista projektów, lista sesji, new/resume.
- [ ] **Faza 4:** Lock manager + heartbeat + paginacja historii. Pull-to-refresh.
- [ ] **Faza 5:** Permissions toggle (bypassPermissions/default) + per-prompt override. UI approval flow.
- [ ] **Faza 6:** UX polish — diff viewer, collapse tool outputs, voice input (Web Speech API), markdown polish.

Każda faza powinna być działająca end-to-end zanim ruszysz dalej. Nie buduj UI dla Fazy 4 zanim Faza 2 działa stabilnie.

## Czego NIE robić

- **Nie wrapuj TUI Claude Code.** Mamy `claude-agent-sdk` — to ten sam silnik, zero parsowania escape sequences.
- **Nie wymyślaj własnego storage sesji.** `.jsonl` w `~/.claude/projects/` jest sourcem prawdy.
- **Nie dodawaj socket.io** — natywny WS wystarczy, socket.io to inny protokół, FastAPI go nie wspiera natywnie.
- **Nie konteneryzuj backendu** — przedyskutowane, nie warte komplikacji.
- **Nie dodawaj allowlist regexów na tools** — celowo proste, dwa tryby + per-prompt override.
- **Nie ufaj loopback bindowi jako jedynemu authowi** — zawsze weryfikuj CF Access JWT.