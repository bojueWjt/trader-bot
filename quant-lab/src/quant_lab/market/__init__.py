"""quant_lab.market —— G2 行情湖与永续执行内核。

模块划分（契约 execution-interface.md）：
- vision           Binance Vision 公开归档下载器 + per-partition manifest（§1）
- partition_check  分区体检与 quarantine（validity mask：ohlc_valid / spike_flag / gap_flag）
- asof             三时钟 as-of 库：asof_join / last_closed_bar / mark_price_at（§2）
- contract         ExecutionRequest / ExecutionResult pydantic schema + 不变量断言（§3）
- kernel_a         候选 A：自研永续参考实现
- nautilus_adapter 候选 B：Nautilus 1.227.0 SimulationModule spike
- execution        simulate / simulate_batch 对外接口（§3）

铁律：零生产凭据，不连交易所私有 API，不 import services/*；尖刺只标不删，缺 bar 不插值。
"""

EXECUTION_CONTRACT_VERSION = "g2-exec-v0"
MARKET_SCHEMA_VERSION = "g2-market-v0"

__all__ = ["EXECUTION_CONTRACT_VERSION", "MARKET_SCHEMA_VERSION"]
