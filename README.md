# Frame Proxy Inspector

Frame Proxy Inspector is a small Python backend for turning uncompressed AVI
footage into three review artifacts:

- a deterministic shot index with contiguous scene ranges;
- a bounded PPM contact sheet with one tile per indexed scene;
- a frozen evidence package that seals one indexed inspection, its source
  summary, its ready contact sheets, and the scene-to-tile coverage into a
  single deterministic ZIP.

The service verifies the RIFF/AVI container, frame geometry, and frame chunk
count before it admits a clip. Shot indexing then computes luma and color
fingerprints from sampled frames and applies a minimum-scene guard to hard
cuts. Contact sheet requests are checked against a fixed pixel budget before
frame decoding starts.

Automated unit, integration, and browser tests are intentionally deferred to a
later task stage. This baseline ships executable workflow checks instead.

## Requirements

- Python 3.11 or newer
- No third-party packages
- No external media service

## Start the service

```bash
export PYTHONPATH=src
python3 -m frame_proxy serve --host 127.0.0.1 --port 8765
```

The service prints its bound URL. Press `Ctrl+C` to stop it.

## Generate demonstration footage

```bash
PYTHONPATH=src python3 -m frame_proxy demo-avi \
  --output demo/scene-walk.avi \
  --width 128 \
  --height 72 \
  --fps 12
```

The generated file is an uncompressed 24-bit AVI with four color scenes and
17 total frames. It is suitable for exercising all three workflows.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | service check |
| `PUT` | `/v1/clips/{clip_id}` | admit an `application/x-avi` payload |
| `GET` | `/v1/clips/{clip_id}` | read clip geometry and digest |
| `POST` | `/v1/clips/{clip_id}/inspections` | index shot boundaries |
| `GET` | `/v1/inspections/{inspection_id}` | read an immutable inspection |
| `POST` | `/v1/inspections/{inspection_id}/contact-sheets` | compose a PPM grid |
| `GET` | `/v1/contact-sheets/{sheet_id}` | read the tile map and digest |
| `GET` | `/v1/contact-sheets/{sheet_id}/artifact` | fetch the PPM bytes |
| `POST` | `/v1/inspections/{inspection_id}/evidence-packages` | seal an inspection into one ZIP |
| `GET` | `/v1/evidence-packages/{package_id}` | read the package record and digests |
| `GET` | `/v1/evidence-packages/{package_id}/artifact` | download the sealed ZIP |

Identifiers use lowercase letters, digits, `_`, and `-`, begin with a letter,
and contain 3 to 48 characters.

Example inspection request:

```json
{
  "inspection_id": "inspection-demo-a",
  "sensitivity": 0.55,
  "sample_stride": 1,
  "min_scene_frames": 2
}
```

Example contact sheet request:

```json
{
  "sheet_id": "sheet-demo-a",
  "columns": 3,
  "tile_width": 96,
  "gutter": 4,
  "selection": "stored",
  "background": "#20252c"
}
```

`selection` may be `stored`, `midpoint`, or `first`. The rendered PPM is
binary P6 with a maximum value of 255.

Example evidence package request:

```json
{
  "package_id": "pack-demo-a",
  "sheet_ids": ["sheet-demo-a"]
}
```

`sheet_ids` is optional; when omitted, every ready contact sheet of the
inspection is sealed. The downloaded ZIP holds a `manifest.json` (source
summary, inspection facts, per-sheet tile maps, scene coverage, and a file
inventory with SHA-256 digests) plus one PPM per sealed sheet. The manifest
contains no timestamps or package-local identifiers, so republishing the same
inspection facts yields an identical `manifest_digest` and `artifact_digest`.
Stored clip and sheet bytes are re-verified against their records at seal
time, and a published package is never rewritten by later source changes.

## Data layout

`FRAME_PROXY_DATA_DIR` selects the data root. Its default is
`.frame-proxy-data` in the current working directory.

```text
.frame-proxy-data/
├── clips/<clip_id>/
│   ├── clip.avi
│   └── record.json
├── inspections/<inspection_id>.json
├── contact-sheets/<sheet_id>/
│   ├── record.json
│   └── sheet.ppm
├── evidence-packages/<package_id>/
│   ├── record.json
│   └── package.zip
└── locks/
```

Records are written through temporary files and renamed into place. Contact
sheet bytes and their ready record are verified before the record becomes
visible.

## Source layout

```text
src/frame_proxy/
├── domain/      records, request rules, and state transitions
├── http/        request routing and response mapping
├── media/       AVI parsing, frame access, fingerprints, and PPM grids
├── service/     workflow orchestration and failure handling
└── storage/     namespaced files, locks, and atomic writes

tools/
├── harness.py
├── check_clip_intake.py
├── check_shot_index.py
├── check_contact_sheet.py
└── check_evidence_package.py
```

## Build and workflow checks

Build:

```bash
python3 -m compileall -q src tools
```

Run one workflow check at a time:

```bash
PYTHONPATH=src:. python3 tools/check_clip_intake.py
PYTHONPATH=src:. python3 tools/check_shot_index.py
PYTHONPATH=src:. python3 tools/check_contact_sheet.py
PYTHONPATH=src:. python3 tools/check_evidence_package.py
```

Each check starts a real loopback HTTP server, generates media through the
production AVI writer, completes the named workflow, and verifies persisted
state or artifact bytes. Temporary records are removed when the check exits.

## Environment variables

| Name | Default | Purpose |
| --- | --- | --- |
| `FRAME_PROXY_DATA_DIR` | `.frame-proxy-data` | durable data root |

Clip, upload, JSON, frame dimension, frame count, and contact sheet limits are
declared in `src/frame_proxy/config.py`.

## Input boundary

The current adapter accepts one uncompressed 24-bit BGR video stream in a
RIFF/AVI container. Audio streams, compressed video, and additional container
formats are intentionally outside this baseline. The extension seams for those
capabilities are recorded in `.task-source.json`.

