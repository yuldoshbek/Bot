/// <reference types="vite/client" />

/**
 * Настройки сборки. Адрес API подставляется при сборке, а не берётся из кода:
 * у боевой и у пробной сборки он разный, а исходник один.
 */
interface ImportMetaEnv {
  readonly VITE_API_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
