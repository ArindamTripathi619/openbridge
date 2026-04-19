# LLM API Rotator: API Endpoints & Programmatic Access Report

This report provides a detailed guide on how to integrate and use your local LLM API rotation service.

## 1. Connection Details

The service runs locally and mimics the OpenAI API standard, making it compatible with almost any LLM client.

- **Base URL:** `http://localhost:8000/v1`
- **Default Port:** `8000`
- **Auth Key:** Not required (you can pass any dummy string like `sk-xxxx`).

## 2. Available Models (50 virtual models across 5 providers)

The service rotates through your underlying keys whenever one hits a rate limit. Key rotation per provider:
- **Groq**: 17 API keys × 7 models = 119 entries
- **Gemini**: 5 API keys × 2 models = 10 entries
- **Scaleway**: 21 API keys × 12 models = 252 entries
- **Mistral**: 2 API keys × 15 models = 30 entries
- **OpenRouter**: 11 API keys × 14 models = 154 entries

### Groq
| Model Name | Underlying Model | Speed | Best Used For |
| :--- | :--- | :--- | :--- |
| `groq-llama` | Llama 3.3 70B | 280 tps | Flagship chat & coding |
| `groq-llama-small` | Llama 3.1 8B | 560 tps | Ultra-fast lightweight tasks |
| `groq-scout` | Llama 4 Scout 17B | 750 tps | Fast multimodal (vision + text) |
| `groq-gpt-oss` | GPT-OSS 120B | 500 tps | Best reasoning |
| `groq-gpt-oss-mini` | GPT-OSS 20B | 1000 tps | Fastest, good quality |
| `groq-qwen` | Qwen3 32B | 400 tps | Multilingual & reasoning |
| `groq-kimi` | Kimi K2 0905 | 200 tps | 262K context, agentic coding |

### Gemini
| Model Name | Underlying Model | Best Used For |
| :--- | :--- | :--- |
| `gemini-flash` | Gemini 2.0 Flash | Vision & long context |
| `gemini-image` | Imagen 3 / Nano Banana | Image generation |

### Scaleway
| Model Name | Underlying Model | Best Used For |
| :--- | :--- | :--- |
| `scw-qwen-235b` | Qwen3 235B A22B | Largest open model |
| `scw-gpt-oss` | GPT-OSS 120B | Reasoning |
| `scw-llama-70b` | Llama 3.3 70B Instruct | Chat & coding |
| `scw-llama-8b` | Llama 3.1 8B Instruct | Fast lightweight |
| `scw-deepseek-r1` | DeepSeek R1 Distill 70B | Reasoning (chain-of-thought) |
| `scw-devstral` | Devstral 2 123B | Coding agent |
| `scw-gemma` | Gemma 3 27B IT | Compact & capable |
| `scw-holo` | Holo2 30B | Creative writing |
| `scw-mistral-nemo` | Mistral Nemo 2407 12B | Lightweight Mistral |
| `scw-mistral-small` | Mistral Small 3.2 24B | Balanced Mistral |
| `scw-pixtral` | Pixtral 12B 2409 | Vision model |
| `scw-qwen-coder` | Qwen3 Coder 30B A3B | Code generation |

### Mistral
| Model Name | Underlying Model | Best Used For |
| :--- | :--- | :--- |
| `mistral-large` | Mistral Large Latest | Flagship reasoning |
| `mistral-medium` | Mistral Medium Latest | Balanced quality |
| `mistral-small` | Mistral Small Latest | Fast & efficient |
| `mistral-codestral` | Codestral Latest | Code generation |
| `mistral-devstral` | Devstral Latest | Coding agent |
| `mistral-devstral-medium` | Devstral Medium Latest | Coding (balanced) |
| `mistral-devstral-small` | Devstral Small Latest | Coding (fast) |
| `mistral-magistral-med` | Magistral Medium Latest | Reasoning |
| `mistral-magistral-sm` | Magistral Small Latest | Reasoning (fast) |
| `mistral-ministral-3b` | Ministral 3B Latest | Ultra-lightweight |
| `mistral-ministral-8b` | Ministral 8B Latest | Lightweight |
| `mistral-ministral-14b` | Ministral 14B Latest | Mid-range |
| `mistral-pixtral` | Pixtral Large Latest | Vision (large) |
| `mistral-nemo` | Open Mistral Nemo | Open-weight |
| `mistral-tiny` | Mistral Tiny Latest | Fastest Mistral |

### OpenRouter (Free Tier)
| Model Name | Underlying Model | Best Used For |
| :--- | :--- | :--- |
| `or-llama-70b` | Llama 3.3 70B | Chat & coding |
| `or-gpt-oss` | GPT-OSS 120B | Reasoning |
| `or-gpt-oss-mini` | GPT-OSS 20B | Fast reasoning |
| `or-qwen-coder` | Qwen3 Coder | Code generation |
| `or-qwen-next` | Qwen3 Next 80B | Latest Qwen |
| `or-gemma-27b` | Gemma 3 27B | Compact & capable |
| `or-gemma-12b` | Gemma 3 12B | Lightweight |
| `or-mistral-small` | Mistral Small 3.1 | Fast Mistral |
| `or-hermes-405b` | Hermes 3 405B | Largest open model |
| `or-nemotron-30b` | Nemotron 3 Nano 30B | NVIDIA reasoning |
| `or-nemotron-9b` | Nemotron Nano 9B | NVIDIA lightweight |
| `or-step-flash` | Step 3.5 Flash | 256K context |
| `or-glm` | GLM 4.5 Air | Chinese + English |
| `or-trinity` | Trinity Large | Arcee hybrid |

## 3. Programmatic Access Examples

### Python (using OpenAI library)
```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="sk-local"
)

# Chat with Groq (Fast)
response = client.chat.completions.create(
    model="groq-llama",
    messages=[{"role": "user", "content": "Hello!"}]
)
print(response.choices[0].message.content)
```

### Curl (CLI)
```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "groq-llama",
    "messages": [{"role": "user", "content": "How does key rotation work?"}]
  }'
```

---
*Generated Documentation Template*
