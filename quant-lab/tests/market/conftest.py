"""G2 market 测试公共夹具。只用合成数据与公开归档，零生产凭据。"""
from __future__ import annotations

import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
EPISODES = FIXTURES / "episodes"


@pytest.fixture(scope="session")
def fixtures_dir() -> pathlib.Path:
    return FIXTURES


@pytest.fixture(scope="session")
def episodes_dir() -> pathlib.Path:
    return EPISODES


@pytest.fixture
def lake_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "lake" / "market"
    d.mkdir(parents=True)
    return d
