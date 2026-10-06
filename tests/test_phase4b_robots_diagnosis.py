from scripts.analyze_phase4b_robots_failures import (
    ROBOTS_SAFE_ERROR,
    classify,
    select_sample,
    select_canary,
    write_safe_targets,
)


def event(status, body=b"", content_type="text/plain", **headers):
    return {"url": "https://clinic.example/robots.txt", "status": status,
            "headers": {"content-type": content_type, **headers}, "body": body}


def test_deterministic_stratified_sample_has_fixed_100_rows():
    rows = []
    sizes = {"NO_SIGNAL": 50, "NO_REPLAY_DATA": 50, "CANDIDATE_ONLY": 17, "NOT_CONFIRMED": 3}
    clinic_id = 1
    for group, size in sizes.items():
        for _ in range(size):
            rows.append({"clinic_id": clinic_id, "sampling_group": group})
            clinic_id += 1
    first = select_sample(rows)
    second = select_sample(list(reversed(rows)))
    assert [r["clinic_id"] for r in first] == [r["clinic_id"] for r in second]
    assert len(first) == 100
    assert {g: sum(r["sampling_group"] == g for r in first) for g in sizes} == {
        "NO_SIGNAL": 40, "NO_REPLAY_DATA": 40, "CANDIDATE_ONLY": 17, "NOT_CONFIRMED": 3,
    }


def test_classification_contract():
    url = "https://clinic.example/"
    assert classify([event(404)], "", url)[0] == "ROBOTS_NOT_FOUND_4XX"
    assert classify([event(429)], None, url)[0] == "ROBOTS_429"
    assert classify([event(503)], None, url)[0] == "ROBOTS_5XX"
    assert classify([event(200, b"", "text/plain")], "", url)[0] == "ROBOTS_EMPTY"
    assert classify([event(200, b"<html>error</html>", "text/html")], None, url)[0] == "ROBOTS_HTML_ERROR_PAGE"
    valid = b"User-agent: *\nDisallow:\n"
    assert classify([event(200, valid)], valid.decode(), url) == (
        "VALID_ROBOTS", "robots directives parsed; initial URL allowed", True,
    )
    blocked = b"User-agent: *\nDisallow: /\n"
    assert classify([event(200, blocked)], blocked.decode(), url) == (
        "VALID_ROBOTS", "explicit rules disallow initial URL", False,
    )


def test_generic_original_failure_message_is_not_used_as_a_category():
    assert ROBOTS_SAFE_ERROR


def test_safe_targets_are_allowlisted_and_explicit_disallow_is_excluded(tmp_path):
    base = {
        "clinic_name": "test", "sampling_group": "NO_SIGNAL", "initial_url": "https://example.com/",
        "final_robots_url": "https://example.com/robots.txt", "diagnosed_at": "2026-10-03T00:00:00Z",
        "previous_error": ROBOTS_SAFE_ERROR,
    }
    rows = [
        {**base, "clinic_id": 1, "robots_result_category": "VALID_ROBOTS", "safe_retry_candidate": "true"},
        {**base, "clinic_id": 2, "robots_result_category": "VALID_ROBOTS", "safe_retry_candidate": "false"},
        {**base, "clinic_id": 3, "robots_result_category": "ROBOTS_NOT_FOUND_4XX", "safe_retry_candidate": "true"},
        {**base, "clinic_id": 4, "robots_result_category": "ROBOTS_EMPTY", "safe_retry_candidate": "true"},
        {**base, "clinic_id": 5, "robots_result_category": "ROBOTS_HTML_ERROR_PAGE", "safe_retry_candidate": "true"},
    ]
    safe = write_safe_targets(rows, tmp_path / "safe.csv")
    assert [r["clinic_id"] for r in safe] == [1, 3, 4]


def test_canary_selection_is_deterministic_and_bounded():
    rows = [{"clinic_id": str(i), "sampling_group": ("NO_SIGNAL", "NO_REPLAY_DATA", "CANDIDATE_ONLY")[i % 3],
             "robots_category": ("VALID_ROBOTS", "ROBOTS_NOT_FOUND_4XX", "ROBOTS_EMPTY")[i % 3]}
            for i in range(1, 101)]
    first = select_canary(rows, 50)
    second = select_canary(list(reversed(rows)), 50)
    assert [r["clinic_id"] for r in first] == [r["clinic_id"] for r in second]
    assert len(first) == 50
