#!/usr/bin/env python3
"""Rebuild creators/models.json + provider manifests from user's api大全.txt"""
import json, yaml, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CREATORS = {
    "deepseek": {
        "label": "DeepSeek", "website": "https://deepseek.com/",
        "models": {
            "deepseek-v4-flash": {"label": "V4 Flash", "context_window": 65536},
            "deepseek-v4-pro": {"label": "V4 Pro", "context_window": 65536},
        }
    },
    "zhipu": {
        "label": "智谱 GLM", "website": "https://open.bigmodel.cn/",
        "models": {
            "glm-4.5-flash": {"label": "GLM-4.5 Flash", "context_window": 131072},
            "glm-4.5": {"label": "GLM-4.5", "context_window": 131072},
            "glm-4.5-air": {"label": "GLM-4.5 Air", "context_window": 131072},
            "glm-4.5-airx": {"label": "GLM-4.5 AirX", "context_window": 131072},
            "glm-4.5v": {"label": "GLM-4.5V", "context_window": 131072},
            "glm-4.6": {"label": "GLM-4.6", "context_window": 131072},
            "glm-4.6v": {"label": "GLM-4.6V", "context_window": 131072},
            "glm-4.6v-flash": {"label": "GLM-4.6V Flash", "context_window": 131072},
            "glm-4.6v-flashx": {"label": "GLM-4.6V FlashX", "context_window": 131072},
            "glm-4": {"label": "GLM-4", "context_window": 131072},
            "glm-4.7": {"label": "GLM-4.7", "context_window": 131072},
        }
    },
    "siliconflow": {
        "label": "硅基流动", "website": "https://www.siliconflow.cn/",
        "models": {
            "Qwen/Qwen3-8B": {"label": "Qwen3 8B", "context_window": 32768},
            "DeepSeek-V3.2": {"label": "DeepSeek V3.2", "context_window": 65536},
        }
    },
    "minimax": {
        "label": "MiniMax", "website": "https://platform.minimaxi.com/",
        "models": {
            "MiniMax-M2.7": {"label": "M2.7", "context_window": 131072},
            "MiniMax-M2.7-highspeed": {"label": "M2.7 HighSpeed", "context_window": 131072},
            "MiniMax-M3": {"label": "M3", "context_window": 131072},
        }
    },
    "moonshot": {
        "label": "月之暗面 Kimi", "website": "https://www.moonshot.cn/",
        "models": {
            "kimi-k2.5": {"label": "Kimi K2.5", "context_window": 131072},
            "kimi-k2.6": {"label": "Kimi K2.6", "context_window": 131072},
            "kimi-k2.7-code": {"label": "Kimi K2.7 Code", "context_window": 131072},
        }
    },
    "anthropic": {
        "label": "Anthropic", "website": "https://www.anthropic.com/",
        "models": {
            "claude-sonnet-4-5": {"label": "Claude Sonnet 4.5", "context_window": 200000},
            "claude-haiku-4-5": {"label": "Claude Haiku 4.5", "context_window": 200000},
            "claude-opus-4-5": {"label": "Claude Opus 4.5", "context_window": 200000},
            "claude-opus-4-6": {"label": "Claude Opus 4.6", "context_window": 200000},
            "claude-sonnet-4-6": {"label": "Claude Sonnet 4.6", "context_window": 200000},
            "claude-opus-4-7": {"label": "Claude Opus 4.7", "context_window": 200000},
        }
    },
    "openai": {
        "label": "OpenAI", "website": "https://openai.com/",
        "models": {
            "gpt-5": {"label": "GPT-5", "context_window": 128000},
            "gpt-5-pro": {"label": "GPT-5 Pro", "context_window": 128000},
            "gpt-5-chat": {"label": "GPT-5 Chat", "context_window": 128000},
            "gpt-5.1": {"label": "GPT-5.1", "context_window": 128000},
            "gpt-5.2": {"label": "GPT-5.2", "context_window": 128000},
            "gpt-5.2-pro": {"label": "GPT-5.2 Pro", "context_window": 128000},
            "gpt-5.4": {"label": "GPT-5.4", "context_window": 128000},
            "gpt-5.4-pro": {"label": "GPT-5.4 Pro", "context_window": 128000},
        }
    },
    "azure": {
        "label": "Azure AI Foundry", "website": "https://azure.microsoft.com/en-us/products/ai-foundry/models/openai/",
        "models": {
            "gpt-4o": {"label": "GPT-4o", "context_window": 128000},
            "gpt-4o-mini": {"label": "GPT-4o Mini", "context_window": 128000},
        }
    },
    "gemini": {
        "label": "Google Gemini", "website": "https://gemini.google.com/",
        "models": {
            "gemini-2.5-flash": {"label": "Gemini 2.5 Flash", "context_window": 1048576},
            "gemini-2.5-pro": {"label": "Gemini 2.5 Pro", "context_window": 2097152},
            "gemini-2.5-flash-image": {"label": "Gemini 2.5 Flash Image", "context_window": 1048576},
            "gemini-3-pro-image-preview": {"label": "Gemini 3 Pro Image Preview", "context_window": 1048576},
            "gemini-3-pro-preview": {"label": "Gemini 3 Pro Preview", "context_window": 1048576},
            "gemini-3.1-pro-preview": {"label": "Gemini 3.1 Pro Preview", "context_window": 1048576},
        }
    },
    "grok": {
        "label": "Grok (xAI)", "website": "https://x.ai/",
        "models": {
            "grok-4.3": {"label": "Grok 4.3", "context_window": 131072},
            "grok-4": {"label": "Grok 4", "context_window": 131072},
            "grok-3": {"label": "Grok 3", "context_window": 131072},
            "grok-3-fast": {"label": "Grok 3 Fast", "context_window": 131072},
            "grok-3-mini": {"label": "Grok 3 Mini", "context_window": 131072},
            "grok-3-mini-fast": {"label": "Grok 3 Mini Fast", "context_window": 131072},
        }
    },
    "volcengine": {
        "label": "火山引擎", "website": "https://console.volcengine.com/ark/region",
        "models": {
            "deepseek-r1": {"label": "DeepSeek R1", "context_window": 65536},
            "deepseek-r1-distill-qwen-32b": {"label": "R1 Distill Qwen 32B", "context_window": 32768},
            "deepseek-r1-distill-qwen-7b": {"label": "R1 Distill Qwen 7B", "context_window": 32768},
            "deepseek-v3": {"label": "DeepSeek V3", "context_window": 65536},
            "doubao-seed-1.8": {"label": "Doubao Seed 1.8", "context_window": 131072},
            "doubao-seed-2.0-pro": {"label": "Doubao Seed 2.0 Pro", "context_window": 131072},
            "doubao-seed-2.0-lite": {"label": "Doubao Seed 2.0 Lite", "context_window": 32768},
            "doubao-seed-2.0-code-preview": {"label": "Doubao Seed 2.0 Code", "context_window": 131072},
            "doubao-seed-2.0-mini": {"label": "Doubao Seed 2.0 Mini", "context_window": 32768},
            "doubao-lite-4k": {"label": "Doubao Lite 4K", "context_window": 4096},
            "doubao-lite-32k": {"label": "Doubao Lite 32K", "context_window": 32768},
            "doubao-lite-128k": {"label": "Doubao Lite 128K", "context_window": 131072},
            "doubao-pro-32k": {"label": "Doubao Pro 32K", "context_window": 32768},
            "doubao-pro-256k": {"label": "Doubao Pro 256K", "context_window": 262144},
        }
    },
    "hunyuan": {
        "label": "腾讯混元", "website": "https://cloud.tencent.com/product/hunyuan",
        "models": {
            "hunyuan-pro": {"label": "Hunyuan Pro", "context_window": 32768},
            "hunyuan-standard": {"label": "Hunyuan Standard", "context_window": 32768},
            "hunyuan-lite": {"label": "Hunyuan Lite", "context_window": 4096},
            "hunyuan-standard-256k": {"label": "Hunyuan Standard 256K", "context_window": 262144},
            "hunyuan-vision": {"label": "Hunyuan Vision", "context_window": 32768},
            "hunyuan-code": {"label": "Hunyuan Code", "context_window": 32768},
            "hunyuan-turbo": {"label": "Hunyuan Turbo", "context_window": 32768},
            "hunyuan-turbos-latest": {"label": "Hunyuan TurboS", "context_window": 32768},
        }
    },
    "openrouter": {
        "label": "OpenRouter", "website": "https://openrouter.ai/",
        "models": {}
    },
    "qwen": {
        "label": "Qwen (通义千问)", "website": "https://www.qianwen.com/",
        "models": {
            "Qwen3.7-PLUS": {"label": "Qwen3.7-PLUS", "context_window": 131072},
            "Qwen3.7-MAX": {"label": "Qwen3.7-MAX", "context_window": 131072},
            "HappyHorse-T2V": {"label": "HappyHorse-T2V", "context_window": 32768},
            "deepseek-V4-flash": {"label": "DeepSeek V4 Flash", "context_window": 65536},
            "GLM-5.2": {"label": "GLM-5.2", "context_window": 131072},
        }
    },
    "ollama": {
        "label": "Ollama (本地)", "website": "https://ollama.com/",
        "models": {}
    },
}

# Write creators/models.json
(ROOT / "creators" / "models.json").write_text(
    json.dumps(CREATORS, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"creators/models.json: {len(CREATORS)} creators")

# Provider manifest definitions (name -> (base_url, creator, description))
PROVIDER_DEFS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "creator": "deepseek",
        "desc": "DeepSeek — V4 Flash / V4 Pro",
        "default_model": "deepseek-v4-flash",
        "context_window": 65536,
        "env_keys": ["DEEPSEEK_API_KEY"],
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "creator": "zhipu",
        "desc": "智谱 GLM — 全系列模型 (Flash/Air/Pro/V)",
        "default_model": "glm-4.5-flash",
        "context_window": 131072,
        "env_keys": ["GLM_API_KEY"],
    },
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "creator": "siliconflow",
        "desc": "硅基流动 — 国产低成本代理 (Qwen/DeepSeek)",
        "default_model": "DeepSeek-V3.2",
        "context_window": 65536,
        "env_keys": ["SILICONFLOW_API_KEY"],
    },
    "minimax": {
        "base_url": "https://api.minimax.chat/v1",
        "creator": "minimax",
        "desc": "MiniMax — M2.7 / M3 系列",
        "default_model": "MiniMax-M2.7",
        "context_window": 131072,
        "env_keys": ["MINIMAX_API_KEY"],
    },
    "moonshot": {
        "base_url": "https://api.moonshot.cn/v1",
        "creator": "moonshot",
        "desc": "月之暗面 Kimi — K2.5 / K2.6 / K2.7 Code",
        "default_model": "kimi-k2.5",
        "context_window": 131072,
        "env_keys": ["MOONSHOT_API_KEY"],
    },
    "anthropic": {
        "base_url": "",
        "creator": "anthropic",
        "desc": "Anthropic Claude — Sonnet/Opus/Haiku 全系列",
        "default_model": "claude-sonnet-4-5",
        "context_window": 200000,
        "env_keys": ["ANTHROPIC_API_KEY"],
        "api_mode": "anthropic_native",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "creator": "openai",
        "desc": "OpenAI — GPT-5 全系列",
        "default_model": "gpt-5",
        "context_window": 128000,
        "env_keys": ["OPENAI_API_KEY"],
    },
    "azure": {
        "base_url": "https://YOUR_RESOURCE.openai.azure.com/openai/v1",
        "creator": "azure",
        "desc": "Azure AI Foundry — 企业级 OpenAI 服务",
        "default_model": "gpt-4o",
        "context_window": 128000,
        "env_keys": ["AZURE_OPENAI_API_KEY"],
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "creator": "gemini",
        "desc": "Google Gemini — 2.5/3/3.1 全系列 (OpenAI兼容)",
        "default_model": "gemini-2.5-flash",
        "context_window": 1048576,
        "env_keys": ["GEMINI_API_KEY"],
    },
    "grok": {
        "base_url": "https://api.x.ai/v1",
        "creator": "grok",
        "desc": "Grok (xAI) — Grok 3/4 全系列",
        "default_model": "grok-4.3",
        "context_window": 131072,
        "env_keys": ["GROK_API_KEY"],
    },
    "volcengine": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "creator": "volcengine",
        "desc": "火山引擎 — DeepSeek/Doubao 全系列",
        "default_model": "deepseek-v3",
        "context_window": 65536,
        "env_keys": ["VOLCENGINE_API_KEY"],
    },
    "hunyuan": {
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "creator": "hunyuan",
        "desc": "腾讯混元 — Pro/Standard/Lite/Vision/Code",
        "default_model": "hunyuan-pro",
        "context_window": 32768,
        "env_keys": ["HUNYUAN_API_KEY"],
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "creator": "openrouter",
        "desc": "OpenRouter — 300+ 模型统一网关",
        "default_model": "deepseek/deepseek-chat",
        "context_window": 65536,
        "env_keys": ["OPENROUTER_API_KEY"],
    },
    "qwen": {
        "base_url": "https://api.qianwen.com/v1",
        "creator": "qwen",
        "desc": "Qwen (通义千问) — Qwen3.7-PLUS/MAX + 多模型代理",
        "default_model": "Qwen3.7-PLUS",
        "context_window": 131072,
        "env_keys": ["QWEN_API_KEY"],
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "creator": "ollama",
        "desc": "Ollama — 本地大模型 (Llama/Qwen/DeepSeek)",
        "default_model": "llama3.1",
        "context_window": 131072,
        "env_keys": ["OLLAMA_API_KEY"],
    },
    "lmstudio": {
        "base_url": "http://localhost:1234/v1",
        "creator": None,
        "desc": "LM Studio — 图形化本地模型管理",
        "default_model": "local-model",
        "context_window": 8192,
        "env_keys": ["LMSTUDIO_API_KEY"],
    },
}

# Write / update provider manifests
for name, defn in PROVIDER_DEFS.items():
    prov_dir = ROOT / "providers" / name
    prov_dir.mkdir(parents=True, exist_ok=True)

    api_mode = defn.get("api_mode", "openai_compat")
    website = CREATORS.get(defn.get("creator", ""), {}).get("website", "")

    manifest = {
        "kind": "provider",
        "name": name,
        "version": "0.2.0",
        "description": defn["desc"],
        "api_mode": api_mode,
        "config": {
            "base_url": defn["base_url"],
            "default_model": defn["default_model"],
            "context_window": defn["context_window"],
            "supports_function_calling": True,
            "supports_streaming": True,
            "tokenizer_encoding": "cl100k_base",
        },
        "env_keys": defn["env_keys"],
    }

    if defn.get("creator"):
        manifest["config"]["creator"] = defn["creator"]

    if website:
        manifest["config"]["website"] = website

    (prov_dir / "manifest.yaml").write_text(
        yaml.dump(manifest, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8")

    # Ensure __init__.py and client.py exist
    (prov_dir / "__init__.py").write_text(
        f'"""{defn["desc"]}"""\n', encoding="utf-8")

    client_path = prov_dir / "client.py"
    if not client_path.exists():
        if api_mode == "openai_compat":
            class_name = "".join(w.capitalize() for w in name.replace("-", "_").split("_")) + "Provider"
            client_path.write_text(
                f'"""{defn["desc"]}"""\n'
                f'from providers.openai_compat import OpenAICompatProvider as {class_name}\n',
                encoding="utf-8")
        else:
            client_path.write_text(
                f'"""{defn["desc"]} — native API provider."""\n'
                f'# See providers/{name}/manifest.yaml for configuration.\n',
                encoding="utf-8")

    print(f"  {name}: {defn['desc']}")

# Remove deprecated kimi directory (merged into moonshot)
kimi_dir = ROOT / "providers" / "kimi"
if kimi_dir.exists():
    shutil.rmtree(kimi_dir)
    print("\n  REMOVED deprecated providers/kimi (merged into moonshot)")

# Remove deprecated glm directory (replaced by zhipu)
for old in ["glm"]:
    old_dir = ROOT / "providers" / old
    if old_dir.exists():
        shutil.rmtree(old_dir)
        print(f"  REMOVED deprecated providers/{old} (replaced)")

print(f"\nDone! {len(PROVIDER_DEFS)} providers configured.")
