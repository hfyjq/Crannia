# Crannia desktop GUI

在项目根目录运行 `npm run dev` 即可。Electron 主进程会自动拉起 Python 后端，
不需要单独启动 WebUI。首次运行前先 `npm install`。

## 目录

| 路径 | 是什么 |
|---|---|
| `main.cjs` | 主进程：起后端 · 建窗口 · 自定义协议 `crannia-app://` · 托盘与更新 |
| `backend-lifecycle.cjs` | 后端进程的启动 / 探活 / 退出 |
| `preload.cjs` · `splash-preload.cjs` · `overlay-preload.cjs` | 各窗口能拿到的能力，逐窗口白名单 |
| `ipc-guard.cjs` | 主进程侧的 IPC 入口校验 |
| `splash.html` · `overlay.html` | 启动小窗 / 前置浮层 |
| `src/` · `vite.config.js` | 渲染进程源码与构建配置 |
| `dist/` | 前端构建产物，`npm run build` 生成，不进版本库 |
| `electron-builder.config.mjs` · `installer.nsh` | 真正的打包配置（文件白名单 + 安装器） |

## 与 WebUI 的关系

`python -m web` 仍然可以单独跑，方便调后端。桌面壳走的是同一个 HTTP 接口，
区别在传输与权限：桌面壳自己生成启动令牌，经 `preload` 交给渲染进程，
不经过浏览器可读的位置。
