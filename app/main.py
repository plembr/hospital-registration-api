from __future__ import annotations

import hmac
import os
import sqlite3
from datetime import date
from enum import Enum
from typing import Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from app.database import get_connection, get_database_path


app = FastAPI(
    title="Hospital Registration Mock API",
    version="0.1.0",
    description="Mock appointment APIs for the ADP registration agent.",
)


class TimePeriod(str, Enum):
    MORNING = "上午"
    AFTERNOON = "下午"


class QueryAvailableSlotsRequest(BaseModel):
    department_id: str | None = None
    doctor_id: str | None = None
    room_id: str | None = None
    campus: str | None = None
    visit_date: date | None = Field(
        default=None,
        description="Required. Visit date in YYYY-MM-DD format.",
    )
    period: TimePeriod | None = Field(
        default=None,
        description="Optional. Omit for a daily summary; provide a period for slot details.",
    )
    start_time: str | None = None
    available_only: bool = False
    limit: int = Field(default=20, ge=1, le=100)


class CreateAppointmentRequest(BaseModel):
    patient_id: str = Field(min_length=1, max_length=64)
    slot_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=128)
    confirmed: Literal[True]


class QueryAppointmentsRequest(BaseModel):
    patient_id: str = Field(min_length=1, max_length=64)
    include_cancelled: bool = True
    limit: int = Field(default=20, ge=1, le=100)


class SearchDepartmentsRequest(BaseModel):
    keyword: str | None = Field(default=None, max_length=100)
    campus: str | None = Field(default=None, max_length=100)
    limit: int = Field(default=20, ge=1, le=100)


class SearchDoctorsRequest(BaseModel):
    department_id: str | None = Field(default=None, max_length=64)
    keyword: str | None = Field(default=None, max_length=100)
    campus: str | None = Field(default=None, max_length=100)
    limit: int = Field(default=20, ge=1, le=100)


class GetMedicalHistorySummaryRequest(BaseModel):
    patient_id: str = Field(min_length=1, max_length=64)
    consent: Literal[True]


class CancelAppointmentRequest(BaseModel):
    patient_id: str = Field(min_length=1, max_length=64)
    appointment_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=128)
    confirmed: Literal[True]


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(
    x_api_key: str | None = Depends(api_key_header),
) -> None:
    configured_key = os.getenv("TOOL_API_KEY")
    if not configured_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="TOOL_API_KEY is not configured.",
        )

    if not x_api_key or not hmac.compare_digest(x_api_key, configured_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
        )


def get_appointment_detail(
    connection: sqlite3.Connection,
    appointment_id: str,
) -> dict[str, object] | None:
    row = connection.execute(
        """
        SELECT
            a.appointment_id,
            a.status AS appointment_status,
            a.idempotency_key,
            a.created_at,
            a.cancelled_at,
            p.patient_id,
            p.name_masked AS patient_name_masked,
            p.relationship,
            s.slot_id,
            s.visit_date,
            s.start_time,
            s.end_time,
            s.fee_cents,
            s.remaining AS slot_remaining,
            doc.doctor_id,
            doc.name AS doctor_name,
            doc.title AS doctor_title,
            r.room_id,
            r.name AS room_name,
            d.department_id,
            d.name AS department_name,
            d.campus
        FROM appointments AS a
        JOIN patients AS p ON p.patient_id = a.patient_id
        JOIN appointment_slots AS s ON s.slot_id = a.slot_id
        JOIN doctors AS doc ON doc.doctor_id = s.doctor_id
        JOIN rooms AS r ON r.room_id = s.room_id
        JOIN departments AS d ON d.department_id = doc.department_id
        WHERE a.appointment_id = ?
        """,
        (appointment_id,),
    ).fetchone()
    return dict(row) if row else None


def get_appointments_for_patient(
    connection: sqlite3.Connection,
    patient_id: str,
    include_cancelled: bool,
    limit: int,
) -> list[dict[str, object]]:
    filters = ["a.patient_id = ?"]
    parameters: list[object] = [patient_id]
    if not include_cancelled:
        filters.append("a.status = 'BOOKED'")
    parameters.append(limit)

    rows = connection.execute(
        f"""
        SELECT
            a.appointment_id,
            a.status AS appointment_status,
            a.created_at,
            a.cancelled_at,
            p.patient_id,
            p.name_masked AS patient_name_masked,
            p.relationship,
            s.slot_id,
            s.visit_date,
            s.start_time,
            s.end_time,
            s.fee_cents,
            s.remaining AS slot_remaining,
            doc.doctor_id,
            doc.name AS doctor_name,
            doc.title AS doctor_title,
            r.room_id,
            r.name AS room_name,
            d.department_id,
            d.name AS department_name,
            d.campus
        FROM appointments AS a
        JOIN patients AS p ON p.patient_id = a.patient_id
        JOIN appointment_slots AS s ON s.slot_id = a.slot_id
        JOIN doctors AS doc ON doc.doctor_id = s.doctor_id
        JOIN rooms AS r ON r.room_id = s.room_id
        JOIN departments AS d ON d.department_id = doc.department_id
        WHERE {' AND '.join(filters)}
        ORDER BY a.created_at DESC
        LIMIT ?
        """,
        parameters,
    ).fetchall()
    return [dict(row) for row in rows]


@app.get("/health")
async def health_check() -> dict[str, str]:
    database_path = get_database_path()
    if not database_path.is_file():
        raise HTTPException(status_code=503, detail="Mock database is unavailable.")
    return {"status": "ok"}


@app.get(
    "/api/v1/patients",
    dependencies=[Depends(verify_api_key)],
)
async def get_patient_list() -> dict[str, object]:
    try:
        connection = get_connection()
        try:
            rows = connection.execute(
                """
                SELECT patient_id, name_masked, relationship, birth_date
                FROM patients
                WHERE is_active = 1
                ORDER BY patient_id
                """
            ).fetchall()
        finally:
            connection.close()
    except (FileNotFoundError, sqlite3.DatabaseError) as error:
        raise HTTPException(status_code=503, detail="Mock database is unavailable.") from error

    patients = [dict(row) for row in rows]
    return {
        "Code": 0,
        "Msg": "success",
        "Data": {"total": len(patients), "patients": patients},
    }


@app.post(
    "/api/v1/medical-history/summary",
    dependencies=[Depends(verify_api_key)],
)
async def get_medical_history_summary(
    request: GetMedicalHistorySummaryRequest,
) -> dict[str, object]:
    try:
        connection = get_connection()
        try:
            patient = connection.execute(
                "SELECT patient_id FROM patients WHERE patient_id = ? AND is_active = 1",
                (request.patient_id,),
            ).fetchone()
            if not patient:
                raise HTTPException(status_code=404, detail="Patient was not found.")

            row = connection.execute(
                """
                SELECT
                    h.summary_id,
                    h.summary,
                    h.updated_at,
                    d.department_id AS last_department_id,
                    d.name AS last_department_name,
                    doc.doctor_id AS last_doctor_id,
                    doc.name AS last_doctor_name,
                    doc.title AS last_doctor_title
                FROM medical_history_summary AS h
                LEFT JOIN departments AS d ON d.department_id = h.last_department_id
                LEFT JOIN doctors AS doc ON doc.doctor_id = h.last_doctor_id
                WHERE h.patient_id = ?
                ORDER BY h.updated_at DESC
                LIMIT 1
                """,
                (request.patient_id,),
            ).fetchone()
        finally:
            connection.close()
    except (FileNotFoundError, sqlite3.DatabaseError) as error:
        raise HTTPException(status_code=503, detail="Mock database is unavailable.") from error

    history = dict(row) if row else None
    return {
        "Code": 0,
        "Msg": "success",
        "Data": {
            "patient_id": request.patient_id,
            "has_history": history is not None,
            "history": history,
        },
    }


@app.post(
    "/api/v1/departments/search",
    dependencies=[Depends(verify_api_key)],
)
async def search_departments(
    request: SearchDepartmentsRequest,
) -> dict[str, object]:
    filters = ["is_active = 1"]
    parameters: list[object] = []

    if request.keyword and request.keyword.strip():
        filters.append("name LIKE ?")
        parameters.append(f"%{request.keyword.strip()}%")
    if request.campus and request.campus.strip():
        filters.append("campus = ?")
        parameters.append(request.campus.strip())
    parameters.append(request.limit)

    try:
        connection = get_connection()
        try:
            rows = connection.execute(
                f"""
                SELECT department_id, name, campus
                FROM departments
                WHERE {' AND '.join(filters)}
                ORDER BY name
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        finally:
            connection.close()
    except (FileNotFoundError, sqlite3.DatabaseError) as error:
        raise HTTPException(status_code=503, detail="Mock database is unavailable.") from error

    departments = [dict(row) for row in rows]
    return {
        "Code": 0,
        "Msg": "success",
        "Data": {"total": len(departments), "departments": departments},
    }


@app.post(
    "/api/v1/doctors/search",
    dependencies=[Depends(verify_api_key)],
)
async def search_doctors(
    request: SearchDoctorsRequest,
) -> dict[str, object]:
    filters = ["doc.is_active = 1", "d.is_active = 1"]
    parameters: list[object] = []

    if request.department_id and request.department_id.strip():
        filters.append("doc.department_id = ?")
        parameters.append(request.department_id.strip())
    if request.keyword and request.keyword.strip():
        filters.append("doc.name LIKE ?")
        parameters.append(f"%{request.keyword.strip()}%")
    if request.campus and request.campus.strip():
        filters.append("doc.campus = ?")
        parameters.append(request.campus.strip())
    parameters.append(request.limit)

    try:
        connection = get_connection()
        try:
            rows = connection.execute(
                f"""
                SELECT
                    doc.doctor_id,
                    doc.name,
                    doc.title,
                    doc.department_id,
                    d.name AS department_name,
                    doc.campus
                FROM doctors AS doc
                JOIN departments AS d ON d.department_id = doc.department_id
                WHERE {' AND '.join(filters)}
                ORDER BY doc.department_id, doc.name
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        finally:
            connection.close()
    except (FileNotFoundError, sqlite3.DatabaseError) as error:
        raise HTTPException(status_code=503, detail="Mock database is unavailable.") from error

    doctors = [dict(row) for row in rows]
    return {
        "Code": 0,
        "Msg": "success",
        "Data": {"total": len(doctors), "doctors": doctors},
    }


@app.post(
    "/api/v1/appointment-slots/query",
    dependencies=[Depends(verify_api_key)],
)
async def query_available_slots(
    request: QueryAvailableSlotsRequest,
) -> dict[str, object]:
    if request.visit_date is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "visit_date is required. Provide YYYY-MM-DD; optionally add "
                "period as morning or afternoon."
            ),
        )
    if request.start_time and request.period is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="period is required when start_time is provided.",
        )

    filters: list[str] = ["d.is_active = 1", "doc.is_active = 1", "r.is_active = 1"]
    parameters: list[object] = []

    if request.department_id:
        filters.append("d.department_id = ?")
        parameters.append(request.department_id)
    if request.doctor_id:
        filters.append("doc.doctor_id = ?")
        parameters.append(request.doctor_id)
    if request.room_id:
        filters.append("r.room_id = ?")
        parameters.append(request.room_id)
    if request.campus:
        filters.append("d.campus = ?")
        parameters.append(request.campus)
    filters.append("s.visit_date = ?")
    parameters.append(request.visit_date.isoformat())
    if request.period == TimePeriod.MORNING:
        filters.append("s.start_time < '12:00'")
    elif request.period == TimePeriod.AFTERNOON:
        filters.append("s.start_time >= '12:00'")
    if request.start_time:
        filters.append("s.start_time = ?")
        parameters.append(request.start_time)

    summary_filters = list(filters)
    summary_parameters = list(parameters)
    if request.available_only:
        filters.extend(["s.status = 'AVAILABLE'", "s.remaining > 0"])

    detail_query = f"""
        SELECT
            s.slot_id,
            s.visit_date,
            s.start_time,
            s.end_time,
            s.fee_cents,
            s.capacity,
            s.remaining,
            s.status,
            COALESCE(bookings.booked_count, 0) AS booked_count,
            doc.doctor_id,
            doc.name AS doctor_name,
            doc.title AS doctor_title,
            r.room_id,
            r.name AS room_name,
            d.department_id,
            d.name AS department_name,
            d.campus
        FROM appointment_slots AS s
        LEFT JOIN (
            SELECT slot_id, COUNT(*) AS booked_count
            FROM appointments
            WHERE status = 'BOOKED'
            GROUP BY slot_id
        ) AS bookings ON bookings.slot_id = s.slot_id
        JOIN doctors AS doc ON doc.doctor_id = s.doctor_id
        JOIN rooms AS r ON r.room_id = s.room_id
        JOIN departments AS d ON d.department_id = doc.department_id
        WHERE {' AND '.join(filters)}
        ORDER BY s.visit_date, s.start_time, r.room_id, doc.doctor_id
        LIMIT ?
    """
    parameters.append(request.limit)

    summary_query = f"""
        SELECT
            COUNT(*) AS slot_count,
            COALESCE(SUM(s.remaining), 0) AS total_remaining_count,
            COALESCE(SUM(COALESCE(bookings.booked_count, 0)), 0) AS total_booked_count
        FROM appointment_slots AS s
        LEFT JOIN (
            SELECT slot_id, COUNT(*) AS booked_count
            FROM appointments
            WHERE status = 'BOOKED'
            GROUP BY slot_id
        ) AS bookings ON bookings.slot_id = s.slot_id
        JOIN doctors AS doc ON doc.doctor_id = s.doctor_id
        JOIN rooms AS r ON r.room_id = s.room_id
        JOIN departments AS d ON d.department_id = doc.department_id
        WHERE {' AND '.join(summary_filters)}
    """

    try:
        connection = get_connection()
        try:
            summary = connection.execute(summary_query, summary_parameters).fetchone()
            rows = (
                connection.execute(detail_query, parameters).fetchall()
                if request.period is not None
                else []
            )
        finally:
            connection.close()
    except (FileNotFoundError, sqlite3.DatabaseError) as error:
        raise HTTPException(status_code=503, detail="Mock database is unavailable.") from error

    slots = [dict(row) for row in rows]
    summary_data = dict(summary) if summary else {}
    return {
        "Code": 0,
        "Msg": "success",
        "Data": {
            "scope": "PERIOD" if request.period is not None else "DAY",
            "visit_date": request.visit_date.isoformat(),
            "period": request.period.value if request.period is not None else None,
            "total": int(summary_data.get("slot_count", 0)),
            "total_booked_count": int(summary_data.get("total_booked_count", 0)),
            "total_remaining_count": int(summary_data.get("total_remaining_count", 0)),
            "detail_slots_returned": len(slots),
            "slots": slots,
        },
    }


@app.post(
    "/api/v1/appointments/create",
    dependencies=[Depends(verify_api_key)],
)
async def create_appointment(
    request: CreateAppointmentRequest,
) -> dict[str, object]:
    try:
        connection = get_connection()
    except (FileNotFoundError, sqlite3.DatabaseError) as error:
        raise HTTPException(status_code=503, detail="Mock database is unavailable.") from error

    try:
        connection.execute("BEGIN IMMEDIATE")

        patient = connection.execute(
            "SELECT patient_id FROM patients WHERE patient_id = ? AND is_active = 1",
            (request.patient_id,),
        ).fetchone()
        if not patient:
            raise HTTPException(status_code=404, detail="Patient was not found.")

        existing = connection.execute(
            "SELECT appointment_id FROM appointments WHERE idempotency_key = ?",
            (request.idempotency_key,),
        ).fetchone()
        if existing:
            appointment = get_appointment_detail(connection, existing["appointment_id"])
            connection.commit()
            return {
                "Code": 0,
                "Msg": "idempotent replay",
                "Data": {"idempotent": True, "appointment": appointment},
            }

        slot = connection.execute(
            "SELECT status, remaining FROM appointment_slots WHERE slot_id = ?",
            (request.slot_id,),
        ).fetchone()
        if not slot:
            raise HTTPException(status_code=404, detail="Appointment slot was not found.")
        if slot["status"] != "AVAILABLE":
            raise HTTPException(status_code=409, detail="Appointment slot is unavailable.")
        if slot["remaining"] <= 0:
            raise HTTPException(status_code=409, detail="Appointment slot is full.")

        updated = connection.execute(
            """
            UPDATE appointment_slots
            SET remaining = remaining - 1
            WHERE slot_id = ? AND status = 'AVAILABLE' AND remaining > 0
            """,
            (request.slot_id,),
        )
        if updated.rowcount != 1:
            raise HTTPException(status_code=409, detail="Appointment slot is no longer available.")

        appointment_id = f"apt_{uuid4().hex[:16]}"
        connection.execute(
            """
            INSERT INTO appointments
                (appointment_id, patient_id, slot_id, status, idempotency_key, created_at)
            VALUES (?, ?, ?, 'BOOKED', ?, datetime('now'))
            """,
            (
                appointment_id,
                request.patient_id,
                request.slot_id,
                request.idempotency_key,
            ),
        )
        appointment = get_appointment_detail(connection, appointment_id)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return {
        "Code": 0,
        "Msg": "success",
        "Data": {"idempotent": False, "appointment": appointment},
    }


@app.post(
    "/api/v1/appointments/query",
    dependencies=[Depends(verify_api_key)],
)
async def query_appointments(
    request: QueryAppointmentsRequest,
) -> dict[str, object]:
    try:
        connection = get_connection()
        try:
            patient = connection.execute(
                "SELECT patient_id FROM patients WHERE patient_id = ? AND is_active = 1",
                (request.patient_id,),
            ).fetchone()
            if not patient:
                raise HTTPException(status_code=404, detail="Patient was not found.")

            appointments = get_appointments_for_patient(
                connection,
                request.patient_id,
                request.include_cancelled,
                request.limit,
            )
        finally:
            connection.close()
    except (FileNotFoundError, sqlite3.DatabaseError) as error:
        raise HTTPException(status_code=503, detail="Mock database is unavailable.") from error

    return {
        "Code": 0,
        "Msg": "success",
        "Data": {"total": len(appointments), "appointments": appointments},
    }


@app.post(
    "/api/v1/appointments/cancel",
    dependencies=[Depends(verify_api_key)],
)
async def cancel_appointment(
    request: CancelAppointmentRequest,
) -> dict[str, object]:
    try:
        connection = get_connection()
    except (FileNotFoundError, sqlite3.DatabaseError) as error:
        raise HTTPException(status_code=503, detail="Mock database is unavailable.") from error

    try:
        connection.execute("BEGIN IMMEDIATE")

        replay = connection.execute(
            """
            SELECT appointment_id, patient_id
            FROM appointments
            WHERE cancel_idempotency_key = ?
            """,
            (request.idempotency_key,),
        ).fetchone()
        if replay:
            if replay["patient_id"] != request.patient_id:
                raise HTTPException(status_code=403, detail="Appointment does not belong to patient.")
            appointment = get_appointment_detail(connection, replay["appointment_id"])
            connection.commit()
            return {
                "Code": 0,
                "Msg": "idempotent replay",
                "Data": {"idempotent": True, "appointment": appointment},
            }

        appointment_row = connection.execute(
            """
            SELECT appointment_id, patient_id, slot_id, status
            FROM appointments
            WHERE appointment_id = ?
            """,
            (request.appointment_id,),
        ).fetchone()
        if not appointment_row:
            raise HTTPException(status_code=404, detail="Appointment was not found.")
        if appointment_row["patient_id"] != request.patient_id:
            raise HTTPException(status_code=403, detail="Appointment does not belong to patient.")
        if appointment_row["status"] != "BOOKED":
            raise HTTPException(status_code=409, detail="Appointment is already cancelled.")

        updated_appointment = connection.execute(
            """
            UPDATE appointments
            SET status = 'CANCELLED',
                cancelled_at = datetime('now'),
                cancel_idempotency_key = ?
            WHERE appointment_id = ? AND status = 'BOOKED'
            """,
            (request.idempotency_key, request.appointment_id),
        )
        if updated_appointment.rowcount != 1:
            raise HTTPException(status_code=409, detail="Appointment is no longer cancellable.")

        updated_slot = connection.execute(
            """
            UPDATE appointment_slots
            SET remaining = MIN(remaining + 1, capacity),
                status = CASE WHEN status = 'FULL' THEN 'AVAILABLE' ELSE status END
            WHERE slot_id = ?
            """,
            (appointment_row["slot_id"],),
        )
        if updated_slot.rowcount != 1:
            raise HTTPException(status_code=503, detail="Appointment slot was not found.")

        appointment = get_appointment_detail(connection, request.appointment_id)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return {
        "Code": 0,
        "Msg": "success",
        "Data": {"idempotent": False, "appointment": appointment},
    }
