# 已退役：断言的是 G0 R-10 裁定 §2.2/§2.3 移除的「通用运行状态反射」。
# 保留为历史记录（A43：裁决只关分歧不关发现），不参与测试套。

def test_R6H_receipt_rejects_non_mcresult_payload():
    """回执里 result 为 None 或非 MCResult 时不得放行（六审 D 指出的载荷校验漏洞）。"""
    frozen = "a" * 64
    for bad in ({"result": None, "digest_start": frozen, "digest_end": frozen},
                {"result": "not-a-result", "digest_start": frozen, "digest_end": frozen}):
        with pytest.raises(SystemExit):
            NM.check_worker_receipt(bad, frozen)

@pytest.mark.parametrize("damage", ["receipts_missing", "exec_field_missing", "exec_forged",
                                    "worker_exec_mixed", "worker_disk_mismatch", "receipts_zero"])
def test_R6H_executing_revision_digest_detects_resident_injection():
    """R6-H：磁盘摘要只回答"文件长什么样"，回答不了"这个进程在跑哪一版"。"""
    d0 = NM.executing_code_digest()
    assert d0 == NM.executing_code_digest()                      # 同进程稳定
    orig = NM.run_mc
    try:
        _inject_resident_revision()
        assert NM.executing_code_digest() != d0                  # 注入即变
        assert NM.research_code_digest() == NM.research_code_digest()   # 磁盘摘要完全没动
    finally:
        NM.run_mc = orig
    assert NM.executing_code_digest() == d0                      # 还原即回

def test_R6H_real_workers_with_resident_revision_are_refused():
    """两个真实 OS worker、磁盘摘要全等、零异常，但执行的是另一修订——必须拒绝发布。"""
    frozen, frozen_exec = NM.research_code_digest(), NM.executing_code_digest()
    kw = dict(mechanism="common_shock", kind="null", n_rep=1, seed0=1,
              world_cfg_data=NM._config_to_data(NM.WorldConfig(n_clusters=150)), pipe_cfg_data=NM._config_to_data(PipelineConfig(B=100)))

    with ProcessPoolExecutor(max_workers=2) as ex:               # 稳定对照：照常发布
        good = [f.result() for f in [ex.submit(NM._run_mc_job, **kw) for _ in range(2)]]
    for p in good:
        assert p["digest_start"] == frozen and p["exec_start"] == frozen_exec
        assert NM.check_worker_receipt(p, frozen, frozen_exec) is p["result"]

    with ProcessPoolExecutor(max_workers=2, initializer=_inject_resident_revision) as ex:
        bad = [f.result() for f in [ex.submit(NM._run_mc_job, **kw) for _ in range(2)]]
    for p in bad:
        assert p["digest_start"] == frozen and p["digest_end"] == frozen      # 磁盘视图完全一致
        assert p["result"].label == "R6-cached-revision"                      # 确实跑了另一修订
        with pytest.raises(SystemExit, match="不是被冻结的那一版"):
            NM.check_worker_receipt(p, frozen, frozen_exec)

def test_R6H_provenance_receipt_fields_are_adjudicated(damage):
    """删掉回执字段不得等于把 R6-H 那道门从制品里摘掉；父进程执行修订可被判读方独立重算。"""
    text, base, start, end = _restamped_report()
    q = copy.deepcopy(base); m = q["meta"]
    if damage == "receipts_missing":
        m.pop("worker_receipts_confirmed"); m.pop("worker_code_sha256")
    elif damage == "exec_field_missing":
        m.pop("parent_exec_revision")
    elif damage == "exec_forged":
        m["parent_exec_revision"] = "f" * 64
    elif damage == "worker_exec_mixed":
        m["worker_exec_revision"] = ["a" * 64, "b" * 64]
    elif damage == "worker_disk_mismatch":
        m["worker_code_sha256"] = ["0" * 64]
    elif damage == "receipts_zero":
        m["worker_receipts_confirmed"] = 0
    with pytest.raises(ValueError):
        verify_report_text(_render(text, start, end, q))

def test_R6H_report_binds_both_identities():
    _, base, _, _ = _restamped_report()
    m = base["meta"]
    assert m["parent_exec_revision"] == NM.executing_code_digest()          # 判读方可独立重算
    assert m["worker_exec_revision"] == [m["parent_exec_revision"]]         # 无混版
    assert m["worker_code_sha256"] == [m["research_code_sha256"]]           # 磁盘身份同源
    assert m["worker_receipts_confirmed"] >= 1
