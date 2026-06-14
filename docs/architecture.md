# Architektura - Agents Anywhere

Diagram blokowy (ręcznie pisany SVG, renderowany przez `Svg.tsx` w podglądzie `.md`).

```svg
<svg viewBox="0 0 1200 560" xmlns="http://www.w3.org/2000/svg" font-family="ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif">
  <defs>
    <marker id="ah" viewBox="0 0 10 10" markerWidth="7.5" markerHeight="7.5" refX="8.5" refY="5" orient="auto-start-reverse">
      <path d="M1,1 L9,5 L1,9 z" fill="#64748b"/>
    </marker>
  </defs>

  <rect x="0" y="0" width="1200" height="560" fill="#0d1117"/>

  <text x="36" y="44" font-size="25" font-weight="700" fill="#f1f5f9">Agents Anywhere - architektura</text>
  <text x="37" y="72" font-size="14" fill="#94a3b8">Przegladarka -&gt; hub (router) -&gt; worker -&gt; Claude Code CLI -&gt; Anthropic API   (WebSocket + HTTP)</text>

  <!-- workers.json -->
  <rect x="446" y="150" width="180" height="50" rx="10" fill="#1e293b" stroke="#94a3b8" stroke-width="1.5"/>
  <text x="536" y="172" text-anchor="middle" font-size="13" font-weight="600" fill="#e2e8f0">workers.json</text>
  <text x="536" y="190" text-anchor="middle" font-size="11" fill="#94a3b8">rejestr workerow</text>

  <!-- frontend-dist -->
  <rect x="446" y="392" width="180" height="50" rx="10" fill="#1e293b" stroke="#94a3b8" stroke-width="1.5"/>
  <text x="536" y="414" text-anchor="middle" font-size="13" font-weight="600" fill="#e2e8f0">frontend-dist</text>
  <text x="536" y="432" text-anchor="middle" font-size="11" fill="#94a3b8">serwowane na zywo</text>

  <!-- Browser -->
  <rect x="36" y="248" width="150" height="96" rx="13" fill="#1e1b4b" stroke="#818cf8" stroke-width="2"/>
  <text x="111" y="286" text-anchor="middle" font-size="16" font-weight="600" fill="#e0e7ff">Przegladarka</text>
  <text x="111" y="308" text-anchor="middle" font-size="12" fill="#a5b4fc">React SPA (Vite)</text>
  <text x="111" y="326" text-anchor="middle" font-size="12" fill="#a5b4fc">chat / pliki / terminal</text>

  <!-- Cloudflare -->
  <rect x="246" y="248" width="140" height="96" rx="13" fill="#2a1a05" stroke="#f6821f" stroke-width="2"/>
  <text x="316" y="286" text-anchor="middle" font-size="16" font-weight="600" fill="#fed7aa">Cloudflare</text>
  <text x="316" y="308" text-anchor="middle" font-size="12" fill="#fdba74">tunnel + Access JWT</text>
  <text x="316" y="326" text-anchor="middle" font-size="12" fill="#fdba74">(opcjonalnie)</text>

  <!-- Hub -->
  <rect x="446" y="234" width="180" height="124" rx="13" fill="#082f36" stroke="#22d3ee" stroke-width="2.5"/>
  <text x="536" y="274" text-anchor="middle" font-size="18" font-weight="700" fill="#cffafe">Hub</text>
  <text x="536" y="296" text-anchor="middle" font-size="12" fill="#67e8f9">FastAPI :8001</text>
  <text x="536" y="314" text-anchor="middle" font-size="12" fill="#67e8f9">router WS / remap id</text>
  <text x="536" y="332" text-anchor="middle" font-size="12" fill="#67e8f9">serwuje SPA / push</text>

  <!-- worker-claude -->
  <rect x="686" y="206" width="190" height="86" rx="13" fill="#07271c" stroke="#34d399" stroke-width="2"/>
  <text x="781" y="240" text-anchor="middle" font-size="15" font-weight="600" fill="#d1fae5">worker-claude</text>
  <text x="781" y="261" text-anchor="middle" font-size="11.5" fill="#6ee7b7">FastAPI :8002</text>
  <text x="781" y="279" text-anchor="middle" font-size="11.5" fill="#6ee7b7">sesje / SQLite</text>

  <!-- worker-claude-elec -->
  <rect x="686" y="332" width="190" height="86" rx="13" fill="#07271c" stroke="#34d399" stroke-width="2"/>
  <text x="781" y="366" text-anchor="middle" font-size="15" font-weight="600" fill="#d1fae5">worker-claude-elec</text>
  <text x="781" y="387" text-anchor="middle" font-size="11.5" fill="#6ee7b7">ten sam obraz</text>
  <text x="781" y="405" text-anchor="middle" font-size="11.5" fill="#6ee7b7">sprzet: ftdi / hid / jtag</text>

  <!-- Claude Code CLI -->
  <rect x="936" y="206" width="160" height="86" rx="13" fill="#1e1640" stroke="#a78bfa" stroke-width="2"/>
  <text x="1016" y="240" text-anchor="middle" font-size="15" font-weight="600" fill="#ede9fe">Claude Code CLI</text>
  <text x="1016" y="261" text-anchor="middle" font-size="11.5" fill="#c4b5fd">1 subprocess</text>
  <text x="1016" y="279" text-anchor="middle" font-size="11.5" fill="#c4b5fd">na sesje</text>

  <!-- Anthropic API -->
  <rect x="936" y="332" width="160" height="86" rx="13" fill="#2a160e" stroke="#d97757" stroke-width="2"/>
  <text x="1016" y="372" text-anchor="middle" font-size="15" font-weight="600" fill="#fde4d8">Anthropic API</text>
  <text x="1016" y="393" text-anchor="middle" font-size="11.5" fill="#f0a78a">api.anthropic.com</text>

  <!-- connectors -->
  <line x1="186" y1="296" x2="244" y2="296" stroke="#64748b" stroke-width="2" marker-start="url(#ah)" marker-end="url(#ah)"/>
  <text x="215" y="287" text-anchor="middle" font-size="11" fill="#94a3b8">WS + HTTP</text>

  <line x1="386" y1="296" x2="444" y2="296" stroke="#64748b" stroke-width="2" marker-start="url(#ah)" marker-end="url(#ah)"/>
  <text x="415" y="287" text-anchor="middle" font-size="11" fill="#94a3b8">tunel</text>

  <line x1="626" y1="280" x2="684" y2="252" stroke="#64748b" stroke-width="2" marker-start="url(#ah)" marker-end="url(#ah)"/>
  <text x="656" y="258" text-anchor="middle" font-size="10.5" fill="#94a3b8">WS routed</text>

  <line x1="626" y1="320" x2="684" y2="368" stroke="#64748b" stroke-width="2" marker-start="url(#ah)" marker-end="url(#ah)"/>
  <text x="650" y="360" text-anchor="middle" font-size="10.5" fill="#94a3b8">/internal/*</text>

  <line x1="536" y1="234" x2="536" y2="202" stroke="#64748b" stroke-width="2" stroke-dasharray="5 4" marker-end="url(#ah)"/>
  <text x="572" y="222" text-anchor="middle" font-size="10.5" fill="#94a3b8">czyta</text>

  <line x1="536" y1="358" x2="536" y2="390" stroke="#64748b" stroke-width="2" stroke-dasharray="5 4" marker-end="url(#ah)"/>
  <text x="578" y="378" text-anchor="middle" font-size="10.5" fill="#94a3b8">serwuje</text>

  <line x1="876" y1="249" x2="934" y2="249" stroke="#64748b" stroke-width="2" marker-start="url(#ah)" marker-end="url(#ah)"/>
  <text x="905" y="240" text-anchor="middle" font-size="10" fill="#94a3b8">subprocess</text>

  <line x1="1016" y1="292" x2="1016" y2="330" stroke="#64748b" stroke-width="2" marker-start="url(#ah)" marker-end="url(#ah)"/>
  <text x="1042" y="315" text-anchor="middle" font-size="10.5" fill="#94a3b8">HTTPS</text>

  <!-- legend -->
  <rect x="36" y="500" width="16" height="16" rx="3" fill="#1e1b4b" stroke="#818cf8" stroke-width="1.5"/>
  <text x="60" y="513" font-size="13" fill="#cbd5e1">Klient</text>
  <rect x="150" y="500" width="16" height="16" rx="3" fill="#2a1a05" stroke="#f6821f" stroke-width="1.5"/>
  <text x="174" y="513" font-size="13" fill="#cbd5e1">Cloudflare (edge)</text>
  <rect x="320" y="500" width="16" height="16" rx="3" fill="#082f36" stroke="#22d3ee" stroke-width="1.5"/>
  <text x="344" y="513" font-size="13" fill="#cbd5e1">Hub / router</text>
  <rect x="470" y="500" width="16" height="16" rx="3" fill="#07271c" stroke="#34d399" stroke-width="1.5"/>
  <text x="494" y="513" font-size="13" fill="#cbd5e1">Workery</text>
  <rect x="600" y="500" width="16" height="16" rx="3" fill="#1e1640" stroke="#a78bfa" stroke-width="1.5"/>
  <text x="624" y="513" font-size="13" fill="#cbd5e1">Claude Code CLI</text>
  <rect x="780" y="500" width="16" height="16" rx="3" fill="#2a160e" stroke="#d97757" stroke-width="1.5"/>
  <text x="804" y="513" font-size="13" fill="#cbd5e1">Anthropic API</text>
  <rect x="940" y="500" width="16" height="16" rx="3" fill="#1e293b" stroke="#94a3b8" stroke-width="1.5"/>
  <text x="964" y="513" font-size="13" fill="#cbd5e1">Konfig / statyki</text>
</svg>
```
