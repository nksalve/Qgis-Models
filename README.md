# Qgis-Models
Different Model For Gis Work Flows

**master_process.py**
End-to-end slope analysis pipeline for Madhya Pradesh district data. Converts raster .tif files to vector polygons using rasterio, dissolves them by DN value, clips each dissolved layer to district boundaries with ≥95% coverage verification, classifies slopes into Plain / Rolling / Mountainous / Steep, and saves one shapefile per district with class and percentage range attributes. Skips already-completed outputs automatically.
Topics: rasterio, geopandas, raster-to-vector, dissolve, clip, classify, slope

**raster_to_vector.py**
Standalone version of the raster-to-classified-vector pipeline. Reads slope rasters, dissolves polygons by DN, clips to district extents with bounding box pre-filtering, classifies into four slope categories, and dissolves the final output by class and percentage range. Outputs one shapefile per district–raster combination.
Topics: rasterio, geopandas, raster-to-vector, classify, district-clip


**Dissolve_shp.py**
Parallel batch dissolve for multiple GeoPackage files with live per-file tqdm progress bars. Uses ProcessPoolExecutor and a shared Manager().Queue() to report six granular steps (read → validate → fix geometries → drop nulls → dissolve → write) across all workers simultaneously. Output format is Shapefile via pyogrio.
Topics: geopandas, pyogrio, multiprocessing, tqdm, gpkg, batch

**Dissolve_shp - Copy.py**
Simplified parallel batch dissolve without progress bars. Same ProcessPoolExecutor approach as Dissolve_shp.py but prints a single status line per completed file — lighter output suited for logging or headless environments. Fixes invalid geometries with buffer(0) before dissolving.
Topics: geopandas, pyogrio, multiprocessing, gpkg, batch

**clip.py**
Multiprocessing clip script that matches dissolved slope shapefiles to district boundaries. Worker processes pre-load all district geometries at startup to avoid redundant I/O. Uses a fast bounds-overlap check before running gpd.clip, then verifies coverage area (≥95% threshold). Tracks completed districts in a shared Manager().dict() across processes to prevent duplicate outputs.
Topics: geopandas, multiprocessing, clip, coverage-check, district

**Classified.py**
Classifies existing clipped shapefiles into four slope categories (Plain 0–10%, Rolling 10–25%, Mountainous 25–60%, Steep >60%) and dissolves all classes into a single output shapefile per input. Adds Class and Slope_Per attribute columns. Validates existing outputs and reprocesses empty files automatically.
Topics: geopandas, classify, dissolve, slope, shapefile

**dissolve_by_class.py**
Variant of slope classification that saves each slope class as a separate shapefile (e.g. DistrictName_Plain.shp, DistrictName_Rolling.shp). Useful when downstream workflows need per-class layers. Skips already-processed outputs and reprocesses files with empty or invalid geometries.
Topics: geopandas, classify, per-class-output, slope

**convert_percentage_to_degree.py**
Converts slope percent values to degrees for all shapefiles in a folder using the formula atan(percent / 100) × (180 / π). Adds a Slope_Deg column while preserving the original percent column. Auto-detects the slope column from a priority list (DN, Value, SLOPE, etc.) and writes converted files to a degree_output subfolder.
Topics: geopandas, math, slope-degrees, unit-conversion
