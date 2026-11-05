"""Private local export and empty-destination PostgreSQL import. Never resets tables."""
import argparse
from datetime import date,datetime
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import sqlalchemy as sa
from db.database import engine


def encode(value):
    if isinstance(value,(datetime,date)):return value.isoformat()
    if isinstance(value,Decimal):return float(value)
    raise TypeError(type(value).__name__)


def recover_missing_historical_variants(data):
    known={r['id'] for r in data['product_variants']}
    missing={}
    for table,parent,key in [('reservation_items','reservations','reservation_id'),('order_items','orders','order_id')]:
        parents={r['id']:r for r in data[parent]}
        for item in data[table]:
            if item['variant_id'] in known:continue
            tenant=item.get('tenant_id') or parents[item[key]].get('tenant_id')
            if tenant is None:raise RuntimeError('No se puede atribuir una variante histórica a una empresa.')
            entry=missing.setdefault(item['variant_id'],{'tenants':set(),'price':item.get('unit_price') or 0})
            entry['tenants'].add(tenant)
    next_id=max((r['id'] for r in data['products']),default=0)+1
    for variant_id,entry in missing.items():
        if len(entry['tenants'])!=1:raise RuntimeError('Variante histórica referenciada por empresas distintas; requiere revisión.')
        tenant=next(iter(entry['tenants']))
        # Explicitly archived reconstruction, never a new sellable catalog item.
        data['products'].append(dict(id=next_id,tenant_id=tenant,name=f'[Histórico recuperado] Prenda eliminada, variante {variant_id}',
            description='Referencia histórica recuperada durante migración. No se conservan talla, color ni imagen originales.',
            base_price=entry['price'],is_active=False))
        data['product_variants'].append(dict(id=variant_id,tenant_id=tenant,product_id=next_id,
            sku=f'HISTORICAL-t{tenant}-v{variant_id}',size='Sin registro',color='Sin registro'))
        next_id+=1
    return len(missing)


def export_bundle(destination,source=engine,uploads=Path('uploads')):
    if destination.exists():raise RuntimeError('El directorio de exportación ya existe.')
    destination.mkdir(parents=True,mode=0o700)
    metadata=sa.MetaData();metadata.reflect(bind=source)
    tables=[t for t in metadata.sorted_tables if t.name!='alembic_version']
    with source.connect() as conn:
        data={t.name:[dict(r) for r in conn.execute(sa.select(t)).mappings()] for t in tables}
    recovered=recover_missing_historical_variants(data)
    with gzip.open(destination/'data.json.gz','wt',encoding='utf-8') as stream:
        json.dump({'version':1,'tables':data},stream,default=encode,ensure_ascii=False)
    with tarfile.open(destination/'uploads.tar.gz','w:gz') as archive:
        if uploads.is_dir():
            for file in sorted(uploads.rglob('*')):
                if file.is_file() and not file.is_symlink() and file.resolve().is_relative_to(uploads.resolve()):
                    archive.add(file,arcname=file.relative_to(uploads).as_posix(),recursive=False)
    checks={name:hashlib.sha256((destination/name).read_bytes()).hexdigest() for name in ('data.json.gz','uploads.tar.gz')}
    (destination/'checksums.json').write_text(json.dumps(checks),encoding='utf-8')
    for file in destination.iterdir():file.chmod(0o600)
    print(json.dumps({'exported_rows':{k:len(v) for k,v in data.items()},'recovered_archived_variants':recovered,'image_archive_ready':True}))


def import_bundle(directory,target=engine,uploads=Path('uploads')):
    if target.dialect.name!='postgresql':raise RuntimeError('El destino debe ser PostgreSQL.')
    checks=json.loads((directory/'checksums.json').read_text())
    for name in ('data.json.gz','uploads.tar.gz'):
        if hashlib.sha256((directory/name).read_bytes()).hexdigest()!=checks[name]:raise RuntimeError('Paquete dañado: '+name)
    with gzip.open(directory/'data.json.gz','rt',encoding='utf-8') as stream:payload=json.load(stream)
    if payload['version']!=1:raise RuntimeError('Versión de paquete no compatible.')
    metadata=sa.MetaData();metadata.reflect(bind=target)
    tables=[t for t in metadata.sorted_tables if t.name!='alembic_version']
    if set(payload['tables'])!={t.name for t in tables}:raise RuntimeError('Los esquemas no coinciden. Aplica las migraciones antes de importar.')
    copied=[]
    with tempfile.TemporaryDirectory(prefix='fashionstore-import-') as temp:
        staging=Path(temp)
        with tarfile.open(directory/'uploads.tar.gz','r:gz') as archive:
            for member in archive:
                path=(staging/member.name).resolve()
                if not path.is_relative_to(staging.resolve()) or not member.isfile():raise RuntimeError('Archivo de imágenes inválido.')
                path.parent.mkdir(parents=True,exist_ok=True)
                with archive.extractfile(member) as src,path.open('wb') as dst:shutil.copyfileobj(src,dst)
        try:
            with target.begin() as conn:
                conn.execute(sa.text("SELECT pg_advisory_xact_lock(hashtext('fashionstore-import'))"))
                for table in tables:
                    rows=conn.execute(sa.select(table)).mappings().all()
                    if not rows:continue
                    # The enterprise migration inserts this single initial plan.
                    if table.name=='subscription_plans' and len(rows)==1 and rows[0]['code']=='enterprise' and rows[0]['max_branches']==30 and rows[0]['price']==199.99:
                        continue
                    raise RuntimeError('Destino con datos: '+table.name+'. No se sobrescribió nada.')
                if 'subscription_plans' in metadata.tables:
                    conn.execute(metadata.tables['subscription_plans'].delete())
                for table in tables:
                    rows=[]
                    for original in payload['tables'][table.name]:
                        row={key:value for key,value in original.items() if key in table.c}
                        for col in table.c:
                            value=row.get(col.name)
                            if value is None:continue
                            if isinstance(col.type,sa.DateTime) and isinstance(value,str):row[col.name]=datetime.fromisoformat(value)
                            elif isinstance(col.type,sa.Date) and isinstance(value,str):row[col.name]=date.fromisoformat(value)
                            elif isinstance(col.type,sa.Boolean):row[col.name]=bool(value)
                        rows.append(row)
                    if rows:
                        keys=set().union(*(row.keys() for row in rows))
                        rows=[{key:row.get(key) for key in keys} for row in rows]
                    for offset in range(0,len(rows),500):conn.execute(table.insert(),rows[offset:offset+500])
                    if 'id' in table.c and isinstance(table.c.id.type,sa.Integer):
                        sequence=conn.execute(sa.text('SELECT pg_get_serial_sequence(:table,:column)'),{'table':table.name,'column':'id'}).scalar()
                        if sequence:
                            maximum=conn.execute(sa.select(sa.func.max(table.c.id))).scalar()
                            conn.execute(sa.text('SELECT setval(CAST(:sequence AS regclass),:value,:used)'),{'sequence':sequence,'value':maximum or 1,'used':maximum is not None})
                    count=conn.execute(sa.select(sa.func.count()).select_from(table)).scalar()
                    if count!=len(rows):raise RuntimeError('Conteo incorrecto: '+table.name)
                uploads.mkdir(parents=True,exist_ok=True)
                for file in staging.rglob('*'):
                    if not file.is_file():continue
                    dest=(uploads/file.relative_to(staging)).resolve()
                    if not dest.is_relative_to(uploads.resolve()):raise RuntimeError('Ruta inválida.')
                    if dest.exists():
                        if dest.read_bytes()!=file.read_bytes():raise RuntimeError('Ya existe otra imagen: '+str(file.relative_to(staging)))
                        continue
                    dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(file,dest);copied.append(dest)
        except Exception:
            for file in copied:file.unlink(missing_ok=True)
            raise
    print(json.dumps({'imported_rows':{k:len(v) for k,v in payload['tables'].items()},'images_imported':len(copied)}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['export','import'])
    parser.add_argument('directory',type=Path)
    args=parser.parse_args()
    if args.action=='export':export_bundle(args.directory)
    else:import_bundle(args.directory)


if __name__=='__main__':main()
