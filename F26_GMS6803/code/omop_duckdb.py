"""
load_folder_to_duckdb.py

Walks a folder and loads CSV data into a DuckDB database:
  - Any .zip files found are opened and their .csv members are extracted
    and loaded.
  - Any loose .csv files sitting directly in the folder (or subfolders)
    are also loaded.
  - Each source file becomes its own table.

Assumptions (adjust the CONFIG section below if these don't match your data):
  - Standard comma-delimited CSVs with a header row on line 1.
  - Table names are derived from the filename (sanitize_table_named, lowercased).
    If two files share the same name (e.g. one inside a zip and one loose,
    or in different subfolders), the table name is disambiguated using the
    parent folder / zip name so nothing silently overwrites another table.

Usage:
    python load_folder_to_duckdb.py path/to/main_folder path/to/output.duckdb
"""

import argparse
import duckdb
import re
import sys
import tempfile
import zipfile
from pathlib import Path

# ---- CONFIG ----------------------------------------------------------
DELIMITER = ","
QUOTECHAR = '"'
HAS_HEADER = True
ENCODING = "utf-8"

# Type inference: DuckDB's read_csv only samples the first N rows by
# default to guess column types, which breaks when a column starts out
# looking numeric (e.g. IDs like "1001") but later has alphanumeric
# values (e.g. "G0001") - common in OMOP-style extracts.
#   SAMPLE_SIZE = -1     -> scan the entire file before inferring types
#                           (accurate, but slower on very large files)
#   ALL_VARCHAR = True   -> skip inference entirely, load every column
#                           as VARCHAR (fastest, safest for messy data;
#                           cast columns yourself afterward as needed)
SAMPLE_SIZE = -1
ALL_VARCHAR = False
# ------------------------------------------------------------------


def sanitize_table_name(filename: str) -> str:
    """Turn a filename into a safe DuckDB table name."""
    name = Path(filename).stem
    name = re.sub(r"[^0-9a-zA-Z_]", "_", name)
    name = name.lower().strip("_")
    name = name.replace("deid_","").replace("_v202607","")
    if re.match(r"^\d", name):
        name = f"t_{name}"
    return name



def unique_table_name(base_name: str, used_names: set) -> str:
    name = base_name
    counter = 2
    while name in used_names:
        name = f"{base_name}_{counter}"
        counter += 1
    used_names.add(name)
    return name


def load_csv_into_table(con, csv_path: Path, table_name: str, if_exists: str) -> None:
    print(f"Loading '{csv_path.name}' -> table '{table_name}'")
    try:
        if if_exists not in ("replace", "append"):
            raise ValueError("if_exists must be 'replace' or 'append'")

        read_csv_sql = '''
            read_csv(
                ?, delim=?, quote=?, header=?, encoding=?,
                ignore_errors=false, sample_size=?, all_varchar=?
            )
        '''
        params = [
            str(csv_path), DELIMITER, QUOTECHAR, HAS_HEADER, ENCODING,
            SAMPLE_SIZE, ALL_VARCHAR,
        ]

        if if_exists == "replace":
            con.execute(f'DROP TABLE IF EXISTS "{table_name}"')
            con.execute(
                f'CREATE TABLE "{table_name}" AS SELECT * FROM {read_csv_sql}',
                params,
            )
        else:  # append
            con.execute(
                f'INSERT INTO "{table_name}" SELECT * FROM {read_csv_sql}',
                params,
            )

        row_count = con.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
        print(f"  -> {row_count:,} rows")

    except Exception as e:
        print(f"  !! Failed to load '{csv_path.name}': {e}", file=sys.stderr)


def load_folder_to_duckdb(folder_path: str, db_path: str, if_exists: str = "replace") -> None:
    folder_path = Path(folder_path)
    if not folder_path.exists():
        raise FileNotFoundError(f"Folder not found: {folder_path}")

    con = duckdb.connect(str(db_path))
    used_names = set()

    zip_files = sorted(folder_path.rglob("*.zip"))
    loose_csvs = sorted(
        p for p in folder_path.rglob("*.csv")
    )

    if not zip_files and not loose_csvs:
        print(f"No .zip or .csv files found under {folder_path}")
        con.close()
        return

    # --- Handle zip files: extract CSVs and load ---
    with tempfile.TemporaryDirectory() as tmpdir:
        for zpath in zip_files:
            print(f"\nOpening zip: {zpath.relative_to(folder_path)}")
            try:
                with zipfile.ZipFile(zpath, "r") as zf:
                    csv_members = [m for m in zf.namelist() if m.lower().endswith(".csv")]
                    if not csv_members:
                        print("  (no .csv files inside)")
                        continue
                    for member in csv_members:
                        extracted_path = Path(zf.extract(member, path=tmpdir))
                        base_name = sanitize_table_name(f"{zpath.stem}_{extracted_path.stem}")
                        table_name = unique_table_name(base_name, used_names)
                        load_csv_into_table(con, extracted_path, table_name, if_exists)
            except zipfile.BadZipFile:
                print(f"  !! '{zpath.name}' is not a valid zip file, skipping", file=sys.stderr)

        # --- Handle loose CSVs (not inside any zip) ---
        if loose_csvs:
            print("\nLoading loose CSV files...")
        for csv_path in loose_csvs:
            base_name = sanitize_table_name(csv_path.stem)
            table_name = unique_table_name(base_name, used_names)
            load_csv_into_table(con, csv_path, table_name, if_exists)

    con.close()
    print(f"\nDone. Database written to: {db_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Load CSVs (loose or inside zip files) from a folder into DuckDB."
    )
    parser.add_argument("folder_path", help="Path to the main folder to scan")
    parser.add_argument("db_path", help="Path to the DuckDB database file to create/update")
    parser.add_argument(
        "--if-exists",
        choices=["replace", "append"],
        default="replace",
        help="Whether to replace or append to existing tables (default: replace)",
    )
    args = parser.parse_args()

    load_folder_to_duckdb(args.folder_path, args.db_path, args.if_exists)


if __name__ == "__main__":
    main()

