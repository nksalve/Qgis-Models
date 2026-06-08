import geopandas as gpd
import os
from pathlib import Path
import multiprocessing
import time
import warnings
warnings.filterwarnings('ignore')

# Global variables for workers to avoid reloading for every slope file
worker_district_dict = {}
worker_bbox_names = []
worker_completed_districts = None

def init_worker(boundingbox_folder, district_folder, shared_completed_districts):
    global worker_district_dict
    global worker_bbox_names
    global worker_completed_districts
    
    worker_completed_districts = shared_completed_districts
    
    # Get bounding box names (we don't need to load their geometry for the clip operation)
    worker_bbox_names = [f.stem for f in Path(boundingbox_folder).glob("*.shp")]
            
    # Load districts
    for district_file in Path(district_folder).glob("*.shp"):
        district_name = district_file.stem
        try:
            worker_district_dict[district_name] = gpd.read_file(str(district_file))
        except Exception as e:
            pass

def process_slope_file(args):
    slope_file, idx, total, output_dir = args
    slope_name = slope_file.stem
    file_size_mb = slope_file.stat().st_size / (1024*1024)
    print(f"[{idx+1}/{total}] Processing {slope_name} ({file_size_mb:.2f} MB)...", flush=True)
    
    shapefile_count = 0
    try:
        slope_gdf = gpd.read_file(str(slope_file))
        
        # For each bounding box
        for bbox_name in worker_bbox_names:
            # Skip if another process or previous run already completed this district
            if bbox_name in worker_completed_districts:
                continue
                
            output_file = f"{bbox_name}_{slope_name}.shp"
            output_path = os.path.join(output_dir, output_file)
            
            # Check if slope and district overlap
            if bbox_name in worker_district_dict:
                district_gdf = worker_district_dict[bbox_name]
                
                # Ensure same CRS
                if slope_gdf.crs != district_gdf.crs:
                    district_gdf = district_gdf.to_crs(slope_gdf.crs)
                
                # Use bounds check ONLY (faster) instead of geometry operations
                slope_bounds = slope_gdf.total_bounds
                district_bounds = district_gdf.total_bounds
                
                # Simple bounds overlap check
                bounds_overlap = (slope_bounds[0] <= district_bounds[2] and 
                                 slope_bounds[2] >= district_bounds[0] and
                                 slope_bounds[1] <= district_bounds[3] and 
                                 slope_bounds[3] >= district_bounds[1])
                
                if bounds_overlap:
                    clipped = gpd.clip(slope_gdf, district_gdf)
                    
                    if len(clipped) > 0:
                        # Check if clipped data covers the entire district
                        clipped_union = clipped.unary_union
                        district_union = district_gdf.unary_union
                        
                        # Calculate coverage percentage
                        coverage_area = clipped_union.intersection(district_union).area
                        district_area = district_union.area
                        coverage_percent = (coverage_area / district_area) * 100 if district_area > 0 else 0
                        
                        # Only save shapefile if coverage is 95% or more (full coverage)
                        if coverage_percent >= 95:
                            # Save shapefile
                            clipped.to_file(output_path)
                            worker_completed_districts[bbox_name] = True
                            shapefile_count += 1
                            print(f"        ✓ SAVED: {output_file} ({len(clipped)} records, {coverage_percent:.1f}% coverage)", flush=True)
    except Exception as e:
        print(f"  ERROR processing {slope_name}: {e}", flush=True)
        
    return shapefile_count

if __name__ == '__main__':
    # ===== PATHS =====
    boundingbox_folder = r"d:\Slop_MP\Boundingbox"
    district_folder = r"d:\Slop_MP\District Boundary"
    slope_dir = r"d:\Slop_MP\Slop\dissolved_output"
    output_dir = r"d:\Slop_MP\clipped_output"

    os.makedirs(output_dir, exist_ok=True)
    
    print("Initializing...")
    bbox_names = [f.stem for f in Path(boundingbox_folder).glob("*.shp")]
    print(f"Found {len(bbox_names)} bounding boxes")
    
    district_files = list(Path(district_folder).glob("*.shp"))
    print(f"Found {len(district_files)} district files")
    
    manager = multiprocessing.Manager()
    shared_completed_districts = manager.dict()
    
    # Find already completed districts to avoid repeating them
    for out_file in Path(output_dir).glob("*.shp"):
        for bbox_name in sorted(bbox_names, key=len, reverse=True):
            if out_file.name.startswith(f"{bbox_name}_"):
                shared_completed_districts[bbox_name] = True
                break
                
    if shared_completed_districts:
        print(f"Found {len(shared_completed_districts)} previously completed districts. These will be skipped.\n")
        
    # Get all slope files
    slope_files = list(Path(slope_dir).glob("*.shp"))
    if not slope_files:
        print(f"ERROR: No slope files found")
        exit(1)

    # Sort by file size (smallest first)
    slope_files.sort(key=lambda x: x.stat().st_size)
    print(f"Found {len(slope_files)} slope files (sorted by size)\n")
    
    total_files = len(slope_files)
    pool_args = [(f, i, total_files, output_dir) for i, f in enumerate(slope_files)]
    
    print("Starting multiprocessing pool with 4 processes...")
    start_time = time.time()
    
    # Use 4 processes
    with multiprocessing.Pool(processes=4, initializer=init_worker, initargs=(boundingbox_folder, district_folder, shared_completed_districts)) as pool:
        results = pool.map(process_slope_file, pool_args)
        
    total_saved = sum(results)
    elapsed_time = time.time() - start_time
    print(f"\n✓ Done! Created {total_saved} output shapefiles in {output_dir}")
    print(f"Total processing time: {elapsed_time:.2f} seconds")
