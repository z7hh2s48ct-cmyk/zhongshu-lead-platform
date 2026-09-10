from sqlalchemy import select

from apps.api.src.core.models import Lead, LeadImportIssue, Region
from apps.api.src.integrations.feishu import FeishuRecord
from apps.api.src.services.lead_service import import_records


def mapping():
    return {"customer_name":"客户姓名","phone":"手机号","city":"市","region_code":"地区编码","source_channel":"来源渠道"}


def test_feishu_import_is_idempotent_and_rejects_duplicate_phone(db) -> None:
    db.add(Region(code="310100", name="上海市", level="CITY", aliases=["上海"]))
    db.commit()
    records = [FeishuRecord("rec1", {"客户姓名":"张先生","手机号":"13800138000","市":"上海市","地区编码":"310100","来源渠道":"抖音"})]
    batch = import_records(db, records, mapping(), app_token="app", table_id="table")
    db.commit()
    assert batch.success_count == 1
    assert len(db.scalars(select(Lead)).all()) == 1

    import_records(db, records, mapping(), app_token="app", table_id="table")
    db.commit()
    assert len(db.scalars(select(Lead)).all()) == 1

    duplicate_batch = import_records(db, [FeishuRecord("rec2", {"客户姓名":"张先生2","手机号":"13800138000","市":"上海市","地区编码":"310100"})], mapping(), app_token="app", table_id="table")
    db.commit()
    assert duplicate_batch.error_count == 1
    assert len(db.scalars(select(Lead)).all()) == 1
    assert any("LEAD_PHONE_DUPLICATE" in issue.message for issue in db.scalars(select(LeadImportIssue)).all())


def test_invalid_import_creates_issue(db) -> None:
    batch = import_records(db, [FeishuRecord("bad", {"客户姓名":"","手机号":"123","市":""})], mapping(), app_token="app", table_id="table")
    db.commit()
    lead = db.scalar(select(Lead).where(Lead.source_record_id == "bad"))
    assert batch.error_count == 1
    assert lead.status == "IMPORT_ERROR"
