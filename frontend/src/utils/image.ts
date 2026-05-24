import type { PromptImage } from "../types";

const SUPPORTED = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);

/** Max raw bytes per image - Claude's hard limit is ~5MB. Reject earlier. */
export const MAX_IMAGE_BYTES = 4 * 1024 * 1024;

export function isSupportedImage(file: File | Blob): boolean {
  return SUPPORTED.has(file.type);
}

/** Read a File as base64 string (no data: prefix). */
export function fileToBase64(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("read failed"));
    reader.onload = () => {
      const result = reader.result as string;
      // result is "data:image/png;base64,XXXX..." - strip the prefix
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.readAsDataURL(file);
  });
}

export async function fileToPromptImage(file: File): Promise<PromptImage> {
  const data_b64 = await fileToBase64(file);
  return { media_type: file.type, data_b64 };
}

/** Build a data: URL for image preview from a PromptImage. */
export function previewUrl(img: PromptImage): string {
  return `data:${img.media_type};base64,${img.data_b64}`;
}

/** Extract image files from a clipboard/drag event. */
export function imagesFromDataTransfer(dt: DataTransfer | null): File[] {
  if (!dt) return [];
  const out: File[] = [];
  // Prefer .items (richer metadata)
  if (dt.items && dt.items.length > 0) {
    for (let i = 0; i < dt.items.length; i++) {
      const item = dt.items[i];
      if (item && item.kind === "file") {
        const f = item.getAsFile();
        if (f && isSupportedImage(f)) out.push(f);
      }
    }
  } else if (dt.files) {
    for (let i = 0; i < dt.files.length; i++) {
      const f = dt.files[i];
      if (f && isSupportedImage(f)) out.push(f);
    }
  }
  return out;
}
