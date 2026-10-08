from django import template
from tcms.ai_assistant.run_outcomes import task_state, test_outcome
register = template.Library()
register.filter('task_state', task_state)
register.filter('test_outcome', test_outcome)
