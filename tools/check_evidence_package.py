from __future__ import annotations

import hashlib
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from tools.harness import RunningService, require, require_status, success

INSPECTION = "inspection-evidence-a"


def _create_sheet(
    service: Any,
    inspection_id: str,
    sheet_id: str,
    *,
    columns: int,
    tile_width: int,
    gutter: int,
    selection: str,
) -> dict[str, Any]:
    response = service.request_json(
        "POST",
        f"/v1/inspections/{inspection_id}/contact-sheets",
        {
            "sheet_id": sheet_id,
            "columns": columns,
            "tile_width": tile_width,
            "gutter": gutter,
            "selection": selection,
            "background": "#20252c",
        },
    )
    require_status(response, 201, f"contact sheet {sheet_id}")
    record = response.json()["contact_sheet"]
    require(record["state"] == "ready", f"contact sheet {sheet_id} is not ready")
    return record


def _publish(
    service: Any,
    inspection_id: str,
    package_id: str,
    sheet_ids: list[str] | None = None,
) -> Any:
    body: dict[str, Any] = {"package_id": package_id}
    if sheet_ids is not None:
        body["sheet_ids"] = sheet_ids
    return service.request_json(
        "POST",
        f"/v1/inspections/{inspection_id}/evidence-packages",
        body,
    )


def main() -> int:
    with RunningService() as service:
        inspection = service.prepare_inspection(
            clip_id="clip-evidence-a",
            inspection_id=INSPECTION,
        )
        require(inspection["state"] == "indexed", "inspection is not indexed")
        sheet_a = _create_sheet(
            service, INSPECTION, "sheet-a",
            columns=3, tile_width=96, gutter=4, selection="stored",
        )
        sheet_b = _create_sheet(
            service, INSPECTION, "sheet-b",
            columns=2, tile_width=64, gutter=2, selection="midpoint",
        )
        clip = service.request("GET", "/v1/clips/clip-evidence-a").json()["clip"]

        publish = _publish(service, INSPECTION, "pack-a", ["sheet-b", "sheet-a"])
        require_status(publish, 201, "evidence package publication")
        record = publish.json()["evidence_package"]
        require(record["state"] == "published", "package did not reach published")
        require(
            record["sheet_ids"] == ["sheet-a", "sheet-b"],
            "sheet ids were not normalized to a stable order",
        )
        require(record["clip_id"] == "clip-evidence-a", "wrong clip binding")
        require(record["scene_count"] == 4, "unexpected scene count")
        digest = record["artifact_digest"]
        manifest_digest = record["manifest_digest"]
        require(digest and manifest_digest, "package digests are missing")

        stored = service.request("GET", "/v1/evidence-packages/pack-a")
        require_status(stored, 200, "package lookup")
        require(
            stored.json()["evidence_package"]["artifact_digest"] == digest,
            "stored package digest differs",
        )

        artifact = service.request("GET", "/v1/evidence-packages/pack-a/artifact")
        require_status(artifact, 200, "package artifact fetch")
        require(
            artifact.content_type.startswith("application/zip"),
            "package artifact is not a ZIP",
        )
        require(
            hashlib.sha256(artifact.body).hexdigest() == digest,
            "downloaded artifact digest differs from its record",
        )
        require(
            len(artifact.body) == record["artifact_size"],
            "downloaded artifact size differs from its record",
        )

        archive = zipfile.ZipFile(io.BytesIO(artifact.body))
        names = archive.namelist()
        require(
            names
            == [
                "manifest.json",
                "contact-sheets/sheet-a/sheet.ppm",
                "contact-sheets/sheet-b/sheet.ppm",
            ],
            f"unexpected package members: {names}",
        )
        for info in archive.infolist():
            require(
                info.date_time == (1980, 1, 1, 0, 0, 0),
                "package members must use a frozen timestamp",
            )
        manifest_bytes = archive.read("manifest.json")
        require(
            hashlib.sha256(manifest_bytes).hexdigest() == manifest_digest,
            "manifest bytes differ from the recorded manifest digest",
        )
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        require(
            manifest["schema"] == "frame-proxy/evidence-package@1",
            "unexpected manifest schema",
        )
        require(
            manifest["source"]["digest"] == clip["digest"],
            "source summary digest differs from the clip record",
        )
        require(manifest["source"]["frame_count"] == 17, "wrong source frame count")
        require(
            manifest["inspection"]["inspection_id"] == INSPECTION,
            "manifest is bound to the wrong inspection",
        )
        require(
            manifest["inspection"]["cut_frames"] == [5, 9, 14],
            "manifest inspection facts differ",
        )
        require(
            len(manifest["inspection"]["scenes"]) == 4,
            "manifest scenes are incomplete",
        )
        require(len(manifest["contact_sheets"]) == 2, "manifest sheets incomplete")
        require(
            manifest["contact_sheets"][0]["tile_map"] == sheet_a["tile_map"],
            "sealed tile map differs from the sheet record",
        )

        files = {entry["path"]: entry for entry in manifest["files"]}
        require(
            set(files) == set(names[1:]),
            "file inventory does not match the package members",
        )
        for path, entry in files.items():
            payload = archive.read(path)
            require(
                hashlib.sha256(payload).hexdigest() == entry["sha256"],
                f"member {path} digest differs from the manifest",
            )
            require(len(payload) == entry["byte_size"], f"member {path} size differs")
        require(
            files["contact-sheets/sheet-a/sheet.ppm"]["sha256"]
            == sheet_a["artifact_digest"],
            "sealed sheet-a bytes differ from its ready record",
        )
        require(
            files["contact-sheets/sheet-b/sheet.ppm"]["sha256"]
            == sheet_b["artifact_digest"],
            "sealed sheet-b bytes differ from its ready record",
        )

        coverage = manifest["coverage"]
        require(len(coverage) == 4, "coverage must list every scene")
        require(coverage[0]["start_frame"] == 0, "first scene must start at zero")
        require(coverage[-1]["end_frame"] == 17, "last scene must end at frame count")
        for entry in coverage:
            require(
                {tile["sheet_id"] for tile in entry["tiles"]} == {"sheet-a", "sheet-b"},
                "each scene must be covered by both sheets",
            )
        require(
            manifest["coverage_summary"]
            == {
                "scene_count": 4,
                "covered_scene_count": 4,
                "sheet_count": 2,
                "fully_covered": True,
            },
            "coverage summary is inconsistent",
        )

        republish = _publish(service, INSPECTION, "pack-b")
        require_status(republish, 201, "evidence package republication")
        repeated = republish.json()["evidence_package"]
        require(
            repeated["sheet_ids"] == ["sheet-a", "sheet-b"],
            "default selection must include every ready sheet",
        )
        require(
            repeated["artifact_digest"] == digest,
            "republished package facts differ",
        )
        require(
            repeated["manifest_digest"] == manifest_digest,
            "republished manifest facts differ",
        )
        repeated_artifact = service.request(
            "GET", "/v1/evidence-packages/pack-b/artifact"
        )
        require_status(repeated_artifact, 200, "republished artifact fetch")
        require(
            repeated_artifact.body == artifact.body,
            "republished artifact bytes differ",
        )

        duplicate = _publish(service, INSPECTION, "pack-a")
        require_status(duplicate, 409, "duplicate package rejection")

        def _concurrent_publish(_: int) -> int:
            return _publish(service, INSPECTION, "pack-c").status

        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(_concurrent_publish, range(6)))
        require(
            statuses.count(201) == 1 and statuses.count(409) == 5,
            f"concurrent publication must elect exactly one winner: {statuses}",
        )
        winner = service.request("GET", "/v1/evidence-packages/pack-c")
        require_status(winner, 200, "concurrent winner lookup")
        require(
            winner.json()["evidence_package"]["artifact_digest"] == digest,
            "concurrent winner published different facts",
        )

        _create_sheet(
            service, INSPECTION, "sheet-c",
            columns=4, tile_width=48, gutter=2, selection="first",
        )
        revised = _publish(service, INSPECTION, "pack-d")
        require_status(revised, 201, "revised package publication")
        revised_record = revised.json()["evidence_package"]
        require(
            revised_record["sheet_ids"] == ["sheet-a", "sheet-b", "sheet-c"],
            "revised package must include the new sheet",
        )
        require(
            revised_record["artifact_digest"] != digest,
            "an extended sheet set must change the package facts",
        )
        after_revision = service.request("GET", "/v1/evidence-packages/pack-a/artifact")
        require_status(after_revision, 200, "package fetch after revision")
        require(
            hashlib.sha256(after_revision.body).hexdigest() == digest,
            "a later revision rewrote a published package",
        )

        service.prepare_inspection(
            clip_id="clip-evidence-b",
            inspection_id="inspection-evidence-b",
        )
        _create_sheet(
            service, "inspection-evidence-b", "sheet-d",
            columns=2, tile_width=64, gutter=2, selection="stored",
        )
        foreign = _publish(service, INSPECTION, "pack-foreign", ["sheet-d"])
        require_status(foreign, 409, "foreign sheet rejection")
        unknown_sheet = _publish(service, INSPECTION, "pack-unknown", ["sheet-zzz"])
        require_status(unknown_sheet, 404, "unknown sheet rejection")
        unknown_inspection = _publish(service, "inspection-zzz", "pack-stray")
        require_status(unknown_inspection, 404, "unknown inspection rejection")

        sheet_media = service.data_root / "contact-sheets" / "sheet-a" / "sheet.ppm"
        original = sheet_media.read_bytes()
        sheet_media.write_bytes(
            original[:20] + bytes([original[20] ^ 0xFF]) + original[21:]
        )
        tampered = _publish(service, INSPECTION, "pack-e", ["sheet-a"])
        require_status(tampered, 500, "tampered source rejection")
        require(
            tampered.json()["error"]["code"] == "storage_failure",
            "tampered source returned the wrong error code",
        )
        missing = service.request("GET", "/v1/evidence-packages/pack-e")
        require_status(missing, 404, "failed publication left a visible record")
        require(
            not (service.data_root / "evidence-packages" / "pack-e").exists(),
            "failed publication left files behind",
        )
        unaffected = _publish(service, INSPECTION, "pack-f", ["sheet-b"])
        require_status(unaffected, 201, "untampered sheet publication")
        still = service.request("GET", "/v1/evidence-packages/pack-a/artifact")
        require_status(still, 200, "package fetch after source tampering")
        require(
            hashlib.sha256(still.body).hexdigest() == digest,
            "source tampering rewrote a published package",
        )

    success(
        "publish-evidence-package",
        {
            "digest": digest,
            "manifest_digest": manifest_digest,
            "sheets": len(record["sheet_ids"]),
            "scenes": record["scene_count"],
            "concurrent_statuses": sorted(statuses),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
