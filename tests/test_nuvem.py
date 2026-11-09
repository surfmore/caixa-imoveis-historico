import copy
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import coletar_caixa as coletor
import executar_nuvem as nuvem
from test_coletor_caixa import planilha


class GithubSimulado(nuvem.GitHub):
    def __init__(self, publico=True):
        super().__init__('exemplo/caixa', 'TOKEN_FICTICIO')
        self.is_public = publico
        self.calls = []
        self.latest = None
        self.releases = []
        self.fail_upload = False

    def api(self, method, endpoint, body=None, upload=False):
        self.calls.append((method, endpoint, upload))
        if method == 'GET' and endpoint == self.base:
            return {'private': not self.is_public, 'visibility': 'public' if self.is_public else 'private'}
        if method == 'GET' and endpoint.endswith('/releases/latest'):
            if self.latest is None:
                raise HTTPError(nuvem.API + endpoint, 404, 'not found', {}, None)
            return copy.deepcopy(self.latest)
        if method == 'POST' and endpoint.endswith('/releases'):
            release = {**body, 'id': len(self.releases) + 1, 'assets': []}
            self.releases.append(release)
            return copy.deepcopy(release)
        if method == 'POST' and upload:
            if self.fail_upload:
                raise HTTPError(nuvem.UPLOADS + endpoint, 503, 'erro simulado', {}, None)
            release_id = int(endpoint.split('/releases/')[1].split('/')[0])
            release = self.releases[release_id - 1]
            nome = parse_qs(urlsplit(endpoint).query)['name'][0]
            asset = {'name': nome, 'size': len(body), 'bytes': body,
                     'browser_download_url': f'https://github.com/{self.repo}/releases/download/{release["tag_name"]}/{nome}'}
            release['assets'].append(asset)
            return copy.deepcopy(asset)
        if method == 'PATCH':
            release_id = int(endpoint.rsplit('/', 1)[1])
            release = self.releases[release_id - 1]
            release.update(body)
            self.latest = release
            return copy.deepcopy(release)
        raise AssertionError((method, endpoint))

    def baixar_asset(self, asset):
        return asset['bytes']


class NuvemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.github = GithubSimulado()
        env = patch.dict(os.environ, {'GITHUB_SHA': 'a' * 40, 'GITHUB_RUN_ID': '123'})
        env.start()
        self.addCleanup(env.stop)

    def rodar(self, dados=None):
        dados = planilha() if dados is None else dados
        with patch.object(coletor, 'baixar_lista', return_value=(dados, {})):
            return nuvem.executar(self.github, self.root)

    def status(self):
        return json.loads((self.root / 'status' / 'ultima_execucao.json').read_text(encoding='utf-8'))

    def test_publica_csv_original_e_json_so_apos_uploads(self):
        resultado = self.rodar()
        release = self.github.latest
        self.assertEqual(resultado['status'], 'arquivado')
        self.assertFalse(release['draft'])
        self.assertEqual(len(release['assets']), 2)
        self.assertEqual(release['assets'][0]['bytes'], planilha())
        self.assertEqual(release['make_latest'], 'true')
        self.assertEqual(self.github.calls[-1][0], 'PATCH')
        self.assertIn('/releases/download/', self.status()['url_arquivo'])

    def test_nova_execucao_restaura_anterior_e_descarta_duplicata(self):
        primeiro = self.rodar()
        segundo = self.rodar()
        self.assertEqual(segundo['status'], 'sem_alteracao')
        self.assertEqual(primeiro['url_arquivo'], segundo['url_arquivo'])
        self.assertEqual(len(self.github.releases), 1)
        linhas = [json.loads(linha) for p in (self.root / 'status' / 'execucoes').glob('*.jsonl')
                  for linha in p.read_text(encoding='utf-8').splitlines()]
        self.assertEqual([r['status'] for r in linhas], ['arquivado', 'sem_alteracao'])

    def test_a_b_a_preserva_tres_releases(self):
        self.rodar()
        self.rodar(planilha('90.000,00'))
        self.rodar()
        self.assertEqual(len(self.github.releases), 3)
        self.assertEqual(self.github.releases[0]['assets'][0]['bytes'],
                         self.github.releases[2]['assets'][0]['bytes'])

    def test_repositorio_privado_bloqueia_antes_do_download(self):
        self.github.is_public = False
        with patch.object(coletor, 'baixar_lista') as download, self.assertRaisesRegex(ValueError, 'público'):
            nuvem.executar(self.github, self.root)
        download.assert_not_called()
        self.assertFalse(self.github.releases)
        self.assertEqual(self.status()['status'], 'erro')

    def test_html_nao_cria_release(self):
        with self.assertRaises(coletor.PortalBloqueado):
            self.rodar(b'<html>bloqueado</html>')
        self.assertFalse(self.github.releases)
        self.assertEqual(self.status()['status'], 'erro')

    def test_corrompimento_da_planilha_anterior_bloqueia(self):
        self.rodar()
        self.github.latest['assets'][0]['bytes'] += b'corrompido'
        with patch.object(coletor, 'baixar_lista') as download, self.assertRaisesRegex(ValueError, 'SHA-256'):
            nuvem.executar(self.github, self.root)
        download.assert_not_called()
        self.assertEqual(len(self.github.releases), 1)

    def test_falha_upload_deixa_rascunho_e_preserva_ultima_release(self):
        self.rodar()
        anterior = copy.deepcopy(self.github.latest)
        self.github.fail_upload = True
        with self.assertRaises(HTTPError):
            self.rodar(planilha('90.000,00'))
        self.assertEqual(self.github.latest, anterior)
        self.assertTrue(self.github.releases[-1]['draft'])
        self.assertEqual(self.status()['status'], 'erro')

    def test_falha_no_status_apos_publicacao_nao_duplica_snapshot(self):
        with patch.object(nuvem, 'registrar_status', side_effect=OSError('disco cheio')):
            with self.assertRaises(OSError):
                self.rodar()
        novo = self.rodar()
        self.assertEqual(novo['status'], 'sem_alteracao')
        self.assertEqual(len(self.github.releases), 1)

    def test_release_estranha_e_metadados_ausentes_bloqueiam(self):
        self.rodar()
        self.github.latest['tag_name'] = 'software-v1'
        with self.assertRaises(ValueError):
            self.rodar()
        self.github.latest['tag_name'] = 'coleta-teste'
        self.github.latest['assets'].pop()
        with self.assertRaises(ValueError):
            self.rodar()
        self.assertEqual(len(self.github.releases), 1)

    def test_falha_da_api_nao_e_tratada_como_primeira_execucao(self):
        real_api = self.github.api
        def api(method, endpoint, body=None, upload=False):
            if endpoint.endswith('/releases/latest'):
                raise HTTPError(nuvem.API + endpoint, 403, 'acesso negado', {}, None)
            return real_api(method, endpoint, body, upload)
        self.github.api = api
        with patch.object(coletor, 'baixar_lista') as download, self.assertRaises(HTTPError):
            nuvem.executar(self.github, self.root)
        download.assert_not_called()
        self.assertFalse(self.github.releases)

    def test_urls_nao_confiaveis_e_redirecionamento_da_api_bloqueados(self):
        for url in ('http://github.com/a', 'https://outro-site.example/a', 'https://github.com@outro-site.example/a'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                nuvem.DownloadsPublicos.validar(url)
        with self.assertRaises(ValueError):
            nuvem.SemRedirecionamento().redirect_request(None, None, 302, '', {}, 'https://outro-site.example/')
        with self.assertRaises(ValueError):
            nuvem.GitHub('', 'TOKEN_FICTICIO')
        with self.assertRaises(ValueError):
            nuvem.GitHub.baixar_asset(self.github, {'browser_download_url': 'https://github.com/outra/conta/releases/download/tag/a', 'size': 1})

    def test_workflow_so_usa_runner_padrao_publico_e_nao_cria_artefatos(self):
        root = Path(nuvem.__file__).resolve().parent
        workflow = (root / '.github' / 'workflows' / 'coletar-caixa.yml').read_text(encoding='utf-8')
        self.assertIn('github.event.repository.private == false', workflow)
        self.assertIn('runs-on: ubuntu-latest', workflow)
        self.assertNotIn('upload-artifact', workflow)
        self.assertNotIn('cloudflare', workflow)


if __name__ == '__main__':
    unittest.main()
