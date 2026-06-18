import sqlite3
import glob
import sys

year = sys.argv[1]
month = sys.argv[2]

files = glob.glob(f"dbs/**/*.db", recursive=True)
print(f"Found {len(files)} DB files: {files}")

final_db = f"{year}_{month}_FINAL.db"
main = sqlite3.connect(final_db)

for f in files:
    print(f"Merging {f}...")
    main.execute(f"ATTACH DATABASE '{f}' AS src")

    # Copy table structure from first file if table doesn't exist yet
    main.execute("""
        CREATE TABLE IF NOT EXISTS district_data AS
        SELECT * FROM src.district_data WHERE 0
    """)

    # Get columns from source and destination, add any missing ones
    src_cols = [r[1] for r in main.execute("PRAGMA src.table_info(district_data)").fetchall()]
    dst_cols = [r[1] for r in main.execute("PRAGMA table_info(district_data)").fetchall()]

    for col in src_cols:
        if col not in dst_cols:
            main.execute(f'ALTER TABLE district_data ADD COLUMN "{col}" TEXT DEFAULT "0.0"')

    main.execute("INSERT OR REPLACE INTO district_data SELECT * FROM src.district_data")
    main.execute("DETACH DATABASE src")
    print(f"  Done.")

main.commit()
main.close()
print(f"\nMerged into {final_db}")