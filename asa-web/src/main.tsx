import React from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import "./styles.css";

document.title = "ASA Agent";
const rootElement = document.getElementById("root");
if (rootElement)
  createRoot(rootElement).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
