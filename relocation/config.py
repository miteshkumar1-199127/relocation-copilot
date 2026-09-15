import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    nebius_key: str = os.getenv("NEBIUS_API_KEY", "")
    nebius_model: str = os.getenv("NEBIUS_MODEL", "")
    research_model: str = os.getenv("NEBIUS_RESEARCH_MODEL", "zai-org/GLM-5.3-Flash")
    embed_model: str = os.getenv("NEBIUS_EMBED_MODEL", "Qwen/Qwen3-Embedding-8B")
    langsmith_key: str = os.getenv("LANGSMITH_API_KEY", "")
    langsmith_project: str = os.getenv("LANGSMITH_PROJECT", "relocation-copilot")
    you_key: str = os.getenv("YOU_API_KEY", "")
    tavily_key: str = os.getenv("TAVILY_API_KEY", "")
    data_dir: Path = Path(os.getenv("RELOCATION_DATA_DIR", "relocation_data"))
    persist_data: bool = os.getenv("PERSIST_RELOCATION_DATA", "false").strip().lower() in ("1", "true", "yes")

    def validate_live(self) -> None:
        missing = [name for name, value in [("NEBIUS_API_KEY", self.nebius_key), ("NEBIUS_MODEL", self.nebius_model)] if not value]
        if missing:
            raise ValueError("Missing required environment variables: " + ", ".join(missing))

    def prepare(self) -> None:
        if self.persist_data:
            self.data_dir.mkdir(parents=True, exist_ok=True)
