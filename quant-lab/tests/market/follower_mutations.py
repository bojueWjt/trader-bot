"""Isolated source mutants; run only the tests that assert the changed rule."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[2]
K='market/kernel_a.py'
X='market/execution.py'
C='market/contract.py'
L='market/l0_replay.py'
P='market/partition_check.py'
F='tests/market/test_follower_execution.py'
I='tests/integration/test_follower_lake.py'
MUTANTS=[
 ('I01-extreme',K,'ref = sl.price','ref = last',F,'stop_intrabar'),
 ('I01-zero-tick',K,'if self.cost.slippage_ticks == 0 and self.cost.slippage_bps == 0:','if False:',F,'stop_intrabar'),
 ('I01-close-next-open',K,'if sl.close_triggered:\n                px = self.market_px(last, sl.side)','if False:\n                px = self.market_px(last, sl.side)',F,'close_stop_recovery'),
 ('I01-gap',K,'if gap:\n                    px = opened','if False:\n                    px = opened',F,'stop_gap'),
 ('I04-entry-cross',K,'px = last if taker or step == "O" else o.price','px = last',F,'resting_entry_cross'),
 ('I04-tp-cross',K,'px = last if taker or step == "O" else o.price','px = last',F,'resting_tp_cross'),
 ('I04-entry-fee',K,'taker = o.first_match','taker = False',F,'marketable_limit'),
 ('I04-tp-fee',K,'taker = o.first_match','taker = False',F,'marketable_tp'),
 ('I04-gap-limit',K,'px = last if taker or step == "O" else o.price','px = last if taker else o.price',F,'resting_entry_gap or resting_tp_cross'),
 ('G5a-path',K,'self.path_orders.get(b.open_time)','None',F,'common_path'),
 ('I03-cap',K,'q = floor_step(want, self.rules.step_size) if o.kind == "market" else take(want)','q = take(want)',F,'ioc_market'),
 ('I12-stop-boundary',L,'mark.price <= stop_price if side == "long" else mark.price >= stop_price','mark.price < stop_price if side == "long" else mark.price > stop_price',F,'plan_stale'),
 ('I02-quote-boundary',L,'abs(quote - mark.price) > Decimal("0.25")','abs(quote - mark.price) >= Decimal("0.25")',F,'quote_quarter'),
 ('I02-quote-sizing',L,'if e.get("kind") == "market_ref" else e for e in entries','if e.get("kind") == "market_ref" and e.get("price_lo") is None else e for e in entries',F,'quote_quarter'),
 ('I42-entry-tick',K,'return [self.q_tick(e.price_lo, side)]','return [e.price_lo]',F,'tick_rounding'),
 ('I42-tp-tick',K,'self.q_tick(tp.level, "buy" if self.sign > 0 else "sell")','tp.level',F,'tick_rounding'),
 ('I42-cascade',K,'reject = rejects[i]','reject = next((r for r in rejects if r), None)',F,'illegal_entry_leg'),
 ('I46-prewindow',X,'window_before_s: int = 60','window_before_s: int = 0',I,'preminute'),
 ('I43-window-mask',K,'seed = self.closed_mark_at(self.t_start)','self.cov["bars_ok"] = self.market.bars_complete\n        seed = self.closed_mark_at(self.t_start)',F,'bar_gap_only'),
 ('I43-gap-skip',K,'if mo.bar_gap:','if False:',F,'bar_gap_only'),
 ('I43-unfilled-boundary',K,'if mo.bar_gap and mo.expiry and self.pos == 0 and self.entry_qty == 0:','if False:',F,'gap_at_unfilled_expiry'),
 ('I45-fill-censor',K,'if filled_now:\n                self.protect(ts)','if filled_now:\n                self.protect(ts)\n                if not self.market.funding_schedule_complete:\n                    self.censor_now("FUNDING_SCHEDULE_GAP", "funding_ok")',F,'funding_missing_row_checked'),
 ('I45-missing-row',K,'if mo.funding_missing and self.pos != 0:','if False:',I,'missing_settlement_row'),
 ('I45-month-manifest',X,'funding_ok = not missing_funding','funding_ok = not missing_funding\n    if not all(manifest_ok("fundingRate", "8h", mo) for mo in sorted(fmonths)):\n        gap_times.append(start)',I,'preminute'),
 ('I45-actual-interval',C,'dt.timedelta(hours=prev.interval_hours)','dt.timedelta(hours=8)',F,'funding_cycle_switch'),
 ('I46-zero-position',K,'if self.pos == 0:\n                return','if False:\n                return',F,'funding_before_first_fill'),
 ('I45-switch',P,'for hours in (ih[i - 1], ih[i])','for hours in (ih[i],)',F,'funding_cycle_switch'),
 ('G1c-evaluable',X,'row["evaluable"] = covered and row["censor_reason"] is None and row["net_R"] is not None','row["evaluable"] = False',F,'evaluable_and'),
 ('G1c-reason',X,'row["censor_reason"] = "COVERAGE_" + "_".join(failed)','row["censor_reason"] = None',F,'evaluable_and'),
]


def main():
    receipts=[]
    with tempfile.TemporaryDirectory(prefix='follower-mutants-') as tmp:
        base=Path(tmp)/'src'
        shutil.copytree(ROOT/'src/quant_lab',base/'quant_lab',ignore=shutil.ignore_patterns('__pycache__'))
        for name,file,before,after,test,selector in MUTANTS:
            path=base/'quant_lab'/file
            original=path.read_text()
            if before not in original:
                raise RuntimeError(f'{name}: missing mutation anchor')
            # Both TP and entry variants share the expression; mutating both is intentional.
            path.write_text(original.replace(before,after))
            env=dict(os.environ,PYTHONPATH=str(base),PYTHONDONTWRITEBYTECODE='1',QUANT_LAB_SYNTHETIC_ONLY='1')
            command=[str(ROOT/'.venv-g2/bin/python'),'-B','-m','pytest','-o',f'pythonpath={base}',test,'-k',selector,'-q']
            try:
                result=subprocess.run(command,cwd=ROOT,env=env,capture_output=True,text=True,timeout=60)
                output=result.stdout+result.stderr
                killed=result.returncode==1 and 'AssertionError' in output and 'failed' in output
                receipts.append({'id':name,'file':file,'tests':test,'selector':selector,'exit_code':result.returncode,
                    'killed':killed,'result_line':output.strip().splitlines()[-1] if output.strip() else '',
                    'failure':output if not killed else None})
                print(f'{name}: {"KILLED" if killed else "SURVIVED/INVALID"} {receipts[-1]["result_line"]}',flush=True)
            finally:
                path.write_text(original)
                for cache in base.rglob('__pycache__'):
                    shutil.rmtree(cache)
    dest=ROOT/'tests/market/follower_mutation_results.json'
    dest.write_text(json.dumps(receipts,ensure_ascii=False,indent=2)+'\n')
    return 0 if all(r['killed'] for r in receipts) else 1


if __name__=='__main__':
    raise SystemExit(main())
