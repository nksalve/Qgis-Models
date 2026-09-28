import os
import sys
import subprocess
import time
import glob

# =====================================================================
# CONFIGURATION
# =====================================================================
DB_HOST = "localhost"
DB_PORT = "5432"
DB_NAME = "MoLE"
DB_USER = "postgres"
DB_PASS = "postgres"

# Output directory for individual .gpkg files (one per layer)
OUTPUT_DIR = r"D:\MOLE\gpkg_layers"

# Option to also create a single master GeoPackage containing all layers
CREATE_MASTER_GPKG = True
MASTER_GPKG_PATH = r"D:\MOLE\MoLE_all_layers.gpkg"

# Assign EPSG:4326 (WGS 84) to layers that have undefined/0 SRID
ASSIGN_EPSG_4326 = True

# Possible paths for ogr2ogr.exe
POSSIBLE_OGR_PATHS = [
    r"C:\Program Files\QGIS 3.34.0\bin\ogr2ogr.exe",
    r"C:\Program Files\QGIS 3.36.0\bin\ogr2ogr.exe",
    r"C:\Program Files\QGIS 3.44.8\bin\ogr2ogr.exe",
    r"C:\Program Files\PostgreSQL\17\bin\ogr2ogr.exe",
]

# Locate ogr2ogr
OGR2OGR = None
for path in POSSIBLE_OGR_PATHS:
    if os.path.exists(path):
        OGR2OGR = path
        break

if not OGR2OGR:
    # Check PATH
    try:
        res = subprocess.run(["ogr2ogr", "--version"], capture_output=True, text=True)
        if res.returncode == 0:
            OGR2OGR = "ogr2ogr"
    except Exception:
        pass

if not OGR2OGR:
    print("ERROR: ogr2ogr.exe not found! Please check QGIS / GDAL installation path.")
    sys.exit(1)

# Locate psql
POSSIBLE_PSQL_PATHS = [
    r"C:\Program Files\PostgreSQL\17\bin\psql.exe",
    r"C:\Program Files\PostgreSQL\16\bin\psql.exe",
]
PSQL = "psql"
for p in POSSIBLE_PSQL_PATHS:
    if os.path.exists(p):
        PSQL = p
        break

def get_tables_to_export():
    """Query PostgreSQL for all base spatial tables in the public schema."""
    env = os.environ.copy()
    env["PGPASSWORD"] = DB_PASS
    query = """
    SELECT f_table_name, srid, type 
    FROM geometry_columns 
    WHERE f_table_schema = 'public'
    ORDER BY f_table_name;
    """
    cmd = [
        PSQL,
        "-h", DB_HOST,
        "-p", DB_PORT,
        "-U", DB_USER,
        "-d", DB_NAME,
        "-tAc", query
    ]
    res = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Error fetching table list: {res.stderr}")
        return []
    
    tables = []
    for line in res.stdout.strip().splitlines():
        parts = line.strip().split("|")
        if len(parts) >= 3:
            tables.append({
                "name": parts[0],
                "srid": parts[1],
                "type": parts[2]
            })
    return tables

def export_all():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # If master GPKG already exists, remove it so we build a fresh one
    if CREATE_MASTER_GPKG and os.path.exists(MASTER_GPKG_PATH):
        try:
            os.remove(MASTER_GPKG_PATH)
        except Exception as e:
            print(f"Warning: Could not remove old master GPKG: {e}")

    tables = get_tables_to_export()
    total = len(tables)

    if total == 0:
        print(f"No spatial tables found in database '{DB_NAME}'!")
        return

    print("=" * 70)
    print(f" Converting {total} Tables from PostgreSQL '{DB_NAME}' to GeoPackage (.gpkg)")
    print(f" Source: {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
    print(f" Individual Output Folder : {OUTPUT_DIR}")
    if CREATE_MASTER_GPKG:
        print(f" Master GeoPackage File   : {MASTER_GPKG_PATH}")
    print("=" * 70 + "\n")

    conn_str = f"PG:host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER} password={DB_PASS}"

    success_count = 0
    start_time = time.time()

    for idx, tbl in enumerate(tables, 1):
        tbl_name = tbl["name"]
        srid = tbl["srid"]
        geom_type = tbl["type"]
        out_individual = os.path.join(OUTPUT_DIR, f"{tbl_name}.gpkg")

        print(f"[{idx:02d}/{total:02d}] Exporting {tbl_name} ({geom_type})...", end="", flush=True)

        # Base flags for ogr2ogr
        # If SRID is 0 / undefined, assign EPSG:4326 so GIS renders it in WGS 84
        srs_flags = []
        if ASSIGN_EPSG_4326 and (srid == "0" or srid == "" or srid is None):
            srs_flags = ["-a_srs", "EPSG:4326"]

        # 1. Export individual GPKG
        cmd_individual = [
            OGR2OGR,
            "-f", "GPKG",
            "-overwrite",
            out_individual,
            conn_str,
            f"public.{tbl_name}",
            "-lco", "ENCODING=UTF-8"
        ] + srs_flags

        res_ind = subprocess.run(cmd_individual, capture_output=True, text=True)

        if res_ind.returncode != 0:
            print(f" FAILED (Individual: {res_ind.stderr.strip()[:100]})")
            continue

        # 2. Append to Master GPKG
        if CREATE_MASTER_GPKG:
            if not os.path.exists(MASTER_GPKG_PATH):
                # Create master with first layer
                cmd_master = [
                    OGR2OGR,
                    "-f", "GPKG",
                    MASTER_GPKG_PATH,
                    conn_str,
                    f"public.{tbl_name}",
                    "-lco", "ENCODING=UTF-8"
                ] + srs_flags
            else:
                # Append subsequent layers
                cmd_master = [
                    OGR2OGR,
                    "-update",
                    "-append",
                    MASTER_GPKG_PATH,
                    conn_str,
                    f"public.{tbl_name}"
                ] + srs_flags

            res_master = subprocess.run(cmd_master, capture_output=True, text=True)
            if res_master.returncode != 0:
                print(f" WARNING (Master append: {res_master.stderr.strip()[:60]})", end="")

        size_kb = os.path.getsize(out_individual) / 1024
        if size_kb > 1024:
            size_str = f"{size_kb / 1024:.2f} MB"
        else:
            size_str = f"{size_kb:.1f} KB"

        print(f" DONE ({size_str})")
        success_count += 1

    elapsed = time.time() - start_time
    print("\n" + "=" * 70)
    print(" CONVERSION COMPLETED")
    print("=" * 70)
    print(f"Total Tables Converted : {success_count}/{total}")
    print(f"Time Taken             : {elapsed:.1f} seconds")
    print(f"\n1. Individual .gpkg files saved in:")
    print(f"   {OUTPUT_DIR}\\")
    if CREATE_MASTER_GPKG and os.path.exists(MASTER_GPKG_PATH):
        master_size_mb = os.path.getsize(MASTER_GPKG_PATH) / (1024 * 1024)
        print(f"\n2. Master combined .gpkg file ({master_size_mb:.2f} MB) saved at:")
        print(f"   {MASTER_GPKG_PATH}")

if __name__ == "__main__":
    export_all()
