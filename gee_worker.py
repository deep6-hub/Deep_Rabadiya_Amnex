import os
import shutil
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
def _configured_key_path():
    # Environment override wins; otherwise use a per-user selected key path.
    env_path = os.environ.get("GEE_SERVICE_ACCOUNT_KEY")
    if env_path:
        return Path(env_path).expanduser()
    config_path = BASE_DIR / "user_config.json"
    if config_path.is_file():
        try:
            import json
            configured = json.loads(config_path.read_text(encoding="utf-8")).get("credential_path")
            if configured:
                return Path(configured).expanduser()
        except Exception:
            pass
    return BASE_DIR / "credentials.json"

SERVICE_ACCOUNT_KEY = _configured_key_path()

def set_service_account_key(path):
    """Set the key path for this running app; caller persists the path separately."""
    global SERVICE_ACCOUNT_KEY
    SERVICE_ACCOUNT_KEY = Path(path).expanduser().resolve()
    return SERVICE_ACCOUNT_KEY

# Project ID is read dynamically from the currently selected service-account JSON.
# Do not hard-code one team's project: every team member can use their own key.
def _service_account_project():
    import json
    if not SERVICE_ACCOUNT_KEY.is_file():
        return ""
    with open(SERVICE_ACCOUNT_KEY, "r", encoding="utf-8") as f:
        return str(json.load(f).get("project_id", "")).strip()

GEE_PROJECT = _service_account_project()
TARGET_CRS = "EPSG:32642"
TARGET_SCALE = 10


def initialize_gee():
    """Initialize Earth Engine using a service-account JSON key (no browser login)."""
    import ee
    from google.oauth2 import service_account

    if not SERVICE_ACCOUNT_KEY.is_file():
        raise FileNotFoundError(
            "Service-account key not found. Place your replacement key at "
            f"'{SERVICE_ACCOUNT_KEY}' or set GEE_SERVICE_ACCOUNT_KEY to its path. "
            "Do not use the key from the previously shared ZIP; rotate it if it was real."
        )

    import json
    with open(SERVICE_ACCOUNT_KEY, "r", encoding="utf-8") as f:
        key_info = json.load(f)
    project_id = str(key_info.get("project_id", "")).strip()
    if not project_id:
        raise ValueError("The selected service-account JSON does not contain a project_id field.")
    # Keep the displayed/logged project synchronized with the selected key.
    global GEE_PROJECT
    GEE_PROJECT = project_id

    credentials = service_account.Credentials.from_service_account_file(
        str(SERVICE_ACCOUNT_KEY),
        scopes=[
            "https://www.googleapis.com/auth/earthengine",
            "https://www.googleapis.com/auth/cloud-platform",
        ],
    )
    ee.Initialize(credentials=credentials, project=project_id)


def _log(log, message, level=1):
    """Log every event with a local timestamp (24-hour clock)."""
    if log:
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        log(f"[{timestamp}] {message}", level)


def _timed_step(log, label, function, *args, **kwargs):
    """Run one processing step and log its start, end, duration, and failures."""
    started_at = time.perf_counter()
    _log(log, f"[STEP START] {label}")
    try:
        result = function(*args, **kwargs)
    except Exception as exc:
        elapsed = time.perf_counter() - started_at
        _log(log, f"[STEP FAILED] {label} | elapsed={elapsed:.2f} sec | {type(exc).__name__}: {exc}")
        raise
    elapsed = time.perf_counter() - started_at
    _log(log, f"[STEP END] {label} | elapsed={elapsed:.2f} sec ({elapsed/60:.2f} min)")
    return result


def _select_district(path, field, name):
    import geopandas as gpd

    path = str(path)
    shp = f"zip://{path}" if path.lower().endswith(".zip") else path
    gdf = gpd.read_file(shp)

    field_match = next((c for c in gdf.columns if c.lower() == field.lower()), None)
    if field_match is None:
        raise ValueError(
            f"District field '{field}' not found. Available fields: {list(gdf.columns)}"
        )

    vals = gdf[field_match].astype(str).str.strip().str.casefold()
    selected = gdf[vals == name.strip().casefold()].copy()
    if selected.empty:
        raise ValueError(f"District '{name}' not found in field '{field_match}'.")

    if selected.crs is None:
        raise ValueError("District shapefile has no CRS defined.")

    selected_wgs84 = selected.to_crs("EPSG:4326")
    selected_32642 = selected.to_crs(TARGET_CRS)

    return (
        selected_wgs84.geometry.union_all(),
        selected_32642.geometry.union_all(),
    )


def get_available_dates(
    district_path, field, district_name, year, start_month, end_month,
    polarization="VH", orbit="DESCENDING", log=None
):
    import ee

    # Date lookup is also timed; use console logging when called from the dates UI.
    if log is None:
        log = lambda message, level=1: print(message, flush=True)
    _timed_step(log, "Initialize Google Earth Engine using service account", initialize_gee)

    geom_wgs84, _ = _select_district(district_path, field, district_name)
    region = ee.Geometry(geom_wgs84.__geo_interface__)

    start_date = ee.Date.fromYMD(year, start_month, 1)
    finish_date = (
        ee.Date.fromYMD(year + 1, 1, 1)
        if end_month == 12
        else ee.Date.fromYMD(year, end_month + 1, 1)
    )

    collection = (
        ee.ImageCollection("COPERNICUS/S1_GRD")
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.eq("orbitProperties_pass", orbit))
        .filterDate(start_date, finish_date)
        .filterBounds(region)
        .select(polarization)
    )

    return (
        collection.aggregate_array("system:time_start")
        .map(lambda t: ee.Date(t).format("YYYY-MM-dd"))
        .distinct()
        .sort()
        .getInfo()
    )


def _get_collection(region, year, start_month, end_month, polarization, orbit):
    import ee

    start_date = ee.Date.fromYMD(year, start_month, 1)
    finish_date = (
        ee.Date.fromYMD(year + 1, 1, 1)
        if end_month == 12
        else ee.Date.fromYMD(year, end_month + 1, 1)
    )

    return (
        ee.ImageCollection("COPERNICUS/S1_GRD")
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.eq("orbitProperties_pass", orbit))
        .filterDate(start_date, finish_date)
        .filterBounds(region)
        .select(polarization)
    )



def _valid_raster(path):
    """Return True only for a readable, non-empty GeoTIFF."""
    try:
        import rasterio
        p = Path(path)
        if not p.exists() or p.stat().st_size < 100:
            return False
        with rasterio.open(p) as src:
            return src.width > 0 and src.height > 0 and src.count >= 1
    except Exception:
        return False


def _download_tile(image, bounds, destination, log, retries=3):
    """Download one Earth Engine GeoTIFF tile directly to local storage."""
    import requests
    import zipfile
    import tempfile
    import rasterio

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    xmin, ymin, xmax, ymax = bounds
    tile_region = __import__("ee").Geometry.Rectangle(
        [xmin, ymin, xmax, ymax],
        proj=__import__("ee").Projection(TARGET_CRS),
        geodesic=False,
    )
    url = image.getDownloadURL({
        "region": tile_region,
        "scale": TARGET_SCALE,
        "crs": TARGET_CRS,
        "format": "GEO_TIFF",
    })

    part_path = destination.with_suffix(destination.suffix + ".part")
    for attempt in range(1, retries + 1):
        try:
            with requests.get(url, stream=True, timeout=(30, 180)) as response:
                response.raise_for_status()
                with open(part_path, "wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            handle.write(chunk)

            if not part_path.exists() or part_path.stat().st_size < 100:
                raise IOError("Earth Engine returned an empty or very small tile.")

            # Earth Engine may return a ZIP archive for GeoTIFF downloads.
            if zipfile.is_zipfile(part_path):
                with zipfile.ZipFile(part_path) as archive:
                    tif_names = [
                        n for n in archive.namelist()
                        if n.lower().endswith((".tif", ".tiff"))
                    ]
                    if not tif_names:
                        raise IOError("Download archive contains no GeoTIFF.")
                    with archive.open(tif_names[0]) as src, open(destination, "wb") as dst:
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            dst.write(chunk)
                part_path.unlink(missing_ok=True)
            else:
                os.replace(part_path, destination)

            if not _valid_raster(destination):
                raise IOError("Downloaded tile is not a readable GeoTIFF.")

            _log(log, f"    Tile ready: {destination.name}", 20)
            return str(destination)

        except Exception as exc:
            part_path.unlink(missing_ok=True)
            if attempt >= retries:
                destination.unlink(missing_ok=True)
                raise RuntimeError(
                    f"Direct Earth Engine tile download failed ({destination.name}) "
                    f"after {retries} attempts: {exc}"
                ) from exc
            _log(
                log,
                f"    Tile {destination.name} attempt {attempt}/{retries} failed: "
                f"{exc}. Retrying...",
                18,
            )
            time.sleep(2 * attempt)


def _tile_bounds(projected_geometry, tile_pixels=2048):
    """Create a fixed, pixel-aligned grid in EPSG:32642 intersecting the district."""
    import math
    from shapely.geometry import box

    tile_size = tile_pixels * TARGET_SCALE
    minx, miny, maxx, maxy = projected_geometry.bounds
    x0 = math.floor(minx / tile_size) * tile_size
    y0 = math.floor(miny / tile_size) * tile_size
    x1 = math.ceil(maxx / tile_size) * tile_size
    y1 = math.ceil(maxy / tile_size) * tile_size

    tiles = []
    y = y0
    while y < y1:
        x = x0
        while x < x1:
            candidate = box(x, y, x + tile_size, y + tile_size)
            if projected_geometry.intersects(candidate):
                tiles.append((x, y, x + tile_size, y + tile_size))
            x += tile_size
        y += tile_size

    if not tiles:
        raise ValueError("Could not create download tiles for the selected district.")
    if len(tiles) > 5000:
        raise ValueError(
            f"District requires {len(tiles)} tiles, above the safety limit of 5000."
        )
    return tiles


def _merge_download_tiles(tile_paths, output_path):
    """Merge direct-download tiles into a single district GeoTIFF."""
    import rasterio
    from rasterio.merge import merge

    if not tile_paths:
        raise ValueError("No downloaded tiles available to merge.")

    sources = []
    try:
        sources = [rasterio.open(p) for p in tile_paths]
        for src in sources:
            if src.crs is None or src.crs.to_string() != TARGET_CRS:
                raise ValueError(f"Tile has unexpected CRS: {src.name} ({src.crs})")
            if src.count != 1:
                raise ValueError(f"Expected a single-band tile: {src.name}")

        mosaic, transform = merge(sources, nodata=0, dtype="float32")
        profile = sources[0].profile.copy()
        profile.update(
            driver="GTiff",
            height=mosaic.shape[1],
            width=mosaic.shape[2],
            count=1,
            dtype="float32",
            crs=TARGET_CRS,
            transform=transform,
            nodata=0.0,
            compress="LZW",
            tiled=True,
            BIGTIFF="IF_SAFER",
        )
    finally:
        for src in sources:
            src.close()

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(".tmp.tif")
    with rasterio.open(temp_path, "w", **profile) as dst:
        dst.write(mosaic[0].astype("float32"), 1)
    os.replace(temp_path, output_path)

    if not _valid_raster(output_path):
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f"Merged raster failed validation: {output_path}")

    return str(output_path)


def process(job, log):
    import ee
    import rasterio

    district_path = job["district_path"]
    field = job.get("district_field", "district")
    district_name = job["district_name"]
    mask_path = job["mask_path"]
    output_dir = Path(job["output_dir"])
    year = int(job["year"])
    start_month = int(job["start_month"])
    end_month = int(job["end_month"])
    polarization = job.get("polarization", "VH")
    orbit = job.get("orbit", "DESCENDING")
    selected_dates = sorted(set(job.get("selected_dates") or []))
    output_specs = job.get("selected_outputs") or [
        {"kind": "date", "label": d, "dates": [d]} for d in selected_dates
    ]
    output_specs = [x for x in output_specs if x.get("mode", "s1") == "s1"]

    if not selected_dates:
        raise ValueError("No dates selected.")
    if polarization not in ("VH", "VV"):
        raise ValueError("Polarization must be VH or VV.")
    if not (1 <= start_month <= 12 and 1 <= end_month <= 12):
        raise ValueError("Start/end month must be between 1 and 12.")

    _log(log, "Authenticating with Google Earth Engine...", 3)
    _timed_step(log, "Initialize Google Earth Engine using service account", initialize_gee)
    _log(log, f"Connected to GEE project: {GEE_PROJECT}", 5)

    _log(log, "Reading district boundary...", 7)
    geom_wgs84, geom_32642 = _timed_step(log, "Read and reproject district boundary", _select_district, district_path, field, district_name)
    _log(log, "District boundary loaded successfully.", 9)

    region = ee.Geometry(geom_wgs84.__geo_interface__)
    collection = _get_collection(
        region, year, start_month, end_month, polarization, orbit
    )

    available_dates = (
        collection.aggregate_array("system:time_start")
        .map(lambda t: ee.Date(t).format("YYYY-MM-dd"))
        .distinct()
        .sort()
        .getInfo()
    )
    invalid = [d for d in selected_dates if d not in set(available_dates)]
    if invalid:
        raise ValueError(
            "Selected dates are not available in the current query: "
            + ", ".join(invalid)
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "01_raw_vh"
    raw_dir.mkdir(exist_ok=True)
    tiles_root = output_dir / "_direct_download_tiles"
    tiles_root.mkdir(exist_ok=True)

    expected_raw_paths = []
    total_dates = len(output_specs)
    tile_bounds = _tile_bounds(geom_32642)

    _log(
        log,
        f"Direct download enabled: {len(tile_bounds)} local tile(s) per date "
        f"(tile size 2048 px, {TARGET_SCALE} m).",
        10,
    )

    for date_index, spec in enumerate(output_specs, start=1):
        date_started_at = time.perf_counter()
        output_dates = sorted(set(spec.get("dates") or []))
        label = str(spec.get("label") or output_dates[0]).strip()
        safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label).strip("_")[:100]
        date_str = output_dates[0] if len(output_dates) == 1 else f"MOSAIC_{safe_label}"
        _log(log, f"[OUTPUT START] {date_index}/{total_dates}: {label} | dates={output_dates}")
        file_name = f"{district_name}_{polarization}_{safe_label or date_str}.tif"
        raw_path = raw_dir / file_name
        expected_raw_paths.append(str(raw_path))

        if _valid_raster(raw_path):
            _log(
                log,
                f"[{date_index}/{total_dates}] Reusing existing local raster: {file_name}",
                13,
            )
            elapsed = time.perf_counter() - date_started_at
            _log(log, f"[DATE END] {date_str} | elapsed={elapsed:.2f} sec ({elapsed/60:.2f} min) | reused existing raster")
            continue
        elif raw_path.exists():
            raw_path.unlink(missing_ok=True)

        _log(
            log,
            f"[{date_index}/{total_dates}] Preparing Earth Engine image: {date_str}",
            12,
        )
        daily_collections = []
        for output_date in output_dates:
            daily_collections.append(collection.filter(
                ee.Filter.date(ee.Date(output_date), ee.Date(output_date).advance(1, "day"))
            ))
        image_collection = daily_collections[0]
        for daily in daily_collections[1:]:
            image_collection = image_collection.merge(daily)
        image = image_collection.sort("system:time_start").mosaic().clip(region).unmask(0).rename(polarization)

        date_tile_dir = tiles_root / f"{district_name}_{polarization}_{safe_label or date_str}"
        date_tile_dir.mkdir(parents=True, exist_ok=True)
        tile_paths = []

        for tile_index, bounds in enumerate(tile_bounds, start=1):
            tile_path = date_tile_dir / f"tile_{tile_index:04d}.tif"
            if _valid_raster(tile_path):
                tile_paths.append(str(tile_path))
                _log(log, f"  Reusing tile {tile_index}/{len(tile_bounds)}", 17)
                continue

            _log(
                log,
                f"  Downloading output {label}, tile {tile_index}/{len(tile_bounds)}...",
                15,
            )
            tile_paths.append(_timed_step(log, f"Download tile {tile_index}/{len(tile_bounds)} for {label}", _download_tile, image, bounds, tile_path, log))

        if len(tile_paths) != len(tile_bounds) or not all(
            _valid_raster(p) for p in tile_paths
        ):
            raise RuntimeError(
                f"Tile validation failed for output {label}; final raster was not created."
            )

        _log(log, f"  Merging {len(tile_paths)} tiles for {label}...", 21)
        _timed_step(log, f"Merge {len(tile_paths)} tiles for {label}", _merge_download_tiles, tile_paths, raw_path)
        _log(log, f"  Local GeoTIFF ready: {raw_path}", 22)
        elapsed = time.perf_counter() - date_started_at
        _log(log, f"[OUTPUT END] {label} | elapsed={elapsed:.2f} sec ({elapsed/60:.2f} min)")

    if len(expected_raw_paths) != len(output_specs):
        raise RuntimeError(
            f"Download validation failed: selected {len(output_specs)} outputs "
            f"but prepared {len(expected_raw_paths)} rasters."
        )

    stack_path = output_dir / (
        f"{district_name}_{polarization}_STACK_{year}_"
        f"{start_month:02d}-{end_month:02d}.tif"
    )
    raw_stack_path = output_dir / "_direct_download_work" / (
        f"{district_name}_{polarization}_RAW_STACK_{year}_"
        f"{start_month:02d}-{end_month:02d}.tif"
    )
    raw_stack_path.parent.mkdir(parents=True, exist_ok=True)

    _log(
        log,
        f"Creating RAW multi-band stack from {len(expected_raw_paths)} selected output raster(s)...",
        23,
    )
    _timed_step(log, "Create raw multiband stack", _stack_images, expected_raw_paths, str(raw_stack_path))

    with rasterio.open(raw_stack_path) as raw_src:
        _log(log, f"RAW stack band count: {raw_src.count}", 24)
        for band_no in range(1, raw_src.count + 1):
            _log(log, f"  Raw Band {band_no}: {raw_src.descriptions[band_no - 1]}", 24)
        if raw_src.count != len(output_specs):
            raise RuntimeError(
                f"Raw stack validation failed: expected {len(output_specs)} "
                f"bands but created {raw_src.count}."
            )

    _log(log, "Preparing agriculture mask on the stack grid...", 25)
    aligned_mask = output_dir / "_direct_download_work" / "agriculture_mask_aligned_to_stack.tif"
    _timed_step(log, "Align agriculture mask to stack grid (nearest-neighbour)", _prepare_mask, mask_path, str(raw_stack_path), str(aligned_mask))

    _log(
        log,
        "Applying agriculture mask after stacking all bands and cropping to mask extent...",
        26,
    )
    _timed_step(
        log, "Apply agriculture mask and crop to mask extent", _apply_agri_mask_to_stack,
        str(raw_stack_path), str(aligned_mask), str(stack_path), original_mask_path=str(mask_path)
    )

    with rasterio.open(stack_path) as final_src:
        _log(
            log,
            f"FINAL masked raster size: {final_src.width} x {final_src.height}",
            27,
        )
        _log(log, f"FINAL masked stack band count: {final_src.count}", 27)
        _log(log, f"FINAL NoData value: {final_src.nodata}", 27)
        for band_no in range(1, final_src.count + 1):
            _log(log, f"  Band {band_no}: {final_src.descriptions[band_no - 1]}", 27)
        if final_src.count != len(output_specs):
            raise RuntimeError(
                f"Final stack validation failed: selected {len(output_specs)} "
                f"outputs but final stack contains {final_src.count}."
            )
        if final_src.nodata != 0.0:
            raise RuntimeError(
                f"Final stack NoData validation failed: expected 0.0, got {final_src.nodata}."
            )

    _log(log, f"FINAL STACK: {stack_path}", 28)
    _log(log, f"Final CRS: {TARGET_CRS}", 29)

    # Delete temporary download tiles only after the final masked stack is validated.
    if tiles_root.exists():
        shutil.rmtree(tiles_root)
        _log(log, f"Removed temporary Sentinel-1 download tiles: {tiles_root.name}", 30)
    _log(log, "Direct Earth Engine download workflow completed successfully.", 31)


def _prepare_mask(mask_path, reference_path, output_mask):
    import numpy as np
    import rasterio
    from rasterio.warp import reproject, Resampling

    with rasterio.open(reference_path) as ref:
        ref_crs = ref.crs
        ref_transform = ref.transform
        ref_width = ref.width
        ref_height = ref.height
        profile = ref.profile.copy()

    if ref_crs.to_string() != TARGET_CRS:
        raise ValueError(f"Reference raster is {ref_crs}, expected {TARGET_CRS}.")

    with rasterio.open(mask_path) as src:
        if src.crs is None:
            raise ValueError("Agriculture mask has no CRS defined.")

        destination = np.zeros((ref_height, ref_width), dtype="uint8")
        reproject(
            source=rasterio.band(src, 1),
            destination=destination,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=ref_transform,
            dst_crs=ref_crs,
            resampling=Resampling.nearest,
            dst_nodata=0,
        )

    profile.update(
        driver="GTiff",
        count=1,
        dtype="uint8",
        nodata=0,
        compress="LZW",
        crs=TARGET_CRS,
    )

    with rasterio.open(output_mask, "w", **profile) as dst:
        dst.write(destination, 1)


def _stack_images(image_paths, output_path):
    """Create a true multi-band GeoTIFF from one-band VH rasters."""
    import rasterio

    image_paths = [str(p) for p in image_paths]
    if not image_paths:
        raise ValueError("No VH images available for stacking.")

    with rasterio.open(image_paths[0]) as ref:
        width, height = ref.width, ref.height
        transform, crs = ref.transform, ref.crs

    if crs.to_string() != TARGET_CRS:
        raise ValueError(f"Stack CRS is {crs}, expected {TARGET_CRS}.")

    expected_band_count = len(image_paths)
    profile = {
        "driver": "GTiff",
        "width": width,
        "height": height,
        "count": expected_band_count,
        "dtype": "float32",
        "crs": TARGET_CRS,
        "transform": transform,
        "nodata": 0.0,
        "compress": "LZW",
        "tiled": True,
        "BIGTIFF": "IF_SAFER",
        "interleave": "band",
    }

    with rasterio.open(output_path, "w", **profile) as dst:
        for band_no, path in enumerate(image_paths, start=1):
            with rasterio.open(path) as src:
                if (
                    src.count < 1
                    or src.width != width
                    or src.height != height
                    or src.transform != transform
                    or src.crs != crs
                ):
                    raise ValueError(f"Grid mismatch while stacking: {path}")

                data = src.read(1).astype("float32")
                dst.write(data, band_no)
                dst.set_band_description(band_no, Path(path).stem)

    with rasterio.open(output_path) as check:
        if check.count != expected_band_count:
            raise RuntimeError(
                f"Final stack validation failed: expected {expected_band_count} "
                f"bands but created {check.count}."
            )


def _apply_agri_mask_to_stack(stack_path, mask_path, output_path, original_mask_path=None):
    """
    Apply the agriculture mask AFTER the complete multi-band stack exists.

    ERDAS-style extent handling:
      - The crop window is derived from the ORIGINAL agriculture-mask raster
        extent, not from the bounding box of non-zero agriculture pixels.
      - The mask is already aligned to the complete stack grid by
        _prepare_mask().
      - Pixels outside agriculture inside the mask extent are written as 0.

    This preserves the mask raster's spatial extent. For the Anand dataset
    this is expected to reproduce the ERDAS 9010 x 6132 output instead of
    shrinking to the non-zero-pixel bounding box.
    """
    import numpy as np
    import rasterio
    from rasterio.windows import from_bounds, Window, transform as window_transform
    from rasterio.warp import transform_bounds

    with rasterio.open(stack_path) as stack, rasterio.open(mask_path) as mask:
        expected_band_count = stack.count

        if stack.crs.to_string() != TARGET_CRS:
            raise ValueError(f"Stack CRS is {stack.crs}, expected {TARGET_CRS}.")
        if mask.crs.to_string() != TARGET_CRS:
            raise ValueError(f"Mask CRS is {mask.crs}, expected {TARGET_CRS}.")
        if (
            stack.width != mask.width
            or stack.height != mask.height
            or stack.transform != mask.transform
        ):
            raise ValueError("Aligned agriculture mask does not match stack grid.")

        # IMPORTANT: crop from the ORIGINAL agriculture-mask FILE extent,
        # not from the bounding box of non-zero pixels and not from the
        # temporary full-stack aligned mask. The temporary aligned mask is
        # intentionally 9010 x 7143, so using its bounds would never crop.
        # Passing original_mask_path preserves the true mask extent used by
        # the user/ERDAS (for Anand this is 9010 x 6132).
        source_mask_path = original_mask_path or mask_path
        with rasterio.open(source_mask_path) as original_mask:
            mask_bounds = original_mask.bounds
            mask_crs = original_mask.crs

        if mask_crs != stack.crs:
            mask_bounds = transform_bounds(
                mask_crs, stack.crs,
                *mask_bounds,
                densify_pts=21,
            )

        raw_window = from_bounds(
            mask_bounds.left,
            mask_bounds.bottom,
            mask_bounds.right,
            mask_bounds.top,
            transform=stack.transform,
        )

        # Snap the window to the stack's pixel grid.
        col_min = max(0, int(round(raw_window.col_off)))
        row_min = max(0, int(round(raw_window.row_off)))
        col_max = min(stack.width, int(round(raw_window.col_off + raw_window.width)))
        row_max = min(stack.height, int(round(raw_window.row_off + raw_window.height)))

        if col_max <= col_min or row_max <= row_min:
            raise ValueError("Agriculture mask extent does not overlap the VH stack.")

        window = Window(
            col_off=col_min,
            row_off=row_min,
            width=col_max - col_min,
            height=row_max - row_min,
        )

        cropped_width = int(window.width)
        cropped_height = int(window.height)
        cropped_transform = window_transform(window, stack.transform)

        profile = stack.profile.copy()
        profile.update(
            driver="GTiff",
            width=cropped_width,
            height=cropped_height,
            count=stack.count,
            dtype="float32",
            nodata=0.0,
            transform=cropped_transform,
            compress="LZW",
            tiled=True,
            BIGTIFF="IF_SAFER",
            interleave="band",
            crs=TARGET_CRS,
        )

        agriculture_cropped = mask.read(1, window=window) != 0

        with rasterio.open(output_path, "w", **profile) as dst:
            for band_no in range(1, stack.count + 1):
                data = stack.read(band_no, window=window).astype("float32")
                data[~agriculture_cropped] = 0.0
                dst.write(data, band_no)

                description = stack.descriptions[band_no - 1]
                if description:
                    dst.set_band_description(band_no, description)

    with rasterio.open(output_path) as check:
        if check.count != expected_band_count:
            raise RuntimeError(
                f"Agriculture-masked stack validation failed: expected "
                f"{expected_band_count} bands but created {check.count}."
            )
        if check.nodata != 0.0:
            raise RuntimeError(
                f"Agriculture-masked stack NoData is {check.nodata}, expected 0.0."
            )
        if check.width != cropped_width or check.height != cropped_height:
            raise RuntimeError(
                f"Agriculture crop validation failed: expected "
                f"{cropped_width} x {cropped_height}, got "
                f"{check.width} x {check.height}."
            )

