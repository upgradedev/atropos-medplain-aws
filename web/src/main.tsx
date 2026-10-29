import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";
import { ReadingPreferences } from "./ReadingControls";
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ReadingPreferences>
      <App />
    </ReadingPreferences>
  </React.StrictMode>,
);
