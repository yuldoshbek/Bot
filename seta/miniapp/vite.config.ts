import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Сборка кладётся в dist/ — то, что раздаёт хостинг. Базовый путь корневой:
// приложение живёт на своём адресе целиком, а не в подпапке чужого сайта.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", sourcemap: false },
});
