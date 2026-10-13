"""Verify shipped procedural artwork and portable model exports, without a GPU."""
import importlib.util
import json
import math
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'bioharness/ui/assets/voxel'
CATALOG = json.loads((ASSETS / 'models.json').read_text())


def test_every_model_has_valid_geometry_and_local_textures():
    assert len(CATALOG['models']) == 22
    for boxes in CATALOG['models'].values():
        assert boxes
        for *numbers, material in boxes:
            assert len(numbers) == 6 and all(math.isfinite(n) for n in numbers)
            assert all(n > 0 for n in numbers[3:])
            assert material in CATALOG['materials']
            png = (ASSETS / f'{material}.png').read_bytes()
            assert png[:8] == b'\x89PNG\r\n\x1a\n'
            assert struct.unpack('>II', png[16:24]) == (16, 16)


def test_glb_exports_embed_geometry_and_textures():
    for name, boxes in CATALOG['models'].items():
        raw = (ASSETS / f'{name}.glb').read_bytes()
        magic, version, length = struct.unpack_from('<III', raw)
        assert (magic, version, length) == (0x46546C67, 2, len(raw))
        size, kind = struct.unpack_from('<II', raw, 12)
        assert kind == 0x4E4F534A
        doc = json.loads(raw[20:20+size])
        bin_size, bin_kind = struct.unpack_from('<II', raw, 20+size)
        binary = raw[28+size:]
        assert bin_kind == 0x004E4942 and bin_size == len(binary)
        assert len(doc['nodes']) == len(boxes)
        assert doc['asset']['version'] == '2.0'
        for view in doc['bufferViews']:
            assert view['byteOffset'] % 4 == 0
            assert view['byteOffset'] + view['byteLength'] <= len(binary)
        for image in doc['images']:
            view = doc['bufferViews'][image['bufferView']]
            assert binary[view['byteOffset']:view['byteOffset']+8] == b'\x89PNG\r\n\x1a\n'
        assert doc['samplers'][0]['magFilter'] == 9728


def test_generator_is_reproducible():
    spec = importlib.util.spec_from_file_location('voxel_generator', ROOT / 'tools/generate_voxel_assets.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.MODELS == CATALOG['models']
    for name, color in module.PALETTE.items():
        assert module.texture(name, color) == (ASSETS / f'{name}.png').read_bytes()
    for name, boxes in module.MODELS.items():
        assert module.export_glb(boxes) == (ASSETS / f'{name}.glb').read_bytes()
