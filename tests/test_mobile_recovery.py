import importlib.util
import json
import re
import sqlite3
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2] / 'Mobil_Si2/fashionstore_mobile'
spec = importlib.util.spec_from_file_location('mobile_recovery', ROOT / 'tools/recover_legacy_queue.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


@pytest.fixture
def files(tmp_path):
    source=tmp_path/'fashionstore_mobile.db'
    target=tmp_path/'fashionstore_account_2_20.db'
    ddl=(ROOT/'lib/core/db/database_helper.dart').read_text(encoding='utf-8')
    # Execute actual application DDL, not a duplicated test schema.
    for file in (source,target):
        with sqlite3.connect(file) as db:
            for sql in re.findall(r"await db.execute\('''(.*?)'''\)",ddl,re.S):db.execute(sql)
            db.execute('CREATE TABLE cache_metadata(id INTEGER PRIMARY KEY,branch_id INTEGER,server_timestamp TEXT)')
            db.execute('PRAGMA user_version=3')
            db.execute("INSERT INTO branches(id,tenant_id,name,address,is_active) VALUES(7,2,'Test','',1)")
            db.execute("INSERT INTO products(id,tenant_id,name,base_price,is_active) VALUES(1,2,'Shirt',120,1)")
            db.execute("INSERT INTO variants(id,product_id,sku,size,color,price,stock) VALUES(10,1,'TEST','M','Blue',120,5)")
            db.execute("INSERT INTO cache_metadata VALUES(1,7,'now')")
    with sqlite3.connect(source) as db:
        db.execute("INSERT INTO sales(client_operation_uuid,tenant_id,branch_id,user_id,payment_method,total_amount,created_at) VALUES('legacy-operation',2,7,20,'efectivo',240,'now')")
        db.execute("INSERT INTO sale_items(client_operation_uuid,variant_id,quantity,unit_price) VALUES('legacy-operation',10,2,120)")
        db.execute("INSERT INTO sync_operations(client_operation_uuid,tenant_id,user_id,branch_id,operation_type,payload,created_at,updated_at,sale_uuid) VALUES('legacy-operation',1,1,1,'sale',?,'now','now','legacy-operation')",(json.dumps({'client_operation_uuid':'legacy-operation','items':[{'variant_id':10,'quantity':2,'unit_price':120}]}),))
    assignment=dict(client_operation_uuid='legacy-operation',tenant_id=2,owner_id=20,branch_id=7,customer_id=20,verified=True)
    return source,target,assignment


def test_verified_recovery_ignores_bad_queue_defaults_and_preserves_source(files):
    source,target,assignment=files
    before=source.read_bytes()
    recovery.recover(source,target,[assignment])
    assert source.read_bytes()==before
    with sqlite3.connect(target) as db:
        assert db.execute('SELECT tenant_id,user_id,branch_id FROM sync_operations').fetchone()==(2,20,7)
        assert db.execute('SELECT COUNT(*) FROM cache_metadata').fetchone()[0]==0
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    recovery.recover(source,target,[assignment])
    with sqlite3.connect(target) as db:assert db.execute('SELECT COUNT(*) FROM sales').fetchone()[0]==1


def test_unverified_context_is_never_assigned(files):
    source,target,assignment=files
    assignment['verified']=False
    with pytest.raises(ValueError):recovery.recover(source,target,[assignment])
    with sqlite3.connect(target) as db:assert db.execute('SELECT COUNT(*) FROM sales').fetchone()[0]==0
