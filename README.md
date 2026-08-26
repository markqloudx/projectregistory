# Project Registry & Governance v4.0.0

A simplified Databricks project registry, development tag-compliance scanner, production approval gate, and protected production dispatcher.

Version 4.0.0 is designed for a mixed population of technical and semi-technical Databricks users. Project registration contains only business and ownership metadata. Users may build in `operations-dev` through the Databricks UI or through Git and Databricks Asset Bundles. Git and Bundle information is collected only when production is requested.

## Fixed Operations route

| Purpose | Workspace | Catalog |
|---|---|---|
| Registry application | `it-prod` | `it_prod.project_registry` |
| Development | `operations-dev` | `foundry_dev` |
| Production | `operations-prod` | `foundry_prod` |

The configured workspace hosts are:

- Registry: `https://dbc-ee468813-8a08.cloud.databricks.com`
- Development: `https://dbc-9c07a238-9e1e.cloud.databricks.com`
- Production: `https://dbc-eadde54f-6391.cloud.databricks.com`

## User flow

```text
Register project and receive PRJ-* ID
        ↓
Build in operations-dev
        ↓
Apply/verify project_tag, environment, data_classification
        ↓
Create Production Request with selected assets and Git/Bundle location
        ↓
Protected development validator checks current branch and Bundle contract
        ↓
READY_FOR_APPROVAL
        ↓
Approver selects Approve and deploy
        ↓
Protected team production job claims the request once
        ↓
Current branch is rechecked, deployed, run, and production tags are read back
        ↓
DEPLOYED or DEPLOY_FAILED
```

Default prefixes created by Bundle `mode: development` are allowed. The governance application does not rename assets and does not require identical development and production display names.

## Deliberate SHA-less approval model

Users do not enter or approve a Git SHA. A source revision is resolved automatically when the development validator reads the branch and again when the production worker deploys it. Both values are retained as audit evidence, but a revision difference does not itself block deployment.

This keeps the workflow simple but does **not** provide an exact-reviewed-commit guarantee. The protected production worker compensates by repeating the complete source/Bundle preflight against the branch it actually deploys.

## Mandatory tags

Every selected governed asset must have:

```text
project_tag=<registered PRJ-* ID>
environment=dev or prod
data_classification=<registered project classification>
```

Jobs and pipelines must declare these tags in Bundle source. Workspace entities and Unity Catalog objects can be repaired centrally when a tag is missing, but an existing conflicting `project_tag` is never overwritten.

## Exact `governed_projects` contract

The v4 application requires this exact 19-column table contract:

```text
project_id
name
team_name
technical_owner_email
description
lifecycle_status
created_at
created_by
updated_at
updated_by
workspace
data_classification
go_live_date
documentation_link
data_sources
technical_details
jira_link
business_owner_email
decision_comment
```

Delivery and production-request fields are stored in the other retained governance tables.

## Repository contents

```text
app/                           FastAPI application, Jinja UI, scanners, service layer
config/                        Operations pilot configuration
sql/                           Fresh install, v3.2.2 migration, grant templates
governance_worker/             Protected Git/Bundle validation and deployment worker
deployment/dev-validator/      Bundle for protected operations-dev validator job
deployment/prod-dispatcher/    Bundle for protected operations-prod deployment job
docs/                          Architecture, deployment, security, API, and test guides
tests/                         Memory-backed workflow and contract tests
```

## Local verification

```powershell
cd project_registry_governance_v4_0_0
python -m pip install -e ".[dev]"
$env:APP_CONFIG_PATH = "config/app_config.local.yaml"
pytest -q
uvicorn app.main:app --reload --port 8000
```

Use local identity headers only with the memory configuration. The production configuration has `trust_local_identity_headers: false` and relies on Databricks Apps authentication headers.

## Deployment order

1. Back up the v3.2.2 tables and App source.
2. Stop the current App during the major table migration.
3. Run `sql/002_migrate_3_2_to_4_0.sql`, or `sql/001_create_v4_tables.sql` for a fresh registry.
4. Deploy v4 once to create/update the Databricks App and obtain its App service-principal application ID.
5. Assign the development validator and production deployer identities to the registry App with `CAN_USE`.
6. Deploy the protected development validator job and protected production dispatcher job, granting the App service principal only `CAN_MANAGE_RUN` on those jobs.
7. Call `/api/me` as each worker identity and put the exact returned actors in `config/app_config.yaml`.
8. Put the worker job IDs and SQL warehouse IDs in `config/app_config.yaml` or the documented environment variables.
9. Redeploy the App.
10. Run the supplied sample project end to end before onboarding the Foundry project.

See [docs/DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md) for exact PowerShell commands and permissions.

## Current validation status

Completed in this build environment:

- Python compilation.
- Memory-backed API and state-transition tests.
- Exact 19-column project contract test.
- SHA-less approval and one-use worker claim test.
- Mandatory-tag failure test.
- self-approval denial test.
- active-request concurrency test.
- team deployment-profile isolation test.
- Bundle preflight test including default development prefixes and protected production root enforcement.
- sample project YAML and governance-contract tests.
- exact production evidence-to-manifest binding and forged-evidence rejection.
- deterministic worker-dispatch idempotency.
- wheel and source archive build.

Environment-dependent validation not performed here:

- Live Delta migration in `it-prod`.
- Databricks App deployment/restart.
- Live OAuth M2M and App direct-URL calls.
- Live `operations-dev` scans or tag repair.
- Live Bundle validation/deployment in Operations workspaces.
- Service-principal grants and cross-team negative tests.
- First approval-triggered production deployment.

These live items are explicitly covered by the deployment and acceptance-test guides.
