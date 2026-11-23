#!/usr/bin/env python3
"""Gera a base de radares do Brasil para o aviso do HTG Dash, a partir do OpenStreetMap (ODbL, © colaboradores do
OpenStreetMap). Baixa estado por estado pelo Overpass (o Brasil inteiro estoura o tempo do servidor público).

Saída: CSV comprimido (gzip), uma linha por radar:  lat,lon,limite,direcao,tipo,eixo
  limite  = km/h (0 = não informado)
  direcao = rumo em graus (0 = norte) do trânsito que o radar fiscaliza; -1 = os dois sentidos/desconhecido
  tipo    = 0 radar de velocidade (inclui lombada eletrônica), 1 câmera de semáforo (avanço de sinal vermelho)
  eixo    = rumo da rua no ponto do radar (graus; -1 sem a rua): o app só avisa se a rua do carro estiver alinhada
Também entram os radares das rodovias federais concedidas da ANTT (dados abertos, CC-BY), com a direção pela pista
do OpenStreetMap; os que o OpenStreetMap já tem ficam só uma vez.
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


ANTT_URLS = [  # rodovias federais concedidas (ANTT, dados abertos, CC-BY): o site às vezes fica fora do ar
    'https://dados.antt.gov.br/dataset/79d287f4-f5ca-4385-a17c-f61f53831f17/resource/'
    '848fafad-6188-493c-8a36-1936635f29e7/download/radar.kmz',
]
ANTT_COPIA = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'radares', 'antt_radar.kmz')


def antt_kmz():
    for u in ANTT_URLS:
        try:
            req = urllib.request.Request(u, headers={'User-Agent': 'htg-radares/1.0'})
            with urllib.request.urlopen(req, timeout=60) as r:
                b = r.read()
            if b[:2] == b'PK':
                try:  # guarda a mais nova como cópia (o workflow publica a pasta radares/ junto)
                    os.makedirs(os.path.dirname(ANTT_COPIA), exist_ok=True)
                    open(ANTT_COPIA, 'wb').write(b)
                except OSError:
                    pass
                return b, 'site da ANTT'
        except Exception as e:
            print(f'ANTT fora do ar ({e}): usando a cópia guardada', flush=True)
    if os.path.isfile(ANTT_COPIA):
        return open(ANTT_COPIA, 'rb').read(), 'cópia guardada'
    return None, ''


def do_pbf(path, antt_pts):
    """Lê o OpenStreetMap do Brasil de um arquivo .osm.pbf (Geofabrik) em vez do Overpass: radares, relações de
    semáforo, as ruas onde eles estão e as rodovias perto dos radares da ANTT. Devolve no formato do Overpass."""
    import osmium
    from osmium.filter import KeyFilter, EntityFilter
    from osmium.osm import WAY
    t0 = time.time()
    cams, rels, device = {}, [], set()
    # 1ª passada (sem coordenadas, rápida): radares e relações de fiscalização de semáforo
    for o in osmium.FileProcessor(path).with_filter(KeyFilter('highway', 'type', 'enforcement')):
        if o.is_node():
            t = dict(o.tags)
            if t.get('highway') == 'speed_camera' or t.get('enforcement') in ('red_light_camera', 'traffic_signals', 'maxspeed'):
                cams[o.id] = {'type': 'node', 'id': o.id, 'lat': o.location.lat, 'lon': o.location.lon, 'tags': t}
        elif o.is_relation():
            t = dict(o.tags)
            if t.get('type') == 'enforcement' and ('traffic_signals' in t.get('enforcement', '') or 'red_light' in t.get('enforcement', '')):
                mem = [{'type': {'n': 'node', 'w': 'way', 'r': 'relation'}[m.type], 'ref': m.ref, 'role': m.role} for m in o.members]
                rels.append({'type': 'relation', 'id': o.id, 'tags': t, 'members': mem})
                device |= {m['ref'] for m in mem if m['type'] == 'node' and m['role'] == 'device'}
    want = set(cams) | device
    print(f'pbf: {len(cams)} radares, {len(rels)} relações de semáforo ({time.time() - t0:.0f} s)', flush=True)
    # 2ª passada (com coordenadas): ruas com radar e rodovias a até ~40 m de um radar da ANTT
    grade = {}
    for i, (la, lo, _) in enumerate(antt_pts):
        grade.setdefault((int(la * 200), int(lo * 200)), []).append(i)
    principais = ('motorway', 'trunk', 'primary', 'secondary', 'tertiary')
    ways, antt_ways, dev_loc = [], [], {}
    fp = (osmium.FileProcessor(path).with_locations('sparse_mem_array')
          .with_filter(EntityFilter(WAY)).with_filter(KeyFilter('highway')))
    for w in fp:
        hw = w.tags.get('highway', '')
        refs = [n.ref for n in w.nodes]
        tem = any(r in want for r in refs)
        perto = False
        if grade and hw.startswith(principais):
            for n in w.nodes:
                if n.location.valid() and (int(n.location.lat * 200), int(n.location.lon * 200)) in grade:
                    perto = True
                    break
        if not (tem or perto):
            continue
        geom = [{'lat': n.location.lat, 'lon': n.location.lon} for n in w.nodes if n.location.valid()]
        if len(geom) != len(refs):
            continue
        e = {'type': 'way', 'id': w.id, 'nodes': refs, 'geometry': geom, 'tags': dict(w.tags)}
        if tem:
            ways.append(e)
            for r, g in zip(refs, geom):
                if r in device:
                    dev_loc[r] = g
        if perto:
            antt_ways.append(e)
    # câmeras de semáforo sem etiqueta: a posição vem da rua
    for r, g in dev_loc.items():
        if r not in cams:
            cams[r] = {'type': 'node', 'id': r, 'lat': g['lat'], 'lon': g['lon'], 'tags': {}}
    print(f'pbf: {len(ways)} ruas com radar, {len(antt_ways)} rodovias perto da ANTT ({time.time() - t0:.0f} s)', flush=True)
    return {'elements': list(cams.values()) + rels + ways}, antt_ways


def antt_pontos():
    import html, io, re, zipfile
    b, origem = antt_kmz()
    if not b:
        return []
    k = zipfile.ZipFile(io.BytesIO(b)).read('doc.kml').decode('utf-8', 'ignore')
    pts = []
    for p in re.findall(r'<Placemark.*?</Placemark>', k, re.S):
        cells = [html.unescape(re.sub(r'<[^>]+>', '', c)).strip() for c in re.findall(r'<td[^>]*>(.*?)</td>', p, re.S)]
        f = dict(zip(cells[1::2], cells[2::2]))
        c = re.search(r'<coordinates>\s*([-\d.]+),([-\d.]+)', p)
        if not c or f.get('situacao', 'Ativo').lower() != 'ativo':
            continue
        pts.append((float(c.group(2)), float(c.group(1)), speed(f.get('velocidade'))))
    print(f'ANTT ({origem}): {len(pts)} radares ativos', flush=True)
    return pts


def antt_com_pistas(pts, ways):
    out = []
    for la, lo, lim in pts:
        best, bd = None, 1e9
        for w in ways:
            g = w['geometry']
            if abs(g[0]['lat'] - la) > 0.5 or abs(g[0]['lon'] - lo) > 0.5:
                continue
            for a, b2 in zip(g, g[1:]):
                d = seg_dist(la, lo, a, b2)
                if d < bd:
                    bd, best = d, (w, a, b2)
        direc = eixo = -1
        if best and bd < 35:
            w, a, b2 = best
            seg = bearing((a['lat'], a['lon']), (b2['lat'], b2['lon']))
            eixo = int(round(seg)) % 360
            ow = w.get('tags', {}).get('oneway', 'no')
            if ow in ('yes', '1', '-1') or w.get('tags', {}).get('highway', '').startswith('motorway'):
                direc = eixo if ow != '-1' else (eixo + 180) % 360
        out.append((round(la, 6), round(lo, 6), lim, direc, 0, eixo))
    return out


def antt():
    # radares das concessionárias: ponto, limite de carro leve e situação. O sentido vem como crescente/decrescente
    # (pelo km): a direção de bússola sai da pista do OpenStreetMap onde o radar está (rodovia duplicada = mão única).
    import html, io, re, zipfile
    b, origem = antt_kmz()
    if not b:
        return []
    k = zipfile.ZipFile(io.BytesIO(b)).read('doc.kml').decode('utf-8', 'ignore')
    pts = []
    for p in re.findall(r'<Placemark.*?</Placemark>', k, re.S):
        cells = [html.unescape(re.sub(r'<[^>]+>', '', c)).strip() for c in re.findall(r'<td[^>]*>(.*?)</td>', p, re.S)]
        f = dict(zip(cells[1::2], cells[2::2]))
        c = re.search(r'<coordinates>\s*([-\d.]+),([-\d.]+)', p)
        if not c or f.get('situacao', 'Ativo').lower() != 'ativo':
            continue
        pts.append((float(c.group(2)), float(c.group(1)), speed(f.get('velocidade'))))
    print(f'ANTT ({origem}): {len(pts)} radares ativos', flush=True)
    out = []
    for i in range(0, len(pts), 80):  # pista de cada um, em lotes
        lote = pts[i:i + 80]
        q = '[out:json][timeout:240];(' + ''.join(
            f'way(around:30,{la},{lo})["highway"~"motorway|trunk|primary|secondary|tertiary"];' for la, lo, _ in lote) + ');out geom;'
        try:
            ways = [e for e in overpass(q)['elements'] if e['type'] == 'way']
        except Exception as e:
            print(f'ANTT: pistas do lote {i // 80} indisponíveis ({e})', flush=True)
            ways = []
        for la, lo, lim in lote:
            best, bd = None, 1e9
            for w in ways:
                g = w.get('geometry') or []
                for a, b2 in zip(g, g[1:]):
                    d = seg_dist(la, lo, a, b2)
                    if d < bd:
                        bd, best = d, (w, a, b2)
            direc = eixo = -1
            if best and bd < 35:
                w, a, b2 = best
                seg = bearing((a['lat'], a['lon']), (b2['lat'], b2['lon']))
                eixo = int(round(seg)) % 360
                ow = w.get('tags', {}).get('oneway', 'no')
                if ow in ('yes', '1', '-1') or w.get('tags', {}).get('highway', '').startswith('motorway'):
                    direc = eixo if ow != '-1' else (eixo + 180) % 360
            out.append((round(la, 6), round(lo, 6), lim, direc, 0, eixo))
        time.sleep(3)
    return out


def seg_dist(la, lo, a, b):
    # distância (m) do ponto ao segmento a-b, em plano local
    kx = 111320 * math.cos(math.radians(la))
    ax, ay = (a['lon'] - lo) * kx, (a['lat'] - la) * 110540
    bx, by = (b['lon'] - lo) * kx, (b['lat'] - la) * 110540
    dx, dy = bx - ax, by - ay
    t = 0 if dx == dy == 0 else max(0, min(1, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
    return math.hypot(ax + t * dx, ay + t * dy)


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
    if len(sys.argv) >= 4 and sys.argv[1] == '--pbf':
        return main_pbf(sys.argv[2], sys.argv[3])
    dest = sys.argv[1]
    ufs = [u.upper() for u in sys.argv[2:]] or UFS
    total = []
    faltam = []
    cache = os.path.join(os.path.dirname(os.path.abspath(dest)), 'por_estado')
    os.makedirs(cache, exist_ok=True)
    prazo = float(os.environ.get('RADARES_PRAZO_S', '0') or 0)  # GitHub: parar antes do limite e continuar depois
    inicio = float(os.environ.get('RADARES_INICIO', time.time()))
    os.environ['RADARES_INICIO'] = str(inicio)
    for s in ufs + ['SEMAFOROS', 'ANTT']:
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
            r = semaforos_br() if s == 'SEMAFOROS' else antt() if s == 'ANTT' else uf(s)
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
    antt_ids = {(x[0], x[1]) for x in (json.load(open(os.path.join(cache, 'v4_ANTT.json'))) if os.path.isfile(
        os.path.join(cache, 'v4_ANTT.json')) else [])}
    escreve(dest, total, antt_ids)


def escreve(dest, total, antt_ids):
    """Junta (radar da ANTT que o OpenStreetMap já tem sai; ponto repetido vira uma linha) e grava o .csv.gz."""
    # radar da ANTT que o OpenStreetMap já tem (até 80 m, sentido compatível): fica o do OpenStreetMap
    osm = [tuple(x) for x in total if (x[0], x[1]) not in antt_ids]
    grade = {}
    for x in osm:
        grade.setdefault((round(x[0], 2), round(x[1], 2)), []).append(x)

    def ja_tem(x):
        for di in (-0.01, 0, 0.01):
            for dj in (-0.01, 0, 0.01):
                for y in grade.get((round(x[0] + di, 2), round(x[1] + dj, 2)), []):
                    dist = math.hypot((x[0] - y[0]) * 110540, (x[1] - y[1]) * 111320 * math.cos(math.radians(x[0])))
                    if dist < 80 and (x[3] < 0 or y[3] < 0 or abs((x[3] - y[3] + 180) % 360 - 180) < 60):
                        return True
        return False
    novos = [tuple(x) for x in total if (x[0], x[1]) in antt_ids and not ja_tem(x)]
    if antt_ids:
        print(f'ANTT: {len(novos)} radares que o OpenStreetMap não tinha', flush=True)
    total = osm + novos
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
        f.write('# radares OpenStreetMap (ODbL, (c) colaboradores do OpenStreetMap) ' + time.strftime('%Y-%m-%d')
                + ' + ANTT dados abertos (CC-BY)\n')
        for la, lo, lim, d, tipo, eixo in total:
            f.write(f'{la},{lo},{lim},{d},{tipo},{eixo}\n')
    print(f'total: {len(total)} radares -> {dest}')


def main_pbf(pbf, dest):
    pts = antt_pontos()
    d, antt_ways = do_pbf(pbf, pts)
    total = processa(d)
    novos = antt_com_pistas(pts, antt_ways)
    print(f'OpenStreetMap: {len(total)} radares; ANTT: {len(novos)} ({sum(1 for x in novos if x[3] >= 0)} com direção)', flush=True)
    escreve(dest, [tuple(x) for x in total] + novos, {(x[0], x[1]) for x in novos})


if __name__ == '__main__':
    main()
