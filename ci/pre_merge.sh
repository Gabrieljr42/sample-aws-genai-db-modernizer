#!/usr/bin/env bash
# Cheap pre-merge failsafe: run before pushing or asking for a merge.
#
#   ./ci/pre_merge.sh [--quick]
#
# fetch main, count commits behind, check for merge conflicts without touching
# the worktree, check contract versions, warn on stale generated files, then lint,
# tests (skipped by --quick) and the needs_llm_run verdict. Exits non-zero on a
# conflict, a lint or test failure, or a contract version problem.
# shellcheck source=lib.sh source-path=SCRIPTDIR
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

quick=0
for arg in "$@"; do
  case "$arg" in
    --quick) quick=1 ;;
    -h | --help) sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "usage: ci/pre_merge.sh [--quick]" >&2; exit 2 ;;
  esac
done

helper() { uv run python ci/pre_merge.py "$@"; }
summary=()
failed=0
note() { printf '%s\n' "$*"; summary+=("$*"); }
fail() { failed=1; note "FAIL $*"; }

# a. Fetch upstream main.
remote="$(git remote -v | helper pick-remote)"
base="$remote/main"
if git fetch -q "$remote" main 2>/dev/null; then
  note "ok   fetch: $base is up to date with the remote"
elif git rev-parse -q --verify "$base" >/dev/null; then
  note "warn fetch: could not reach $remote, using the local $base"
else
  fail "fetch: could not fetch $remote main and there is no local $base"
  echo "pre_merge: cannot continue without $base" >&2
  exit 1
fi

# b. Behind count.
behind="$(git rev-list --count "HEAD..$base")"
ahead="$(git rev-list --count "$base..HEAD")"
if [ "$behind" -eq 0 ]; then
  note "ok   behind: 0 commits behind $base ($ahead ahead)"
else
  note "warn behind: $behind commit(s) behind $base ($ahead ahead); rebase and re-test before merging"
fi

# c. Conflict check against main. merge-tree writes objects only: no index, worktree or ref changes.
conflict_files=""
set +e
mt_out="$(git merge-tree --write-tree --name-only --no-messages HEAD "$base" 2>/dev/null)"
mt_rc=$?
set -e
if [ "$mt_rc" -eq 0 ]; then
  note "ok   conflicts: none against $base"
elif [ "$mt_rc" -eq 1 ]; then
  conflict_files="$(printf '%s\n' "$mt_out" | helper merge-tree-files)"
  fail "conflicts: $(printf '%s\n' "$conflict_files" | wc -l | tr -d ' ') conflicting file(s) against $base"
  printf '%s\n' "$conflict_files" | sed 's/^/       /'
else
  # git older than 2.38: no `merge-tree --write-tree`. Approximate with files both sides touched.
  mb="$(git merge-base HEAD "$base")"
  conflict_files="$(comm -12 \
    <(git diff --name-only "$mb" HEAD | sort) \
    <(git diff --name-only "$mb" "$base" | sort))"
  if [ -z "$conflict_files" ]; then
    note "ok   conflicts: no file changed on both sides (old git, approximate check)"
  else
    note "warn conflicts: files changed on both sides, may conflict (old git, approximate):"
    printf '%s\n' "$conflict_files" | sed 's/^/       /'
    failed=1
  fi
fi

# d. Contract versions.
changed="$(git diff --name-only "$base...HEAD")"
contracts="$(printf '%s\n' "$changed" | grep -E '^src/contracts/[^/]+\.py$' || true)"
if [ -z "$contracts" ]; then
  note "ok   contracts: no contract file changed"
else
  # shellcheck disable=SC2086 # one path per word is intended
  if contract_out="$(helper contracts "$base" $contracts)"; then
    note "ok   contracts: versions checked ($(printf '%s\n' "$contracts" | wc -l | tr -d ' ') file(s))"
    printf '%s\n' "$contract_out" | sed 's/^/       /'
  else
    fail "contracts: version problem"
    printf '%s\n' "$contract_out" | sed 's/^/       /'
  fi
fi

# e. Generated files that differ from main must still match their generator.
check_generated() {  # check_generated NAME "path|path" command...
  local name="$1" paths="$2" p hit=0
  shift 2
  for p in ${paths//|/ }; do
    printf '%s\n' "$changed" | grep -qxF "$p" && hit=1
  done
  [ "$hit" -eq 1 ] || return 0
  if "$@" >/dev/null 2>&1; then
    note "ok   generated: $name differs from main and its generator check passes"
  else
    note "warn generated: $name differs from main but its generator check fails; regenerate it on top of main, never hand-merge ($*)"
  fi
}
check_generated "routing snapshot" "tests/e2e/baselines/routing_snapshot.json" \
  uv run pytest tests/e2e/test_routing_snapshot.py -p no:xdist -q
check_generated "contract schema snapshot" "tests/contract/schema_snapshot.json" \
  uv run pytest tests/contract/test_schema_snapshot.py -p no:xdist -q
check_generated "rendered catalog" "docs/architecture/rule-catalog.md|src/ui/src/utils/engineCatalog.generated.js" \
  uv run python scripts/render_catalog.py --check
check_generated "report template" "src/report/templates/analysis_report.css|src/report/templates/analysis_report.js|src/report/templates/analysis_report.html.tpl" \
  uv run python scripts/sync_report_template.py --check

# f. Lint.
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
if ./ci/lint.sh >"$tmp" 2>&1; then
  note "ok   lint: ci/lint.sh passed"
else
  tail -n 20 "$tmp"
  fail "lint: ci/lint.sh failed (last lines above)"
fi

# g. Tests.
if [ "$quick" -eq 1 ]; then
  note "skip tests: --quick"
elif ./ci/test.sh -q >"$tmp" 2>&1; then
  note "ok   tests: $(tail -n 1 "$tmp")"
else
  tail -n 20 "$tmp"
  fail "tests: ci/test.sh -q failed (last lines above)"
fi

# h. Real-model runs the branch needs (informational).
if verdict="$(./ci/needs_llm_run.sh "$base" 2>&1)"; then
  note "info llm: $(printf '%s\n' "$verdict" | grep -E '^[a-z0-9-]+: ' | tr '\n' ';' | sed 's/;$//; s/;/; /g')"
else
  note "warn llm: needs_llm_run could not decide ($verdict)"
fi

log "pre_merge summary"
for line in "${summary[@]}"; do echo "$line"; done
if [ "$failed" -eq 0 ]; then
  echo "pre_merge: OK"
else
  echo "pre_merge: FAILED"
fi
exit "$failed"
