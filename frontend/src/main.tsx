import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import CapturePage from "./capture/CapturePage";
import "./styles.css";

const isCapture = window.location.pathname.startsWith("/capture");

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>{isCapture ? <CapturePage /> : <App />}</React.StrictMode>,
);
