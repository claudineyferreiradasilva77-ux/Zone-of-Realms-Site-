import json
import os
import re
import secrets
import sqlite3
import string
from contextlib import closing
from pathlib import Path

from pyramid.config import Configurator
from pyramid.response import Response
from waitress import serve

BASE_DIR = Path(__file__).resolve().parent

CHAVE_PIX = os.environ.get("CHAVE_PIX", "").strip()
NOME_RECEBEDOR = os.environ.get("NOME_RECEBEDOR", "ZORVANYX SERVER").strip()
CIDADE_RECEBEDOR = os.environ.get("CIDADE_RECEBEDOR", "CACU").strip()
DISCORD_LINK = os.environ.get("DISCORD_LINK", "").strip()
ADMIN_KEY = os.environ.get("ADMIN_KEY", "").strip()
API_KEY = os.environ.get("API_KEY", "").strip()
TEST_NICK = "pedrinhodograu67"
DB_PATH = Path(os.environ.get("DB_PATH", str(BASE_DIR / "vips.db")))

PLANOS = {
    "normal": {"nome": "VIP Normal", "valor": 8.00, "codigo": "NORMAL"},
    "premium": {"nome": "VIP Premium", "valor": 29.90, "codigo": "PREMIUM"},
}


def conectar_banco():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def iniciar_banco():
    with closing(conectar_banco()) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pedidos (
                id TEXT PRIMARY KEY,
                nick TEXT NOT NULL,
                plano TEXT NOT NULL,
                valor REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDENTE',
                criado TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                confirmado TEXT,
                codigo TEXT,
                chat_token TEXT,
                tipo_conta TEXT NOT NULL DEFAULT 'java_original'
            )
        """)

        colunas = {
            r["name"]
            for r in con.execute("PRAGMA table_info(pedidos)").fetchall()
        }

        if "codigo" not in colunas:
            con.execute("ALTER TABLE pedidos ADD COLUMN codigo TEXT")
        if "chat_token" not in colunas:
            con.execute("ALTER TABLE pedidos ADD COLUMN chat_token TEXT")
        if "tipo_conta" not in colunas:
            con.execute(
                "ALTER TABLE pedidos ADD COLUMN tipo_conta "
                "TEXT NOT NULL DEFAULT 'java_original'"
            )

        con.execute("""
            CREATE TABLE IF NOT EXISTS mensagens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pedido_id TEXT NOT NULL,
                remetente TEXT NOT NULL
                    CHECK(remetente IN ('COMPRADOR','ADMIN')),
                mensagem TEXT NOT NULL,
                criado TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(pedido_id) REFERENCES pedidos(id)
            )
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_mensagens_pedido
            ON mensagens(pedido_id, id)
        """)
        con.commit()


def resposta_json(data, status=200):
    return Response(
        json.dumps(data, ensure_ascii=False),
        status=status,
        content_type="application/json",
        charset="utf-8",
        headers={"Cache-Control": "no-store"}
    )


def resposta_texto(texto, status=200):
    return Response(
        texto,
        status=status,
        content_type="text/plain",
        charset="utf-8",
        headers={"Cache-Control": "no-store"}
    )


def ler_json(request):
    try:
        dados = request.json_body
        return dados if isinstance(dados, dict) else None
    except Exception:
        return None


def calcular_crc16(payload):
    crc = 0xFFFF
    for byte in payload.encode("utf-8"):
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return f"{crc:04X}"


def gerar_payload_pix(chave, nome, cidade, valor, txid):
    def campo(codigo, conteudo):
        tamanho = len(conteudo.encode("utf-8"))
        return f"{codigo}{tamanho:02d}{conteudo}"

    conta = (
        campo("00", "br.gov.bcb.pix")
        + campo("01", chave)
    )

    adicional = campo("62", campo("05", txid[:25]))

    sem_crc = (
        campo("00", "01")
        + campo("26", conta)
        + campo("52", "0000")
        + campo("53", "986")
        + campo("54", f"{valor:.2f}")
        + campo("58", "BR")
        + campo("59", nome[:25].upper())
        + campo("60", cidade[:15].upper())
        + adicional
        + "6304"
    )

    return sem_crc + calcular_crc16(sem_crc)


def gerar_codigo(con):
    alfabeto = string.ascii_uppercase + string.digits

    for _ in range(40):
        codigo = "".join(
            secrets.choice(alfabeto) for _ in range(8)
        )
        existe = con.execute(
            "SELECT 1 FROM pedidos WHERE codigo=?",
            (codigo,)
        ).fetchone()

        if not existe:
            return codigo

    raise RuntimeError("Não foi possível gerar um código único.")


def pagina_loja(request):
    plano = request.matchdict.get("plano", "normal").lower()

    if plano not in PLANOS:
        plano = "normal"

    arquivo = BASE_DIR / "index.html"
    html = arquivo.read_text(encoding="utf-8")
    html = html.replace("__PLANO_INICIAL__", plano)
    html = html.replace("__DISCORD_LINK__", DISCORD_LINK)

    return Response(
        html,
        content_type="text/html",
        charset="utf-8",
        headers={"Cache-Control": "no-store"}
    )


def criar_pedido(request):
    dados = ler_json(request)

    if dados is None:
        return resposta_json({"erro": "Envie um pedido JSON válido."}, 400)

    nick = str(dados.get("nick", "")).strip()
    plano = str(dados.get("plano", "")).lower().strip()
    tipo_conta = str(
        dados.get("tipo_conta", "java_original")
    ).lower().strip()

    if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", nick):
        return resposta_json({
            "erro": "Nick inválido. Use 3 a 16 letras, números ou _."
        }, 400)

    if plano not in PLANOS:
        return resposta_json({"erro": "Plano inválido."}, 400)

    if tipo_conta not in ("java_original", "tlauncher"):
        return resposta_json({
            "erro": "Escolha Minecraft Original ou TLauncher."
        }, 400)

    if not CHAVE_PIX:
        return resposta_json({
            "erro": "O administrador ainda não configurou a chave PIX."
        }, 503)

    info = PLANOS[plano]

    try:
        with closing(conectar_banco()) as con:
            pedido_id = secrets.token_hex(6).upper()
            codigo = gerar_codigo(con)
            chat_token = secrets.token_urlsafe(32)

            pix = gerar_payload_pix(
                CHAVE_PIX,
                NOME_RECEBEDOR,
                CIDADE_RECEBEDOR,
                info["valor"],
                pedido_id
            )

            con.execute("""
                INSERT INTO pedidos
                (id,nick,plano,valor,codigo,chat_token,tipo_conta)
                VALUES (?,?,?,?,?,?,?)
            """, (
                pedido_id,
                nick,
                info["codigo"],
                info["valor"],
                codigo,
                chat_token,
                tipo_conta
            ))
            con.commit()

        return resposta_json({
            "id": pedido_id,
            "codigo": codigo,
            "chat_token": chat_token,
            "nick": nick,
            "tipo_conta": tipo_conta,
            "plano": info["nome"],
            "valor": info["valor"],
            "pix": pix,
            "discord": DISCORD_LINK,
            "aviso": (
                "O VIP só será liberado após a conferência manual "
                "do PIX. Guarde seu código."
            )
        })

    except Exception:
        return resposta_json({
            "erro": "Erro interno ao criar o pedido. Tente novamente."
        }, 500)


def autorizado(request):
    chave_recebida = request.headers.get("X-Admin-Key", "")
    return bool(ADMIN_KEY) and secrets.compare_digest(
        chave_recebida, ADMIN_KEY
    )


def admin_page(request):
    return Response(
        ADMIN_HTML,
        content_type="text/html",
        charset="utf-8",
        headers={"Cache-Control": "no-store"}
    )


def admin_listar(request):
    if not autorizado(request):
        return resposta_json({"erro": "Não autorizado."}, 401)

    with closing(conectar_banco()) as con:
        rows = con.execute("""
            SELECT id,nick,plano,valor,status,criado,
                   confirmado,codigo,tipo_conta
            FROM pedidos
            ORDER BY criado DESC
            LIMIT 200
        """).fetchall()

    return resposta_json([dict(row) for row in rows])


def admin_confirmar(request):
    if not autorizado(request):
        return resposta_json({"erro": "Não autorizado."}, 401)

    dados = ler_json(request)
    if dados is None:
        return resposta_json({"erro": "Pedido inválido."}, 400)

    pedido_id = str(dados.get("id", "")).strip().upper()

    with closing(conectar_banco()) as con:
        cur = con.execute("""
            UPDATE pedidos
            SET status='PAGO', confirmado=CURRENT_TIMESTAMP
            WHERE id=? AND status='PENDENTE'
        """, (pedido_id,))
        con.commit()

        if cur.rowcount != 1:
            return resposta_json({
                "erro": "Pedido não encontrado ou já confirmado."
            }, 404)

    return resposta_json({
        "ok": True,
        "mensagem": (
            "Pagamento confirmado. Agora confira e aprove o código "
            "no servidor."
        )
    })


def admin_gerar_teste(request):
    if not autorizado(request):
        return resposta_json({"erro": "Não autorizado."}, 401)

    dados = ler_json(request)
    if dados is None:
        return resposta_json({"erro": "Pedido inválido."}, 400)

    nick = str(dados.get("nick", "")).strip()

    if nick.casefold() != TEST_NICK.casefold():
        return resposta_json({
            "erro": "O teste grátis é exclusivo do nick autorizado."
        }, 403)

    with closing(conectar_banco()) as con:
        antigo = con.execute("""
            SELECT id,codigo
            FROM pedidos
            WHERE lower(nick)=lower(?) AND status='TESTE_GRATIS'
        """, (TEST_NICK,)).fetchone()

        if antigo:
            return resposta_json({
                "erro": "O teste grátis já foi emitido.",
                "codigo": antigo["codigo"]
            }, 409)

        pedido_id = secrets.token_hex(6).upper()
        codigo = gerar_codigo(con)

        con.execute("""
            INSERT INTO pedidos
            (id,nick,plano,valor,status,codigo,tipo_conta)
            VALUES (?,?,?,0,'TESTE_GRATIS',?,'tlauncher')
        """, (
            pedido_id,
            TEST_NICK,
            "NORMAL",
            codigo
        ))
        con.commit()

    return resposta_json({
        "ok": True,
        "nick": TEST_NICK,
        "codigo": codigo,
        "plano": "VIP NORMAL - TESTE GRÁTIS",
        "duracao_dias": 30,
        "aviso": "Aprove o código manualmente no servidor."
    })


def pedido_por_id(con, pedido_id):
    return con.execute(
        "SELECT * FROM pedidos WHERE id=?",
        (pedido_id,)
    ).fetchone()


def mensagens_json(con, pedido_id):
    rows = con.execute("""
        SELECT id,remetente,mensagem,criado
        FROM mensagens
        WHERE pedido_id=?
        ORDER BY id ASC
        LIMIT 200
    """, (pedido_id,)).fetchall()

    return [dict(row) for row in rows]


def comprador_chat(request):
    pedido_id = request.matchdict["pedido_id"].upper()
    token = request.headers.get("X-Chat-Token", "")

    with closing(conectar_banco()) as con:
        pedido = pedido_por_id(con, pedido_id)

        if (
            not pedido
            or not pedido["chat_token"]
            or not secrets.compare_digest(token, pedido["chat_token"])
        ):
            return resposta_json({
                "erro": "Pedido ou acesso ao chat inválido."
            }, 403)

        if request.method == "GET":
            return resposta_json(mensagens_json(con, pedido_id))

        dados = ler_json(request)
        if dados is None:
            return resposta_json({"erro": "Mensagem inválida."}, 400)

        mensagem = str(dados.get("mensagem", "")).strip()

        if not mensagem or len(mensagem) > 1000:
            return resposta_json({
                "erro": "A mensagem deve ter entre 1 e 1000 caracteres."
            }, 400)

        con.execute("""
            INSERT INTO mensagens (pedido_id,remetente,mensagem)
            VALUES (?,?,?)
        """, (pedido_id, "COMPRADOR", mensagem))
        con.commit()

    return resposta_json({"ok": True})


def admin_chat_lista(request):
    if not autorizado(request):
        return resposta_json({"erro": "Não autorizado."}, 401)

    with closing(conectar_banco()) as con:
        rows = con.execute("""
            SELECT p.id,p.nick,p.plano,p.status,p.codigo,
                (SELECT mensagem FROM mensagens m
                 WHERE m.pedido_id=p.id ORDER BY m.id DESC LIMIT 1) AS ultima,
                (SELECT criado FROM mensagens m
                 WHERE m.pedido_id=p.id ORDER BY m.id DESC LIMIT 1) AS atualizado
            FROM pedidos p
            ORDER BY COALESCE(atualizado,p.criado) DESC
            LIMIT 100
        """).fetchall()

    return resposta_json([dict(row) for row in rows])


def admin_chat(request):
    if not autorizado(request):
        return resposta_json({"erro": "Não autorizado."}, 401)

    pedido_id = request.matchdict["pedido_id"].upper()

    with closing(conectar_banco()) as con:
        pedido = pedido_por_id(con, pedido_id)

        if not pedido:
            return resposta_json({"erro": "Pedido não encontrado."}, 404)

        if request.method == "GET":
            return resposta_json({
                "pedido": {
                    "id": pedido["id"],
                    "nick": pedido["nick"],
                    "plano": pedido["plano"],
                    "status": pedido["status"],
                    "codigo": pedido["codigo"]
                },
                "mensagens": mensagens_json(con, pedido_id)
            })

        dados = ler_json(request)
        if dados is None:
            return resposta_json({"erro": "Mensagem inválida."}, 400)

        mensagem = str(dados.get("mensagem", "")).strip()

        if not mensagem or len(mensagem) > 1000:
            return resposta_json({
                "erro": "A mensagem deve ter entre 1 e 1000 caracteres."
            }, 400)

        con.execute("""
            INSERT INTO mensagens (pedido_id,remetente,mensagem)
            VALUES (?,?,?)
        """, (pedido_id, "ADMIN", mensagem))
        con.commit()

    return resposta_json({"ok": True})


def entregas(request):
    if not API_KEY or not secrets.compare_digest(
        request.params.get("key", ""), API_KEY
    ):
        return resposta_texto("FORBIDDEN", 403)

    with closing(conectar_banco()) as con:
        rows = con.execute("""
            SELECT nick,plano
            FROM pedidos
            WHERE status='PAGO'
            ORDER BY confirmado ASC
            LIMIT 20
        """).fetchall()

    if not rows:
        return resposta_texto("VAZIO")

    return resposta_texto(";".join(
        f'{r["nick"]}:{r["plano"].lower()}'
        for r in rows
    ))


ADMIN_HTML = r'''<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ZORVANYX • Painel VIP</title>
<style>
*{box-sizing:border-box;font-family:Segoe UI,Arial}
body{margin:0;background:#030405;color:#f2f5f7;padding:22px}
main{max-width:1100px;margin:auto}
h1{color:#70f0e1}
.sub,.muted{color:#9ba6ad}
.bar,.pedido,.trial{background:#0d1115;border:1px solid #293238;border-radius:16px;padding:16px;margin:14px 0}
input,button{padding:12px;border-radius:10px;border:1px solid #39464d;background:#080b0d;color:white}
input{min-width:230px}
button{cursor:pointer;background:#174f4b;border:1px solid #70f0e1;font-weight:800}
.pedido{display:flex;gap:12px;justify-content:space-between;align-items:center;flex-wrap:wrap}
.status{color:#f3cf83}
.trial{border-color:#8266bd}
.trial h2{color:#c4b5fd}
.code{font:800 1.4rem monospace;color:#76e6a7;letter-spacing:3px}
.hidden{display:none}
</style>
</head>
<body>
<main>
<h1>✦ ZORVANYX • Painel VIP</h1>
<p class="sub">Pedidos, códigos de resgate e teste exclusivo.</p>
<div class="bar">
<input id="key" type="password" placeholder="ADMIN_KEY do Render" autocomplete="current-password">
<button onclick="entrar()">Entrar</button>
<span id="aviso" class="muted"></span>
</div>
<div id="conteudo" class="hidden">
<section class="trial">
<h2>★ Teste grátis exclusivo • 30 dias</h2>
<p>Permitido somente para <b>pedrinhodograu67</b>.</p>
<input id="testeNick" value="pedrinhodograu67" maxlength="16">
<button onclick="gerarTeste()">Emitir código de teste</button>
<p id="testeResultado"></p>
</section>
<h2>Pedidos da loja</h2>
<button onclick="carregarPedidos()">Atualizar pedidos</button>
<section id="pedidos"></section>
</div>
</main>
<script>
let chave='';
const $=id=>document.getElementById(id);

async function api(url,opt={}){
  opt.headers={
    ...(opt.headers||{}),
    'X-Admin-Key':chave
  };
  const r=await fetch(url,opt);
  let d={};
  try{d=await r.json()}catch(e){}
  if(!r.ok)throw Error(d.erro||'Falha na solicitação');
  return d;
}

async function entrar(){
  chave=$('key').value.trim();
  try{
    await api('/admin/pedidos');
    $('conteudo').classList.remove('hidden');
    $('aviso').textContent='Acesso autorizado';
    carregarPedidos();
  }catch(e){
    $('aviso').textContent=e.message;
  }
}

async function gerarTeste(){
  const nick=$('testeNick').value.trim();
  $('testeResultado').textContent='Gerando...';
  try{
    const d=await api('/admin/teste',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({nick})
    });
    $('testeResultado').textContent='Código emitido: '+d.codigo+' • 30 dias';
    carregarPedidos();
  }catch(e){
    $('testeResultado').textContent=e.message;
  }
}

async function carregarPedidos(){
  try{
    const d=await api('/admin/pedidos');
    $('pedidos').replaceChildren();

    if(!d.length){
      $('pedidos').textContent='Nenhum pedido ainda.';
      return;
    }

    d.forEach(p=>{
      const el=document.createElement('div');
      el.className='pedido';

      const info=document.createElement('div');
      const nome=document.createElement('b');
      nome.textContent=p.nick+' • '+p.plano;

      const meta=document.createElement('div');
      meta.className='muted';
      meta.textContent=
        'Código: '+(p.codigo||'—')+
        ' | ID: '+p.id+
        ' | Tipo: '+(p.tipo_conta||'java_original')+
        ' | R$ '+Number(p.valor).toFixed(2).replace('.',',')+
        ' | '+p.criado;

      const status=document.createElement('div');
      status.className='status';
      status.textContent='Status: '+p.status;

      info.append(nome,meta,status);
      el.append(info);

      if(p.status==='PENDENTE'){
        const bt=document.createElement('button');
        bt.textContent='Confirmar PIX recebido';
        bt.onclick=()=>confirmar(p.id);
        el.append(bt);
      }

      $('pedidos').append(el);
    });
  }catch(e){
    $('aviso').textContent=e.message;
  }
}

async function confirmar(id){
  if(!confirm('Você conferiu no banco que o PIX entrou?'))return;

  try{
    const d=await api('/admin/confirmar',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id})
    });
    $('aviso').textContent=d.mensagem;
    carregarPedidos();
  }catch(e){
    $('aviso').textContent=e.message;
  }
}
</script>
</body>
</html>'''


def main():
    iniciar_banco()

    with Configurator() as config:
        config.add_route("home", "/")
        config.add_route("plano", "/vip-{plano}")
        config.add_route("criar_pedido", "/api/pedidos")
        config.add_route("entregas", "/api/entregas")
        config.add_route("admin", "/admin")
        config.add_route("admin_listar", "/admin/pedidos")
        config.add_route("admin_confirmar", "/admin/confirmar")
        config.add_route("admin_teste", "/admin/teste")
        config.add_route("admin_chats", "/admin/chats")
        config.add_route("admin_chat", "/admin/chat/{pedido_id}")
        config.add_route("comprador_chat", "/api/chat/{pedido_id}")

        config.add_view(
            pagina_loja, route_name="home", request_method="GET"
        )
        config.add_view(
            pagina_loja, route_name="plano", request_method="GET"
        )
        config.add_view(
            criar_pedido, route_name="criar_pedido", request_method="POST"
        )
        config.add_view(
            entregas, route_name="entregas", request_method="GET"
        )
        config.add_view(
            admin_page, route_name="admin", request_method="GET"
        )
        config.add_view(
            admin_listar, route_name="admin_listar", request_method="GET"
        )
        config.add_view(
            admin_confirmar, route_name="admin_confirmar", request_method="POST"
        )
        config.add_view(
            admin_gerar_teste, route_name="admin_teste", request_method="POST"
        )
        config.add_view(
            admin_chat_lista, route_name="admin_chats", request_method="GET"
        )
        config.add_view(
            admin_chat, route_name="admin_chat",
            request_method=["GET", "POST"]
        )
        config.add_view(
            comprador_chat, route_name="comprador_chat",
            request_method=["GET", "POST"]
        )

        app = config.make_wsgi_app()

    port = int(os.environ.get("PORT", "8080"))
    print(f"ZORVANYX VIP online na porta {port}")
    serve(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
    
