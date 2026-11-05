"""Generate a private VM environment file without printing secrets."""
import argparse
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit


def https_origin(value):
    parsed=urlsplit(value)
    if (parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('','/')):
        raise argparse.ArgumentTypeError('Indica una URL HTTPS sin rutas ni credenciales.')
    return f'https://{parsed.netloc}'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frontend-origin',required=True,type=https_origin)
    parser.add_argument('--api-origin',type=https_origin,required=True)
    args=parser.parse_args()
    target=Path(__file__).resolve().parent/'.env.vm'
    content='\n'.join([
        'POSTGRES_PASSWORD='+secrets.token_hex(32),
        'SECRET_KEY='+secrets.token_hex(32),
        'FRONTEND_ORIGIN='+args.frontend_origin,
        'PUBLIC_API_URL='+args.api_origin,
        'API_DOMAIN='+urlsplit(args.api_origin).hostname,
        'GEMINI_MODEL=gemini-3.1-flash-image',
        'GEMINI_API_KEY_1=', 'GEMINI_TEXT_MODEL=', 'LIBELULA_TEST_APPKEY=', '',
    ])
    # Exclusive creation preserves credentials if setup is accidentally repeated.
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as stream:
        stream.write(content)
    print('deploy/.env.vm creado con permisos privados. No compartas su contenido.')


if __name__=='__main__':main()
