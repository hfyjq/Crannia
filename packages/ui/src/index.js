// `@crannia/ui` —— 两个壳共用的那一层（`packages/web` 与 `packages/desktop`
// 都依赖同一个组件包；不是"一份静态产物两边塞"）。
//
// 今天只有一个消费者（`apps/desktop`）；将来 `apps/web`（WebUI 迁 SPA）接进来时，
// 它只换壳（怎么拿 base/token、有没有托盘/窗口），组件一行不改。

import "./styles.css";

export { App } from "./App.jsx";
export { createClient, jsonRequest } from "./api.js";
