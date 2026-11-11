from __future__ import annotations

import hashlib

from frame_proxy.config import Settings
from frame_proxy.domain.errors import (
    ApplicationError,
    ConflictError,
    ErrorCode,
    MediaValidationError,
    StateError,
    ValidationError,
)
from frame_proxy.domain.evidence import build_evidence_package
from frame_proxy.domain.models import ContactSheetRecord, EvidencePackageRecord
from frame_proxy.domain.policies import EvidencePackageRequest, validate_identifier
from frame_proxy.domain.transitions import transition_evidence_package
from frame_proxy.storage.repository import FileRepository


class EvidencePackageService:
    def __init__(self, repository: FileRepository, settings: Settings) -> None:
        self._repository = repository
        self._settings = settings

    def create(
        self,
        inspection_id: str,
        request: EvidencePackageRequest,
    ) -> EvidencePackageRecord:
        normalized_id = validate_identifier(inspection_id, "inspection_id")
        inspection = self._repository.get_inspection(normalized_id)
        if inspection.state != "indexed":
            raise StateError(
                "evidence packages require an indexed inspection",
                details={"state": inspection.state},
            )
        clip = self._repository.get_clip(inspection.clip_id)
        sheets = self._resolve_sheets(inspection.inspection_id, request)
        if len(sheets) > self._settings.max_package_sheets:
            raise ValidationError(
                "evidence package sheet budget is exceeded",
                code=ErrorCode.EVIDENCE_PACKAGE_BUDGET,
                details={
                    "sheet_count": len(sheets),
                    "max_sheets": self._settings.max_package_sheets,
                },
            )

        media = self._repository.get_clip_media(clip.clip_id)
        if hashlib.sha256(media).hexdigest() != clip.digest:
            raise MediaValidationError(
                "stored clip digest does not match its record",
                code=ErrorCode.FRAME_DECODE,
            )
        artifacts: dict[str, bytes] = {}
        artifact_bytes = 0
        for sheet in sheets:
            payload = self._repository.get_contact_sheet_artifact(
                sheet.sheet_id,
                sheet.artifact_name,
            )
            if hashlib.sha256(payload).hexdigest() != sheet.artifact_digest:
                raise ApplicationError(
                    ErrorCode.STORAGE,
                    "stored contact sheet artifact does not match its record",
                    status=500,
                    details={"sheet_id": sheet.sheet_id},
                )
            artifacts[sheet.sheet_id] = payload
            artifact_bytes += len(payload)
        if artifact_bytes > self._settings.max_package_bytes:
            raise ValidationError(
                "evidence package byte budget is exceeded",
                code=ErrorCode.EVIDENCE_PACKAGE_BUDGET,
                details={
                    "byte_size": artifact_bytes,
                    "max_bytes": self._settings.max_package_bytes,
                },
            )

        bundle = build_evidence_package(
            clip=clip,
            inspection=inspection,
            sheets=tuple(sheets),
            artifacts=artifacts,
        )
        if len(bundle.payload) > self._settings.max_package_bytes:
            raise ValidationError(
                "evidence package byte budget is exceeded",
                code=ErrorCode.EVIDENCE_PACKAGE_BUDGET,
                details={
                    "byte_size": len(bundle.payload),
                    "max_bytes": self._settings.max_package_bytes,
                },
            )

        record = EvidencePackageRecord(
            package_id=request.package_id,
            inspection_id=inspection.inspection_id,
            clip_id=clip.clip_id,
            state="sealing",
            sheet_ids=[sheet.sheet_id for sheet in sheets],
            scene_count=len(inspection.scenes),
            artifact_name="package.zip",
            artifact_digest=bundle.digest,
            artifact_size=len(bundle.payload),
            manifest_digest=bundle.manifest_digest,
        )
        self._repository.create_evidence_package(record)
        try:
            published = EvidencePackageRecord.from_dict(record.as_dict())
            transition_evidence_package(
                published,
                "published",
                reason="package digests verified",
            )
            self._repository.commit_evidence_package(published, bundle.payload)
            return published
        except ApplicationError as exc:
            self._mark_failed(record, exc.code.value, exc.message)
            raise
        except Exception as exc:
            self._mark_failed(record, ErrorCode.INTERNAL.value, str(exc))
            raise ApplicationError(
                ErrorCode.INTERNAL,
                "evidence package publication failed",
                status=500,
            ) from exc

    def _resolve_sheets(
        self,
        inspection_id: str,
        request: EvidencePackageRequest,
    ) -> list[ContactSheetRecord]:
        if request.sheet_ids is None:
            sheets = [
                sheet
                for sheet in self._repository.list_contact_sheets_for_inspection(
                    inspection_id
                )
                if sheet.state == "ready"
            ]
        else:
            sheets = []
            for sheet_id in request.sheet_ids:
                sheet = self._repository.get_contact_sheet(sheet_id)
                if sheet.inspection_id != inspection_id:
                    raise ConflictError(
                        "contact sheet belongs to a different inspection",
                        details={"sheet_id": sheet.sheet_id},
                    )
                if sheet.state != "ready":
                    raise StateError(
                        "contact sheet is not ready for publication",
                        details={"sheet_id": sheet.sheet_id, "state": sheet.state},
                    )
                sheets.append(sheet)
        return sorted(sheets, key=lambda sheet: sheet.sheet_id)

    def _mark_failed(
        self,
        record: EvidencePackageRecord,
        code: str,
        message: str,
    ) -> None:
        record.error_code = code
        record.error_message = message
        if record.state == "sealing":
            transition_evidence_package(record, "failed", reason=code)
        try:
            self._repository.update_evidence_package(record)
        except Exception:
            pass

    def get(self, package_id: str) -> EvidencePackageRecord:
        normalized = validate_identifier(package_id, "package_id")
        return self._repository.get_evidence_package(normalized)

    def get_artifact(self, package_id: str) -> bytes:
        normalized = validate_identifier(package_id, "package_id")
        record = self._repository.get_evidence_package(normalized)
        if record.state != "published":
            raise StateError(
                "evidence package is not published",
                details={"state": record.state},
            )
        return self._repository.get_evidence_package_artifact(
            record.package_id,
            record.artifact_name,
        )
