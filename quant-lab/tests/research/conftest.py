"""G3 research 测试公共夹具：全部合成数据，零生产凭据。湖根目录指向 tmp（QUANT_LAB_DATA_ROOT）。"""
from __future__ import annotations

import os
import pathlib

import polars as pl
import pytest

from quant_lab.research import synthetic

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def _isolated_data_root(tmp_path_factory):
    """全部研究测试隔离湖根目录：任何测试都不得写仓库 data/（持久账本只给真实 run）。"""
    root = tmp_path_factory.mktemp("ql-research-root")
    os.environ[synthetic_env_name()] = str(root)
    yield root


@pytest.fixture(scope="session")
def fixtures_dir() -> pathlib.Path:
    return FIXTURES


@pytest.fixture
def lake_root(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """把研究湖根目录指到 tmp，避免测试写进仓库 data/。"""
    root = tmp_path / "lake-root"
    root.mkdir()
    monkeypatch.setenv(synthetic_env_name(), str(root))
    return root


def synthetic_env_name() -> str:
    from quant_lab.research.paths import ENV_DATA_ROOT
    return ENV_DATA_ROOT


@pytest.fixture(scope="session")
def fake_bars() -> pl.DataFrame:
    return synthetic.fake_bars(n_bars=4000, interval="15m", seed=1)


@pytest.fixture(scope="session")
def fake_bars_gapped() -> pl.DataFrame:
    return synthetic.fake_bars(n_bars=4000, interval="15m", seed=1, gap_frac=0.05)


@pytest.fixture(scope="session")
def fake_episodes(fake_bars: pl.DataFrame) -> pl.DataFrame:
    return synthetic.fake_episodes(240, span_days=40, seed=1, bars=fake_bars)


@pytest.fixture(scope="session")
def fake_execution(fake_episodes: pl.DataFrame) -> pl.DataFrame:
    """执行请求只对冻结机会集内的 episode 发（不合格的 entry_decision=False 不进执行）。"""
    from quant_lab.research.evaluator import freeze_opportunity_set
    ids = freeze_opportunity_set(fake_episodes).episode_ids
    return synthetic.fake_execution(fake_episodes.filter(pl.col("episode_id").is_in(ids)), seed=1)
