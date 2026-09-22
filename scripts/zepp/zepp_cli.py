"""Configure and inspect the local Zepp Life cache used by Running Coach."""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import keyring
from platformdirs import user_data_path

APP_NAME = "running-coach"
CREDENTIAL_SERVICE = "running-coach-zepp"
DEFAULT_REGION = "eu"
VALID_REGIONS = {"cn", "eu", "us", "sg"}
CONFIG_FILE = "zepp.json"
DATABASE_FILE = "zepp.sqlite3"
DATA_TYPES = ("daily_activity", "sleep", "heart_rate", "workouts", "body_measurements")
API_BASE = "https://api-mifit.huami.com"
WEIGHT_API_BASE = "https://api-mifit.zepp.com"
SYNC_OVERLAP_DAYS = 7


class CliError(Exception):
    """An expected command-line error with a stable, nonzero exit code."""


@dataclass
class Configuration:
    region: str = DEFAULT_REGION
    user_id: str | None = None


def app_directory() -> Path:
    override = os.environ.get("RUNNING_COACH_ZEPP_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path(user_data_path(APP_NAME, appauthor=False)) / "zepp"


def config_path() -> Path:
    return app_directory() / CONFIG_FILE


def database_path() -> Path:
    return app_directory() / DATABASE_FILE


def load_configuration() -> Configuration | None:
    path = config_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return Configuration(region=raw["region"], user_id=raw.get("user_id"))
    except (OSError, ValueError, KeyError) as error:
        raise CliError(f"Invalid configuration at {path}: {error}") from error


def write_configuration(configuration: Configuration) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(configuration), indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def credential_account(configuration: Configuration) -> str:
    return configuration.user_id or "default"


def store_token(configuration: Configuration, token: str) -> None:
    try:
        keyring.set_password(CREDENTIAL_SERVICE, credential_account(configuration), token)
    except keyring.errors.KeyringError as error:
        raise CliError(f"Could not store the Zepp token in the system keyring: {error}") from error


def has_token(configuration: Configuration) -> bool:
    try:
        token = keyring.get_password(CREDENTIAL_SERVICE, credential_account(configuration))
        if token is None and configuration.user_id:
            token = keyring.get_password(CREDENTIAL_SERVICE, "default")
        return token is not None
    except keyring.errors.KeyringError:
        return False


def get_token(configuration: Configuration) -> str:
    try:
        token = keyring.get_password(CREDENTIAL_SERVICE, credential_account(configuration))
        if token is None and configuration.user_id:
            token = keyring.get_password(CREDENTIAL_SERVICE, "default")
            if token is not None:
                keyring.set_password(CREDENTIAL_SERVICE, configuration.user_id, token)
                keyring.delete_password(CREDENTIAL_SERVICE, "default")
    except keyring.errors.KeyringError as error:
        raise CliError(f"Could not read the Zepp token from the system keyring: {error}") from error
    if not token:
        raise CliError("Zepp is not configured. Run 'configure' first.")
    return token


def emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return
    for key, value in payload.items():
        print(f"{key.replace('_', ' ')}: {value}")


def configure_command(args: argparse.Namespace) -> int:
    region = args.region.lower()
    if region not in VALID_REGIONS:
        regions = ", ".join(sorted(VALID_REGIONS))
        raise CliError(f"Unsupported region '{args.region}'. Use one of: {regions}.")

    token = (
        os.environ.get("ZEPP_APP_TOKEN") if args.from_env else getpass.getpass("Zepp app token: ")
    )
    if not token:
        raise CliError("No Zepp app token provided.")

    configuration = Configuration(region=region, user_id=args.user_id)
    store_token(configuration, token)
    write_configuration(configuration)
    emit({"configured": True, "region": region, "user_id": args.user_id}, args.json)
    return 0


def initialize_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sync_runs (
                id INTEGER PRIMARY KEY,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT,
                status TEXT NOT NULL,
                detail TEXT
            );
            CREATE TABLE IF NOT EXISTS sync_state (
                source_type TEXT NOT NULL,
                user_id TEXT NOT NULL,
                data_type TEXT NOT NULL,
                cursor_date TEXT,
                last_attempt_at TEXT,
                last_success_at TEXT,
                records_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                PRIMARY KEY (source_type, user_id, data_type)
            );
            CREATE TABLE IF NOT EXISTS raw_records (
                source_type TEXT NOT NULL,
                user_id TEXT NOT NULL,
                data_type TEXT NOT NULL,
                source_record_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (source_type, user_id, data_type, source_record_id)
            );
            CREATE TABLE IF NOT EXISTS daily_activity (
                user_id TEXT NOT NULL,
                date TEXT NOT NULL,
                steps INTEGER NOT NULL DEFAULT 0,
                distance_m REAL NOT NULL DEFAULT 0,
                active_kcal REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, date)
            );
            CREATE TABLE IF NOT EXISTS sleep_sessions (
                user_id TEXT NOT NULL,
                sleep_id TEXT NOT NULL,
                local_date TEXT NOT NULL,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL,
                time_asleep_minutes INTEGER NOT NULL,
                PRIMARY KEY (user_id, sleep_id)
            );
            CREATE TABLE IF NOT EXISTS heart_rate_samples (
                user_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                sample_type TEXT NOT NULL,
                bpm INTEGER NOT NULL,
                PRIMARY KEY (user_id, timestamp, sample_type)
            );
            CREATE TABLE IF NOT EXISTS workouts (
                user_id TEXT NOT NULL,
                workout_id TEXT NOT NULL,
                local_date TEXT NOT NULL,
                activity_type TEXT NOT NULL,
                workout_name TEXT,
                start_at TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL,
                distance_m REAL,
                calories_kcal REAL,
                average_heart_rate_bpm REAL,
                min_heart_rate_bpm REAL,
                max_heart_rate_bpm REAL,
                elevation_gain_m REAL,
                elevation_loss_m REAL,
                PRIMARY KEY (user_id, workout_id)
            );
            CREATE TABLE IF NOT EXISTS body_measurements (
                user_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                weight_kg REAL NOT NULL,
                body_fat_pct REAL,
                PRIMARY KEY (user_id, timestamp)
            );
            """
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(workouts)")}
        if "calories_kcal" not in columns:
            connection.execute("ALTER TABLE workouts ADD COLUMN calories_kcal REAL")
        if "workout_name" not in columns:
            connection.execute("ALTER TABLE workouts ADD COLUMN workout_name TEXT")
        workout_columns = {
            "average_heart_rate_bpm": "REAL",
            "min_heart_rate_bpm": "REAL",
            "max_heart_rate_bpm": "REAL",
            "elevation_gain_m": "REAL",
            "elevation_loss_m": "REAL",
        }
        for column, column_type in workout_columns.items():
            if column not in columns:
                connection.execute(f"ALTER TABLE workouts ADD COLUMN {column} {column_type}")
        connection.execute("PRAGMA user_version = 5")


def cache_coverage(database: Path) -> dict[str, int]:
    if not database.exists():
        return {data_type: 0 for data_type in DATA_TYPES}
    with sqlite3.connect(database) as connection:
        return {
            data_type: connection.execute(
                "SELECT COUNT(*) FROM sync_state "
                "WHERE data_type = ? AND last_success_at IS NOT NULL",
                (data_type,),
            ).fetchone()[0]
            for data_type in DATA_TYPES
        }


def parse_band_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data", [])
    if isinstance(data, str):
        try:
            data = json.loads(base64.b64decode(data))
        except (ValueError, UnicodeDecodeError) as error:
            raise CliError("Zepp returned an invalid band-data payload.") from error
    if not isinstance(data, list):
        raise CliError("Zepp returned an unexpected band-data payload.")
    return [item for item in data if isinstance(item, dict)]


def parse_summary(item: dict[str, Any]) -> dict[str, Any]:
    summary = item.get("summary", {})
    if isinstance(summary, str):
        try:
            summary = json.loads(base64.b64decode(summary))
        except (ValueError, UnicodeDecodeError) as error:
            raise CliError("Zepp returned an invalid daily summary.") from error
    return summary if isinstance(summary, dict) else {}


def timestamp_iso(value: Any) -> str:
    timestamp = float(value)
    if abs(timestamp) >= 100_000_000_000:
        timestamp /= 1000
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def first_present(item: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        value = item.get(name)
        if value not in (None, ""):
            return value
    return None


def nonnegative_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    parsed = float(value)
    return parsed if parsed >= 0 else None


def centimeters_to_meters(value: Any) -> float | None:
    centimeters = nonnegative_float(value)
    return centimeters / 100 if centimeters is not None else None


def request_json(client: httpx.Client, url: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        response = client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise CliError(f"Zepp request failed: {type(error).__name__}") from error
    if not isinstance(payload, dict):
        raise CliError("Zepp returned an unexpected response.")
    if "code" in payload and payload["code"] != 1:
        raise CliError(
            "Zepp rejected the request. Reconfigure the session if the problem persists."
        )
    return payload


def resolve_user_id(client: httpx.Client, configuration: Configuration, end_date: str) -> str:
    if configuration.user_id:
        return configuration.user_id
    start_date = (date.fromisoformat(end_date) - timedelta(days=30)).isoformat()
    payload = request_json(
        client,
        f"{API_BASE}/v1/data/band_data.json",
        {
            "query_type": "summary",
            "device_type": "android_phone",
            "from_date": start_date,
            "to_date": end_date,
        },
    )
    for item in parse_band_payload(payload):
        user_id = str(item.get("uid", ""))
        if user_id.isdigit():
            configuration.user_id = user_id
            write_configuration(configuration)
            return user_id
    raise CliError("Could not discover a Zepp user ID. Configure with --user-id.")


def sync_window(
    connection: sqlite3.Connection, user_id: str, data_type: str, args: argparse.Namespace
) -> tuple[str, str]:
    end_date = args.end_date or date.today().isoformat()
    if args.start_date:
        return args.start_date, end_date
    if not args.force_full_sync:
        row = connection.execute(
            "SELECT cursor_date FROM sync_state "
            "WHERE source_type = ? AND user_id = ? AND data_type = ?",
            ("cloud_session", user_id, data_type),
        ).fetchone()
        if row and row[0]:
            return (
                date.fromisoformat(row[0]) - timedelta(days=SYNC_OVERLAP_DAYS)
            ).isoformat(), end_date
    return (date.fromisoformat(end_date) - timedelta(days=365)).isoformat(), end_date


def update_sync_state(
    connection: sqlite3.Connection,
    user_id: str,
    data_type: str,
    cursor_date: str | None,
    records_count: int,
    error: str | None = None,
) -> None:
    success = error is None
    connection.execute(
        """
        INSERT INTO sync_state (
            source_type, user_id, data_type, cursor_date, last_attempt_at, last_success_at,
            records_count, last_error
        ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CASE WHEN ? THEN CURRENT_TIMESTAMP END, ?, ?)
        ON CONFLICT(source_type, user_id, data_type) DO UPDATE SET
            cursor_date = CASE WHEN ? THEN excluded.cursor_date ELSE sync_state.cursor_date END,
            last_attempt_at = CURRENT_TIMESTAMP,
            last_success_at = CASE WHEN ? THEN CURRENT_TIMESTAMP
                ELSE sync_state.last_success_at END,
            records_count = excluded.records_count,
            last_error = excluded.last_error
        """,
        (
            "cloud_session",
            user_id,
            data_type,
            cursor_date,
            success,
            records_count,
            error,
            success,
            success,
        ),
    )


def record_raw(
    connection: sqlite3.Connection, user_id: str, data_type: str, record_id: str, item: Any
) -> None:
    payload_hash = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
    connection.execute(
        """
        INSERT INTO raw_records (source_type, user_id, data_type, source_record_id, payload_hash)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_type, user_id, data_type, source_record_id)
        DO UPDATE SET payload_hash = excluded.payload_hash, received_at = CURRENT_TIMESTAMP
        """,
        ("cloud_session", user_id, data_type, record_id, payload_hash),
    )


def sync_summary(
    connection: sqlite3.Connection, client: httpx.Client, user_id: str, start: str, end: str
) -> dict[str, int]:
    payload = request_json(
        client,
        f"{API_BASE}/v1/data/band_data.json",
        {
            "query_type": "summary",
            "device_type": "android_phone",
            "userid": user_id,
            "from_date": start,
            "to_date": end,
        },
    )
    counts = {"daily_activity": 0, "sleep": 0, "heart_rate": 0}
    for item in parse_band_payload(payload):
        local_date = str(item.get("date_time") or item.get("date") or "")
        if not local_date:
            continue
        summary = parse_summary(item)
        activity = summary.get("stp")
        if isinstance(activity, dict):
            connection.execute(
                "INSERT INTO daily_activity VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, date) DO UPDATE SET steps = excluded.steps, "
                "distance_m = excluded.distance_m, active_kcal = excluded.active_kcal",
                (
                    user_id,
                    local_date,
                    int(activity.get("ttl", 0)),
                    float(activity.get("dis", 0)),
                    float(activity.get("cal", 0)),
                ),
            )
            record_raw(connection, user_id, "daily_activity", local_date, activity)
            counts["daily_activity"] += 1
        sleep = summary.get("slp")
        if isinstance(sleep, dict) and sleep.get("st") and sleep.get("ed"):
            duration = max(0, int((float(sleep["ed"]) - float(sleep["st"])) / 60))
            connection.execute(
                "INSERT INTO sleep_sessions VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, sleep_id) DO UPDATE SET end_at = excluded.end_at, "
                "duration_minutes = excluded.duration_minutes, "
                "time_asleep_minutes = excluded.time_asleep_minutes",
                (
                    user_id,
                    f"sleep_{local_date}",
                    local_date,
                    timestamp_iso(sleep["st"]),
                    timestamp_iso(sleep["ed"]),
                    duration,
                    duration,
                ),
            )
            record_raw(connection, user_id, "sleep", local_date, sleep)
            counts["sleep"] += 1
        if isinstance(sleep, dict) and sleep.get("rhr") and sleep.get("ed"):
            connection.execute(
                "INSERT INTO heart_rate_samples VALUES (?, ?, 'resting', ?) "
                "ON CONFLICT(user_id, timestamp, sample_type) "
                "DO UPDATE SET bpm = excluded.bpm",
                (user_id, timestamp_iso(sleep["ed"]), int(sleep["rhr"])),
            )
            counts["heart_rate"] += 1
    return counts


def sync_heart_rate_detail(
    connection: sqlite3.Connection, client: httpx.Client, user_id: str, start: str, end: str
) -> int:
    payload = request_json(
        client,
        f"{API_BASE}/v1/data/band_data.json",
        {
            "query_type": "detail",
            "device_type": "android_phone",
            "userid": user_id,
            "from_date": start,
            "to_date": end,
        },
    )
    count = 0
    for item in parse_band_payload(payload):
        local_date = str(item.get("date_time") or "")
        encoded = item.get("data_hr")
        if not local_date or not isinstance(encoded, str):
            continue
        try:
            values = base64.b64decode(encoded)
        except ValueError as error:
            raise CliError("Zepp returned invalid heart-rate data.") from error
        midnight = datetime.combine(date.fromisoformat(local_date), datetime.min.time(), UTC)
        for minute, bpm in enumerate(values):
            if bpm in {0, 254, 255} or not 30 <= bpm <= 240:
                continue
            timestamp = (midnight + timedelta(minutes=minute)).isoformat()
            connection.execute(
                "INSERT INTO heart_rate_samples VALUES (?, ?, 'passive', ?) "
                "ON CONFLICT(user_id, timestamp, sample_type) DO UPDATE SET bpm = excluded.bpm",
                (user_id, timestamp, bpm),
            )
            count += 1
    return count


def sync_workouts(
    connection: sqlite3.Connection, client: httpx.Client, user_id: str, start: str, end: str
) -> int:
    start_datetime = datetime.combine(date.fromisoformat(start), datetime.min.time(), UTC)
    end_datetime = datetime.combine(
        date.fromisoformat(end) + timedelta(days=1), datetime.min.time(), UTC
    )
    start_timestamp = int(start_datetime.timestamp())
    end_timestamp = int(
        end_datetime.timestamp()
    )
    payload = request_json(
        client,
        f"{API_BASE}/v1/sport/run/history.json",
        {
            "userid": user_id,
            "startTrackId": start_timestamp,
            "stopTrackId": end_timestamp,
            "need_sub_data": 1,
            "type": "",
        },
    )
    summaries = payload.get("data", {}).get("summary", [])
    if not isinstance(summaries, list):
        raise CliError("Zepp returned invalid workout history.")
    sport_types = {
        "1": "running",
        "6": "walking",
        "8": "treadmill",
        "9": "cycling",
        "14": "pool_swimming",
    }
    count = 0
    for item in summaries:
        if not isinstance(item, dict):
            continue
        workout_id = first_present(item, ("workout_id", "workoutId", "trackId", "trackid", "id"))
        start_raw = first_present(item, ("start_time", "startTime", "beginTime"))
        end_raw = first_present(item, ("end_time", "endTime", "finishTime"))
        duration_seconds = int(float(first_present(item, ("run_time", "runTime")) or 0))
        if not workout_id or (start_raw is None and end_raw is None):
            continue
        if start_raw is None:
            end_at = datetime.fromisoformat(timestamp_iso(end_raw))
            start_at = (end_at - timedelta(seconds=duration_seconds)).isoformat()
        else:
            start_at = timestamp_iso(start_raw)
        local_date = start_at[:10]
        if not start <= local_date <= end:
            continue
        duration = duration_seconds // 60
        workout_name = first_present(
            item, ("sport_title", "sportTitle", "workout_name", "workoutName")
        )
        connection.execute(
            "INSERT INTO workouts ("
            "user_id, workout_id, local_date, activity_type, workout_name, start_at, "
            "duration_minutes, distance_m, calories_kcal, average_heart_rate_bpm, "
            "min_heart_rate_bpm, max_heart_rate_bpm, elevation_gain_m, elevation_loss_m"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, workout_id) DO UPDATE SET local_date = excluded.local_date, "
            "activity_type = excluded.activity_type, workout_name = excluded.workout_name, "
            "start_at = excluded.start_at, "
            "duration_minutes = excluded.duration_minutes, distance_m = excluded.distance_m, "
            "calories_kcal = excluded.calories_kcal, "
            "average_heart_rate_bpm = excluded.average_heart_rate_bpm, "
            "min_heart_rate_bpm = excluded.min_heart_rate_bpm, "
            "max_heart_rate_bpm = excluded.max_heart_rate_bpm, "
            "elevation_gain_m = excluded.elevation_gain_m, "
            "elevation_loss_m = excluded.elevation_loss_m",
            (
                user_id,
                str(workout_id),
                local_date,
                sport_types.get(str(item.get("type", "unknown")), str(item.get("type", "unknown"))),
                str(workout_name) if workout_name is not None else None,
                start_at,
                duration,
                nonnegative_float(item.get("dis")),
                nonnegative_float(item.get("calorie")),
                nonnegative_float(first_present(item, ("avg_heart_rate", "average_beat"))),
                nonnegative_float(first_present(item, ("min_heart_rate",))),
                nonnegative_float(first_present(item, ("max_heart_rate",))),
                centimeters_to_meters(
                    first_present(
                        item,
                        ("elevationGain", "altitude_ascend", "cumulativeMountainClimbing"),
                    )
                ),
                centimeters_to_meters(
                    first_present(item, ("elevationLoss", "altitude_descend"))
                ),
            ),
        )
        record_raw(connection, user_id, "workouts", str(workout_id), item)
        count += 1
    return count


def sync_body_measurements(
    connection: sqlite3.Connection, client: httpx.Client, user_id: str, start: str, end: str
) -> int:
    payload = request_json(
        client, f"{WEIGHT_API_BASE}/users/{user_id}/members/-1/weightRecords", {"limit": 200}
    )
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise CliError("Zepp returned invalid body measurements.")
    count = 0
    for item in items:
        if not isinstance(item, dict) or not item.get("generatedTime"):
            continue
        timestamp = timestamp_iso(item["generatedTime"])
        if not start <= timestamp[:10] <= end:
            continue
        summary = item.get("summary", {})
        if not isinstance(summary, dict) or summary.get("weight") is None:
            continue
        connection.execute(
            "INSERT INTO body_measurements VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, timestamp) DO UPDATE SET weight_kg = excluded.weight_kg, "
            "body_fat_pct = excluded.body_fat_pct",
            (user_id, timestamp, float(summary["weight"]), summary.get("fatRate")),
        )
        record_raw(connection, user_id, "body_measurements", str(item.get("id", timestamp)), item)
        count += 1
    return count


def doctor_command(args: argparse.Namespace) -> int:
    configuration = load_configuration()
    database = database_path()
    database_ok = False
    if database.exists():
        try:
            with sqlite3.connect(database) as connection:
                database_ok = connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        except sqlite3.Error:
            database_ok = False

    payload = {
        "configured": configuration is not None,
        "region": configuration.region if configuration else None,
        "user_id_configured": bool(configuration and configuration.user_id),
        "token_available": has_token(configuration) if configuration else False,
        "cache_directory": str(app_directory()),
        "database_exists": database.exists(),
        "database_healthy": database_ok,
        "synced_data_types": cache_coverage(database) if database_ok else {},
    }
    emit(payload, args.json)
    return 0 if configuration and payload["token_available"] else 2


def sync_command(args: argparse.Namespace) -> int:
    configuration = load_configuration()
    if configuration is None:
        raise CliError("Zepp is not configured. Run 'configure' first.")
    if args.start_date and args.end_date and args.start_date > args.end_date:
        raise CliError("start-date must be on or before end-date.")
    for value in (args.start_date, args.end_date):
        if value:
            date.fromisoformat(value)
    requested_types = tuple(
        data_type.strip() for data_type in args.types.split(",") if data_type.strip()
    )
    invalid_types = set(requested_types).difference(DATA_TYPES)
    if not requested_types or invalid_types:
        raise CliError(f"Unsupported data types: {', '.join(sorted(invalid_types)) or 'none'}.")
    initialize_database(database_path())
    token = get_token(configuration)
    headers = {
        "apptoken": token,
        "appname": "com.huami.midong",
        "appplatform": "ios_phone",
        "accept": "*/*",
        "v": "2.0",
        "vn": "10.2.5",
        "cv": "1722_10.2.5",
        "vb": "202604132257",
        "lang": "en",
        "country": "",
        "timezone": "UTC",
    }
    results: dict[str, dict[str, Any]] = {}
    with (
        httpx.Client(headers=headers, timeout=30.0) as client,
        sqlite3.connect(database_path()) as connection,
    ):
        user_id = resolve_user_id(client, configuration, args.end_date or date.today().isoformat())
        summary_counts: dict[tuple[str, str], dict[str, int]] = {}
        for data_type in requested_types:
            start, end = sync_window(connection, user_id, data_type, args)
            try:
                if data_type in {"daily_activity", "sleep", "heart_rate"}:
                    window = (start, end)
                    if window not in summary_counts:
                        summary_counts[window] = sync_summary(
                            connection, client, user_id, start, end
                        )
                    count = summary_counts[window][data_type]
                    if data_type == "heart_rate":
                        count += sync_heart_rate_detail(connection, client, user_id, start, end)
                elif data_type == "workouts":
                    count = sync_workouts(connection, client, user_id, start, end)
                else:
                    count = sync_body_measurements(connection, client, user_id, start, end)
                update_sync_state(connection, user_id, data_type, end, count)
                results[data_type] = {
                    "status": "ok",
                    "records": count,
                    "start_date": start,
                    "end_date": end,
                }
            except CliError as error:
                update_sync_state(connection, user_id, data_type, None, 0, str(error))
                results[data_type] = {
                    "status": "error",
                    "error": str(error),
                    "start_date": start,
                    "end_date": end,
                }
            connection.commit()
    failed = [data_type for data_type, result in results.items() if result["status"] == "error"]
    emit({"user_id": user_id, "results": results, "failed_data_types": failed}, args.json)
    return 1 if failed else 0


def workout_probe_command(args: argparse.Namespace) -> int:
    configuration = load_configuration()
    if configuration is None:
        raise CliError("Zepp is not configured. Run 'configure' first.")
    token = get_token(configuration)
    headers = {
        "apptoken": token,
        "appname": "com.huami.midong",
        "appplatform": "ios_phone",
        "accept": "*/*",
        "v": "2.0",
        "vn": "10.2.5",
        "cv": "1722_10.2.5",
        "vb": "202604132257",
        "lang": "en",
        "country": "",
        "timezone": "UTC",
    }
    end = date.today()
    start = end - timedelta(days=365)
    with httpx.Client(headers=headers, timeout=30.0) as client:
        payload = request_json(
            client,
            f"{API_BASE}/v1/sport/run/history.json",
            {
                "userid": configuration.user_id,
                "startTrackId": int(datetime.combine(start, datetime.min.time(), UTC).timestamp()),
                "stopTrackId": int(
                    datetime.combine(end + timedelta(days=1), datetime.min.time(), UTC).timestamp()
                ),
                "need_sub_data": 1,
                "type": "",
            },
        )
    data = payload.get("data")
    summary = data.get("summary", []) if isinstance(data, dict) else []
    record_keys = []
    if isinstance(summary, list) and summary and isinstance(summary[0], dict):
        record_keys = sorted(summary[0])
    dates = []
    for item in summary if isinstance(summary, list) else []:
        if not isinstance(item, dict) or item.get("start_time") is None:
            continue
        try:
            dates.append(timestamp_iso(item["start_time"])[:10])
        except (TypeError, ValueError, OSError):
            continue
    emit(
        {
            "top_level_keys": sorted(payload),
            "code": payload.get("code"),
            "data_keys": sorted(data) if isinstance(data, dict) else [],
            "summary_count": len(summary) if isinstance(summary, list) else None,
            "summary_record_keys": record_keys,
            "first_workout_date": min(dates) if dates else None,
            "last_workout_date": max(dates) if dates else None,
            "next_cursor": data.get("next") if isinstance(data, dict) else None,
        },
        args.json,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    configure = subcommands.add_parser(
        "configure", help="Store Zepp configuration and token securely"
    )
    configure.add_argument("--region", default=DEFAULT_REGION)
    configure.add_argument("--user-id")
    configure.add_argument("--from-env", action="store_true")
    configure.add_argument("--json", action="store_true")
    configure.set_defaults(handler=configure_command)

    doctor = subcommands.add_parser(
        "doctor", help="Report safe local configuration and cache health"
    )
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=doctor_command)

    sync = subcommands.add_parser("sync", help="Synchronize Zepp health data into the local cache")
    sync.add_argument("--start-date")
    sync.add_argument("--end-date")
    sync.add_argument("--types", default=",".join(DATA_TYPES))
    sync.add_argument("--force-full-sync", action="store_true")
    sync.add_argument("--json", action="store_true")
    sync.set_defaults(handler=sync_command)

    workout_probe = subcommands.add_parser(
        "workout-probe", help="Inspect the safe structural metadata of the workout response"
    )
    workout_probe.add_argument("--json", action="store_true")
    workout_probe.set_defaults(handler=workout_probe_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except CliError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
