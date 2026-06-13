"""analyze_run.py 纯统计函数测试（第1套验证分析层）。

覆盖：R 分布分位数/直方图 · exit_reason 占比+毛盈亏贡献 · target2 命中率
· 分层统计 · reject 原因文本匹配。全手算 oracle。

注：analyze_run 的统计函数按 row["key"] 访问，dict 即可充当 sqlite3.Row。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import analyze_run as ar  # noqa: E402


def _trade(R, pnl, reason, cand_id=None):
    return {"R_multiple": R, "realized_pnl": pnl, "exit_reason": reason, "signal_candidate_id": cand_id}


def _cand(cid, family="BPB", read="strong", decision="accept", reason="accepted"):
    return {
        "signal_candidate_id": cid,
        "setup_family": family,
        "read_status": read,
        "decision": decision,
        "reason": reason,
    }


# =========================================================================
# R 分布
# =========================================================================
def test_r_distribution_buckets():
    """直方图分桶左闭右开，末桶含上界。"""
    trades = [_trade(r, 0, "stop") for r in (-2.0, -0.5, 0.3, 0.9, 1.5, 3.0, 7.0)]
    rd = ar.r_distribution(trades)
    assert rd["n"] == 7
    b = rd["buckets"]
    assert b["<-1"] == 1      # -2.0
    assert b["-1~0"] == 1     # -0.5
    assert b["0~1"] == 2      # 0.3, 0.9
    assert b["1~2"] == 1      # 1.5
    assert b["2~5"] == 1      # 3.0
    assert b[">5"] == 1       # 7.0


def test_r_distribution_quantiles():
    """p50 = 中位数。"""
    trades = [_trade(r, 0, "stop") for r in (1.0, 2.0, 3.0, 4.0, 5.0)]
    rd = ar.r_distribution(trades)
    assert rd["quantiles"]["p50"] == 3.0
    assert rd["quantiles"]["min"] == 1.0
    assert rd["quantiles"]["max"] == 5.0
    assert abs(rd["quantiles"]["mean"] - 3.0) < 1e-9


def test_r_distribution_empty():
    assert ar.r_distribution([])["n"] == 0


# =========================================================================
# exit_reason 占比 + 毛盈亏贡献
# =========================================================================
def test_exit_reason_breakdown():
    """毛盈 1000(target2)+200(trailing)=1200；毛亏 300(stop)。"""
    trades = [
        _trade(5.0, 1000, "target2"),
        _trade(0.5, 200, "trailing"),
        _trade(-1.0, -300, "stop"),
    ]
    eb = ar.exit_reason_breakdown(trades)
    assert eb["target2"]["count"] == 1
    assert abs(eb["target2"]["pct"] - 1 / 3) < 1e-9
    assert abs(eb["target2"]["win_share"] - 1000 / 1200) < 1e-9
    assert eb["stop"]["loss_share"] == 1.0  # 唯一亏损
    assert eb["target2"]["loss_share"] == 0.0


def test_exit_reason_breakdown_empty():
    assert ar.exit_reason_breakdown([]) == {}


# =========================================================================
# target2 命中率
# =========================================================================
def test_target2_hit_rate():
    trades = [_trade(5, 1, "target2"), _trade(0, 0, "stop"), _trade(0, 0, "trailing"), _trade(6, 1, "target2")]
    assert ar.target2_hit_rate(trades) == 0.5
    assert ar.target2_hit_rate([]) == 0.0


# =========================================================================
# 分层
# =========================================================================
def test_layered_stats_by_family_and_read():
    cands = [_cand(1, "BPB", "strong"), _cand(2, "PB", "mixed"), _cand(3, "BPB", "strong")]
    trades = [
        _trade(1.0, 100, "target1", 1),
        _trade(-1.0, -100, "stop", 2),
        _trade(2.0, 200, "target2", 3),
    ]
    ls = ar.layered_stats(trades, cands)
    # BPB: 2 笔（id1,id3）win_rate=1.0 avg_R=1.5；PB: 1 笔亏
    assert ls["by_family"]["BPB"]["count"] == 2
    assert ls["by_family"]["BPB"]["win_rate"] == 1.0
    assert abs(ls["by_family"]["BPB"]["avg_R"] - 1.5) < 1e-9
    assert ls["by_family"]["PB"]["win_rate"] == 0.0
    # read: strong 2 笔 / mixed 1 笔
    assert ls["by_read_status"]["strong"]["count"] == 2
    assert ls["by_read_status"]["mixed"]["count"] == 1


# =========================================================================
# reject 原因文本匹配
# =========================================================================
def test_reject_breakdown_text_match():
    cands = [
        _cand(1, decision="accept", reason="accepted"),
        _cand(2, decision="reject", reason="rr_below_min"),
        _cand(3, decision="reject", reason="rr_below_min"),
        _cand(4, decision="reject", reason="read_status_too_weak"),
        _cand(5, decision="reject", reason="life_state_exhausted"),
    ]
    rb = ar.reject_breakdown(cands)
    assert rb["total_candidates"] == 5
    assert rb["accept"] == 1
    assert rb["reject"] == 4
    assert abs(rb["accept_rate"] - 0.2) < 1e-9
    assert rb["by_reason"]["rr_below_min"] == 2
    assert rb["by_reason"]["read_status_too_weak"] == 1
    assert rb["by_reason"]["life_state_exhausted"] == 1
    assert rb["other"] == 0


def test_reject_breakdown_unmatched_counts_as_other():
    cands = [_cand(1, decision="reject", reason="some_unknown_reason")]
    rb = ar.reject_breakdown(cands)
    assert rb["other"] == 1
    assert rb["by_reason"] == {}
