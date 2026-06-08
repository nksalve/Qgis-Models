import os
import gc
import geopandas as gpd
import rasterio
from rasterio.features import shapes
import numpy as np
from pathlib import Path

# ==========================================
# CONFIGURATION & PATHS
# ==========================================
raster_dir = r"D:\Slop_MP\Slop"                # Input folder with .tif raster files
boundingbox_folder = r"d:\Slop_MP\Boundingbox" # Folder with District Bounding Box Shapefiles
district_folder = r"d:\Slop_MP\District Boundary" # Folder with District Boundary Shapefiles
output_dir = r"D:\Slop_MP\clipped_output\classified" # Final Output Folder

os.makedirs(output_dir, exist_ok=True)

# ==========================================
# CLASSIFICATION FUNCTIONS
# ==========================================
def get_slope_class(dn_value):
    """Classify slope based on DN value"""
    if dn_value <= 10:
        return "Plain"
    elif dn_value <= 25:
        return "Rolling"
    elif dn_value <= 60:
        return "Mountainous"
    else:
        return "Steep"

def get_slope_percentage(slope_class):
    """Get slope percentage range based on class"""
    ranges = {
        "Plain": "0-10",
        "Rolling": "10-25",
        "Mountainous": "25-60",
        "Steep": ">60"
    }
    return ranges.get(slope_class, "")

# ==========================================
# MAIN PROCESSING FUNCTION
# ==========================================
def main():
    print(f"{'='*60}")
    print("  RASTER TO VECTOR -> DISSOLVE -> CLIP -> CLASSIFY")
    print(f"{'='*60}\n")

    # 1. Load Bounding Boxes
    print("Loading Bounding Boxes...")
    bbox_dict = {}
    for bbox_file in Path(boundingbox_folder).glob("*.shp"):
        bbox_name = bbox_file.stem
        try:
            bbox_dict[bbox_name] = gpd.read_file(str(bbox_file))
        except Exception as e:
            print(f"  [!] ERROR loading bounding box {bbox_name}: {e}")
    print(f"  -> Loaded {len(bbox_dict)} bounding boxes.\n")

    # 2. Load District Boundaries
    print("Loading District Boundaries...")
    district_dict = {}
    for district_file in Path(district_folder).glob("*.shp"):
        district_name = district_file.stem
        try:
            district_dict[district_name] = gpd.read_file(str(district_file))
        except Exception as e:
            print(f"  [!] ERROR loading district {district_name}: {e}")
    print(f"  -> Loaded {len(district_dict)} districts.\n")

    # 3. Find already completed files to skip
    completed_combinations = set()
    completed_files_str = []
    for out_file in Path(output_dir).glob("*.shp"):
        completed_files_str.append(out_file.name.lower())
        # Expecting format: DistrictName_RasterName_classified.shp
        parts = out_file.stem.split('_')
        if len(parts) >= 2:
            dist_name = parts[0]
            rast_name = "_".join(parts[1:-1]) # In case raster name has underscores
            completed_combinations.add(f"{dist_name}_{rast_name}")

    # 4. Get Raster Files
    raster_files = list(Path(raster_dir).glob("*.tif"))
    if not raster_files:
        print(f"[!] No raster files found in {raster_dir}")
        return

    print(f"Found {len(raster_files)} raster file(s) to process.\n")

    # 5. Process Each Raster
    for idx, raster_file in enumerate(raster_files):
        filename = raster_file.name
        name_only = raster_file.stem
        
        # Check if raster is already in completed files (100% done)
        is_already_done = any(name_only.lower() in f for f in completed_files_str)
        if is_already_done:
            print(f"\n{'-'*50}")
            print(f"[{idx+1}/{len(raster_files)}] SKIPPING RASTER: {filename} (Already fully processed)")
            print(f"{'-'*50}")
            continue

        print(f"\n{'-'*50}")
        print(f"[{idx+1}/{len(raster_files)}] PROCESSING RASTER: {filename}")
        print(f"{'-'*50}")

        try:
            # Step A: RASTER TO VECTOR
            print("  [Step 1] Converting Raster to Vector polygons...")
            with rasterio.open(str(raster_file)) as src:
                image = src.read(1)
                
                mask = None
                if src.nodata is not None:
                    mask = image != src.nodata
                elif np.isnan(image).any():
                    mask = ~np.isnan(image)
                
                results = (
                    {'properties': {'DN': float(v)}, 'geometry': s}
                    for i, (s, v) in enumerate(shapes(image, mask=mask, transform=src.transform))
                )
                
                gdf = gpd.GeoDataFrame.from_features(list(results))
                gdf.crs = src.crs

            if gdf.empty:
                print("  -> [!] Raster converted to empty vector. Skipping.")
                continue

            print(f"  -> Generated {len(gdf)} raw polygons.")

            # Drop empty geometries just in case (from Dissolve_shp.py logic)
            gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()]

            # Step B: DISSOLVE BY DN
            print("  [Step 2] Dissolving polygons by DN value...")
            dissolved_gdf = gdf.dissolve(by='DN').reset_index()
            print(f"  -> Reduced to {len(dissolved_gdf)} dissolved polygons.")
            
            # Free up memory from raw geometries
            del gdf
            gc.collect()

            slope_bounds = dissolved_gdf.total_bounds

            # Step C: CLIP WITH DISTRICTS
            print("  [Step 3] Checking intersections with districts...")
            for dist_name, bbox_gdf in bbox_dict.items():
                
                # Check if already processed
                combo_key = f"{dist_name}_{name_only}"
                if combo_key in completed_combinations:
                    continue
                    
                output_file = f"{dist_name}_{name_only}_classified.shp"
                output_path = os.path.join(output_dir, output_file)
                
                if os.path.exists(output_path):
                    print(f"    ✓ SKIPPED: {output_file} already exists")
                    completed_combinations.add(combo_key)
                    continue
                
                if dist_name in district_dict:
                    district_gdf = district_dict[dist_name]
                    
                    # Convert CRS if mismatch
                    if dissolved_gdf.crs != district_gdf.crs:
                        district_gdf = district_gdf.to_crs(dissolved_gdf.crs)
                    
                    district_bounds = district_gdf.total_bounds
                    
                    # Bounds overlap check (FAST)
                    bounds_overlap = (slope_bounds[0] <= district_bounds[2] and 
                                     slope_bounds[2] >= district_bounds[0] and
                                     slope_bounds[1] <= district_bounds[3] and 
                                     slope_bounds[3] >= district_bounds[1])
                    
                    if bounds_overlap:
                        print(f"    -> Clipping with {dist_name}...")
                        clipped_gdf = gpd.clip(dissolved_gdf, district_gdf)
                        
                        if len(clipped_gdf) > 0:
                            # Verify Coverage Area
                            clipped_union = clipped_gdf.unary_union
                            district_union = district_gdf.unary_union
                            
                            coverage_area = clipped_union.intersection(district_union).area
                            district_area = district_union.area
                            coverage_percent = (coverage_area / district_area) * 100 if district_area > 0 else 0
                            
                            if coverage_percent >= 95:
                                print(f"      ✓ Coverage: {coverage_percent:.1f}% (Valid)")
                                
                                # Step D: CLASSIFY
                                print(f"      -> Classifying slope values...")
                                clipped_gdf = clipped_gdf.copy() 
                                clipped_gdf['Class'] = clipped_gdf['DN'].apply(get_slope_class)
                                clipped_gdf['Percentage'] = clipped_gdf['Class'].apply(get_slope_percentage)

                                # Final dissolve by Class so that output is very clean
                                final_gdf = clipped_gdf.dissolve(by=['Class', 'Percentage']).reset_index()

                                # Save Output
                                final_gdf.to_file(output_path)
                                completed_combinations.add(combo_key)
                                print(f"      ✓ SAVED FINAL OUTPUT: {output_file} ({len(final_gdf)} distinct classes)")
                            else:
                                print(f"      ✗ Skipped {dist_name}: Coverage only {coverage_percent:.1f}% (Requires >=95%)")
                                
            # Free up memory after processing this raster
            del dissolved_gdf
            gc.collect()

        except Exception as e:
            print(f"  [!] ERROR processing {filename}: {e}\n")

    print(f"\n{'='*60}")
    print("ALL PROCESSING COMPLETE!")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()