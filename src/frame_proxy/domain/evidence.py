"""Deterministic evidence package assembly.

The manifest and the ZIP container are pure functions of the sealed facts:
no wall-clock timestamps, no package-local identifiers, and a fixed member
order. Republishing the same inspection facts therefore yields byte-identical
artifacts and identical digests.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from typing import Any

from frame_proxy.domain.models import (
    ClipRecord,
    ContactSheetRecord,
    InspectionRecord,
)

EVIDENCE_SCHEMA = "frame-proxy/evidence-package@1"
MANIFEST_NAME = "manifest.json"
_FROZEN_DATE_TIME = (1980, 1, 1, 0, 0, 0)


@dataclass(frozen=True, slots=True)
class EvidencePackageBundle:
    payload: bytes
    digest: str
    manifest_digest: str
    manifest: dict[str, Any]
    member_paths: tuple[str, ...]


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8") + b"\n"


def sheet_member_path(sheet: ContactSheetRecord) -> str:
    return f"contact-sheets/{sheet.sheet_id}/{sheet.artifact_name}"


def _source_summary(clip: ClipRecord) -> dict[str, Any]:
    return {
        "clip_id": clip.clip_id,
        "state": clip.state,
        "digest": clip.digest,
        "byte_size": clip.byte_size,
        "width": clip.width,
        "height": clip.height,
        "frame_count": clip.frame_count,
        "fps": {
            "numerator": clip.fps_numerator,
            "denominator": clip.fps_denominator,
        },
        "row_stride": clip.row_stride,
        "frame_payload_size": clip.frame_payload_size,
        "top_down": clip.top_down,
        "handler": clip.handler,
    }


def _inspection_facts(inspection: InspectionRecord) -> dict[str, Any]:
    return {
        "inspection_id": inspection.inspection_id,
        "clip_id": inspection.clip_id,
        "state": inspection.state,
        "sensitivity": inspection.sensitivity,
        "sample_stride": inspection.sample_stride,
        "min_scene_frames": inspection.min_scene_frames,
        "sampled_frames": list(inspection.sampled_frames),
        "cut_frames": list(inspection.cut_frames),
        "scenes": [scene.as_dict() for scene in inspection.scenes],
    }


def _sheet_entry(sheet: ContactSheetRecord, payload: bytes) -> dict[str, Any]:
    return {
        "sheet_id": sheet.sheet_id,
        "state": sheet.state,
        "selection": sheet.selection,
        "columns": sheet.columns,
        "rows": sheet.rows,
        "tile_width": sheet.tile_width,
        "tile_height": sheet.tile_height,
        "gutter": sheet.gutter,
        "background_rgb": list(sheet.background_rgb),
        "image_width": sheet.image_width,
        "image_height": sheet.image_height,
        "artifact": {
            "path": sheet_member_path(sheet),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "byte_size": len(payload),
        },
        "tile_map": [dict(tile) for tile in sheet.tile_map],
    }


def _coverage(
    inspection: InspectionRecord,
    sheets: tuple[ContactSheetRecord, ...],
) -> list[dict[str, Any]]:
    coverage: list[dict[str, Any]] = []
    for scene in inspection.scenes:
        tiles: list[dict[str, Any]] = []
        for sheet in sheets:
            for tile in sheet.tile_map:
                if tile.get("scene_index") == scene.index:
                    tiles.append(
                        {
                            "sheet_id": sheet.sheet_id,
                            "tile_index": tile["tile_index"],
                            "source_frame": tile["source_frame"],
                        }
                    )
        coverage.append(
            {
                "scene_index": scene.index,
                "start_frame": scene.start_frame,
                "end_frame": scene.end_frame,
                "representative_frame": scene.representative_frame,
                "tiles": tiles,
            }
        )
    return coverage


def build_evidence_manifest(
    *,
    clip: ClipRecord,
    inspection: InspectionRecord,
    sheets: tuple[ContactSheetRecord, ...],
    artifacts: dict[str, bytes],
) -> dict[str, Any]:
    sheet_entries: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    for sheet in sheets:
        payload = artifacts[sheet.sheet_id]
        entry = _sheet_entry(sheet, payload)
        sheet_entries.append(entry)
        files.append(
            {
                "path": entry["artifact"]["path"],
                "sha256": entry["artifact"]["sha256"],
                "byte_size": entry["artifact"]["byte_size"],
            }
        )
    coverage = _coverage(inspection, sheets)
    covered = sum(1 for entry in coverage if entry["tiles"])
    return {
        "schema": EVIDENCE_SCHEMA,
        "source": _source_summary(clip),
        "inspection": _inspection_facts(inspection),
        "contact_sheets": sheet_entries,
        "coverage": coverage,
        "coverage_summary": {
            "scene_count": len(coverage),
            "covered_scene_count": covered,
            "sheet_count": len(sheet_entries),
            "fully_covered": covered == len(coverage),
        },
        "files": files,
    }


def _fixed_zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=name, date_time=_FROZEN_DATE_TIME)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 0
    info.external_attr = 0o600 << 16
    return info


def render_evidence_zip(
    manifest_payload: bytes,
    members: tuple[tuple[str, bytes], ...],
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            _fixed_zip_info(MANIFEST_NAME),
            manifest_payload,
            compress_type=zipfile.ZIP_STORED,
        )
        for path, payload in members:
            archive.writestr(
                _fixed_zip_info(path),
                payload,
                compress_type=zipfile.ZIP_STORED,
            )
    return buffer.getvalue()


def build_evidence_package(
    *,
    clip: ClipRecord,
    inspection: InspectionRecord,
    sheets: tuple[ContactSheetRecord, ...],
    artifacts: dict[str, bytes],
) -> EvidencePackageBundle:
    manifest = build_evidence_manifest(
        clip=clip,
        inspection=inspection,
        sheets=sheets,
        artifacts=artifacts,
    )
    manifest_payload = canonical_json_bytes(manifest)
    members = tuple(
        (sheet_member_path(sheet), artifacts[sheet.sheet_id]) for sheet in sheets
    )
    payload = render_evidence_zip(manifest_payload, members)
    return EvidencePackageBundle(
        payload=payload,
        digest=hashlib.sha256(payload).hexdigest(),
        manifest_digest=hashlib.sha256(manifest_payload).hexdigest(),
        manifest=manifest,
        member_paths=tuple(path for path, _ in members),
    )
