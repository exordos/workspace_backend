# Project Instructions

- Use the `admin/admin` account for checks.
- Do not use the sandbox for this project.
- The project virtual environment is in `.tox/develop`.
- Repository content must be written in English unless explicitly requested otherwise.
- Before making changes, inspect the project style and follow it.
- For required dict values, use `value = data[key]`, not `data.get(key)`.
- Write minimal code without extra defensive checks for every possible case.
- Let domain models perform data validation; do not duplicate model validation in controllers or helpers.
- Do not instantiate synthetic domain models as fallbacks for event payloads; use persisted/view models and let missing required data surface naturally.
- Do not write helper/controller raw SQL delete chains for dependent rows; delete the root model and make the database own cleanup through `ON DELETE CASCADE` foreign keys added by migrations.
- Do not open `engines.engine_factory.get_engine().session_manager()` inside API helpers/controllers for request work; restalchemy already runs request operations in one transaction, so pass the current `session` argument through.
- Restalchemy provides project CLI utilities in `.tox/develop/bin` such as `ra-new-migration`, `ra-apply-migration`, `ra-rollback-migration`, and `ra-rename-migrations`; check and use them before hand-writing equivalent restalchemy workflow code.
- Create new migrations with `.tox/develop/bin/ra-new-migration`, not by hand.
- When creating migrations, pass the migrations directory with `--path migrations` and set the dependency to the latest migration, preferably with `--depend HEAD` unless an explicit latest filename is required.
- Apply migrations with `.tox/develop/bin/ra-apply-migration --config-file etc/workspace/workspace.conf --path migrations`; pass `--migration <name>` for a specific target, otherwise let the utility apply the latest HEAD migration.
- If a migration appears applied but schema changes are missing, check the `ra_migrations` table by `migration_id`; restalchemy tracks applied migrations by UUID, so duplicated or previously applied UUIDs can hide an unapplied file.
