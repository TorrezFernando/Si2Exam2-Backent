import importlib.util
from pathlib import Path
import pytest


def test_generated_models_and_sqlite_diagrams_do_not_drift():
    folder=Path(__file__).resolve().parents[2]/'docs/iconix'
    if not (folder/'generate_schema.py').is_file():
        pytest.skip('Los artefactos ICONIX están en docs/iconix del workspace completo, fuera del repositorio backend.')
    spec=importlib.util.spec_from_file_location('iconix_generator',folder/'generate_schema.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    for name,content in module.render().items():
        assert (folder/name).read_text(encoding='utf-8')==content, f'Regenerate diagram {name}'
