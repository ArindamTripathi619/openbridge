import os
import requests
from typing import Dict, Any, Optional, List
from abc import ABC, abstractmethod

# Local LLM Rotator Presets from API_REPORT.md
ROTATOR_PRESETS = {
    "Groq": [
        ("groq-llama", "Llama 3.3 70B (Flagship)"),
        ("groq-llama-small", "Llama 3.1 8B (Fast)"),
        ("groq-scout", "Llama 4 Scout 17B (Vision)"),
        ("groq-gpt-oss", "GPT-OSS 120B (Reasoning)"),
        ("groq-gpt-oss-mini", "GPT-OSS 20B (Fast Reasoning)"),
        ("groq-qwen", "Qwen3 32B (Multilingual)"),
        ("groq-kimi", "Kimi K2 0905 (Long Context)"),
    ],
    "Gemini": [
        ("gemini-flash", "Gemini 2.0 Flash (Vision/Context)"),
    ],
    "Scaleway": [
        ("scw-qwen-235b", "Qwen3 235B (Large)"),
        ("scw-gpt-oss", "GPT-OSS 120B (Reasoning)"),
        ("scw-llama-70b", "Llama 3.3 70B"),
        ("scw-llama-8b", "Llama 3.1 8B"),
        ("scw-deepseek-r1", "DeepSeek R1 (Reasoning)"),
        ("scw-devstral", "Devstral 2 123B (Code)"),
        ("scw-gemma", "Gemma 3 27B"),
        ("scw-mistral-small", "Mistral Small 3.2 24B"),
        ("scw-pixtral", "Pixtral 12B (Vision)"),
        ("scw-qwen-coder", "Qwen3 Coder 30B"),
    ],
    "Mistral": [
        ("mistral-large", "Mistral Large (Reasoning)"),
        ("mistral-medium", "Mistral Medium"),
        ("mistral-small", "Mistral Small"),
        ("mistral-codestral", "Codestral (Code)"),
        ("mistral-devstral", "Devstral (Coding Agent)"),
        ("mistral-ministral-8b", "Ministral 8B"),
        ("mistral-nemo", "Open Mistral Nemo"),
        ("mistral-tiny", "Mistral Tiny"),
    ],
    "OpenRouter": [
        ("or-llama-70b", "Llama 3.3 70B"),
        ("or-gpt-oss", "GPT-OSS 120B"),
        ("or-qwen-coder", "Qwen3 Coder"),
        ("or-gemma-27b", "Gemma 3 27B"),
        ("or-mistral-small", "Mistral Small 3.1"),
        ("or-hermes-405b", "Hermes 3 405B"),
        ("or-nemotron-30b", "Nemotron 3 Nano"),
        ("or-step-flash", "Step 3.5 Flash (256K)"),
        ("or-glm", "GLM 4.5 Air"),
    ]
}


class LLMError(Exception):
    """LLM error."""
    pass


class BaseLLMClient(ABC):
    """Base LLM client interface."""
    
    @abstractmethod
    def analyze(self, prompt: str) -> str:
        """Analyze with LLM."""
        pass

    def _handle_error(self, provider: str, e: Exception, endpoint: str = None):
        """Standardized error handling for all providers.
        
        Args:
            provider: Provider name for error messages
            e: The exception that was raised
            endpoint: Optional endpoint URL for connection errors
        """
        error_msg = str(e).lower()
        
        # Detect connection issues
        if any(word in error_msg for word in ["connection", "timeout", "unreachable", "refused"]):
            location = f" at {endpoint}" if endpoint else ""
            raise LLMError(f"CONNECTION_ERROR: {provider} server not reachable{location}. {e}")
        
        # Detect quota/billing issues
        if any(word in error_msg for word in ["quota", "rate limit", "insufficient", "billing", "credits", "429"]):
            raise LLMError(f"QUOTA_EXHAUSTED: {provider} quota exceeded or rate limited. {e}")
            
        # Detect authentication issues
        if any(word in error_msg for word in ["invalid", "authentication", "api key", "unauthorized", "401"]):
            raise LLMError(f"AUTH_ERROR: {provider} authentication failed. Check your API key. {e}")
            
        raise LLMError(f"{provider} API error: {e}")


class OpenAIClient(BaseLLMClient):
    """OpenAI client."""
    
    def __init__(self, api_key: str, model: str = "gpt-4o-mini", base_url: Optional[str] = None):
        try:
            from openai import OpenAI
        except ImportError:
            raise LLMError("openai package not installed. Run: pip install openai")
        
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.base_url = base_url
    
    def analyze(self, prompt: str) -> str:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a log analysis expert helping monitor system processes."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=500,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            self._handle_error("OpenAI", e, endpoint=self.base_url)


class AnthropicClient(BaseLLMClient):
    """Anthropic client."""
    
    def __init__(self, api_key: str, model: str = "claude-3-5-haiku-20241022"):
        try:
            from anthropic import Anthropic
        except ImportError:
            raise LLMError("anthropic package not installed. Run: pip install anthropic")
        
        self.client = Anthropic(api_key=api_key)
        self.model = model
    
    def analyze(self, prompt: str) -> str:
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=500,
                messages=[
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
            )
            return response.content[0].text
        except Exception as e:
            self._handle_error("Anthropic", e)


class GroqClient(BaseLLMClient):
    """Groq client."""
    
    def __init__(self, api_key: str, model: str = "llama-3.3-70b-versatile"):
        try:
            from groq import Groq
        except ImportError:
            raise LLMError("groq package not installed. Run: pip install groq")
        
        self.client = Groq(api_key=api_key)
        self.model = model
    
    def analyze(self, prompt: str) -> str:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a log analysis expert helping monitor system processes."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=500,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            self._handle_error("Groq", e)


class OllamaClient(BaseLLMClient):
    """Ollama (local) client."""
    
    def __init__(self, model: str = "llama3.2", base_url: str = "http://localhost:11434"):
        import requests
        self.requests = requests
        self.model = model
        self.base_url = base_url.rstrip("/")
    
    def analyze(self, prompt: str) -> str:
        try:
            response = self.requests.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "You are a log analysis expert helping monitor system processes."},
                        {"role": "user", "content": prompt}
                    ],
                    "stream": False,
                },
                timeout=60,
            )
            response.raise_for_status()
            return response.json()["message"]["content"]
        except Exception as e:
            self._handle_error("Ollama", e, endpoint=self.base_url)


def create_llm_client(config: Dict[str, Any]) -> BaseLLMClient:
    """Create LLM client from configuration."""
    provider = config["provider"].lower()
    
    mapping = {
        "openai": (OpenAIClient, "gpt-4o-mini"),
        "anthropic": (AnthropicClient, "claude-3-5-haiku-20241022"),
        "groq": (GroqClient, "llama-3.3-70b-versatile"),
    }
    
    if provider in mapping:
        cls, default_model = mapping[provider]
        return cls(
            api_key=config["api_key"],
            model=config.get("model", default_model),
            base_url=config.get("base_url")
        )
    elif provider == "local-rotator":
        # Use OpenAI client but pointed at local rotator
        return OpenAIClient(
            api_key=config.get("api_key", "sk-local-rotator"),
            model=config.get("model", "groq-llama"),
            base_url=config.get("base_url", "http://localhost:8000/v1")
        )
    elif provider == "ollama":
        return OllamaClient(
            model=config.get("model", "llama3.2"),
            base_url=config.get("base_url", "http://localhost:11434")
        )
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")
