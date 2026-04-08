import sqlite3
import shutil
from pathlib import Path
from collections import defaultdict

DB_PATH    = "/home/cisa/Documents/ProyectoIA/detecciones.db"
CROPS_DIR  = Path("/media/cisa/JETSON_SD/cisa_frames/crops")
OUTPUT_DIR = Path("/home/cisa/Documents/ProyectoIA/digit_model/data/raw")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

conn = sqlite3.connect(DB_PATH)
rows = conn.execute("""
    SELECT no_economico, captura_url
    FROM evento_paso
    WHERE estado = 'VERIFICADO'
      AND no_economico IS NOT NULL
      AND captura_url IS NOT NULL
      AND length(no_economico) = 4
""").fetchall()
conn.close()

print(f"Registros verificados con captura_url: {len(rows)}")

crop_index = defaultdict(list)
for f in CROPS_DIR.glob("*_crop.jpg"):
    ts = f.name[:15]
    crop_index[ts].append(f)

print(f"Crops indexados: {sum(len(v) for v in crop_index.values())}")

copiados    = 0
no_match    = 0
seq_counter = defaultdict(int)

for no_economico, captura_url in rows:
    ts = captura_url[:15]
    matches = crop_index.get(ts, [])

    if not matches:
        no_match += 1
        continue

    for crop_file in matches:
        seq_counter[no_economico] += 1
        seq  = seq_counter[no_economico]
        dest = OUTPUT_DIR / f"{no_economico}_{seq:03d}.jpg"
        shutil.copy2(crop_file, dest)
        copiados += 1

print(f"\n{'='*40}")
print(f"Crops copiados:  {copiados}")
print(f"Sin match:       {no_match}")
print(f"Numeros unicos:  {len(seq_counter)}")
print(f"\nDistribucion por numero:")
for num, count in sorted(seq_counter.items()):
    print(f"  {num}: {count}")
