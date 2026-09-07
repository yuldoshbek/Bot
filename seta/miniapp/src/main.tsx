/**
 * Точка входа. Тема ставится до первой отрисовки: страница, мигнувшая белым
 * у человека с тёмной темой, выглядит чужой ещё до того, как что-то покажет.
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { applyTheme, ready } from "./telegram";
import "./ui.css";

applyTheme();
ready();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
