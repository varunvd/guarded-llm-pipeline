"""Runtime settings, read from environment variables (or a local .env file)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./guarded.db"
    # Empty means "no Redis": tickets are processed in-process. Used by tests
    # and by single-process local runs.
    redis_url: str = ""

    llm_provider: str = "simulated"  # "simulated" | "anthropic"
    llm_model: str = "claude-opus-5-5"

    # Send a reply without human review when it passes validation and carries
    # no risk flag. When false, everything waits in the review queue.
    auto_send: bool = True
    max_attempts: int = 3

    # Refunds above this amount always need a person, however clean the reply.
    refund_review_limit_cents: int = 10_000

    # Prices used for the cost column only.
    price_input_per_mtok: float = 4.00
    price_output_per_mtok: float = 20.00

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return round(
            input_tokens / 1_000_000 * self.price_input_per_mtok
            + output_tokens / 1_000_000 * self.price_output_per_mtok,
            6,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
