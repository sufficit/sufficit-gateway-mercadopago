#!/usr/bin/env python3
"""Publica o resultado do CI local no GitHub, junto do commit e do PR.

Usa a Commit Status API — o mecanismo que o GitHub mostra como ✓/✗ em commits
e pull requests — que funciona com um token comum e não depende do Actions
(desativado nos repositórios privados por consumir minutos pagos).

Chamado pelo ci-local.sh:

  ci-report.py pending
  ci-report.py success --log /tmp/ci.log
  ci-report.py failure --step "dotnet build" --log /tmp/ci.log

Com --log, o arquivo vira um gist secreto e o status aponta para ele, então o
✗ no PR leva direto ao log completo. Se o branch tiver um PR aberto, um
comentário é criado (ou atualizado — nunca empilhado) com o resumo.

Configuração por variáveis de ambiente:
  GH_TOKEN     obrigatório (escopo repo)
  GH_REPO      opcional; padrão: deduzido do remote origin
  GH_CONTEXT   opcional; padrão "ci-local" (nome do check no PR)
"""
from __future__ import annotations

import argparse
from pathlib import Path
import json
import os
import subprocess
import sys
import urllib.request

API = "https://api.github.com"
MARKER = "<!-- ci-local-report -->"
GIST_LIMIT = 900_000  # bytes; a API de gists rejeita arquivos muito grandes


def sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout.strip()


def repo_slug() -> str:
    if slug := os.environ.get("GH_REPO"):
        return slug
    url = sh("git", "remote", "get-url", "origin")
    # aceita https://github.com/org/repo(.git) e git@github.com:org/repo(.git)
    tail = url.split("github.com", 1)[1].lstrip(":/")
    return tail.removesuffix(".git")


def call(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp) if resp.length != 0 else {}


def upload_log(path: str, sha: str, state: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        if len(data) > GIST_LIMIT:
            # mantém o fim, onde mora a falha; o começo é o menos informativo
            data = b"[... inicio truncado ...]\n" + data[-GIST_LIMIT:]
        gist = call("POST", "/gists", {
            "description": f"ci-local {state} @ {sha[:10]}",
            "public": False,
            "files": {"ci-local.log": {"content": data.decode(errors="replace")}},
        })
        return gist.get("html_url")
    except Exception as exc:  # o log é acessório; o status nunca deixa de sair por causa dele
        print(f"ci-report: gist indisponivel ({exc})", file=sys.stderr)
        return None


def comment_on_pr(slug: str, branch: str, body: str) -> None:
    org = slug.split("/", 1)[0]
    prs = call("GET", f"/repos/{slug}/pulls?head={org}:{branch}&state=open")
    if not prs:
        return
    number = prs[0]["number"]
    for existing in call("GET", f"/repos/{slug}/issues/{number}/comments?per_page=100"):
        if MARKER in existing.get("body", ""):
            call("PATCH", f"/repos/{slug}/issues/comments/{existing['id']}", {"body": body})
            return
    call("POST", f"/repos/{slug}/issues/{number}/comments", {"body": body})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("state", choices=["pending", "success", "failure", "error"])
    parser.add_argument("--step", default="", help="etapa que falhou, para a descricao")
    parser.add_argument("--log", default="", help="arquivo de log a publicar como gist")
    parser.add_argument("--sha", default="", help="commit; padrao: HEAD")
    args = parser.parse_args()

    if not os.environ.get("GH_TOKEN"):
        print("ci-report: GH_TOKEN ausente; nada publicado (modo local puro)")
        return 0

    slug = repo_slug()
    sha = args.sha or sh("git", "rev-parse", "HEAD")
    branch = sh("git", "rev-parse", "--abbrev-ref", "HEAD")
    context = os.environ.get("GH_CONTEXT", "ci-local")

    description = {
        "pending": "validação local em andamento",
        "success": "validação local verde",
        "failure": f"falhou em: {args.step}" if args.step else "validação local falhou",
        "error": f"erro em: {args.step}" if args.step else "erro na validação local",
    }[args.state]

    target = upload_log(args.log, sha, args.state) if args.log and args.state != "pending" else None

    call("POST", f"/repos/{slug}/statuses/{sha}", {
        "state": args.state,
        "context": context,
        # a UI corta em ~140 caracteres
        "description": description[:140],
        **({"target_url": target} if target else {}),
    })
    print(f"ci-report: status '{args.state}' publicado em {slug}@{sha[:10]}")

    if args.state in ("success", "failure", "error"):
        icon = "✅" if args.state == "success" else "❌"
        lines = [
            MARKER,
            f"{icon} **CI local:** {description}",
            f"commit `{sha[:10]}`" + (f" · [log completo]({target})" if target else ""),
        ]
        # Sem gist (token sem escopo `gist`, por exemplo), a falha ainda precisa
        # ser diagnosticável a partir do PR: as últimas linhas do log entram no
        # próprio comentário, recolhidas.
        if args.log and not target and args.state != "success":
            try:
                tail = Path(args.log).read_text(errors="replace").splitlines()[-60:]
                block = "\n".join(tail)[-6000:]
                lines += ["", "<details><summary>últimas linhas do log</summary>", "",
                          "```", block, "```", "</details>"]
            except OSError:
                pass
        try:
            comment_on_pr(slug, branch, "\n".join(lines))
        except Exception as exc:
            print(f"ci-report: comentario de PR indisponivel ({exc})", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
