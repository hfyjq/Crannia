import React from "react";
import { createRoot } from "react-dom/client";
import { App, createClient } from "@crannia/ui";

// 壳只管一件事：base 从哪儿来。
//   dev：渲染进程挂在 vite（5173），走它的同源代理，base = ""。
//   打包：页面来自 `crannia-app://app`，`/api` 一族由壳同源转给后端
//        所以base 也是 ""。`?api=` 那个口留着只为兼容老写法，壳已经不再传它。
const baseUrl = new URLSearchParams(window.location.search).get("api") || "";

createRoot(document.getElementById("root")).render(<App client={createClient(baseUrl)} />);
