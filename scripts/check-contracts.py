from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import PROJECT_COLUMNS
from app.models import ProjectRecord

EXPECTED = (
    "project_id", "name", "team_name", "technical_owner_email", "description",
    "lifecycle_status", "created_at", "created_by", "updated_at", "updated_by",
    "workspace", "data_classification", "go_live_date", "documentation_link",
    "data_sources", "technical_details", "jira_link", "business_owner_email",
    "decision_comment",
)

assert PROJECT_COLUMNS == EXPECTED
assert tuple(ProjectRecord.model_fields) == EXPECTED
sql = (ROOT / "sql/001_create_v4_tables.sql").read_text()
start = sql.index("`governed_projects` (")
end = sql.index(")\nUSING DELTA", start)
section = sql[start:end]
for column in EXPECTED:
    assert column in section, column
print("v4 project contract passed")
