from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

import coletar_caixa as c

TZ = timezone(timedelta(hours=-3))
T0 = datetime(2026, 11, 1, 7, 0, tzinfo=TZ)


def planilha(preco='100.000,00', encoding='cp1252', nova_linha='\n'):
    texto = (
        '\nLista de Imóveis da Caixa;;Data de geração:;30/10/2026;;;;;;;\n'
        ' N° do imóvel;UF;Cidade;Bairro;Endereço;Preço;Valor de avaliação;Desconto;'
        'Financiamento;Descrição;Modalidade de venda;Link de acesso\n\n'
        ' 123456 ;SP ;SAO PAULO ;CENTRO ;RUA TESTE ;' + preco + ';200.000,00;50;'
        'Sim;"Casa; descrição com ponto e vírgula";Venda Online;'
        'https://venda-imoveis.caixa.gov.br/sistema/detalhe-imovel.asp?hdnimovel=123456\n'
    )
    return texto.replace('\n', nova_linha).encode(encoding)


class ColetorCaixaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pasta = Path(self.temp.name)

    def salvar(self, dados, minutos=0):
        fim = T0 + timedelta(minutes=minutos)
        return c.arquivar(self.pasta, dados, {'url_final': c.CSV_URL}, fim, fim)

    def arquivos(self):
        return sorted((self.pasta / 'planilhas').rglob('*.csv'))

    def test_valida_encoding_modalidades_e_campos(self):
        for encoding in ('cp1252', 'utf-8-sig'):
            with self.subTest(encoding=encoding):
                resumo = c.validar_csv(planilha(encoding=encoding))
                self.assertEqual(resumo['quantidade_imoveis'], 1)
                self.assertEqual(resumo['data_geracao_caixa'], '30/10/2026')
                self.assertEqual(resumo['modalidades'], {'Venda Online': 1})
                self.assertEqual(resumo['estados'], {'SP': 1})

    def test_preserva_bytes_e_metadados(self):
        dados = planilha()
        evento = self.salvar(dados)
        destino = self.pasta / evento['arquivo']
        self.assertEqual(evento['status'], 'arquivado')
        self.assertEqual(destino.read_bytes(), dados)
        self.assertIn('2026-11-01_07-00-00-000000-0300', destino.name)
        self.assertEqual(json.loads(destino.with_suffix('.json').read_text(encoding='utf-8')), evento)

    def test_duplicata_exata_nao_cria_segunda_planilha(self):
        primeiro = self.salvar(planilha())
        segundo = self.salvar(planilha(), 5)
        self.assertEqual(segundo['status'], 'sem_alteracao')
        self.assertEqual(segundo['arquivo'], primeiro['arquivo'])
        self.assertEqual(len(self.arquivos()), 1)
        self.assertFalse(list(self.pasta.rglob('*.part')))

    def test_a_b_a_preserva_retorno_a_um_preco_anterior(self):
        self.salvar(planilha())
        self.salvar(planilha('90.000,00'), 5)
        terceiro = self.salvar(planilha(), 10)
        self.assertEqual(terceiro['status'], 'arquivado')
        self.assertEqual(len(self.arquivos()), 3)

    def test_mudanca_so_na_quebra_de_linha_tambem_e_arquivada(self):
        self.salvar(planilha())
        novo = self.salvar(planilha(nova_linha='\r\n'), 5)
        self.assertEqual(novo['status'], 'arquivado')
        self.assertEqual(len(self.arquivos()), 2)

    def test_data_geracao_diferente_tambem_e_arquivada(self):
        self.salvar(planilha())
        novo = self.salvar(planilha().replace(b'30/10/2026', b'31/10/2026'), 5)
        self.assertEqual(novo['status'], 'arquivado')
        self.assertEqual(novo['data_geracao_caixa'], '31/10/2026')

    def test_html_bloqueio_e_vazio_nao_entram_no_historico(self):
        primeiro = self.salvar(planilha())
        for dados in (b'<html>blocked</html>', b'<head><title>Radware Bot Manager Block</title>', b'', b'erro'):
            with self.subTest(dados=dados), self.assertRaises(ValueError):
                self.salvar(dados, 5)
        self.assertEqual(len(self.arquivos()), 1)
        self.assertEqual((self.pasta / primeiro['arquivo']).read_bytes(), planilha())

    def test_truncamento_linha_incompleta_preco_e_uf_invalidos(self):
        dados = planilha()
        variantes = (
            dados.rsplit(b';https://', 1)[0],
            dados.replace(b';SP ;', b';ZZ ;'),
            dados.replace(b'100.000,00', b'NaN'),
            dados.replace(b'100.000,00', b'-1,00'),
            dados.replace(b'100.000,00', b'invalido'),
        )
        for variante in variantes:
            with self.subTest(variante=variante[-80:]), self.assertRaises(ValueError):
                self.salvar(variante)
        self.assertFalse(self.arquivos())

    def test_lock_concorrente_e_liberacao_apos_excecao(self):
        with self.assertRaisesRegex(RuntimeError, 'simulada'):
            with c.trava(self.pasta):
                with self.assertRaises(c.ColetaEmAndamento):
                    with c.trava(self.pasta):
                        self.fail('Não poderia adquirir a mesma trava')
                raise RuntimeError('simulada')
        with c.trava(self.pasta):
            pass

    def test_falha_de_gravacao_nao_deixa_csv_parcial(self):
        with patch.object(c.os, 'replace', side_effect=OSError('disco indisponível')):
            with self.assertRaises(OSError):
                self.salvar(planilha())
        self.assertFalse(self.arquivos())
        self.assertFalse(list(self.pasta.rglob('*.part')))

    def test_todas_consultas_registradas_inclusive_duplicata_e_erro(self):
        with patch.object(c, 'baixar_lista', return_value=(planilha(), {})):
            a = c.executar(self.pasta)
            b = c.executar(self.pasta)
        with patch.object(c, 'baixar_lista', side_effect=URLError('sem rede')):
            with self.assertRaises(URLError):
                c.executar(self.pasta)
        registros = [json.loads(linha) for p in (self.pasta / 'execucoes').glob('*.jsonl')
                     for linha in p.read_text(encoding='utf-8').splitlines()]
        self.assertEqual([r['status'] for r in registros], ['arquivado', 'sem_alteracao', 'erro'])
        self.assertEqual(a['arquivo'], b['arquivo'])

    def test_bloqueia_redirecionamento_externo(self):
        handler = c.RedirecionamentoOficial()
        with self.assertRaises(c.PortalBloqueado):
            handler.redirect_request(None, None, 302, '', {}, 'https://validate.perfdrive.com/teste')

    def test_recupera_falha_temporaria_e_usa_geral(self):
        pagina = b'<option value="geral">Todos</option><script>".csv?12345"</script>'
        with patch.object(c, 'requisitar', side_effect=[
            URLError('sem rede'), (pagina, {}, c.PAGE_URL), (planilha(), {}, c.CSV_URL)
        ]) as req, patch.object(c.time, 'sleep'):
            dados, info = c.baixar_lista(tentativas=2)
        self.assertEqual(dados, planilha())
        self.assertEqual(req.call_args.args[1], c.CSV_URL + '?12345')
        self.assertEqual(info['tentativa'], 2)

    def test_bloqueio_nao_entra_em_repeticao_de_requisicoes(self):
        with patch.object(c, 'requisitar', return_value=(b'<html>bloqueado</html>', {}, c.PAGE_URL)) as req:
            with self.assertRaises(c.PortalBloqueado):
                c.baixar_lista()
        self.assertEqual(req.call_count, 1)

    def test_exportacao_nacional_direta_sem_consultar_html(self):
        with patch.object(c, "requisitar", return_value=(planilha(), {}, c.CSV_URL)) as req:
            dados, info = c.baixar_lista(consultar_pagina=False)
        self.assertEqual(req.call_count, 1)
        self.assertTrue(req.call_args.args[1].startswith(c.CSV_URL + "?"))
        self.assertEqual(dados, planilha())

    def test_content_length_incompleto_rejeitado(self):
        class Resposta(io.BytesIO):
            url = c.CSV_URL
            headers = {'Content-Length': '100'}
        opener = unittest.mock.Mock()
        opener.open.return_value = Resposta(b'abc')
        with self.assertRaisesRegex(OSError, 'incompleto'):
            c.requisitar(opener, c.CSV_URL, 1000, 60)


if __name__ == '__main__':
    unittest.main()
