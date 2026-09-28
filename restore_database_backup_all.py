import os
import sys
import subprocess
import glob
import time

# ==========================================
# CONFIGURATION
# ==========================================
DB_HOST = "localhost"
DB_PORT = "5432"
DB_NAME = "MoLE"         # Change to your target database if different (e.g., NMP, postgres)
DB_USER = "postgres"
DB_PASS = "postgres"     # Password for postgres user

BACKUP_DIR = r"D:\MOLE\MOLE Layer Backup"

# Locate PostgreSQL binaries (checks PostgreSQL 17, 16, 15, or PATH)
POSSIBLE_BIN_DIRS = [
    r"C:\Program Files\PostgreSQL\17\bin",
    r"C:\Program Files\PostgreSQL\16\bin",
    r"C:\Program Files\PostgreSQL\15\bin",
]

PG_RESTORE = None
PSQL = None

for d in POSSIBLE_BIN_DIRS:
    restore_candidate = os.path.join(d, "pg_restore.exe")
    psql_candidate = os.path.join(d, "psql.exe")
    if os.path.exists(restore_candidate) and os.path.exists(psql_candidate):
        PG_RESTORE = restore_candidate
        PSQL = psql_candidate
        break

if not PG_RESTORE:
    PG_RESTORE = "pg_restore"
    PSQL = "psql"

def run_psql_command(query, dbname=DB_NAME):
    """Run a single SQL command via psql."""
    env = os.environ.copy()
    env["PGPASSWORD"] = DB_PASS
    cmd = [
        PSQL,
        "-h", DB_HOST,
        "-p", DB_PORT,
        "-U", DB_USER,
        "-d", dbname,
        "-c", query
    ]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    return result

def ensure_database_and_postgis():
    print("=" * 60)
    print("1. Checking Database and PostGIS Extension...")
    print("=" * 60)

    # Check if target database exists by connecting to 'postgres' db
    env = os.environ.copy()
    env["PGPASSWORD"] = DB_PASS
    check_db_cmd = [
        PSQL, "-h", DB_HOST, "-p", DB_PORT, "-U", DB_USER, "-d", "postgres",
        "-tAc", f"SELECT 1 FROM pg_database WHERE datname = '{DB_NAME}';"
    ]
    res = subprocess.run(check_db_cmd, env=env, capture_output=True, text=True)
    if "1" not in res.stdout.strip():
        print(f"Creating database '{DB_NAME}'...")
        create_res = subprocess.run(
            [PSQL, "-h", DB_HOST, "-p", DB_PORT, "-U", DB_USER, "-d", "postgres", "-c", f"CREATE DATABASE \"{DB_NAME}\";"],
            env=env, capture_output=True, text=True
        )
        if create_res.returncode != 0:
            print(f"Error creating database: {create_res.stderr}")
            return False
        print(f"Database '{DB_NAME}' created successfully.")
    else:
        print(f"Database '{DB_NAME}' exists.")

    # Enable PostGIS extension (required for spatial tables with geometry)
    print("Ensuring 'postgis' extension is enabled...")
    ext_res = run_psql_command("CREATE EXTENSION IF NOT EXISTS postgis;", dbname=DB_NAME)
    if ext_res.returncode == 0:
        print("PostGIS extension is active.")
    else:
        print(f"Notice/Warning on PostGIS extension: {ext_res.stderr.strip()}")

    return True

def restore_all():
    if not os.path.exists(BACKUP_DIR):
        print(f"Error: Backup directory not found: {BACKUP_DIR}")
        return

    backup_files = sorted(glob.glob(os.path.join(BACKUP_DIR, "*.backup")))
    total_files = len(backup_files)

    if total_files == 0:
        print(f"No .backup files found in {BACKUP_DIR}")
        return

    print("\n" + "=" * 60)
    print(f"2. Found {total_files} backup files to restore into '{DB_NAME}'")
    print(f"   Target: {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
    print("=" * 60 + "\n")

    env = os.environ.copy()
    env["PGPASSWORD"] = DB_PASS

    success_count = 0
    failed_files = []

    start_time = time.time()

    for idx, filepath in enumerate(backup_files, 1):
        filename = os.path.basename(filepath)
        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        print(f"[{idx:02d}/{total_files:02d}] Restoring {filename} ({size_mb:.2f} MB)...", end="", flush=True)

        cmd = [
            PG_RESTORE,
            "-h", DB_HOST,
            "-p", DB_PORT,
            "-U", DB_USER,
            "-d", DB_NAME,
            "--no-owner",             # Avoid ownership permission conflicts
            "--no-privileges",        # Avoid ACL permission conflicts
            "--clean",                # Drop existing table before restoring if it exists
            "--if-exists",            # Don't error if table didn't exist when dropping
            filepath
        ]

        result = subprocess.run(cmd, env=env, capture_output=True, text=True)

        # pg_restore returns 0 on success, or 1 on warnings (e.g., table didn't exist to drop)
        if result.returncode in [0, 1]:
            print(" DONE")
            success_count += 1
        else:
            print(" FAILED")
            print(f"     Error: {result.stderr.strip()[:200]}")
            failed_files.append((filename, result.stderr.strip()))

    elapsed = time.time() - start_time
    print("\n" + "=" * 60)
    print("3. RESTORATION SUMMARY")
    print("=" * 60)
    print(f"Total Files Processed : {total_files}")
    print(f"Successfully Restored : {success_count}")
    print(f"Failed                : {len(failed_files)}")
    print(f"Time Taken            : {elapsed:.1f} seconds")

    if failed_files:
        print("\nFailed Files:")
        for fname, err in failed_files:
            print(f"  - {fname}: {err[:100]}")

    # List restored tables and row counts
    print("\n" + "=" * 60)
    print("4. Verifying Restored Tables in PostgreSQL...")
    print("=" * 60)
    table_count_cmd = run_psql_command(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE';"
    )
    print(f"Total tables now in 'public' schema: {table_count_cmd.stdout.strip()}")
    print("\nAll done! You can now open pgAdmin, right-click your database/Tables and click 'Refresh'!")

if __name__ == "__main__":
    if ensure_database_and_postgis():
        restore_all()
