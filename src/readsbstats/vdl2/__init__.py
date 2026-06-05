"""VDL2 / ACARS optional feature package.

Self-contained, opt-in (``RSBS_VDL2_ENABLED``). Stores decoded VDL Mode 2 /
ACARS messages in a SEPARATE SQLite database (``RSBS_VDL2_DB_PATH``) so the
core ``history.db`` schema is never touched. When the feature is disabled the
core app imports none of this at request time and behaves exactly as before.
"""
