from pathlib import Path
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import BoundedSemaphore
import time
import numpy as np
import rasterio
from rasterio.merge import merge
from rasterio.warp import reproject, Resampling
import ee
from gee_worker import initialize_gee, _select_district, _tile_bounds, _download_tile, _valid_raster, _log, _timed_step, TARGET_CRS, TARGET_SCALE

S2_COLLECTION = 'COPERNICUS/S2_HARMONIZED'
VALID_BANDS = {'B2','B3','B4','B8','B11','B12'}
VALID_INDICES = {'NDVI','LSWI','SAVI'}
S2_TILE_PIXELS = 2048  # one band per request keeps payload below the direct-download limit
S2_DOWNLOAD_WORKERS = 4  # tile workers within each band
S2_BAND_WORKERS = 2      # bands downloaded simultaneously
S2_GLOBAL_DOWNLOAD_LIMIT = 6  # total active Earth Engine tile downloads across all bands
_GLOBAL_DOWNLOAD_SEMAPHORE = BoundedSemaphore(S2_GLOBAL_DOWNLOAD_LIMIT)

def _date_window(year, start_month, end_month):
    start = ee.Date.fromYMD(int(year), int(start_month), 1)
    end = ee.Date.fromYMD(int(year)+1, 1, 1) if int(end_month)==12 else ee.Date.fromYMD(int(year), int(end_month)+1, 1)
    return start, end

def _collection(region, year, start_month, end_month):
    start, end = _date_window(year, start_month, end_month)
    return (ee.ImageCollection(S2_COLLECTION).filterDate(start, end)
            .filterBounds(region).filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 100)))

def get_s2_dates(district_path, field, district_name, year, start_month, end_month):
    initialize_gee()
    geom_wgs84, _ = _select_district(district_path, field, district_name)
    region = ee.Geometry(geom_wgs84.__geo_interface__)
    coll = _collection(region, year, start_month, end_month)
    return (ee.List(coll.aggregate_array('system:time_start'))
            .map(lambda t: ee.Date(t).format('YYYY-MM-dd')).distinct().sort().getInfo())

def _merge_single_band_tiles(tile_paths, output_path):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    srcs = [rasterio.open(p) for p in tile_paths]
    try:
        mosaic, transform = merge(srcs, nodata=0, dtype='float32')
        profile = srcs[0].profile.copy()
        profile.update(driver='GTiff', height=mosaic.shape[1], width=mosaic.shape[2], count=1,
                       dtype='float32', crs=TARGET_CRS, transform=transform, nodata=0,
                       compress='LZW', tiled=True, BIGTIFF='IF_SAFER')
    finally:
        for src in srcs:
            src.close()
    temp = output_path.with_suffix('.tmp.tif')
    with rasterio.open(temp, 'w', **profile) as dst:
        dst.write(mosaic[0].astype('float32'), 1)
    temp.replace(output_path)
    if not _valid_raster(output_path):
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f'Band mosaic failed validation: {output_path}')
    return str(output_path)

def _download_band_for_date(coll, date, band, region, tile_bounds, date_tile_root, band_raster_path, log):
    band_raster_path = Path(band_raster_path)
    if _valid_raster(band_raster_path):
        _log(log, f'  Reusing completed band raster: {band_raster_path.name}')
        return str(band_raster_path)
    dates = date if isinstance(date, (list, tuple)) else [date]
    daily_collections = []
    for day in sorted(set(dates)):
        start = ee.Date(day)
        daily_collections.append(coll.filterDate(start, start.advance(1, 'day')).select([band]))
    image_collection = daily_collections[0]
    for daily in daily_collections[1:]:
        image_collection = image_collection.merge(daily)
    # Native GEE mosaic across the selected acquisition dates; later times win overlaps.
    image = (image_collection.sort('system:time_start').mosaic()
             .clip(region).unmask(0).toFloat())
    band_tile_dir = Path(date_tile_root) / band
    band_tile_dir.mkdir(parents=True, exist_ok=True)
    tile_paths = [None] * len(tile_bounds)

    def download_one(item):
        idx, bounds = item
        tile_path = band_tile_dir / f'tile_{idx+1:04d}.tif'
        if _valid_raster(tile_path):
            return idx, str(tile_path), True
        # Each band keeps its own 4-worker pool, while this shared semaphore
        # caps active Earth Engine requests across all bands at a global limit.
        with _GLOBAL_DOWNLOAD_SEMAPHORE:
            _timed_step(log, f'Sentinel-2 {date} band {band} tile {idx+1}/{len(tile_bounds)}',
                        _download_tile, image, bounds, tile_path, log)
        return idx, str(tile_path), False

    _log(log, f'  Band {band}: downloading {len(tile_bounds)} tile(s) with {S2_DOWNLOAD_WORKERS} workers.')
    with ThreadPoolExecutor(max_workers=S2_DOWNLOAD_WORKERS) as pool:
        futures = [pool.submit(download_one, item) for item in enumerate(tile_bounds)]
        for future in as_completed(futures):
            idx, path, reused = future.result()
            tile_paths[idx] = path
    if not all(tile_paths) or not all(_valid_raster(p) for p in tile_paths):
        raise RuntimeError(f'One or more tiles failed for {date} band {band}.')
    _timed_step(log, f'Merge Sentinel-2 {date} band {band}', _merge_single_band_tiles,
                tile_paths, band_raster_path)
    return str(band_raster_path)

def _safe_ratio(numerator, denominator):
    out = np.zeros_like(numerator, dtype=np.float32)
    np.divide(numerator, denominator, out=out, where=np.abs(denominator) > 1e-10)
    return out

def _stack_s2_outputs(image_paths, labels, output_path):
    """Stack every band from each selected output in the requested output order."""
    output_path = Path(output_path)
    if not image_paths:
        raise ValueError('No Sentinel-2 output rasters available for stacking.')
    with rasterio.open(image_paths[0]) as ref:
        width, height = ref.width, ref.height
        transform, crs = ref.transform, ref.crs
        dtype_profile = ref.profile.copy()
    if crs.to_string() != TARGET_CRS:
        raise ValueError(f'Sentinel-2 stack CRS is {crs}, expected {TARGET_CRS}.')

    total_bands = 0
    for path in image_paths:
        with rasterio.open(path) as src:
            if (src.width != width or src.height != height or
                    src.transform != transform or src.crs != crs):
                raise ValueError(f'Grid mismatch while stacking Sentinel-2 output: {path}')
            total_bands += src.count
    profile = dtype_profile.copy()
    profile.update(driver='GTiff', width=width, height=height, count=total_bands,
                   dtype='float32', crs=TARGET_CRS, transform=transform, nodata=0,
                   compress='LZW', tiled=True, BIGTIFF='IF_SAFER', interleave='band')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp = output_path.with_name(output_path.stem + '.tmp.tif')
    band_no = 1
    with rasterio.open(temp, 'w', **profile) as dst:
        for path, label in zip(image_paths, labels):
            with rasterio.open(path) as src:
                for source_band in range(1, src.count + 1):
                    dst.write(src.read(source_band).astype(np.float32), band_no)
                    description = src.descriptions[source_band - 1] or f'Band{source_band}'
                    dst.set_band_description(band_no, f'{label}_{description}')
                    band_no += 1
    temp.replace(output_path)
    if not _valid_raster(output_path):
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f'Sentinel-2 final stack failed validation: {output_path}')
    with rasterio.open(output_path) as check:
        if check.count != total_bands or check.crs.to_string() != TARGET_CRS:
            raise RuntimeError('Sentinel-2 final stack validation failed (band count or CRS).')
    return str(output_path)


def process_s2(job, log):
    bands = list(dict.fromkeys(job.get('selected_bands') or []))
    indices = list(dict.fromkeys(job.get('selected_indices') or []))
    if any(b not in VALID_BANDS for b in bands):
        raise ValueError('Invalid Sentinel-2 band selected.')
    if any(i not in VALID_INDICES for i in indices):
        raise ValueError('Invalid index selected.')
    if not bands and not indices:
        raise ValueError('Select at least one Sentinel-2 band or index.')

    # Add only the spectral dependencies required to calculate requested indices.
    source_bands = list(bands)
    if any(i in indices for i in ('NDVI', 'SAVI')):
        source_bands.extend(['B8', 'B4'])
    if 'LSWI' in indices:
        source_bands.extend(['B8', 'B11'])
    source_bands = list(dict.fromkeys(source_bands))

    initialize_gee()
    _log(log, 'Connected to Google Earth Engine for Sentinel-2.', 5)
    geom_wgs84, geom_32642 = _timed_step(
        log, 'Read and reproject district boundary', _select_district,
        job['district_path'], job.get('district_field', 'district'), job['district_name'])
    region = ee.Geometry(geom_wgs84.__geo_interface__)
    coll = _collection(region, job['year'], job['start_month'], job['end_month'])
    available = (ee.List(coll.aggregate_array('system:time_start'))
                 .map(lambda t: ee.Date(t).format('YYYY-MM-dd')).distinct().sort().getInfo())
    dates = sorted(set(job.get('selected_dates') or []))
    output_specs = job.get('selected_outputs') or [
        {'kind': 'date', 'label': d, 'dates': [d], 'mode': 's2'} for d in dates
    ]
    output_specs = [x for x in output_specs if x.get('mode', 's2') == 's2']
    if not output_specs:
        raise ValueError('No Sentinel-2 outputs selected.')
    invalid = [d for d in dates if d not in set(available)]
    if invalid:
        raise ValueError('Selected Sentinel-2 dates are no longer available: ' + ', '.join(invalid))

    outdir = Path(job['output_dir'])
    outdir.mkdir(parents=True, exist_ok=True)
    rawdir = outdir / 'Sentinel2_Optical'
    rawdir.mkdir(exist_ok=True)
    band_root = outdir / '_s2_band_tiles'
    band_root.mkdir(exist_ok=True)
    band_raster_root = outdir / '_s2_band_rasters'
    band_raster_root.mkdir(exist_ok=True)
    tile_bounds = _tile_bounds(geom_32642, tile_pixels=S2_TILE_PIXELS)
    stack_inputs = []
    stack_labels = []
    _log(log, f'Sentinel-2 output CRS: {TARGET_CRS}; resolution: {TARGET_SCALE} m; '
             f'tile size: {S2_TILE_PIXELS} px; tile workers/band: {S2_DOWNLOAD_WORKERS}; '
             f'parallel bands: {S2_BAND_WORKERS}; global active-download limit: {S2_GLOBAL_DOWNLOAD_LIMIT}; '
             f'output bands: {bands or "none"}; locally calculated indices: {indices or "none"}; '
             f'source bands needed: {source_bands}.')

    for date_i, spec in enumerate(output_specs, 1):
        output_dates = sorted(set(spec.get('dates') or []))
        label = str(spec.get('label') or output_dates[0]).strip()
        safe_label = ''.join(c if c.isalnum() or c in '-_' else '_' for c in label).strip('_')[:100]
        date = output_dates[0] if len(output_dates) == 1 else safe_label
        _log(log, f'[S2 OUTPUT START] {date_i}/{len(output_specs)}: {label} | dates={output_dates}')
        date_key = safe_label.replace('-', '')
        date_tile_root = band_root / date_key
        date_raster_root = band_raster_root / date_key
        date_raster_root.mkdir(parents=True, exist_ok=True)
        band_paths = {}

        # Download different source bands concurrently. Each band still has its own
        # 4 tile workers, but the shared semaphore above caps all active tile requests.
        def download_band(item):
            band_i, band = item
            _log(log, f'[S2 BAND QUEUED] {date}: {band_i}/{len(source_bands)} - {band}')
            band_path = date_raster_root / f'{band}.tif'
            result = _download_band_for_date(
                coll, output_dates, band, region, tile_bounds, date_tile_root, band_path, log)
            return band, result

        with ThreadPoolExecutor(max_workers=min(S2_BAND_WORKERS, len(source_bands))) as band_pool:
            futures = [band_pool.submit(download_band, item)
                       for item in enumerate(source_bands, 1)]
            for future in as_completed(futures):
                band, path = future.result()
                band_paths[band] = path

        dstpath = rawdir / f"{job['district_name']}_S2_{safe_label}.tif"
        output_names = bands + indices
        with rasterio.open(band_paths[source_bands[0]]) as ref:
            profile = ref.profile.copy()
            height, width = ref.height, ref.width
            transform, crs = ref.transform, ref.crs

        # Read source bands and calculate indices locally; no index bands are downloaded from EE.
        arrays = {}
        for band, path in band_paths.items():
            with rasterio.open(path) as src:
                if src.width != width or src.height != height or src.transform != transform or src.crs != crs:
                    raise RuntimeError(f'Grid mismatch for source band {band} on {date}.')
                arrays[band] = src.read(1).astype(np.float32)

        output_arrays = [arrays[b] for b in bands]
        for index_name in indices:
            if index_name == 'NDVI':
                nir, red = arrays['B8'], arrays['B4']
                output_arrays.append(_safe_ratio(nir - red, nir + red))
            elif index_name == 'LSWI':
                nir, swir = arrays['B8'], arrays['B11']
                output_arrays.append(_safe_ratio(nir - swir, nir + swir))
            elif index_name == 'SAVI':
                nir, red = arrays['B8'] / 10000.0, arrays['B4'] / 10000.0
                output_arrays.append(_safe_ratio(1.5 * (nir - red), nir + red + 0.5))

        profile.update(driver='GTiff', height=height, width=width, count=len(output_arrays),
                       dtype='float32', crs=TARGET_CRS, transform=transform, nodata=0,
                       compress='LZW', tiled=True, BIGTIFF='IF_SAFER')
        temp = dstpath.with_suffix('.tmp.tif')
        with rasterio.open(temp, 'w', **profile) as dst:
            for bi, (name, arr) in enumerate(zip(output_names, output_arrays), 1):
                dst.write(arr.astype(np.float32), bi)
                dst.set_band_description(bi, name)
        temp.replace(dstpath)

        # Apply agriculture mask on final output grid using nearest-neighbour resampling.
        mask_path = job.get('mask_path')
        if mask_path and Path(mask_path).is_file():
            masked_path = dstpath.with_name(dstpath.stem + '_agri_masked.tif')
            with rasterio.open(dstpath) as src, rasterio.open(mask_path) as mask:
                mask_arr = np.zeros((src.height, src.width), dtype='uint8')
                reproject(source=rasterio.band(mask, 1), destination=mask_arr,
                          src_transform=mask.transform, src_crs=mask.crs,
                          dst_transform=src.transform, dst_crs=src.crs,
                          resampling=Resampling.nearest)
                prof = src.profile.copy()
                with rasterio.open(masked_path, 'w', **prof) as dst:
                    for bi in range(1, src.count + 1):
                        arr = src.read(bi)
                        arr[mask_arr == 0] = 0
                        dst.write(arr, bi)
                        desc = src.descriptions[bi-1]
                        if desc:
                            dst.set_band_description(bi, desc)
            _log(log, f'Wrote masked output: {masked_path.name}')
            stack_source = masked_path
        else:
            stack_source = dstpath
        stack_inputs.append(str(stack_source))
        stack_labels.append(safe_label)
        _log(log, f'[S2 OUTPUT END] {label}: {dstpath.name}')

    # The output_specs order is the numeric order chosen in the UI.
    stack_path = rawdir / f"{job['district_name']}_S2_STACK_{job['year']}_{job['start_month']:02d}-{job['end_month']:02d}.tif"
    _log(log, f'Creating final Sentinel-2 multiband stack from {len(stack_inputs)} selected outputs in UI order...')
    _timed_step(log, 'Create final Sentinel-2 ordered multiband stack', _stack_s2_outputs,
                stack_inputs, stack_labels, stack_path)
    with rasterio.open(stack_path) as final_stack:
        _log(log, f'FINAL S2 STACK: {stack_path}')
        _log(log, f'Final S2 stack bands: {final_stack.count}; grid: {final_stack.width} x {final_stack.height}; CRS: {final_stack.crs}; resolution: {final_stack.res}')

    # Remove temporary tile caches only after the final stack has been written and validated.
    for temporary_dir in (band_root, band_raster_root):
        if temporary_dir.exists():
            shutil.rmtree(temporary_dir)
            _log(log, f'Removed temporary Sentinel-2 tiles/intermediate band files: {temporary_dir.name}')
    _log(log, f'Sentinel-2 workflow complete. Final stack: {stack_path}')
