import os
import sqlite3
import base64
from pathlib import Path
from decimal import Decimal, InvalidOperation
from datetime import datetime

import psycopg2

from config import DB_PATH, FRAMES_DIR, PG_CONFIG


SQLITE_SELECT = """
SELECT
    id_evento,
    no_economico,
    direccion,
    hora_paso,
    id_puerta,
    captura_url,
    duracion_camara,
    estado
FROM evento_paso
WHERE UPPER(estado) = 'VERIFICADO'
  AND no_economico IS NOT NULL
ORDER BY id_evento ASC
"""

PG_FIND_UNIDAD = """
SELECT id_unidad
FROM public.unidad
WHERE no_economico = %s
LIMIT 1
"""

PG_INSERT_EVENTO = """
INSERT INTO public.evento_paso (
    sqlite_evento_id,
    no_economico,
    id_unidad,
    direccion,
    id_puerta,
    hora_paso,
    captura_base64,
    duracion_camara
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (sqlite_evento_id) DO NOTHING
RETURNING id_evento
"""


def resolve_image_path(captura_url):
    if not captura_url:
        return None

    direct_path = Path(captura_url)
    if direct_path.is_file():
        return direct_path

    frame_path = Path(FRAMES_DIR) / captura_url
    if frame_path.is_file():
        return frame_path

    return None


def image_to_base64(image_path):
    if image_path is None:
        return None

    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def parse_hora_paso(value):
    if isinstance(value, datetime):
        return value

    if value is None:
        raise ValueError("hora_paso is null")

    return datetime.fromisoformat(str(value))


def normalize_direccion(value):
    if not value:
        return "entrada"

    value = str(value).strip().lower()
    if value not in ("entrada", "salida"):
        return "entrada"

    return value


def normalize_id_puerta(value):
    if value is None:
        return 1

    try:
        puerta = int(value)
        return puerta if puerta >= 1 else 1
    except Exception:
        return 1


def normalize_duracion(value):
    if value is None:
        return Decimal("0.00")

    txt = str(value).strip().replace(",", ".")

    if not txt:
        return Decimal("0.00")

    try:
        return Decimal(txt).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def fetch_sqlite_rows():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(SQLITE_SELECT).fetchall()
        return rows
    finally:
        con.close()


def find_id_unidad(pg_cur, no_economico):
    pg_cur.execute(PG_FIND_UNIDAD, (str(no_economico).strip(),))
    row = pg_cur.fetchone()
    return row[0] if row else None


def migrate():
    sqlite_rows = fetch_sqlite_rows()

    if not sqlite_rows:
        print("[INFO] No hay registros VERIFICADO en SQLite.")
        return

    pg_con = psycopg2.connect(**PG_CONFIG)
    pg_cur = pg_con.cursor()

    inserted = 0
    skipped_no_unidad = 0
    skipped_duplicate = 0
    missing_image = 0
    errors = 0

    try:
        for row in sqlite_rows:
            try:
                sqlite_evento_id = int(row["id_evento"])
                no_economico = str(row["no_economico"]).strip()
                direccion = normalize_direccion(row["direccion"])
                id_puerta = normalize_id_puerta(row["id_puerta"])
                hora_paso = parse_hora_paso(row["hora_paso"])
                duracion_camara = normalize_duracion(row["duracion_camara"])

                id_unidad = find_id_unidad(pg_cur, no_economico)
                if id_unidad is None:
                    skipped_no_unidad += 1
                    print(f"[SKIP] No existe id_unidad para no_economico={no_economico}")
                    continue

                image_path = resolve_image_path(row["captura_url"])
                if image_path is None:
                    captura_base64 = None
                    missing_image += 1
                    print(f"[WARN] Imagen no encontrada para sqlite_evento_id={sqlite_evento_id}")
                else:
                    captura_base64 = image_to_base64(image_path)

                pg_cur.execute(
                    PG_INSERT_EVENTO,
                    (
                        sqlite_evento_id,
                        no_economico,
                        id_unidad,
                        direccion,
                        id_puerta,
                        hora_paso,
                        captura_base64,
                        duracion_camara,
                    ),
                )

                result = pg_cur.fetchone()
                if result is None:
                    skipped_duplicate += 1
                else:
                    inserted += 1

                processed = inserted + skipped_duplicate + skipped_no_unidad + errors
                if processed % 100 == 0:
                    pg_con.commit()

            except Exception as e:
                errors += 1
                pg_con.rollback()
                print(f"[ERROR] sqlite_evento_id={row['id_evento']} -> {e}")

        pg_con.commit()

    finally:
        pg_cur.close()
        pg_con.close()

    print("=" * 60)
    print("[RESUMEN]")
    print(f"Insertados: {inserted}")
    print(f"Duplicados omitidos: {skipped_duplicate}")
    print(f"Sin id_unidad en PG: {skipped_no_unidad}")
    print(f"Imagen faltante: {missing_image}")
    print(f"Errores: {errors}")
    print("=" * 60)


if __name__ == "__main__":
    migrate()