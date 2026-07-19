import pytest

from solar_monitor.config import AppConfig, ConfigError, Secret

BASE_ENV = {
    "RENOVIGI_USERNAME": "owner",
    "RENOVIGI_PASSWORD": "private-password",
    "RENOVIGI_PLANT_ID": "1234567",
    "SOLAR_LATITUDE": "-16.65",
    "SOLAR_LONGITUDE": "-49.49",
    "SOLAR_TIMEZONE": "America/Sao_Paulo",
}


def test_loads_valid_non_live_configuration() -> None:
    config = AppConfig.from_env(BASE_ENV)

    assert config.generation.username == "owner"
    assert config.generation.password.reveal() == "private-password"
    assert config.location.timezone.key == "America/Sao_Paulo"
    assert config.telegram is None
    assert config.live_monitoring_enabled is False


def test_documented_baseline_date_overrides_default() -> None:
    config = AppConfig.from_env({**BASE_ENV, "SOLAR_BASELINE_DATE": "2026-05-31"})
    assert config.baseline_day.isoformat() == "2026-05-31"


def test_optional_energy_rate_accepts_zero_and_positive_values() -> None:
    assert (
        AppConfig.from_env({**BASE_ENV, "SOLAR_ENERGY_RATE_BRL_PER_KWH": "0"})
        .energy_rate_brl_per_kwh
        == 0
    )
    assert AppConfig.from_env(
        {**BASE_ENV, "SOLAR_ENERGY_RATE_BRL_PER_KWH": "0.95"}
    ).energy_rate_brl_per_kwh == pytest.approx(0.95)


@pytest.mark.parametrize("value", ["-0.01", "invalid", "nan", "inf"])
def test_rejects_invalid_energy_rate(value: str) -> None:
    with pytest.raises(ConfigError, match="SOLAR_ENERGY_RATE_BRL_PER_KWH"):
        AppConfig.from_env({**BASE_ENV, "SOLAR_ENERGY_RATE_BRL_PER_KWH": value})


def test_secret_does_not_render_its_value() -> None:
    secret = Secret("canary-value")

    assert "canary-value" not in repr(secret)
    assert "canary-value" not in str(secret)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("SOLAR_LATITUDE", "91", "between -90 and 90"),
        ("SOLAR_LONGITUDE", "not-a-number", "must be a number"),
        ("SOLAR_TIMEZONE", "Mars/Olympus", "valid IANA timezone"),
        ("ENABLE_LIVE_MONITORING", "yes", "either true or false"),
    ],
)
def test_rejects_invalid_environment_values(name: str, value: str, message: str) -> None:
    env = {**BASE_ENV, name: value}

    with pytest.raises(ConfigError, match=message):
        AppConfig.from_env(env)


def test_rejects_partial_telegram_configuration() -> None:
    with pytest.raises(ConfigError, match="must be set together"):
        AppConfig.from_env({**BASE_ENV, "TELEGRAM_BOT_TOKEN": "token"})


def test_live_monitoring_requires_telegram_configuration() -> None:
    with pytest.raises(ConfigError, match="required when live monitoring"):
        AppConfig.from_env({**BASE_ENV, "ENABLE_LIVE_MONITORING": "true"})


def test_loads_live_configuration_without_exposing_token() -> None:
    config = AppConfig.from_env(
        {
            **BASE_ENV,
            "ENABLE_LIVE_MONITORING": "TRUE",
            "TELEGRAM_BOT_TOKEN": "canary-token",
            "TELEGRAM_CHAT_ID": "42",
        }
    )

    assert config.telegram is not None
    assert config.telegram.bot_token.reveal() == "canary-token"
    assert "canary-token" not in repr(config)
