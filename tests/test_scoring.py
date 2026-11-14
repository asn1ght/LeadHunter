from app.audit.scoring import Weights, compute_score


def test_tech_only():
    issues = [{"code": "no_metrika", "severity": 2, "title": "a"}, {"code": "title_bad", "severity": 1, "title": "b"}]
    r = compute_score(issues, Weights())
    assert (r.score_tech, r.score, r.score_quality) == (11, 11, None)
    assert [f["points"] for f in r.factors] == [8, 3]


def test_bonuses_and_hot():
    issues = [{"code": "no_https", "severity": 3, "title": "x"}]
    r = compute_score(issues, Weights(), inn="7707083893", segment="b2c", priority_segment="b2c")
    assert r.score_tech == 15
    assert r.score == 15 + 5 + 5 + 10


def test_with_quality():
    issues = [{"code": "no_metrika", "severity": 2, "title": "a"}] * 5  # 40
    r = compute_score(issues, Weights(), score_quality=80)
    assert r.score == 60


def test_caps():
    issues = [{"code": "site_down", "severity": 3, "title": "x"}] * 10
    r = compute_score(issues, Weights(), inn="1")
    assert r.score_tech == 100
    assert r.score == 100


def test_custom_weights():
    issues = [{"code": "a", "severity": 3, "title": "a"}]
    assert compute_score(issues, Weights(sev3=20)).score == 20
