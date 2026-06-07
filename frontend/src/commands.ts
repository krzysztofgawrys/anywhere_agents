/** Slash command definitions for the Composer autocomplete. */

export type SlashCommand = {
  /** Command name without the leading slash (e.g. "clear"). */
  name: string;
  /** Short description shown in the autocomplete dropdown. */
  description: string;
};

/** Built-in commands always available regardless of worker state.
 *
 * Per-worker model entries (e.g. `/model gpt-5.3-codex` for Copilot,
 * `/model claude-sonnet-4-6` for Claude) are injected dynamically by App.tsx
 * via the `extra` argument of {@link matchCommands}, sourced from the
 * `workers[].models` field of the hub's projects payload. That way the
 * autocomplete reflects whatever the active worker's SDK actually accepts.
 */
export const BUILTIN_COMMANDS: SlashCommand[] = [
  { name: "clear", description: "Clear chat messages" },
  { name: "compact", description: "Summarize conversation to save context" },
  { name: "effort default", description: "Reset to SDK default effort" },
  { name: "model default", description: "Switch to SDK default model" },
  { name: "new", description: "Start a new session in this project" },
  { name: "plan", description: "Switch to plan mode (read-only, no changes)" },
  { name: "act", description: "Switch to act mode (normal execution)" },
];

/** Return commands whose name starts with the typed prefix. */
export function matchCommands(
  input: string,
  extra: SlashCommand[] = [],
): SlashCommand[] {
  if (!input.startsWith("/")) return [];
  const prefix = input.slice(1).toLowerCase();
  return [...BUILTIN_COMMANDS, ...extra].filter((c) =>
    c.name.toLowerCase().startsWith(prefix),
  );
}
