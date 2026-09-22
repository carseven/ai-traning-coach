import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture
def cli(monkeypatch, tmp_path):
    module_path = Path(__file__).parents[1] / "zepp_cli.py"
    spec = importlib.util.spec_from_file_location("zepp_cli", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    monkeypatch.setenv("RUNNING_COACH_ZEPP_DATA_DIR", str(tmp_path))
    return module


def test_configure_from_environment_stores_secret_outside_configuration(cli, monkeypatch, capsys):
    saved = {}
    monkeypatch.setenv("ZEPP_APP_TOKEN", "secret-token")
    monkeypatch.setattr(
        cli.keyring,
        "set_password",
        lambda service, account, token: saved.update(
            {"service": service, "account": account, "token": token}
        ),
    )

    exit_code = cli.main(["configure", "--from-env", "--region", "eu", "--user-id", "42", "--json"])

    assert exit_code == 0
    assert saved == {"service": "running-coach-zepp", "account": "42", "token": "secret-token"}
    assert json.loads((cli.app_directory() / "zepp.json").read_text()) == {
        "region": "eu",
        "user_id": "42",
    }
    assert "secret-token" not in capsys.readouterr().out


def test_doctor_reports_unconfigured_cache_without_creating_database(cli, capsys):
    exit_code = cli.main(["doctor", "--json"])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["configured"] is False
    assert payload["database_exists"] is False
    assert not cli.database_path().exists()


def test_doctor_reports_healthy_initialized_database(cli, monkeypatch, capsys):
    configuration = cli.Configuration(region="eu", user_id="42")
    cli.write_configuration(configuration)
    cli.initialize_database(cli.database_path())
    monkeypatch.setattr(cli, "has_token", lambda _: True)

    exit_code = cli.main(["doctor", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["database_exists"] is True
    assert payload["database_healthy"] is True


def test_get_token_migrates_default_credential_after_user_id_discovery(cli, monkeypatch):
    credentials = {("running-coach-zepp", "default"): "secret-token"}

    monkeypatch.setattr(
        cli.keyring,
        "get_password",
        lambda service, account: credentials.get((service, account)),
    )
    monkeypatch.setattr(
        cli.keyring,
        "set_password",
        lambda service, account, token: credentials.__setitem__((service, account), token),
    )
    monkeypatch.setattr(
        cli.keyring,
        "delete_password",
        lambda service, account: credentials.pop((service, account)),
    )

    token = cli.get_token(cli.Configuration(region="eu", user_id="42"))

    assert token == "secret-token"
    assert credentials == {("running-coach-zepp", "42"): "secret-token"}


def test_nonnegative_float_discards_zepp_missing_value_sentinels(cli):
    assert cli.nonnegative_float(-1) is None
    assert cli.nonnegative_float("350") == 350


def test_sync_writes_all_supported_data_types_and_advances_cursors(cli, monkeypatch, capsys):
    cli.write_configuration(cli.Configuration(region="eu", user_id="42"))
    monkeypatch.setattr(cli.keyring, "get_password", lambda *_: "secret-token")

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Client:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def get(self, url, params):
            if "weightRecords" in url:
                return Response(
                    {
                        "items": [
                            {
                                "id": "weight-1",
                                "generatedTime": 1704153600,
                                "summary": {"weight": 70.5, "fatRate": 15.2},
                            }
                        ]
                    }
                )
            if "history" in url:
                assert params["userid"] == "42"
                assert params["need_sub_data"] == 1
                return Response(
                    {
                        "data": {
                            "summary": [
                                {
                                    "trackid": "run-1",
                                    "type": 1,
                                    "sport_title": "Morning run",
                                    "start_time": 1704153600,
                                    "run_time": 1800,
                                    "dis": 5000,
                                    "calorie": 350,
                                    "avg_heart_rate": 142,
                                    "min_heart_rate": 108,
                                    "max_heart_rate": 171,
                                    "elevationGain": 220,
                                    "elevationLoss": 218,
                                }
                            ]
                        }
                    }
                )
            if params["query_type"] == "detail":
                return Response(
                    {
                        "data": [
                            {
                                "date_time": "2024-01-02",
                                "data_hr": base64.b64encode(bytes([0, 61, 255])).decode(),
                            }
                        ]
                    }
                )
            return Response(
                {
                    "data": [
                        {
                            "date_time": "2024-01-02",
                            "summary": {
                                "stp": {"ttl": 1000, "dis": 750, "cal": 100},
                                "slp": {"st": 1704153600, "ed": 1704182400, "rhr": 52},
                            },
                        }
                    ]
                }
            )

    monkeypatch.setattr(cli.httpx, "Client", Client)

    exit_code = cli.main(
        ["sync", "--start-date", "2024-01-01", "--end-date", "2024-01-02", "--json"]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["failed_data_types"] == []
    with cli.sqlite3.connect(cli.database_path()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM daily_activity").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM sleep_sessions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM heart_rate_samples").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM workouts").fetchone()[0] == 1
        assert connection.execute("SELECT calories_kcal FROM workouts").fetchone()[0] == 350
        assert (
            connection.execute("SELECT workout_name FROM workouts").fetchone()[0] == "Morning run"
        )
        assert connection.execute(
            "SELECT average_heart_rate_bpm, min_heart_rate_bpm, max_heart_rate_bpm, "
            "elevation_gain_m, elevation_loss_m FROM workouts"
        ).fetchone() == (142.0, 108.0, 171.0, 2.2, 2.18)
        assert connection.execute("SELECT COUNT(*) FROM body_measurements").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0] == 5