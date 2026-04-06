from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_STARS = 5


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    discord_token: str
    discord_channel_id: int
    github_token: str

    supabase_url: str
    supabase_key: str

    gemini_api_key: str
    llm: str

    repos_per_keyword: int = Field(default=3, ge=1, le=10)
    max_post_per_run: int = Field(default=3, ge=1, le=20)
    quick_max_eval: int = Field(default=20, ge=5, le=100)
    min_score_to_post: float = Field(default=7.5, ge=1.0, le=10.0)
    llm_json_object_mode: bool = Field(default=False)
    llm_eval_max_attempts: int = Field(default=2, ge=1, le=4)
    llm_http_retries: int = Field(default=1, ge=0, le=3)

    search_keywords: list[str] = Field(default_factory=lambda: [
        # Interleaved by category so each daily cycle gets diverse signal
        # Day 1
        "agent", "llm", "sandbox", "saas", "voice-agent", "trading-agent", "cli", "scraper", "openclaw", "qwen",
        # Day 2
        "multi-agent", "rag", "memory", "self-hosted", "text-to-speech", "ai-finance", "devtool", "data-pipeline", "mcp", "gemma",
        # Day 3
        "coding-agent", "ollama", "observability", "open-source", "realtime-voice", "code-assistant", "automation", "search-engine", "claw", "kimi",
        # Day 4
        "swe-agent", "local-llm", "evals", "no-code", "virtual-character", "headless-browser", "workflow", "web-scraping", "agentic", "glm",
        # Day 5
        "langgraph", "vllm", "red-team", "internal-tool", "ai-security", "ai-platform", "orchestration", "skills", "browser-use", "bitnet",
        # Day 6
        "crewai", "embeddings", "local-first", "dashboard", "ai-gateway", "hooks", "deepagent", "computer-use", "voltagent", "claude",
        # Day 7
        "autogen", "vector-database", "agent-harness", "builder", "ai-proxy", "web-agent", "inference", "copilot", "openai", "anthropic",
    ])


settings = Settings()  # type: ignore[call-arg]
