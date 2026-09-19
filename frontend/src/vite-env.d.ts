/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Base URL of a running `cci serve`, e.g. `http://127.0.0.1:8787`.
   * Unset: the app reads the committed fixtures and filters in the browser.
   */
  readonly VITE_API_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
