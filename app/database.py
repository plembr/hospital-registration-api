from __future__ import annotations

import os
import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "hospital_mock.db"


def get_database_path() -> Path:
    return Path(os.getenv("DATABASE_PATH", str(DEFAULT_DATABASE_PATH))).resolve()


def get_connection() -> sqlite3.Connection:
    database_path = get_database_path()
    if not database_path.is_file():
        raise FileNotFoundError(f"Mock database was not found: {database_path}")

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    _apply_migrations(connection)
    return connection


def _apply_migrations(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS rooms (
            room_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            department_id TEXT NOT NULL,
            campus TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (department_id) REFERENCES departments(department_id),
            UNIQUE (department_id, name)
        )
        """
    )

    slot_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(appointment_slots)").fetchall()
    }
    if "room_id" not in slot_columns:
        connection.execute("ALTER TABLE appointment_slots ADD COLUMN room_id TEXT")

    connection.execute(
        """
        INSERT OR IGNORE INTO rooms (room_id, name, department_id, campus, is_active)
        SELECT 'room_' || department_id || '_a', '诊室A', department_id, campus, 1
        FROM departments
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO rooms (room_id, name, department_id, campus, is_active)
        SELECT 'room_' || department_id || '_b', '诊室B', department_id, campus, 1
        FROM departments
        """
    )
    connection.execute(
        """
        UPDATE appointment_slots
        SET room_id = (
            SELECT 'room_' || doc.department_id ||
                CASE WHEN substr(doc.doctor_id, -2) = '01' THEN '_a' ELSE '_b' END
            FROM doctors AS doc
            WHERE doc.doctor_id = appointment_slots.doctor_id
        )
        WHERE room_id IS NULL
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_appointment_slots_room_id
        ON appointment_slots(room_id)
        """
    )

    appointment_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(appointments)").fetchall()
    }
    if "cancel_idempotency_key" not in appointment_columns:
        connection.execute(
            "ALTER TABLE appointments ADD COLUMN cancel_idempotency_key TEXT"
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_appointments_cancel_idempotency_key
            ON appointments(cancel_idempotency_key)
            WHERE cancel_idempotency_key IS NOT NULL
            """
        )
    connection.commit()
