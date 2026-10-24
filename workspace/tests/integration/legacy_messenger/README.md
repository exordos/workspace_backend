# Historical Messenger writers

These modules freeze the writers from `724bb466b6d7fab96470c0b4c0131c302e90392e`.
They let published-schema tests create and inspect pre-retirement records without
making application code depend on the removed tables. The canonical v2 read models and projection worker are frozen too, so new provider/status columns cannot
leak into historical projections. Only imports are redirected
to the reference modules and frozen migration helpers.

Tests using these writers run in disposable databases before migration `0231`.
Current runtime tests use the production store on the latest schema, where the
retired tables do not exist. Application modules must never import these fixtures.
