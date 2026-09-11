"""G1 数据系统测试公共夹具。所有测试只用合成数据，零生产凭据；湖根目录一律指向 tmp（QUANT_LAB_DATA_ROOT）。"""
from __future__ import annotations

import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> pathlib.Path:
    return FIXTURES


@pytest.fixture
def out_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "lake"
    d.mkdir()
    return d


@pytest.fixture
def lake(tmp_path: pathlib.Path, monkeypatch) -> pathlib.Path:
    """把研究湖根目录指到临时目录，避免测试写进 quant-lab/data。"""
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("QUANT_LAB_DATA_ROOT", str(root))
    return root
