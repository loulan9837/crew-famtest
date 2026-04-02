# -*- coding: utf-8 -*-
"""design_import_service：ZIP 展开与候选构建"""
import io
import zipfile

from design_import_service import build_candidates, format_size_cap, normalize_ext


def test_normalize_ext():
    assert normalize_ext("a.PNG") == "png"
    assert normalize_ext("x.zip") == "zip"


def test_format_size_cap():
    assert format_size_cap(1024 * 1024 * 1024) == "1GB"
    assert format_size_cap(30 * 1024 * 1024) == "30MB"


def test_build_candidates_single_png():
    c, f, total = build_candidates([{"name": "a.png", "bytes": b"\x89PNG\r\n\x1a\n"}])
    assert len(c) == 1
    assert c[0].ext == "png"
    assert total > 0
    assert not f or all("重复" not in x.get("reason", "") for x in f)


def test_build_candidates_zip_expands():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("sub/one.png", b"\x89PNG\r\n\x1a\nfake")
        zf.writestr("two.jpg", b"\xff\xd8\xff\xe0")  # minimal jpeg-like
    data = buf.getvalue()
    c, failed, total = build_candidates([{"name": "pack.zip", "bytes": data}])
    assert len(c) == 2
    names = {x.name for x in c}
    assert "pack.zip/one.png" in names
    assert "pack.zip/two.jpg" in names
    assert total == sum(len(x.data) for x in c)


def test_build_candidates_zip_empty_supported():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("readme.txt", b"hello")
    c, failed, _ = build_candidates([{"name": "only_txt.zip", "bytes": buf.getvalue()}])
    assert c == []
    assert any("无支持" in (x.get("reason") or "") for x in failed)
