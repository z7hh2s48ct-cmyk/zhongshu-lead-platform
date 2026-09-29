from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import select

from apps.api.src.core.models import AuditLog, Lead
from apps.api.src.core.security import decrypt_text
from apps.api.src.core.v12_enums import LeadSourceKind
from apps.api.src.services.xlsx_writer import WorksheetSpec, write_xlsx
from apps.api.src.services.supplier_lead_import_v12 import parse_supplier_import_xlsx
from apps.api.tests.test_v12_supplier_draft_http import (
    _approve_supplier_capability,
    _data,
    _login,
)
from apps.api.tests.xlsx_reader import read_xlsx


def _workbook(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    path = tmp_path / "supplier-leads.xlsx"
    headers = [
        "客户姓名",
        "手机号",
        "客户微信号",
        "所在省",
        "所在市",
        "所在区县",
        "地区编码",
        "客户需求",
        "已获客户授权",
    ]
    write_xlsx(
        path,
        [
            WorksheetSpec(
                name="客资导入",
                fieldnames=headers,
                rows=rows,
                rows_per_sheet=200,
                total_rows=len(rows),
            )
        ],
        heartbeat=lambda: None,
        heartbeat_row_interval=50,
        max_uncompressed_bytes=2 * 1024 * 1024,
    )
    return path


def test_supplier_can_download_bulk_import_template(api_client) -> None:
    client, factory = api_client
    _approve_supplier_capability(factory)
    supplier = _login(client, "franchise_demo", "Franchise123!")

    response = client.get(
        "/api/v1/v1.2/supplier/leads/import-template",
        headers=supplier,
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "supplier-leads-import-template.xlsx" in response.headers[
        "content-disposition"
    ]
    workbook = read_xlsx(response.content)
    assert list(workbook) == ["客资导入模板"]
    assert set(workbook["客资导入模板"][0]) >= {
        "客户姓名",
        "手机号",
        "地区编码",
        "客户需求",
        "已获客户授权",
    }


def test_supplier_bulk_import_preserves_province_only_draft(api_client, tmp_path: Path) -> None:
    client, factory = api_client
    _approve_supplier_capability(factory)
    supplier = _login(client, "franchise_demo", "Franchise123!")
    workbook = _workbook(tmp_path, [{
        "客户姓名": "仅知省份客户",
        "客户微信号": "wx-province-import-929",
        "所在省": "四川省",
        "已获客户授权": "是",
    }])

    with workbook.open("rb") as upload:
        response = client.post(
            "/api/v1/v1.2/supplier/leads/import",
            headers=supplier,
            files={"file": (workbook.name, upload, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )

    data = _data(response)
    assert data["success_count"] == 1
    assert data["failure_count"] == 0
    assert data["draft_count"] == 1
    assert data["results"][0]["status"] == "DRAFT"
    with factory() as db:
        lead = db.scalar(select(Lead).where(Lead.customer_name == "仅知省份客户"))
        assert lead is not None
        assert lead.status == "DRAFT"
        assert lead.pending_reason == "LOCATION_PROVINCE_ONLY"


def test_supplier_bulk_import_submits_valid_rows_and_reports_invalid_rows(
    api_client,
    tmp_path: Path,
) -> None:
    client, factory = api_client
    company_id, _supplier_user_id = _approve_supplier_capability(factory)
    supplier = _login(client, "franchise_demo", "Franchise123!")
    workbook = _workbook(
        tmp_path,
        [
            {
                "客户姓名": "批量导入成功客户",
                "手机号": "13900139921",
                "所在省": "上海市",
                "所在市": "上海市",
                "所在区县": "黄浦区",
                "地区编码": "310101",
                "客户需求": "计划建设两层自住房",
                "已获客户授权": "是",
            },
            {
                "客户姓名": "批量导入失败客户",
                "手机号": "123",
                "所在省": "浙江省",
                "所在市": "杭州市",
                "客户需求": "咨询改建",
                "已获客户授权": "是",
            },
        ],
    )

    with workbook.open("rb") as upload:
        response = client.post(
            "/api/v1/v1.2/supplier/leads/import",
            headers=supplier,
            files={
                "file": (
                    workbook.name,
                    upload,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )

    data = _data(response)
    assert data["total"] == 2
    assert data["success_count"] == 1
    assert data["failure_count"] == 1
    assert data["results"][0]["row_number"] == 2
    assert data["results"][0]["status"] == "SUCCESS"
    assert data["results"][0]["lead_id"]
    assert data["results"][1] == {
        "row_number": 3,
        "status": "FAILED",
        "customer_name": "批量导入失败客户",
        "error_code": "LEAD_PHONE_INVALID",
        "message": "手机号格式错误",
    }

    with factory() as db:
        imported = list(
            db.scalars(
                select(Lead).where(
                    Lead.supplier_company_id == company_id,
                    Lead.customer_name.in_([
                        "批量导入成功客户",
                        "批量导入失败客户",
                    ]),
                )
            ).all()
        )
        assert [lead.customer_name for lead in imported] == ["批量导入成功客户"]
        assert imported[0].source_kind == LeadSourceKind.SUPPLIER_H5.value
        assert imported[0].status != "DRAFT"
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "V12_SUPPLIER_LEAD_BULK_IMPORT",
                AuditLog.resource_id == imported[0].id,
            )
        )
        assert audit is not None
        assert audit.after_json["row_number"] == 2


def test_supplier_bulk_import_rejects_unknown_template_headers(
    api_client,
    tmp_path: Path,
) -> None:
    client, factory = api_client
    _approve_supplier_capability(factory)
    supplier = _login(client, "franchise_demo", "Franchise123!")
    workbook = _workbook(tmp_path, [{"客户姓名": "无手机列"}])
    # Rebuild a workbook that has no supported contact column at all.
    write_xlsx(
        workbook,
        [
            WorksheetSpec(
                name="错误模板",
                fieldnames=["客户姓名", "备注"],
                rows=[{"客户姓名": "无联系方式", "备注": "测试"}],
                rows_per_sheet=200,
                total_rows=1,
            )
        ],
        heartbeat=lambda: None,
        heartbeat_row_interval=50,
        max_uncompressed_bytes=2 * 1024 * 1024,
    )

    with workbook.open("rb") as upload:
        response = client.post(
            "/api/v1/v1.2/supplier/leads/import",
            headers=supplier,
            files={"file": (workbook.name, upload)},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "SUPPLIER_IMPORT_HEADER_INVALID"


def test_supplier_bulk_import_accepts_wechat_only_contact(
    api_client,
    tmp_path: Path,
) -> None:
    client, factory = api_client
    _approve_supplier_capability(factory)
    supplier = _login(client, "franchise_demo", "Franchise123!")
    workbook = _workbook(
        tmp_path,
        [
            {
                "客户姓名": "微信批量导入客户",
                "客户微信号": "Wx_Bulk_929",
                "客户需求": "咨询自建房方案",
                "已获客户授权": "是",
            }
        ],
    )

    with workbook.open("rb") as upload:
        data = _data(
            client.post(
                "/api/v1/v1.2/supplier/leads/import",
                headers=supplier,
                files={"file": (workbook.name, upload)},
            )
        )

    assert data["success_count"] == 1
    with factory() as db:
        lead = db.get(Lead, data["results"][0]["lead_id"])
        assert lead is not None
        assert lead.phone_encrypted is None
        assert decrypt_text(lead.customer_wechat_encrypted) == "Wx_Bulk_929"


def test_supplier_h5_exposes_excel_bulk_import_and_partial_failure_results() -> None:
    source = Path("apps/h5/public/v12-workbench.js").read_text(encoding="utf-8")

    assert 'id="supply-bulk-import"' in source
    assert "/v1.2/supplier/leads/import-template" in source
    assert "/v1.2/supplier/leads/import" in source
    assert 'accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"' in source
    assert "每次最多 200 条" in source
    assert "成功保存" in source
    assert "待补草稿" in source
    assert "仅知省份的行保存为待补草稿" in source
    assert "失败行不会保存" in source


def test_supplier_import_reader_supports_excel_shared_strings(tmp_path: Path) -> None:
    path = tmp_path / "shared-strings.xlsx"
    shared = ["客户姓名", "手机号", "已获客户授权", "共享字符串客户", "是"]
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as workbook:
        workbook.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="客资" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        workbook.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        workbook.writestr(
            "xl/sharedStrings.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            + "".join(f"<si><t>{value}</t></si>" for value in shared)
            + "</sst>",
        )
        workbook.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>3</v></c><c r="B2"><v>13900139922</v></c><c r="C2" t="s"><v>4</v></c></row>'
            '</sheetData></worksheet>',
        )

    rows = parse_supplier_import_xlsx(path.read_bytes())

    assert rows[0].values == {
        "customer_name": "共享字符串客户",
        "phone": "13900139922",
        "consent_confirmed": True,
    }


def test_supplier_import_rejects_oversized_column_before_allocating_rows() -> None:
    import pytest
    from apps.api.src.services.supplier_lead_import_v12 import _column_index

    with pytest.raises(ValueError, match="column"):
        _column_index("ZZZZZZ1")


def test_supplier_import_accepts_last_template_column() -> None:
    from apps.api.src.services.supplier_lead_import_v12 import _column_index

    assert _column_index("M201") == 13


def test_supplier_import_rejects_xml_entities(tmp_path: Path) -> None:
    from io import BytesIO
    import pytest
    from apps.api.src.core.errors import AppError

    path = _workbook(tmp_path, [{"手机号": "13900139922"}])
    output = BytesIO()
    with ZipFile(path) as source, ZipFile(output, "w") as target:
        for name in source.namelist():
            content = source.read(name)
            if name == "xl/workbook.xml":
                content = content.replace(b"<workbook ", b'<!DOCTYPE workbook [<!ENTITY payload "test">]><workbook ', 1)
            target.writestr(name, content)
    with pytest.raises(AppError) as error:
        parse_supplier_import_xlsx(output.getvalue())
    assert error.value.code == "SUPPLIER_IMPORT_FILE_INVALID"


def test_supplier_import_stops_collecting_rows_at_limit(tmp_path: Path) -> None:
    from io import BytesIO
    import pytest
    from apps.api.src.services.supplier_lead_import_v12 import _first_worksheet_rows

    path = _workbook(tmp_path, [])
    output = BytesIO()
    with ZipFile(path) as source, ZipFile(output, "w") as target:
        for name in source.namelist():
            content = source.read(name)
            if name == "xl/worksheets/sheet1.xml":
                data_row = b'<row><c r="A2" t="inlineStr"><is><t>test</t></is></c></row>'
                content = content.replace(b"</sheetData>", data_row * 201 + b"</sheetData>")
            target.writestr(name, content)
    with ZipFile(output) as workbook, pytest.raises(ValueError, match="rows"):
        _first_worksheet_rows(workbook)
