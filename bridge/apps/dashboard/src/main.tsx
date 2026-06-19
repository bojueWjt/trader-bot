import { createRoot } from "react-dom/client";
import { App } from "./App";

const rootElement = document.getElementById("root");

if (!rootElement) {
  throw new Error("Root element missing");
}

createRoot(rootElement).render(<App />);
