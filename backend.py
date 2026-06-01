import os
from datetime import datetime
from typing import List, Literal, Optional

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr

# =========================
# Config
# =========================

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://neondb_owner:npg_K1zSXbWGchs9@ep-wild-band-aqia44wa.c-8.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require",
)

INSTITUTION_SLUG = "runika-edupanel"

# En producción cambia esto a tu dominio real
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

app = FastAPI(title="Runika EduPanel API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def fetch_one(query, params=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            return cur.fetchone()


def fetch_all(query, params=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            return cur.fetchall()


def get_institution_id() -> int:
    row = fetch_one("SELECT id FROM institutions WHERE slug = %s", (INSTITUTION_SLUG,))
    if not row:
        raise HTTPException(status_code=500, detail="Institución no configurada")
    return row["id"]


# =========================
# Pydantic models
# =========================

class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    role: Literal["admin", "profesor"]


class ProfesorCreate(BaseModel):
    nombre: str
    email: EmailStr
    password: str
    materias: List[str]


class ProfesorUpdate(BaseModel):
    nombre: str
    email: EmailStr
    materias: List[str]


class EstudianteCreate(BaseModel):
    nombres: str
    apellidos: str
    documento: str
    email: EmailStr
    materias: List[str]
    profesor_nombre: str


class EstudianteUpdate(BaseModel):
    nombres: str
    apellidos: str
    documento: str
    email: EmailStr
    materias: List[str]
    profesor_nombre: str


class HorarioCreate(BaseModel):
    student_id: int
    teacher_id: int
    subject_code: str  # MAT / ESP / ING
    weekday: str       # MON, TUE, WED, THU, FRI, SAT
    start_time: str    # "08:00:00"


class HorarioUpdate(BaseModel):
    teacher_id: int
    student_id: int
    subject_code: str
    weekday: str
    start_time: str


# =========================
# Healthcheck
# =========================

@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


# =========================
# Auth
# =========================

@app.post("/api/v1/login")
def login(payload: LoginRequest):
    iid = get_institution_id()
    db_role = "super_admin" if payload.role == "admin" else "teacher"

    row = fetch_one(
        """
        SELECT u.id, u.full_name, u.email, u.role
        FROM users u
        WHERE u.institution_id = %s
          AND u.email = %s
          AND u.role = %s
          AND u.status = 'active'
          AND u.password_hash = crypt(%s, u.password_hash)
        """,
        (iid, payload.email, db_role, payload.password),
    )
    if not row:
        raise HTTPException(status_code=401, detail="Credenciales inválidas")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET last_login_at = NOW() WHERE id = %s",
                (row["id"],),
            )
            conn.commit()

    return {
        "name": row["full_name"],
        "email": row["email"],
        "role": "admin" if row["role"] in ("super_admin", "admin") else "profesor",
    }


# =========================
# Dashboard
# =========================

@app.get("/api/v1/dashboard/stats")
def dashboard_stats():
    iid = get_institution_id()

    stats = fetch_one(
        """
        SELECT *
        FROM v_dashboard_stats
        WHERE institution_id = %s
        """,
        (iid,),
    ) or {
        "total_students": 0,
        "total_teachers": 0,
        "total_active_schedule_slots": 0,
        "traffic_verde": 0,
        "traffic_amarillo": 0,
        "traffic_rojo": 0,
        "materias_matematicas": 0,
        "materias_espanol": 0,
        "materias_ingles": 0,
    }

    boletines = fetch_one(
        """
        SELECT COUNT(*) AS total
        FROM diagnostics d
        WHERE d.institution_id = %s AND d.status = 'completed'
        """,
        (iid,),
    )["total"]

    return {
        "totalEstudiantes": stats["total_students"],
        "totalProfesores": stats["total_teachers"],
        "clasesHoy": stats["total_active_schedule_slots"],
        "boletinesGenerados": boletines,
        "semaforoVerde": stats["traffic_verde"],
        "semaforoAmarillo": stats["traffic_amarillo"],
        "semaforoRojo": stats["traffic_rojo"],
        "materiasMatematicas": stats["materias_matematicas"],
        "materiasEspanol": stats["materias_espanol"],
        "materiasIngles": stats["materias_ingles"],
    }


# =========================
# Profesores CRUD
# =========================

@app.get("/api/v1/profesores")
def listar_profesores():
    iid = get_institution_id()
    rows = fetch_all(
        """
        SELECT *
        FROM v_teachers_summary
        WHERE institution_id = %s
        ORDER BY teacher_name
        """,
        (iid,),
    )
    return [
        {
            "idProfesor": r["teacher_id"],
            "nombre": r["teacher_name"],
            "email": r["email"],
            "materias": r["subjects"] or [],
            "totalEstudiantes": r["total_students"],
        }
        for r in rows
    ]


@app.get("/api/v1/profesores-simple")
def listar_profesores_simple():
    iid = get_institution_id()
    rows = fetch_all(
        """
        SELECT
            t.id AS teacher_id,
            u.full_name AS teacher_name,
            ARRAY_AGG(s.code ORDER BY s.code) FILTER (WHERE s.code IS NOT NULL) AS subjects
        FROM teachers t
        JOIN users u ON u.id = t.user_id
        LEFT JOIN teacher_subjects ts ON ts.teacher_id = t.id
        LEFT JOIN subjects s ON s.id = ts.subject_id
        WHERE t.institution_id = %s
        GROUP BY t.id, u.full_name
        ORDER BY u.full_name
        """,
        (iid,),
    )
    return [
        {
            "idProfesor": r["teacher_id"],
            "nombre": r["teacher_name"],
            "materias": r["subjects"] or [],
        }
        for r in rows
    ]


@app.post("/api/v1/profesores")
def crear_profesor(p: ProfesorCreate):
    iid = get_institution_id()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (institution_id, full_name, email, password_hash, role, status, must_change_password)
                VALUES (%s, %s, %s, crypt(%s, gen_salt('bf', 10)), 'teacher', 'active', TRUE)
                RETURNING id
                """,
                (iid, p.nombre, p.email, p.password),
            )
            user_id = cur.fetchone()["id"]

            cur.execute(
                """
                INSERT INTO teachers (institution_id, user_id, is_active)
                VALUES (%s, %s, TRUE)
                RETURNING id
                """,
                (iid, user_id),
            )
            teacher_id = cur.fetchone()["id"]

            for materia in p.materias:
                codigo = materia.upper()
                cur.execute(
                    "SELECT id FROM subjects WHERE institution_id = %s AND code = %s",
                    (iid, codigo),
                )
                sub = cur.fetchone()
                if sub:
                    cur.execute(
                        """
                        INSERT INTO teacher_subjects (institution_id, teacher_id, subject_id)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (teacher_id, subject_id) DO NOTHING
                        """,
                        (iid, teacher_id, sub["id"]),
                    )

            conn.commit()

    return {"ok": True, "idProfesor": teacher_id}


@app.put("/api/v1/profesores/{teacher_id}")
def editar_profesor(teacher_id: int, p: ProfesorUpdate):
    iid = get_institution_id()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET full_name = %s,
                    email = %s,
                    updated_at = NOW()
                WHERE id = (
                    SELECT user_id FROM teachers
                    WHERE id = %s AND institution_id = %s
                )
                RETURNING id
                """,
                (p.nombre, p.email, teacher_id, iid),
            )
            user_row = cur.fetchone()
            if not user_row:
                raise HTTPException(status_code=404, detail="Profesor no encontrado")

            cur.execute(
                """
                DELETE FROM teacher_subjects
                WHERE institution_id = %s AND teacher_id = %s
                """,
                (iid, teacher_id),
            )

            for materia in p.materias:
                codigo = materia.upper()
                cur.execute(
                    "SELECT id FROM subjects WHERE institution_id = %s AND code = %s",
                    (iid, codigo),
                )
                sub = cur.fetchone()
                if sub:
                    cur.execute(
                        """
                        INSERT INTO teacher_subjects (institution_id, teacher_id, subject_id)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (teacher_id, subject_id) DO NOTHING
                        """,
                        (iid, teacher_id, sub["id"]),
                    )

            conn.commit()

    return {"ok": True, "idProfesor": teacher_id}


@app.delete("/api/v1/profesores/{teacher_id}")
def eliminar_profesor(teacher_id: int):
    iid = get_institution_id()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM teacher_subjects WHERE institution_id = %s AND teacher_id = %s",
                (iid, teacher_id),
            )
            cur.execute(
                """
                UPDATE students
                SET teacher_id = NULL
                WHERE institution_id = %s AND teacher_id = %s
                """,
                (iid, teacher_id),
            )
            cur.execute(
                """
                DELETE FROM schedules
                WHERE institution_id = %s AND teacher_id = %s
                """,
                (iid, teacher_id),
            )
            cur.execute(
                """
                DELETE FROM teachers
                WHERE institution_id = %s AND id = %s
                RETURNING user_id
                """,
                (iid, teacher_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Profesor no encontrado")

            cur.execute("DELETE FROM users WHERE id = %s", (row["user_id"],))
            conn.commit()

    return {"ok": True, "idProfesor": teacher_id}


# =========================
# Estudiantes CRUD
# =========================

@app.get("/api/v1/estudiantes")
def listar_estudiantes():
    iid = get_institution_id()
    rows = fetch_all(
        """
        SELECT *
        FROM v_students_summary
        WHERE institution_id = %s
        ORDER BY full_name
        """,
        (iid,),
    )

    horarios = fetch_all(
        """
        SELECT *
        FROM v_schedule_grid
        WHERE institution_id = %s
        """,
        (iid,),
    )

    day_map = {
        "MON": "Lunes",
        "TUE": "Martes",
        "WED": "Miércoles",
        "THU": "Jueves",
        "FRI": "Viernes",
        "SAT": "Sábado",
    }

    horarios_por_estudiante = {}
    for h in horarios:
        horarios_por_estudiante.setdefault(h["student_id"], []).append(
            {
                "dia": day_map.get(h["weekday"], h["weekday"]),
                "franja": h["start_time"].strftime("%H:%M"),
                "materia": h["subject_code"],
                "profesor": h["teacher_name"],
            }
        )

    return [
        {
            "idEstudiante": r["student_id"],
            "nombres": r["first_name"],
            "apellidos": r["last_name"],
            "documento": r["document_number"],
            "email": r["email"],
            "materias": r["subjects"] or [],
            "profesorNombre": r["teacher_name"] or "Sin asignar",
            "horario": horarios_por_estudiante.get(r["student_id"], []),
        }
        for r in rows
    ]


@app.post("/api/v1/estudiantes")
def crear_estudiante(e: EstudianteCreate):
    iid = get_institution_id()

    profesor = fetch_one(
        """
        SELECT t.id
        FROM teachers t
        JOIN users u ON u.id = t.user_id
        WHERE t.institution_id = %s AND u.full_name = %s
        """,
        (iid, e.profesor_nombre),
    )
    if not profesor:
        raise HTTPException(status_code=400, detail="Profesor no encontrado")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO students (
                    institution_id, first_name, last_name, document_number, email,
                    status, teacher_id, enrollment_date
                )
                VALUES (%s, %s, %s, %s, %s, 'active', %s, CURRENT_DATE)
                RETURNING id
                """,
                (iid, e.nombres, e.apellidos, e.documento, e.email, profesor["id"]),
            )
            student_id = cur.fetchone()["id"]

            for materia in e.materias:
                codigo = materia.upper()
                cur.execute(
                    "SELECT id FROM subjects WHERE institution_id = %s AND code = %s",
                    (iid, codigo),
                )
                sub = cur.fetchone()
                if sub:
                    cur.execute(
                        """
                        INSERT INTO student_subjects (institution_id, student_id, subject_id)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (student_id, subject_id) DO NOTHING
                        """,
                        (iid, student_id, sub["id"]),
                    )

            conn.commit()

    return {"ok": True, "idEstudiante": student_id}


@app.put("/api/v1/estudiantes/{student_id}")
def editar_estudiante(student_id: int, e: EstudianteUpdate):
    iid = get_institution_id()

    profesor = fetch_one(
        """
        SELECT t.id
        FROM teachers t
        JOIN users u ON u.id = t.user_id
        WHERE t.institution_id = %s AND u.full_name = %s
        """,
        (iid, e.profesor_nombre),
    )
    if not profesor:
        raise HTTPException(status_code=400, detail="Profesor no encontrado")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE students
                SET first_name = %s,
                    last_name = %s,
                    document_number = %s,
                    email = %s,
                    teacher_id = %s,
                    updated_at = NOW()
                WHERE institution_id = %s
                  AND id = %s
                RETURNING id
                """,
                (
                    e.nombres,
                    e.apellidos,
                    e.documento,
                    e.email,
                    profesor["id"],
                    iid,
                    student_id,
                ),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Estudiante no encontrado")

            cur.execute(
                """
                DELETE FROM student_subjects
                WHERE institution_id = %s AND student_id = %s
                """,
                (iid, student_id),
            )

            for materia in e.materias:
                codigo = materia.upper()
                cur.execute(
                    "SELECT id FROM subjects WHERE institution_id = %s AND code = %s",
                    (iid, codigo),
                )
                sub = cur.fetchone()
                if sub:
                    cur.execute(
                        """
                        INSERT INTO student_subjects (institution_id, student_id, subject_id)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (student_id, subject_id) DO NOTHING
                        """,
                        (iid, student_id, sub["id"]),
                    )

            conn.commit()

    return {"ok": True, "idEstudiante": student_id}


@app.delete("/api/v1/estudiantes/{student_id}")
def eliminar_estudiante(student_id: int):
    iid = get_institution_id()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM schedules
                WHERE institution_id = %s AND student_id = %s
                """,
                (iid, student_id),
            )
            cur.execute(
                """
                DELETE FROM student_subjects
                WHERE institution_id = %s AND student_id = %s
                """,
                (iid, student_id),
            )
            cur.execute(
                """
                DELETE FROM students
                WHERE institution_id = %s AND id = %s
                RETURNING id
                """,
                (iid, student_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Estudiante no encontrado")

            conn.commit()

    return {"ok": True, "idEstudiante": student_id}


# =========================
# Horarios CRUD
# =========================

@app.post("/api/v1/horario")
def asignar_horario(h: HorarioCreate):
    iid = get_institution_id()

    estudiante = fetch_one(
        "SELECT id FROM students WHERE id = %s AND institution_id = %s",
        (h.student_id, iid),
    )
    if not estudiante:
        raise HTTPException(status_code=404, detail="Estudiante no encontrado")

    profesor = fetch_one(
        "SELECT id FROM teachers WHERE id = %s AND institution_id = %s",
        (h.teacher_id, iid),
    )
    if not profesor:
        raise HTTPException(status_code=404, detail="Profesor no encontrado")

    materia_estudiante = fetch_one(
        """
        SELECT ss.subject_id
        FROM student_subjects ss
        JOIN subjects s ON s.id = ss.subject_id
        WHERE ss.student_id = %s
          AND ss.institution_id = %s
          AND s.code = %s
        """,
        (h.student_id, iid, h.subject_code),
    )
    if not materia_estudiante:
        raise HTTPException(
            status_code=400,
            detail="El estudiante no tiene asignada esa materia",
        )

    materia_profesor = fetch_one(
        """
        SELECT ts.subject_id
        FROM teacher_subjects ts
        JOIN subjects s ON s.id = ts.subject_id
        WHERE ts.teacher_id = %s
          AND ts.institution_id = %s
          AND s.code = %s
        """,
        (h.teacher_id, iid, h.subject_code),
    )
    if not materia_profesor:
        raise HTTPException(
            status_code=400,
            detail="El profesor no tiene asignada esa materia",
        )

    conflicto = fetch_one(
        """
        SELECT id
        FROM schedules
        WHERE institution_id = %s
          AND weekday = %s::weekday_code
          AND start_time = %s::time
          AND (
                student_id = %s
                OR teacher_id = %s
              )
        """,
        (iid, h.weekday, h.start_time, h.student_id, h.teacher_id),
    )
    if conflicto:
        raise HTTPException(
            status_code=400,
            detail="Ya existe una clase para ese estudiante o profesor en esa franja",
        )

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO schedules (
                    institution_id,
                    teacher_id,
                    student_id,
                    subject_id,
                    weekday,
                    start_time,
                    end_time,
                    status
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s::weekday_code,
                    %s::time,
                    (%s::time + interval '1 hour')::time,
                    'active'
                )
                RETURNING id
                """,
                (
                    iid,
                    h.teacher_id,
                    h.student_id,
                    materia_estudiante["subject_id"],
                    h.weekday,
                    h.start_time,
                    h.start_time,
                ),
            )
            schedule_id = cur.fetchone()["id"]
            conn.commit()

    return {"ok": True, "idHorario": schedule_id}


@app.get("/api/v1/horario")
def listar_horario():
    iid = get_institution_id()
    rows = fetch_all(
        """
        SELECT
            sc.id,
            sc.weekday,
            sc.start_time,
            sc.end_time,
            st.full_name AS student_name,
            u.full_name AS teacher_name,
            sb.code::text AS subject_code
        FROM schedules sc
        JOIN students st ON st.id = sc.student_id
        JOIN teachers t ON t.id = sc.teacher_id
        JOIN users u ON u.id = t.user_id
        JOIN subjects sb ON sb.id = sc.subject_id
        WHERE sc.institution_id = %s
          AND sc.status = 'active'
        ORDER BY sc.weekday, sc.start_time
        """,
        (iid,),
    )
    return [
        {
            "idHorario": r["id"],
            "dia": r["weekday"],
            "horaInicio": r["start_time"].strftime("%H:%M"),
            "horaFin": r["end_time"].strftime("%H:%M"),
            "estudiante": r["student_name"],
            "profesor": r["teacher_name"],
            "materia": r["subject_code"],
        }
        for r in rows
    ]


@app.get("/api/v1/horario-profesor")
def listar_horario_profesor(email: EmailStr = Query(...)):
    iid = get_institution_id()
    rows = fetch_all(
        """
        SELECT
            sc.id,
            sc.weekday,
            sc.start_time,
            sc.end_time,
            st.full_name AS student_name,
            u.full_name AS teacher_name,
            sb.code::text AS subject_code
        FROM schedules sc
        JOIN students st ON st.id = sc.student_id
        JOIN teachers t ON t.id = sc.teacher_id
        JOIN users u ON u.id = t.user_id
        JOIN subjects sb ON sb.id = sc.subject_id
        WHERE sc.institution_id = %s
          AND sc.status = 'active'
          AND u.email = %s
        ORDER BY sc.weekday, sc.start_time
        """,
        (iid, email),
    )
    return [
        {
            "idHorario": r["id"],
            "dia": r["weekday"],
            "horaInicio": r["start_time"].strftime("%H:%M"),
            "horaFin": r["end_time"].strftime("%H:%M"),
            "estudiante": r["student_name"],
            "profesor": r["teacher_name"],
            "materia": r["subject_code"],
        }
        for r in rows
    ]


@app.put("/api/v1/horario/{schedule_id}")
def editar_horario(schedule_id: int, h: HorarioUpdate):
    iid = get_institution_id()

    materia = fetch_one(
        """
        SELECT id
        FROM subjects
        WHERE institution_id = %s
          AND code = %s
        """,
        (iid, h.subject_code),
    )
    if not materia:
        raise HTTPException(status_code=404, detail="Materia no encontrada")

    conflicto = fetch_one(
        """
        SELECT id
        FROM schedules
        WHERE institution_id = %s
          AND id <> %s
          AND weekday = %s::weekday_code
          AND start_time = %s::time
          AND (
                student_id = %s
                OR teacher_id = %s
              )
          AND status = 'active'
        """,
        (iid, schedule_id, h.weekday, h.start_time, h.student_id, h.teacher_id),
    )
    if conflicto:
        raise HTTPException(status_code=400, detail="Ya existe un conflicto en esa franja")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE schedules
                SET teacher_id = %s,
                    student_id = %s,
                    subject_id = %s,
                    weekday = %s::weekday_code,
                    start_time = %s::time,
                    end_time = (%s::time + interval '1 hour')::time,
                    updated_at = NOW()
                WHERE id = %s
                  AND institution_id = %s
                RETURNING id
                """,
                (
                    h.teacher_id,
                    h.student_id,
                    materia["id"],
                    h.weekday,
                    h.start_time,
                    h.start_time,
                    schedule_id,
                    iid,
                ),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Horario no encontrado")

            conn.commit()

    return {"ok": True, "idHorario": row["id"]}


@app.delete("/api/v1/horario/{schedule_id}")
def eliminar_horario(schedule_id: int):
    iid = get_institution_id()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM schedules
                WHERE id = %s
                  AND institution_id = %s
                RETURNING id
                """,
                (schedule_id, iid),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Horario no encontrado")

            conn.commit()

    return {"ok": True, "idHorario": row["id"]}


# =========================
# Notas y jobs recientes
# =========================

@app.get("/api/v1/notas")
def listar_notas():
    iid = get_institution_id()
    rows = fetch_all(
        """
        SELECT
            cn.note_date,
            st.full_name AS estudiante,
            u.full_name AS profesor,
            sb.code::text AS materia,
            cn.stars,
            cn.observation
        FROM class_notes cn
        JOIN students st ON st.id = cn.student_id
        JOIN teachers t ON t.id = cn.teacher_id
        JOIN users u ON u.id = t.user_id
        JOIN subjects sb ON sb.id = cn.subject_id
        WHERE cn.institution_id = %s
        ORDER BY cn.note_date DESC, cn.id DESC
        """,
        (iid,),
    )
    return [
        {
            "fecha": r["note_date"].isoformat(),
            "estudiante": r["estudiante"],
            "profesor": r["profesor"],
            "materia": r["materia"],
            "estrellas": r["stars"],
            "observacion": r["observation"],
        }
        for r in rows
    ]

# =========================
# Profesor - estudiantes reales
# =========================

@app.get("/api/v1/profesor/estudiantes")
def listar_estudiantes_profesor(email: EmailStr = Query(...)):
    iid = get_institution_id()

    teacher = fetch_one(
        """
        SELECT t.id AS teacher_id, u.full_name
        FROM teachers t
        JOIN users u ON u.id = t.user_id
        WHERE t.institution_id = %s
          AND u.email = %s
        """,
        (iid, email),
    )
    if not teacher:
        raise HTTPException(status_code=404, detail="Profesor no encontrado")

    rows = fetch_all(
        """
        WITH teacher_students AS (
            SELECT s.id
            FROM students s
            WHERE s.institution_id = %s
              AND s.teacher_id = %s

            UNION

            SELECT sc.student_id AS id
            FROM schedules sc
            WHERE sc.institution_id = %s
              AND sc.teacher_id = %s
              AND sc.status = 'active'
        )
        SELECT
            st.id AS student_id,
            st.first_name,
            st.last_name,
            st.document_number,
            st.email,
            COALESCE(u.full_name, 'Sin asignar') AS teacher_name,
            ARRAY_AGG(DISTINCT sb.code ORDER BY sb.code) FILTER (WHERE sb.code IS NOT NULL) AS subjects
        FROM teacher_students ts
        JOIN students st ON st.id = ts.id
        LEFT JOIN teachers t ON t.id = st.teacher_id
        LEFT JOIN users u ON u.id = t.user_id
        LEFT JOIN student_subjects ss ON ss.student_id = st.id
        LEFT JOIN subjects sb ON sb.id = ss.subject_id
        WHERE st.institution_id = %s
        GROUP BY st.id, st.first_name, st.last_name, st.document_number, st.email, u.full_name
        ORDER BY st.first_name, st.last_name
        """,
        (iid, teacher["teacher_id"], iid, teacher["teacher_id"], iid),
    )

    return [
        {
            "idEstudiante": r["student_id"],
            "nombres": r["first_name"],
            "apellidos": r["last_name"],
            "documento": r["document_number"],
            "email": r["email"],
            "materias": r["subjects"] or [],
            "profesorNombre": r["teacher_name"],
        }
        for r in rows
    ]


# =========================
# Crear notas
# =========================

class NotaCreate(BaseModel):
    teacher_email: EmailStr
    student_id: int
    subject_code: Literal["MAT", "ESP", "ING"]
    stars: int
    observation: str


@app.post("/api/v1/notas")
def crear_nota(payload: NotaCreate):
    iid = get_institution_id()

    if payload.stars < 0 or payload.stars > 5:
        raise HTTPException(status_code=400, detail="Las estrellas deben estar entre 0 y 5")

    teacher = fetch_one(
        """
        SELECT t.id AS teacher_id
        FROM teachers t
        JOIN users u ON u.id = t.user_id
        WHERE t.institution_id = %s
          AND u.email = %s
        """,
        (iid, payload.teacher_email),
    )
    if not teacher:
        raise HTTPException(status_code=404, detail="Profesor no encontrado")

    student = fetch_one(
        """
        SELECT id
        FROM students
        WHERE institution_id = %s
          AND id = %s
        """,
        (iid, payload.student_id),
    )
    if not student:
        raise HTTPException(status_code=404, detail="Estudiante no encontrado")

    subject = fetch_one(
        """
        SELECT id
        FROM subjects
        WHERE institution_id = %s
          AND code = %s
        """,
        (iid, payload.subject_code),
    )
    if not subject:
        raise HTTPException(status_code=404, detail="Materia no encontrada")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO class_notes (
                    institution_id,
                    student_id,
                    teacher_id,
                    subject_id,
                    note_date,
                    stars,
                    observation
                )
                VALUES (%s, %s, %s, %s, CURRENT_DATE, %s, %s)
                RETURNING id
                """,
                (
                    iid,
                    payload.student_id,
                    teacher["teacher_id"],
                    subject["id"],
                    payload.stars,
                    payload.observation,
                ),
            )
            row = cur.fetchone()
            conn.commit()

    return {"ok": True, "idNota": row["id"]}
@app.get("/api/v1/dashboard/jobs-recientes")
def jobs_recientes():
    iid = get_institution_id()
    rows = fetch_all(
        """
        SELECT
            d.job_code,
            s.full_name AS estudiante,
            sb.code::text AS materia,
            d.status,
            d.percentage,
            d.traffic,
            d.created_at
        FROM diagnostics d
        JOIN students s ON s.id = d.student_id
        LEFT JOIN subjects sb ON sb.id = d.subject_id
        WHERE d.institution_id = %s
        ORDER BY d.created_at DESC
        LIMIT 20
        """,
        (iid,),
    )
    return {
        "jobs": [
            {
                "idJob": r["job_code"],
                "estudiante": r["estudiante"],
                "materia": r["materia"] or "N/A",
                "status": r["status"].upper(),
                "date": r["created_at"].isoformat(),
                "score": float(r["percentage"]) if r["percentage"] is not None else None,
                "semaforo": r["traffic"],
            }
            for r in rows
        ]
    }
from fastapi.responses import HTMLResponse
import os

@app.get("/")
async def serve_frontend():
    # Lee y devuelve tu archivo HTML
    with open("frontend.html", "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content, status_code=200)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend:app", host="0.0.0.0", port=8000, reload=True)