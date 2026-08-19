import React from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import { initTheme } from "./agent/theme";
import { ErrorBoundary } from "./shared/ErrorBoundary";
import { installChunkLoadRecovery } from "./shared/chunkLoadRecovery";
import "./styles.css";

initTheme();
installChunkLoadRecovery();
document.title = "ASA Agent";
const rootElement = document.getElementById("root");
if (rootElement)
  createRoot(rootElement).render(
    <React.StrictMode>
      <ErrorBoundary label="应用">
        <App />
      </ErrorBoundary>
    </React.StrictMode>,
  );
