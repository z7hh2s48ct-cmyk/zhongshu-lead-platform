from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..core.auth import Principal
from ..core.errors import AppError
from ..core.v12_enums import LeadSourceKind
from ..schemas.v12_lead_supply import LeadDraftBody
from .audit import write_audit
from .lead_supply_v12 import create_draft, submit_draft
from .xlsx_writer import WorksheetSpec, write_xlsx

_XLSX_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XLSX_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

MAX_IMPORT_FILE_BYTES = 2 * 1024 * 1024
MAX_IMPORT_UNCOMPRESSED_BYTES = 8 * 1024 * 1024
MAX_IMPORT_ROWS = 200

_HEADER_FIELDS = {
    "客户姓名": "customer_name",
    "手机号": "phone",
    "客户微信号": "customer_wechat",
    "微信号": "customer_wechat",
    "所在省": "province",
    "所在市": "city",
    "所在区县": "district",
    "地区编码": "region_code",
    "客资类型": "category_code",
    "品牌": "brand_code",
    "来源渠道": "source_channel",
    "来源说明": "source_detail",
    "客户需求": "need_summary",
    "已获客户授权": "consent_confirmed",
}
_CONTACT_HEADERS = {"手机号", "客户微信号", "微信号"}
_TRUE_VALUES = {"1", "true", "yes", "y", "是", "已授权", "已同意", "确认"}


@dataclass(frozen=True, slots=True)
class SupplierImportRow:
    row_number: int
    customer_name: str
    values: dict[str, Any]


def build_supplier_import_template() -> bytes:
    headers = [
        "客户姓名",
        "手机号",
        "客户微信号",
        "所在省",
        "所在市",
        "所在区县",
        "地区编码",
        "客资类型",
        "品牌",
        "来源渠道",
        "来源说明",
        "客户需求",
        "已获客户授权",
    ]
    with NamedTemporaryFile(suffix=".xlsx") as temporary:
        write_xlsx(
            Path(temporary.name),
            [
                WorksheetSpec(
                    name="客资导入模板",
                    fieldnames=headers,
                    rows=[{header: "" for header in headers}],
                    rows_per_sheet=MAX_IMPORT_ROWS,
                    total_rows=1,
                )
            ],
            heartbeat=lambda: None,
            heartbeat_row_interval=50,
            max_uncompressed_bytes=MAX_IMPORT_UNCOMPRESSED_BYTES,
        )
        return Path(temporary.name).read_bytes()


def parse_supplier_import_xlsx(content: bytes) -> list[SupplierImportRow]:
    if not content or len(content) > MAX_IMPORT_FILE_BYTES:
        raise AppError(
            "SUPPLIER_IMPORT_FILE_SIZE_INVALID",
            "Excel 文件不能为空且不得超过 2MB",
            422,
        )
    try:
        with ZipFile(BytesIO(content)) as workbook:
            if sum(item.file_size for item in workbook.infolist()) > MAX_IMPORT_UNCOMPRESSED_BYTES:
                raise AppError(
                    "SUPPLIER_IMPORT_FILE_SIZE_INVALID",
                    "Excel 解压后文件过大",
                    422,
                )
            rows = _first_worksheet_rows(workbook)
    except AppError:
        raise
    except (BadZipFile, KeyError, ValueError, ElementTree.ParseError) as exc:
        raise AppError(
            "SUPPLIER_IMPORT_FILE_INVALID",
            "无法读取 Excel 文件，请使用系统模板",
            422,
        ) from exc

    if not rows:
        raise AppError("SUPPLIER_IMPORT_HEADER_INVALID", "Excel 缺少表头", 422)
    headers = [value.strip().lstrip("\ufeff") for value in rows[0]]
    if len(headers) != len(set(headers)):
        raise AppError("SUPPLIER_IMPORT_HEADER_INVALID", "Excel 表头不能重复", 422)
    supported_headers = set(headers).intersection(_HEADER_FIELDS)
    if not supported_headers.intersection(_CONTACT_HEADERS):
        raise AppError(
            "SUPPLIER_IMPORT_HEADER_INVALID",
            "Excel 至少需要包含手机号或客户微信号列",
            422,
        )
    unknown_headers = [header for header in headers if header and header not in _HEADER_FIELDS]
    if unknown_headers:
        raise AppError(
            "SUPPLIER_IMPORT_HEADER_INVALID",
            "Excel 包含不支持的列",
            422,
            {"headers": unknown_headers},
        )

    parsed: list[SupplierImportRow] = []
    for row_number, cells in enumerate(rows[1:], start=2):
        values_by_header = {
            header: cells[index].strip() if index < len(cells) else ""
            for index, header in enumerate(headers)
            if header
        }
        if not any(values_by_header.values()):
            continue
        values: dict[str, Any] = {}
        for header, raw_value in values_by_header.items():
            field = _HEADER_FIELDS[header]
            if field == "consent_confirmed":
                values[field] = raw_value.lower() in _TRUE_VALUES
            elif raw_value:
                values[field] = raw_value
        parsed.append(
            SupplierImportRow(
                row_number=row_number,
                customer_name=str(values.get("customer_name") or "未填写"),
                values=values,
            )
        )
        if len(parsed) > MAX_IMPORT_ROWS:
            raise AppError(
                "SUPPLIER_IMPORT_ROW_LIMIT_EXCEEDED",
                f"每次最多导入 {MAX_IMPORT_ROWS} 条客资",
                422,
            )
    if not parsed:
        raise AppError("SUPPLIER_IMPORT_EMPTY", "Excel 中没有可导入的客资", 422)
    return parsed


def import_supplier_leads(
    db: Session,
    *,
    principal: Principal,
    content: bytes,
    request_id: str | None,
) -> dict[str, Any]:
    rows = parse_supplier_import_xlsx(content)
    results: list[dict[str, Any]] = []
    success_count = 0
    draft_count = 0
    for row in rows:
        try:
            with db.begin_nested():
                body = LeadDraftBody.model_validate(row.values)
                lead = create_draft(
                    db,
                    principal=principal,
                    source_kind=LeadSourceKind.SUPPLIER_H5,
                    values=body.model_dump(exclude_none=True),
                )
                province_only = lead.pending_reason == "LOCATION_PROVINCE_ONLY"
                dedup = None if province_only else submit_draft(db, lead=lead, principal=principal)
                write_audit(
                    db,
                    principal=principal,
                    action="V12_SUPPLIER_LEAD_BULK_IMPORT",
                    resource_type="lead",
                    resource_id=lead.id,
                    company_id=principal.company_id,
                    after={
                        "row_number": row.row_number,
                        "status": lead.status,
                        "duplicate_status": lead.duplicate_status,
                    },
                    reason="LOCATION_PROVINCE_ONLY" if province_only else dedup.decision.value,
                    request_id=request_id,
                )
                db.flush()
            success_count += 1
            if province_only:
                draft_count += 1
            results.append(
                {
                    "row_number": row.row_number,
                    "status": "DRAFT" if province_only else "SUCCESS",
                    "customer_name": lead.customer_name,
                    "lead_id": lead.id,
                    "lead_status": lead.status,
                    **({"message": "已保存待补资料草稿，补全城市后可提交"} if province_only else {}),
                }
            )
        except AppError as exc:
            results.append(
                {
                    "row_number": row.row_number,
                    "status": "FAILED",
                    "customer_name": row.customer_name,
                    "error_code": exc.code,
                    "message": exc.message,
                }
            )
        except ValidationError as exc:
            first_error = exc.errors()[0] if exc.errors() else {}
            results.append(
                {
                    "row_number": row.row_number,
                    "status": "FAILED",
                    "customer_name": row.customer_name,
                    "error_code": "SUPPLIER_IMPORT_ROW_INVALID",
                    "message": str(first_error.get("msg") or "导入行数据格式错误"),
                }
            )
    return {
        "total": len(rows),
        "success_count": success_count,
        "draft_count": draft_count,
        "failure_count": len(rows) - success_count,
        "results": results,
    }


def _first_worksheet_rows(workbook: ZipFile) -> list[list[str]]:
    workbook_root = ElementTree.fromstring(workbook.read("xl/workbook.xml"))
    first_sheet = workbook_root.find(f"{{{_XLSX_MAIN_NS}}}sheets/{{{_XLSX_MAIN_NS}}}sheet")
    if first_sheet is None:
        return []
    relationship_id = first_sheet.attrib.get(f"{{{_XLSX_REL_NS}}}id")
    if not relationship_id:
        return []
    relationships = ElementTree.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
    target = next(
        (
            item.attrib.get("Target")
            for item in relationships.findall(f"{{{_PACKAGE_REL_NS}}}Relationship")
            if item.attrib.get("Id") == relationship_id
        ),
        None,
    )
    if not target:
        return []
    sheet_path = target.lstrip("/")
    if not sheet_path.startswith("xl/"):
        sheet_path = f"xl/{sheet_path}"
    shared_strings = _shared_strings(workbook)
    sheet_root = ElementTree.fromstring(workbook.read(sheet_path))
    rows: list[list[str]] = []
    for row in sheet_root.findall(f".//{{{_XLSX_MAIN_NS}}}row"):
        values: list[str] = []
        for cell in row.findall(f"{{{_XLSX_MAIN_NS}}}c"):
            reference = cell.attrib.get("r", "A1")
            column = _column_index(reference)
            while len(values) < column - 1:
                values.append("")
            values.append(_cell_value(cell, shared_strings))
        rows.append(values)
    return rows


def _shared_strings(workbook: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in workbook.namelist():
        return []
    root = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
    return [
        "".join(node.text or "" for node in item.findall(f".//{{{_XLSX_MAIN_NS}}}t"))
        for item in root.findall(f"{{{_XLSX_MAIN_NS}}}si")
    ]


def _cell_value(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    if cell.attrib.get("t") == "inlineStr":
        return "".join(
            node.text or "" for node in cell.findall(f".//{{{_XLSX_MAIN_NS}}}t")
        )
    value = cell.findtext(f"{{{_XLSX_MAIN_NS}}}v", default="")
    if cell.attrib.get("t") == "s" and value:
        index = int(value)
        return shared_strings[index] if index < len(shared_strings) else ""
    return value


def _column_index(reference: str) -> int:
    index = 0
    for character in reference:
        if not character.isalpha():
            break
        index = index * 26 + ord(character.upper()) - ord("A") + 1
    return index
