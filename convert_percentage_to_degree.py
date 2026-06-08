"""
Convert slope PERCENT values to slope DEGREES for all shapefiles
located in D:\\Slop_MP\\clipped_output.

Conversion formula:
    slope_degrees = atan(slope_percent / 100) * (180 / pi)

A new column 'Slope_Deg' (slope in degrees) is added to each shapefile.
The original percent value column is preserved.
Converted shapefiles are written to: clipped_output\\degree_output
"""

import os
import math
from pathlib import Path

import geopandas as gpd

# ===== PATHS =====
input_dir = r"D:\Slop_MP\clipped_output"
output_dir = r"D:\Slop_MP\clipped_output\degree_output"

os.makedirs(output_dir, exist_ok=True)

# Candidate column names that may hold the slope percent value
SLOPE_COLUMN_CANDIDATES = ["DN", "Value", "value", "dn", "SLOPE", "Slope", "slope"]


def percent_to_degree(percent):
    """Convert a slope percentage value to degrees."""
    if percent is None:
        return None
    try:
        return math.degrees(math.atan(float(percent) / 100.0))
    except (TypeError, ValueError):
        return None


def main():
    shapefiles = sorted(Path(input_dir).glob("*.shp"))

    if not shapefiles:
        print(f"ERROR: No shapefiles found in {input_dir}")
        return

    print(f"Found {len(shapefiles)} shapefiles\n")

    processed = 0
    for idx, shp_file in enumerate(shapefiles):
        shp_name = shp_file.stem
        print(f"[{idx + 1}/{len(shapefiles)}] Processing {shp_name}...")

        try:
            gdf = gpd.read_file(str(shp_file))
            print(f"  Loaded: {len(gdf)} records")

            slope_col = None
            for col in SLOPE_COLUMN_CANDIDATES:
                if col in gdf.columns:
                    slope_col = col
                    break

            if slope_col is None:
                print("  WARNING: No slope value column found.")
                print(f"  Available columns: {list(gdf.columns)}")
                continue

            print(f"  Using percent column: {slope_col}")

            gdf["Slope_Deg"] = gdf[slope_col].apply(percent_to_degree).round(4)

            output_file = f"{shp_name}_degree.shp"
            output_path = os.path.join(output_dir, output_file)
            gdf.to_file(output_path)

            print(f"  SAVED: {output_file}")
            try:
                print(
                    f"    Percent range: {gdf[slope_col].min()} - {gdf[slope_col].max()}"
                    f"  ->  Degree range: {gdf['Slope_Deg'].min()} - {gdf['Slope_Deg'].max()}"
                )
            except Exception:
                pass
            print()
            processed += 1

        except Exception as e:
            print(f"  ERROR: {e}\n")

    print(f"Done! Converted {processed}/{len(shapefiles)} shapefiles.")
    print(f"Output folder: {output_dir}")


if __name__ == "__main__":
    main()
