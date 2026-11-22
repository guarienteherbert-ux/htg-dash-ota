#!/usr/bin/env python3
"""Gera a base de radares do Brasil para o aviso do HTG Dash, a partir do OpenStreetMap (ODbL, © colaboradores do
OpenStreetMap). Baixa estado por estado pelo Overpass (o Brasil inteiro estoura o tempo do servidor público).

Saída: CSV comprimido (gzip), uma linha por radar:  lat,lon,limite,direcao,tipo,eixo
  limite  = km/h (0 = não informado)
  direcao = rumo em graus (0 = norte) do trânsito que o radar fiscaliza; -1 = os dois sentidos/desconhecido
  tipo    = 0 radar de velocidade (inclui lombada eletrônica), 1 câmera de semáforo (avanço de sinal vermelho)
  eixo    = rumo da rua no ponto do radar (graus; -1 sem a rua): o app só avisa se a rua do carro estiver alinhada
A direção vem da tag direction do radar (graus, N/NE..., forward/backward em relação à rua) ou, sem ela, da rua de mão
única onde o radar está (oneway=yes / -1). O limite vem do radar (maxspeed) ou da rua (maxspeed, :forward/:backward).

uso: gera_radares.py <saida.csv.gz> [UF ...]
Cada estado fica em por_estado/<UF>.json ao lado da saída: rodar de novo continua de onde parou.
"""
import gzip, json, math, os, sys, time, urllib.parse, urllib.request

UFS = 'AC AL AM AP BA CE DF ES GO MA MG MS MT PA PB PE PI PR RJ RN RO RR RS SC SE SP TO'.split()
SERVERS = ['https://overpass-api.de/api/interpreter', 'https://overpass.kumi.systems/api/interpreter']
CARDINAL = {'N': 0, 'NNE': 22.5, 'NE': 45, 'ENE': 67.5, 'E': 90, 'ESE': 112.5, 'SE': 135, 'SSE': 157.5, 'S': 180,
            'SSW': 202.5, 'SW': 225, 'WSW': 247.5, 'W': 270, 'WNW': 292.5, 'NW': 315, 'NNW': 337.5,
            'L': 90, 'O': 270, 'NO': 315, 'SO': 225}


def overpass(q):
    data = urllib.parse.urlencode({'data': q}).encode()
    last = None
    for attempt in range(4):
        url = SERVERS[attempt % len(SERVERS)]
        try:
            req = urllib.request.Request(url, data, headers={'User-Agent': 'htg-radares/1.0'})
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)
        except Exception as e:  # servidor ocupado: espera e tenta o outro
            last = e
            time.sleep(20 * (attempt + 1))
    raise RuntimeError(f'overpass falhou: {last}')


def bearing(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def speed(v):
    if not v:
        return 0
    v = v.strip().split(';')[0].split(' ')[0]
    try:
        n = int(float(v))
        return n if 10 <= n <= 150 else 0
    except ValueError:
        return 0


def uf(sigla):
    # radares (nós highway=speed_camera) e as ruas onde estão (para o sentido e o limite)
    q = (f'[out:json][timeout:240];area["ISO3166-2"="BR-{sigla}"]->.a;'
         f'node["highway"="speed_camera"](area.a);out body;way(bn)["highway"];out geom;')
    return processa(overpass(q))


def semaforos_br():
    # câmeras de semáforo: relações type=enforcement (o equipamento no papel "device"). São poucas: uma consulta só
    # para o Brasil inteiro (por estado, somada aos radares, estourava o tempo do GitHub).
    q = ('[out:json][timeout:600];area["ISO3166-1"="BR"][admin_level=2]->.a;'
         'rel["type"="enforcement"]["enforcement"~"traffic_signals|red_light"](area.a)->.r;'
         'node(r.r:"device")->.d;.d out body;.r out body;way(bn.d)["highway"];out geom;')
    return processa(overpass(q), so_semaforo=True)


def processa(d, so_semaforo=False):
    nodes = {e['id']: e for e in d['elements'] if e['type'] == 'node'}
    semaforo = set()
    for r in (e for e in d['elements'] if e['type'] == 'relation'):
        for m in r.get('members', []):
            if m.get('type') == 'node' and m.get('role') == 'device':
                semaforo.add(m['ref'])
    ways = [e for e in d['elements'] if e['type'] == 'way']
    on_way = {}  # radar -> (way, índice na geometria)
    for w in ways:
        for i, nid in enumerate(w.get('nodes', [])):
            if nid in nodes:
                on_way.setdefault(nid, (w, i))
    out = []
    for nid, n in nodes.items():
        t = n.get('tags', {})
        lim = speed(t.get('maxspeed'))
        direc = -1.0
        w = on_way.get(nid)
        seg = None
        if w:
            way, i = w
            g = way.get('geometry') or []
            if len(g) >= 2:
                a = g[max(0, i - 1)] if i > 0 else g[0]
                b = g[min(len(g) - 1, i + 1)] if i < len(g) - 1 else g[-1]
                if a is not b:
                    seg = bearing((a['lat'], a['lon']), (b['lat'], b['lon']))
        dt = (t.get('direction') or '').strip()
        if dt:
            u = dt.upper()
            if u in CARDINAL:
                direc = CARDINAL[u]
            elif u in ('FORWARD', 'BACKWARD') and seg is not None:
                direc = seg if u == 'FORWARD' else (seg + 180) % 360
            else:
                try:
                    direc = float(dt.split(';')[0]) % 360
                except ValueError:
                    pass
        if w:
            wt = w[0].get('tags', {})
            ow = wt.get('oneway', 'no')
            if direc < 0 and seg is not None and ow in ('yes', '1', '-1'):
                direc = seg if ow != '-1' else (seg + 180) % 360
            if not lim:
                if direc >= 0 and seg is not None:
                    fwd = abs(((direc - seg + 180) % 360) - 180) < 90
                    lim = speed(wt.get('maxspeed:forward' if fwd else 'maxspeed:backward'))
                lim = lim or speed(wt.get('maxspeed'))
        tipo = 1 if (so_semaforo or nid in semaforo or t.get('enforcement') in ('red_light_camera', 'traffic_signals')
                     or t.get('speed_camera') in ('traffic_signals', 'red_light_camera')) else 0
        eixo = -1 if seg is None else int(round(seg)) % 360  # rumo da rua no ponto do radar (o app confere a rua)
        out.append((round(n['lat'], 6), round(n['lon'], 6), lim, -1 if direc < 0 else int(round(direc)) % 360, tipo, eixo))
    return out


def main():
    dest = sys.argv[1]
    ufs = [u.upper() for u in sys.argv[2:]] or UFS
    total = []
    faltam = []
    cache = os.path.join(os.path.dirname(os.path.abspath(dest)), 'por_estado')
    os.makedirs(cache, exist_ok=True)
    prazo = float(os.environ.get('RADARES_PRAZO_S', '0') or 0)  # GitHub: parar antes do limite e continuar depois
    inicio = float(os.environ.get('RADARES_INICIO', time.time()))
    os.environ['RADARES_INICIO'] = str(inicio)
    for s in ufs + ['SEMAFOROS']:
        if prazo and time.time() - inicio > prazo:
            print(f'prazo esgotado antes de {s}: o que já veio está guardado, continua na próxima execução', flush=True)
            sys.exit(3)
        cf = os.path.join(cache, 'v4_' + s + '.json')  # v4: + eixo da rua; semáforos numa consulta só
        if os.path.isfile(cf):  # já baixado (o script continua de onde parou)
            r = [tuple(x) for x in json.load(open(cf))]
            print(f'{s}: {len(r)} radares (já baixado)', flush=True)
            total += r
            continue
        try:
            r = semaforos_br() if s == 'SEMAFOROS' else uf(s)
        except Exception as e:  # servidor sobrecarregado: segue, e tenta este de novo no fim
            print(f'{s}: falhou ({e}), fica para depois', flush=True)
            faltam.append(s)
            continue
        json.dump(r, open(cf + '.tmp', 'w'))
        os.replace(cf + '.tmp', cf)
        print(f'{s}: {len(r)} radares ({sum(1 for x in r if x[2])} com limite, {sum(1 for x in r if x[3] >= 0)} com direção)',
              flush=True)
        total += r
        time.sleep(5)
    if faltam:
        if os.environ.get('RADARES_VOLTA', '0') < '2' and not (prazo and time.time() - inicio + 600 > prazo):
            print('faltaram %s: nova volta em 10 min' % ' '.join(faltam), flush=True)
            time.sleep(600)
            os.environ['RADARES_VOLTA'] = str(int(os.environ.get('RADARES_VOLTA', '0')) + 1)
            os.execv(sys.executable, [sys.executable, '-I'] + sys.argv)
        sys.exit('faltaram %s (rode de novo mais tarde; os outros estados já estão guardados)' % ' '.join(faltam))
    # o mesmo ponto pode vir como radar e como câmera de semáforo: fica uma linha, com o tipo semáforo
    pontos = {}
    for la, lo, lim, d, tipo, eixo in (tuple(x) for x in total):
        k = (la, lo)
        old = pontos.get(k)
        if old is None or tipo > old[4] or (tipo == old[4] and lim > old[2]):
            pontos[k] = (la, lo, max(lim, old[2] if old else 0), d if d >= 0 or old is None else old[3], tipo,
                         eixo if eixo >= 0 or old is None else old[5])
    total = sorted(pontos.values())
    with gzip.open(dest, 'wt', encoding='ascii') as f:
        f.write('# radares OpenStreetMap (ODbL, (c) colaboradores do OpenStreetMap) ' + time.strftime('%Y-%m-%d') + '\n')
        for la, lo, lim, d, tipo, eixo in total:
            f.write(f'{la},{lo},{lim},{d},{tipo},{eixo}\n')
    print(f'total: {len(total)} radares -> {dest}')


if __name__ == '__main__':
    main()
