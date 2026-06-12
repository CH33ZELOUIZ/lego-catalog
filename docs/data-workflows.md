# Data workflows

## Owned/wanted tracking

Track items as owned, wanted, or review-needed. The review state is useful when a match is uncertain.

## CSV import/export

CSV keeps the catalog portable. A good import flow should:

- preserve existing rows where possible
- avoid duplicate records
- let users review ambiguous matches
- support export before bulk changes

## Image cache

Remote images should be cached locally. The cache should be rebuildable and excluded from git.
