import path from "node:path"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

export default defineConfig({
  plugins: [react()],
  // Relative base so the build can be served from any S3 prefix or subpath.
  base: "./",
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
})
