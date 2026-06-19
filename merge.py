import sqlite3
import glob
import sys
import os
import re

year  = sys.argv[1]
month = sys.argv[2]
mode  = sys.argv[3] if len(sys.argv) > 3 else "all"  # "all" or "one"

# Only pick up DB files matching the parallel job naming pattern for THIS mode
# e.g. 2023_4_0_9_all.db or 2023_4_27_None_one.db — NOT old-format files,
# and NOT files from the other mode (so an "all" run never silently merges
# in leftover "one" chunks, or vice versa).
pattern = f"dbs/**/{year}_{month}_*_{mode}.db"
files   = glob.glob(pattern, recursive=True)

print(f"Found {len(files)} DB files to merge (mode={mode}): {files}")

if not files:
    print("No matching DB files found — exiting")
    sys.exit(1)

final_db = f"{year}_{month}_FINAL_{mode}.db"

# Remove stale final DB if it exists from a previous run
if os.path.exists(final_db):
    os.remove(final_db)

for f in files:
    print(f"Merging {f}...")

    # Open both connections fresh for each file — no ATTACH, avoids locking
    src  = sqlite3.connect(f)
    dst  = sqlite3.connect(final_db)

    try:
        src_cur = src.execute("PRAGMA table_info(district_data)")
        src_cols = [row[1] for row in src_cur.fetchall()]

        if not src_cols:
            print(f"  [SKIP] {f} has no district_data table")
            continue

        # Create table in destination if it doesn't exist yet
        src_create = src.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='district_data'"
        ).fetchone()

        if src_create:
            dst.execute(src_create[0].replace(
                "CREATE TABLE", "CREATE TABLE IF NOT EXISTS"
            ))

        # Add any columns in source that are missing in destination
        dst_cols = [row[1] for row in dst.execute("PRAGMA table_info(district_data)").fetchall()]
        for col in src_cols:
            if col not in dst_cols:
                dst.execute(f'ALTER TABLE district_data ADD COLUMN "{col}" TEXT DEFAULT "0.0"')

        # Read all rows from source and insert into destination
        rows = src.execute("SELECT * FROM district_data").fetchall()
        if rows:
            placeholders = ", ".join("?" for _ in src_cols)
            col_names    = ", ".join(f'"{c}"' for c in src_cols)
            dst.executemany(
                f'INSERT OR REPLACE INTO district_data ({col_names}) VALUES ({placeholders})',
                rows
            )
            dst.commit()
            print(f"  Merged {len(rows)} rows from {f}")
        else:
            print(f"  [SKIP] {f} is empty")

    finally:
        src.close()
        dst.close()

print(f"\nDone — merged into {final_db}")