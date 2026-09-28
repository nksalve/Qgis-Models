r"""
High-Speed Streaming District Spatial Join Pipeline
===================================================
Optimized for very large GeoPackages (5 GB - 22 GB, 10M - 60M features) on network drives.

Architecture:
1. Fast continuous stream copy from Z:\ to D:\temp_spatial_join\in.gpkg (16MB buffers).
2. Zero network interaction during queries: All SQLite/Pyogrio queries run on local SSD D:\.
3. Pre-filtering candidate districts by tile bounding box (reduces 778 districts to ~1-50).
4. Vectorized centroid spatial index query via shapely.prepare() + shapely.STRtree.
5. Chunked processing (250,000 features/batch) with fixed RAM footprint (< 2.5 GB).
6. Clean single-layer output written to D:\temp_spatial_join\out.gpkg.
7. Verification of row counts before safe atomic replacement on Z:\.
8. Automatic cleanup of D:\ temp files after each file.
"""

import os
import sys
import gc
import json
import time
import shutil
import sqlite3
import argparse
from pathlib import Path

# Configure UTF-8 output for Windows console
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import pyogrio
import shapely
import geopandas as gpd
from tqdm import tqdm


# Configuration defaults
DATA_DIR = Path(r"z:\footprint\Link Height Data").absolute()
DISTRICT_FILE = DATA_DIR / "DISTRICT.gpkg"
PROGRESS_FILE = DATA_DIR / "_district_join_progress.json"
STAGE_DIR = Path(r"D:\temp_spatial_join")
DEFAULT_CHUNK_SIZE = 250_000

DISTRICT_COLS = ["LG_State_N", "LG_State_C", "LG_Di_Name", "LG_Di_Code"]


def copy_with_progress(src: Path, dst: Path, desc: str):
    """Copies a file using 16MB stream buffers with a progress bar."""
    size = src.stat().st_size
    buf_size = 16 * 1024 * 1024  # 16 MB buffer
    if dst.exists():
        dst.unlink()

    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        with tqdm(total=size, unit="B", unit_scale=True, desc=desc, leave=False) as pbar:
            while True:
                buf = fsrc.read(buf_size)
                if not buf:
                    break
                fdst.write(buf)
                pbar.update(len(buf))


def load_progress(prog_path: Path) -> set:
    if prog_path.exists():
        try:
            with open(prog_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data.get("done", []))
        except Exception as e:
            print(f"WARN: Could not read progress file: {e}")
            return set()
    return set()


def save_progress(prog_path: Path, done_set: set):
    try:
        with open(prog_path, "w", encoding="utf-8") as f:
            json.dump({"done": sorted(done_set)}, f, indent=2)
    except Exception as e:
        print(f"WARN: Could not save progress file: {e}")


def select_best_layer(file_path: Path) -> str:
    """Select the primary layer name that contains the full set of attributes."""
    layers = pyogrio.list_layers(str(file_path))
    if len(layers) == 1:
        return layers[0][0]

    best_layer = layers[0][0]
    best_field_count = -1
    for lyr_name, _ in layers:
        try:
            info = pyogrio.read_info(str(file_path), layer=lyr_name)
            fields = set(info["fields"])
            if len(fields) > best_field_count:
                best_field_count = len(fields)
                best_layer = lyr_name
        except Exception:
            continue
    return best_layer


def get_fid_range_local(file_path: Path, layer_name: str):
    """Retrieve min and max fid directly from local SQLite database in O(1) time."""
    conn = sqlite3.connect(f"file:{file_path.as_posix()}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT min(fid), max(fid) FROM [{layer_name}]")
        min_fid, max_fid = cur.fetchone()
        return min_fid, max_fid
    finally:
        conn.close()


def process_file(
    file_path: Path,
    district_gdf: gpd.GeoDataFrame,
    stage_dir: Path,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> bool:
    """Processes a single large GeoPackage file by staging locally on D:\."""
    fname = file_path.name
    size_gb = file_path.stat().st_size / (1024**3)

    tqdm.write(f"\n{'='*75}")
    tqdm.write(f"[>] PROCESSING: {fname} ({size_gb:.2f} GB)")

    # Ensure clean staging directory
    stage_dir.mkdir(parents=True, exist_ok=True)
    local_in = stage_dir / f"in_{fname}"
    local_out = stage_dir / f"out_{fname}"

    if local_in.exists():
        local_in.unlink()
    if local_out.exists():
        local_out.unlink()

    # Step 1: Copy to local SSD D:\
    t0_copy_in = time.time()
    tqdm.write(f"  [1/4] Transferring {size_gb:.2f} GB to local SSD (D:\\)...")
    try:
        copy_with_progress(file_path, local_in, desc=f"  Downloading {fname[:20]}")
    except Exception as e:
        tqdm.write(f"  [-] Failed to copy file to local SSD: {e}")
        return False
    elapsed_in = time.time() - t0_copy_in
    rate_in = size_gb / (elapsed_in / 1024) if elapsed_in > 0 else 0
    tqdm.write(f"  [+] Local copy ready in {elapsed_in:.1f}s ({rate_in:.1f} MB/s)")

    # Step 2: Inspect local file
    try:
        target_layer = select_best_layer(local_in)
        info = pyogrio.read_info(str(local_in), layer=target_layer)
        total_features = info["features"]
        bounds = info["total_bounds"]
        crs = info["crs"]
        min_fid, max_fid = get_fid_range_local(local_in, target_layer)
        tqdm.write(f"  [2/4] Layer: '{target_layer}' | Total: {total_features:,} features | FID: {min_fid} - {max_fid}")
    except Exception as e:
        tqdm.write(f"  [-] Failed to inspect local file: {e}")
        if local_in.exists():
            local_in.unlink()
        return False

    if total_features == 0:
        tqdm.write("  [!] File contains 0 features, skipping.")
        local_in.unlink()
        return True

    # Step 3: Spatial Indexing & Candidate Filtering
    t0_prep = time.time()
    cand_dist = district_gdf.cx[bounds[0]:bounds[2], bounds[1]:bounds[3]].copy()
    if district_gdf.crs != crs:
        cand_dist = cand_dist.to_crs(crs)
    cand_count = len(cand_dist)
    tqdm.write(f"  Candidate districts in tile: {cand_count} / {len(district_gdf)}")

    dist_geoms = cand_dist.geometry.values
    shapely.prepare(dist_geoms)
    tree = shapely.STRtree(dist_geoms)
    tqdm.write(f"  STRtree initialized in {time.time()-t0_prep:.2f}s")

    # Step 4: Chunked Processing on local SSD
    tqdm.write(f"  [3/4] Streaming {total_features:,} features in chunks of {chunk_size:,}...")
    total_matched = 0
    total_processed = 0
    first_chunk = True

    pbar = tqdm(
        total=total_features,
        desc=f"  Computing {fname[:20]}",
        unit="feats",
        unit_scale=True,
        leave=False,
    )

    curr_fid = min_fid
    t0_proc = time.time()

    try:
        while curr_fid <= max_fid:
            end_fid = curr_fid + chunk_size - 1

            query = f"SELECT * FROM [{target_layer}] WHERE fid >= {curr_fid} AND fid <= {end_fid}"
            chunk = pyogrio.read_dataframe(str(local_in), sql=query)

            if len(chunk) > 0:
                if chunk.crs is None and crs is not None:
                    chunk.set_crs(crs, inplace=True)

                # Initialize columns with explicit types
                if "LG_State_N" not in chunk.columns:
                    chunk["LG_State_N"] = None
                    chunk["LG_State_N"] = chunk["LG_State_N"].astype("object")
                if "LG_State_C" not in chunk.columns:
                    chunk["LG_State_C"] = float("nan")
                    chunk["LG_State_C"] = chunk["LG_State_C"].astype("float64")
                if "LG_Di_Name" not in chunk.columns:
                    chunk["LG_Di_Name"] = None
                    chunk["LG_Di_Name"] = chunk["LG_Di_Name"].astype("object")
                if "LG_Di_Code" not in chunk.columns:
                    chunk["LG_Di_Code"] = float("nan")
                    chunk["LG_Di_Code"] = chunk["LG_Di_Code"].astype("float64")

                # Fast centroid vector query
                if cand_count > 0:
                    pts = shapely.centroid(chunk.geometry.values)
                    idx_geom, idx_dist = tree.query(pts, predicate="within")

                    if len(idx_geom) > 0:
                        chunk.loc[chunk.index[idx_geom], "LG_State_N"] = cand_dist["LG_State_N"].iloc[idx_dist].values
                        chunk.loc[chunk.index[idx_geom], "LG_State_C"] = cand_dist["LG_State_C"].iloc[idx_dist].astype("float64").values
                        chunk.loc[chunk.index[idx_geom], "LG_Di_Name"] = cand_dist["LG_Di_Name"].iloc[idx_dist].values
                        chunk.loc[chunk.index[idx_geom], "LG_Di_Code"] = cand_dist["LG_Di_Code"].iloc[idx_dist].astype("float64").values

                        total_matched += len(idx_geom)

                # Write to local out.gpkg
                if first_chunk:
                    pyogrio.write_dataframe(chunk, str(local_out), layer=target_layer, driver="GPKG")
                    first_chunk = False
                else:
                    pyogrio.write_dataframe(chunk, str(local_out), layer=target_layer, driver="GPKG", append=True)

                total_processed += len(chunk)
                pbar.update(len(chunk))

                del chunk
                if total_processed % (chunk_size * 4) == 0:
                    gc.collect()

            curr_fid = end_fid + 1

    except Exception as e:
        pbar.close()
        tqdm.write(f"\n  [-] ERROR during chunk processing at FID {curr_fid}: {e}")
        import traceback
        traceback.print_exc()
        if local_in.exists():
            local_in.unlink()
        if local_out.exists():
            local_out.unlink()
        return False

    pbar.close()
    elapsed_proc = time.time() - t0_proc
    match_pct = (100.0 * total_matched / total_processed) if total_processed > 0 else 0
    speed = total_processed / elapsed_proc if elapsed_proc > 0 else 0
    tqdm.write(
        f"  [+] Computation complete: {total_processed:,} features in {elapsed_proc:.1f}s "
        f"({speed:,.0f} feats/sec) | Matched: {total_matched:,} ({match_pct:.2f}%)"
    )

    # Free input file immediately to free D:\ disk space
    if local_in.exists():
        local_in.unlink()

    # Step 5: Verification
    out_info = pyogrio.read_info(str(local_out), layer=target_layer)
    if out_info["features"] != total_processed:
        tqdm.write(f"  [-] Verification failed: expected {total_processed:,}, got {out_info['features']:,}!")
        local_out.unlink()
        return False

    out_size_gb = local_out.stat().st_size / (1024**3)
    tqdm.write(f"  [+] Output verified: {out_info['features']:,} features ({out_size_gb:.2f} GB)")

    # Step 6: Transfer back to network drive Z:\
    t0_copy_out = time.time()
    temp_target_on_z = file_path.with_suffix(".tmp_fast.gpkg")
    if temp_target_on_z.exists():
        temp_target_on_z.unlink()

    tqdm.write(f"  [4/4] Uploading {out_size_gb:.2f} GB back to network drive...")
    try:
        copy_with_progress(local_out, temp_target_on_z, desc=f"  Uploading {fname[:20]}")
    except Exception as e:
        tqdm.write(f"  [-] Failed to transfer file back to network drive: {e}")
        return False

    # Remove local out file to free D:\ space
    local_out.unlink()
    elapsed_out = time.time() - t0_copy_out
    tqdm.write(f"  [+] Network upload complete in {elapsed_out:.1f}s")

    # Step 7: Atomic replace on Z:\
    replaced = False
    for attempt in range(8):
        try:
            gc.collect()
            temp_target_on_z.replace(file_path)
            replaced = True
            break
        except PermissionError as pe:
            tqdm.write(f"  [!] Lock on {fname} (attempt {attempt+1}/8): {pe}. Waiting {attempt+2}s...")
            time.sleep(attempt + 2)
            gc.collect()

    if not replaced:
        tqdm.write(f"  [-] Could not replace original file. Saved as {temp_target_on_z.name}")
        return False

    tqdm.write(f"[+] COMPLETED: {fname}\n")
    return True


def main():
    parser = argparse.ArgumentParser(description="High-Speed District Spatial Join")
    parser.add_argument("--file", type=str, help="Process a single specific file")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE, help="Features per batch")
    args = parser.parse_args()

    print("=" * 75)
    print("ULTRA-FAST DISTRICT SPATIAL JOIN PIPELINE")
    print(f"Data directory:    {DATA_DIR}")
    print(f"Staging directory: {STAGE_DIR}")
    print(f"District file:     {DISTRICT_FILE}")
    print(f"Chunk size:        {args.chunk_size:,} features")
    print("=" * 75)

    # 1. Load Reference Boundaries
    print("\nLoading reference district boundaries...")
    if not DISTRICT_FILE.exists():
        print(f"ERROR: District file not found: {DISTRICT_FILE}")
        sys.exit(1)

    t0 = time.time()
    district_gdf = gpd.read_file(str(DISTRICT_FILE))
    print(f"[+] Loaded {len(district_gdf)} district polygons in {time.time()-t0:.2f}s")
    print(f"    Columns: {district_gdf.columns.tolist()}\n")

    # 2. Check Staging Free Space
    STAGE_DIR.mkdir(parents=True, exist_ok=True)
    _, _, stage_free = shutil.disk_usage(str(STAGE_DIR))
    stage_free_gb = stage_free / (1024**3)
    print(f"Staging Drive Free Space: {stage_free_gb:.1f} GB")
    if stage_free_gb < 25.0:
        print(f"WARN: Less than 25 GB free space on {STAGE_DIR}. Minimum recommended is 30 GB.")

    # 3. Load Progress
    done_files = load_progress(PROGRESS_FILE)
    print(f"Already completed (per progress log): {len(done_files)} files\n")

    # 4. Find Target Files
    all_linked_paths = sorted(DATA_DIR.glob("*_linked.gpkg"), key=lambda p: p.stat().st_size)
    if args.file:
        target_files = [p for p in all_linked_paths if p.name == args.file]
        if not target_files:
            print(f"ERROR: Specified file '{args.file}' not found in {DATA_DIR}")
            sys.exit(1)
    else:
        target_files = [p for p in all_linked_paths if p.name not in done_files]

    print(f"Found {len(target_files)} pending file(s) to process:")
    for tf in target_files:
        print(f"  - {tf.name} ({tf.stat().st_size / (1024**3):.2f} GB)")
    print()

    if not target_files:
        print("[+] All files are already completed! Nothing to do.")
        return

    # 5. Process Files
    overall_pbar = tqdm(
        total=len(target_files),
        desc="Overall Files Progress",
        unit="file",
    )

    completed_count = 0
    failed_count = 0

    for tf in target_files:
        success = process_file(
            file_path=tf,
            district_gdf=district_gdf,
            stage_dir=STAGE_DIR,
            chunk_size=args.chunk_size,
        )

        if success:
            done_files.add(tf.name)
            save_progress(PROGRESS_FILE, done_files)
            completed_count += 1
        else:
            failed_count += 1

        overall_pbar.update(1)

    overall_pbar.close()

    print("\n" + "=" * 75)
    print("PIPELINE EXECUTION SUMMARY")
    print(f"Completed this run: {completed_count} file(s)")
    print(f"Failed this run:    {failed_count} file(s)")
    print(f"Total done in log:  {len(done_files)} / {len(all_linked_paths)}")
    print("=" * 75)


if __name__ == "__main__":
    main()
