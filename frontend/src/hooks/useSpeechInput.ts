import { useEffect, useRef, useState } from "react";

// Minimal Web Speech API typings (avoids depending on full lib.dom updates)
type AnyWindow = typeof window & {
  SpeechRecognition?: new () => SpeechRecognitionLike;
  webkitSpeechRecognition?: new () => SpeechRecognitionLike;
};

interface SpeechRecognitionLike {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onresult: ((event: SpeechRecognitionEventLike) => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
}

interface SpeechRecognitionEventLike {
  resultIndex: number;
  results: ArrayLike<{
    0: { transcript: string };
    isFinal: boolean;
    length: number;
  }>;
}

type Options = {
  lang?: string;
  onFinal: (text: string) => void;
};

export function useSpeechInput({ lang = "en-US", onFinal }: Options) {
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const [listening, setListening] = useState(false);
  const [supported, setSupported] = useState(false);

  useEffect(() => {
    const w = window as AnyWindow;
    const Ctor = w.SpeechRecognition || w.webkitSpeechRecognition;
    if (!Ctor) {
      setSupported(false);
      return;
    }
    setSupported(true);
    const recognition = new Ctor();
    recognition.lang = lang;
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.onresult = (event) => {
      const results = event.results;
      let text = "";
      for (let i = event.resultIndex; i < results.length; i++) {
        text += results[i]![0].transcript;
      }
      if (text) onFinal(text);
    };
    recognition.onerror = () => setListening(false);
    recognition.onend = () => setListening(false);
    recognitionRef.current = recognition;

    return () => {
      try {
        recognition.stop();
      } catch {
        // ignore
      }
      recognitionRef.current = null;
    };
  }, [lang, onFinal]);

  const start = () => {
    const r = recognitionRef.current;
    if (!r) return;
    try {
      r.start();
      setListening(true);
    } catch {
      // already started
    }
  };

  const stop = () => {
    const r = recognitionRef.current;
    if (!r) return;
    try {
      r.stop();
    } catch {
      // ignore
    }
    setListening(false);
  };

  return { listening, supported, start, stop };
}
