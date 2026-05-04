from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Pyro API
    pyro_base_url: str
    pyro_api_key: str
    pyro_login_id: str
    pyro_password: str
    pyro_mpin: str
    pyro_secret_key: str

    # Oracle DB (BCD table only)
    oracle_user: str
    oracle_password: str
    oracle_dsn: str              # host:port/service_name

    # Postgres DB (all other tables)
    pg_host: str
    pg_port: int = 5432
    pg_database: str
    pg_user: str
    pg_password: str
    pg_min_conn: int = 2
    pg_max_conn: int = 10

    # Deployment
    callback_base_url: str

    # Batch population schedule
    batch_population_hour: int = 7
    batch_population_minute: int = 0
    oracle_batch_fetch_size: int = 500

    # Recharge processing
    recharge_batch_size: int = 500


settings = Settings()