"""Coleta em repositório público: CSVs em Releases e consultas em status/."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import coletar_caixa as coletor

API = 'https://api.github.com'
UPLOADS = 'https://uploads.github.com'
REPO_RE = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
RELEASE_PREFIX = 'coleta-'


class SemRedirecionamento(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Redirecionamento inesperado na API do GitHub.')


class DownloadsPublicos(HTTPRedirectHandler):
    @staticmethod
    def validar(url):
        p = urlsplit(url)
        hosts = {'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
        if p.scheme != 'https' or p.hostname not in hosts or p.username or p.password or p.port not in (None, 443):
            raise ValueError('URL de download do GitHub não reconhecida.')

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.validar(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitHub:
    def __init__(self, repo, token):
        if not REPO_RE.fullmatch(repo) or not token:
            raise ValueError('Informe GITHUB_REPOSITORY e GITHUB_TOKEN.')
        self.repo = repo
        self.token = token
        self.base = f'/repos/{repo}'
        self.opener = build_opener(SemRedirecionamento())

    def api(self, method, endpoint, body=None, upload=False):
        if not endpoint.startswith(self.base + '/') and endpoint != self.base:
            raise ValueError('A operação precisa pertencer ao repositório do coletor.')
        binary = isinstance(body, bytes)
        data = body if binary else json.dumps(body).encode('utf-8') if body is not None else None
        headers = {'Authorization': 'Bearer ' + self.token,
                   'User-Agent': 'CaixaHistorico/1.0',
                   'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28'}
        if data is not None:
            headers['Content-Type'] = 'application/octet-stream' if binary else 'application/json'
        req = Request((UPLOADS if upload else API) + endpoint, data=data, headers=headers, method=method)
        with self.opener.open(req, timeout=90) as response:
            raw = response.read(4_000_001)
            if len(raw) > 4_000_000:
                raise ValueError('Resposta da API excedeu o limite.')
            return json.loads(raw)

    def baixar_asset(self, asset):
        url = asset['browser_download_url']
        DownloadsPublicos.validar(url)
        if not urlsplit(url).path.startswith('/' + self.repo + '/releases/download/'):
            raise ValueError('Asset de outro repositório.')
        limite = coletor.MAX_BYTES
        if asset['size'] > limite:
            raise ValueError('Asset maior que o limite do coletor.')
        # Sem Authorization: o token nunca acompanha o redirecionamento ao CDN.
        req = Request(url, headers={'User-Agent': 'CaixaHistorico/1.0', 'Cache-Control': 'no-cache'})
        with build_opener(DownloadsPublicos()).open(req, timeout=90) as response:
            DownloadsPublicos.validar(response.url)
            conteudo = response.read(limite + 1)
        if len(conteudo) != asset['size'] or len(conteudo) > limite:
            raise ValueError('Asset incompleto ou maior que o limite.')
        return conteudo

    def publico(self):
        repo = self.api('GET', self.base)
        if repo.get('private') is not False or repo.get('visibility') != 'public':
            raise ValueError('Coleta bloqueada: use repositório público para evitar minutos privados faturáveis.')
        return repo

    def ultima_release(self):
        try:
            release = self.api('GET', self.base + '/releases/latest')
        except HTTPError as erro:
            if erro.code == 404:
                return None
            raise
        if not release['tag_name'].startswith(RELEASE_PREFIX) or release.get('draft') or release.get('prerelease'):
            raise ValueError('A última release não pertence ao coletor. Use um repositório exclusivo para este projeto.')
        return release

    def publicar(self, evento, csv, metadata):
        tag = RELEASE_PREFIX + csv.stem.removeprefix('caixa_imoveis_')
        release = self.api('POST', self.base + '/releases', {
            'tag_name': tag, 'target_commitish': os.environ['GITHUB_SHA'],
            'name': 'Lista CAIXA - ' + evento['fim_download'],
            'body': (f"Coleta nacional: {evento['quantidade_imoveis']} imóveis.\n"
                     f"Download: {evento['fim_download']}.\n"
                     f"Geração declarada: {evento['data_geracao_caixa']}.\n"
                     f"SHA-256 do CSV: {evento['sha256']}.\n"
                     f"Fonte: {coletor.PAGE_URL}\n"),
            'draft': True, 'prerelease': False})
        assets = []
        for arquivo in (csv, metadata):
            endpoint = self.base + f"/releases/{release['id']}/assets?" + urlencode({'name': arquivo.name})
            assets.append(self.api('POST', endpoint, arquivo.read_bytes(), upload=True))
        # Publica somente depois de receber os dois arquivos. Falhas deixam rascunho.
        self.api('PATCH', self.base + f"/releases/{release['id']}",
                 {'draft': False, 'make_latest': 'true'})
        return next(asset['browser_download_url'] for asset in assets if asset['name'] == csv.name)


def restaurar_anterior(github, release, pasta):
    if release is None:
        return None
    assets = {asset['name']: asset for asset in release['assets']}
    nomes = [nome for nome in assets if coletor.SNAPSHOT_RE.fullmatch(nome)]
    if len(nomes) != 1 or len(assets) != 2:
        raise ValueError('A última release precisa conter exatamente um CSV do coletor e seu JSON.')
    nome = nomes[0]
    json_nome = nome.removesuffix('.csv') + '.json'
    if json_nome not in assets:
        raise ValueError('Metadados da última planilha ausentes.')
    metadata = github.baixar_asset(assets[json_nome])
    evento = json.loads(metadata)
    dados = github.baixar_asset(assets[nome])
    if evento.get('bytes') != len(dados) or evento.get('sha256') != hashlib.sha256(dados).hexdigest():
        raise ValueError('SHA-256/tamanho da planilha anterior divergem dos metadados.')
    coletor.validar_csv(dados)
    ano, mes = nome[len('caixa_imoveis_'):].split('-')[:2]
    destino = pasta / 'planilhas' / ano / mes / nome
    coletor.escrever_atomico(destino.with_suffix('.json'), metadata)
    coletor.escrever_atomico(destino, dados)
    return assets[nome]['browser_download_url']


def registrar_status(root, evento):
    pasta = root / 'status'
    instante = coletor.agora()
    evento = {**evento, 'registrado_em': coletor.iso(instante)}
    coletor.escrever_atomico(pasta / 'ultima_execucao.json', coletor.json_bytes(evento))
    consultas = pasta / 'execucoes'
    consultas.mkdir(parents=True, exist_ok=True)
    with (consultas / (instante.strftime('%Y-%m-%d') + '.jsonl')).open('a', encoding='utf-8', newline='\n') as saida:
        saida.write(json.dumps(evento, ensure_ascii=False) + '\n')


def executar(github, root):
    root = Path(root)
    inicio = coletor.iso(coletor.agora())
    run_url = 'https://github.com/' + github.repo + '/actions/runs/' + os.environ.get('GITHUB_RUN_ID', '')
    try:
        github.publico()
        release = github.ultima_release()
        with TemporaryDirectory(prefix='caixa-') as temporario:
            pasta = Path(temporario)
            url_anterior = restaurar_anterior(github, release, pasta)
            evento = coletor.executar(pasta, consultar_pagina=False)
            if evento['status'] == 'arquivado':
                csv = pasta / evento['arquivo']
                url = github.publicar(evento, csv, csv.with_suffix('.json'))
            else:
                url = url_anterior
            evento.update(url_arquivo=url, workflow=run_url, repositorio=github.repo)
        registrar_status(root, evento)
        logging.info('%s | %s imóveis | %s', evento['status'], evento['quantidade_imoveis'], url)
        return evento
    except Exception as erro:
        evento = {'versao': 1, 'status': 'erro', 'inicio': inicio,
                  'fim': coletor.iso(coletor.agora()), 'tipo_erro': type(erro).__name__,
                  'erro': str(erro), 'workflow': run_url}
        registrar_status(root, evento)
        raise


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    github = GitHub(os.environ.get('GITHUB_REPOSITORY', ''), os.environ.get('GITHUB_TOKEN', ''))
    try:
        evento = executar(github, Path(__file__).resolve().parent)
        code = 0
    except Exception as erro:
        logging.error('Coleta falhou: %s', erro)
        evento = {'status': 'erro', 'erro': str(erro)}
        code = 1
    resumo = os.environ.get('GITHUB_STEP_SUMMARY')
    if resumo:
        with Path(resumo).open('a', encoding='utf-8') as saida:
            saida.write('Resultado da coleta CAIXA:\n\n')
            saida.write('~~~json\n' + json.dumps(evento, ensure_ascii=False, indent=2) + '\n~~~\n')
    return code


if __name__ == '__main__':
    sys.exit(main())
