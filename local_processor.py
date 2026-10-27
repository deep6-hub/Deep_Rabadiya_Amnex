"""Local-only raster processing utilities (no Earth Engine dependency)."""
from pathlib import Path
import os
import shutil
import tempfile
import time
from datetime import datetime

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.merge import merge
from rasterio.vrt import WarpedVRT
from rasterio.warp import reproject, transform_bounds
from rasterio.transform import from_origin

TARGET_CRS = "EPSG:32642"
TARGET_RESOLUTION = 10.0


def _log(log, message):
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    log(f"[{stamp}] {message}")


def list_geotiffs(folder, recursive=False):
    root = Path(folder).expanduser()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Input folder does not exist or is not a folder: {root}")
    iterator = root.rglob("*") if recursive else root.iterdir()
    files = sorted((p for p in iterator if p.is_file() and p.suffix.lower() in (".tif", ".tiff") and not p.name.lower().endswith(".aux.xml")), key=lambda p: p.name.lower())
    return [{"name": p.name, "path": str(p.resolve()), "size_mb": round(p.stat().st_size / (1024 * 1024), 2)} for p in files]


def _target_profile(src, count=None, dtype=None, nodata=None):
    profile = src.profile.copy()
    profile.update(
        driver="GTiff", crs=TARGET_CRS,
        transform=None, width=None, height=None,
        tiled=True, compress="deflate", BIGTIFF="IF_SAFER",
    )
    return profile


def _validate_inputs(paths):
    if not paths:
        raise ValueError("Select at least one input GeoTIFF.")
    result = []
    for raw in paths:
        p = Path(raw).expanduser().resolve()
        if not p.is_file() or p.suffix.lower() not in (".tif", ".tiff"):
            raise ValueError(f"Input GeoTIFF not found: {p}")
        with rasterio.open(p) as ds:
            if ds.count < 1 or ds.width < 1 or ds.height < 1:
                raise ValueError(f"Raster has no valid bands or dimensions: {p.name}")
        result.append(p)
    return result


def _open_warped(paths):
    """Open all sources on one common EPSG:32642, 10 m grid covering their union."""
    sources = []
    try:
        bounds = []
        for p in paths:
            src = rasterio.open(p)
            sources.append(src)
            if src.crs is None:
                raise ValueError(f"Raster has no CRS defined: {p.name}")
            bounds.append(transform_bounds(src.crs, TARGET_CRS, *src.bounds, densify_pts=21))
        left = min(b[0] for b in bounds); bottom = min(b[1] for b in bounds)
        right = max(b[2] for b in bounds); top = max(b[3] for b in bounds)
        width = max(1, int(np.ceil((right - left) / TARGET_RESOLUTION)))
        height = max(1, int(np.ceil((top - bottom) / TARGET_RESOLUTION)))
        transform = from_origin(left, top, TARGET_RESOLUTION, TARGET_RESOLUTION)
        datasets = []
        for src in sources:
            vrt = WarpedVRT(src, crs=TARGET_CRS, transform=transform, width=width, height=height,
                            resampling=Resampling.nearest, nodata=src.nodata)
            datasets.append((src, vrt))
        return datasets
    except Exception:
        for src in sources:
            src.close()
        raise


def run_local_processing(payload, log):
    operation = str(payload.get("operation", "stack")).lower()
    paths = _validate_inputs(payload.get("input_paths", []))
    output_dir = Path(payload.get("output_dir", "")).expanduser().resolve()
    if not str(payload.get("output_dir", "")).strip():
        raise ValueError("Choose an output folder.")
    output_dir.mkdir(parents=True, exist_ok=True)
    out_name = Path(str(payload.get("output_name", "Local_Processed_Output.tif"))).name
    if not out_name.lower().endswith((".tif", ".tiff")):
        out_name += ".tif"
    out_path = output_dir / out_name
    if any(p == out_path for p in paths):
        raise ValueError("Output filename cannot overwrite an input image.")
    mask_path = str(payload.get("mask_path", "")).strip()
    mask_path = Path(mask_path).expanduser().resolve() if mask_path else None
    if mask_path and not mask_path.is_file():
        raise ValueError(f"Mask raster not found: {mask_path}")

    started = time.perf_counter()
    _log(log, f"Local processing started | operation={operation} | inputs={len(paths)}")
    _log(log, f"Target grid: {TARGET_CRS}, {TARGET_RESOLUTION:g} m")
    tmp_dir = Path(tempfile.mkdtemp(prefix="local_raster_work_", dir=str(output_dir)))
    try:
        if operation == "stack":
            datasets = _open_warped(paths)
            try:
                first_vrt = datasets[0][1]
                width, height = first_vrt.width, first_vrt.height
                transform = first_vrt.transform
                total_bands = sum(vrt.count for _, vrt in datasets)
                dtype = np.result_type(*[np.dtype(vrt.dtypes[0]) for _, vrt in datasets]).name
                nodata = first_vrt.nodata
                if nodata is None:
                    nodata = np.nan if np.issubdtype(np.dtype(dtype), np.floating) else 0
                profile = {"driver":"GTiff", "width":width, "height":height, "count":total_bands,
                           "crs":TARGET_CRS, "transform":transform, "dtype":dtype, "nodata":nodata,
                           "compress":"deflate", "tiled":True, "BIGTIFF":"IF_SAFER"}
                band_descriptions = []
                with rasterio.open(out_path, "w", **profile) as dst:
                    idx = 1
                    for p, (_, vrt) in zip(paths, datasets):
                        _log(log, f"Stacking {p.name} ({vrt.count} band(s))")
                        for b in range(1, vrt.count + 1):
                            arr = vrt.read(b, out_dtype=dtype)
                            dst.write(arr, idx)
                            desc = vrt.descriptions[b-1] or f"Band_{b}"
                            label = f"{p.stem}_{desc}"[:240]
                            dst.set_band_description(idx, label)
                            band_descriptions.append(label)
                            idx += 1
            finally:
                for src, vrt in datasets:
                    vrt.close(); src.close()
            if mask_path:
                _apply_mask_file(out_path, mask_path, log)
        elif operation == "mosaic":
            datasets = _open_warped(paths)
            try:
                counts = {vrt.count for _, vrt in datasets}
                if len(counts) != 1:
                    raise ValueError("Mosaic inputs must have the same number of bands. Use Stack for rasters with different band counts.")
                vrt_list = [vrt for _, vrt in datasets]
                nodata_vals = {vrt.nodata for vrt in vrt_list}
                nodata = next(iter(nodata_vals)) if len(nodata_vals) == 1 else None
                mosaic, transform = merge(vrt_list, nodata=nodata, method="first", resampling=Resampling.nearest)
                profile = {"driver":"GTiff", "width":mosaic.shape[2], "height":mosaic.shape[1], "count":mosaic.shape[0],
                           "crs":TARGET_CRS, "transform":transform, "dtype":mosaic.dtype,
                           "nodata":nodata, "compress":"deflate", "tiled":True, "BIGTIFF":"IF_SAFER"}
                with rasterio.open(out_path, "w", **profile) as dst:
                    dst.write(mosaic)
                    for b in range(1, dst.count + 1):
                        dst.set_band_description(b, f"Mosaic_Band_{b}")
                if mask_path:
                    _apply_mask_file(out_path, mask_path, log)
                _log(log, f"Mosaic method: first valid source in the selected input order; later inputs do not overwrite earlier valid pixels.")
            finally:
                for src, vrt in datasets:
                    vrt.close(); src.close()
        elif operation == "mask":
            if not mask_path:
                raise ValueError("Select an agriculture mask GeoTIFF for Mask operation.")
            if len(paths) != 1:
                raise ValueError("Mask operation accepts one image at a time. Use Stack to combine multiple images and apply the mask, or run Mask for each image separately.")
            datasets = _open_warped(paths)
            src, vrt = datasets[0]
            try:
                profile = vrt.profile.copy()
                profile.pop("blockxsize", None); profile.pop("blockysize", None)
                profile.update(driver="GTiff", compress="deflate", tiled=False, BIGTIFF="IF_SAFER")
                with rasterio.open(out_path, "w", **profile) as dst:
                    for b in range(1, vrt.count + 1):
                        dst.write(vrt.read(b), b)
                        if vrt.descriptions[b-1]: dst.set_band_description(b, vrt.descriptions[b-1])
            finally:
                vrt.close(); src.close()
            _apply_mask_file(out_path, mask_path, log)
        else:
            raise ValueError("Operation must be Stack, Mosaic, or Mask.")

        with rasterio.open(out_path) as check:
            if check.count < 1 or check.width < 1 or check.height < 1 or check.crs is None:
                raise RuntimeError("Output validation failed: invalid raster metadata.")
            _log(log, f"Validated output: {check.width} x {check.height}, {check.count} band(s), {check.crs}, pixel size={check.res}")
        _log(log, f"Output saved: {out_path}")
        _log(log, f"Completed in {time.perf_counter()-started:.2f} seconds.")
        return str(out_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _apply_mask_file(output_path, mask_path, log):
    """Set pixels to output nodata where nearest-neighbour mask equals zero or is invalid."""
    with rasterio.open(output_path, "r+") as dst, rasterio.open(mask_path) as mask_src:
        mask_arr = np.zeros((dst.height, dst.width), dtype=np.float32)
        reproject(source=rasterio.band(mask_src, 1), destination=mask_arr,
                  src_transform=mask_src.transform, src_crs=mask_src.crs,
                  src_nodata=mask_src.nodata, dst_transform=dst.transform, dst_crs=dst.crs,
                  dst_nodata=0, resampling=Resampling.nearest)
        valid = np.isfinite(mask_arr) & (mask_arr > 0)
        nodata = dst.nodata
        if nodata is None:
            nodata = np.nan if np.issubdtype(np.dtype(dst.dtypes[0]), np.floating) else 0
            dst.nodata = nodata
        _log(log, f"Applying mask {Path(mask_path).name}; valid mask pixels are values > 0.")
        for _, window in dst.block_windows(1):
            rows = slice(int(window.row_off), int(window.row_off + window.height))
            cols = slice(int(window.col_off), int(window.col_off + window.width))
            keep = valid[rows, cols]
            for b in range(1, dst.count + 1):
                arr = dst.read(b, window=window)
                arr[~keep] = nodata
                dst.write(arr, b, window=window)
