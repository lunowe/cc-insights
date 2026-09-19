import { useCallback, useEffect, useState } from "react"

export type ThemeChoice = "light" | "dark" | "system"

const STORAGE_KEY = "cci.theme"

function readStored(): ThemeChoice {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    if (v === "light" || v === "dark" || v === "system") return v
  } catch {
    /* private mode / blocked storage: fall through to system */
  }
  return "system"
}

function prefersDark(): boolean {
  return window.matchMedia("(prefers-color-scheme: dark)").matches
}

export function applyTheme(choice: ThemeChoice) {
  const dark = choice === "dark" || (choice === "system" && prefersDark())
  document.documentElement.classList.toggle("dark", dark)
  document.documentElement.style.colorScheme = dark ? "dark" : "light"
}

/**
 * `prefers-color-scheme` is the default and keeps tracking the OS; an explicit
 * choice overrides it and is remembered.
 */
export function useTheme() {
  const [choice, setChoice] = useState<ThemeChoice>(readStored)

  useEffect(() => {
    applyTheme(choice)
    try {
      localStorage.setItem(STORAGE_KEY, choice)
    } catch {
      /* not worth failing a render over */
    }
    if (choice !== "system") return
    const mq = window.matchMedia("(prefers-color-scheme: dark)")
    const onChange = () => applyTheme("system")
    mq.addEventListener("change", onChange)
    return () => mq.removeEventListener("change", onChange)
  }, [choice])

  const resolved: "light" | "dark" =
    choice === "system" ? (prefersDark() ? "dark" : "light") : choice

  const cycle = useCallback(() => {
    setChoice((c) => (c === "system" ? "light" : c === "light" ? "dark" : "system"))
  }, [])

  return { choice, resolved, setChoice, cycle }
}
