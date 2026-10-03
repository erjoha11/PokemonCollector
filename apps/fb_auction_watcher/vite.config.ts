import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

const src = (path: string) => fileURLToPath(new URL(`./src/${path}`, import.meta.url));

// Two passes into the same dist/ (see package.json "build"): content scripts can't be
// ES modules, so they're built as a self-contained IIFE; the service worker is an ES module.
export default defineConfig(({ mode }) => {
  const content = mode === "content";
  return {
    publicDir: content ? false : "public",
    build: {
      outDir: "dist",
      emptyOutDir: !content,
      target: "chrome120",
      minify: false,
      sourcemap: true,
      lib: content
        ? {
            entry: src("content/post/index.ts"),
            formats: ["iife"],
            name: "fbawPost",
            fileName: () => "content-post.js",
          }
        : {
            entry: src("background/index.ts"),
            formats: ["es"],
            fileName: () => "background.js",
          },
    },
  };
});
