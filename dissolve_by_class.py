import geopandas as gpd
import os
from pathlib import Path

# ===== PATHS =====
input_dir = r"D:\Slop_MP\clipped_output"
output_dir = r"D:\Slop_MP\clipped_output\classified"

os.makedirs(output_dir, exist_ok=True)

# Define slope classes
def get_slope_class(dn_value):
    """Classify slope based on DN value"""
    if dn_value < 10:
        return "Plain"
    elif dn_value < 25:
        return "Rolling"
    elif dn_value < 60:
        return "Mountainous"
    else:
        return "Steep"

# Get all shapefiles
shapefiles = list(Path(input_dir).glob("*.shp"))

if not shapefiles:
    print(f"ERROR: No shapefiles found in {input_dir}")
    exit(1)

print(f"Found {len(shapefiles)} shapefiles\n")

# Process each shapefile
for idx, shp_file in enumerate(shapefiles):
    shp_name = shp_file.stem
    print(f"[{idx+1}/{len(shapefiles)}] Processing {shp_name}...")
    
    try:
        # Read shapefile
        gdf = gpd.read_file(str(shp_file))
        print(f"  Loaded: {len(gdf)} records")
        
        # Check if DN/Value column exists
        dn_column = None
        for col in ['DN', 'Value', 'value', 'dn', 'SLOPE']:
            if col in gdf.columns:
                dn_column = col
                break
        
        if dn_column is None:
            print(f"  WARNING: No slope value column found (tried DN, Value, value, dn, SLOPE)")
            print(f"  Available columns: {list(gdf.columns)}")
            continue
        
        print(f"  Using column: {dn_column}")
        
        # Add slope class column
        gdf['slope_class'] = gdf[dn_column].apply(get_slope_class)
        
        # Dissolve by slope class
        dissolved = gdf.dissolve(by='slope_class', aggfunc='first')
        
        # Save each class separately
        for slope_class in ['Plain', 'Rolling', 'Mountainous', 'Steep']:
            class_data = gdf[gdf['slope_class'] == slope_class]
            
            if len(class_data) > 0:
                # Dissolve geometries
                class_dissolved = class_data.dissolve(by='slope_class')
                
                # Save to file
                output_file = f"{shp_name}_{slope_class}.shp"
                output_path = os.path.join(output_dir, output_file)
                class_dissolved.to_file(output_path)
                
                print(f"    ✓ {slope_class}: {len(class_data)} records → {output_file}")
        
        print()
        
    except Exception as e:
        print(f"  ERROR: {e}\n")

print(f"✓ Done! Classification complete in {output_dir}")
