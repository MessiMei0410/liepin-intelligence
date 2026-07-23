import React from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import { CopilotSurface } from "./copilot/CopilotSurface";
import "./styles.css";

const surface = new URLSearchParams(location.search).get("surface");
document.title = surface === "copilot" ? "ASA Copilot" : "ASA Agent";
const rootElement = document.getElementById("root");
if (rootElement)
  createRoot(rootElement).render(
    <React.StrictMode>
      {surface === "copilot" ? <CopilotSurface /> : <App />}
    </React.StrictMode>,
  );
