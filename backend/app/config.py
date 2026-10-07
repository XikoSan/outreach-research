from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    database_url: str = 'sqlite:///./data/app.db'
    data_dir: Path = Path('data')
    openai_api_key: str = ''
    model_discovery: str = 'gpt-6-luna'
    model_extraction: str = 'gpt-6-luna'
    model_synthesis: str = 'gpt-6-luna'
    model_segmentation: str = 'gpt-6.1-sol'
    model_prompt: str = 'gpt-6.1-sol'

settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
