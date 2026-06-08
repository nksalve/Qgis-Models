"""
batch_dissolve_gpkg.py
----------------------
Batch-dissolve multiple GeoPackage files to Shapefiles in parallel,
with a live progress bar for each file.

Usage:
    pip install tqdm
    python batch_dissolve_gpkg.py

Edit the CONFIG section at the bottom to set your input folder,
output folder, dissolve field, and worker count.
"""

import os
import gc
import glob
import time
import traceback
from multiprocessing import Manager
from concurrent.futures import ProcessPoolExecutor, as_completed

import geopandas as gpd

try:
    from tqdm import tqdm
except ImportError:
    raise SystemExit("tqdm is required: pip install tqdm")


# ─────────────────────────────────────────────────────────────────────────────
# Progress-reporting helper (runs inside each subprocess)
# ─────────────────────────────────────────────────────────────────────────────

class _SubProgress:
    """Thin wrapper that pushes (file_label, step, total) tuples into a queue."""

    def __init__(self, queue, label: str):
        self._q     = queue
        self._label = label

    def update(self, step: int, total: int, desc: str = ""):
        self._q.put(("update", self._label, step, total, desc))

    def close(self):
        self._q.put(("close", self._label, 0, 0, ""))


# ─────────────────────────────────────────────────────────────────────────────
# Core worker function (runs in each subprocess)
# ─────────────────────────────────────────────────────────────────────────────

def dissolve_single(
    input_gpkg_path: str,
    output_folder:   str,
    dissolve_field:  str,
    queue,                     # multiprocessing.Manager().Queue()
) -> dict:
    """
    Dissolve one GPKG file and write the result as a Shapefile.

    Progress steps (6 total):
        1 – file opened
        2 – field validated
        3 – geometries fixed
        4 – null geoms dropped
        5 – dissolve done
        6 – shapefile written
    """
    t0               = time.perf_counter()
    base_name        = os.path.basename(input_gpkg_path)
    file_name_no_ext = os.path.splitext(base_name)[0]
    output_shp_path  = os.path.join(output_folder, f"dissolve_{file_name_no_ext}.shp")
    STEPS            = 6

    progress = _SubProgress(queue, base_name)

    try:
        # ── 1. Read ──────────────────────────────────────────────────────────
        gdf = gpd.read_file(input_gpkg_path, engine="pyogrio")
        progress.update(1, STEPS, "read")

        if gdf.empty:
            progress.close()
            return _result(base_name, "SKIP", "Empty layer – skipped.", t0)

        # ── 2. Validate field ────────────────────────────────────────────────
        if dissolve_field not in gdf.columns:
            available = list(gdf.columns)
            progress.close()
            return _result(
                base_name, "ERROR",
                f"Column '{dissolve_field}' not found. "
                f"Available columns: {available}", t0,
            )
        progress.update(2, STEPS, "field ok")

        # ── 3. Fix invalid geometries ────────────────────────────────────────
        invalid_mask = ~gdf.geometry.is_valid
        if invalid_mask.any():
            gdf.loc[invalid_mask, "geometry"] = (
                gdf.loc[invalid_mask, "geometry"].buffer(0)
            )
        progress.update(3, STEPS, "geom fixed")

        # ── 4. Drop null / empty geometries ─────────────────────────────────
        gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()]
        progress.update(4, STEPS, "nulls dropped")

        # ── 5. Dissolve ──────────────────────────────────────────────────────
        dissolved_gdf = (
            gdf.dissolve(by=dissolve_field, as_index=False)
               .reset_index(drop=True)
        )
        del gdf
        gc.collect()
        progress.update(5, STEPS, "dissolved")

        # ── 6. Write shapefile ───────────────────────────────────────────────
        os.makedirs(output_folder, exist_ok=True)
        dissolved_gdf.to_file(output_shp_path, engine="pyogrio")
        progress.update(6, STEPS, "written")
        progress.close()

        msg = (
            f"OK → dissolve_{file_name_no_ext}.shp  "
            f"({len(dissolved_gdf)} features from previous count)"
        )
        return _result(base_name, "OK", msg, t0)

    except Exception:
        progress.close()
        return _result(base_name, "ERROR", traceback.format_exc(), t0)


def _result(file, status, message, t0):
    return {
        "file":        file,
        "status":      status,
        "message":     message,
        "elapsed_sec": round(time.perf_counter() - t0, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Batch runner
# ─────────────────────────────────────────────────────────────────────────────

def batch_dissolve(
    input_folder:   str,
    output_folder:  str,
    dissolve_field: str,
    max_workers:    int  = 4,
    recursive:      bool = False,
):
    """
    Discover all .gpkg files in input_folder and dissolve them in parallel,
    showing a live per-file progress bar for each one.

    Parameters
    ----------
    input_folder   : folder (or glob pattern) containing .gpkg files
    output_folder  : where dissolved shapefiles will be written
    dissolve_field : attribute column name to dissolve on
    max_workers    : parallel processes (rule-of-thumb: CPU count – 1)
    recursive      : if True, also search sub-folders
    """
    pattern    = os.path.join(input_folder, "**/*.gpkg" if recursive else "*.gpkg")
    gpkg_files = sorted(glob.glob(pattern, recursive=recursive))

    if not gpkg_files:
        print(f"[!] No .gpkg files found in: {input_folder}")
        return

    total = len(gpkg_files)
    print(f"\n{'─'*62}")
    print(f"  Batch dissolve  |  field: '{dissolve_field}'")
    print(f"  Input  : {input_folder}")
    print(f"  Output : {output_folder}")
    print(f"  Files  : {total}   Workers: {max_workers}")
    print(f"{'─'*62}\n")

    # ── Shared queue between main process and workers ─────────────────────────
    manager   = Manager()
    queue     = manager.Queue()

    # One tqdm bar per file, keyed by filename
    MAX_LABEL = 30                   # chars shown in bar description
    bars: dict[str, tqdm] = {}

    def _make_bar(label: str) -> tqdm:
        short = label if len(label) <= MAX_LABEL else "…" + label[-(MAX_LABEL - 1):]
        return tqdm(
            total       = 6,
            desc        = f"  {short:<{MAX_LABEL}}",
            unit        = "step",
            leave       = True,
            dynamic_ncols= True,
            bar_format  = (
                "{desc} |{bar:20}| {n_fmt}/{total_fmt} {postfix}"
            ),
        )

    results   = []
    completed = 0
    t_start   = time.perf_counter()

    # ── Overall progress bar ──────────────────────────────────────────────────
    overall_bar = tqdm(
        total       = total,
        desc        = "  Overall",
        unit        = "file",
        leave       = True,
        dynamic_ncols= True,
        colour      = "green",
        bar_format  = "{desc} |{bar:25}| {n_fmt}/{total_fmt} files  [{elapsed}<{remaining}]",
        position    = 0,
    )

    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(dissolve_single, fp, output_folder, dissolve_field, queue): fp
            for fp in gpkg_files
        }

        # ── Drain the progress queue until all futures are done ──────────────
        done_set = set()
        while len(done_set) < total:
            # Poll queue (non-blocking loop so we also catch finished futures)
            while not queue.empty():
                msg = queue.get_nowait()
                kind, label, step, total_steps, desc = msg

                if label not in bars:
                    bars[label] = _make_bar(label)

                bar = bars[label]

                if kind == "update":
                    # Advance to exactly `step`
                    increment = step - bar.n
                    if increment > 0:
                        bar.set_postfix_str(desc, refresh=False)
                        bar.update(increment)
                elif kind == "close":
                    if bar.n < bar.total:
                        bar.update(bar.total - bar.n)   # fill to 100 % on error/skip
                    bar.close()

            # Check for finished futures
            for future in list(futures):
                if future.done() and future not in done_set:
                    done_set.add(future)
                    completed += 1
                    res = future.result()
                    results.append(res)
                    overall_bar.update(1)

                    icon = "✓" if res["status"] == "OK" else ("⚠" if res["status"] == "SKIP" else "✗")
                    overall_bar.write(
                        f"  {icon} {res['file']:<35} {res['elapsed_sec']:>6.1f}s  {res['status']}"
                    )
                    if res["status"] == "ERROR":
                        overall_bar.write(f"    └─ {res['message'][:200]}")

            time.sleep(0.05)    # 50 ms poll

    # Flush remaining queue messages (edge-case: close signals after pool exits)
    while not queue.empty():
        msg  = queue.get_nowait()
        kind, label, step, total_steps, desc = msg
        if label in bars:
            bar = bars[label]
            if kind == "close" and not bar.disable:
                bar.update(bar.total - bar.n)
                bar.close()

    overall_bar.close()
    manager.shutdown()

    # ── Summary ───────────────────────────────────────────────────────────────
    ok_count    = sum(1 for r in results if r["status"] == "OK")
    skip_count  = sum(1 for r in results if r["status"] == "SKIP")
    error_count = sum(1 for r in results if r["status"] == "ERROR")
    total_time  = round(time.perf_counter() - t_start, 1)

    print(f"\n{'─'*62}")
    print(f"  Done in {total_time}s   "
          f"✓ {ok_count} success   ⚠ {skip_count} skipped   ✗ {error_count} errors")
    print(f"{'─'*62}\n")

    errors = [r for r in results if r["status"] == "ERROR"]
    if errors:
        print("ERROR DETAILS:")
        for r in errors:
            print(f"\n  File : {r['file']}")
            print(f"  Msg  : {r['message']}")


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — edit this section before running
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    CONFIG = {
        # Folder containing your .gpkg files (use raw string on Windows)
        "input_folder":   r"D:\Slop_MP\Slop",

        # Where dissolved shapefiles will be saved
        "output_folder":  r"D:\Slop_MP\Slop\dissolved_output",

        # Column to dissolve on
        "dissolve_field": "DN",

        # Number of parallel workers
        # Recommended: (CPU cores - 1), e.g. 4 for a 6-core machine
        "max_workers":    4,

        # Set True to also search sub-folders
        "recursive":      False,
    }

    batch_dissolve(**CONFIG)