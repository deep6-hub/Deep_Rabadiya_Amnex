from pathlib import Path
import threading
import traceback
import time
import uuid
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import json
import os
from pathlib import Path

from gee_worker import process, get_available_dates
from s2_worker import process_s2, get_s2_dates, S2_COLLECTION
import ee
from gee_worker import initialize_gee, _select_district, set_service_account_key
from local_processor import list_geotiffs, run_local_processing

app = FastAPI(title="Sentinel-1 VH GEE → Direct Local Download Automation")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

JOBS = {}
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "user_config.json"

class CredentialPathRequest(BaseModel):
    credential_path: str

def _credential_path_status():
    env_path = os.environ.get("GEE_SERVICE_ACCOUNT_KEY")
    if env_path:
        path = Path(env_path).expanduser()
    elif CONFIG_PATH.is_file():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("credential_path", "")
            path = Path(saved).expanduser() if saved else BASE_DIR / "credentials.json"
        except Exception:
            path = BASE_DIR / "credentials.json"
    else:
        path = BASE_DIR / "credentials.json"
    project_id = ""
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            project_id = str(data.get("project_id", "")).strip()
        except Exception:
            pass
    return {
        "configured": path.is_file() and bool(project_id),
        "path": str(path.resolve()) if path.is_file() else "",
        "project_id": project_id,
        "default_path": str(BASE_DIR / "credentials.json"),
    }


class JobRequest(BaseModel):
    district_path: str
    district_field: str = "district"
    district_name: str
    mask_path: str
    output_dir: str
    year: int
    start_month: int
    end_month: int
    polarization: str = "VH"
    orbit: str = "DESCENDING"
    crs: str = "EPSG:32642"
    selected_dates: list[str] = []
    mode: str = "s1"
    selected_bands: list[str] = []
    selected_indices: list[str] = []
    selected_outputs: list[dict] = []


class LocalFilesRequest(BaseModel):
    folder: str
    recursive: bool = False


class LocalProcessingRequest(BaseModel):
    operation: str = "stack"
    input_paths: list[str] = []
    output_dir: str
    output_name: str = "Local_Processed_Output.tif"
    mask_path: str = ""


@app.get("/credentials/status")
def credentials_status():
    return _credential_path_status()

@app.post("/credentials/configure")
def configure_credentials(req: CredentialPathRequest):
    """Validate a local service-account key and save only its path, never its contents."""
    raw = req.credential_path.strip().strip('"')
    if not raw:
        return JSONResponse(status_code=400, content={"error": "Choose or enter the full path to your Google Earth Engine service-account JSON key."})
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    if not path.is_file() or path.suffix.lower() != ".json":
        return JSONResponse(status_code=400, content={"error": f"JSON key file not found: {path}"})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        required = ("client_email", "private_key", "project_id")
        missing = [key for key in required if not data.get(key)]
        if missing:
            return JSONResponse(status_code=400, content={"error": "This JSON does not look like a Google service-account key. Missing: " + ", ".join(missing)})
        # Validate with Earth Engine before saving the selected path.
        set_service_account_key(path)
        # initialize_gee reads project_id directly from this selected JSON key.
        initialize_gee()
        # Persist path only; never copy private key contents into the application folder.
        CONFIG_PATH.write_text(json.dumps({"credential_path": str(path.resolve())}, indent=2), encoding="utf-8")
        return {"configured": True, "path": str(path.resolve()), "project_id": data["project_id"], "message": "Credentials validated and saved on this PC."}
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": f"Credential validation failed: {type(exc).__name__}: {exc}"})

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/dates")
def get_dates(job: JobRequest):
    try:
        if job.mode == "s2":
            dates = get_s2_dates(job.district_path, job.district_field, job.district_name, job.year, job.start_month, job.end_month)
            return {"dates": dates}
        dates = get_available_dates(
            job.district_path,
            job.district_field,
            job.district_name,
            job.year,
            job.start_month,
            job.end_month,
            job.polarization,
            job.orbit,
        )
        return {"dates": dates}
    except Exception:
        return JSONResponse(
            status_code=500,
            content={"error": traceback.format_exc()},
        )


def run_job(job_id, payload):
    JOBS[job_id] = {"status": "running", "logs": []}

    def log(message, level=1):
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        JOBS[job_id]["logs"].append(f"[{timestamp}] {message}")

    job_started = time.perf_counter()
    log("[JOB START] Full workflow started")
    try:
        if payload.get("mode") == "s2":
            process_s2(payload, log)
        else:
            process(payload, log)
        elapsed = time.perf_counter() - job_started
        log(f"[JOB END] Workflow completed | total elapsed={elapsed:.2f} sec ({elapsed/60:.2f} min)")
        JOBS[job_id]["status"] = "completed"
    except Exception:
        elapsed = time.perf_counter() - job_started
        log(f"[JOB FAILED] Workflow stopped | elapsed={elapsed:.2f} sec ({elapsed/60:.2f} min)")
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["logs"].append("ERROR:")
        JOBS[job_id]["logs"].append(traceback.format_exc())



@app.post("/preview/{preview_date}")
def preview_image(preview_date: str, job: JobRequest):
    """Return a temporary Earth Engine tile URL for S1 VH/VV or S2 FCC preview."""
    try:
        initialize_gee()
        geom_wgs84, _ = _select_district(job.district_path, job.district_field, job.district_name)
        region = ee.Geometry(geom_wgs84.__geo_interface__)
        start = ee.Date(preview_date)
        end = start.advance(1, "day")
        bounds = list(geom_wgs84.bounds)  # minx, miny, maxx, maxy in EPSG:4326

        if job.mode == "s2":
            collection = (ee.ImageCollection(S2_COLLECTION)
                          .filterDate(start, end)
                          .filterBounds(region)
                          .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 100)))
            count = collection.size().getInfo()
            if not count:
                return JSONResponse(status_code=404, content={"error": f"No Sentinel-2 scenes found for {preview_date}."})
            mosaic = collection.select(["B2", "B3", "B4", "B8"]).mosaic().clip(region)
            visual = mosaic.visualize(bands=["B8", "B4", "B3"], min=0, max=3000, gamma=1.1)
            visualization = "FCC: B8 (NIR), B4 (Red), B3 (Green)"
        else:
            polarization = job.polarization.upper()
            if polarization not in ("VH", "VV"):
                return JSONResponse(status_code=400, content={"error": "Preview polarization must be VH or VV."})
            collection = (ee.ImageCollection("COPERNICUS/S1_GRD")
                          .filterDate(start, end)
                          .filterBounds(region)
                          .filter(ee.Filter.eq("instrumentMode", "IW"))
                          .filter(ee.Filter.eq("orbitProperties_pass", job.orbit))
                          .filter(ee.Filter.listContains("transmitterReceiverPolarisation", polarization))
                          .select(polarization))
            count = collection.size().getInfo()
            if not count:
                return JSONResponse(status_code=404, content={"error": f"No Sentinel-1 {polarization} scenes found for {preview_date} with {job.orbit} orbit."})
            mosaic = collection.mosaic().clip(region)
            # Sentinel-1 GRD values are in dB. Grayscale stretch emphasizes typical crop-land backscatter.
            visual = mosaic.visualize(min=-25, max=-5, palette=["000000", "FFFFFF"])
            visualization = f"Sentinel-1 {polarization} backscatter (dB, grayscale; {job.orbit})"

        map_info = visual.getMapId()
        return {"date": preview_date,
                "tile_url": map_info["tile_fetcher"].url_format,
                "bounds": [[bounds[1], bounds[0]], [bounds[3], bounds[2]]],
                "scene_count": count,
                "visualization": visualization}
    except Exception:
        return JSONResponse(status_code=500, content={"error": traceback.format_exc()})

@app.post("/preview-mosaic")
def preview_mosaic(job: JobRequest):
    """Preview a native Earth Engine ImageCollection.mosaic() across selected dates."""
    try:
        if len(job.selected_dates) < 2:
            return JSONResponse(status_code=400, content={"error": "Select at least two acquisition dates using the date checkboxes to create a mosaic."})
        initialize_gee()
        geom_wgs84, _ = _select_district(job.district_path, job.district_field, job.district_name)
        region = ee.Geometry(geom_wgs84.__geo_interface__)
        bounds = list(geom_wgs84.bounds)

        # Merge the selected one-day collections, then sort chronologically.
        # GEE mosaic() gives priority to later images in collection order.
        if job.mode == "s2":
            collection = ee.ImageCollection([])
            for day in sorted(set(job.selected_dates)):
                start = ee.Date(day)
                daily = (ee.ImageCollection(S2_COLLECTION)
                         .filterDate(start, start.advance(1, "day"))
                         .filterBounds(region)
                         .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 100))
                         .select(["B2", "B3", "B4", "B8"]))
                collection = collection.merge(daily)
            count = collection.size().getInfo()
            if not count:
                return JSONResponse(status_code=404, content={"error": "No Sentinel-2 scenes found on the selected dates."})
            collection = collection.sort("system:time_start")
            image = collection.mosaic().clip(region)
            visual = image.visualize(bands=["B8", "B4", "B3"], min=0, max=3000, gamma=1.1)
            visualization = "Sentinel-2 FCC GEE mosaic(): B8 (NIR), B4 (Red), B3 (Green)"
        else:
            polarization = job.polarization.upper()
            if polarization not in ("VH", "VV"):
                return JSONResponse(status_code=400, content={"error": "Preview polarization must be VH or VV."})
            collection = ee.ImageCollection([])
            for day in sorted(set(job.selected_dates)):
                start = ee.Date(day)
                daily = (ee.ImageCollection("COPERNICUS/S1_GRD")
                         .filterDate(start, start.advance(1, "day"))
                         .filterBounds(region)
                         .filter(ee.Filter.eq("instrumentMode", "IW"))
                         .filter(ee.Filter.eq("orbitProperties_pass", job.orbit))
                         .filter(ee.Filter.listContains("transmitterReceiverPolarisation", polarization))
                         .select(polarization))
                collection = collection.merge(daily)
            count = collection.size().getInfo()
            if not count:
                return JSONResponse(status_code=404, content={"error": f"No Sentinel-1 {polarization} scenes found on the selected dates with {job.orbit} orbit."})
            collection = collection.sort("system:time_start")
            image = collection.mosaic().clip(region)
            visual = image.visualize(min=-25, max=-5, palette=["000000", "FFFFFF"])
            visualization = f"Sentinel-1 {polarization} GEE mosaic() (dB, grayscale; {job.orbit})"

        map_info = visual.getMapId()
        return {"date": ", ".join(sorted(set(job.selected_dates))), 
                "tile_url": map_info["tile_fetcher"].url_format,
                "bounds": [[bounds[1], bounds[0]], [bounds[3], bounds[2]]],
                "scene_count": count,
                "visualization": visualization,
                "mosaic_dates": sorted(set(job.selected_dates)),
                "mosaic_method": "ee.ImageCollection.mosaic()"}
    except Exception:
        return JSONResponse(status_code=500, content={"error": traceback.format_exc()})


@app.post("/run")
def start_job(job: JobRequest):
    if job.crs != "EPSG:32642":
        return JSONResponse(
            status_code=400,
            content={"error": "CRS is locked to EPSG:32642."},
        )

    if not job.selected_outputs:
        return JSONResponse(
            status_code=400,
            content={"error": "Select at least one previewed date or mosaic in the output list."},
        )
    if any(not item.get("dates") or item.get("kind") not in ("date", "mosaic") for item in job.selected_outputs):
        return JSONResponse(status_code=400, content={"error": "Invalid output selection."})
    job.selected_dates = sorted({d for item in job.selected_outputs for d in item.get("dates", [])})

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"status": "queued", "logs": []}

    thread = threading.Thread(
        target=run_job,
        args=(job_id, job.model_dump()),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id}


@app.post("/local/files")
def scan_local_files(req: LocalFilesRequest):
    try:
        files = list_geotiffs(req.folder, req.recursive)
        return {"files": files}
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


def run_local_job(job_id: str, payload: dict):
    JOBS[job_id] = {"status": "running", "logs": []}
    def log(message, level=1):
        JOBS[job_id]["logs"].append(message)
    try:
        output_path = run_local_processing(payload, log)
        JOBS[job_id]["output_path"] = output_path
        JOBS[job_id]["status"] = "completed"
    except Exception as exc:
        log(f"ERROR: {type(exc).__name__}: {exc}")
        import traceback as _traceback
        log(_traceback.format_exc())
        JOBS[job_id]["status"] = "error"


@app.post("/local/run")
def start_local_job(req: LocalProcessingRequest):
    if not req.input_paths:
        return JSONResponse(status_code=400, content={"error": "Select at least one GeoTIFF."})
    if req.operation not in ("stack", "mosaic", "mask"):
        return JSONResponse(status_code=400, content={"error": "Choose Stack, Mosaic, or Mask."})
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"status": "queued", "logs": []}
    threading.Thread(target=run_local_job, args=(job_id, req.model_dump()), daemon=True).start()
    return {"job_id": job_id}


@app.get("/status/{job_id}")
def status(job_id: str):
    if job_id not in JOBS:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    return JOBS[job_id]
