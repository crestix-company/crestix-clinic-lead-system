from src.cloud_run.deployment_context import is_cloud_run_deployment


def test_false_when_k_service_unset():
    assert is_cloud_run_deployment({}) is False


def test_false_when_k_service_blank():
    assert is_cloud_run_deployment({"K_SERVICE": "   "}) is False
