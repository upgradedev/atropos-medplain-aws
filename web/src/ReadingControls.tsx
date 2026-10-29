import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";

type Preferences = { largerText: boolean; highContrast: boolean };
const defaults: Preferences = { largerText: false, highContrast: false };
const PreferencesContext = createContext({ ...defaults, update: (_value: Partial<Preferences>) => {} });

export function ReadingPreferences({ children }: { children: ReactNode }) {
  const [preferences, setPreferences] = useState<Preferences>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem("medplain.reading.v1") || "null");
      return { largerText: saved?.largerText === true, highContrast: saved?.highContrast === true };
    } catch { return defaults; }
  });
  useEffect(() => {
    document.documentElement.dataset.readingLarge = String(preferences.largerText);
    document.documentElement.dataset.readingContrast = String(preferences.highContrast);
    try {
      localStorage.setItem("medplain.reading.v1", JSON.stringify(preferences));
      const theme = localStorage.getItem("medplain.theme.v2");
      if (theme === "dark" || theme === "light") document.documentElement.dataset.theme = theme;
    } catch { /* Presentation still works when storage is unavailable. */ }
  }, [preferences]);
  const update = (value: Partial<Preferences>) => setPreferences((current) => ({ ...current, ...value }));
  return <PreferencesContext.Provider value={{ ...preferences, update }}>{children}</PreferencesContext.Provider>;
}

export default function ReadingControls({ text, language = "en", unavailableReason = "Read aloud is available after this content is released." }: { text?: string | null; language?: "en" | "el"; unavailableReason?: string }) {
  const preferences = useContext(PreferencesContext);
  const [voices, setVoices] = useState<SpeechSynthesisVoice[]>([]);
  const [state, setState] = useState<"idle" | "speaking" | "paused">("idle");
  const [voiceError, setVoiceError] = useState("");
  const current = useRef<SpeechSynthesisUtterance | null>(null);
  const supported = typeof window !== "undefined" && "speechSynthesis" in window && "SpeechSynthesisUtterance" in window;
  useEffect(() => {
    if (!supported) return;
    const read = () => setVoices(window.speechSynthesis.getVoices());
    read(); window.speechSynthesis.addEventListener("voiceschanged", read);
    return () => window.speechSynthesis.removeEventListener("voiceschanged", read);
  }, [supported]);
  useEffect(() => {
    setState("idle"); setVoiceError("");
    return () => {
      if (current.current && supported) {
        current.current.onend = null; current.current.onerror = null;
        window.speechSynthesis.cancel(); current.current = null;
      }
    };
  }, [text, language, supported]);
  const voice = voices.find((item) => item.localService && item.lang.toLowerCase().startsWith(language));
  const reason = !text ? unavailableReason : !supported ? "This browser does not provide device read aloud." : !voice ? `No local ${language === "el" ? "Greek" : "English"} device voice is available. The written card remains available.` : "";
  const start = () => {
    if (!text || !voice || !supported) return;
    if (state === "paused") { window.speechSynthesis.resume(); setState("speaking"); return; }
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.voice = voice; utterance.lang = voice.lang; utterance.rate = 0.9;
    utterance.onend = () => { current.current = null; setState("idle"); };
    utterance.onerror = () => { current.current = null; setState("idle"); setVoiceError("The device voice could not finish. Use the written card or try again."); };
    setVoiceError(""); current.current = utterance; window.speechSynthesis.speak(utterance); setState("speaking");
  };
  const stop = () => {
    if (current.current && supported) {
      current.current.onend = null; current.current.onerror = null;
      window.speechSynthesis.cancel();
    }
    current.current = null; setState("idle");
  };
  return <section className="reading-controls" aria-label="Reading and accessibility controls">
    <div className="action-row">
      <button type="button" className="button secondary" aria-pressed={preferences.largerText} onClick={() => preferences.update({ largerText: !preferences.largerText })}>Larger text</button>
      <button type="button" className="button secondary" aria-pressed={preferences.highContrast} onClick={() => preferences.update({ highContrast: !preferences.highContrast })}>High contrast</button>
      <button type="button" className="button secondary" disabled={Boolean(reason) || state === "speaking"} onClick={start}>{state === "paused" ? "Resume device voice" : "Read aloud with device voice"}</button>
      <button type="button" className="button secondary" disabled={state !== "speaking"} onClick={() => { window.speechSynthesis.pause(); setState("paused"); }}>Pause voice</button>
      <button type="button" className="button secondary" disabled={state === "idle"} onClick={stop}>Stop voice</button>
    </div>
    <p className="control-reason" role="status">{reason || voiceError || (state === "idle" ? "Device voice starts only when you press Read aloud. Keep the written source-cited card visible. This is not Amazon Polly." : state === "paused" ? "Device voice is paused. Resume or stop it." : "Reading the released text. Pause or stop at any time.")}</p>
    <p className="control-reason">{state === "idle" ? "Pause and Stop become available only while device voice is active." : state === "paused" ? "Pause is unavailable while already paused." : "Read aloud is unavailable while this reading is active."}</p>
  </section>;
}
