import { useMemo } from "react";

type DiffLine = { kind: "add" | "del" | "ctx"; text: string };

/** Naive line diff using LCS — fine for tool-call previews, not huge files. */
function diffLines(a: string, b: string): DiffLine[] {
  const aLines = a.split("\n");
  const bLines = b.split("\n");
  const m = aLines.length;
  const n = bLines.length;

  // LCS table
  const dp: number[][] = Array.from({ length: m + 1 }, () =>
    new Array(n + 1).fill(0)
  );
  for (let i = 0; i < m; i++) {
    for (let j = 0; j < n; j++) {
      if (aLines[i] === bLines[j]) {
        dp[i + 1]![j + 1] = dp[i]![j]! + 1;
      } else {
        dp[i + 1]![j + 1] = Math.max(dp[i]![j + 1]!, dp[i + 1]![j]!);
      }
    }
  }

  const result: DiffLine[] = [];
  let i = m;
  let j = n;
  while (i > 0 && j > 0) {
    if (aLines[i - 1] === bLines[j - 1]) {
      result.push({ kind: "ctx", text: aLines[i - 1]! });
      i--;
      j--;
    } else if (dp[i - 1]![j]! >= dp[i]![j - 1]!) {
      result.push({ kind: "del", text: aLines[i - 1]! });
      i--;
    } else {
      result.push({ kind: "add", text: bLines[j - 1]! });
      j--;
    }
  }
  while (i > 0) {
    result.push({ kind: "del", text: aLines[i - 1]! });
    i--;
  }
  while (j > 0) {
    result.push({ kind: "add", text: bLines[j - 1]! });
    j--;
  }
  return result.reverse();
}

type Props = {
  before: string;
  after: string;
  maxLines?: number;
};

export function DiffView({ before, after, maxLines = 200 }: Props) {
  const lines = useMemo(() => diffLines(before, after), [before, after]);
  const display = lines.slice(0, maxLines);
  const truncated = lines.length > maxLines;

  return (
    <div className="font-mono text-xs bg-black/40 rounded overflow-x-auto max-h-96 overflow-y-auto">
      {display.map((l, i) => (
        <div
          key={i}
          className={
            l.kind === "add"
              ? "bg-green-950/60 text-green-300"
              : l.kind === "del"
                ? "bg-red-950/60 text-red-300"
                : "text-gray-500"
          }
        >
          <span className="select-none inline-block w-4 text-center opacity-60">
            {l.kind === "add" ? "+" : l.kind === "del" ? "-" : " "}
          </span>
          <span className="whitespace-pre-wrap break-words">{l.text || " "}</span>
        </div>
      ))}
      {truncated && (
        <div className="text-gray-500 px-2 py-1">
          … {lines.length - maxLines} more lines
        </div>
      )}
    </div>
  );
}
