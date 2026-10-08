"""Explicit per-suite ordering; legacy sequence is only a compatibility fallback."""
def ordered_cases(cases, case_ids):
    cases = list(cases)
    if (not isinstance(case_ids, list) or len(case_ids) > 20
            or any(type(value) is not int or value <= 0 for value in case_ids)
            or len(set(case_ids)) != len(case_ids)
            or set(case_ids) != {case.pk for case in cases}):
        raise ValueError("脚本顺序与所选脚本不一致，请重新选择；每条脚本只能添加一次。")
    by_id = {case.pk: case for case in cases}
    return [by_id[value] for value in case_ids]


def clean_case_order(form, data):
    if "cases" not in data:
        return data
    requested = data.get("ordered_case_ids")
    if requested is None or (isinstance(requested, list) and not requested):
        values = form.data.getlist("cases") if hasattr(form.data, "getlist") else form.data.get("cases", [])
        try:
            requested = [int(value) for value in values]
        except (TypeError, ValueError):
            form.add_error("cases", "脚本顺序无效，请重新选择。")
            return data
    try:
        data["cases"] = ordered_cases(data["cases"], requested)
        data["ordered_case_ids"] = requested
    except ValueError as exc:
        form.add_error("cases", str(exc))
    return data
