"""Permission-scoped plan relationships and read-only historical rollups."""

from collections import defaultdict
from django.db.models import Count, Q
from django.urls import reverse
from guardian.shortcuts import get_objects_for_user
from tcms.testplans.models import TestPlan
from tcms.testcases.models import TestCase, TestCasePlan
from tcms.testruns.models import TestRun, TestExecution


def visible_plans(user):
    return get_objects_for_user(user, "testplans.view_testplan", klass=TestPlan)


class PlanHierarchy:
    def __init__(self, user):
        self.user = user
        self.plans = {
            plan.pk: plan
            for plan in visible_plans(user).select_related("product_version").order_by("name", "pk")
        }
        self.parents = {}
        for plan in self.plans.values():
            parent = self.plans.get(plan.parent_id)
            self.parents[plan.pk] = (
                parent.pk if parent and parent.product_id == plan.product_id else None
            )
        # Historical data may contain cycles inserted without model validation.
        # Break the display edge deterministically; never rewrite persisted relations.
        resolved = set()
        for pk in self.plans:
            trail, positions = [], {}
            current = pk
            while current is not None and current not in resolved:
                if current in positions:
                    cycle = trail[positions[current] :]
                    self.parents[min(cycle)] = None
                    break
                positions[current] = len(trail)
                trail.append(current)
                current = self.parents[current]
            resolved.update(trail)
        self.children = defaultdict(list)
        for pk, parent in self.parents.items():
            self.children[parent].append(pk)
        self.roots = {}
        for pk in self.plans:
            trail, current = [], pk
            while current not in self.roots and self.parents[current] is not None:
                trail.append(current)
                current = self.parents[current]
            root = self.roots.get(current, current)
            self.roots[current] = root
            for item in trail:
                self.roots[item] = root

    def family(self, pk):
        if pk not in self.plans:
            return []
        result, stack = [], [(pk, 0)]
        while stack:
            item, depth = stack.pop()
            result.append((self.plans[item], depth))
            stack.extend((child, depth + 1) for child in reversed(self.children[item]))
        return result

    def folders(self, folders):
        from tcms.ai_assistant.models import ProjectResourceAssignment

        by_id = {folder.pk: folder for folder in folders}
        assigned = dict(
            ProjectResourceAssignment.objects.filter(
                resource_type="plan", object_id__in=self.plans, folder_id__in=by_id
            ).values_list("object_id", "folder_id")
        )
        result = {}
        for pk, root in self.roots.items():
            folder = by_id.get(assigned.get(root))
            result[pk] = (
                folder.pk if folder and folder.product_id == self.plans[pk].product_id else None
            )
        return result


def hierarchy_for(request):
    if not hasattr(request, "_plan_hierarchy"):
        request._plan_hierarchy = PlanHierarchy(request.user)
    return request._plan_hierarchy


def filter_plan_folder(query, request, selected, product=None):
    if not selected:
        return query
    from tcms.ai_assistant.case_directories import visible_folders, descendant_ids

    folders = list(visible_folders(request.user).filter(resource_type="plan"))
    folder = next((item for item in folders if str(item.pk) == str(selected)), None)
    if not folder or (product and folder.product_id != product.pk):
        return query.none()
    ids = descendant_ids(folder, [item for item in folders if item.product_id == folder.product_id])
    locations = hierarchy_for(request).folders(folders)
    return query.filter(pk__in=[pk for pk, location in locations.items() if location in ids])


def rollup_context(plan, request):
    hierarchy = hierarchy_for(request)
    family = hierarchy.family(plan.pk)
    ids = [item.pk for item, _depth in family]
    cases = get_objects_for_user(request.user, "testcases.view_testcase", klass=TestCase)
    links = TestCasePlan.objects.filter(
        plan_id__in=ids, case_id__in=cases.values("pk"), case__category__product_id=plan.product_id
    )
    case_total = links.values("case_id").distinct().count()
    runs = get_objects_for_user(request.user, "testruns.view_testrun", klass=TestRun).filter(
        plan_id__in=ids, build__version__product_id=plan.product_id
    )
    executions = TestExecution.objects.filter(run_id__in=runs.values("pk"))
    totals = executions.aggregate(
        total=Count("pk"),
        completed=Count("pk", filter=~Q(status__weight=0)),
        passed=Count("pk", filter=Q(status__weight__gt=0)),
        unsuccessful=Count("pk", filter=Q(status__weight__lt=0)),
        pending=Count("pk", filter=Q(status__weight=0)),
    )
    totals["completion_rate"] = (
        round(totals["completed"] * 100 / totals["total"], 1) if totals["total"] else None
    )
    totals["pass_rate"] = (
        round(totals["passed"] * 100 / totals["completed"], 1) if totals["completed"] else None
    )
    case_counts = dict(
        links.order_by().values("plan_id").annotate(total=Count("pk")).values_list("plan_id", "total")
    )
    run_counts = {
        row["plan_id"]: row
        for row in runs.order_by()
        .values("plan_id")
        .annotate(total=Count("pk"), opened=Count("pk", filter=Q(stop_date__isnull=True)))
    }
    rows = []
    for item, depth in family[1:]:
        rows.append(
            dict(
                plan=item,
                indent=(depth - 1) * 18,
                cases=case_counts.get(item.pk, 0),
                runs=run_counts.get(item.pk, {}).get("total", 0),
                opened=run_counts.get(item.pk, {}).get("opened", 0),
            )
        )
    parent = hierarchy.plans.get(hierarchy.parents.get(plan.pk))
    return dict(
        plan_parent=parent,
        plan_child_url=reverse("plans-new") + f"?parent={plan.pk}",
        plan_descendants=rows,
        plan_rollup=dict(
            subplans=len(rows),
            cases=case_total,
            runs=runs.count(),
            open_runs=runs.filter(stop_date__isnull=True).count(),
            **totals,
        ),
        plan_rollup_runs=runs.select_related("plan", "manager", "build").order_by("-pk"),
    )
