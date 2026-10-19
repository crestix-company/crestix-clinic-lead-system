"""No external service or DB is accessed by these batch-coordinator tests."""
from dataclasses import replace
import pytest

from src.outreach.auto_draft_batch import MAX_AUTO_DRAFTS, plan_auto_drafts, run_approved_batch
from src.outreach.compose import ARTICLE_LISTED, ARTICLE_NOT_LISTED, ARTICLE_UNCONFIRMED
from src.outreach.generation import READY, NEEDS_REVIEW, OutreachResult
from src.outreach.gmail_workflow import DraftWorkflowResult
from src.outreach.targets import ArticleState, ClinicTarget, EMAIL_FOUND


def generated(cid=1, *, email=None, article=ARTICLE_NOT_LISTED, article_type="NORMAL",
              ready=True, director=True):
    cid = int(cid)
    target = ClinicTarget(
        clinic_id=cid,
        clinic_uuid=f"clinic-{cid}",
        clinic_name=f"Test Clinic {cid}",
        prefecture="東京都", municipality="港区", departments=("内科",),
        treatments=(), hp_rank="A",
        director_name="院長" if director else "",
        email=email or f"clinic{cid}@example.jp", email_status=EMAIL_FOUND,
        director_name_confirmed=director,
        article=ArticleState(article, article_type=article_type),
    )
    return OutreachResult(
        target, READY if ready else NEEDS_REVIEW,
        subject=f"Subject {cid}", body=f"Body {cid}",
    )


def test_eligible_auto_selection_from_ready_list():
    draft = generated()
    result = plan_auto_drafts([draft])
    assert result.eligible == (draft,)
    assert not result.held and result.overflow == 0


def test_all_duplicate_identifiers_are_held_even_if_one_was_ready():
    same = generated()
    second = replace(generated(2), target=replace(generated(2).target, clinic_uuid=same.target.clinic_uuid))
    plan = plan_auto_drafts([same, second])
    assert len(plan.eligible) == 0
    assert len(plan.held) == 2


def test_shared_recipient_across_clinics_blocks_both():
    first = generated(1, email="shared@example.jp")
    second = generated(2, email="SHARED@example.jp")
    plan = plan_auto_drafts([first, second])
    assert len(plan.eligible) == 0
    assert len(plan.held) == 2


@pytest.mark.parametrize("item", [
    generated(1, ready=False),
    generated(1, director=False),
    generated(1, article=ARTICLE_UNCONFIRMED),
    generated(1, article=ARTICLE_LISTED, article_type="PR"),
    generated(1, article=ARTICLE_LISTED, article_type="SUPERVISED"),
    generated(1, article=ARTICLE_LISTED, article_type="UNKNOWN"),
])
def test_unknown_or_unverified_is_not_automatically_drafted(item):
    plan = plan_auto_drafts([item])
    assert not plan.eligible
    assert len(plan.held) == 1


def test_cap_is_25_and_unprocessed_are_counted():
    plan = plan_auto_drafts([generated(i) for i in range(1, 31)])
    assert len(plan.eligible) == MAX_AUTO_DRAFTS
    assert plan.overflow == 5 and not plan.held
    with pytest.raises(ValueError):
        plan_auto_drafts([generated(1)], limit=26)


def test_runner_calls_each_eligible_once_without_retries_or_send():
    called = []
    def save(item):
        called.append(item.target.clinic_id)
        return DraftWorkflowResult("DRAFT_CREATED", gmail_draft_id=str(item.target.clinic_id))
    run = run_approved_batch([generated(1), generated(2)], create_one=save)
    assert called == [1, 2]
    assert not run.stopped_early
    assert [o.status for o in run.outcomes] == ["DRAFT_CREATED", "DRAFT_CREATED"]


@pytest.mark.parametrize("uncertain", ["UNKNOWN", "REVIEW", "DISABLED"])
def test_runner_stops_after_uncertain_state(uncertain):
    called = []
    def save(item):
        called.append(item.target.clinic_id)
        return DraftWorkflowResult(uncertain)
    run = run_approved_batch([generated(1), generated(2)], create_one=save)
    assert called == [1]
    assert run.stopped_early and len(run.outcomes) == 1


def test_runner_stops_on_unknown_exception_and_no_retries():
    called = []
    def save(item):
        called.append(item.target.clinic_id)
        raise TimeoutError("unknown whether already created")
    run = run_approved_batch([generated(1), generated(2)], create_one=save)
    assert called == [1]
    assert run.stopped_early and run.outcomes == ()


def test_runner_rejects_duplicate_inputs_without_touching_gmail():
    called = []
    with pytest.raises(ValueError):
        run_approved_batch([generated(1), generated(1)], create_one=lambda x: called.append(x))
    assert called == []


def test_runner_rejects_batch_greater_than_25_without_touching_gmail():
    called = []
    with pytest.raises(ValueError):
        run_approved_batch([generated(i) for i in range(1, 27)], create_one=lambda x: called.append(x))
    assert called == []


def test_status_of_existing_draft_does_not_auto_retry():
    called = []
    def save(item):
        called.append(item.target.clinic_id)
        return DraftWorkflowResult("DRAFT_EXISTS")
    run = run_approved_batch([generated(1)], create_one=save)
    assert called == [1]
    assert run.outcomes[0].status == "DRAFT_EXISTS"
