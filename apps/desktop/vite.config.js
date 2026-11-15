import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 代理目标可配（默认 5000）—— 后端换端口而代理写死 5000 时，
// 渲染进程连不上，症状长得像"代码坏了"
// （开屏卡住 + logo 是坏的 + 右上角"服务未连接"），其实是探针接错了口。
// 判据：探针的输入源必须先核对生产从哪里读。
const API_TARGET = process.env.CRANNIA_API_TARGET || "http://127.0.0.1:5000";

export default defineConfig(({ command }) => ({
  root: "apps/desktop",
  plugins: [react()],
  // 构建产物的资源路径必须是相对的（`./assets/...`）。
  //    vite 默认给 `base:"/"`，打出来的 `index.html` 里是 `/assets/xxx.js`，
  //    而页面若从 `file://` 加载 —— 那里 `/assets/...` 会解析成
  //    `file:///assets/...`（文件系统根目录），404，整页空白。
  //    资源的 base 只该有一处定义：打包产物必须保持相对路径。
  //    dev 仍用 "/" —— dev 下面页面由 vite 自己服务，两者不能混。
  base: command === "build" ? "./" : "/",
  // `@crannia/ui` 住在 root 外面，② 放开 fs 白名单 ③ 别预打包它
  optimizeDeps: { exclude: ["@crannia/ui"] },
  server: {
    host: "127.0.0.1",
    fs: { allow: ["..", "../.."] },
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: API_TARGET, changeOrigin: true },
      "/ping": { target: API_TARGET, changeOrigin: true },
      "/static": { target: API_TARGET, changeOrigin: true }
    }
  },
  build: { outDir: "dist", emptyOutDir: true },
}));
