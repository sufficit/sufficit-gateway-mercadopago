#!/usr/bin/env bash
# CI local deste repositório.
#
# O GitHub Actions foi desativado em 2026-08-16 em todos os repositórios
# privados da organização — Actions em privado consome minutos pagos. Este
# script valida o repositório localmente e, com GH_TOKEN no ambiente, publica
# o resultado como status do commit (o ✓/✗ de sempre) e comentário no PR, via
# scripts/ci-report.py. Os workflows ficam em .github/workflows como
# documentação; reativar Actions em Settings volta tudo ao que era.
#
# Uso: bash scripts/ci-local.sh [--fast]   (--fast pula dependências irmãs)
set -euo pipefail

FAST=0
[[ "${1:-}" == "--fast" ]] && FAST=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PARENT="$(dirname "$ROOT")"
cd "$ROOT"

LOG="$(mktemp /tmp/ci-local.XXXXXX.log)"
exec > >(tee "$LOG") 2>&1

CURRENT_STEP="preparação"
CI_DONE=0
report() { python3 "$ROOT/scripts/ci-report.py" "$@" || true; }
on_exit() {
  local code=$?
  [[ "$CI_DONE" -eq 1 ]] && return
  if [[ $code -eq 1 ]]; then report failure --step "$CURRENT_STEP" --log "$LOG"
  else report error --step "$CURRENT_STEP" --log "$LOG"; fi
}
trap on_exit EXIT
step() { CURRENT_STEP="$*"; printf '\n\033[1;34m== %s ==\033[0m\n' "$*"; }
fail() { printf '\033[1;31mFALHOU: %s\033[0m\n' "$*"; exit 1; }

report pending

# Dependências irmãs — extraídas dos workflows deste repositório: são os
# checkouts lado a lado que as ProjectReference relativas esperam.
SIBLINGS=()
if [[ ${#SIBLINGS[@]} -gt 0 && "$FAST" -eq 0 ]]; then
  step "Dependências irmãs em $PARENT"
  for repo in "${SIBLINGS[@]}"; do
    if [[ -d "$PARENT/$repo/.git" ]]; then
      git -C "$PARENT/$repo" pull --ff-only --quiet 2>/dev/null \
        && echo "  $repo: atualizado" || echo "  $repo: mantido como está"
    else
      git clone --quiet --depth 1 "https://github.com/sufficit/$repo.git" "$PARENT/$repo" \
        && echo "  $repo: clonado" || fail "clone de $repo (credencial git?)"
    fi
  done
fi

# Encoding, quando o repositório carrega o verificador.
if [[ -f "$ROOT/scripts/check_mojibake.py" ]]; then
  step "Verificação de encoding"
  python3 "$ROOT/scripts/check_mojibake.py" || fail "encoding"
fi

# npm, onde houver package.json.
for dir in "$ROOT" "$ROOT/src" "$ROOT/server"; do
  if [[ -f "$dir/package.json" ]]; then
    step "npm ($(basename "$dir"))"
    ( cd "$dir"
      if [[ -f package-lock.json ]]; then npm ci --omit=dev --silent
      else npm install --omit=dev --silent; fi ) || fail "npm em $dir"
  fi
done

BUILT=0

# .NET: solution na raiz quando existir; senão, cada csproj fora de obj/bin.
SLN="$(ls -1 "$ROOT"/*.slnx "$ROOT"/*.sln 2>/dev/null | head -1 || true)"
if [[ -n "$SLN" ]]; then
  step "dotnet restore ($(basename "$SLN"))"
  dotnet restore "$SLN" || fail "restore"
  step "dotnet build (Release)"
  dotnet build "$SLN" --configuration Release --no-restore || fail "build"
  BUILT=1
elif compgen -G "$ROOT/**/*.csproj" > /dev/null || ls "$ROOT"/*/*.csproj >/dev/null 2>&1; then
  step "dotnet build por projeto (sem solution na raiz)"
  while IFS= read -r proj; do
    echo "  -> $proj"
    dotnet build "$proj" --configuration Release || fail "build: $proj"
  done < <(find "$ROOT" -name "*.csproj" -not -path "*/obj/*" -not -path "*/bin/*" -not -path "*/Vendor/*" | sort)
  BUILT=1
fi

# Testes .NET: projetos com Test no nome.
while IFS= read -r proj; do
  step "dotnet test ($(basename "$proj"))"
  dotnet test "$proj" --configuration Release --no-build --verbosity minimal \
    || fail "testes: $proj"
done < <(find "$ROOT" -name "*Test*.csproj" -not -path "*/obj/*" -not -path "*/bin/*" | sort)

# Go, quando for o caso.
if [[ -f "$ROOT/go.mod" ]]; then
  step "go build + test"
  go build ./... || fail "go build"
  go test ./... || fail "go test"
  BUILT=1
fi

[[ "$BUILT" -eq 1 ]] || fail "nenhum alvo reconhecido para construir (sln, csproj, go.mod)"

printf '\n\033[1;32mCI local: tudo verde.\033[0m\n'
CI_DONE=1
report success --log "$LOG"
