import { StrictMode } from "react"
import { createRoot } from "react-dom/client"

import "@fontsource-variable/ibm-plex-sans/wght.css"
import "@fontsource/ibm-plex-mono/latin-400.css"
import "@fontsource/ibm-plex-mono/latin-500.css"
import "./index.css"

import { App } from "./App"

const root = document.getElementById("root")
if (root === null) throw new Error("#root is missing from index.html")

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
