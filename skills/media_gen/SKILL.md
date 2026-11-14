---
name: media-gen
description: Generate images from text prompts by calling an image model (OpenAI-compatible /images/generations endpoints, e.g. Volcano Ark Seedream, OpenAI, SiliconFlow). Use when the user asks to draw, illustrate or generate a picture and the conversation model itself cannot produce images. Requires an image entry (base URL + model + API key) in the media-gen config.
metadata:
  kind: skill
  loader: manifest.yaml
  tools: generate_image
---

# 生图（media_gen）

调用一个**生图服务**把一句描述变成一张图，结果直接落到工作区并**显示在聊天里**。
对话模型自己不会画图时走这里。

## 凭据

`data/media_gen_credentials.json`（独立文件，**不进** `settings.json`、不进导出包）::

    {"default": "ark",
     "entries": {"ark": {"base_url": "https://ark.cn-beijing.volces.com/api/v3",
                          "model": "doubao-seedream-4-0-20260415",
                          "api_key": "…"}}}

`base_url` 填到那家的 **API 根**（火山是 `/api/v3`，不一定是 `/v1`）。
**没配时**：工具照常出现，调用会回一句「还没有配置生图」—— 不是说功能不存在。

## 什么时候用它

- 用户要**从无到有画一张**（"画一只在雨里的猫"）⇒ 用它
- 要**读**一张现成的图、改一张现成的图、识别图里的字 ⇒ **不用它**
  （读图走多模态输入；改图是另一个能力，目前没有）

## 怎么读错误

- `401` 密钥无效 —— 注意：有些平台显示的是**密钥的名字**，不是密钥本身
- `404` 模型名或地址不对（模型下架 / `base_url` 少了层级）
- `429` 多半是额度或限流；火山方舟常见是「安心体验」把服务暂停了
- 「跨站跳转了（未跟随）」⇒ 图片地址跳到了别的域名，我们没有跟 —— 重新生成一次

## 花费

直连厂商的响应里没有花费字段，所以这里不会告诉你花了多少钱。
钱由厂商账号结算，用户自己把握。
