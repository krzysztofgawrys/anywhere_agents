import type { LockInfo } from "../stores/chat";

type Props = {
  lock: LockInfo;
  onTakeover: () => void;
  onCancel: () => void;
};

export function LockTakeoverModal({ lock, onTakeover, onCancel }: Props) {
  const when = new Date(lock.lockedAt * 1000).toLocaleString();
  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4">
      <div className="bg-gray-900 border border-gray-700 rounded-xl max-w-md w-full p-6">
        <h2 className="text-lg font-semibold text-white mb-2">Session in use</h2>
        <p className="text-sm text-gray-300 mb-4">
          This session is currently open by{" "}
          <span className="font-mono text-blue-300">{lock.lockedBy}</span>{" "}
          (since {when}).
        </p>
        <p className="text-sm text-gray-400 mb-6">
          Taking over will drop the other client into read-only mode.
        </p>
        <div className="flex gap-3 justify-end">
          <button
            type="button"
            onClick={onCancel}
            className="px-4 py-2 rounded text-sm text-gray-300 hover:bg-gray-800"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onTakeover}
            className="px-4 py-2 rounded text-sm bg-red-600 hover:bg-red-700 text-white font-medium"
          >
            Take over
          </button>
        </div>
      </div>
    </div>
  );
}
