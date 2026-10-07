import copy
from .automation_data import datasets


def validate_dataset_cases(cases, environment, rows):
    from .api_runner import CASE_FIELDS, prepare_case, required_variables
    rows = datasets(rows) or [{}]
    if len(rows)*len(cases)>20:
        raise ValueError('数据组数 × 用例数不能超过 20。')
    for row in rows:
        env = copy.deepcopy(environment)
        env['variables'].update(row)
        for case in sorted(cases, key=lambda item:(item.sequence,item.pk)):
            config = {key:getattr(case,key) for key in CASE_FIELDS}
            missing = required_variables(config,env)-env['variables'].keys()
            if missing: raise ValueError('缺少变量：'+', '.join(sorted(missing))+'。请脚本数据或选择前置提取用例。')
            prepare_case(config,env)
            env['variables'].update({key:'runtime-value' for key in case.extracts})
