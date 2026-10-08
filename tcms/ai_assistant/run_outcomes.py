"""Separate executor lifecycle from the observed test verdict without changing states."""
from django.db.models import Count, Q


def annotate_outcomes(query):
    return query.annotate(
        outcome_total=Count('results', distinct=True),
        outcome_passed=Count('results', filter=Q(results__status='passed'), distinct=True),
        outcome_failed=Count('results', filter=Q(results__status='failed'), distinct=True),
        outcome_errors=Count('results', filter=Q(results__status='error'), distinct=True),
        outcome_unknown=Count('results', filter=~Q(results__status__in=['passed', 'failed', 'error', 'pending', 'skipped']), distinct=True),
    )


def task_state(run):
    return {'queued':'排队中', 'running':'执行中', 'cancel_requested':'取消中',
            'cancelled':'已取消', 'interrupted':'已中断'}.get(run.status, '已结束')


def test_outcome(run):
    if run.status in {'queued', 'running', 'cancel_requested'}:
        return {'state':'pending', 'label':'待完成'}
    if run.status in {'error', 'interrupted'} or run.error or getattr(run, 'outcome_errors', 0) or getattr(run, 'outcome_unknown', 0):
        return {'state':'error', 'label':'异常'}
    if getattr(run, 'outcome_failed', 0):
        return {'state':'failed', 'label':'失败'}
    total = max(getattr(run, 'total', 0), getattr(run, 'outcome_total', 0))
    if not total:
        return {'state':'skipped', 'label':'未执行'}
    if run.status == 'cancelled' or getattr(run, 'outcome_passed', 0) != total:
        return {'state':'skipped', 'label':'未完成'}
    return {'state':'passed', 'label':'通过'}
