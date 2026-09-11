"""quant_lab.research —— G3 研究协议与表达式引擎（契约 contracts/feature-snapshot.md）。

模块划分：
- ast        JSON AST schema、lint 硬门、规范化与 canonical_hash（§1）
- ops        算子登记表 REGISTRY 与 OpSpec（§2）
- backends   polars / polars_ta 两后端（§2 backends）
- features   feature_snapshot：as-of 对齐（close_time <= t_dec 等号）、validity、缓存 key（§3）
- evaluator  OpportunitySet / evaluate / EvalResult：簇均权 θ、NaN→skip、删失排除（§4、§7.4）
- protocol   walk_forward / PurgedKFold(t1)（§5）
- ledger     尝试账本 data/lockbox/ledger.parquet（§5，路径经 QUANT_LAB_DATA_ROOT）
- maxt       共同日历块 max-t bootstrap（§5）
- nullmodel  整块残差重采样空模型 + FPR/功效验收（§5）
- tiers      分档报告 K / DEFF / p 硬顶（§5）
- grammar    封顶小语法枚举生成器（§6）
- api        run_protocol / enumerate_grammar（§6）
- synthetic  合成夹具生成器（fake episodes / bars / execution），P1 全部在合成数据上完成

铁律：AST 只收 JSON，不执行外来字符串（不调用 DEAP compile/from_string、不用 Qlib eval）；只读 G1 决策图；
账本每次评估前写入，失败也留终态；结论措辞按档位；零生产凭据；不 import services/*。
"""

RESEARCH_SCHEMA_VERSION = "g3-research-v0"

__all__ = ["RESEARCH_SCHEMA_VERSION"]
