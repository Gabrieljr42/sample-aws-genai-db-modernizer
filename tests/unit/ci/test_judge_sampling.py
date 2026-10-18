"""ci/llm/judge.py: the judge model is pinned and recorded, each run takes
several samples and reports the per-criterion median, and a saved main
``judge.json`` can be compared per criterion (#952).

No real model is called: ``CLAUDE_BIN`` is a stub that prints the recorded
CLI result record (``fixtures/judge-cli/result-record.json``) with a canned
reply, one reply per call from a list.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from ci.llm import judge
from tests.unit.ci.test_judge import DB, JOB, _build_job_dir

RECORD = json.loads((Path(__file__).parent / "fixtures/judge-cli/result-record.json").read_text())
RECORDED_MODEL = "global.anthropic.claude-sonnet-5-5"


def _scores(**overrides: int) -> dict[str, int]:
    scores = dict.fromkeys(judge.CRITERIA, 4)
    scores.update(overrides)
    return scores


def _reply(scores: dict[str, int], defects: list | None = None) -> str:
    payload: dict = {"scores": scores, "notes": {k: f"note {k} {scores[k]}" for k in scores}}
    if defects is not None:
        payload["defects"] = defects
    return json.dumps(payload)


def _record(reply: str, **extra) -> dict:
    outer = dict(RECORD)
    outer["result"] = reply
    outer["total_cost_usd"] = 0.13
    outer.update(extra)
    return outer


def _stub(tmp_path: Path, outers: list[dict], *, help_text: str = "Usage: claude") -> Path:
    """A CLAUDE_BIN that prints ``outers[i]`` on its i-th call (the last one
    repeats) and appends each call's argv to ``calls.jsonl``."""
    payloads = tmp_path / "payloads.json"
    payloads.write_text(json.dumps(outers))
    counter = tmp_path / "calls.jsonl"
    stub = tmp_path / "fake_claude.py"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if sys.argv[1:] == ['--help']:\n"
        f"    print({help_text!r})\n"
        "    sys.exit(0)\n"
        "sys.stdin.read()\n"
        f"counter = {str(counter)!r}\n"
        "try:\n"
        "    n = sum(1 for _ in open(counter))\n"
        "except FileNotFoundError:\n"
        "    n = 0\n"
        "with open(counter, 'a') as f:\n"
        "    f.write(json.dumps(sys.argv) + '\\n')\n"
        f"outers = json.load(open({str(payloads)!r}))\n"
        "outer = outers[min(n, len(outers) - 1)]\n"
        "if outer is None:\n"
        "    sys.stderr.write('boom')\n"
        "    sys.exit(3)\n"
        "print(json.dumps(outer))\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _calls(tmp_path: Path) -> list[list[str]]:
    return [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]


def _run(tmp_path: Path, outers: list[dict], **kwargs) -> tuple[dict, int]:
    _build_job_dir(tmp_path)
    stub = _stub(tmp_path, outers)
    return judge.run_judge(
        artifact_root=str(tmp_path), db=DB, job=JOB, claude_bin=str(stub), **kwargs
    )


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


def test_model_is_read_from_the_recorded_cli_record(tmp_path: Path) -> None:
    """The real CLI record has no top-level ``model``; it is a ``modelUsage`` key."""
    assert "model" not in RECORD
    result, code = _run(tmp_path, [_record(_reply(_scores()))])
    assert code == 0, result
    assert result["model"] == RECORDED_MODEL
    assert result["models"] == [RECORDED_MODEL]
    assert all(s["model"] == RECORDED_MODEL for s in result["samples"])


def test_cli_models_ranks_by_output_tokens_and_falls_back_to_model() -> None:
    usage = {
        "small-model": {"outputTokens": 10},
        "judge-model": {"outputTokens": 900},
    }
    assert judge.cli_models({"modelUsage": usage}) == ["judge-model", "small-model"]
    assert judge.cli_models({"model": "legacy-shape"}) == ["legacy-shape"]
    assert judge.cli_models({}) == []
    assert judge.cli_models({"modelUsage": "not a dict", "model": None}) == []


def test_the_judge_model_is_pinned_on_the_command_line(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("JUDGE_MODEL", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    result, code = _run(tmp_path, [_record(_reply(_scores()))])
    assert code == 0, result
    for argv in _calls(tmp_path):
        assert argv[argv.index("--model") + 1] == judge.DEFAULT_JUDGE_MODEL_BEDROCK
    assert result["model_requested"] == judge.DEFAULT_JUDGE_MODEL_BEDROCK


def test_the_pinned_model_follows_the_auth_mode_and_env(monkeypatch) -> None:
    monkeypatch.delenv("JUDGE_MODEL", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_USE_BEDROCK", raising=False)
    assert judge.judge_model() == judge.DEFAULT_JUDGE_MODEL_API
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "0")
    assert judge.judge_model() == judge.DEFAULT_JUDGE_MODEL_API
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    assert judge.judge_model() == judge.DEFAULT_JUDGE_MODEL_BEDROCK
    monkeypatch.setenv("JUDGE_MODEL", "some-other-model")
    assert judge.judge_model() == "some-other-model"
    assert judge.judge_model("explicit") == "explicit"


def test_a_different_model_than_requested_is_recorded_as_served(tmp_path: Path) -> None:
    other = _record(_reply(_scores()), modelUsage={"fallback-model": {"outputTokens": 5}})
    result, code = _run(tmp_path, [other], model="requested-model")
    assert code == 0, result
    assert result["model_requested"] == "requested-model"
    assert result["model"] == "fallback-model"


# ---------------------------------------------------------------------------
# Samples and the median
# ---------------------------------------------------------------------------


def test_three_samples_by_default_and_scores_are_the_per_criterion_median(tmp_path: Path) -> None:
    replies = [
        _scores(risks=3, hedging=5),
        _scores(risks=4, hedging=2),
        _scores(risks=3, hedging=4),
    ]
    result, code = _run(tmp_path, [_record(_reply(s)) for s in replies])
    assert len(_calls(tmp_path)) == judge.DEFAULT_SAMPLES == 3
    assert result["scores"]["risks"] == 3
    assert result["scores"]["hedging"] == 4
    assert result["scores"]["grounded"] == 4
    assert result["spread"]["risks"] == 1
    assert result["spread"]["hedging"] == 3
    assert result["spread"]["grounded"] == 0
    assert [s["scores"] for s in result["samples"]] == replies
    assert result["sample_count"] == 3
    assert result["cost_usd"] == pytest.approx(0.39)
    assert result["mean"] == round(sum(result["scores"].values()) / len(judge.CRITERIA), 3)
    assert code == 0


def test_notes_come_from_the_sample_closest_to_the_median(tmp_path: Path) -> None:
    replies = [_scores(risks=2, roadmap=2), _scores(risks=4), _scores(roadmap=2)]
    result, _ = _run(tmp_path, [_record(_reply(s)) for s in replies])
    # medians: risks 4, roadmap 2 -> sample 3 matches exactly
    assert result["notes"]["roadmap"] == "note roadmap 2"
    assert result["notes"]["risks"] == "note risks 4"
    assert result["notes_from_sample"] == 3


def test_pass_rule_uses_the_median_not_one_outlier_sample(tmp_path: Path) -> None:
    """One sample at 1 does not fail the run when the other two agree on 4."""
    replies = [_scores(hedging=1), _scores(), _scores()]
    result, code = _run(tmp_path, [_record(_reply(s)) for s in replies])
    assert code == 0, result
    assert result["pass"] is True
    assert result["scores"]["hedging"] == 4

    replies = [_scores(hedging=1), _scores(hedging=1), _scores()]
    result, code = _run(tmp_path / "b", [_record(_reply(s)) for s in replies])
    assert code == 1
    assert result["scores"]["hedging"] == 1


def test_samples_flag_and_env(tmp_path: Path, monkeypatch) -> None:
    result, code = _run(tmp_path, [_record(_reply(_scores()))], samples=1)
    assert code == 0, result
    assert len(_calls(tmp_path)) == 1
    monkeypatch.setenv("JUDGE_SAMPLES", "5")
    assert judge.judge_samples() == 5
    monkeypatch.setenv("JUDGE_SAMPLES", "zero")
    with pytest.raises(judge.JudgeError):
        judge.judge_samples()
    with pytest.raises(judge.JudgeError):
        judge.judge_samples(0)


def test_one_failed_sample_of_three_is_recorded_not_fatal(tmp_path: Path) -> None:
    outers = [
        _record(_reply(_scores(risks=3))),
        _record("not json at all"),
        _record(_reply(_scores(risks=5))),
    ]
    result, code = _run(tmp_path, outers)
    assert code == 0, result
    assert result["sample_count"] == 2
    assert len(result["samples_failed"]) == 1
    assert "could not parse" in result["samples_failed"][0]
    assert result["scores"]["risks"] == 4  # median of 3 and 5


def test_a_majority_of_failed_samples_is_a_judge_error(tmp_path: Path) -> None:
    outers = [_record("nope"), _record(_reply(_scores())), _record("nope")]
    result, code = _run(tmp_path, outers)
    assert code == 2
    assert "2 of 3 judge samples failed" in result["error"]
    assert "could not parse" in result["error"]


def test_aggregate_samples_handles_any_criteria_and_even_counts() -> None:
    agg = judge.aggregate_samples([{"a": 3, "b": 5}, {"a": 4, "b": 5}])
    assert agg["scores"] == {"a": 3.5, "b": 5}
    assert agg["spread"] == {"a": 1, "b": 0}
    with pytest.raises(judge.JudgeError):
        judge.aggregate_samples([])


# ---------------------------------------------------------------------------
# Comparison against a main baseline
# ---------------------------------------------------------------------------


def _baseline(tmp_path: Path, scores: dict) -> Path:
    path = tmp_path / "baseline-judge.json"
    path.write_text(json.dumps({"scores": scores, "mean": 4.0, "pass": True}))
    return path


def test_baseline_comparison_flags_a_one_point_median_drop(tmp_path: Path) -> None:
    base = _baseline(tmp_path, _scores(risks=4))
    result, code = _run(tmp_path, [_record(_reply(_scores(risks=3)))], baseline_path=base)
    assert code == 1, result
    assert result["pass"] is False
    assert result["baseline"]["regressions"] == ["risks"]
    assert result["baseline"]["deltas"]["risks"] == -1
    assert result["baseline"]["deltas"]["grounded"] == 0


def test_baseline_comparison_passes_when_no_criterion_drops(tmp_path: Path) -> None:
    base = _baseline(tmp_path, _scores(risks=3))
    result, code = _run(tmp_path, [_record(_reply(_scores(risks=4)))], baseline_path=base)
    assert code == 0, result
    assert result["baseline"]["regressions"] == []
    assert result["baseline"]["deltas"]["risks"] == 1


def test_baseline_with_the_old_six_criteria_compares_only_shared_ones(tmp_path: Path) -> None:
    """A judge.json saved before #952 (cost and tone, single sample) still loads."""
    old = {"grounded": 4, "justified_engines": 5, "cost": 5, "risks": 3, "roadmap": 4, "tone": 3}
    base = _baseline(tmp_path, old)
    result, code = _run(tmp_path, [_record(_reply(_scores()))], baseline_path=base)
    assert code == 1, result  # justified_engines 5 -> 4
    cmp = result["baseline"]
    assert cmp["regressions"] == ["justified_engines"]
    assert set(cmp["deltas"]) == {"grounded", "justified_engines", "risks", "roadmap"}
    assert cmp["not_in_baseline"] == ["concision", "contradictions", "hedging"]
    assert cmp["only_in_baseline"] == ["cost", "tone"]


def test_unreadable_baseline_is_a_judge_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    result, code = _run(tmp_path, [_record(_reply(_scores()))], baseline_path=missing)
    assert code == 2
    assert "baseline" in result["error"]
    errored = tmp_path / "errored.json"
    errored.write_text(json.dumps({"error": "claude CLI exited 2"}))
    result, code = _run(tmp_path / "b", [_record(_reply(_scores()))], baseline_path=errored)
    assert code == 2
    assert "baseline" in result["error"]
