# Sentinel-1 VH GEE Direct-to-Local Automation

This version downloads Sentinel-1 VH/VV date rasters directly from Google Earth Engine to the local computer. It does not export through Google Drive and does not use Google Drive OAuth credentials.

## Run on Windows without Anaconda

1. Install Python 3.11 (64-bit).
2. Open Command Prompt in this project folder.
3. Create a virtual environment: `py -3.11 -m venv .venv`
4. Install dependencies: `.venv\Scripts\python -m pip install -r requirements.txt`
5. Place your newly generated service-account key beside `start.bat` as `credentials.json`. The application reads the project ID from that key by default.
6. Double-click `start.bat`, then open `http://127.0.0.1:8000`.

## Service-account requirements

- The JSON key must belong to a service account that is enabled and authorized to use Earth Engine.
- The application automatically reads `project_id` from the selected service-account JSON. That project must be registered/enabled for Earth Engine, and the service account must be authorized to use it. No separate project-name entry is needed for the standard setup.
- The service account must have the required access to use that project.
- This application initializes Earth Engine using the service-account JSON key; it does not call `earthengine authenticate` and does not require interactive browser OAuth.
- **SECURITY WARNING:** This package intentionally excludes a live service-account key. Use a newly generated key for Earth Engine workflows, and revoke any previously shared key. Never commit or share credentials.

## Workflow
- Query Sentinel-1 GRD dates by district, year/month range, polarization, and orbit.
- Download each selected date in 2048-pixel tiles directly from `getDownloadURL()`.
- Retry failed tiles and reuse readable tiles / date rasters from prior runs.
- Merge tiles into one local date GeoTIFF.
- Stack all selected outputs into a multi-band raster in the numeric order set in the output queue.
- Validate the final stack, then automatically delete temporary tile folders. Per-date output rasters and the final stack are retained.
- Align and apply the agriculture mask locally.
- Output CRS is locked to EPSG:32642 and scale is 10 m.

## Output folders
- `01_raw_vh`: one merged GeoTIFF per selected date.
- `_direct_download_tiles`: resumable direct-download tiles.
- `_direct_download_work`: temporary raw stack and aligned mask.
- Final masked multi-band stack is written in the selected output folder.

## Step timing logs

The job log records local timestamps, explicit start/end entries and elapsed duration for processing steps and failed timed steps.

## Sentinel-2 Optical tab
- Use the Sentinel-2 (Optical) tab beside Sentinel-1 (SAR).
- Select output bands (B2, B3, B4, B8, B11, B12) and/or indices (NDVI, LSWI, SAVI) **before** loading dates.
- For every selected date, the app downloads only the required source spectral bands as separate single-band GeoTIFF tiles, merges each band locally, and stacks the selected output bands at the end.
- Selected indices (NDVI, LSWI, SAVI) are calculated locally from the downloaded source bands; index images are not downloaded from Earth Engine. If an index is selected, its required source bands are downloaded even if those bands are not selected for the final output.
- Single-band requests use 2048-pixel tiles and up to 4 concurrent downloads. This keeps each request smaller than multiband requests and reduces tile count; actual speed depends on network and Earth Engine response time.
- Intermediate band tiles and merged band files are reused while a job is running or after an interrupted run. After a successful final stack is created and validated, the temporary tile and merged-band working folders are deleted to save disk space.
- Final per-date outputs contain selected bands plus selected indices; an agriculture mask is applied locally using nearest-neighbour resampling. A final multi-band stack combines every selected output in the exact numeric order set in the output queue, retaining each output’s bands and indices. Output CRS is EPSG:32642 at 10 m. After the final stack validates, temporary Sentinel-2 tile and merged-band working folders are deleted; per-date outputs and the final stack are retained.
- Index formulas: NDVI=(B8-B4)/(B8+B4); LSWI=(B8-B11)/(B8+B11); SAVI=1.5*(B8-B4)/(B8+B4+0.5), with reflectance scaled from Sentinel-2 stored values by 10000 for SAVI calculation.


## Service-account key security

This package intentionally does not include a live service-account key. Create a new key in your Earth Engine-enabled Google Cloud project, save it in this folder as `credentials.json`, and ensure Earth Engine is enabled for that project. Do not reuse any key previously shared in a ZIP or chat. `credentials.example.json` shows the expected file structure only.

## Multi-date mosaic preview

Both Sentinel-1 SAR (VH/VV) and Sentinel-2 optical preview support a native Google Earth Engine `ImageCollection.mosaic()` preview. Select at least two acquisition dates using the date checkboxes and click **MOSAIC SELECTED DATES**. The app merges scenes from those exact days, sorts them by acquisition time, and calls `mosaic()`; later acquisition times take priority where valid pixels overlap. Sentinel-2 is displayed as FCC (B8/B4/B3); Sentinel-1 is displayed as grayscale backscatter. This is preview-only and does not change the normal download workflow.

## Local Processing tab
The third tab processes GeoTIFFs already on the computer and does not call Earth Engine. Enter an input folder, scan it, check files and set their numeric order. Available operations:
- **Stack:** appends the selected files' bands in the listed order. Files are aligned to a common EPSG:32642 grid at 10 m using nearest-neighbour resampling; output band descriptions include the source filename and source band description where available.
- **Mosaic:** spatially merges selected rasters. All inputs must have the same band count. The first valid input in the displayed order has priority in overlap areas.
- **Mask:** applies a one-band mask to one selected raster. Mask values greater than zero are retained; zero and invalid mask pixels become output NoData. Stack and Mosaic can also apply the optional mask after the operation.

Temporary working files are removed when processing finishes. Input images are never deleted. Test local operations on a small sample before production use.


## Team-member credential setup and quiet launch

- Each user should keep their own Google Earth Engine service-account JSON key in a private local folder. Do not distribute private keys in the application ZIP.
- Start the app, open `http://127.0.0.1:8000`, enter the full JSON key path in the **Google Earth Engine setup** panel, then click **VALIDATE & SAVE KEY PATH**. The app reads `project_id` directly from the selected key, validates authentication with Earth Engine, and saves only the key file path in `user_config.json` on that PC. No separate project ID entry is required for normal use.
- The Local Processing tab works without a key. Sentinel-1 and Sentinel-2 acquisition operations require a successfully validated key.
- For a visible console while debugging, use `start.bat`. For a quiet launch without the command prompt, double-click `Start_Silent.vbs`. Output is written to `server.log`; open that file if startup fails.
- To stop a quiet server, use Task Manager to stop the `python.exe` process running Uvicorn, or use `start.bat` in a visible console for easier shutdown. The app binds to `127.0.0.1` and is intended for use on the same PC. For multiple team members on separate PCs, each person runs their own local copy and selects their own key.
