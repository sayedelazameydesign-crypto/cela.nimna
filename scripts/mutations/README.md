# Mutation harnesses

Each script copies the tree to a temp dir, mutates **the copy**, runs the
target tests with `python -B`, and restores — the working tree is never
modified. Every listed mutation MUST break at least one test; a green run on a
mutation means a coverage hole that must be fixed before the corresponding
claim stands.

| Script | Ticket | Mutations |
|---|---|---|
| `run_t71_mutations.sh` | T7.1 binding migration | MB1 refusal→legacy-fallback · MB2 gateway-failure→legacy-fallback · MB3 swarm-refusal removed · MB4 binding ignored |
