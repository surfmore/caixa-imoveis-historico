"""Arquiva a lista nacional da CAIXA. Python 3.10+, somente biblioteca padrão."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import filecmp
import hashlib
from http.cookiejar import CookieJar
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, Request, build_opener
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent
PAGE_URL = 'https://venda-imoveis.caixa.gov.br/sistema/download-lista.asp'
CSV_URL = 'https://venda-imoveis.caixa.gov.br/listaweb/Lista_imoveis_geral.csv'
HOST = 'venda-imoveis.caixa.gov.br'
MAX_BYTES = 64 * 1024 * 1024
UFS = set('AC AL AM AP BA CE DF ES GO MA MG MS MT PA PB PE PI PR RJ RN RO RR RS SC SE SP TO'.split())
SNAPSHOT_RE = re.compile(r'^caixa_imoveis_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-\d{6}[+-]\d{4}\.csv$')
try:
    FUSO = ZoneInfo('America/Sao_Paulo')
    FUSO_ORIGEM = 'IANA America/Sao_Paulo'
except ZoneInfoNotFoundError:
    FUSO = timezone(timedelta(hours=-3))
    FUSO_ORIGEM = 'UTC-03 fixo (tzdata ausente)'


class PortalBloqueado(ValueError):
    pass


class ColetaEmAndamento(Exception):
    pass


def agora():
    return datetime.now(FUSO)


def iso(data):
    return data.isoformat(timespec='microseconds')


def normalizar(texto):
    return ''.join(c for c in unicodedata.normalize('NFKD', texto.strip().lower())
                   if not unicodedata.combining(c))


def validar_url(url):
    p = urlsplit(url)
    if (p.scheme != 'https' or p.hostname != HOST or p.username or p.password
            or p.port not in (None, 443)):
        raise PortalBloqueado('A CAIXA redirecionou para validação externa. Tente novamente mais tarde e confira o portal no navegador.')


class RedirecionamentoOficial(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validar_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def requisitar(opener, url, limite, timeout):
    validar_url(url)
    req = Request(url, headers={
        'User-Agent': 'Mozilla/5.0',
        'Referer': PAGE_URL,
        'Accept': 'text/csv,application/octet-stream,text/html;q=0.8,*/*;q=0.5',
        'Cache-Control': 'no-cache',
    })
    with opener.open(req, timeout=timeout) as response:
        validar_url(response.url)
        data = response.read(limite + 1)
        if len(data) > limite:
            raise ValueError(f'Resposta excedeu o limite de {limite} bytes.')
        length = response.headers.get('Content-Length')
        if length and len(data) != int(length):
            raise OSError('Download incompleto: tamanho diferente do Content-Length.')
        headers = {k: response.headers.get(k) for k in
                   ('Content-Type', 'Content-Length', 'Last-Modified', 'ETag', 'Date')}
        return data, headers, response.url


def baixar_lista(timeout=60, tentativas=3, consultar_pagina=True):
    """Usa o mesmo arquivo da opção Todos e mantém cookies somente nesta execução."""
    for tentativa in range(1, tentativas + 1):
        try:
            opener = build_opener(RedirecionamentoOficial(), HTTPCookieProcessor(CookieJar()))
            if consultar_pagina:
                pagina, _, _ = requisitar(opener, PAGE_URL, 2_000_000, timeout)
                html = pagina.decode('utf-8', errors='replace')
                if not re.search(r'<option\b[^>]*value=[\'\"]geral[\'\"]', html, re.I):
                    raise PortalBloqueado('Página não contém a opção Todos (geral). O portal pode ter mudado ou bloqueado a consulta.')
                # O sufixo variável é o que a própria página oficial usa para evitar cache.
                match = re.search(r'\.csv\?(\d+)', html)
                sufixo = match.group(1) if match else str(time.time_ns())
            else:
                # Exportação nacional pública, idêntica ao botão Todos da página.
                sufixo = str(time.time_ns())
            url = CSV_URL + '?' + sufixo
            conteudo, headers, final_url = requisitar(opener, url, MAX_BYTES, timeout)
            return conteudo, {'url_solicitada': url, 'url_final': final_url,
                              'cabecalhos_http': headers, 'tentativa': tentativa}
        except HTTPError as erro:
            if erro.code not in (408, 429, 500, 502, 503, 504) or tentativa == tentativas:
                raise
        except (URLError, TimeoutError, ConnectionError, OSError):
            if tentativa == tentativas:
                raise
        logging.warning('Falha temporária na tentativa %s; repetindo.', tentativa)
        time.sleep(5 * tentativa)
    raise RuntimeError('Download não concluído.')


def validar_csv(conteudo):
    """Valida sem regravar ou normalizar os bytes que serão arquivados."""
    if not conteudo:
        raise ValueError('A resposta está vazia.')
    for encoding in ('utf-8-sig', 'cp1252'):
        try:
            texto = conteudo.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError('Codificação da planilha não reconhecida.')
    if re.search(r'<(?:!doctype|html|head|script)\b|Radware Bot Manager|captcha', texto[:4000], re.I):
        raise PortalBloqueado('A resposta é uma página HTML de erro/validação, não uma planilha. Nenhum CSV foi arquivado.')
    csv.field_size_limit(MAX_BYTES)
    leitor = csv.reader(io.StringIO(texto, newline=''), delimiter=';', strict=True)
    cabecalho = None
    indices = {}
    modalidades, estados = Counter(), Counter()
    total = 0
    geracao = None
    try:
        for linha in leitor:
            if not any(c.strip() for c in linha):
                continue
            if cabecalho is None:
                for i, celula in enumerate(linha[:-1]):
                    if 'data de geracao' in normalizar(celula):
                        geracao = linha[i + 1].strip()
                nomes = [normalizar(c) for c in linha]
                obrigatorios = {'uf', 'cidade', 'preco', 'valor de avaliacao', 'modalidade de venda', 'link de acesso'}
                if obrigatorios.issubset(nomes):
                    cabecalho = linha
                    indices = {nome: nomes.index(nome) for nome in obrigatorios}
                    ids = [i for i, nome in enumerate(nomes) if 'imovel' in nome and ('n' == nome[:1] or 'numero' in nome)]
                    if not ids:
                        raise ValueError('Coluna do número do imóvel não encontrada.')
                    indices['id'] = ids[0]
                elif leitor.line_num > 30:
                    raise ValueError('Cabeçalho esperado não encontrado nas primeiras 30 linhas.')
                continue
            if len(linha) != len(cabecalho):
                raise ValueError(f'Linha {leitor.line_num} possui {len(linha)} colunas; esperado {len(cabecalho)}.')
            campos = {nome: linha[i].strip() for nome, i in indices.items()}
            if not campos['id'].isdigit() or campos['uf'] not in UFS or not campos['cidade']:
                raise ValueError(f'Imóvel, UF ou cidade inválido na linha {leitor.line_num}.')
            if not campos['modalidade de venda']:
                raise ValueError(f'Modalidade ausente na linha {leitor.line_num}.')
            for coluna in ('preco', 'valor de avaliacao'):
                valor = Decimal(campos[coluna].replace('.', '').replace(',', '.'))
                if not valor.is_finite() or valor < 0:
                    raise ValueError(f'Valor monetário inválido na linha {leitor.line_num}.')
            validar_url(campos['link de acesso'])
            modalidades[campos['modalidade de venda']] += 1
            estados[campos['uf']] += 1
            total += 1
    except (csv.Error, InvalidOperation) as erro:
        raise ValueError(f'CSV inválido perto da linha {leitor.line_num}: {erro}') from erro
    if cabecalho is None or total == 0:
        raise ValueError('Não foi encontrada uma lista de imóveis válida e não vazia.')
    return {'encoding': encoding, 'separador': ';', 'colunas': [c.strip() for c in cabecalho],
            'data_geracao_caixa': geracao, 'quantidade_imoveis': total,
            'modalidades': dict(sorted(modalidades.items())), 'estados': dict(sorted(estados.items()))}


@contextmanager
def trava(diretorio):
    """Trava do sistema operacional; liberada automaticamente se o processo cair."""
    diretorio.mkdir(parents=True, exist_ok=True)
    with (diretorio / '.coleta.lock').open('a+b') as arquivo:
        arquivo.seek(0, 2)
        if arquivo.tell() == 0:
            arquivo.write(b'0')
            arquivo.flush()
        arquivo.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(arquivo.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(arquivo.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as erro:
            raise ColetaEmAndamento('Outra coleta já está em andamento neste diretório.') from erro
        try:
            yield
        finally:
            arquivo.seek(0)
            if os.name == 'nt':
                msvcrt.locking(arquivo.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(arquivo.fileno(), fcntl.LOCK_UN)


def escrever_atomico(destino, conteudo):
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporario = destino.with_name('.' + destino.name + '.' + uuid4().hex + '.part')
    try:
        with temporario.open('xb') as arquivo:
            arquivo.write(conteudo)
            arquivo.flush()
            os.fsync(arquivo.fileno())
        os.replace(temporario, destino)
    finally:
        temporario.unlink(missing_ok=True)


def json_bytes(objeto):
    return (json.dumps(objeto, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def sha_arquivo(arquivo):
    digest = hashlib.sha256()
    with arquivo.open('rb') as entrada:
        for bloco in iter(lambda: entrada.read(1024 * 1024), b''):
            digest.update(bloco)
    return digest.hexdigest()


def ultimo_snapshot(diretorio):
    arquivos = (p for p in (diretorio / 'planilhas').rglob('caixa_imoveis_*.csv')
                if SNAPSHOT_RE.fullmatch(p.name))
    return max(arquivos, key=lambda p: p.name, default=None)


def arquivar(diretorio, conteudo, info, inicio, fim):
    resumo = validar_csv(conteudo)
    sha256 = hashlib.sha256(conteudo).hexdigest()
    anterior = ultimo_snapshot(diretorio)
    evento = {'versao': 1, 'inicio': iso(inicio), 'fim_download': iso(fim),
              'fuso': 'America/Sao_Paulo', 'fuso_origem': FUSO_ORIGEM,
              'pagina_origem': PAGE_URL, 'selecao_estado': 'Todos (geral)',
              'filtro_modalidade': None, 'sha256': sha256, 'bytes': len(conteudo),
              **info, **resumo}
    temporario = diretorio / ('.download-' + uuid4().hex + '.part')
    try:
        escrever_atomico(temporario, conteudo)
        # Hash + comparação de todos os bytes: igualdade exata, sem deduplicar versões antigas.
        igual = (anterior is not None and anterior.stat().st_size == len(conteudo)
                 and sha_arquivo(anterior) == sha256
                 and filecmp.cmp(anterior, temporario, shallow=False))
        if igual:
            evento.update(status='sem_alteracao', arquivo=anterior.relative_to(diretorio).as_posix())
            return evento
        nome = 'caixa_imoveis_' + fim.strftime('%Y-%m-%d_%H-%M-%S-%f%z') + '.csv'
        destino = diretorio / 'planilhas' / fim.strftime('%Y') / fim.strftime('%m') / nome
        if destino.exists():
            raise FileExistsError(f'Já existe um arquivo com este instante: {destino}')
        evento.update(status='arquivado', arquivo=destino.relative_to(diretorio).as_posix(),
                      arquivo_anterior=anterior.relative_to(diretorio).as_posix() if anterior else None)
        destino.parent.mkdir(parents=True, exist_ok=True)
        escrever_atomico(destino.with_suffix('.json'), json_bytes(evento))
        os.replace(temporario, destino)
        return evento
    finally:
        temporario.unlink(missing_ok=True)


def registrar_evento(diretorio, evento):
    pasta = diretorio / 'execucoes'
    pasta.mkdir(parents=True, exist_ok=True)
    # Uma linha por consulta permite distinguir "sem mudança" de falha/ausência de coleta.
    with (pasta / (agora().strftime('%Y-%m') + '.jsonl')).open('a', encoding='utf-8', newline='\n') as log:
        log.write(json.dumps(evento, ensure_ascii=False) + '\n')
        log.flush()
        os.fsync(log.fileno())


def executar(diretorio, timeout=60, tentativas=3, consultar_pagina=True):
    inicio = agora()
    with trava(diretorio):
        try:
            conteudo, info = baixar_lista(timeout, tentativas, consultar_pagina=consultar_pagina)
            evento = arquivar(diretorio, conteudo, info, inicio, agora())
            registrar_evento(diretorio, evento)
            return evento
        except Exception as erro:
            registrar_evento(diretorio, {'versao': 1, 'status': 'erro', 'inicio': iso(inicio),
                                         'fim': iso(agora()), 'tipo_erro': type(erro).__name__,
                                         'erro': str(erro), 'pagina_origem': PAGE_URL})
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--diretorio', type=Path, default=ROOT / 'dados' / 'historico_caixa',
                        help='Pasta do histórico (padrão: dados/historico_caixa junto ao script).')
    parser.add_argument('--timeout', type=int, default=60, help='Timeout de cada requisição, em segundos.')
    parser.add_argument('--tentativas', type=int, choices=range(1, 6), default=3)
    args = parser.parse_args(argv)
    if args.timeout < 1:
        parser.error('--timeout deve ser positivo.')
    diretorio = args.diretorio.resolve()
    diretorio.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(diretorio / 'coleta.log', maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[handler] + ([logging.StreamHandler()] if sys.stderr else []),
                        format='%(asctime)s %(levelname)s %(message)s', force=True)
    try:
        evento = executar(diretorio, args.timeout, args.tentativas)
        logging.info('%s: %s | %s imóveis | SHA-256 %s', evento['status'], evento['arquivo'],
                     evento['quantidade_imoveis'], evento['sha256'])
        return 0
    except ColetaEmAndamento as erro:
        logging.info('%s', erro)
        return 2
    except Exception as erro:
        logging.error('Coleta falhou: %s: %s', type(erro).__name__, erro)
        return 1


if __name__ == '__main__':
    sys.exit(main())
