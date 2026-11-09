"""Copias de seguridad y restauración de todo el sistema (base de datos + imágenes opcionales).

Formato .fsbak (tar.gz): manifest.json (versión de esquema Alembic, conteos y SHA-256),
data.json.gz (todas las tablas) y, opcionalmente, uploads.tar.gz (imágenes).
La restauración valida checksums y versión de esquema, guarda antes una copia de seguridad
automática y reemplaza los datos dentro de UNA transacción: si algo falla no cambia nada.
La bitácora viaja cifrada; la llave del desarrollador nunca se incluye en la copia.
"""
import gzip
import hashlib
import io
import json
import re
import shutil
import tarfile
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlalchemy as sa
from fastapi import HTTPException
from core.config import settings

FORMAT_VERSION = 1
NAME_PATTERN = re.compile(r'^fashionstore-[a-z0-9\-]{6,60}\.fsbak$')
SKIP_TABLES = {'alembic_version'}


def backup_dir() -> Path:
    path = Path(settings.BACKUP_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def uploads_dir() -> Path:
    return Path('uploads')


def _encode(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode('utf-8', errors='replace')
    raise TypeError(type(value).__name__)


def _revision(conn) -> str | None:
    try:
        return conn.execute(sa.text('SELECT version_num FROM alembic_version')).scalar()
    except Exception:
        return None


def _tables(engine):
    metadata = sa.MetaData()
    metadata.reflect(bind=engine)
    return metadata, [t for t in metadata.sorted_tables if t.name not in SKIP_TABLES]


def safe_name(name: str) -> Path:
    if not NAME_PATTERN.match(name or ''):
        raise HTTPException(400, 'Nombre de copia de seguridad inválido.')
    path = backup_dir() / name
    if not path.is_file():
        raise HTTPException(404, 'Copia de seguridad no encontrada.')
    return path


def create_backup(engine, include_uploads: bool = True, label: str = 'manual') -> dict:
    _, tables = _tables(engine)
    with engine.connect() as conn:
        revision = _revision(conn)
        data = {t.name: [dict(r) for r in conn.execute(sa.select(t)).mappings()] for t in tables}
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    name = f'fashionstore-{label}-{stamp}.fsbak'
    data_bytes = gzip.compress(json.dumps({'version': FORMAT_VERSION, 'tables': data}, default=_encode,
                                          ensure_ascii=False).encode())
    files = {'data.json.gz': data_bytes}
    if include_uploads:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            root = uploads_dir()
            if root.is_dir():
                for file in sorted(root.rglob('*')):
                    if file.is_file() and not file.is_symlink() and file.resolve().is_relative_to(root.resolve()):
                        archive.add(file, arcname=file.relative_to(root).as_posix(), recursive=False)
        files['uploads.tar.gz'] = buffer.getvalue()
    manifest = {
        'format': FORMAT_VERSION, 'created_at': datetime.now(timezone.utc).isoformat(), 'label': label,
        'schema_revision': revision, 'includes_uploads': include_uploads,
        'tables': {k: len(v) for k, v in data.items()},
        'checksums': {k: hashlib.sha256(v).hexdigest() for k, v in files.items()},
    }
    files = {'manifest.json': json.dumps(manifest, ensure_ascii=False, indent=2).encode(), **files}
    path = backup_dir() / name
    with tarfile.open(path, 'w:gz') as archive:
        for member, content in files.items():
            info = tarfile.TarInfo(member)
            info.size = len(content)
            info.mtime = int(datetime.now().timestamp())
            archive.addfile(info, io.BytesIO(content))
    path.chmod(0o600)
    return describe(path)


def _read(path: Path) -> tuple[dict, dict]:
    contents = {}
    try:
        with tarfile.open(path, 'r:gz') as archive:
            for member in archive.getmembers():
                if member.name not in ('manifest.json', 'data.json.gz', 'uploads.tar.gz') or not member.isfile():
                    raise HTTPException(400, 'La copia contiene archivos no permitidos.')
                contents[member.name] = archive.extractfile(member).read()
    except (tarfile.TarError, OSError, EOFError):
        raise HTTPException(400, 'El archivo no es una copia de seguridad válida de FashionStore.')
    if 'manifest.json' not in contents or 'data.json.gz' not in contents:
        raise HTTPException(400, 'La copia de seguridad está incompleta.')
    manifest = json.loads(contents.pop('manifest.json'))
    for member, digest in manifest.get('checksums', {}).items():
        if member not in contents or hashlib.sha256(contents[member]).hexdigest() != digest:
            raise HTTPException(400, f'Copia dañada: la verificación de {member} no coincide.')
    return manifest, contents


def describe(path: Path) -> dict:
    try:
        with tarfile.open(path, 'r:gz') as archive:
            manifest = json.loads(archive.extractfile('manifest.json').read())
    except Exception:
        manifest = {}
    return {'name': path.name, 'size_bytes': path.stat().st_size, 'created_at': manifest.get('created_at'),
            'label': manifest.get('label'), 'schema_revision': manifest.get('schema_revision'),
            'includes_uploads': manifest.get('includes_uploads', False),
            'rows': sum((manifest.get('tables') or {}).values())}


def list_backups() -> list[dict]:
    return [describe(p) for p in sorted(backup_dir().glob('fashionstore-*.fsbak'), reverse=True)]


def _convert(table, rows):
    converted = []
    for original in rows:
        row = {key: value for key, value in original.items() if key in table.c}
        for col in table.c:
            value = row.get(col.name)
            if value is None:
                continue
            if isinstance(col.type, sa.DateTime) and isinstance(value, str):
                row[col.name] = datetime.fromisoformat(value)
            elif isinstance(col.type, sa.Date) and isinstance(value, str):
                row[col.name] = date.fromisoformat(value)
            elif isinstance(col.type, sa.Boolean):
                row[col.name] = bool(value)
        converted.append(row)
    if converted:
        keys = set().union(*(r.keys() for r in converted))
        converted = [{k: r.get(k) for k in keys} for r in converted]
    return converted


def restore_backup(engine, path: Path) -> dict:
    manifest, contents = _read(path)
    payload = json.loads(gzip.decompress(contents['data.json.gz']))
    if payload.get('version') != FORMAT_VERSION:
        raise HTTPException(400, 'Versión de copia de seguridad no compatible.')
    metadata, tables = _tables(engine)
    with engine.connect() as conn:
        current_revision = _revision(conn)
    if manifest.get('schema_revision') != current_revision:
        raise HTTPException(409, f'La copia es de otra versión del sistema ({manifest.get("schema_revision")}); '
                                 f'la base actual está en {current_revision}. Actualiza el sistema a esa versión antes de restaurar.')
    if set(payload['tables']) != {t.name for t in tables}:
        raise HTTPException(409, 'Las tablas de la copia no coinciden con las del sistema actual.')

    safety = create_backup(engine, include_uploads=False, label='antes-de-restaurar')
    postgres = engine.dialect.name == 'postgresql'
    with engine.begin() as conn:
        if postgres:
            conn.execute(sa.text("SELECT pg_advisory_xact_lock(hashtext('fashionstore-restore'))"))
            names = ', '.join(conn.dialect.identifier_preparer.quote(t.name) for t in tables)
            conn.execute(sa.text(f'TRUNCATE TABLE {names} RESTART IDENTITY CASCADE'))
        else:
            conn.exec_driver_sql('PRAGMA foreign_keys=OFF')
            for table in reversed(tables):
                conn.execute(table.delete())
        restored = {}
        for table in tables:
            rows = _convert(table, payload['tables'][table.name])
            for offset in range(0, len(rows), 500):
                conn.execute(table.insert(), rows[offset:offset + 500])
            restored[table.name] = len(rows)
            if postgres and 'id' in table.c and isinstance(table.c.id.type, sa.Integer):
                sequence = conn.execute(sa.text('SELECT pg_get_serial_sequence(:t, :c)'), {'t': table.name, 'c': 'id'}).scalar()
                if sequence:
                    maximum = conn.execute(sa.select(sa.func.max(table.c.id))).scalar()
                    conn.execute(sa.text('SELECT setval(CAST(:s AS regclass), :v, :u)'),
                                 {'s': sequence, 'v': maximum or 1, 'u': maximum is not None})
        if not postgres:
            conn.exec_driver_sql('PRAGMA foreign_keys=ON')

    images = 0
    if 'uploads.tar.gz' in contents:
        root = uploads_dir()
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='fashionstore-restore-') as temp:
            staging = Path(temp)
            with tarfile.open(fileobj=io.BytesIO(contents['uploads.tar.gz']), mode='r:gz') as archive:
                for member in archive.getmembers():
                    target = (staging / member.name).resolve()
                    if not member.isfile() or not target.is_relative_to(staging.resolve()):
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as src, target.open('wb') as dst:
                        shutil.copyfileobj(src, dst)
            for file in staging.rglob('*'):
                if file.is_file():
                    dest = (root / file.relative_to(staging)).resolve()
                    if dest.is_relative_to(root.resolve()):
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(file, dest)
                        images += 1
    return {'restored_rows': sum(restored.values()), 'tables': restored, 'images_restored': images,
            'safety_backup': safety['name'], 'backup_created_at': manifest.get('created_at')}
