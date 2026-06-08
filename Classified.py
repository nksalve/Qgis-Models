import geopandas as gpd
import os
from pathlib import Path

# ===== PATHS =====
input_dir = r"D:\Slop_MP\clipped_output"
output_dir = r"D:\Slop_MP\clipped_output\classified"

os.makedirs(output_dir, exist_ok=True)

# Define slope classes and ranges
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

def get_slope_range(slope_class):
    """Get slope percentage range"""
    ranges = {
        "Plain": "0-10",
        "Rolling": "10-25",
        "Mountainous": "25-60",
        "Steep": ">60"
    }
    return ranges.get(slope_class, "")

# Get all shapefiles
shapefiles = list(Path(input_dir).glob("*.shp"))

if not shapefiles:
    print(f"ERROR: No shapefiles found in {input_dir}")
    exit(1)

print(f"Found {len(shapefiles)} shapefiles\n")

# Process each shapefile
for idx, shp_file in enumerate(shapefiles):
    shp_name = shp_file.stem
    
    # Check if output file already exists
    output_file = f"{shp_name}_Classified.shp"
    output_path = os.path.join(output_dir, output_file)
    
    if os.path.exists(output_path):
        print(f"[{idx+1}/{len(shapefiles)}] {shp_name} - SKIPPED (already processed)")
        continue
    
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
            print(f"  WARNING: No slope value column found")
            print(f"  Available columns: {list(gdf.columns)}")
            continue
        
        print(f"  Using column: {dn_column}")
        
        # Add slope class column
        gdf['slope_class'] = gdf[dn_column].apply(get_slope_class)
        
        # Dissolve by slope class to merge adjacent features
        dissolved = gdf.dissolve(by='slope_class', aggfunc='first')
        
        # Add Class and Slope_Per columns
        dissolved['Class'] = dissolved.index
        dissolved['Slope_Per'] = dissolved['Class'].apply(get_slope_range)
        
        # Save single file with all classes
        output_file = f"{shp_name}_Classified.shp"
        output_path = os.path.join(output_dir, output_file)
        dissolved.to_file(output_path)
        
        print(f"  ✓ SAVED: {output_file}")
        print(f"    Plain (0-10): {len(dissolved[dissolved.index == 'Plain'])} features")
        print(f"    Rolling (10-25): {len(dissolved[dissolved.index == 'Rolling'])} features")
        print(f"    Mountainous (25-60): {len(dissolved[dissolved.index == 'Mountainous'])} features")
        print(f"    Steep (>60): {len(dissolved[dissolved.index == 'Steep'])} features")
        print()
        
    except Exception as e:
        print(f"  ERROR: {e}\n")

print(f"✓ Done! Classification complete in {output_dir}")
