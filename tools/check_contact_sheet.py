from __future__ import annotations

import hashlib

from frame_proxy.media.avi_writer import build_sample_avi
from frame_proxy.media.ppm import parse_ppm_header
from frame_proxy.storage.paths import DataPaths
from tools.harness import RunningService, require, require_status, success


def main() -> int:
    with RunningService() as service:
        service.prepare_inspection(
            clip_id="clip-sheet-a",
            inspection_id="inspection-sheet-a",
        )
        response = service.request_json(
            "POST",
            "/v1/inspections/inspection-sheet-a/contact-sheets",
            {
                "sheet_id": "sheet-a",
                "columns": 3,
                "tile_width": 96,
                "gutter": 4,
                "selection": "stored",
                "background": "#20252c",
            },
        )
        require_status(response, 201, "contact sheet composition")
        record = response.json()["contact_sheet"]
        require(record["state"] == "ready", "contact sheet did not become ready")
        require(record["rows"] == 2, "unexpected contact sheet row count")
        require(len(record["tile_map"]) == 4, "tile map is incomplete")

        artifact_response = service.request(
            "GET",
            "/v1/contact-sheets/sheet-a/artifact",
        )
        require_status(artifact_response, 200, "artifact fetch")
        header = parse_ppm_header(artifact_response.body)
        require(
            header.width == record["image_width"],
            "artifact width differs from its record",
        )
        require(
            header.height == record["image_height"],
            "artifact height differs from its record",
        )
        require(
            hashlib.sha256(artifact_response.body).hexdigest()
            == record["artifact_digest"],
            "artifact digest differs from its record",
        )

        oversized = service.request_json(
            "POST",
            "/v1/inspections/inspection-sheet-a/contact-sheets",
            {
                "sheet_id": "sheet-b",
                "columns": 1,
                "tile_width": 320,
                "gutter": 20,
                "selection": "midpoint",
                "background": "#20252c",
            },
        )
        require_status(oversized, 400, "oversized grid rejection")
        require(
            oversized.json()["error"]["code"] == "contact_sheet_budget_exceeded",
            "oversized grid returned the wrong error code",
        )
        missing = service.request("GET", "/v1/contact-sheets/sheet-b")
        require_status(missing, 404, "partial contact sheet visibility")

        # A swapped source with identical geometry must not compose: the
        # registered clip digest anchors the sheet to the indexed footage.
        paths = DataPaths(service.data_root)
        paths.clip_media("clip-sheet-a").write_bytes(
            build_sample_avi(scene_lengths=(2, 6, 6, 3))
        )
        swapped = service.request_json(
            "POST",
            "/v1/inspections/inspection-sheet-a/contact-sheets",
            {
                "sheet_id": "sheet-c",
                "columns": 3,
                "tile_width": 96,
                "gutter": 4,
                "selection": "stored",
                "background": "#20252c",
            },
        )
        require_status(swapped, 422, "swapped source rejection")
        require(
            swapped.json()["error"]["code"] == "frame_decode_failed",
            "swapped source returned the wrong error code",
        )
        failed = service.request("GET", "/v1/contact-sheets/sheet-c")
        require_status(failed, 200, "failed sheet record lookup")
        failed_record = failed.json()["contact_sheet"]
        require(
            failed_record["state"] == "failed",
            "swapped source sheet did not become failed",
        )
        require(
            failed_record["error_code"] == "frame_decode_failed",
            "failed sheet lost its stable error code",
        )
        require(
            failed_record["artifact_digest"] is None,
            "failed sheet must not carry an artifact digest",
        )
        blocked = service.request("GET", "/v1/contact-sheets/sheet-c/artifact")
        require_status(blocked, 409, "failed sheet artifact must not resolve")
        require(
            not paths.contact_sheet_artifact("sheet-c", "sheet.ppm").exists(),
            "failed sheet left an artifact on disk",
        )

        # The previously committed sheet keeps serving its verified artifact.
        existing = service.request("GET", "/v1/contact-sheets/sheet-a/artifact")
        require_status(existing, 200, "existing artifact fetch")
        require(
            hashlib.sha256(existing.body).hexdigest() == record["artifact_digest"],
            "existing artifact changed after the source swap",
        )

    success(
        "create-contact-sheet",
        {
            "height": record["image_height"],
            "tiles": len(record["tile_map"]),
            "width": record["image_width"],
            "digest": record["artifact_digest"],
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

